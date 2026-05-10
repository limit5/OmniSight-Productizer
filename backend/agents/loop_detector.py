"""B3 — 3x-loop detector (OP-830, master plan §2.5).

Tracks per-ticket tool-call signatures. Flags ``reset_required`` when the
model has issued the same ``(tool_name, args_hash, error_class)`` triple
3 times in a row — the canonical signal that the model is stuck in a
non-productive loop (S1 pilot v1-v3 failure mode F1).

Signature definition (per AC #2):

    args_hash = sha256(canonical_json(tool_args))[:16]

Triple match (3x same signature) triggers ``reset_required = True``.

D3 false-positive mitigation (per AC #3): if successive args differ by
≥ ``token_tolerance`` tokens (Levenshtein over whitespace-split
tokens), the run is treated as **progress**, not as a match.
This handles the legitimate "progressive narrowing" pattern where the
model issues ``Glob`` with progressively tighter patterns:
``**/*.py`` → ``backend/**/*.py`` → ``backend/agents/*.py`` — all of
which would hash differently anyway, but the token guard also catches
the rare ``args_hash_collision`` case (see error catalog).

Reset upper bound (per AC #5, FSM critical decision Q1): N=3. After 3
resets, the orchestrator must abort with ``loop_aborted_terminal``.
Total raw attempts ≤ 9 (3 attempts × 3 resets).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# AC #2 / Q1 constants. These are sprint-charter constants, not per-ticket
# knobs (the spec is explicit: "Not configurable per-ticket").
TRIPLE_MATCH_THRESHOLD: int = 3
RESET_LIMIT: int = 3
LEVENSHTEIN_TOKEN_TOLERANCE: int = 1
ARGS_HASH_HEX_LEN: int = 16

# Standard error_class string when a tool returned successfully. Detectors
# only fire when the same error_class repeats — a single failing call
# followed by two successes does not trigger.
ERROR_CLASS_OK: str = "ok"
ERROR_CLASS_UNKNOWN: str = "unknown"

# Internal sentinel name used to mark the boundary in the audit log when
# a reset has been issued. Future records following this marker do NOT
# match historical records (so the rolling window is effectively wiped).
RESET_BOUNDARY_TOOL: str = "<<reset_boundary>>"


@dataclass(frozen=True)
class ToolCallSignature:
    """The (tool_name, args_hash, error_class) triple per AC #1."""

    tool_name: str
    args_hash: str
    error_class: str

    def to_dict(self) -> dict[str, str]:
        return {
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "error_class": self.error_class,
        }

    def render(self) -> str:
        return (
            f"{self.tool_name}(args_hash={self.args_hash}, "
            f"err={self.error_class})"
        )


@dataclass
class _Record:
    """Internal log row — keeps ``canonical_args`` so we can apply the D3
    Levenshtein-tolerance check on consecutive matching-hash records."""

    signature: ToolCallSignature
    canonical_args: str


def canonical_args_json(args: Any) -> str:
    """JSON-canonical representation; stable hash input.

    ``sort_keys=True`` for stable key ordering (so ``{"a":1,"b":2}`` and
    ``{"b":2,"a":1}`` hash identically). ``default=str`` covers the rare
    case where a handler is invoked with a Path / UUID / custom dataclass
    in args — we stringify rather than crash, so the detector keeps
    working even if a tool schema regression slips a non-JSON type into
    ``tool_input``.
    """
    return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def args_hash(args: Any) -> str:
    """SHA256 over canonical JSON, hex-truncated to 16 chars (AC #2)."""
    canonical = canonical_args_json(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:ARGS_HASH_HEX_LEN]


def _token_levenshtein(a: str, b: str) -> int:
    """Distance over whitespace-split tokens — multiset symmetric difference.

    For B3 D3 mitigation we only need to know whether two arg payloads
    differ by ``≥ 1 token``. Multiset symmetric difference is an O(n)
    upper-bound proxy for true Levenshtein that is good enough for the
    progressive-narrowing case where the model swaps a regex/glob/path
    fragment between calls.

    Examples (each line returns ≥ 1):
        "a b c"           vs "a b d"        => 2 (c removed, d added)
        "Glob **/*.py"    vs "Glob **/*.ts" => 2
        "Read /a/b.py"    vs "Read /a/b.py" => 0  (identical, true match)
    """
    ta = a.split()
    tb = b.split()
    if ta == tb:
        return 0
    ca: Counter[str] = Counter(ta)
    cb: Counter[str] = Counter(tb)
    diff = (ca - cb) + (cb - ca)
    return sum(diff.values())


def classify_tool_error(*, is_error: bool, content: str) -> str:
    """Map a ``ToolResult.is_error`` + ``content`` pair to an ``error_class``.

    The dispatcher returns errors as JSON in ``content`` (per
    ``backend.agents.tool_dispatcher._error_result``). We extract the
    ``error`` field which carries the AC-defined error code
    (``bash_metachar_blocked``, ``text_editor_no_match``, etc.).
    Non-errors collapse to ``ok``.
    """
    if not is_error:
        return ERROR_CLASS_OK
    try:
        payload = json.loads(content)
    except (ValueError, json.JSONDecodeError):
        return ERROR_CLASS_UNKNOWN
    if isinstance(payload, dict):
        for key in ("error", "error_type"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
    return ERROR_CLASS_UNKNOWN


class LoopDetector:
    """Per-ticket detector. Records tool calls; flags ``reset_required``."""

    def __init__(
        self,
        *,
        ticket_key: str,
        triple_threshold: int = TRIPLE_MATCH_THRESHOLD,
        token_tolerance: int = LEVENSHTEIN_TOKEN_TOLERANCE,
        reset_limit: int = RESET_LIMIT,
    ) -> None:
        if triple_threshold < 2:
            raise ValueError("triple_threshold must be >= 2 for a meaningful loop check")
        if reset_limit < 1:
            raise ValueError("reset_limit must be >= 1")
        self.ticket_key = ticket_key
        self.triple_threshold = triple_threshold
        self.token_tolerance = token_tolerance
        self.reset_limit = reset_limit
        self._log: list[_Record] = []
        self._reset_count = 0

    # ── Recording ──────────────────────────────────────────────────────

    def record_tool_call(
        self,
        *,
        tool_name: str,
        tool_args: Any,
        error_class: str = ERROR_CLASS_OK,
    ) -> ToolCallSignature:
        """Append a triple to the detector log; return the new signature."""
        canonical = canonical_args_json(tool_args)
        sig = ToolCallSignature(
            tool_name=tool_name,
            args_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:ARGS_HASH_HEX_LEN],
            error_class=error_class,
        )
        self._log.append(_Record(signature=sig, canonical_args=canonical))
        return sig

    # ── Detection ──────────────────────────────────────────────────────

    def is_reset_required(self) -> bool:
        """True iff the last ``triple_threshold`` records in the active
        window are all the same signature AND no consecutive pair differs
        by ≥ ``token_tolerance`` tokens (D3 mitigation per AC #3).

        The "active window" is the suffix of the log since the most
        recent ``RESET_BOUNDARY_TOOL`` marker — so post-reset the
        detector starts counting from zero.
        """
        window = self._active_window()
        if len(window) < self.triple_threshold:
            return False
        tail = window[-self.triple_threshold:]
        first_sig = tail[0].signature
        if not all(r.signature == first_sig for r in tail):
            return False
        # D3: any consecutive pair differing by ≥ token_tolerance tokens
        # counts as progress (not a true loop). This catches both the
        # progressive-narrowing case and the rare args_hash_collision
        # case where two genuinely different inputs hash identically.
        for prev, cur in zip(tail, tail[1:]):
            if _token_levenshtein(prev.canonical_args, cur.canonical_args) >= self.token_tolerance:
                return False
        return True

    def loop_signature(self) -> ToolCallSignature | None:
        """Signature currently looping; ``None`` if log is empty.

        Callers should only invoke this AFTER ``is_reset_required()``
        returned True; otherwise the value is the most recent call,
        not necessarily a looped one.
        """
        window = self._active_window()
        if not window:
            return None
        return window[-1].signature

    # ── Reset orchestration ────────────────────────────────────────────

    def can_reset(self) -> bool:
        """``False`` once ``reset_limit`` has been hit — orchestrator must
        abort with ``loop_aborted_terminal`` (AC #5)."""
        return self._reset_count < self.reset_limit

    def mark_reset(self) -> None:
        """Increment reset counter + drop a boundary marker so the next
        3 calls form a fresh detection window. Records before the
        boundary stay for audit / telemetry (the spec is "append-only").
        """
        self._reset_count += 1
        self._log.append(
            _Record(
                signature=ToolCallSignature(
                    tool_name=RESET_BOUNDARY_TOOL,
                    args_hash="0" * ARGS_HASH_HEX_LEN,
                    error_class=f"reset_{self._reset_count}",
                ),
                canonical_args="",
            )
        )

    @property
    def reset_count(self) -> int:
        return self._reset_count

    @property
    def is_terminal(self) -> bool:
        """``True`` once reset_count has reached the limit. The next
        ``is_reset_required()`` flag should drive the orchestrator
        toward ``loop_aborted_terminal`` rather than another reset."""
        return self._reset_count >= self.reset_limit

    @property
    def log(self) -> list[ToolCallSignature]:
        """All recorded signatures, including reset boundaries (audit)."""
        return [r.signature for r in self._log]

    # ── Internals ──────────────────────────────────────────────────────

    def _active_window(self) -> list[_Record]:
        """Slice of the log since the most recent reset boundary."""
        for idx in range(len(self._log) - 1, -1, -1):
            if self._log[idx].signature.tool_name == RESET_BOUNDARY_TOOL:
                return self._log[idx + 1:]
        return list(self._log)

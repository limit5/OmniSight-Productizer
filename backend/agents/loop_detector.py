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

B16 — Outcomes-graded final attempt (OP-847, operator-opt-in).
The ``OutcomesConfig`` block (plus ``build_outcomes_rubric`` /
``load_outcomes_config`` / ``detect_rubric_goodhart_warnings``) is the
config surface for B16: when ``OMNISIGHT_OUTCOMES_FINAL_ATTEMPT=1`` the
context-reset orchestrator wraps the LAST allowed attempt in an
Outcomes (rubric + Haiku grader) envelope. B3 reset semantics for the
first 2 attempts remain unchanged. See ``docs/research/b3-outcomes-spike-2026-05.md``
for the disjoint-failure-mode analysis that motivated B16.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

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


# ── B16 — Outcomes-graded final attempt (OP-847) ──────────────────────


OUTCOMES_FINAL_ATTEMPT_ENV: str = "OMNISIGHT_OUTCOMES_FINAL_ATTEMPT"
OUTCOMES_GRADER_MODEL_ENV: str = "OMNISIGHT_OUTCOMES_GRADER_MODEL"
DEFAULT_GRADER_MODEL: str = "claude-haiku-4-5"

# Per AC #5: phrases in the AC that historically correlate with grader
# Goodhart-failures. Matched as whole words (so "simplify" doesn't trip
# on "simply"). Char limit catches under-specified one-liner ACs.
GOODHART_TRIGGER_PHRASES: tuple[str, ...] = (
    "just", "simply", "trivial", "obvious",
)
GOODHART_THIN_AC_CHAR_LIMIT: int = 30
OUTCOMES_RUBRIC_THIN_LOG_TAG: str = "[outcomes-rubric-thin]"

# Per AC #2: templated rubric wrapper for non-empty AC sections.
OUTCOMES_RUBRIC_WRAPPER: str = (
    "PASS iff each of the following AC items is verifiable in the diff "
    "or test output:\n\n{ac_text}"
)

# Per AC #2: fallback rubric when AC section is missing / freeform / empty.
OUTCOMES_FALLBACK_RUBRIC: str = (
    "PASS iff the worktree HEAD's tests pass and the AC verification "
    "comment was posted."
)

# Pattern for extracting the JIRA `## Acceptance criteria` block. Case-
# insensitive; matches the canonical markdown header form first, then
# stops at the next ``##`` section or EOF. Bare ``Acceptance criteria``
# (no header marker) is intentionally NOT matched here — it falls into
# the freeform path so the fallback rubric kicks in.
_AC_SECTION_RE = re.compile(
    r"^\s*##\s+acceptance\s+criteria\s*\n(?P<body>.*?)(?=^\s*##\s|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


class OutcomesGraderUnavailable(RuntimeError):
    """Grader call refused (beta header rejected / network / config gap).

    The orchestrator catches this on the final attempt and falls back
    to pure B3 hard-reset semantics — i.e. the runner's result is
    accepted as-is and the rest of the pipeline (critic, push, JIRA
    walk) proceeds. Per AC #1 error catalog.
    """


@dataclass(frozen=True)
class OutcomesVerdict:
    """Verdict returned by the Outcomes grader call.

    ``grader_input_tokens`` / ``grader_output_tokens`` MUST be populated
    so the orchestrator can thread the grader's cost through
    ``_post_call_cost_record`` (AC #4). A grader that does not surface
    a ``usage`` object is treated as ``OutcomesGraderUnavailable``.
    """

    verdict: str  # "pass" | "fail"
    grader_reasoning: str
    grader_input_tokens: int
    grader_output_tokens: int

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


@dataclass(frozen=True)
class OutcomesConfig:
    """Operator-opt-in config for B16 (OP-847).

    ``enabled=False`` is the production default — caller behaviour is
    identical to pure B3 (AC #7 rollback property). ``rubric_warnings``
    surfaces the Goodhart-guard hits from AC #5 so the orchestrator
    can attach them to telemetry / operator notifications without
    re-scanning the rubric text.
    """

    enabled: bool
    rubric: str
    grader_model: str = DEFAULT_GRADER_MODEL
    rubric_warnings: tuple[str, ...] = ()


def extract_acceptance_criteria_section(ticket_description: str) -> str:
    """Pull the verbatim ``## Acceptance criteria`` body from a JIRA
    ticket description. Returns the inner body stripped of surrounding
    whitespace. Returns empty string when no canonical section is
    present (caller should fall back to the templated rubric, AC #2).

    Matches only the markdown-header form ``## Acceptance criteria``
    (case-insensitive). Plain-text headers like ``Acceptance criteria:``
    are intentionally NOT matched — those tickets are typically the
    freeform / under-specified ones the fallback rubric is for.
    """
    match = _AC_SECTION_RE.search(ticket_description or "")
    if match is None:
        return ""
    return match.group("body").strip()


def detect_rubric_goodhart_warnings(ac_text: str) -> list[str]:
    """Per AC #5: scan AC text for grader-failure-prone phrases /
    under-specified one-liners. Returns a list of human-readable
    warning strings (empty list = no Goodhart risk detected).

    Warnings DO NOT block the rubric — the orchestrator still runs the
    grader. They are surfaced so the operator can correct the rubric
    in a follow-up if grader hallucination rates spike.
    """
    warnings: list[str] = []
    if not ac_text:
        return warnings
    lowered = ac_text.lower()
    for phrase in GOODHART_TRIGGER_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            warnings.append(
                f"AC contains grader-failure-prone phrase '{phrase}' — "
                f"rubric may train surface signal over outcome"
            )
    # Per-line thin-AC check: any non-blank line ≤ char limit looks
    # under-specified. Most well-formed ACs are paragraph-length; the
    # short ones are the ones graders over-fit to.
    for raw_line in ac_text.splitlines():
        line = raw_line.strip(" \t-*0123456789.").strip()
        if 0 < len(line) <= GOODHART_THIN_AC_CHAR_LIMIT:
            warnings.append(
                f"AC line under-specified ({len(line)} ≤ "
                f"{GOODHART_THIN_AC_CHAR_LIMIT} chars): {line!r}"
            )
    return warnings


def build_outcomes_rubric(ac_text: str) -> tuple[str, list[str]]:
    """Per AC #2 + #5: build a rubric (templated wrapper or fallback)
    and return the Goodhart-guard warnings alongside.

    The fallback rubric is used when ``ac_text`` is empty / whitespace.
    Warnings are computed over the verbatim AC text (not the wrapper),
    since the wrapper itself is fixed and never matches a Goodhart
    phrase.
    """
    stripped = (ac_text or "").strip()
    if not stripped:
        return OUTCOMES_FALLBACK_RUBRIC, []
    rubric = OUTCOMES_RUBRIC_WRAPPER.format(ac_text=stripped)
    warnings = detect_rubric_goodhart_warnings(stripped)
    return rubric, warnings


def load_outcomes_config(
    *,
    ticket_description: str,
    env: Mapping[str, str] | None = None,
) -> OutcomesConfig:
    """Build an ``OutcomesConfig`` from env + ticket description.

    Per AC #1: ``OMNISIGHT_OUTCOMES_FINAL_ATTEMPT=1`` enables B16.
    Per AC #3: ``OMNISIGHT_OUTCOMES_GRADER_MODEL`` overrides the
    default grader model (``claude-haiku-4-5``).
    Per AC #5: any rubric warnings are logged with the
    ``[outcomes-rubric-thin]`` tag (operator-warn, still run).
    Per AC #7: flag=0 returns a disabled config — caller's existing
    code path (pure B3) is preserved bit-for-bit.
    """
    env = env if env is not None else os.environ
    enabled = env.get(OUTCOMES_FINAL_ATTEMPT_ENV, "0") == "1"
    grader_model = env.get(OUTCOMES_GRADER_MODEL_ENV, DEFAULT_GRADER_MODEL)
    if not enabled:
        return OutcomesConfig(
            enabled=False, rubric="", grader_model=grader_model,
        )
    ac_text = extract_acceptance_criteria_section(ticket_description)
    rubric, warnings = build_outcomes_rubric(ac_text)
    for warning in warnings:
        logger.warning("%s %s", OUTCOMES_RUBRIC_THIN_LOG_TAG, warning)
    return OutcomesConfig(
        enabled=True,
        rubric=rubric,
        grader_model=grader_model,
        rubric_warnings=tuple(warnings),
    )

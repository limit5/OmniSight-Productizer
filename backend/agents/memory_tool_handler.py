"""OP-851 (Sprint C / C1) — Anthropic Memory Tool standalone integration.

This module implements the client-side handler for Anthropic's Memory Tool
(``memory_20260120``), backed by a per-fleet shared filesystem.

Spike result (AC #2)
====================
Memory Tool is **standalone**: the ``memory_20260120`` tool is a
*client-side* tool (the client implements storage; Anthropic only
emits ``tool_use`` blocks). The ``managed-agents-2026-04-01`` beta
header is a *feature flag*, not a runtime gate — sending that header
does NOT require the agent to run inside Anthropic's Managed Agents
runtime. Verified against the public docs at
``platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool``
(referenced 2026-05-11) and the Code w/ Claude 2026 announcement.

If a future Anthropic release re-couples the two (i.e. the tool starts
refusing without a Managed Agents-bound session), the spike abort path
raises :class:`MemoryToolNotStandalone` so the runner can fall back to
B10 BM25 without burning iterations.

Tool spec
=========
Per the public spec, the tool entry is schema-less from the client's
perspective::

    {"type": "memory_20260120", "name": "memory"}

The API beta header that pins this version is ``managed-agents-2026-04-01``.
Both are exported as module constants so callers don't hand-roll them.

Operations
==========
Anthropic emits ``tool_use`` blocks with one of six commands:

* ``view`` — list a directory OR read a file (returns dir listing or
  file body)
* ``create`` — create or overwrite a file
* ``str_replace`` — substring find/replace in a file
* ``insert`` — insert text at a line
* ``delete`` — delete a file
* ``rename`` — rename a file

All paths are MODEL-VISIBLE under ``/memories/...`` and remapped onto the
host storage root (``/var/omnisight/memory/<fleet_id>/``).

Tier-aware filter (AC #6)
=========================
Each memory entry may carry a ``tier:`` label (S/M/L/X) — either as
YAML front-matter ``tier: S`` or via a filename prefix
``tier-S-<rest>.md``. When the model ``view``s a directory:

* ``tier:S`` and ``tier:M`` entries are auto-returned.
* ``tier:L`` entries require ``OMNISIGHT_MEMORY_TIER_L_OPTIN=1``;
  without the opt-in, they are hidden and an audit row is logged.
* ``tier:X`` entries are ALWAYS hidden — auto-recall is refused;
  operator approval is required (out of band).
* Entries without a ``tier:`` label default to ``S`` (lessons-learned
  baseline; see :data:`DEFAULT_TIER`).

When the model ``view``s a single tier-restricted FILE that it isn't
permitted to read, the handler returns ``TierViolationUnauthorizedRecall``
via :func:`_tier_violation_error` and emits one audit row.

Storage cap + eviction (AC #4)
==============================
The fleet directory has a 100MB soft cap (overridable via
``OMNISIGHT_MEMORY_TOOL_CAP_MB``). When a ``create`` or ``str_replace``
would push the directory over the cap, the handler evicts the
oldest-modified files (``min(st_mtime)``) until the cap is restored,
emitting one ``op=evict`` audit row per evicted file. If the inbound
write is by itself larger than the cap, :class:`MemoryStorageFull`
is raised and surfaced as an error tool_result.

Audit log (AC #5)
=================
Every operation appends one JSONL row to ``progress.txt``::

    {"type": "memory_tool", "tool": "memory", "op": "...",
     "key": "/memories/foo.md", "timestamp": "...", "ticket_key": "OP-..."}

The format matches :class:`ToMScratchpad`'s rows so B9 can ingest both
streams from the same file.

Lesson seeding (AC #7)
======================
On first runner start, :func:`seed_lessons_from` walks the per-file
lesson archive (``docs/sop/lessons/L-*.md``) and creates one Memory Tool
entry per lesson, prefixed ``lesson:<id>.md`` so the model can list them
via the ``view`` command (the Anthropic Memory Tool does not currently
support glob-style ``list("lesson:*")`` patterns — directory listing is
the documented surface, which is functionally equivalent for the seed).

Failure modes (AC error catalog)
================================
* :class:`MemoryToolNotStandalone` — Anthropic refused the call without
  Managed Agents runtime (spike abort).
* :class:`MemoryDirNotWritable` — storage root is missing or not writable
  at backend start; fix provisioning before retrying.
* :class:`MemoryStorageFull` — single-file write exceeds the cap.
* :class:`MemoryCorrupted` — index unreadable; caller restores from
  daily backup.
* :class:`TierViolationUnauthorizedRecall` — ``tier:L`` without opt-in,
  or ``tier:X``.
* :class:`MemoryToolUnavailable` — generic API / filesystem error
  surface; orchestrator falls back to B10 BM25.

Fallback contract
=================
On any unexpected exception inside :meth:`MemoryToolHandler.handle`,
the handler returns a structured error tool_result whose ``error``
field is one of the catalog codes above. The orchestrator can then
invoke :func:`backend.agents.lesson_retrieval.retrieve_lessons` as the
documented B10 fallback path.
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Callable
import datetime as _dt
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Callable, Mapping

log = logging.getLogger(__name__)

# ── Tool spec constants (AC #1) ────────────────────────────────────────

MEMORY_TOOL_TYPE = "memory_20260120"
MEMORY_TOOL_NAME = "memory"
MEMORY_TOOL_BETA_HEADER = "managed-agents-2026-04-01"

# Anthropic surfaces this as a schema-less built-in (the model owns the
# input shape). The runner registers the spec by reference; the handler
# below validates the input itself.
MEMORY_TOOL_SPEC: dict[str, str] = {
    "type": MEMORY_TOOL_TYPE,
    "name": MEMORY_TOOL_NAME,
}

# Model-visible path prefix. Anthropic documents this convention so the
# model cannot wander into the host filesystem — every operation must
# start with /memories/.
MEMORY_PATH_PREFIX = "/memories"

# Default per-fleet storage root. Overridable by the runner / tests.
DEFAULT_STORAGE_ROOT = Path("/var/omnisight/memory")
DEFAULT_CAP_MB = 100
TIER_L_OPTIN_ENV = "OMNISIGHT_MEMORY_TIER_L_OPTIN"
CAP_MB_ENV = "OMNISIGHT_MEMORY_TOOL_CAP_MB"
STORAGE_ROOT_ENV = "OMNISIGHT_MEMORY_TOOL_ROOT"

# Tier labels we recognise. Anything outside this set is treated as
# missing -> DEFAULT_TIER ("S").
VALID_TIERS: frozenset[str] = frozenset({"S", "M", "L", "X"})
DEFAULT_TIER = "S"

# Audit row "type" field — keeps multiplexing with B3 ToM scratchpad
# clean inside the same progress.txt file.
AUDIT_TYPE = "memory_tool"

# Error catalog codes (AC #2 — Error catalog block).
ERR_NOT_STANDALONE = "memory_tool_not_standalone"
ERR_STORAGE_FULL = "memory_storage_full"
ERR_DIR_NOT_WRITABLE = "memory_dir_not_writable"
ERR_CORRUPTED = "memory_corrupted"
ERR_TIER_VIOLATION = "tier_violation_unauthorized_recall"
ERR_UNAVAILABLE = "memory_tool_unavailable"
ERR_BAD_INPUT = "memory_bad_input"

# Recognised tier label patterns.
#   YAML front-matter: tier: S  (case-insensitive)
#   Filename prefix:   tier-S-...  (case-insensitive)
_FRONTMATTER_TIER_RE = re.compile(
    r"^\s*tier\s*:\s*([SMLX])\s*$", re.IGNORECASE | re.MULTILINE
)
_FILENAME_TIER_RE = re.compile(r"^tier-([SMLX])-", re.IGNORECASE)


# ── Exceptions ─────────────────────────────────────────────────────────


class MemoryToolError(Exception):
    """Base class — has an ``error_code`` so the dispatcher can render
    a structured tool_result."""

    error_code: str = ERR_UNAVAILABLE

    def to_tool_result(self) -> dict[str, Any]:
        return {"error": self.error_code, "message": str(self)}


class MemoryToolNotStandalone(MemoryToolError):
    """Anthropic refused the call without a Managed Agents runtime."""

    error_code = ERR_NOT_STANDALONE


class MemoryDirNotWritable(MemoryToolError):
    """Storage root missing or not writable at backend start."""

    error_code = ERR_DIR_NOT_WRITABLE


class MemoryStorageFull(MemoryToolError):
    """Single-file write exceeds the cap even after eviction."""

    error_code = ERR_STORAGE_FULL


class MemoryCorrupted(MemoryToolError):
    """Backing file unreadable / index mismatch."""

    error_code = ERR_CORRUPTED


class TierViolationUnauthorizedRecall(MemoryToolError):
    """tier:L without opt-in OR tier:X (merged C1 storage + C6 policy semantics).

    C6 extension: carries an ``escalate`` flag so callers can route
    ``tier:X`` refusals to the operator pager while ``tier:L`` refusals
    are merely logged. The runner promotes ``escalate=True`` refusals
    to a notify-operator hook in ``scripts/run_s1_via_anthropic_sdk.py``.

    Backward-compat with C1: when constructed without args, behaves as
    a plain ``MemoryToolError`` with ``error_code = ERR_TIER_VIOLATION``.
    """

    error_code = ERR_TIER_VIOLATION

    def __init__(
        self,
        tier: "MemoryTier | str | None" = None,
        reason: str | None = None,
        *,
        escalate: bool = False,
    ) -> None:
        if tier is None and reason is None:
            super().__init__("tier violation")
        else:
            tier_val = getattr(tier, "value", tier)
            super().__init__(f"recall refused for tier:{tier_val}: {reason}")
        self.tier = tier
        self.reason = reason
        self.escalate = escalate


class MemoryToolUnavailable(MemoryToolError):
    """Generic API / filesystem error — fall back to B10."""

    error_code = ERR_UNAVAILABLE


# ── Audit log ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuditRow:
    """One row in ``progress.txt`` for B9 ingestion."""

    op: str  # read | write | delete | list | rename | evict | tier_refuse
    key: str
    timestamp: str
    ticket_key: str | None = None
    extra: dict[str, Any] | None = None

    def to_jsonl(self) -> str:
        payload: dict[str, Any] = {
            "type": AUDIT_TYPE,
            "tool": MEMORY_TOOL_NAME,
            "op": self.op,
            "key": self.key,
            "timestamp": self.timestamp,
            "ticket_key": self.ticket_key,
        }
        if self.extra:
            payload.update(self.extra)
        return json.dumps(payload, ensure_ascii=False)


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ── Path mapping ───────────────────────────────────────────────────────


def _resolve_model_path(storage_root: Path, model_path: str) -> Path:
    """Map ``/memories/<rel>`` -> ``<storage_root>/<rel>`` with traversal guards.

    Raises :class:`MemoryToolError` (error_code ``memory_bad_input``)
    when the path is malformed or escapes the storage root.
    """
    if not isinstance(model_path, str) or not model_path:
        err = MemoryToolError("path must be a non-empty string")
        err.error_code = ERR_BAD_INPUT
        raise err
    p = model_path.strip()
    if p == MEMORY_PATH_PREFIX:
        rel = ""
    elif p.startswith(MEMORY_PATH_PREFIX + "/"):
        rel = p[len(MEMORY_PATH_PREFIX) + 1 :]
    else:
        err = MemoryToolError(
            f"path must start with {MEMORY_PATH_PREFIX!r}, got {model_path!r}"
        )
        err.error_code = ERR_BAD_INPUT
        raise err

    # Block traversal / absolute / drive-letter shenanigans before
    # joining onto the storage root.
    if ".." in Path(rel).parts or Path(rel).is_absolute():
        err = MemoryToolError(f"path escapes storage root: {model_path!r}")
        err.error_code = ERR_BAD_INPUT
        raise err

    candidate = (storage_root / rel).resolve()
    root_resolved = storage_root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        err = MemoryToolError(f"path escapes storage root: {model_path!r}")
        err.error_code = ERR_BAD_INPUT
        raise err from exc
    return candidate


# ── Tier classification ────────────────────────────────────────────────


def classify_tier(*, content: str | None, filename: str) -> str:
    """Return the tier label for a memory entry.

    Order of precedence: YAML front-matter tier > filename prefix >
    :data:`DEFAULT_TIER`. Unrecognised labels fall back to default.
    """
    if content:
        m = _FRONTMATTER_TIER_RE.search(content)
        if m:
            label = m.group(1).upper()
            if label in VALID_TIERS:
                return label
    fm = _FILENAME_TIER_RE.match(filename)
    if fm:
        label = fm.group(1).upper()
        if label in VALID_TIERS:
            return label
    return DEFAULT_TIER


def tier_is_recallable(tier: str, *, tier_l_optin: bool) -> bool:
    """Return True iff the runner may auto-recall this tier."""
    if tier in {"S", "M"}:
        return True
    if tier == "L":
        return tier_l_optin
    return False  # tier == "X" or unknown


# ── Handler config ─────────────────────────────────────────────────────


@dataclass
class MemoryToolConfig:
    """All knobs the runner can twist at construction time.

    Values default to env-var overrides so the call-site in the
    SDK launcher stays one line.
    """

    fleet_id: str = "default"
    storage_root: Path = field(default=DEFAULT_STORAGE_ROOT)
    cap_mb: int = DEFAULT_CAP_MB
    tier_l_optin: bool = False
    progress_path: Path | None = None
    ticket_key: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        fleet_id: str = "default",
        progress_path: Path | None = None,
        ticket_key: str | None = None,
    ) -> "MemoryToolConfig":
        root_env = os.environ.get(STORAGE_ROOT_ENV)
        root = Path(root_env) if root_env else DEFAULT_STORAGE_ROOT
        cap_env = os.environ.get(CAP_MB_ENV)
        try:
            cap = int(cap_env) if cap_env else DEFAULT_CAP_MB
        except ValueError:
            log.warning("invalid %s=%r — falling back to default", CAP_MB_ENV, cap_env)
            cap = DEFAULT_CAP_MB
        return cls(
            fleet_id=fleet_id,
            storage_root=root / fleet_id,
            cap_mb=cap,
            tier_l_optin=os.environ.get(TIER_L_OPTIN_ENV) == "1",
            progress_path=progress_path,
            ticket_key=ticket_key,
        )


# ── Handler ────────────────────────────────────────────────────────────


class MemoryToolHandler:
    """Client-side implementation of the Anthropic Memory Tool.

    Plug into a :class:`ToolDispatcher` via
    ``dispatcher.register("memory", handler)`` (the tool's ``name``).
    The dispatcher's contract calls ``await handler(tool_input)``;
    we implement ``__call__`` to make instances coroutine-callable.
    """

    def __init__(self, config: MemoryToolConfig) -> None:
        self.config = config
        try:
            self.config.storage_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryDirNotWritable(
                f"memory storage root is not creatable: {self.config.storage_root}"
            ) from exc
        if not os.access(self.config.storage_root, os.W_OK | os.X_OK):
            raise MemoryDirNotWritable(
                f"memory storage root is not writable: {self.config.storage_root}"
            )

    # — Public surface —

    async def __call__(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        return self.handle(tool_input)

    def handle(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one tool_use payload to the matching command."""
        command = tool_input.get("command")
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        try:
            if command == "view":
                return self._cmd_view(path, tool_input)
            if command == "create":
                return self._cmd_create(path, tool_input)
            if command == "str_replace":
                return self._cmd_str_replace(path, tool_input)
            if command == "insert":
                return self._cmd_insert(path, tool_input)
            if command == "delete":
                return self._cmd_delete(path, tool_input)
            if command == "rename":
                return self._cmd_rename(path, tool_input)
            err = MemoryToolError(f"unknown memory command: {command!r}")
            err.error_code = ERR_BAD_INPUT
            raise err
        except MemoryToolError as exc:
            self._audit(op=f"error:{command}", key=path, extra={
                "error": exc.error_code, "message": str(exc)[:500],
            })
            return exc.to_tool_result()
        except Exception as exc:  # noqa: BLE001 — boundary surface
            log.exception("memory tool %r raised", command)
            err = MemoryToolUnavailable(
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            self._audit(op=f"error:{command}", key=path, extra={
                "error": err.error_code, "message": str(err)[:500],
            })
            return err.to_tool_result()

    # — Spike: standalone verification (AC #2) —

    def verify_standalone(self) -> None:
        """Verify the Memory Tool can be used WITHOUT Managed Agents runtime.

        The spike result lives in the module docstring (file-based
        client-side tool, beta header is a flag). This method is a
        runtime guard: it raises :class:`MemoryToolNotStandalone` if
        a future API change demands a Managed Agents session, so the
        runner can fall back to B10 cleanly instead of looping on
        opaque 400 errors.

        The check is conservative — we look for an explicit operator
        kill-switch env (``OMNISIGHT_MEMORY_TOOL_REQUIRES_MA=1``) so
        operators can pin the abort path during a vendor incident.
        """
        if os.environ.get("OMNISIGHT_MEMORY_TOOL_REQUIRES_MA") == "1":
            raise MemoryToolNotStandalone(
                "operator-set kill-switch: Memory Tool flagged as "
                "requiring Managed Agents runtime; falling back to B10."
            )

    # — Tool list registration helper —

    @staticmethod
    def tool_spec() -> dict[str, str]:
        """Return the Anthropic tools=[] entry for the runner to splat in."""
        return dict(MEMORY_TOOL_SPEC)

    @staticmethod
    def beta_header() -> str:
        return MEMORY_TOOL_BETA_HEADER

    # — Commands —

    def _cmd_view(
        self, path: str, _tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        target = _resolve_model_path(self.config.storage_root, path)
        if not target.exists():
            self._audit(op="read", key=path, extra={"missing": True})
            return {"error": "not_found", "path": path}

        if target.is_dir():
            entries = []
            for child in sorted(target.iterdir()):
                tier = classify_tier(
                    content=None if child.is_dir() else _safe_read_text(child),
                    filename=child.name,
                )
                if not tier_is_recallable(
                    tier, tier_l_optin=self.config.tier_l_optin
                ):
                    self._audit(
                        op="tier_refuse",
                        key=f"{path}/{child.name}",
                        extra={"tier": tier},
                    )
                    continue
                entries.append({
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "size": child.stat().st_size if child.is_file() else None,
                    "tier": tier,
                })
            self._audit(op="list", key=path, extra={"count": len(entries)})
            return {"entries": entries}

        body = _safe_read_text(target)
        if body is None:
            raise MemoryCorrupted(f"unreadable file: {path}")
        tier = classify_tier(content=body, filename=target.name)
        if not tier_is_recallable(tier, tier_l_optin=self.config.tier_l_optin):
            self._audit(op="tier_refuse", key=path, extra={"tier": tier})
            raise TierViolationUnauthorizedRecall(
                f"tier:{tier} entry blocked from auto-recall: {path}"
            )
        self._audit(op="read", key=path, extra={"tier": tier})
        return {"content": body, "tier": tier}

    def _cmd_create(
        self, path: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        body = tool_input.get("file_text") or tool_input.get("content") or ""
        if not isinstance(body, str):
            err = MemoryToolError("file_text must be a string")
            err.error_code = ERR_BAD_INPUT
            raise err
        target = _resolve_model_path(self.config.storage_root, path)
        self._ensure_capacity(len(body.encode("utf-8")), target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        tier = classify_tier(content=body, filename=target.name)
        self._audit(
            op="write", key=path,
            extra={"bytes": len(body.encode("utf-8")), "tier": tier},
        )
        return {"ok": True, "path": path, "tier": tier}

    def _cmd_str_replace(
        self, path: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        old = tool_input.get("old_str", "")
        new = tool_input.get("new_str", "")
        if not isinstance(old, str) or not isinstance(new, str):
            err = MemoryToolError("old_str/new_str must be strings")
            err.error_code = ERR_BAD_INPUT
            raise err
        target = _resolve_model_path(self.config.storage_root, path)
        body = _safe_read_text(target)
        if body is None:
            raise MemoryCorrupted(f"file not readable: {path}")
        if old not in body:
            return {"error": "old_str_not_found", "path": path}
        new_body = body.replace(old, new, 1)
        delta = len(new_body.encode("utf-8")) - len(body.encode("utf-8"))
        if delta > 0:
            self._ensure_capacity(delta, target)
        target.write_text(new_body, encoding="utf-8")
        self._audit(op="write", key=path, extra={"delta_bytes": delta})
        return {"ok": True, "path": path}

    def _cmd_insert(
        self, path: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        line = tool_input.get("insert_line")
        text = tool_input.get("text") or tool_input.get("insert_text") or ""
        if not isinstance(line, int) or line < 0:
            err = MemoryToolError("insert_line must be a non-negative int")
            err.error_code = ERR_BAD_INPUT
            raise err
        if not isinstance(text, str):
            err = MemoryToolError("text must be a string")
            err.error_code = ERR_BAD_INPUT
            raise err
        target = _resolve_model_path(self.config.storage_root, path)
        body = _safe_read_text(target) or ""
        lines = body.splitlines()
        line = min(line, len(lines))
        lines.insert(line, text)
        new_body = "\n".join(lines)
        delta = len(new_body.encode("utf-8")) - len(body.encode("utf-8"))
        if delta > 0:
            self._ensure_capacity(delta, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_body, encoding="utf-8")
        self._audit(op="write", key=path, extra={"insert_line": line})
        return {"ok": True, "path": path}

    def _cmd_delete(
        self, path: str, _tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        target = _resolve_model_path(self.config.storage_root, path)
        if not target.exists():
            return {"error": "not_found", "path": path}
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        self._audit(op="delete", key=path)
        return {"ok": True, "path": path}

    def _cmd_rename(
        self, path: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        new_path = tool_input.get("new_path", "")
        if not isinstance(new_path, str) or not new_path:
            err = MemoryToolError("new_path must be a non-empty string")
            err.error_code = ERR_BAD_INPUT
            raise err
        src = _resolve_model_path(self.config.storage_root, path)
        dst = _resolve_model_path(self.config.storage_root, new_path)
        if not src.exists():
            return {"error": "not_found", "path": path}
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        self._audit(op="rename", key=path, extra={"new_path": new_path})
        return {"ok": True, "path": new_path}

    # — Eviction (AC #4) —

    def _current_bytes(self) -> int:
        total = 0
        for child in self.config.storage_root.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
        return total

    def _ensure_capacity(self, incoming_bytes: int, exclude: Path) -> None:
        cap_bytes = self.config.cap_mb * 1024 * 1024
        if incoming_bytes > cap_bytes:
            raise MemoryStorageFull(
                f"single write {incoming_bytes}B > cap {cap_bytes}B "
                f"({self.config.cap_mb}MB)"
            )
        used = self._current_bytes()
        if used + incoming_bytes <= cap_bytes:
            return
        self._audit(
            op="MemoryCapExceeded",
            key=f"{MEMORY_PATH_PREFIX}/",
            extra={
                "used_bytes": used,
                "incoming_bytes": incoming_bytes,
                "cap_bytes": cap_bytes,
            },
        )
        # Oldest-first eviction.
        candidates = sorted(
            (
                p for p in self.config.storage_root.rglob("*")
                if p.is_file() and p != exclude
            ),
            key=lambda p: p.stat().st_mtime,
        )
        # The incoming/rewritten file becomes part of the final newest-3
        # safety floor, so protect the newest two existing files.
        protected = set(candidates[-2:])
        for victim in candidates:
            if used + incoming_bytes <= cap_bytes:
                break
            if victim in protected:
                continue
            try:
                size = victim.stat().st_size
            except OSError:
                continue
            try:
                rel = victim.relative_to(self.config.storage_root)
            except ValueError:
                continue
            try:
                victim.unlink()
            except OSError as exc:
                log.warning("eviction failed for %s: %s", victim, exc)
                continue
            used -= size
            self._audit(
                op="evict",
                key=f"{MEMORY_PATH_PREFIX}/{rel.as_posix()}",
                extra={"bytes": size},
            )
        if used + incoming_bytes > cap_bytes:
            raise MemoryStorageFull(
                f"unable to free enough space — used={used}B, "
                f"incoming={incoming_bytes}B, cap={cap_bytes}B; "
                "newest 3 files preserved"
            )

    # — Audit log (AC #5) —

    def _audit(
        self,
        *,
        op: str,
        key: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = AuditRow(
            op=op,
            key=key,
            timestamp=_utcnow_iso(),
            ticket_key=self.config.ticket_key,
            extra=extra,
        )
        path = self.config.progress_path
        if path is None:
            log.debug("memory audit (no progress path): %s", row.to_jsonl())
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(row.to_jsonl() + "\n")
        except OSError as exc:
            log.warning("memory audit write failed: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def seed_lessons_from(
    handler: MemoryToolHandler,
    lessons_dir: Path,
    *,
    overwrite: bool = False,
) -> int:
    """Seed lessons-learned content into Memory Tool storage (AC #7).

    Walks ``<lessons_dir>/L-*.md`` and creates one entry per lesson at
    ``/memories/lesson:<basename>.md``. Returns the count of entries
    written (existing entries are skipped unless ``overwrite=True``).

    The ``lesson:`` prefix lets the model list seeded entries via
    ``view`` on the root directory and pattern-match the name; it
    is the seam C2 (failure-class-indexed memory) plugs into.
    """
    if not lessons_dir.is_dir():
        log.warning("seed_lessons_from: %s is not a directory", lessons_dir)
        return 0
    written = 0
    for lesson_path in sorted(lessons_dir.glob("L-*.md")):
        body = _safe_read_text(lesson_path)
        if body is None:
            continue
        key_name = f"lesson:{lesson_path.stem}.md"
        target = handler.config.storage_root / key_name
        if target.exists() and not overwrite:
            continue
        try:
            handler.handle({
                "command": "create",
                "path": f"{MEMORY_PATH_PREFIX}/{key_name}",
                "file_text": body,
            })
        except MemoryToolError as exc:
            log.warning("seed_lessons_from: %s failed: %s", lesson_path, exc)
            continue
        written += 1
    return written


def list_seeded_lessons(handler: MemoryToolHandler) -> list[str]:
    """Return the names of seeded ``lesson:*`` entries.

    Convenience for the DoD check ``memory.list("lesson:*")`` — wraps
    a ``view`` on the root and filters to the ``lesson:`` prefix.
    Tier-restricted entries are excluded by the underlying ``view``.
    """
    result = handler.handle({
        "command": "view",
        "path": MEMORY_PATH_PREFIX,
    })
    entries = result.get("entries") if isinstance(result, dict) else None
    if not entries:
        return []
    return [
        e["name"] for e in entries
        if isinstance(e, dict) and str(e.get("name", "")).startswith("lesson:")
    ]


def build_memory_tool_handler(
    *,
    fleet_id: str = "default",
    progress_path: Path | None = None,
    ticket_key: str | None = None,
    seed_dir: Path | None = None,
) -> MemoryToolHandler | None:
    """Build + spike-check + (optionally) seed in one call.

    Returns ``None`` when the spike check fails — the caller should
    treat that as "Memory Tool unavailable; fall back to B10".
    """
    config = MemoryToolConfig.from_env(
        fleet_id=fleet_id,
        progress_path=progress_path,
        ticket_key=ticket_key,
    )
    handler = MemoryToolHandler(config)
    try:
        handler.verify_standalone()
    except MemoryToolNotStandalone as exc:
        log.warning("Memory Tool not standalone: %s — fallback to B10", exc)
        return None
    if seed_dir is not None and seed_dir.is_dir():
        try:
            count = seed_lessons_from(handler, seed_dir)
            log.info("seeded %s lesson entries from %s", count, seed_dir)
        except Exception as exc:  # noqa: BLE001 - seeding is best-effort
            log.warning("lesson seeding failed: %s", exc)
    return handler

# ══════════════════════════════════════════════════════════════════════
# C6 (OP-856) — Tier-aware Memory recall policy layer
# ══════════════════════════════════════════════════════════════════════
#
# Policy seam that sits on top of the C1 storage handler above.
# Covers S/M/L/X recall rules + cross-fleet federation + audit emit.
# Imported by backend/agents/incident_recorder.py and
# backend/tests/test_memory_tier_policy.py. C1 storage code does NOT
# call into this layer directly; the runner orchestrates both.

class MemoryTier(str, Enum):
    """JIRA-style tier label attached to a memory record."""

    S = "S"
    M = "M"
    L = "L"
    X = "X"

    @classmethod
    def parse(cls, raw: str | "MemoryTier") -> "MemoryTier":
        if isinstance(raw, cls):
            return raw
        token = (raw or "").strip().upper().removeprefix("TIER:")
        try:
            return cls(token)
        except ValueError as exc:
            raise UnknownMemoryTier(token) from exc


class UnknownMemoryTier(ValueError):
    """Raised when a recall request carries a tier label outside S/M/L/X."""



class CrossFleetRecallRefused(TierViolationUnauthorizedRecall):
    """Raised specifically when cross-fleet federation env is missing."""

    def __init__(self, tier: MemoryTier, query_fleet: str, target_fleet: str):
        super().__init__(
            tier,
            f"cross-fleet recall {query_fleet!r}->{target_fleet!r} refused; "
            "set OMNISIGHT_MEMORY_FEDERATION to opt in",
            escalate=False,
        )
        self.query_fleet = query_fleet
        self.target_fleet = target_fleet


class MemoryAuditWriteFailed(RuntimeError):
    """Raised internally when the audit-write side-effect fails.

    Caught at the policy boundary so the recall remains fail-open;
    the caller never sees this exception.
    """


# ─── Env-driven runtime knobs ──────────────────────────────────────



FEDERATION_ENV = "OMNISIGHT_MEMORY_FEDERATION"
"""Comma-separated list of fleet ids whose memory namespaces this
runner is permitted to recall *across*. Order does not matter; empty
or unset disables federation entirely."""


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(env_value: str | None) -> bool:
    if env_value is None:
        return False
    return env_value.strip().lower() in _TRUTHY


def parse_federation(env_value: str | None) -> frozenset[str]:
    """Parse ``OMNISIGHT_MEMORY_FEDERATION`` into a fleet-id set.

    Whitespace is stripped, blanks are dropped. Returned set is frozen
    so downstream consumers cannot mutate the runtime federation view.
    """
    if not env_value:
        return frozenset()
    parts = (chunk.strip() for chunk in env_value.split(","))
    return frozenset(p for p in parts if p)


# ─── Policy decision ───────────────────────────────────────────────


@dataclass(frozen=True)
class RecallRequest:
    """One memory.recall(query, tier, fleet) invocation.

    ``query_fleet`` is the caller's own fleet (fleet of the runner
    issuing the recall). ``target_fleet`` is the namespace being
    queried; when they differ this is a cross-fleet recall and falls
    under the federation rule.
    """

    query: str
    tier: MemoryTier
    query_fleet: str
    target_fleet: str

    @property
    def is_cross_fleet(self) -> bool:
        return self.query_fleet != self.target_fleet


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of ``evaluate_recall``.

    * ``permitted`` — whether the recall may execute.
    * ``escalate`` — true when the operator must be paged (tier:X
      refusal). Refusals at tier:M / tier:L do not escalate because
      they reflect a missing opt-in, not a policy violation.
    * ``refusal_reason`` — populated only when permitted is false;
      mirrors the message attached to the raised exception. Useful
      for audit rows on refusals.
    * ``audit_summary`` — pre-formatted summary string for the
      ``runner_incidents.summary`` column.
    """

    permitted: bool
    tier: MemoryTier
    escalate: bool
    refusal_reason: str | None
    audit_summary: str


# Audit emission seam. Default points at ``incident_recorder``; tests
# inject an in-memory recorder to keep the policy module dependency-
# free at import time. The signature must match
# ``incident_recorder.record_memory_recall_audit``.
AuditEmitter = Callable[[RecallRequest, PolicyDecision], None]


def _default_audit_emitter(request: RecallRequest, decision: PolicyDecision) -> None:
    # Local import keeps the policy module importable even if the
    # incident_recorder shim is unavailable (e.g. early-boot or a
    # narrowly-scoped unit test).
    from backend.agents import incident_recorder

    incident_recorder.record_memory_recall_audit(request, decision)


def evaluate_recall(
    request: RecallRequest,
    *,
    env: Mapping[str, str] | None = None,
) -> PolicyDecision:
    """Resolve a recall request to a permit/refuse decision.

    Pure function — does not raise on policy refusal (caller does that
    via :func:`enforce_recall`) and does not perform the audit-write
    side-effect. The split lets tests assert the decision shape
    without coupling to the audit recorder.
    """
    env = env if env is not None else os.environ

    tier = request.tier
    cross = request.is_cross_fleet

    if tier is MemoryTier.S:
        return _allow(request, escalate=False, note="tier:S unrestricted")

    if tier is MemoryTier.M:
        if not cross:
            return _allow(request, escalate=False, note="tier:M same-fleet")
        federation = parse_federation(env.get(FEDERATION_ENV))
        if request.target_fleet in federation:
            return _allow(
                request,
                escalate=False,
                note=f"tier:M cross-fleet via federation={sorted(federation)}",
            )
        return _refuse(
            request,
            escalate=False,
            reason=(
                f"cross-fleet recall {request.query_fleet!r}->"
                f"{request.target_fleet!r} refused; "
                f"set {FEDERATION_ENV} to opt in"
            ),
        )

    if tier is MemoryTier.L:
        if not _is_truthy(env.get(TIER_L_OPTIN_ENV)):
            return _refuse(
                request,
                escalate=False,
                reason=f"tier:L recall refused; set {TIER_L_OPTIN_ENV}=1 to opt in",
            )
        if cross:
            federation = parse_federation(env.get(FEDERATION_ENV))
            if request.target_fleet not in federation:
                return _refuse(
                    request,
                    escalate=False,
                    reason=(
                        f"tier:L cross-fleet recall refused; "
                        f"set {FEDERATION_ENV} to opt in"
                    ),
                )
        # tier:L permitted recalls always escalate to operator-visible
        # audit (per AC #1 escalation level distinguished from C1's
        # basic refusal). Escalation here = "operator review", not
        # "page", since the recall itself was opted in.
        return _allow(
            request,
            escalate=True,
            note="tier:L opt-in honoured; per-recall audit",
        )

    # tier:X — always refused, always escalate (operator pager).
    if tier is MemoryTier.X:
        return _refuse(
            request,
            escalate=True,
            reason="tier:X recall refused; operator authorization required",
        )

    raise UnknownMemoryTier(tier)  # type: ignore[arg-type]


def enforce_recall(
    request: RecallRequest,
    *,
    env: Mapping[str, str] | None = None,
    audit_emitter: AuditEmitter | None = None,
) -> PolicyDecision:
    """Evaluate, audit, and raise on refusal.

    Audit-write happens **before** raising / returning — the C6 spec
    requires audit-write happen-before semantics so refusals leave a
    paper trail even when the caller short-circuits. Audit failures
    are caught and logged (fail-open per ``MemoryAuditWriteFailed``);
    the original decision still drives the return path.
    """
    decision = evaluate_recall(request, env=env)
    emit = audit_emitter or _default_audit_emitter
    try:
        emit(request, decision)
    except MemoryAuditWriteFailed as exc:
        log.warning(
            "memory_recall_audit_write_failed: tier=%s permitted=%s err=%s",
            decision.tier.value,
            decision.permitted,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        log.warning(
            "memory_recall_audit_unexpected_error: tier=%s permitted=%s err=%s",
            decision.tier.value,
            decision.permitted,
            exc,
        )

    if not decision.permitted:
        if (
            request.is_cross_fleet
            and request.tier is MemoryTier.M
            and decision.refusal_reason
            and FEDERATION_ENV in decision.refusal_reason
        ):
            raise CrossFleetRecallRefused(
                request.tier, request.query_fleet, request.target_fleet
            )
        raise TierViolationUnauthorizedRecall(
            request.tier,
            decision.refusal_reason or "policy refused",
            escalate=decision.escalate,
        )
    return decision


def tier_filter(
    records: Iterable[Mapping[str, object]],
    *,
    request: RecallRequest,
    env: Mapping[str, str] | None = None,
    audit_emitter: AuditEmitter | None = None,
) -> list[Mapping[str, object]]:
    """C1's recall-time filter, extended with the C6 policy.

    ``records`` is the raw set of memory rows the storage layer would
    return. The function consults :func:`enforce_recall` to decide
    permit/refuse; on refuse it raises (matches C1's existing
    contract). On permit, the records pass through unchanged — the
    tier policy is *gate*, not *re-rank*.

    Forward compatibility with C1: when C1's broader memory_tool
    handler lands, it should call ``tier_filter(records, request=...)``
    immediately after retrieval and before returning to the model.
    """
    enforce_recall(request, env=env, audit_emitter=audit_emitter)
    return list(records)


def _allow(request: RecallRequest, *, escalate: bool, note: str) -> PolicyDecision:
    return PolicyDecision(
        permitted=True,
        tier=request.tier,
        escalate=escalate,
        refusal_reason=None,
        audit_summary=_format_audit_summary(request, "permitted", note),
    )


def _refuse(request: RecallRequest, *, escalate: bool, reason: str) -> PolicyDecision:
    return PolicyDecision(
        permitted=False,
        tier=request.tier,
        escalate=escalate,
        refusal_reason=reason,
        audit_summary=_format_audit_summary(request, "refused", reason),
    )


def _format_audit_summary(request: RecallRequest, outcome: str, note: str) -> str:
    return (
        f"recall query={request.query!r} tier={request.tier.value} "
        f"fleet={request.query_fleet}->{request.target_fleet} "
        f"outcome={outcome} note={note}"
    )


__all__ = [
    "AuditEmitter",
    "CrossFleetRecallRefused",
    "FEDERATION_ENV",
    "MemoryAuditWriteFailed",
    "MemoryTier",
    "PolicyDecision",
    "RecallRequest",
    "TIER_L_OPTIN_ENV",
    "TierViolationUnauthorizedRecall",
    "UnknownMemoryTier",
    "enforce_recall",
    "evaluate_recall",
    "parse_federation",
    "tier_filter",
]

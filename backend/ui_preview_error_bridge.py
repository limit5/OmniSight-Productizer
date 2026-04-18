"""V2 #5 (issue #318) — Preview error bridge.

Streams compile / runtime errors out of the sandbox dev server, parses
them into structured error objects, and packages them into an
agent-consumable context so the next ReAct turn can fix the bug
automatically and re-screenshot.

Where this sits in the V2 stack
--------------------------------

V2 #1 (``ui_sandbox.py``) exposes :func:`parse_compile_error` — a
best-effort parser for Next.js / Vite / CRA compile errors emitted on
stderr.  V2 #1 also exposes :meth:`SandboxManager.logs` which tails
the dev server's combined stdout/stderr via the Docker CLI.  The
primitive parser is deliberately stateless: it takes a string, returns
a tuple of :class:`~backend.ui_sandbox.CompileError`.

V2 #2 (``ui_sandbox_lifecycle.py``) brings session-scoped policy over
V2 #1 — ensure / teardown / wait-ready / screenshot-hook / reap.  It
owns the lifetime of each sandbox.

V2 #3 (``ui_screenshot.py``) is the Playwright side of the
screenshot hook — takes a running preview and turns it into PNG bytes.

V2 #4 (``ui_responsive_viewport.py``) fans out the hook across three
viewports in one call.

V2 #5 (this module) closes the auto-fix loop.  It:

  * **Polls** the sandbox's logs via :meth:`SandboxManager.logs`.
  * **Parses** them with :func:`~backend.ui_sandbox.parse_compile_error`
    plus this module's :func:`parse_runtime_error` (for React /
    Next.js runtime exceptions that show up on stdout once the dev
    server is live).
  * **Diffs** against a per-session tracker so the same error doesn't
    re-fire events every sweep.  New errors emit
    ``ui_sandbox.error.detected``; errors that were last scan and are
    gone this scan emit ``ui_sandbox.error.cleared``.
  * **Bundles** active errors into an
    :class:`AgentContextPayload` — a markdown summary + structured
    JSON + ``auto_fix_hint`` prompt fragment the ReAct loop stitches
    into its next turn.
  * **Watches** a session on a background thread (opt-in via
    :meth:`start_watch`) so the agent can ask
    :meth:`has_active_errors` any time without blocking on I/O.
  * **Acknowledge** lets the agent mark a fixed error as seen before
    the next scan proves it — useful when the agent fixes a syntax
    error and wants to suppress the stale error in the next multimodal
    prompt.

Design decisions
----------------

* **Composition over inheritance.**  :class:`PreviewErrorBridge`
  *holds* a :class:`~backend.ui_sandbox.SandboxManager`; it does not
  subclass.  Mirrors V2 #2 / V2 #3 / V2 #4.  Lets the bridge run
  alongside :class:`~backend.ui_sandbox_lifecycle.SandboxLifecycle`
  without layering inheritance.
* **Stateful but deterministic.**  Each session keeps a dict of
  ``error_id → PreviewError`` with occurrence counts + first/last
  seen.  Every mutation goes through the lifecycle lock.  Tests
  drive a ``FakeClock`` so ``first_seen_at`` / ``last_seen_at`` /
  ``scanned_at`` are reproducible.
* **Stable error IDs.**  :func:`hash_error` produces a short,
  content-derived SHA-256 prefix from ``(source, error_type,
  message, file, line)``.  Same bug ⇒ same ID across sweeps ⇒ dedup
  works.  The agent uses this ID when calling
  :meth:`acknowledge`.
* **Runtime parser is best-effort.**  Returns empty tuple for empty
  / unrecognised input, never raises.  Runtime errors use
  ``error_type="runtime/<kind>"`` so V2 row 6 SSE subscribers can
  filter compile vs runtime without inspecting the message body.
* **Watch is opt-in.**  Production callers invoke :meth:`start_watch`
  per session; tests call :meth:`scan` synchronously.  The watch
  thread sleeps on ``threading.Event.wait`` so :meth:`stop_watch`
  returns in <100 ms regardless of interval.
* **Graceful log failure.**  ``manager.logs`` raising or returning
  empty never crashes the bridge — the scan returns an empty batch
  with the exception shunted into ``ErrorBatch.warnings``.
* **No agent / LLM coupling.**  This module does not import
  ``llm_adapter`` / Opus SDK / event bus internals.  It *produces*
  an :class:`AgentContextPayload` the orchestration layer injects;
  it does not inject itself.
* **No side effects on sandbox.**  The bridge never calls
  ``touch`` / ``stop`` / ``teardown`` — it's read-only against the
  sandbox manager.  Auto-fix is driven by the agent, not the bridge.

Contract (pinned by ``backend/tests/test_ui_preview_error_bridge.py``)
---------------------------------------------------------------------

* :data:`UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION` is semver; bump on
  shape changes to :class:`PreviewError` / :class:`ErrorBatch` /
  :class:`AgentContextPayload` ``to_dict()``.
* Event names live in the ``ui_sandbox.error.*`` namespace — distinct
  from V2 #2's ``ui_sandbox.ensure_session`` / V2 #3's
  ``ui_sandbox.screenshot``.
* :class:`ErrorSource` is a :class:`str`-:class:`enum.Enum` with
  values ``compile`` and ``runtime``.
* :class:`PreviewError.error_id` is stable across sweeps: same
  ``(source, error_type, message, file, line)`` tuple ⇒ identical
  ID byte-for-byte.
* :meth:`PreviewErrorBridge.scan` never raises — every failure
  (missing session, log fetch error, parser blow-up) surfaces as a
  ``warnings`` entry on the returned :class:`ErrorBatch`.
* :meth:`start_watch` is single-instance per ``session_id`` —
  re-entry raises :class:`WatchAlreadyRunning`.
* :meth:`acknowledge` is idempotent — acknowledging an already-cleared
  error returns ``False`` without raising.
* :meth:`__exit__` stops every active watch and clears per-session
  state so the bridge can be used as a ``with`` block inside agent
  loops with SIGINT handlers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

from backend.ui_sandbox import (
    CompileError,
    SandboxInstance,
    SandboxManager,
    parse_compile_error,
)

logger = logging.getLogger(__name__)


__all__ = [
    "UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION",
    "DEFAULT_LOG_TAIL",
    "DEFAULT_WATCH_INTERVAL_S",
    "DEFAULT_MAX_ERRORS_PER_SESSION",
    "DEFAULT_MAX_EXCERPT_CHARS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_LEVELS",
    "ERROR_EVENT_DETECTED",
    "ERROR_EVENT_CLEARED",
    "ERROR_EVENT_BATCH",
    "ERROR_EVENT_CONTEXT_BUILT",
    "ERROR_EVENT_WATCH_STARTED",
    "ERROR_EVENT_WATCH_STOPPED",
    "ERROR_EVENT_TYPES",
    "ErrorSource",
    "PreviewError",
    "ErrorBatch",
    "AgentContextPayload",
    "LogSource",
    "PreviewErrorBridgeError",
    "WatchAlreadyRunning",
    "WatchNotRunning",
    "parse_runtime_error",
    "hash_error",
    "classify_severity",
    "combine_errors",
    "render_error_markdown",
    "build_auto_fix_hint",
    "PreviewErrorBridge",
]


#: Bump on shape changes to :class:`PreviewError` / :class:`ErrorBatch`
#: / :class:`AgentContextPayload` ``to_dict()`` output.
UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION = "1.0.0"

#: How many log lines :meth:`PreviewErrorBridge.scan` asks
#: :meth:`SandboxManager.logs` for by default.  500 is enough to catch
#: the tail of a big webpack stack without paging the whole buffer.
DEFAULT_LOG_TAIL = 500

#: Default interval between background watch-loop sweeps.  1.5 s is
#: slower than the ready-detector in V2 #2 (0.5 s) — the watch is only
#: interesting once the server is up and errors tend to persist for
#: many sweeps so the agent doesn't benefit from sub-second cadence.
DEFAULT_WATCH_INTERVAL_S = 1.5

#: Upper bound on how many distinct errors we retain per session
#: before dropping the oldest.  Prevents unbounded growth when a dev
#: server spews thousands of warnings a minute.
DEFAULT_MAX_ERRORS_PER_SESSION = 50

#: Cap on how many characters of the raw log excerpt we keep per
#: error.  Keeps SSE frames / Opus multimodal messages bounded.
DEFAULT_MAX_EXCERPT_CHARS = 2000


#: Severity level for blocking errors (prevents render).
SEVERITY_ERROR = "error"

#: Severity level for non-blocking warnings (page still renders).
SEVERITY_WARNING = "warning"

#: Frozen tuple of legal severity values.
SEVERITY_LEVELS: tuple[str, ...] = (SEVERITY_ERROR, SEVERITY_WARNING)


ERROR_EVENT_DETECTED = "ui_sandbox.error.detected"
ERROR_EVENT_CLEARED = "ui_sandbox.error.cleared"
ERROR_EVENT_BATCH = "ui_sandbox.error.batch"
ERROR_EVENT_CONTEXT_BUILT = "ui_sandbox.error.context_built"
ERROR_EVENT_WATCH_STARTED = "ui_sandbox.error.watch_started"
ERROR_EVENT_WATCH_STOPPED = "ui_sandbox.error.watch_stopped"

#: Full roster of events this module emits — callers wiring the SSE
#: bus (V2 row 6) use this tuple for deterministic topic subscription.
ERROR_EVENT_TYPES: tuple[str, ...] = (
    ERROR_EVENT_DETECTED,
    ERROR_EVENT_CLEARED,
    ERROR_EVENT_BATCH,
    ERROR_EVENT_CONTEXT_BUILT,
    ERROR_EVENT_WATCH_STARTED,
    ERROR_EVENT_WATCH_STOPPED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class PreviewErrorBridgeError(RuntimeError):
    """Base class for preview-error-bridge errors."""


class WatchAlreadyRunning(PreviewErrorBridgeError):
    """Raised by :meth:`PreviewErrorBridge.start_watch` when a watch
    thread is already live for ``session_id``."""


class WatchNotRunning(PreviewErrorBridgeError):
    """Raised by :meth:`PreviewErrorBridge.stop_watch` when no watch
    thread exists for ``session_id`` and ``missing_ok=False``."""


# ───────────────────────────────────────────────────────────────────
#  Enums + dataclasses
# ───────────────────────────────────────────────────────────────────


class ErrorSource(str, Enum):
    """Where the error was observed.

    ``compile`` errors come from the bundler / transpiler stage
    (Next.js / Vite / CRA webpack) and typically show up before the
    preview is reachable.  ``runtime`` errors come from the running
    app — React hydration, unhandled promise rejections, browser
    console errors surfaced via the dev overlay.
    """

    compile = "compile"
    runtime = "runtime"


class LogSource(str, Enum):
    """Which stream a line was observed on.  ``combined`` is the
    default when the bridge pulls merged stdout/stderr from
    :meth:`SandboxManager.logs`."""

    stdout = "stdout"
    stderr = "stderr"
    combined = "combined"


@dataclass(frozen=True)
class PreviewError:
    """Structured view of one dev-server error the bridge is tracking.

    Combines the parsed :class:`~backend.ui_sandbox.CompileError`
    shape with session-scoped metadata (stable ID, first / last seen,
    occurrence count, raw excerpt) so the agent loop can both:

      * **render** a deterministic markdown summary, and
      * **diff** between scans to spot which errors are brand new
        (emit ``ui_sandbox.error.detected``) and which persist.
    """

    session_id: str
    error_id: str
    message: str
    source: ErrorSource
    error_type: str
    severity: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    occurrences: int = 1
    raw_excerpt: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.error_id, str) or not self.error_id.strip():
            raise ValueError("error_id must be a non-empty string")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if not isinstance(self.source, ErrorSource):
            raise ValueError(
                f"source must be ErrorSource, got {type(self.source).__name__}"
            )
        if not isinstance(self.error_type, str) or not self.error_type.strip():
            raise ValueError("error_type must be a non-empty string")
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(
                f"severity must be one of {SEVERITY_LEVELS!r}, got {self.severity!r}"
            )
        if self.line is not None and (
            not isinstance(self.line, int) or self.line < 0
        ):
            raise ValueError(f"line must be non-negative int, got {self.line!r}")
        if self.column is not None and (
            not isinstance(self.column, int) or self.column < 0
        ):
            raise ValueError(f"column must be non-negative int, got {self.column!r}")
        if self.first_seen_at < 0 or self.last_seen_at < 0:
            raise ValueError("timestamps must be non-negative")
        if self.last_seen_at < self.first_seen_at:
            raise ValueError(
                f"last_seen_at ({self.last_seen_at}) must be >= "
                f"first_seen_at ({self.first_seen_at})"
            )
        if not isinstance(self.occurrences, int) or self.occurrences < 1:
            raise ValueError("occurrences must be a positive int")
        if not isinstance(self.raw_excerpt, str):
            raise ValueError("raw_excerpt must be a string")

    @property
    def is_compile(self) -> bool:
        return self.source is ErrorSource.compile

    @property
    def is_runtime(self) -> bool:
        return self.source is ErrorSource.runtime

    @property
    def is_blocking(self) -> bool:
        return self.severity == SEVERITY_ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "error_id": self.error_id,
            "message": self.message,
            "source": self.source.value,
            "error_type": self.error_type,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "first_seen_at": float(self.first_seen_at),
            "last_seen_at": float(self.last_seen_at),
            "occurrences": int(self.occurrences),
            "raw_excerpt": self.raw_excerpt,
        }


@dataclass(frozen=True)
class ErrorBatch:
    """Result of one :meth:`PreviewErrorBridge.scan` sweep.

    ``detected`` holds errors that were **new** this sweep (not in the
    prior state snapshot).  ``cleared`` holds error IDs that were in
    the prior snapshot and are absent now.  ``active`` is the full
    current set (detected + still-present persisters).
    """

    session_id: str
    scanned_at: float
    detected: tuple[PreviewError, ...] = ()
    cleared: tuple[str, ...] = ()
    active: tuple[PreviewError, ...] = ()
    log_chars_scanned: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if self.scanned_at < 0:
            raise ValueError("scanned_at must be non-negative")
        if not isinstance(self.log_chars_scanned, int) or self.log_chars_scanned < 0:
            raise ValueError("log_chars_scanned must be non-negative")
        for item in self.detected:
            if not isinstance(item, PreviewError):
                raise ValueError("detected items must be PreviewError")
        for item in self.active:
            if not isinstance(item, PreviewError):
                raise ValueError("active items must be PreviewError")
        for item in self.cleared:
            if not isinstance(item, str) or not item:
                raise ValueError("cleared items must be non-empty strings")
        object.__setattr__(self, "detected", tuple(self.detected))
        object.__setattr__(self, "cleared", tuple(self.cleared))
        object.__setattr__(self, "active", tuple(self.active))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def detected_count(self) -> int:
        return len(self.detected)

    @property
    def cleared_count(self) -> int:
        return len(self.cleared)

    @property
    def active_count(self) -> int:
        return len(self.active)

    @property
    def has_activity(self) -> bool:
        """``True`` if this sweep produced any diff at all."""

        return bool(self.detected) or bool(self.cleared)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "scanned_at": float(self.scanned_at),
            "detected": [e.to_dict() for e in self.detected],
            "cleared": list(self.cleared),
            "active": [e.to_dict() for e in self.active],
            "detected_count": self.detected_count,
            "cleared_count": self.cleared_count,
            "active_count": self.active_count,
            "log_chars_scanned": int(self.log_chars_scanned),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AgentContextPayload:
    """Agent-consumable bundle of current errors for one session.

    Produced by :meth:`PreviewErrorBridge.build_agent_context`.  The
    orchestration layer (not this module) pastes this into the next
    ReAct turn's system / user prompt.  Shape is JSON-safe and stable.
    """

    session_id: str
    built_at: float
    errors: tuple[PreviewError, ...]
    summary_markdown: str
    auto_fix_hint: str
    turn_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if self.built_at < 0:
            raise ValueError("built_at must be non-negative")
        for item in self.errors:
            if not isinstance(item, PreviewError):
                raise ValueError("errors items must be PreviewError")
        if not isinstance(self.summary_markdown, str):
            raise ValueError("summary_markdown must be a string")
        if not isinstance(self.auto_fix_hint, str):
            raise ValueError("auto_fix_hint must be a string")
        if self.turn_id is not None and (
            not isinstance(self.turn_id, str) or not self.turn_id.strip()
        ):
            raise ValueError("turn_id must be non-empty string or None")
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def has_blocking_errors(self) -> bool:
        return any(e.is_blocking for e in self.errors)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "built_at": float(self.built_at),
            "error_count": self.error_count,
            "has_blocking_errors": self.has_blocking_errors,
            "errors": [e.to_dict() for e in self.errors],
            "summary_markdown": self.summary_markdown,
            "auto_fix_hint": self.auto_fix_hint,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


# Runtime error heuristic.  We match the *opening* token of each
# runtime-error flavour we want to surface:
#
#   * ``Uncaught TypeError: foo is undefined``
#   * ``[Error] TypeError: foo is undefined``
#   * ``Warning: Each child in a list should have a unique "key" prop``
#   * ``unhandledRejection: Error: fetch failed``
#   * ``[hmr] Failed to reload /foo.tsx``
#   * ``Error: Hydration failed because the initial UI does not match``
#
# The trailing ``(?=:)`` / ``(?=$)`` lookaheads keep us out of false
# positives like "ErrorBoundary" class names.
_RUNTIME_TRIGGER_RE = re.compile(
    r"(?P<type>"
    r"Uncaught\s+(?:TypeError|ReferenceError|SyntaxError|RangeError)|"
    r"\[Error\]\s+(?:TypeError|ReferenceError|SyntaxError|RangeError)|"
    r"unhandledRejection|"
    r"Hydration\s+failed|"
    r"React\s+(?:Error|Warning)|"
    r"Warning(?=:)|"
    r"\[hmr\]\s+(?:Failed|failed)|"
    r"Failed\s+to\s+fetch|"
    r"TypeError|"
    r"ReferenceError|"
    r"RangeError"
    r")",
    re.IGNORECASE,
)

# Stack-frame file:line:col — matches ``at foo (file.tsx:10:5)`` or
# bare ``file.tsx:10:5``.  Restricts to JS/TS/CSS extensions so we
# don't mistake "Error: 2024:12:01" for a stack frame.
_RUNTIME_FRAME_RE = re.compile(
    r"(?P<file>(?:\.{1,2}/|/)?[A-Za-z0-9_.\-/\\]+\.(?:tsx?|jsx?|mjs|cjs|css|scss))"
    r"(?::(?P<line>\d+)(?::(?P<col>\d+))?)?"
)

# Compile-error types we treat as blocking (severity=error).  Runtime
# parser always emits severity=error for thrown exceptions but may
# emit warning for ``Warning:`` prefixed lines (React strict-mode
# notices).
_WARNING_ERROR_TYPES = frozenset({"warning", "react_warning", "hmr_warning"})

# Max payload emitted on ``ui_sandbox.error.batch`` when detecting
# activity — callers SSE-serialise it, keep it lean.
_MAX_BATCH_EVENT_ERRORS = 25


def parse_runtime_error(
    log_text: str,
    *,
    max_errors: int = DEFAULT_MAX_ERRORS_PER_SESSION,
) -> tuple[CompileError, ...]:
    """Best-effort parse of React / Next.js runtime errors in ``log_text``.

    Returns :class:`~backend.ui_sandbox.CompileError` instances (the
    shape is reusable for runtime errors — message + optional file /
    line / column + error_type) with ``error_type`` namespaced under
    ``runtime/``.  Empty / ``None`` input returns an empty tuple;
    never raises.

    Why reuse ``CompileError``: ``parse_compile_error`` already pins
    the same shape for V2 row 4, and keeping both parsers emitting
    the same dataclass means the bridge can concatenate their output
    without a wrapping step.  The bridge layer re-classifies each
    entry into a :class:`PreviewError` with ``source=runtime`` before
    persisting.
    """

    if not log_text or not isinstance(log_text, str):
        return ()

    lines = log_text.splitlines()
    out: list[CompileError] = []
    seen: set[tuple[str, str | None, int | None]] = set()

    i = 0
    while i < len(lines):
        line = lines[i]
        trigger = _RUNTIME_TRIGGER_RE.search(line)
        if not trigger:
            i += 1
            continue

        matched_token = trigger.group("type").strip()
        error_type = _runtime_error_type(matched_token)
        message = line.strip()

        # Look for a file:line:col within this line or the next 5.
        frame = _RUNTIME_FRAME_RE.search(line)
        scan_limit = min(len(lines), i + 6)
        j = i + 1
        while frame is None and j < scan_limit:
            frame = _RUNTIME_FRAME_RE.search(lines[j])
            j += 1

        if frame is not None:
            file = frame.group("file")
            raw_line = frame.group("line")
            raw_col = frame.group("col")
            line_no = int(raw_line) if raw_line else None
            col_no = int(raw_col) if raw_col else None
        else:
            file, line_no, col_no = None, None, None

        key = (message, file, line_no)
        if key in seen:
            i += 1
            continue
        seen.add(key)
        out.append(
            CompileError(
                message=message,
                file=file,
                line=line_no,
                column=col_no,
                error_type=error_type,
            )
        )
        if len(out) >= max_errors:
            break
        i += 1

    return tuple(out)


def _runtime_error_type(raw: str) -> str:
    """Normalise a runtime-trigger regex match into an ``error_type``
    string.  Lowercased + whitespace collapsed + prefixed with
    ``runtime/``."""

    cleaned = re.sub(r"\s+", "_", raw.strip().lower())
    cleaned = cleaned.strip("[]")
    # Strip the `[error]_` prefix so `TypeError` shows up as
    # `runtime/typeerror` not `runtime/[error]_typeerror`.
    cleaned = re.sub(r"^(?:\[error\]_|uncaught_)", "", cleaned)
    return f"runtime/{cleaned or 'unknown'}"


def hash_error(
    *,
    source: ErrorSource | str,
    error_type: str,
    message: str,
    file: str | None = None,
    line: int | None = None,
) -> str:
    """Stable, short content-hash of the error's identity fields.

    Same input ⇒ same output across processes / Python versions.  We
    SHA-256 a deterministic string and return the first 12 hex chars —
    enough entropy for dedup within a session (< 50 concurrent errors)
    without bloating event payloads.
    """

    if isinstance(source, ErrorSource):
        source_val = source.value
    else:
        if not isinstance(source, str):
            raise TypeError("source must be ErrorSource or str")
        source_val = source
    if not isinstance(error_type, str):
        raise TypeError("error_type must be a string")
    if not isinstance(message, str):
        raise TypeError("message must be a string")
    if file is not None and not isinstance(file, str):
        raise TypeError("file must be a string or None")
    if line is not None and not isinstance(line, int):
        raise TypeError("line must be an int or None")

    key = "\x1f".join(
        [
            source_val,
            error_type,
            message.strip(),
            file or "",
            "" if line is None else str(line),
        ]
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return digest[:12]


def classify_severity(error_type: str) -> str:
    """Map a parsed ``error_type`` to :data:`SEVERITY_ERROR` or
    :data:`SEVERITY_WARNING`.

    Runtime ``Warning:`` lines (React strict-mode notices, prop-type
    warnings) degrade to non-blocking severity so the agent can
    render+screenshot anyway while still surfacing the issue in
    context.  Everything else is blocking.
    """

    if not isinstance(error_type, str):
        return SEVERITY_ERROR
    lowered = error_type.lower()
    for token in _WARNING_ERROR_TYPES:
        if token in lowered:
            return SEVERITY_WARNING
    if lowered.startswith("runtime/warning") or lowered == "runtime/warning":
        return SEVERITY_WARNING
    return SEVERITY_ERROR


def combine_errors(
    compile_errors: Sequence[CompileError],
    runtime_errors: Sequence[CompileError],
) -> tuple[tuple[CompileError, ErrorSource], ...]:
    """Merge compile + runtime error tuples, preserving order.

    Returns ``(error, source)`` pairs so the bridge can stamp each
    one with the right source when building :class:`PreviewError`.
    Dedup happens at the bridge layer using :func:`hash_error` —
    this helper is purely a concatenator.
    """

    combined: list[tuple[CompileError, ErrorSource]] = []
    for err in compile_errors:
        if not isinstance(err, CompileError):
            raise TypeError("compile_errors items must be CompileError")
        combined.append((err, ErrorSource.compile))
    for err in runtime_errors:
        if not isinstance(err, CompileError):
            raise TypeError("runtime_errors items must be CompileError")
        combined.append((err, ErrorSource.runtime))
    return tuple(combined)


def render_error_markdown(errors: Sequence[PreviewError]) -> str:
    """Deterministic markdown summary of ``errors`` for agent context.

    Shape::

        ### Preview errors (N active)

        | # | Source | Type | File:Line:Col | Message |
        |---|--------|------|---------------|---------|
        | 1 | compile | module_not_found | ./a.tsx:10:5 | ... |

    Empty input returns a stable "no active errors" body so the agent
    can still paste the block without branching.
    """

    if not errors:
        return "### Preview errors\n\nNo active errors.\n"

    rows: list[str] = [
        f"### Preview errors ({len(errors)} active)",
        "",
        "| # | Source | Severity | Type | Location | Message |",
        "|---|--------|----------|------|----------|---------|",
    ]
    for idx, err in enumerate(errors, start=1):
        location: str
        if err.file:
            parts = [err.file]
            if err.line is not None:
                parts.append(str(err.line))
                if err.column is not None:
                    parts.append(str(err.column))
            location = ":".join(parts)
        else:
            location = "(unknown)"
        safe_message = _escape_markdown_cell(err.message)
        rows.append(
            "| {idx} | {source} | {severity} | {type_} | {loc} | {msg} |".format(
                idx=idx,
                source=err.source.value,
                severity=err.severity,
                type_=err.error_type,
                loc=location,
                msg=safe_message,
            )
        )
    return "\n".join(rows) + "\n"


def _escape_markdown_cell(text: str) -> str:
    """Escape pipes + newlines so ``text`` fits inside a markdown
    table cell."""

    if not text:
        return ""
    return text.replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()


def build_auto_fix_hint(errors: Sequence[PreviewError]) -> str:
    """Produce the agent-facing prompt fragment nudging the next turn
    to fix the current errors.

    The fragment is deliberately short + structured — the agent loop
    pastes it into the ReAct prompt alongside the screenshot.  Shape
    is deterministic so retrievability / evaluation tests can pin it.
    """

    if not errors:
        return (
            "The preview rendered cleanly — no compile or runtime "
            "errors detected.  Proceed with the next design task."
        )

    blocking = [e for e in errors if e.is_blocking]
    warnings = [e for e in errors if not e.is_blocking]
    pieces: list[str] = []
    pieces.append(
        f"The sandbox preview is reporting {len(errors)} active error"
        f"{'s' if len(errors) != 1 else ''} "
        f"({len(blocking)} blocking, {len(warnings)} warning"
        f"{'s' if len(warnings) != 1 else ''})."
    )
    pieces.append(
        "Read each entry in the Preview errors table and write targeted "
        "code edits that resolve the listed files + lines.  After the "
        "next hot-reload, the bridge will re-scan and a fresh screenshot "
        "will be captured automatically."
    )
    first = errors[0]
    where = first.file or "the source file referenced above"
    locator = where
    if first.line is not None:
        locator = f"{where}:{first.line}"
    pieces.append(
        f"Start with the first blocking error in {locator} — clearing it "
        "usually unlocks the rest."
    )
    return " ".join(pieces)


# ───────────────────────────────────────────────────────────────────
#  Bridge
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class _LogsProvider(Protocol):
    def logs(self, session_id: str, *, tail: int | None = ...) -> str: ...

    def get(self, session_id: str) -> SandboxInstance | None: ...


@dataclass
class _WatchState:
    thread: threading.Thread
    stop_event: threading.Event
    interval_s: float
    started_at: float
    sweeps: int = 0
    failures: int = 0


class PreviewErrorBridge:
    """Per-session tracker that turns dev-server logs into structured
    agent context.

    Usage::

        bridge = PreviewErrorBridge(manager=sandbox_manager, event_cb=bus)
        # Manual scan (tests / on-demand)
        batch = bridge.scan("sess-1")
        # Background watch (production)
        bridge.start_watch("sess-1")
        ...
        ctx = bridge.build_agent_context("sess-1", turn_id="react-42")
        next_turn_prompt.extend(ctx.summary_markdown, ctx.auto_fix_hint)
        if bridge.acknowledge("sess-1", error_id="abc123"):
            ...
        bridge.stop_watch("sess-1")

    Thread-safe.  All mutations go through :attr:`_lock`.  Every
    mutable seam (``clock`` / ``sleep`` / ``event_cb``) is injectable
    so tests are deterministic.
    """

    def __init__(
        self,
        *,
        manager: SandboxManager,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        event_cb: EventCallback | None = None,
        log_tail: int = DEFAULT_LOG_TAIL,
        watch_interval_s: float = DEFAULT_WATCH_INTERVAL_S,
        max_errors_per_session: int = DEFAULT_MAX_ERRORS_PER_SESSION,
        max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    ) -> None:
        if not isinstance(manager, SandboxManager):
            raise TypeError("manager must be a SandboxManager")
        if not isinstance(log_tail, int) or log_tail <= 0:
            raise ValueError("log_tail must be a positive int")
        if watch_interval_s <= 0:
            raise ValueError("watch_interval_s must be positive")
        if max_errors_per_session <= 0:
            raise ValueError("max_errors_per_session must be positive")
        if max_excerpt_chars <= 0:
            raise ValueError("max_excerpt_chars must be positive")

        self._manager = manager
        self._clock = clock
        self._sleep = sleep
        self._event_cb = event_cb
        self._log_tail = int(log_tail)
        self._watch_interval_s = float(watch_interval_s)
        self._max_errors = int(max_errors_per_session)
        self._max_excerpt_chars = int(max_excerpt_chars)

        self._lock = threading.RLock()
        # session_id → {error_id: PreviewError}
        self._state: dict[str, dict[str, PreviewError]] = {}
        # Last batch per session for introspection / tests.
        self._last_batch: dict[str, ErrorBatch] = {}
        # Session id → watch state for background threads.
        self._watches: dict[str, _WatchState] = {}
        # Counters for operator telemetry.
        self._scan_count = 0
        self._detected_total = 0
        self._cleared_total = 0

    # ─────────────── Public properties ───────────────

    @property
    def manager(self) -> SandboxManager:
        return self._manager

    @property
    def watch_interval_s(self) -> float:
        return self._watch_interval_s

    @property
    def log_tail(self) -> int:
        return self._log_tail

    @property
    def max_errors_per_session(self) -> int:
        return self._max_errors

    def scan_count(self) -> int:
        with self._lock:
            return self._scan_count

    def detected_total(self) -> int:
        with self._lock:
            return self._detected_total

    def cleared_total(self) -> int:
        with self._lock:
            return self._cleared_total

    # ─────────────── Scan ───────────────

    def scan(
        self,
        session_id: str,
        *,
        tail: int | None = None,
    ) -> ErrorBatch:
        """Pull logs, parse, diff against prior state, emit events.

        Never raises — every failure surfaces on
        :attr:`ErrorBatch.warnings`.  Empty log ⇒ empty batch +
        no events (also clears stale errors if any).
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        effective_tail = int(tail) if tail is not None else self._log_tail
        if effective_tail <= 0:
            raise ValueError("tail must be a positive int")

        warnings: list[str] = []
        log_text = ""
        try:
            log_text = self._manager.logs(session_id, tail=effective_tail) or ""
        except Exception as exc:
            warnings.append(f"logs_fetch_failed: {exc}")
            log_text = ""

        compile_errors: tuple[CompileError, ...] = ()
        runtime_errors: tuple[CompileError, ...] = ()
        try:
            compile_errors = parse_compile_error(log_text)
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"compile_parse_failed: {exc}")
        try:
            runtime_errors = parse_runtime_error(
                log_text, max_errors=self._max_errors
            )
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(f"runtime_parse_failed: {exc}")

        now = self._clock()
        excerpt_template = log_text[-self._max_excerpt_chars :] if log_text else ""
        combined = combine_errors(compile_errors, runtime_errors)

        # Dedup across sources: the V2 #1 compile-trigger regex matches
        # "TypeError:" which overlaps with the runtime parser's
        # "Uncaught TypeError:".  When the same ``(message, file, line)``
        # surfaces in both parsers, runtime wins — it's the more specific
        # classification (the line was actually thrown at runtime, not
        # during bundling).
        deduped: list[tuple[CompileError, ErrorSource]] = []
        seen_content: dict[tuple[str, str | None, int | None], int] = {}
        for err, source in combined:
            key = (err.message.strip(), err.file, err.line)
            if key in seen_content:
                idx = seen_content[key]
                existing_source = deduped[idx][1]
                if (
                    existing_source is ErrorSource.compile
                    and source is ErrorSource.runtime
                ):
                    deduped[idx] = (err, source)
                continue
            seen_content[key] = len(deduped)
            deduped.append((err, source))

        parsed_by_id: dict[str, PreviewError] = {}
        for err, source in deduped:
            severity = classify_severity(err.error_type)
            error_id = hash_error(
                source=source,
                error_type=err.error_type,
                message=err.message,
                file=err.file,
                line=err.line,
            )
            if error_id in parsed_by_id:
                # Already saw this exact error in this scan — bump
                # occurrence count (happens when the same trace appears
                # multiple times in the tailed log window).
                existing = parsed_by_id[error_id]
                parsed_by_id[error_id] = replace(
                    existing,
                    occurrences=existing.occurrences + 1,
                    last_seen_at=now,
                )
                continue
            parsed_by_id[error_id] = PreviewError(
                session_id=session_id,
                error_id=error_id,
                message=err.message,
                source=source,
                error_type=err.error_type,
                severity=severity,
                file=err.file,
                line=err.line,
                column=err.column,
                first_seen_at=now,
                last_seen_at=now,
                occurrences=1,
                raw_excerpt=excerpt_template,
            )

        detected: list[PreviewError] = []
        cleared: list[str] = []

        with self._lock:
            self._scan_count += 1
            prior = self._state.setdefault(session_id, {})
            # Diff: new vs persisted vs removed.
            new_state: dict[str, PreviewError] = {}
            for error_id, current in parsed_by_id.items():
                old = prior.get(error_id)
                if old is None:
                    new_state[error_id] = current
                    detected.append(current)
                else:
                    # Preserve first_seen_at + bump occurrences + last_seen_at.
                    merged = replace(
                        current,
                        first_seen_at=old.first_seen_at,
                        last_seen_at=now,
                        occurrences=old.occurrences + current.occurrences,
                    )
                    new_state[error_id] = merged
            for error_id in prior.keys():
                if error_id not in parsed_by_id:
                    cleared.append(error_id)

            # Enforce per-session cap — drop oldest by first_seen_at.
            if len(new_state) > self._max_errors:
                sorted_items = sorted(
                    new_state.values(), key=lambda e: e.first_seen_at
                )
                overflow = len(new_state) - self._max_errors
                to_drop = sorted_items[:overflow]
                for dropped in to_drop:
                    new_state.pop(dropped.error_id, None)

            self._state[session_id] = new_state
            self._detected_total += len(detected)
            self._cleared_total += len(cleared)

            active_sorted = tuple(
                sorted(
                    new_state.values(),
                    key=lambda e: (0 if e.is_blocking else 1, e.first_seen_at),
                )
            )
            batch = ErrorBatch(
                session_id=session_id,
                scanned_at=now,
                detected=tuple(detected),
                cleared=tuple(cleared),
                active=active_sorted,
                log_chars_scanned=len(log_text),
                warnings=tuple(warnings),
            )
            self._last_batch[session_id] = batch

        # Emit outside the lock — event callbacks should never deadlock
        # the bridge.
        for err in detected:
            self._emit(ERROR_EVENT_DETECTED, err.to_dict())
        for error_id in cleared:
            self._emit(
                ERROR_EVENT_CLEARED,
                {
                    "session_id": session_id,
                    "error_id": error_id,
                    "cleared_at": now,
                    "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
                },
            )
        if batch.has_activity:
            # Truncate the `active` list in the event payload — SSE
            # subscribers render the first N and drop the rest.
            event_payload = batch.to_dict()
            if len(event_payload["active"]) > _MAX_BATCH_EVENT_ERRORS:
                event_payload["active"] = event_payload["active"][
                    :_MAX_BATCH_EVENT_ERRORS
                ]
                event_payload["active_truncated"] = True
            else:
                event_payload["active_truncated"] = False
            self._emit(ERROR_EVENT_BATCH, event_payload)
        return batch

    # ─────────────── Agent context ───────────────

    def build_agent_context(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
    ) -> AgentContextPayload:
        """Bundle active errors into an :class:`AgentContextPayload`.

        The orchestration layer pastes ``summary_markdown`` and
        ``auto_fix_hint`` into the next ReAct turn.  If no errors are
        active the payload still renders (``has_errors=False``,
        ``auto_fix_hint`` reads "preview rendered cleanly") so the
        agent loop doesn't special-case the empty path.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if turn_id is not None and (
            not isinstance(turn_id, str) or not turn_id.strip()
        ):
            raise ValueError("turn_id must be a non-empty string or None")

        with self._lock:
            errors = tuple(
                sorted(
                    self._state.get(session_id, {}).values(),
                    key=lambda e: (0 if e.is_blocking else 1, e.first_seen_at),
                )
            )
            now = self._clock()

        summary = render_error_markdown(errors)
        hint = build_auto_fix_hint(errors)
        payload = AgentContextPayload(
            session_id=session_id,
            built_at=now,
            errors=errors,
            summary_markdown=summary,
            auto_fix_hint=hint,
            turn_id=turn_id,
        )
        self._emit(ERROR_EVENT_CONTEXT_BUILT, payload.to_dict())
        return payload

    # ─────────────── State queries ───────────────

    def active_errors(self, session_id: str) -> tuple[PreviewError, ...]:
        """Current active errors for ``session_id``, sorted
        (blocking first, then first_seen_at ascending)."""

        with self._lock:
            return tuple(
                sorted(
                    self._state.get(session_id, {}).values(),
                    key=lambda e: (0 if e.is_blocking else 1, e.first_seen_at),
                )
            )

    def has_active_errors(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._state.get(session_id))

    def get_error(self, session_id: str, error_id: str) -> PreviewError | None:
        with self._lock:
            return self._state.get(session_id, {}).get(error_id)

    def tracked_sessions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._state.keys()))

    def last_batch(self, session_id: str) -> ErrorBatch | None:
        with self._lock:
            return self._last_batch.get(session_id)

    # ─────────────── Mutation ───────────────

    def acknowledge(self, session_id: str, error_id: str) -> bool:
        """Mark ``error_id`` as cleared without waiting for the next
        scan.  Returns ``True`` if the error was active and is now
        removed, ``False`` if it wasn't tracked.

        Emits ``ui_sandbox.error.cleared`` on success.  Idempotent on
        repeated calls.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(error_id, str) or not error_id.strip():
            raise ValueError("error_id must be a non-empty string")

        now = self._clock()
        with self._lock:
            bucket = self._state.get(session_id)
            if not bucket or error_id not in bucket:
                return False
            bucket.pop(error_id)
            self._cleared_total += 1

        self._emit(
            ERROR_EVENT_CLEARED,
            {
                "session_id": session_id,
                "error_id": error_id,
                "cleared_at": now,
                "source": "acknowledge",
                "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            },
        )
        return True

    def clear_session(self, session_id: str) -> int:
        """Drop **all** tracked errors for ``session_id``.  Returns
        the number of errors cleared.  Useful on sandbox teardown
        (V2 #2 :meth:`SandboxLifecycle.teardown`) — the next
        ensure_session will start with a clean slate."""

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")

        with self._lock:
            bucket = self._state.pop(session_id, None)
            self._last_batch.pop(session_id, None)
            if not bucket:
                return 0
            count = len(bucket)
            self._cleared_total += count
        return count

    # ─────────────── Background watch ───────────────

    def start_watch(
        self,
        session_id: str,
        *,
        interval_s: float | None = None,
    ) -> None:
        """Spawn a daemon thread that calls :meth:`scan` every
        ``interval_s``.  Raises :class:`WatchAlreadyRunning` if a
        watch already exists for this session.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        period = (
            float(interval_s) if interval_s is not None else self._watch_interval_s
        )
        if period <= 0:
            raise ValueError("interval_s must be positive")

        with self._lock:
            existing = self._watches.get(session_id)
            if existing is not None and existing.thread.is_alive():
                raise WatchAlreadyRunning(
                    f"watch already running for session_id={session_id!r}"
                )
            stop_event = threading.Event()
            state = _WatchState(
                thread=None,  # type: ignore[arg-type]
                stop_event=stop_event,
                interval_s=period,
                started_at=self._clock(),
            )
            thread = threading.Thread(
                target=self._watch_loop,
                name=f"ui-preview-error-watch-{session_id}",
                args=(session_id, state),
                daemon=True,
            )
            state.thread = thread
            self._watches[session_id] = state
            thread.start()

        self._emit(
            ERROR_EVENT_WATCH_STARTED,
            {
                "session_id": session_id,
                "interval_s": float(period),
                "started_at": float(state.started_at),
                "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            },
        )

    def stop_watch(
        self,
        session_id: str,
        *,
        wait: bool = True,
        timeout_s: float = 5.0,
        missing_ok: bool = True,
    ) -> bool:
        """Signal the background watch to exit.

        Returns ``True`` if a live watch was stopped, ``False`` if
        none was running (when ``missing_ok=True``).  Raises
        :class:`WatchNotRunning` when ``missing_ok=False``.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")

        with self._lock:
            state = self._watches.get(session_id)
            if state is None:
                if missing_ok:
                    return False
                raise WatchNotRunning(
                    f"no watch running for session_id={session_id!r}"
                )
            state.stop_event.set()
            thread = state.thread

        if wait and thread is not None:
            thread.join(timeout=timeout_s)

        with self._lock:
            state = self._watches.get(session_id)
            if state is not None and not state.thread.is_alive():
                self._watches.pop(session_id, None)
                sweeps = state.sweeps
                failures = state.failures
                interval_s = state.interval_s
            else:
                return True

        self._emit(
            ERROR_EVENT_WATCH_STOPPED,
            {
                "session_id": session_id,
                "sweeps": int(sweeps),
                "failures": int(failures),
                "interval_s": float(interval_s),
                "stopped_at": float(self._clock()),
                "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
            },
        )
        return True

    def stop_all_watches(self, *, wait: bool = True, timeout_s: float = 5.0) -> int:
        """Stop every background watch — returns the count stopped."""

        with self._lock:
            session_ids = tuple(self._watches.keys())
        stopped = 0
        for session_id in session_ids:
            if self.stop_watch(session_id, wait=wait, timeout_s=timeout_s):
                stopped += 1
        return stopped

    def is_watching(self, session_id: str) -> bool:
        with self._lock:
            state = self._watches.get(session_id)
            return state is not None and state.thread.is_alive()

    def watch_sessions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    sid
                    for sid, state in self._watches.items()
                    if state.thread.is_alive()
                )
            )

    def watch_sweeps(self, session_id: str) -> int:
        with self._lock:
            state = self._watches.get(session_id)
            return 0 if state is None else state.sweeps

    def watch_failures(self, session_id: str) -> int:
        with self._lock:
            state = self._watches.get(session_id)
            return 0 if state is None else state.failures

    def _watch_loop(self, session_id: str, state: _WatchState) -> None:
        """Thread target.  Sweeps until stop_event fires."""

        while not state.stop_event.is_set():
            if state.stop_event.wait(timeout=state.interval_s):
                break
            try:
                self.scan(session_id)
            except Exception as exc:  # pragma: no cover - scan is try/except itself
                with self._lock:
                    state.failures += 1
                logger.warning(
                    "preview-error watch sweep for %s raised: %s", session_id, exc
                )
                continue
            with self._lock:
                state.sweeps += 1

    # ─────────────── Snapshot / CM ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe view of current state — handy for operator endpoints."""

        with self._lock:
            sessions = {
                sid: [err.to_dict() for err in bucket.values()]
                for sid, bucket in self._state.items()
            }
            watches = {
                sid: {
                    "alive": state.thread.is_alive(),
                    "interval_s": float(state.interval_s),
                    "started_at": float(state.started_at),
                    "sweeps": int(state.sweeps),
                    "failures": int(state.failures),
                }
                for sid, state in self._watches.items()
            }
            return {
                "schema_version": UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
                "sessions": sessions,
                "watches": watches,
                "scan_count": int(self._scan_count),
                "detected_total": int(self._detected_total),
                "cleared_total": int(self._cleared_total),
                "log_tail": int(self._log_tail),
                "max_errors_per_session": int(self._max_errors),
                "watch_interval_s": float(self._watch_interval_s),
                "now": float(self._clock()),
            }

    def __enter__(self) -> "PreviewErrorBridge":
        return self

    def __exit__(self, *_: Any) -> None:
        """Stop all watches + clear state — hygiene for ``with`` users."""

        self.stop_all_watches(wait=True, timeout_s=5.0)
        with self._lock:
            self._state.clear()
            self._last_batch.clear()

    # ─────────────── Event plumbing ───────────────

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(payload))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "ui_preview_error_bridge event callback raised: %s", exc
            )

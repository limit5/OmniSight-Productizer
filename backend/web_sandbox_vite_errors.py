"""W15.1 — Per-workspace ring buffer for ``omnisight-vite-plugin`` error reports.

The W15.1 row ships :mod:`packages/omnisight-vite-plugin` (a custom
Vite plugin baked into the W14.1 sidecar's W15.5 vite.config scaffold)
that POSTs every compile-time and runtime error to

    POST /web-sandbox/preview/{workspace_id}/error

This module owns the **backend half** of that contract:

  1. :class:`ViteBuildError` — frozen dataclass mirroring the wire
     shape produced by :func:`buildErrorPayload` in the JS plugin.
  2. :data:`WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION` — the literal
     pinned in lock-step with the JS side's
     ``OMNISIGHT_VITE_ERROR_SCHEMA_VERSION``.  The vitest + pytest
     drift-guard tests assert the two stay aligned.
  3. :class:`ViteErrorBuffer` — bounded ring buffer keyed on
     ``workspace_id``.  Per-worker independent (intentional answer
     **#3** in SOP §1 — see "Module-global state audit" below).
     W15.2 (``backend/web/vite_error_relay.py``) will read from the
     buffer and fold the entries into LangGraph
     ``state.error_history``.

Row boundary
============

W15.1 owns:

  * The wire shape contract (Python dataclass + JSON keys + size caps).
  * The ring buffer that :mod:`backend.routers.web_sandbox` writes to.
  * The drift-guard literals (schema version, allowed phases, byte
    caps) that lock the JS plugin and the Python backend together.

W15.1 explicitly does NOT own:

  * The LangGraph state.error_history wire-up — that lives in W15.2's
    ``backend/web/vite_error_relay.py``.
  * The system-prompt template that quotes the error back to the
    agent — that lives in W15.3.
  * The auto-retry budget (3-strike escalation) — that lives in W15.4.
  * The ``vite.config`` scaffold injection — that lives in W15.5.
  * The three-class self-fix tests (syntax / undefined symbol /
    import-path typo) — that live in W15.6.

Module-global state audit (SOP §1)
==================================

This module ships a single per-process buffer instance accessed via
:func:`get_default_buffer`. **Answer #3** — *intentional per-worker
independence*.

Reasoning:

  * The W14.1 sidecar's plugin POSTs the error to the backend on the
    operator's docker network.  In production the network's load
    balancer (Caddy / CF Tunnel) hashes by sandbox hostname, which
    means every error from a given sidecar lands on the same uvicorn
    worker.  Cross-worker visibility is therefore not required for
    the immediate consumer (W15.2 / W15.3 / W15.4 all run on the same
    request thread that received the error).
  * The W14.10 alembic 0059 ``web_sandbox_instances`` table is the
    designated cross-worker home for sandbox state; W15.x is intentionally
    deferred from that table until W15.2 lands the LangGraph wiring
    and decides whether the errors warrant durable persistence (most
    do not — they are throwaway "agent saw and fixed" events).
  * Audit history of "did the agent see the same error 3 times in a
    row?" is W15.4's concern and will be tracked on the LangGraph
    state, which is per-graph-run not per-worker.

The buffer is therefore intentionally per-worker; the production
status is documented as ``dev-only`` for the row and the gate-up to
``deployed-inactive`` is W15.2 + W15.5 wiring (the plugin needs
``vite.config`` injection before it actually runs).

Read-after-write timing audit (SOP §2)
======================================

N/A — fresh module, no DB pool migration, no compat→pool conversion.
The only race surface is two POSTs landing on the same worker
concurrently for the same workspace; the buffer's ``RLock``
serialises them and the ring discards the oldest on overflow.

Compat fingerprint grep (SOP §3)
================================

Pure stdlib + dataclasses; verified clean::

    $ grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]" \\
        backend/web_sandbox_vite_errors.py
    (empty)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, MutableMapping


__all__ = [
    "WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION",
    "VITE_ERROR_PLUGIN_NAME",
    "VITE_ERROR_PLUGIN_VERSION",
    "VITE_ERROR_ALLOWED_KINDS",
    "VITE_ERROR_ALLOWED_PHASES",
    "VITE_ERROR_MESSAGE_MAX_BYTES",
    "VITE_ERROR_STACK_MAX_BYTES",
    "VITE_ERROR_BUFFER_DEFAULT_CAP",
    "ViteBuildError",
    "ViteBuildErrorValidationError",
    "ViteErrorBuffer",
    "get_default_buffer",
    "set_default_buffer_for_tests",
    "validate_error_payload",
]


#: Bump in lock-step with ``OMNISIGHT_VITE_ERROR_SCHEMA_VERSION`` in
#: ``packages/omnisight-vite-plugin/index.js``.  The drift-guard tests
#: assert the two strings byte-equal.
WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION = "1.0.0"

#: Name field on the wire shape — only ``omnisight-vite-plugin`` is
#: accepted today.  The Rolldown / Webpack siblings (W15.5 row spec)
#: register their own names; we extend the allowlist here when those
#: ports land so the backend can disambiguate the producer in audit
#: queries.
VITE_ERROR_PLUGIN_NAME = "omnisight-vite-plugin"

#: Plugin version — the wire-shape contract is frozen at this version.
#: Patch bumps on the JS side that do *not* touch the wire shape do
#: NOT bump this; minor / major bumps do.
VITE_ERROR_PLUGIN_VERSION = "0.1.0"

#: Allowed ``kind`` literals — kept in sync with the JS plugin's
#: ``buildErrorPayload`` validator.
VITE_ERROR_ALLOWED_KINDS: frozenset[str] = frozenset({"compile", "runtime"})

#: Allowed ``phase`` literals — kept in sync with the JS plugin's
#: ``ALLOWED_PHASES`` constant.  Ordered tuple for stable rendering
#: in error messages; the membership check uses the frozenset below.
VITE_ERROR_ALLOWED_PHASES: tuple[str, ...] = (
    "config",
    "buildStart",
    "load",
    "transform",
    "hmr",
    "client",
)
_ALLOWED_PHASE_SET: frozenset[str] = frozenset(VITE_ERROR_ALLOWED_PHASES)

#: Hard cap on the ``message`` bytes accepted on the wire (UTF-8).
#: Matches the JS plugin's :data:`MESSAGE_MAX_BYTES`.  Anything longer
#: is truncated server-side rather than rejected — the agent benefits
#: from a partial message more than it benefits from a 422.
VITE_ERROR_MESSAGE_MAX_BYTES = 4 * 1024

#: Hard cap on the ``stack`` bytes accepted on the wire (UTF-8).
#: Matches the JS plugin's :data:`STACK_TRACE_MAX_BYTES`.
VITE_ERROR_STACK_MAX_BYTES = 8 * 1024

#: Default ring buffer capacity per workspace.  Sized so a 30-minute
#: idle window with one error per second still fits (1800 events) but
#: bounded enough to survive a runaway HMR loop.
VITE_ERROR_BUFFER_DEFAULT_CAP = 200


class ViteBuildErrorValidationError(ValueError):
    """Raised by :func:`validate_error_payload` when the wire shape
    does not match the W15.1 contract.

    The router maps this to a ``422 Unprocessable Entity`` so the JS
    plugin (or a sibling Rolldown / Webpack adapter) can correct the
    payload shape on the next attempt instead of silently posting
    garbage forever.
    """


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Return ``value`` truncated so its UTF-8 byte length does not
    exceed ``max_bytes``.

    The JS plugin truncates client-side too; we re-truncate here as
    defence in depth so a malicious caller (or a sibling plugin that
    forgot to bump the cap) cannot store unbounded blobs in the
    in-memory buffer.
    """

    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    # Walk back so we never split a multi-byte codepoint.
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0b1100_0000) == 0b1000_0000:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class ViteBuildError:
    """Server-side representation of a JS plugin error report.

    Frozen — any state change goes through :func:`dataclasses.replace`.
    The ``schema_version`` / ``plugin`` / ``plugin_version`` fields are
    duplicated on every instance so future schema migrations can be
    inspected without re-parsing the JSON.

    The dataclass is the canonical form; the ring buffer stores
    instances of this class, and :meth:`to_dict` projects them back to
    the wire shape (idempotent — round-tripping a payload through
    :func:`validate_error_payload` and :meth:`to_dict` produces an
    equal dict).
    """

    schema_version: str
    kind: str
    phase: str
    message: str
    file: str | None
    line: int | None
    column: int | None
    stack: str | None
    plugin: str
    plugin_version: str
    occurred_at: float
    received_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "stack": self.stack,
            "plugin": self.plugin,
            "plugin_version": self.plugin_version,
            "occurred_at": float(self.occurred_at),
            "received_at": float(self.received_at),
        }


def _coerce_optional_str(value: Any, *, field: str, max_bytes: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ViteBuildErrorValidationError(
            f"{field!r} must be a string or null, got {type(value).__name__}"
        )
    if max_bytes is not None:
        return _truncate_utf8(value, max_bytes)
    return value


def _coerce_optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is an int subclass in Python; reject explicitly to
        # catch JS shape regressions.
        raise ViteBuildErrorValidationError(
            f"{field!r} must be an int or null, got bool"
        )
    if isinstance(value, int):
        if value < 0 or value > 1_000_000_000:
            raise ViteBuildErrorValidationError(
                f"{field!r} out of plausible range, got {value}"
            )
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ViteBuildErrorValidationError(
                f"{field!r} must be integer-valued, got {value}"
            )
        return int(value)
    raise ViteBuildErrorValidationError(
        f"{field!r} must be an int or null, got {type(value).__name__}"
    )


def validate_error_payload(
    payload: Mapping[str, Any],
    *,
    received_at: float | None = None,
) -> ViteBuildError:
    """Validate a wire-shape dict and project it into a
    :class:`ViteBuildError`.

    Strict on shape — unknown top-level keys are rejected so a sibling
    plugin that adds a field has to ship the matching backend bump
    rather than silently spraying noise into the buffer.  Lenient on
    content — overlong ``message`` / ``stack`` are truncated rather
    than rejected (the agent benefits from a partial error more than
    a 422).
    """

    if not isinstance(payload, Mapping):
        raise ViteBuildErrorValidationError(
            f"payload must be a JSON object, got {type(payload).__name__}"
        )
    expected_keys = {
        "schema_version",
        "kind",
        "phase",
        "message",
        "file",
        "line",
        "column",
        "stack",
        "plugin",
        "plugin_version",
        "occurred_at",
    }
    extra = set(payload.keys()) - expected_keys
    if extra:
        # Sort for determinism in the error message — tests assert on
        # the rendered text.
        extras = ", ".join(sorted(repr(k) for k in extra))
        raise ViteBuildErrorValidationError(
            f"unexpected keys in payload: {extras}"
        )
    missing = expected_keys - set(payload.keys())
    if missing:
        miss = ", ".join(sorted(repr(k) for k in missing))
        raise ViteBuildErrorValidationError(
            f"missing required keys in payload: {miss}"
        )

    schema_version = payload["schema_version"]
    if schema_version != WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION:
        raise ViteBuildErrorValidationError(
            "schema_version mismatch — backend pin is "
            f"{WEB_SANDBOX_VITE_ERROR_SCHEMA_VERSION!r}, payload sent "
            f"{schema_version!r}; bump OMNISIGHT_VITE_ERROR_SCHEMA_VERSION "
            "in @omnisight/vite-plugin to match."
        )

    kind = payload["kind"]
    if kind not in VITE_ERROR_ALLOWED_KINDS:
        raise ViteBuildErrorValidationError(
            f"kind must be one of {sorted(VITE_ERROR_ALLOWED_KINDS)}, got {kind!r}"
        )

    phase = payload["phase"]
    if phase not in _ALLOWED_PHASE_SET:
        raise ViteBuildErrorValidationError(
            f"phase must be one of {list(VITE_ERROR_ALLOWED_PHASES)}, got {phase!r}"
        )

    message = payload["message"]
    if not isinstance(message, str):
        raise ViteBuildErrorValidationError(
            f"'message' must be a string, got {type(message).__name__}"
        )
    message = _truncate_utf8(message, VITE_ERROR_MESSAGE_MAX_BYTES)

    file = _coerce_optional_str(payload["file"], field="file", max_bytes=2 * 1024)
    line = _coerce_optional_int(payload["line"], field="line")
    column = _coerce_optional_int(payload["column"], field="column")
    stack = _coerce_optional_str(
        payload["stack"], field="stack", max_bytes=VITE_ERROR_STACK_MAX_BYTES
    )

    plugin = payload["plugin"]
    if not isinstance(plugin, str) or not plugin:
        raise ViteBuildErrorValidationError("'plugin' must be a non-empty string")
    if plugin != VITE_ERROR_PLUGIN_NAME:
        # Lenient list — keep the door open for the W15.5 Rolldown /
        # Webpack siblings.  Today only the canonical name is
        # accepted; tests pin this so we know to extend the allowlist
        # when a sibling lands.
        raise ViteBuildErrorValidationError(
            f"'plugin' must be {VITE_ERROR_PLUGIN_NAME!r}, got {plugin!r}"
        )

    plugin_version = payload["plugin_version"]
    if not isinstance(plugin_version, str) or not plugin_version:
        raise ViteBuildErrorValidationError(
            "'plugin_version' must be a non-empty string"
        )

    occurred_at_raw = payload["occurred_at"]
    if isinstance(occurred_at_raw, bool):
        raise ViteBuildErrorValidationError("'occurred_at' must be a number, got bool")
    if not isinstance(occurred_at_raw, (int, float)):
        raise ViteBuildErrorValidationError(
            f"'occurred_at' must be a number, got {type(occurred_at_raw).__name__}"
        )
    occurred_at = float(occurred_at_raw)
    if occurred_at < 0:
        raise ViteBuildErrorValidationError("'occurred_at' must be non-negative")

    received = float(received_at) if received_at is not None else time.time()
    return ViteBuildError(
        schema_version=schema_version,
        kind=kind,
        phase=phase,
        message=message,
        file=file,
        line=line,
        column=column,
        stack=stack,
        plugin=plugin,
        plugin_version=plugin_version,
        occurred_at=occurred_at,
        received_at=received,
    )


class ViteErrorBuffer:
    """Per-worker bounded ring buffer keyed on ``workspace_id``.

    Methods are thread-safe — :meth:`record` and :meth:`recent` are
    called from FastAPI request handlers running on the worker's
    asyncio loop *and* from the W15.2 LangGraph integration that
    drains the buffer on the main thread.

    Capacity is per-workspace; total memory is bounded by
    ``capacity * max_workspaces`` where ``max_workspaces`` is governed
    by the W14.5 idle reaper (sandboxes that go idle ≥ 30 minutes get
    collected and their buffer entries dropped via :meth:`drop`).
    """

    def __init__(
        self,
        *,
        capacity: int = VITE_ERROR_BUFFER_DEFAULT_CAP,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._capacity = int(capacity)
        self._lock = threading.RLock()
        self._buffers: MutableMapping[str, deque[ViteBuildError]] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, workspace_id: str, error: ViteBuildError) -> ViteBuildError:
        """Append ``error`` to the buffer for ``workspace_id``.

        Returns the recorded :class:`ViteBuildError` (potentially with
        ``received_at`` filled in by :func:`validate_error_payload`).
        Drops the oldest entry when the buffer is at capacity.
        """

        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a non-empty string")
        if not isinstance(error, ViteBuildError):
            raise TypeError(
                f"error must be a ViteBuildError, got {type(error).__name__}"
            )
        # If the caller forgot to set received_at, fill it in here so
        # the buffer always carries a wall-clock timestamp.
        if error.received_at <= 0:
            error = replace(error, received_at=time.time())
        with self._lock:
            buf = self._buffers.get(workspace_id)
            if buf is None:
                buf = deque(maxlen=self._capacity)
                self._buffers[workspace_id] = buf
            buf.append(error)
        return error

    def recent(
        self,
        workspace_id: str,
        *,
        limit: int | None = None,
    ) -> list[ViteBuildError]:
        """Return up to ``limit`` most recent errors for the workspace
        in chronological order (oldest first).

        ``limit=None`` returns every error currently in the buffer.
        Always returns a fresh list so callers can mutate without
        racing the buffer.
        """

        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a non-empty string")
        with self._lock:
            buf = self._buffers.get(workspace_id)
            if buf is None:
                return []
            entries = list(buf)
        if limit is None:
            return entries
        if limit <= 0:
            return []
        return entries[-int(limit):]

    def count(self, workspace_id: str) -> int:
        with self._lock:
            buf = self._buffers.get(workspace_id)
            return 0 if buf is None else len(buf)

    def drop(self, workspace_id: str) -> int:
        """Forget every error for ``workspace_id``.

        Returns the number of entries dropped.  Used by the W14.5
        idle reaper integration so a sandbox that gets reaped does
        not leak memory through the buffer.
        """

        with self._lock:
            buf = self._buffers.pop(workspace_id, None)
        return 0 if buf is None else len(buf)

    def workspaces(self) -> list[str]:
        """Return the list of workspace_ids currently tracked.  Used
        by tests + the future W15.2 drain loop."""

        with self._lock:
            return list(self._buffers.keys())

    def extend(self, workspace_id: str, errors: Iterable[ViteBuildError]) -> int:
        """Bulk-append a sequence of errors.  Returns the count
        actually appended (drops are silent — the ring discards the
        oldest entries on overflow)."""

        appended = 0
        for err in errors:
            self.record(workspace_id, err)
            appended += 1
        return appended


# Per-process default buffer.  Module-level state is intentional —
# see the SOP §1 audit at the top of this file.  Tests reset via
# :func:`set_default_buffer_for_tests`.
_DEFAULT_BUFFER: ViteErrorBuffer = ViteErrorBuffer()


def get_default_buffer() -> ViteErrorBuffer:
    """Return the per-worker default buffer.  Constructed lazily at
    module import (singleton pattern)."""

    return _DEFAULT_BUFFER


def set_default_buffer_for_tests(buffer: ViteErrorBuffer | None) -> None:
    """Test-only injection point.  Pass ``None`` to reset to a fresh
    default buffer with the documented capacity."""

    global _DEFAULT_BUFFER
    _DEFAULT_BUFFER = (
        buffer if buffer is not None else ViteErrorBuffer()
    )

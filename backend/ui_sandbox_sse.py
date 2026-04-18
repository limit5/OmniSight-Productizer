"""V2 #7 (issue #318) — UI sandbox SSE event bridge.

Publishes two canonical SSE events to the frontend real-time bus
(:mod:`backend.events`):

  * ``ui_sandbox.screenshot`` — one screenshot was captured.
    Payload::

        {
          "session_id":  str,
          "viewport":    str,       # e.g. "desktop" / "tablet" / "mobile"
          "image_url":   str,       # pointer URL or data URL; never raw bytes
          "timestamp":   float,     # epoch seconds
          "schema_version": str,
          # optional extras —
          "preview_url": str,
          "path":        str,
          "byte_len":    int,
          "duration_ms": float,
          "viewport_width":  int,
          "viewport_height": int,
        }

  * ``ui_sandbox.error`` — a compile / runtime error was detected.
    Payload::

        {
          "error_type":  str,
          "message":     str,
          "file":        str | None,
          "line":        int | None,
          "timestamp":   float,
          "schema_version": str,
          # optional extras —
          "session_id":  str,
          "error_id":    str,
          "severity":    str,        # "error" | "warning"
          "source":      str,        # "compile" | "runtime"
          "column":      int | None,
          "first_seen_at": float,
          "last_seen_at":  float,
          "occurrences": int,
        }

Where this sits
---------------

V2 #1-#6 (``ui_sandbox.py`` / ``ui_sandbox_lifecycle.py`` /
``ui_screenshot.py`` / ``ui_responsive_viewport.py`` /
``ui_preview_error_bridge.py`` / ``ui_agent_visual_context.py``) all
emit their internal lifecycle events via injected callbacks matching
``EventCallback = Callable[[str, Mapping[str, Any]], None]``.  Those
callbacks carry rich dict payloads (e.g. ``ScreenshotCapture.to_dict()``
or ``PreviewError.to_dict()``).

This module is the wire between those internal callbacks and the
system-wide SSE bus in :mod:`backend.events`.  It:

  1. Re-shapes the rich internal payloads into the compact spec above
     (V2 row 7 — ``session_id / viewport / image_url / timestamp`` for
     screenshots, ``error_type / message / file / line`` for errors).
  2. Strips PNG bytes out of SSE frames — frames must stay small to
     avoid pushing 10-MB images through a typical Server-Sent-Events
     writer.  The ``image_url`` field points at either a REST endpoint
     the frontend can fetch, or an inlined ``data:image/png;base64,…``
     URL when the caller opts in.
  3. Deduplicates: V2 #2 ``LIFECYCLE_EVENT_SCREENSHOT`` and V2 #3
     ``SCREENSHOT_EVENT_CAPTURED`` share the ``ui_sandbox.screenshot``
     topic string — the bridge publishes exactly once per capture_id
     regardless of which callback fires first.
  4. Remains best-effort: if the SSE bus is unreachable, the bridge
     logs a warning and continues — an agent loop must never crash
     because the frontend isn't listening.

Design decisions
----------------

* **Spec-first payload shape.**  The SSE contract with the frontend
  is pinned by :data:`UI_SANDBOX_SSE_SCHEMA_VERSION` and
  :data:`SCREENSHOT_EVENT_FIELDS` / :data:`ERROR_EVENT_FIELDS` —
  callers can never drift from V2 row 7's ``session_id / viewport /
  image_url / timestamp`` and ``error_type / message / file / line``
  required fields.
* **Image URL strategy is a choice.**  ``endpoint`` gives the
  frontend a pointer URL it can GET (the V2 integration story —
  frontend fetches PNG, SSE frame stays tiny).  ``data`` inlines the
  PNG as a ``data:image/png;base64,…`` URL for environments without
  a sidecar endpoint (tests, demos, single-process setups).
  ``omit`` is the degradation path — emit metadata only with
  ``image_url=""`` if the caller hasn't chosen.
* **Idempotent dedup.**  The bridge keys dedup on
  ``(session_id, captured_at, viewport_name)`` — two callbacks firing
  for the same capture (V2 #2 + V2 #3) don't produce two SSE frames.
* **No side effects on V2 modules.**  The bridge only *subscribes* to
  their event_cb seam; it never touches their state, never holds
  their locks beyond the per-call callback invocation, never writes
  files.
* **Composition over inheritance.**  Mirrors V2 #2-#6 —
  ``UiSandboxSseBridge`` holds an ``EventPublisher`` (default: the
  ``backend.events.bus.publish`` binding), not a subclass of
  ``EventBus``.
* **Thread-safe.**  The internal dedup window is guarded by an
  ``RLock``; 20 concurrent bridge-callback calls survive the stress
  test without corruption.

Contract (pinned by ``backend/tests/test_ui_sandbox_sse.py``)
-------------------------------------------------------------

* :data:`SSE_EVENT_SCREENSHOT` == ``"ui_sandbox.screenshot"`` matches
  the V2 row 7 spec string byte-for-byte.
* :data:`SSE_EVENT_ERROR` == ``"ui_sandbox.error"`` matches likewise.
* :data:`SCREENSHOT_EVENT_FIELDS` contains at minimum
  ``("session_id", "viewport", "image_url", "timestamp")``.
* :data:`ERROR_EVENT_FIELDS` contains at minimum
  ``("error_type", "message", "file", "line")``.
* :func:`build_screenshot_event_payload` never returns raw PNG bytes.
* :func:`build_error_event_payload` coalesces ``cleared`` events into
  the single ``ui_sandbox.error`` topic with ``phase="cleared"`` —
  frontend subscribers only need one topic.
* The bridge callback never raises.  SSE bus failures log + continue.
"""

from __future__ import annotations

import base64
import logging
import threading
import time as _time_mod
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

logger = logging.getLogger(__name__)


__all__ = [
    "UI_SANDBOX_SSE_SCHEMA_VERSION",
    "SSE_EVENT_SCREENSHOT",
    "SSE_EVENT_ERROR",
    "SSE_EVENT_TYPES",
    "SCREENSHOT_EVENT_FIELDS",
    "ERROR_EVENT_FIELDS",
    "IMAGE_URL_STRATEGY_ENDPOINT",
    "IMAGE_URL_STRATEGY_DATA",
    "IMAGE_URL_STRATEGY_OMIT",
    "IMAGE_URL_STRATEGIES",
    "DEFAULT_IMAGE_URL_STRATEGY",
    "DEFAULT_IMAGE_URL_TEMPLATE",
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "ERROR_PHASE_DETECTED",
    "ERROR_PHASE_CLEARED",
    "ERROR_PHASES",
    "build_screenshot_image_url",
    "build_screenshot_event_payload",
    "build_error_event_payload",
    "build_error_cleared_payload",
    "EventPublisher",
    "BusEventPublisher",
    "UiSandboxSseBridge",
]


#: Bump on any shape change to the screenshot / error SSE payloads.
#: Frontend pins the same value; mismatch means re-sync time.
UI_SANDBOX_SSE_SCHEMA_VERSION = "1.0.0"

#: The canonical SSE topic emitted by :func:`build_screenshot_event_payload`
#: and published by :class:`UiSandboxSseBridge` whenever a V2 screenshot
#: capture callback fires.  Matches V2 row 7 spec byte-for-byte.
SSE_EVENT_SCREENSHOT = "ui_sandbox.screenshot"

#: The canonical SSE topic emitted whenever a V2 compile / runtime
#: error is detected (or cleared).  V2 row 7 spec.
SSE_EVENT_ERROR = "ui_sandbox.error"

#: Roster of SSE topics this module is allowed to publish.  The frontend
#: uses this tuple to register its event handlers deterministically.
SSE_EVENT_TYPES: tuple[str, ...] = (
    SSE_EVENT_SCREENSHOT,
    SSE_EVENT_ERROR,
)

#: Fields the frontend relies on in :data:`SSE_EVENT_SCREENSHOT` payloads.
#: This is the *hard* contract; other fields may be appended but these
#: four must always be present.
SCREENSHOT_EVENT_FIELDS: tuple[str, ...] = (
    "session_id",
    "viewport",
    "image_url",
    "timestamp",
)

#: Fields the frontend relies on in :data:`SSE_EVENT_ERROR` payloads.
ERROR_EVENT_FIELDS: tuple[str, ...] = (
    "error_type",
    "message",
    "file",
    "line",
)


#: ``image_url`` strategy — frontend fetches PNG from a REST endpoint.
#: Keeps SSE frames tiny (preferred for production).
IMAGE_URL_STRATEGY_ENDPOINT = "endpoint"

#: ``image_url`` strategy — inline the PNG as ``data:image/png;base64,…``.
#: Simpler wiring (no sidecar endpoint) at the cost of large SSE frames.
IMAGE_URL_STRATEGY_DATA = "data"

#: ``image_url`` strategy — emit ``""`` and ship metadata only.
#: Used when the caller has no endpoint *and* doesn't want bytes on the
#: wire; the subscriber can fall back to querying the service directly.
IMAGE_URL_STRATEGY_OMIT = "omit"

#: Legal strategies in deterministic order — tests assert on this tuple.
IMAGE_URL_STRATEGIES: tuple[str, ...] = (
    IMAGE_URL_STRATEGY_ENDPOINT,
    IMAGE_URL_STRATEGY_DATA,
    IMAGE_URL_STRATEGY_OMIT,
)

#: Default strategy — endpoint pointer keeps SSE frames small.  A
#: sidecar REST handler is expected to serve PNG bytes at the URL
#: produced by :data:`DEFAULT_IMAGE_URL_TEMPLATE`.
DEFAULT_IMAGE_URL_STRATEGY = IMAGE_URL_STRATEGY_ENDPOINT

#: Default endpoint template; placeholders are ``{session_id}`` /
#: ``{viewport}`` / ``{capture_id}``.  Callers override when they host
#: the screenshot-serving endpoint somewhere else.
DEFAULT_IMAGE_URL_TEMPLATE = "/api/ui_sandbox/{session_id}/screenshots/{capture_id}"

#: How long the bridge suppresses duplicate screenshot events keyed on
#: ``(session_id, captured_at, viewport)``.  V2 #2 and V2 #3 both fire
#: ``ui_sandbox.screenshot`` internal events for the same capture; this
#: window makes sure only one SSE frame reaches the frontend.
DEFAULT_DEDUP_WINDOW_SECONDS = 2.0


#: Phase of an error lifecycle — "detected" (new / persisting) or
#: "cleared" (fixed in the most recent scan).  Frontend uses this to
#: toggle error panels without needing two SSE topics.
ERROR_PHASE_DETECTED = "detected"
ERROR_PHASE_CLEARED = "cleared"

#: Legal phases in deterministic order.
ERROR_PHASES: tuple[str, ...] = (
    ERROR_PHASE_DETECTED,
    ERROR_PHASE_CLEARED,
)


# ───────────────────────────────────────────────────────────────────
#  Pure payload builders
# ───────────────────────────────────────────────────────────────────


def build_screenshot_image_url(
    payload: Mapping[str, Any],
    *,
    strategy: str = DEFAULT_IMAGE_URL_STRATEGY,
    url_template: str = DEFAULT_IMAGE_URL_TEMPLATE,
    capture_bytes: bytes | None = None,
) -> str:
    """Resolve the ``image_url`` field for a screenshot SSE event.

    ``payload`` is the rich internal dict emitted by V2 #3
    (``ScreenshotCapture.to_dict()``) or V2 #2's lifecycle callback.
    It must contain ``session_id`` + ``viewport`` (dict or name).

    Strategies:

    * ``endpoint`` — format ``url_template`` with available keys.
    * ``data`` — build ``data:image/png;base64,...`` from ``capture_bytes``.
      The internal payload *does not* carry raw PNG bytes (V2 #3 strips
      them from SSE frames), so callers wanting ``data`` must pass
      ``capture_bytes=`` explicitly.
    * ``omit`` — always returns ``""``.
    """

    if strategy not in IMAGE_URL_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {IMAGE_URL_STRATEGIES!r}, got {strategy!r}"
        )
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")

    if strategy == IMAGE_URL_STRATEGY_OMIT:
        return ""

    if strategy == IMAGE_URL_STRATEGY_DATA:
        if capture_bytes is None:
            raise ValueError(
                "strategy='data' requires capture_bytes; SSE payloads do "
                "not carry raw PNG bytes by default"
            )
        if not isinstance(capture_bytes, (bytes, bytearray)):
            raise TypeError("capture_bytes must be bytes-like")
        if not capture_bytes:
            raise ValueError("capture_bytes must be non-empty")
        encoded = base64.b64encode(bytes(capture_bytes)).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    # endpoint strategy — format the template.
    if not isinstance(url_template, str) or not url_template.strip():
        raise ValueError("url_template must be a non-empty string")

    session_id = str(payload.get("session_id", "") or "")
    viewport_name = _extract_viewport_name(payload.get("viewport"))
    captured_at = payload.get("captured_at", 0.0) or 0.0
    try:
        captured_at_f = float(captured_at)
    except (TypeError, ValueError):
        captured_at_f = 0.0
    capture_id = f"{viewport_name}-{int(captured_at_f * 1000)}"

    keys: dict[str, Any] = {
        "session_id": session_id,
        "viewport": viewport_name,
        "capture_id": capture_id,
        "captured_at": captured_at_f,
    }
    try:
        return url_template.format(**keys)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"url_template {url_template!r} references unknown placeholder: {exc}"
        ) from exc


def _extract_viewport_name(viewport: Any) -> str:
    """Return the short viewport name from either a dict (V2 #3 payload
    shape) or a plain string."""

    if viewport is None:
        return ""
    if isinstance(viewport, str):
        return viewport
    if isinstance(viewport, Mapping):
        name = viewport.get("name")
        if isinstance(name, str) and name:
            return name
    return ""


def _extract_viewport_dims(viewport: Any) -> tuple[int | None, int | None]:
    if isinstance(viewport, Mapping):
        w = viewport.get("width")
        h = viewport.get("height")
        try:
            return (int(w) if w is not None else None,
                    int(h) if h is not None else None)
        except (TypeError, ValueError):
            return (None, None)
    return (None, None)


def build_screenshot_event_payload(
    internal_payload: Mapping[str, Any],
    *,
    image_url_strategy: str = DEFAULT_IMAGE_URL_STRATEGY,
    image_url_template: str = DEFAULT_IMAGE_URL_TEMPLATE,
    capture_bytes: bytes | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Shape a V2 screenshot internal payload into an SSE frame dict.

    ``internal_payload`` is the dict the V2 #3 / V2 #2 callback passes
    to its event_cb — typically ``ScreenshotCapture.to_dict()``.
    The return value contains *exactly* the four V2 row 7 required
    fields plus optional extras, and NEVER contains raw PNG bytes.
    """

    if not isinstance(internal_payload, Mapping):
        raise TypeError("internal_payload must be a mapping")

    session_id = str(internal_payload.get("session_id", "") or "")
    if not session_id:
        raise ValueError("internal_payload missing 'session_id'")

    viewport_name = _extract_viewport_name(internal_payload.get("viewport"))
    if not viewport_name:
        raise ValueError("internal_payload missing 'viewport' name")

    captured_at = internal_payload.get("captured_at")
    try:
        timestamp = float(captured_at) if captured_at is not None else float(
            now if now is not None else _time_mod.time()
        )
    except (TypeError, ValueError):
        timestamp = float(now if now is not None else _time_mod.time())

    image_url = build_screenshot_image_url(
        internal_payload,
        strategy=image_url_strategy,
        url_template=image_url_template,
        capture_bytes=capture_bytes,
    )

    w, h = _extract_viewport_dims(internal_payload.get("viewport"))
    out: dict[str, Any] = {
        "schema_version": UI_SANDBOX_SSE_SCHEMA_VERSION,
        "session_id": session_id,
        "viewport": viewport_name,
        "image_url": image_url,
        "timestamp": timestamp,
    }

    # Optional extras — frontend may show these but spec doesn't require.
    for key in ("preview_url", "target_url", "path"):
        val = internal_payload.get(key)
        if isinstance(val, str) and val:
            out[key] = val

    byte_len = internal_payload.get("byte_len")
    if isinstance(byte_len, int) and byte_len >= 0:
        out["byte_len"] = byte_len

    duration_ms = internal_payload.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        out["duration_ms"] = float(duration_ms)

    if w is not None:
        out["viewport_width"] = w
    if h is not None:
        out["viewport_height"] = h

    return out


def build_error_event_payload(
    internal_payload: Mapping[str, Any],
    *,
    phase: str = ERROR_PHASE_DETECTED,
    now: float | None = None,
) -> dict[str, Any]:
    """Shape a V2 #5 ``PreviewError.to_dict()`` into an SSE frame dict.

    Emits the four V2 row 7 required fields
    ``error_type / message / file / line`` plus session context, phase,
    timestamp, schema_version.  Never raises on optional-field absence.
    """

    if not isinstance(internal_payload, Mapping):
        raise TypeError("internal_payload must be a mapping")
    if phase not in ERROR_PHASES:
        raise ValueError(f"phase must be one of {ERROR_PHASES!r}, got {phase!r}")

    error_type = internal_payload.get("error_type")
    if not isinstance(error_type, str) or not error_type.strip():
        raise ValueError("internal_payload missing 'error_type'")

    message = internal_payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("internal_payload missing 'message'")

    file_val = internal_payload.get("file")
    if file_val is not None and not isinstance(file_val, str):
        file_val = str(file_val)

    line_val = internal_payload.get("line")
    if line_val is not None:
        try:
            line_val = int(line_val)
        except (TypeError, ValueError):
            line_val = None

    # Timestamp: prefer last_seen_at (most-recent observation) > first_seen_at
    # > now().  Float-coerce defensively.
    ts_source = internal_payload.get("last_seen_at")
    if ts_source is None or (isinstance(ts_source, (int, float)) and ts_source <= 0):
        ts_source = internal_payload.get("first_seen_at")
    if ts_source is None or (isinstance(ts_source, (int, float)) and ts_source <= 0):
        ts_source = now if now is not None else _time_mod.time()
    try:
        timestamp = float(ts_source)
    except (TypeError, ValueError):
        timestamp = float(now if now is not None else _time_mod.time())

    out: dict[str, Any] = {
        "schema_version": UI_SANDBOX_SSE_SCHEMA_VERSION,
        "error_type": error_type,
        "message": message,
        "file": file_val,
        "line": line_val,
        "phase": phase,
        "timestamp": timestamp,
    }

    # Optional session / severity / source extras — preserve when present.
    session_id = internal_payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        out["session_id"] = session_id

    error_id = internal_payload.get("error_id")
    if isinstance(error_id, str) and error_id:
        out["error_id"] = error_id

    severity = internal_payload.get("severity")
    if isinstance(severity, str) and severity:
        out["severity"] = severity

    source = internal_payload.get("source")
    if isinstance(source, str) and source:
        out["source"] = source

    column = internal_payload.get("column")
    if column is not None:
        try:
            out["column"] = int(column)
        except (TypeError, ValueError):
            pass

    occurrences = internal_payload.get("occurrences")
    if isinstance(occurrences, int) and occurrences >= 0:
        out["occurrences"] = occurrences

    for key in ("first_seen_at", "last_seen_at"):
        val = internal_payload.get(key)
        if isinstance(val, (int, float)):
            out[key] = float(val)

    return out


def build_error_cleared_payload(
    internal_payload: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Shape a V2 #5 ``ERROR_EVENT_CLEARED`` payload into an SSE frame.

    The cleared payload only carries ``session_id`` + ``error_id`` +
    ``cleared_at``; it lacks ``error_type`` / ``message`` / ``file`` /
    ``line``.  V2 row 7 still requires the four fields so the frontend
    can render a single-topic UI — we synthesise the missing required
    fields with sentinel values and set ``phase="cleared"``.
    """

    if not isinstance(internal_payload, Mapping):
        raise TypeError("internal_payload must be a mapping")

    session_id = internal_payload.get("session_id")
    error_id = internal_payload.get("error_id")
    if not isinstance(error_id, str) or not error_id:
        raise ValueError("internal_payload missing 'error_id'")

    cleared_at = internal_payload.get("cleared_at")
    if cleared_at is None:
        cleared_at = now if now is not None else _time_mod.time()
    try:
        timestamp = float(cleared_at)
    except (TypeError, ValueError):
        timestamp = float(now if now is not None else _time_mod.time())

    out: dict[str, Any] = {
        "schema_version": UI_SANDBOX_SSE_SCHEMA_VERSION,
        "error_type": "",
        "message": "",
        "file": None,
        "line": None,
        "phase": ERROR_PHASE_CLEARED,
        "timestamp": timestamp,
        "error_id": error_id,
    }
    if isinstance(session_id, str) and session_id:
        out["session_id"] = session_id
    return out


# ───────────────────────────────────────────────────────────────────
#  Publisher protocol + default
# ───────────────────────────────────────────────────────────────────


class EventPublisher(Protocol):
    """Shape of the publisher the bridge writes SSE frames to.

    ``publish(event_type, payload, session_id=...)`` — mirrors
    :meth:`backend.events.EventBus.publish` but without the broadcast /
    tenant kwargs so the bridge stays decoupled from the full bus API.
    """

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> None: ...


@dataclass
class BusEventPublisher:
    """Production adapter that routes to :mod:`backend.events.bus`.

    Lazily imports the bus at first ``publish()`` so unit tests that
    don't need SSE can construct the bridge without paying the import
    cost / without triggering ``backend.events`` side effects.
    """

    broadcast_scope: str = "global"
    tenant_id: str | None = None

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        from backend.events import bus  # lazy import — avoid at startup
        bus.publish(
            event_type,
            dict(payload),
            session_id=session_id,
            broadcast_scope=self.broadcast_scope,
            tenant_id=self.tenant_id,
        )


# ───────────────────────────────────────────────────────────────────
#  Bridge class
# ───────────────────────────────────────────────────────────────────


@dataclass
class _DedupEntry:
    key: str
    emitted_at: float


class UiSandboxSseBridge:
    """Translates V2 #1-#6 internal event callbacks into SSE frames.

    Usage::

        bridge = UiSandboxSseBridge()
        lifecycle = SandboxLifecycle(event_cb=bridge.on_lifecycle_event)
        service   = ScreenshotService(event_cb=bridge.on_screenshot_event)
        errors    = PreviewErrorBridge(event_cb=bridge.on_error_event)

    Every ``on_*_event`` matches the ``EventCallback`` signature V2 #1-#6
    share — ``(event_type: str, payload: Mapping[str, Any]) -> None``.
    The bridge re-shapes the payload according to V2 row 7 spec and
    forwards exactly one frame per unique capture / error transition to
    the configured :class:`EventPublisher`.
    """

    # Event types coming *into* the bridge from V2 #2 / V2 #3 that the
    # bridge treats as "screenshot captured" (both resolve to the same
    # spec topic ``ui_sandbox.screenshot``).
    _SCREENSHOT_IN_TYPES: tuple[str, ...] = (
        "ui_sandbox.screenshot",  # V2 #2 lifecycle + V2 #3 capture
    )

    # Event types coming *into* the bridge from V2 #5 that map to
    # ``ui_sandbox.error``.
    _ERROR_IN_TYPES: tuple[str, ...] = (
        "ui_sandbox.error.detected",
        "ui_sandbox.error.cleared",
    )

    def __init__(
        self,
        *,
        publisher: EventPublisher | None = None,
        image_url_strategy: str = DEFAULT_IMAGE_URL_STRATEGY,
        image_url_template: str = DEFAULT_IMAGE_URL_TEMPLATE,
        dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS,
        clock: Callable[[], float] = _time_mod.time,
    ) -> None:
        if image_url_strategy not in IMAGE_URL_STRATEGIES:
            raise ValueError(
                f"image_url_strategy must be one of {IMAGE_URL_STRATEGIES!r}, "
                f"got {image_url_strategy!r}"
            )
        if not isinstance(image_url_template, str) or not image_url_template.strip():
            raise ValueError("image_url_template must be a non-empty string")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if dedup_window_seconds < 0:
            raise ValueError("dedup_window_seconds must be non-negative")

        self._publisher: EventPublisher = publisher or BusEventPublisher()
        self._image_url_strategy = image_url_strategy
        self._image_url_template = image_url_template
        self._dedup_window = float(dedup_window_seconds)
        self._clock = clock

        self._lock = threading.RLock()
        self._dedup: dict[str, _DedupEntry] = {}
        # Counters are cheap but immensely useful for tests + metrics.
        self._screenshot_emitted = 0
        self._screenshot_deduped = 0
        self._error_emitted = 0
        self._error_cleared_emitted = 0
        self._ignored_events = 0
        self._publish_failures = 0

    # ─────────────── Counters (for tests / metrics) ───────────────

    @property
    def screenshot_emitted(self) -> int:
        with self._lock:
            return self._screenshot_emitted

    @property
    def screenshot_deduped(self) -> int:
        with self._lock:
            return self._screenshot_deduped

    @property
    def error_emitted(self) -> int:
        with self._lock:
            return self._error_emitted

    @property
    def error_cleared_emitted(self) -> int:
        with self._lock:
            return self._error_cleared_emitted

    @property
    def ignored_events(self) -> int:
        with self._lock:
            return self._ignored_events

    @property
    def publish_failures(self) -> int:
        with self._lock:
            return self._publish_failures

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe snapshot of the bridge's counters + config."""

        with self._lock:
            return {
                "schema_version": UI_SANDBOX_SSE_SCHEMA_VERSION,
                "image_url_strategy": self._image_url_strategy,
                "image_url_template": self._image_url_template,
                "dedup_window_seconds": self._dedup_window,
                "screenshot_emitted": self._screenshot_emitted,
                "screenshot_deduped": self._screenshot_deduped,
                "error_emitted": self._error_emitted,
                "error_cleared_emitted": self._error_cleared_emitted,
                "ignored_events": self._ignored_events,
                "publish_failures": self._publish_failures,
            }

    # ─────────────── Callbacks ───────────────

    def on_screenshot_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        capture_bytes: bytes | None = None,
    ) -> None:
        """Feed a V2 #2 / V2 #3 screenshot callback into the SSE bus.

        ``event_type`` — internal topic (``ui_sandbox.screenshot`` or a
        sibling such as ``ui_sandbox.screenshot.failed``).  The bridge
        only publishes ``ui_sandbox.screenshot``; others are counted as
        ``ignored_events``.
        """

        if event_type not in self._SCREENSHOT_IN_TYPES:
            with self._lock:
                self._ignored_events += 1
            return
        try:
            frame = build_screenshot_event_payload(
                payload,
                image_url_strategy=self._image_url_strategy,
                image_url_template=self._image_url_template,
                capture_bytes=capture_bytes,
                now=self._clock(),
            )
        except Exception as exc:  # payload malformed — log and move on.
            logger.warning(
                "ui_sandbox_sse: screenshot payload malformed: %s", exc
            )
            with self._lock:
                self._publish_failures += 1
            return

        dedup_key = f"screenshot::{frame['session_id']}::{frame['viewport']}::{frame['timestamp']:.6f}"
        if self._seen_recently(dedup_key):
            with self._lock:
                self._screenshot_deduped += 1
            return

        self._mark_seen(dedup_key)
        self._publish(
            SSE_EVENT_SCREENSHOT,
            frame,
            session_id=frame.get("session_id"),
        )
        with self._lock:
            self._screenshot_emitted += 1

    def on_error_event(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Feed a V2 #5 error callback into the SSE bus.

        Accepts both ``ui_sandbox.error.detected`` (new/persisting error)
        and ``ui_sandbox.error.cleared`` (error gone from latest scan).
        Both resolve to the single SSE topic ``ui_sandbox.error`` with
        the ``phase`` field disambiguating — frontend only needs one
        subscriber.
        """

        if event_type not in self._ERROR_IN_TYPES:
            with self._lock:
                self._ignored_events += 1
            return

        try:
            if event_type == "ui_sandbox.error.detected":
                frame = build_error_event_payload(
                    payload, phase=ERROR_PHASE_DETECTED, now=self._clock()
                )
            else:
                frame = build_error_cleared_payload(payload, now=self._clock())
        except Exception as exc:
            logger.warning(
                "ui_sandbox_sse: error payload malformed (%s): %s",
                event_type, exc,
            )
            with self._lock:
                self._publish_failures += 1
            return

        self._publish(
            SSE_EVENT_ERROR,
            frame,
            session_id=frame.get("session_id"),
        )
        with self._lock:
            if frame.get("phase") == ERROR_PHASE_CLEARED:
                self._error_cleared_emitted += 1
            else:
                self._error_emitted += 1

    # Convenience alias — V2 #2 lifecycle events include
    # ``ui_sandbox.screenshot`` with the ``ScreenshotResult.to_dict()``
    # shape (slightly different from V2 #3's ``ScreenshotCapture``
    # shape, but both carry ``session_id`` + ``viewport`` + ``captured_at``).
    on_lifecycle_event = on_screenshot_event

    # ─────────────── Internal ───────────────

    def _seen_recently(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            entry = self._dedup.get(key)
            if entry is None:
                return False
            if (now - entry.emitted_at) > self._dedup_window:
                return False
            return True

    def _mark_seen(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._dedup[key] = _DedupEntry(key=key, emitted_at=now)
            # GC old entries periodically so the dict doesn't grow
            # unbounded in long-running processes.
            if len(self._dedup) > 1024:
                cutoff = now - self._dedup_window
                stale = [k for k, e in self._dedup.items() if e.emitted_at < cutoff]
                for k in stale:
                    self._dedup.pop(k, None)

    def _publish(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        session_id: str | None,
    ) -> None:
        try:
            self._publisher.publish(
                event_type, payload, session_id=session_id or None
            )
        except Exception as exc:  # bus down / queue full / serialisation
            logger.warning(
                "ui_sandbox_sse: publisher failed for %s: %s",
                event_type, exc,
            )
            with self._lock:
                self._publish_failures += 1


# Module-level convenience publisher for callers that just want "publish
# one event now" without constructing a bridge.  Wraps :func:`build_*`
# and :class:`BusEventPublisher`.


def emit_ui_sandbox_screenshot_event(
    internal_payload: Mapping[str, Any],
    *,
    publisher: EventPublisher | None = None,
    image_url_strategy: str = DEFAULT_IMAGE_URL_STRATEGY,
    image_url_template: str = DEFAULT_IMAGE_URL_TEMPLATE,
    capture_bytes: bytes | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One-shot: shape + publish a single ``ui_sandbox.screenshot`` event.

    Returns the frame that was published so the caller can log / assert.
    """

    frame = build_screenshot_event_payload(
        internal_payload,
        image_url_strategy=image_url_strategy,
        image_url_template=image_url_template,
        capture_bytes=capture_bytes,
        now=now,
    )
    pub = publisher or BusEventPublisher()
    try:
        pub.publish(SSE_EVENT_SCREENSHOT, frame, session_id=frame.get("session_id"))
    except Exception as exc:
        logger.warning("ui_sandbox_sse: one-shot screenshot publish failed: %s", exc)
    return frame


def emit_ui_sandbox_error_event(
    internal_payload: Mapping[str, Any],
    *,
    phase: str = ERROR_PHASE_DETECTED,
    publisher: EventPublisher | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One-shot: shape + publish a single ``ui_sandbox.error`` event.

    Returns the frame that was published.
    """

    if phase == ERROR_PHASE_CLEARED:
        frame = build_error_cleared_payload(internal_payload, now=now)
    else:
        frame = build_error_event_payload(internal_payload, phase=phase, now=now)
    pub = publisher or BusEventPublisher()
    try:
        pub.publish(SSE_EVENT_ERROR, frame, session_id=frame.get("session_id"))
    except Exception as exc:
        logger.warning("ui_sandbox_sse: one-shot error publish failed: %s", exc)
    return frame


__all__ = __all__ + [
    "emit_ui_sandbox_screenshot_event",
    "emit_ui_sandbox_error_event",
]

"""V2 #4 (issue #318 / TODO row 1512) — Responsive viewport capture matrix.

Thin policy wrapper over :mod:`backend.ui_screenshot`'s ``ScreenshotService``
that captures the same preview URL across the three viewport presets the V2
spec calls out:

  * ``desktop`` — 1440 × 900
  * ``tablet`` — 768 × 1024
  * ``mobile`` — 375 × 812

Where this sits in the V2 stack
--------------------------------

V2 #1 ``ui_sandbox.py`` brings the Next.js dev server up inside a Docker
container and exposes a ``preview_url``.

V2 #2 ``ui_sandbox_lifecycle.py`` orchestrates ensure/hot-reload/screenshot/
teardown and exposes a ``screenshot_hook`` injection point.

V2 #3 ``ui_screenshot.py`` is the Playwright side of that hook — it already
registers the three viewport presets and knows how to render any single one
via ``ScreenshotService.capture(viewport=...)``.

**V2 #4 (this module)** closes the loop: V2 row 4 spec requires a batched
*three*-viewport capture, not a single render.  The agent needs one
structured report showing what the same page looks like at each breakpoint
so the visual-context injection (V2 row 5) can hand Opus one message with
three images, and so the SSE bus (V2 row 6) can emit a single
``ui_sandbox.viewport_batch.completed`` frame per batch instead of three
unrelated ``ui_sandbox.screenshot`` frames the UI has to stitch.

Contract
--------

* :class:`ResponsiveViewportCapture` wraps a ``ScreenshotService`` — it does
  **not** own Playwright.  Construction requires an already-wired service,
  which matches how V2 #2's :class:`SandboxLifecycle` composes V2 #1.
* :meth:`capture_all` is the one public verb.  It iterates the matrix in a
  deterministic order and returns a :class:`ResponsiveCaptureReport` with
  per-viewport :class:`ViewportCaptureOutcome` records (success with a
  :class:`~backend.ui_screenshot.ScreenshotCapture`, or failure with
  structured error info).
* ``failure_mode="collect"`` (default) keeps going on per-viewport failures
  so the agent sees what worked.  ``failure_mode="abort"`` stops on the
  first failure, emits the batch completion event with the partial report,
  then raises :class:`BatchAborted` carrying that report.
* Event emission is namespaced ``ui_sandbox.viewport_batch.*`` — V2 row 6
  SSE bus subscribes to the prefix and gets ``started`` / ``viewport_captured``
  / ``viewport_failed`` / ``completed`` as four first-class topics.  The
  per-viewport screenshot events already emitted by ``ScreenshotService``
  stay where they are — batch events add structure, they don't replace.
* :class:`ResponsiveCaptureReport.to_dict(include_bytes=True)` base64-encodes
  every capture so V2 row 5 can inject a three-image multimodal block.

Why a separate module and not a method on ``ScreenshotService``?
----------------------------------------------------------------

1. **Single responsibility** — ``ScreenshotService`` is one-shot plus a
   periodic loop.  A matrix is conceptually different: one call, N shots,
   one aggregated report, one lifecycle event pair.
2. **Event namespace clarity** — mixing ``ui_sandbox.screenshot`` with
   ``ui_sandbox.viewport_batch.*`` inside the same class would force every
   subscriber to filter on event-type prefix; keeping them in sibling
   modules means each subscriber picks the granularity it wants.
3. **Schema independence** — this module has its own
   :data:`UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION`, so adding fields to
   ``ResponsiveCaptureReport`` never forces a bump of
   :data:`ui_screenshot.UI_SCREENSHOT_SCHEMA_VERSION`.
4. **Testability** — a service-level batch method would need a test fixture
   that coordinates three viewports *plus* the single-viewport happy path;
   split modules let each test file pin one concept.

Design decisions
----------------

* **Serial capture, not parallel.**  ``PlaywrightEngine`` serialises every
  capture through an internal lock anyway (one Chromium browser is not
  thread-safe), so parallel capture would just contend on the lock while
  making event ordering non-deterministic.
* **Canonical order is desktop → tablet → mobile.**  Matches V2 row 4's
  spec wording and matches the declaration order of
  ``ui_screenshot.VIEWPORT_PRESETS``.  Callers can override via
  ``matrix=`` when they want a different order or a subset (e.g.
  mobile-only regression captures).
* **Deduping but preserving order.**  Repeated viewport names in a
  caller-supplied matrix raise :class:`ValueError` — the assumption is the
  caller made a typo, and silently deduping would mask it.
* **Report records the matrix that *ran*, not the matrix requested.**
  ``failure_mode="abort"`` truncates the outcomes list at the failure; the
  report's ``viewport_names`` tuple reflects the requested matrix so
  subscribers can still see which viewports were skipped.
* **Errors caught: ``ScreenshotError`` only.**  Unexpected exceptions (e.g.
  a ``TypeError`` inside an event callback) bubble up so bugs surface fast.
  ``ScreenshotService.capture`` already wraps engine-side ``Exception`` into
  ``ScreenshotError`` so this boundary is safe.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from backend.ui_screenshot import (
    SCREENSHOT_EVENT_CAPTURED,
    ScreenshotCapture,
    ScreenshotError,
    ScreenshotService,
    Viewport,
    get_viewport,
)

logger = logging.getLogger(__name__)


__all__ = [
    "UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION",
    "DEFAULT_VIEWPORT_MATRIX",
    "DEFAULT_FAILURE_MODE",
    "FAILURE_MODES",
    "VIEWPORT_BATCH_EVENT_STARTED",
    "VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED",
    "VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED",
    "VIEWPORT_BATCH_EVENT_COMPLETED",
    "VIEWPORT_BATCH_EVENT_TYPES",
    "ViewportCaptureOutcome",
    "ResponsiveCaptureReport",
    "ResponsiveViewportCapture",
    "ResponsiveViewportError",
    "InvalidViewportMatrix",
    "BatchAborted",
    "resolve_viewport_matrix",
    "render_responsive_report_markdown",
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on any shape change to :class:`ResponsiveCaptureReport.to_dict()` or
#: :class:`ViewportCaptureOutcome.to_dict()`.
UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION = "1.0.0"

#: Canonical capture order — matches V2 row 4 spec wording and the
#: declaration order of :data:`ui_screenshot.VIEWPORT_PRESETS`.
DEFAULT_VIEWPORT_MATRIX: tuple[str, ...] = ("desktop", "tablet", "mobile")

#: Valid values for the ``failure_mode=`` parameter on :meth:`capture_all`.
#: * ``collect`` — continue after per-viewport failure; return a partial
#:   report.  Default.
#: * ``abort`` — stop on first failure, emit ``completed`` event with the
#:   partial report, then raise :class:`BatchAborted` carrying it.
FAILURE_MODES: tuple[str, ...] = ("collect", "abort")

#: Default failure mode — collect lets the agent see what worked so it can
#: decide whether to fix a viewport-specific regression or abandon the
#: multi-breakpoint render.
DEFAULT_FAILURE_MODE = "collect"


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


VIEWPORT_BATCH_EVENT_STARTED = "ui_sandbox.viewport_batch.started"
VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED = "ui_sandbox.viewport_batch.viewport_captured"
VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED = "ui_sandbox.viewport_batch.viewport_failed"
VIEWPORT_BATCH_EVENT_COMPLETED = "ui_sandbox.viewport_batch.completed"


#: Full roster of batch events — V2 row 6 SSE bus subscribes on the
#: ``ui_sandbox.viewport_batch.`` prefix.  The per-viewport screenshot
#: events emitted by :class:`ScreenshotService` are *not* included here;
#: they're a separate topic family.
VIEWPORT_BATCH_EVENT_TYPES: tuple[str, ...] = (
    VIEWPORT_BATCH_EVENT_STARTED,
    VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED,
    VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED,
    VIEWPORT_BATCH_EVENT_COMPLETED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class ResponsiveViewportError(ScreenshotError):
    """Base class for ``ui_responsive_viewport`` errors.  Inherits from
    :class:`ScreenshotError` so callers that already ``except
    ScreenshotError`` keep working."""


class InvalidViewportMatrix(ResponsiveViewportError):
    """Raised by :func:`resolve_viewport_matrix` when the caller-supplied
    matrix is empty, contains unknowns, or contains duplicates."""


class BatchAborted(ResponsiveViewportError):
    """Raised by :meth:`ResponsiveViewportCapture.capture_all` when
    ``failure_mode="abort"`` and a per-viewport capture failed.  Carries
    the partial :class:`ResponsiveCaptureReport` so callers can still
    inspect which viewports succeeded before the abort."""

    def __init__(self, message: str, *, report: "ResponsiveCaptureReport") -> None:
        super().__init__(message)
        self.report = report


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def resolve_viewport_matrix(matrix: Iterable[str | Viewport]) -> tuple[Viewport, ...]:
    """Resolve a caller-supplied matrix of names / :class:`Viewport`
    instances to a tuple of :class:`Viewport` in the order supplied.

    * Accepts strings (resolved via :func:`get_viewport`) and direct
      :class:`Viewport` instances.  Mixing is allowed.
    * Preserves order — callers who want mobile-first captures supply
      ``("mobile", "tablet", "desktop")``.
    * Rejects empty matrices and exact-duplicate viewport *names* (by
      ``.name``) — a typo that asks for desktop twice is almost always a
      bug.
    * Raises :class:`InvalidViewportMatrix` on any of the above; the
      underlying ``ViewportUnknown`` from :func:`get_viewport` is wrapped
      for uniform handling upstream.
    """

    if matrix is None:
        raise InvalidViewportMatrix("matrix must not be None")
    resolved: list[Viewport] = []
    seen: set[str] = set()
    for i, entry in enumerate(matrix):
        if isinstance(entry, Viewport):
            vp = entry
        elif isinstance(entry, str):
            try:
                vp = get_viewport(entry)
            except ScreenshotError as exc:
                raise InvalidViewportMatrix(
                    f"matrix[{i}]={entry!r}: {exc}"
                ) from exc
        else:
            raise InvalidViewportMatrix(
                f"matrix[{i}] must be str or Viewport, got "
                f"{type(entry).__name__}"
            )
        if vp.name in seen:
            raise InvalidViewportMatrix(
                f"duplicate viewport {vp.name!r} in matrix at index {i}"
            )
        seen.add(vp.name)
        resolved.append(vp)
    if not resolved:
        raise InvalidViewportMatrix("matrix must not be empty")
    return tuple(resolved)


def render_responsive_report_markdown(report: "ResponsiveCaptureReport") -> str:
    """Deterministic operator-facing markdown summary of a batch report.

    Used by V2 row 6's SSE bus preview renderer and by ad-hoc CLI tooling.
    Output shape is pinned by tests so the UI side can parse it back if
    needed.
    """

    lines: list[str] = []
    lines.append(f"# Responsive capture — session `{report.session_id}`")
    lines.append("")
    lines.append(f"- preview_url: `{report.preview_url}`")
    lines.append(f"- path: `{report.path}`")
    lines.append(f"- viewports requested: {', '.join(report.viewport_names)}")
    lines.append(
        f"- outcome: {report.success_count}/{len(report.viewport_names)} succeeded"
    )
    lines.append(f"- duration: {report.duration_ms:.1f} ms")
    lines.append("")
    lines.append("| viewport | status | dims | bytes | error |")
    lines.append("| --- | --- | --- | --- | --- |")
    # Include outcomes in requested order; unreached viewports (abort
    # mode) render as `skipped`.
    reached = {o.viewport_name: o for o in report.outcomes}
    for name in report.viewport_names:
        o = reached.get(name)
        if o is None:
            lines.append(f"| {name} | skipped | — | — | — |")
            continue
        if o.success and o.capture is not None:
            dims = f"{o.capture.viewport.width}×{o.capture.viewport.height}"
            lines.append(
                f"| {name} | ok | {dims} | {o.capture.byte_len} | — |"
            )
        else:
            err = o.error_message or "unknown"
            etype = o.error_type or "Error"
            lines.append(f"| {name} | failed | — | — | {etype}: {err} |")
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────
#  Outcome + report records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ViewportCaptureOutcome:
    """Per-viewport slot in a batch report.

    Exactly one of ``capture`` / ``error_message`` is set depending on
    ``success``.  Frozen so downstream consumers can cache safely.
    """

    viewport_name: str
    success: bool
    capture: ScreenshotCapture | None = None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.viewport_name, str) or not self.viewport_name.strip():
            raise ValueError("viewport_name must be non-empty")
        if not isinstance(self.success, bool):
            raise ValueError("success must be bool")
        if self.success:
            if self.capture is None:
                raise ValueError("successful outcome requires capture")
            if not isinstance(self.capture, ScreenshotCapture):
                raise ValueError("capture must be a ScreenshotCapture")
            if self.capture.viewport.name != self.viewport_name:
                raise ValueError(
                    f"capture.viewport.name={self.capture.viewport.name!r} "
                    f"does not match outcome viewport_name="
                    f"{self.viewport_name!r}"
                )
            if self.error_type is not None or self.error_message is not None:
                raise ValueError(
                    "successful outcome must not carry error fields"
                )
        else:
            if self.capture is not None:
                raise ValueError(
                    "failed outcome must not carry a capture"
                )
            if not self.error_type or not self.error_message:
                raise ValueError(
                    "failed outcome requires error_type + error_message"
                )
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
            "viewport_name": self.viewport_name,
            "success": bool(self.success),
            "duration_ms": float(self.duration_ms),
        }
        if self.capture is not None:
            out["capture"] = self.capture.to_dict(include_bytes=include_bytes)
        else:
            out["capture"] = None
        out["error_type"] = self.error_type
        out["error_message"] = self.error_message
        return out


@dataclass(frozen=True)
class ResponsiveCaptureReport:
    """Aggregated report of one three-viewport (or N-viewport) batch.

    ``viewport_names`` is the *requested* matrix — in abort mode, some
    viewports may be absent from ``outcomes``.  Callers that need to
    detect skips can compute ``set(viewport_names) -
    {o.viewport_name for o in outcomes}``.
    """

    session_id: str
    preview_url: str
    path: str
    viewport_names: tuple[str, ...]
    outcomes: tuple[ViewportCaptureOutcome, ...]
    started_at: float
    finished_at: float
    failure_mode: str = DEFAULT_FAILURE_MODE

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(self.preview_url, str) or not self.preview_url.strip():
            raise ValueError("preview_url must be non-empty")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        if not isinstance(self.viewport_names, tuple):
            raise ValueError("viewport_names must be a tuple")
        if not self.viewport_names:
            raise ValueError("viewport_names must not be empty")
        if not isinstance(self.outcomes, tuple):
            raise ValueError("outcomes must be a tuple")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be >= started_at")
        if self.failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"failure_mode must be one of {FAILURE_MODES}, got "
                f"{self.failure_mode!r}"
            )
        for i, o in enumerate(self.outcomes):
            if not isinstance(o, ViewportCaptureOutcome):
                raise ValueError(
                    f"outcomes[{i}] must be a ViewportCaptureOutcome"
                )

    @property
    def success_count(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for o in self.outcomes if not o.success)

    @property
    def is_complete_success(self) -> bool:
        return (
            len(self.outcomes) == len(self.viewport_names)
            and all(o.success for o in self.outcomes)
        )

    @property
    def is_partial(self) -> bool:
        return not self.is_complete_success

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.finished_at - self.started_at) * 1000.0)

    @property
    def captures(self) -> tuple[ScreenshotCapture, ...]:
        return tuple(
            o.capture for o in self.outcomes if o.success and o.capture is not None
        )

    @property
    def failures(self) -> tuple[ViewportCaptureOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.success)

    @property
    def skipped_viewports(self) -> tuple[str, ...]:
        reached = {o.viewport_name for o in self.outcomes}
        return tuple(n for n in self.viewport_names if n not in reached)

    def to_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        return {
            "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "preview_url": self.preview_url,
            "path": self.path,
            "viewport_names": list(self.viewport_names),
            "failure_mode": self.failure_mode,
            "started_at": float(self.started_at),
            "finished_at": float(self.finished_at),
            "duration_ms": float(self.duration_ms),
            "success_count": int(self.success_count),
            "failure_count": int(self.failure_count),
            "is_complete_success": bool(self.is_complete_success),
            "skipped_viewports": list(self.skipped_viewports),
            "outcomes": [
                o.to_dict(include_bytes=include_bytes) for o in self.outcomes
            ],
        }


# ───────────────────────────────────────────────────────────────────
#  Event callback type
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


# ───────────────────────────────────────────────────────────────────
#  Main class
# ───────────────────────────────────────────────────────────────────


class ResponsiveViewportCapture:
    """Capture the same preview URL across a configured viewport matrix.

    Composition-over-inheritance — this class *has* a ``ScreenshotService``,
    it does not subclass it.  V2 #2 uses the same pattern with
    ``SandboxLifecycle`` composing ``SandboxManager``.

    Typical wire-up::

        service = ScreenshotService(engine=PlaywrightEngine(), event_cb=bus.emit)
        responsive = ResponsiveViewportCapture(service=service, event_cb=bus.emit)
        report = responsive.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            path="/pricing",
        )
        # report.captures is (desktop, tablet, mobile) captures in order
        for cap in report.captures:
            ...

    Thread-safety: all public state changes are guarded by an internal
    ``RLock``.  The underlying ``ScreenshotService`` and Playwright engine
    already serialise captures internally, so multiple concurrent
    :meth:`capture_all` invocations against different sessions are safe
    but will queue behind the engine lock.
    """

    def __init__(
        self,
        *,
        service: ScreenshotService,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        default_matrix: Sequence[str] = DEFAULT_VIEWPORT_MATRIX,
    ) -> None:
        if service is None:
            raise TypeError("service must be a ScreenshotService")
        if not hasattr(service, "capture"):
            raise TypeError("service must implement .capture(...)")
        # Validate the default matrix eagerly so bad config surfaces now,
        # not on the first capture.  Store resolved Viewports alongside
        # the names so capture_all can skip re-resolving on the hot path.
        resolved_default = resolve_viewport_matrix(tuple(default_matrix))

        self._service = service
        self._clock = clock
        self._event_cb = event_cb
        self._default_matrix: tuple[Viewport, ...] = resolved_default
        self._default_matrix_names: tuple[str, ...] = tuple(
            vp.name for vp in resolved_default
        )

        self._lock = threading.RLock()
        self._batch_count = 0
        self._success_batches = 0
        self._aborted_batches = 0
        self._partial_batches = 0
        self._last_report: ResponsiveCaptureReport | None = None

    # ─────────────── Accessors ───────────────

    @property
    def service(self) -> ScreenshotService:
        return self._service

    @property
    def default_matrix(self) -> tuple[str, ...]:
        return self._default_matrix_names

    def batch_count(self) -> int:
        with self._lock:
            return self._batch_count

    def success_batches(self) -> int:
        with self._lock:
            return self._success_batches

    def partial_batches(self) -> int:
        with self._lock:
            return self._partial_batches

    def aborted_batches(self) -> int:
        with self._lock:
            return self._aborted_batches

    def last_report(self) -> ResponsiveCaptureReport | None:
        with self._lock:
            return self._last_report

    # ─────────────── Core API ───────────────

    def capture_all(
        self,
        *,
        session_id: str,
        preview_url: str,
        path: str = "/",
        matrix: Sequence[str | Viewport] | None = None,
        failure_mode: str = DEFAULT_FAILURE_MODE,
        full_page: bool = False,
    ) -> ResponsiveCaptureReport:
        """Capture ``preview_url`` across every viewport in the matrix.

        Returns a :class:`ResponsiveCaptureReport` with per-viewport
        outcomes.  In ``failure_mode="abort"`` raises
        :class:`BatchAborted` (carrying the partial report) instead of
        returning when any viewport fails.
        """

        if failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"failure_mode must be one of {FAILURE_MODES}, got "
                f"{failure_mode!r}"
            )
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(preview_url, str) or not preview_url.strip():
            raise ValueError("preview_url must be non-empty")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path must start with '/'")

        viewports = (
            self._default_matrix
            if matrix is None
            else resolve_viewport_matrix(tuple(matrix))
        )
        viewport_names = tuple(vp.name for vp in viewports)

        started = self._clock()
        self._emit(
            VIEWPORT_BATCH_EVENT_STARTED,
            {
                "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
                "session_id": session_id,
                "preview_url": preview_url,
                "path": path,
                "viewport_names": list(viewport_names),
                "failure_mode": failure_mode,
                "started_at": float(started),
            },
        )

        outcomes: list[ViewportCaptureOutcome] = []
        aborted_on: str | None = None

        for vp in viewports:
            vp_started = self._clock()
            try:
                capture = self._service.capture(
                    session_id=session_id,
                    preview_url=preview_url,
                    viewport=vp,
                    path=path,
                    full_page=full_page,
                )
            except ScreenshotError as exc:
                vp_finished = self._clock()
                outcome = ViewportCaptureOutcome(
                    viewport_name=vp.name,
                    success=False,
                    capture=None,
                    error_type=type(exc).__name__,
                    error_message=str(exc) or type(exc).__name__,
                    duration_ms=max(
                        0.0, (vp_finished - vp_started) * 1000.0
                    ),
                )
                outcomes.append(outcome)
                self._emit(
                    VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED,
                    {
                        "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
                        "session_id": session_id,
                        "preview_url": preview_url,
                        "path": path,
                        "viewport_name": vp.name,
                        "error_type": outcome.error_type,
                        "error_message": outcome.error_message,
                        "duration_ms": float(outcome.duration_ms),
                        "at": float(vp_finished),
                    },
                )
                if failure_mode == "abort":
                    aborted_on = vp.name
                    break
                continue

            vp_finished = self._clock()
            outcome = ViewportCaptureOutcome(
                viewport_name=vp.name,
                success=True,
                capture=capture,
                duration_ms=max(0.0, (vp_finished - vp_started) * 1000.0),
            )
            outcomes.append(outcome)
            self._emit(
                VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED,
                {
                    "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
                    "session_id": session_id,
                    "preview_url": preview_url,
                    "path": path,
                    "viewport_name": vp.name,
                    "byte_len": int(capture.byte_len),
                    "duration_ms": float(outcome.duration_ms),
                    "at": float(vp_finished),
                },
            )

        finished = self._clock()
        report = ResponsiveCaptureReport(
            session_id=session_id,
            preview_url=preview_url,
            path=path,
            viewport_names=viewport_names,
            outcomes=tuple(outcomes),
            started_at=started,
            finished_at=finished,
            failure_mode=failure_mode,
        )

        with self._lock:
            self._batch_count += 1
            self._last_report = report
            if report.is_complete_success:
                self._success_batches += 1
            elif aborted_on is not None:
                self._aborted_batches += 1
            else:
                self._partial_batches += 1

        self._emit(VIEWPORT_BATCH_EVENT_COMPLETED, report.to_dict())

        if aborted_on is not None:
            raise BatchAborted(
                f"batch aborted on viewport {aborted_on!r} "
                f"({report.failure_count}/{len(outcomes)} failed)",
                report=report,
            )
        return report

    # ─────────────── Introspection ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe introspection of batch-level state.  The per-capture
        state (history / periodic) still lives on the underlying
        :class:`ScreenshotService` — use ``service.snapshot()`` for that.
        """

        with self._lock:
            last = self._last_report.to_dict() if self._last_report else None
            return {
                "schema_version": UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
                "default_matrix": list(self._default_matrix_names),
                "batch_count": int(self._batch_count),
                "success_batches": int(self._success_batches),
                "partial_batches": int(self._partial_batches),
                "aborted_batches": int(self._aborted_batches),
                "last_report": last,
                "now": float(self._clock()),
            }

    # ─────────────── Internal event plumbing ───────────────

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(payload))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "ui_responsive_viewport event callback raised: %s", exc
            )

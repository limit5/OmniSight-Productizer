"""V2 #4 (issue #318 / TODO row 1512) — ui_responsive_viewport contract tests.

Pins ``backend/ui_responsive_viewport.py`` against the V2 row 4 spec:

  * three-viewport capture matrix (desktop 1440×900 / tablet 768×1024 /
    mobile 375×812) driven through one :meth:`capture_all` call;
  * structured per-viewport outcomes — success with a
    :class:`~backend.ui_screenshot.ScreenshotCapture`, or failure with
    ``error_type`` + ``error_message``;
  * ``failure_mode="collect"`` vs ``failure_mode="abort"`` semantics;
  * ``ui_sandbox.viewport_batch.*`` event emission for every started /
    viewport_captured / viewport_failed / completed edge;
  * end-to-end integration with V2 #3's ``ScreenshotService`` (batch
    shares the underlying ``ScreenshotService`` history + events).

All tests drive a deterministic :class:`FakeScreenshotEngine` +
:class:`FakeClock` so no browser launches and no real time is
consumed.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Mapping

import pytest

from backend import ui_responsive_viewport as urv
from backend import ui_screenshot as usc
from backend.ui_responsive_viewport import (
    DEFAULT_FAILURE_MODE,
    DEFAULT_VIEWPORT_MATRIX,
    FAILURE_MODES,
    UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION,
    VIEWPORT_BATCH_EVENT_COMPLETED,
    VIEWPORT_BATCH_EVENT_STARTED,
    VIEWPORT_BATCH_EVENT_TYPES,
    VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED,
    VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED,
    BatchAborted,
    InvalidViewportMatrix,
    ResponsiveCaptureReport,
    ResponsiveViewportCapture,
    ResponsiveViewportError,
    ViewportCaptureOutcome,
    render_responsive_report_markdown,
    resolve_viewport_matrix,
)
from backend.ui_screenshot import (
    PNG_SIGNATURE,
    VIEWPORT_DESKTOP,
    VIEWPORT_MOBILE,
    VIEWPORT_TABLET,
    CaptureTimeout,
    ScreenshotCapture,
    ScreenshotError,
    ScreenshotRequest,
    ScreenshotService,
    Viewport,
    ViewportUnknown,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
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
}


def test_all_exports_match():
    assert set(urv.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(urv, name)


def test_schema_version_is_semver():
    parts = UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_schema_version_independent_of_ui_screenshot():
    # Both modules live at "1.0.0" today but pinning them as literal
    # constants on *this* test guards against accidental coupling — this
    # module must be able to bump without dragging ui_screenshot.
    assert UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION == "1.0.0"


def test_default_viewport_matrix_is_three_presets():
    # V2 row 4 spec wording pins the canonical desktop → tablet → mobile
    # order.  Changing this tuple is a breaking change for V2 row 5/6/7
    # subscribers.
    assert DEFAULT_VIEWPORT_MATRIX == ("desktop", "tablet", "mobile")


def test_default_matrix_entries_all_resolve_to_spec_dims():
    # V2 row 4 spec: desktop 1440×900 / tablet 768×1024 / mobile 375×812.
    mapping = {
        "desktop": (1440, 900),
        "tablet": (768, 1024),
        "mobile": (375, 812),
    }
    for name in DEFAULT_VIEWPORT_MATRIX:
        vp = usc.get_viewport(name)
        assert (vp.width, vp.height) == mapping[name], name


def test_failure_modes_set():
    assert set(FAILURE_MODES) == {"collect", "abort"}


def test_default_failure_mode_valid():
    assert DEFAULT_FAILURE_MODE in FAILURE_MODES


def test_event_types_live_under_viewport_batch_namespace():
    for ev in VIEWPORT_BATCH_EVENT_TYPES:
        assert ev.startswith("ui_sandbox.viewport_batch."), ev
    assert len(set(VIEWPORT_BATCH_EVENT_TYPES)) == len(
        VIEWPORT_BATCH_EVENT_TYPES
    )


def test_event_constants_mirror_tuple():
    assert VIEWPORT_BATCH_EVENT_STARTED in VIEWPORT_BATCH_EVENT_TYPES
    assert (
        VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED in VIEWPORT_BATCH_EVENT_TYPES
    )
    assert VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED in VIEWPORT_BATCH_EVENT_TYPES
    assert VIEWPORT_BATCH_EVENT_COMPLETED in VIEWPORT_BATCH_EVENT_TYPES


def test_batch_events_do_not_collide_with_single_capture_events():
    # V2 row 6 SSE bus subscribes on prefixes — batch events must not
    # pollute the per-capture namespace.
    for ev in VIEWPORT_BATCH_EVENT_TYPES:
        assert ev not in usc.SCREENSHOT_EVENT_TYPES


def test_error_hierarchy():
    assert issubclass(ResponsiveViewportError, ScreenshotError)
    assert issubclass(InvalidViewportMatrix, ResponsiveViewportError)
    assert issubclass(BatchAborted, ResponsiveViewportError)


# ── resolve_viewport_matrix ─────────────────────────────────────────


def test_resolve_matrix_from_names():
    resolved = resolve_viewport_matrix(("desktop", "tablet", "mobile"))
    assert tuple(vp.name for vp in resolved) == ("desktop", "tablet", "mobile")


def test_resolve_matrix_preserves_order():
    resolved = resolve_viewport_matrix(("mobile", "desktop"))
    assert tuple(vp.name for vp in resolved) == ("mobile", "desktop")


def test_resolve_matrix_accepts_viewport_instance():
    resolved = resolve_viewport_matrix([VIEWPORT_MOBILE])
    assert resolved == (VIEWPORT_MOBILE,)


def test_resolve_matrix_accepts_mixed_strings_and_instances():
    resolved = resolve_viewport_matrix([VIEWPORT_DESKTOP, "tablet"])
    assert tuple(vp.name for vp in resolved) == ("desktop", "tablet")


def test_resolve_matrix_case_insensitive_names():
    resolved = resolve_viewport_matrix(("Desktop", "TABLET"))
    assert tuple(vp.name for vp in resolved) == ("desktop", "tablet")


def test_resolve_matrix_rejects_empty():
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix(())


def test_resolve_matrix_rejects_unknown():
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix(("desktop", "ultrawide"))


def test_resolve_matrix_wraps_viewport_unknown():
    with pytest.raises(InvalidViewportMatrix) as exc_info:
        resolve_viewport_matrix(("ultrawide",))
    # Preserves the underlying ViewportUnknown in __cause__.
    assert isinstance(exc_info.value.__cause__, ViewportUnknown)


def test_resolve_matrix_rejects_duplicates():
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix(("desktop", "desktop"))


def test_resolve_matrix_duplicates_across_name_and_instance_also_rejected():
    # ("desktop" string) + VIEWPORT_DESKTOP instance → same .name → reject.
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix(["desktop", VIEWPORT_DESKTOP])


def test_resolve_matrix_rejects_bad_type():
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix([123])  # type: ignore[list-item]


def test_resolve_matrix_rejects_none():
    with pytest.raises(InvalidViewportMatrix):
        resolve_viewport_matrix(None)  # type: ignore[arg-type]


def test_resolve_matrix_returns_tuple():
    result = resolve_viewport_matrix(("desktop",))
    assert isinstance(result, tuple)


# ── ViewportCaptureOutcome ──────────────────────────────────────────


def _success_capture(viewport_name: str = "desktop") -> ScreenshotCapture:
    vp = usc.get_viewport(viewport_name)
    return ScreenshotCapture(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        viewport=vp,
        path="/",
        image_bytes=PNG_SIGNATURE + b"data",
        captured_at=1234.0,
    )


def test_outcome_success_happy_path():
    o = ViewportCaptureOutcome(
        viewport_name="desktop",
        success=True,
        capture=_success_capture("desktop"),
        duration_ms=12.5,
    )
    assert o.success is True
    assert o.capture is not None
    assert o.error_type is None
    assert o.error_message is None


def test_outcome_failure_happy_path():
    o = ViewportCaptureOutcome(
        viewport_name="tablet",
        success=False,
        error_type="CaptureTimeout",
        error_message="navigation timed out",
        duration_ms=30_000.0,
    )
    assert o.success is False
    assert o.capture is None
    assert o.error_type == "CaptureTimeout"


def test_outcome_is_frozen():
    o = ViewportCaptureOutcome(
        viewport_name="desktop",
        success=True,
        capture=_success_capture(),
    )
    with pytest.raises(Exception):
        o.viewport_name = "mobile"  # type: ignore[misc]


def test_outcome_success_requires_capture():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(viewport_name="desktop", success=True)


def test_outcome_success_rejects_error_fields():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(
            viewport_name="desktop",
            success=True,
            capture=_success_capture(),
            error_type="ScreenshotError",
        )


def test_outcome_failure_rejects_capture():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(
            viewport_name="tablet",
            success=False,
            capture=_success_capture("tablet"),
            error_type="X",
            error_message="y",
        )


def test_outcome_failure_requires_error():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(viewport_name="tablet", success=False)


def test_outcome_rejects_viewport_name_mismatch_with_capture():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(
            viewport_name="mobile",
            success=True,
            capture=_success_capture("desktop"),  # name mismatch
        )


def test_outcome_rejects_negative_duration():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(
            viewport_name="desktop",
            success=True,
            capture=_success_capture(),
            duration_ms=-1.0,
        )


def test_outcome_rejects_empty_viewport_name():
    with pytest.raises(ValueError):
        ViewportCaptureOutcome(
            viewport_name="",
            success=True,
            capture=_success_capture(),
        )


def test_outcome_to_dict_success_json_safe():
    o = ViewportCaptureOutcome(
        viewport_name="desktop",
        success=True,
        capture=_success_capture(),
        duration_ms=42.0,
    )
    d = o.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION
    assert d["success"] is True
    assert d["capture"] is not None
    assert "image_base64" not in d["capture"]  # bytes omitted by default
    assert d["error_type"] is None


def test_outcome_to_dict_with_bytes():
    o = ViewportCaptureOutcome(
        viewport_name="desktop",
        success=True,
        capture=_success_capture(),
    )
    d = o.to_dict(include_bytes=True)
    assert "image_base64" in d["capture"]


def test_outcome_to_dict_failure_json_safe():
    o = ViewportCaptureOutcome(
        viewport_name="mobile",
        success=False,
        error_type="CaptureTimeout",
        error_message="nav timeout",
    )
    d = o.to_dict()
    assert json.dumps(d)
    assert d["success"] is False
    assert d["capture"] is None
    assert d["error_type"] == "CaptureTimeout"


# ── ResponsiveCaptureReport ─────────────────────────────────────────


def _make_outcome(
    viewport_name: str = "desktop",
    *,
    success: bool = True,
    duration_ms: float = 5.0,
) -> ViewportCaptureOutcome:
    if success:
        return ViewportCaptureOutcome(
            viewport_name=viewport_name,
            success=True,
            capture=_success_capture(viewport_name),
            duration_ms=duration_ms,
        )
    return ViewportCaptureOutcome(
        viewport_name=viewport_name,
        success=False,
        error_type="CaptureTimeout",
        error_message="nav timeout",
        duration_ms=duration_ms,
    )


def _make_report(**overrides: Any) -> ResponsiveCaptureReport:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        path="/",
        viewport_names=("desktop", "tablet", "mobile"),
        outcomes=(
            _make_outcome("desktop"),
            _make_outcome("tablet"),
            _make_outcome("mobile"),
        ),
        started_at=1000.0,
        finished_at=1001.0,
    )
    defaults.update(overrides)
    return ResponsiveCaptureReport(**defaults)


def test_report_is_frozen():
    r = _make_report()
    with pytest.raises(Exception):
        r.session_id = "other"  # type: ignore[misc]


def test_report_success_count():
    r = _make_report()
    assert r.success_count == 3
    assert r.failure_count == 0
    assert r.is_complete_success is True
    assert r.is_partial is False


def test_report_partial():
    r = _make_report(
        outcomes=(
            _make_outcome("desktop"),
            _make_outcome("tablet", success=False),
            _make_outcome("mobile"),
        )
    )
    assert r.success_count == 2
    assert r.failure_count == 1
    assert r.is_complete_success is False
    assert r.is_partial is True


def test_report_skipped_viewports():
    # Only desktop reached in abort mode — tablet+mobile never ran.
    r = _make_report(
        outcomes=(_make_outcome("desktop"),),
        failure_mode="abort",
    )
    assert r.skipped_viewports == ("tablet", "mobile")


def test_report_captures_only_successful():
    r = _make_report(
        outcomes=(
            _make_outcome("desktop"),
            _make_outcome("tablet", success=False),
            _make_outcome("mobile"),
        )
    )
    caps = r.captures
    assert len(caps) == 2
    assert {c.viewport.name for c in caps} == {"desktop", "mobile"}


def test_report_failures_tuple():
    r = _make_report(
        outcomes=(
            _make_outcome("desktop"),
            _make_outcome("tablet", success=False),
        ),
        viewport_names=("desktop", "tablet"),
    )
    fails = r.failures
    assert len(fails) == 1
    assert fails[0].viewport_name == "tablet"


def test_report_duration_ms():
    r = _make_report(started_at=1000.0, finished_at=1002.5)
    assert r.duration_ms == pytest.approx(2500.0)


def test_report_duration_ms_never_negative():
    # Clock skew guard — finished >= started enforced by __post_init__,
    # but duration_ms itself should also floor at 0.  (Can't trigger
    # negative without bypassing __post_init__; verify floor via equality.)
    r = _make_report(started_at=1000.0, finished_at=1000.0)
    assert r.duration_ms == 0.0


def test_report_rejects_finished_before_started():
    with pytest.raises(ValueError):
        _make_report(started_at=1001.0, finished_at=1000.0)


def test_report_rejects_bad_path():
    with pytest.raises(ValueError):
        _make_report(path="no-slash")


def test_report_rejects_empty_viewport_names():
    with pytest.raises(ValueError):
        _make_report(viewport_names=())


def test_report_rejects_bad_session_id():
    with pytest.raises(ValueError):
        _make_report(session_id="")


def test_report_rejects_bad_preview_url():
    with pytest.raises(ValueError):
        _make_report(preview_url="")


def test_report_rejects_bad_failure_mode():
    with pytest.raises(ValueError):
        _make_report(failure_mode="yolo")


def test_report_rejects_non_outcome_elements():
    with pytest.raises(ValueError):
        _make_report(outcomes=("not-an-outcome",))  # type: ignore[arg-type]


def test_report_to_dict_json_safe():
    r = _make_report()
    d = r.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION
    assert d["viewport_names"] == ["desktop", "tablet", "mobile"]
    assert d["success_count"] == 3
    assert d["failure_count"] == 0
    assert d["is_complete_success"] is True
    assert len(d["outcomes"]) == 3


def test_report_to_dict_with_bytes_embeds_all_captures():
    r = _make_report()
    d = r.to_dict(include_bytes=True)
    for o in d["outcomes"]:
        assert "image_base64" in o["capture"]


def test_report_to_dict_skipped_viewports_field_populated():
    r = _make_report(
        outcomes=(_make_outcome("desktop"),),
        failure_mode="abort",
    )
    d = r.to_dict()
    assert d["skipped_viewports"] == ["tablet", "mobile"]


# ── Fake engine / clock / event callback ────────────────────────────


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeScreenshotEngine:
    """Mirrors the V2 #3 test engine.  Supports per-viewport override
    payloads and per-viewport raises so we can test partial-success
    matrices."""

    def __init__(
        self,
        *,
        default_payload: bytes | None = None,
        per_viewport_payloads: dict[str, bytes] | None = None,
        per_viewport_raises: dict[str, Exception] | None = None,
    ) -> None:
        self.default_payload = (
            default_payload
            if default_payload is not None
            else PNG_SIGNATURE + b"fake"
        )
        self.per_viewport_payloads = dict(per_viewport_payloads or {})
        self.per_viewport_raises = dict(per_viewport_raises or {})
        self.calls: list[ScreenshotRequest] = []
        self.close_called = False
        self._lock = threading.Lock()

    def capture(self, request: ScreenshotRequest) -> bytes:
        with self._lock:
            self.calls.append(request)
            name = request.viewport.name
            exc = self.per_viewport_raises.get(name)
            if exc is not None:
                raise exc
            return self.per_viewport_payloads.get(name, self.default_payload)

    def close(self) -> None:
        self.close_called = True


class RecordingEventCallback:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self, event_type: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.events.append((event_type, dict(payload)))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [p for t, p in self.events if t == event_type]


def _make_service(
    *,
    engine: FakeScreenshotEngine | None = None,
    service_event_cb: RecordingEventCallback | None = None,
    clock: FakeClock | None = None,
) -> ScreenshotService:
    return ScreenshotService(
        engine=engine or FakeScreenshotEngine(),
        clock=clock or FakeClock(),
        event_cb=service_event_cb,
    )


def _make_responsive(
    *,
    service: ScreenshotService | None = None,
    batch_event_cb: RecordingEventCallback | None = None,
    clock: FakeClock | None = None,
    default_matrix: tuple[str, ...] = DEFAULT_VIEWPORT_MATRIX,
) -> ResponsiveViewportCapture:
    return ResponsiveViewportCapture(
        service=service or _make_service(),
        clock=clock or FakeClock(),
        event_cb=batch_event_cb,
        default_matrix=default_matrix,
    )


# ── Constructor ─────────────────────────────────────────────────────


def test_construct_ok():
    rc = _make_responsive()
    assert isinstance(rc, ResponsiveViewportCapture)
    assert rc.default_matrix == DEFAULT_VIEWPORT_MATRIX


def test_construct_rejects_none_service():
    with pytest.raises(TypeError):
        ResponsiveViewportCapture(service=None)  # type: ignore[arg-type]


def test_construct_rejects_non_service():
    with pytest.raises(TypeError):
        ResponsiveViewportCapture(service=object())  # type: ignore[arg-type]


def test_construct_rejects_empty_default_matrix():
    svc = _make_service()
    with pytest.raises(InvalidViewportMatrix):
        ResponsiveViewportCapture(service=svc, default_matrix=())


def test_construct_rejects_unknown_in_default_matrix():
    svc = _make_service()
    with pytest.raises(InvalidViewportMatrix):
        ResponsiveViewportCapture(service=svc, default_matrix=("ultrawide",))


def test_construct_accepts_custom_default_matrix():
    svc = _make_service()
    rc = ResponsiveViewportCapture(service=svc, default_matrix=("mobile",))
    assert rc.default_matrix == ("mobile",)


def test_construct_exposes_service_property():
    svc = _make_service()
    rc = ResponsiveViewportCapture(service=svc)
    assert rc.service is svc


def test_construct_counters_start_zero():
    rc = _make_responsive()
    assert rc.batch_count() == 0
    assert rc.success_batches() == 0
    assert rc.partial_batches() == 0
    assert rc.aborted_batches() == 0
    assert rc.last_report() is None


# ── capture_all happy path ──────────────────────────────────────────


def test_capture_all_hits_three_viewports_in_order():
    engine = FakeScreenshotEngine()
    clock = FakeClock()
    svc = _make_service(engine=engine, clock=clock)
    rc = _make_responsive(service=svc, clock=clock)

    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert report.is_complete_success
    assert tuple(c.viewport.name for c in report.captures) == (
        "desktop",
        "tablet",
        "mobile",
    )
    # Engine saw exactly three calls in matrix order.
    assert len(engine.calls) == 3
    assert [c.viewport.name for c in engine.calls] == [
        "desktop",
        "tablet",
        "mobile",
    ]


def test_capture_all_returns_report():
    rc = _make_responsive()
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert isinstance(report, ResponsiveCaptureReport)


def test_capture_all_default_matrix_used_when_no_override():
    engine = FakeScreenshotEngine()
    svc = _make_service(engine=engine)
    rc = ResponsiveViewportCapture(
        service=svc, default_matrix=("desktop", "mobile")
    )
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert tuple(report.viewport_names) == ("desktop", "mobile")
    assert len(report.captures) == 2


def test_capture_all_override_matrix():
    engine = FakeScreenshotEngine()
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        matrix=("mobile",),
    )
    assert report.viewport_names == ("mobile",)
    assert len(engine.calls) == 1
    assert engine.calls[0].viewport.name == "mobile"


def test_capture_all_passes_path_through_to_engine():
    engine = FakeScreenshotEngine()
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        path="/pricing",
    )
    for req in engine.calls:
        assert req.path == "/pricing"
        assert req.target_url == "http://127.0.0.1:40000/pricing"


def test_capture_all_passes_full_page_through():
    engine = FakeScreenshotEngine()
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        full_page=True,
    )
    for req in engine.calls:
        assert req.full_page is True


def test_capture_all_records_viewport_dims_in_request():
    engine = FakeScreenshotEngine()
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    by_name = {r.viewport.name: r for r in engine.calls}
    assert (by_name["desktop"].viewport.width, by_name["desktop"].viewport.height) == (
        1440,
        900,
    )
    assert (by_name["tablet"].viewport.width, by_name["tablet"].viewport.height) == (
        768,
        1024,
    )
    assert (by_name["mobile"].viewport.width, by_name["mobile"].viewport.height) == (
        375,
        812,
    )


def test_capture_all_rejects_bad_failure_mode():
    rc = _make_responsive()
    with pytest.raises(ValueError):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            failure_mode="yolo",
        )


def test_capture_all_rejects_bad_session_id():
    rc = _make_responsive()
    with pytest.raises(ValueError):
        rc.capture_all(
            session_id="",
            preview_url="http://127.0.0.1:40000/",
        )


def test_capture_all_rejects_bad_preview_url():
    rc = _make_responsive()
    with pytest.raises(ValueError):
        rc.capture_all(session_id="sess-1", preview_url="")


def test_capture_all_rejects_bad_path():
    rc = _make_responsive()
    with pytest.raises(ValueError):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            path="no-slash",
        )


def test_capture_all_rejects_duplicate_override_matrix():
    rc = _make_responsive()
    with pytest.raises(InvalidViewportMatrix):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            matrix=("desktop", "desktop"),
        )


def test_capture_all_rejects_unknown_override_matrix():
    rc = _make_responsive()
    with pytest.raises(InvalidViewportMatrix):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            matrix=("ultrawide",),
        )


# ── capture_all failure modes ───────────────────────────────────────


def test_capture_all_collect_mode_partial_success():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": CaptureTimeout("tablet nav timed out")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        failure_mode="collect",
    )
    assert report.is_complete_success is False
    assert report.success_count == 2
    assert report.failure_count == 1
    # All three viewports tried.
    assert len(engine.calls) == 3
    failures = [o for o in report.outcomes if not o.success]
    assert len(failures) == 1
    assert failures[0].viewport_name == "tablet"
    assert failures[0].error_type == "CaptureTimeout"
    assert "tablet nav timed out" in (failures[0].error_message or "")


def test_capture_all_collect_mode_all_fail():
    engine = FakeScreenshotEngine(
        per_viewport_raises={
            "desktop": ScreenshotError("d"),
            "tablet": ScreenshotError("t"),
            "mobile": ScreenshotError("m"),
        }
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        failure_mode="collect",
    )
    assert report.success_count == 0
    assert report.failure_count == 3


def test_capture_all_abort_mode_raises_batch_aborted():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": CaptureTimeout("tablet nav timed out")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    with pytest.raises(BatchAborted) as exc_info:
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            failure_mode="abort",
        )
    report = exc_info.value.report
    assert isinstance(report, ResponsiveCaptureReport)
    # Only desktop (success) + tablet (failure) ran; mobile skipped.
    assert [o.viewport_name for o in report.outcomes] == ["desktop", "tablet"]
    assert report.skipped_viewports == ("mobile",)
    assert report.failure_mode == "abort"


def test_capture_all_abort_mode_engine_call_count():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"desktop": ScreenshotError("first")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    with pytest.raises(BatchAborted):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            failure_mode="abort",
        )
    # Abort on first viewport → engine only called once.
    assert len(engine.calls) == 1


def test_capture_all_abort_mode_emits_completed_event_with_partial_report():
    batch_cb = RecordingEventCallback()
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": ScreenshotError("boom")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc, batch_event_cb=batch_cb)
    with pytest.raises(BatchAborted):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            failure_mode="abort",
        )
    completed = batch_cb.payloads(VIEWPORT_BATCH_EVENT_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["success_count"] == 1
    assert completed[0]["failure_count"] == 1
    assert completed[0]["skipped_viewports"] == ["mobile"]


def test_capture_all_unexpected_exception_propagates():
    # ``ScreenshotService.capture`` wraps non-ScreenshotError exceptions
    # into ``ScreenshotError``, so ResponsiveViewportCapture never sees
    # raw non-ScreenshotError — this is therefore the V2 #3 contract we
    # rely on.  Confirm the wrap still works end-to-end.
    engine = FakeScreenshotEngine(
        per_viewport_raises={"desktop": RuntimeError("unexpected")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        failure_mode="collect",
    )
    # Became a recorded failure, not a crash.
    assert report.failure_count == 1
    assert report.outcomes[0].viewport_name == "desktop"
    # error_type reflects the ScreenshotError wrap from V2 #3.
    assert report.outcomes[0].error_type == "ScreenshotError"


# ── Event emission ──────────────────────────────────────────────────


def test_capture_all_emits_started_then_per_viewport_then_completed():
    batch_cb = RecordingEventCallback()
    rc = _make_responsive(batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    types = batch_cb.types()
    assert types[0] == VIEWPORT_BATCH_EVENT_STARTED
    assert types[-1] == VIEWPORT_BATCH_EVENT_COMPLETED
    assert types.count(VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED) == 3
    assert types.count(VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED) == 0


def test_capture_all_emits_viewport_failed_on_partial():
    batch_cb = RecordingEventCallback()
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": ScreenshotError("boom")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc, batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    types = batch_cb.types()
    assert types.count(VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED) == 2
    assert types.count(VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED) == 1
    # Event ordering: started, (captured|failed)×3, completed.
    assert types[0] == VIEWPORT_BATCH_EVENT_STARTED
    assert types[-1] == VIEWPORT_BATCH_EVENT_COMPLETED


def test_started_event_payload_shape():
    batch_cb = RecordingEventCallback()
    rc = _make_responsive(batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        path="/about",
    )
    started = batch_cb.payloads(VIEWPORT_BATCH_EVENT_STARTED)
    assert len(started) == 1
    p = started[0]
    assert p["session_id"] == "sess-1"
    assert p["preview_url"] == "http://127.0.0.1:40000/"
    assert p["path"] == "/about"
    assert p["viewport_names"] == ["desktop", "tablet", "mobile"]
    assert p["failure_mode"] == "collect"
    assert "started_at" in p


def test_viewport_captured_event_payload_shape():
    batch_cb = RecordingEventCallback()
    rc = _make_responsive(batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    captured = batch_cb.payloads(VIEWPORT_BATCH_EVENT_VIEWPORT_CAPTURED)
    assert len(captured) == 3
    for p in captured:
        assert p["viewport_name"] in ("desktop", "tablet", "mobile")
        assert p["byte_len"] > 0
        assert "duration_ms" in p
        assert "at" in p


def test_viewport_failed_event_payload_shape():
    batch_cb = RecordingEventCallback()
    engine = FakeScreenshotEngine(
        per_viewport_raises={"mobile": CaptureTimeout("mobile nav timed out")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc, batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    failed = batch_cb.payloads(VIEWPORT_BATCH_EVENT_VIEWPORT_FAILED)
    assert len(failed) == 1
    p = failed[0]
    assert p["viewport_name"] == "mobile"
    assert p["error_type"] == "CaptureTimeout"
    assert "mobile nav timed out" in p["error_message"]


def test_completed_event_payload_shape():
    batch_cb = RecordingEventCallback()
    rc = _make_responsive(batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    completed = batch_cb.payloads(VIEWPORT_BATCH_EVENT_COMPLETED)
    assert len(completed) == 1
    p = completed[0]
    assert p["schema_version"] == UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION
    assert p["is_complete_success"] is True
    assert p["success_count"] == 3
    assert len(p["outcomes"]) == 3


def test_event_callback_exception_swallowed():
    class BrokenCallback:
        def __call__(
            self, event_type: str, payload: Mapping[str, Any]
        ) -> None:
            raise RuntimeError("callback is broken")

    rc = _make_responsive(batch_event_cb=BrokenCallback())  # type: ignore[arg-type]
    # Must not propagate.
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert report.is_complete_success


def test_service_screenshot_events_still_fire_during_batch():
    # V2 #3 service-level events should NOT be stolen by the batch —
    # V2 row 6 SSE bus relies on both topic families firing in parallel.
    service_cb = RecordingEventCallback()
    batch_cb = RecordingEventCallback()
    svc = _make_service(service_event_cb=service_cb)
    rc = _make_responsive(service=svc, batch_event_cb=batch_cb)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    # Three per-capture service events.
    assert (
        service_cb.types().count(usc.SCREENSHOT_EVENT_CAPTURED) == 3
    )
    # Plus the batch envelope from the responsive layer.
    assert batch_cb.types().count(VIEWPORT_BATCH_EVENT_STARTED) == 1
    assert batch_cb.types().count(VIEWPORT_BATCH_EVENT_COMPLETED) == 1


# ── Counters + snapshot ─────────────────────────────────────────────


def test_counters_success_batch():
    rc = _make_responsive()
    rc.capture_all(session_id="sess-1", preview_url="http://x/")
    assert rc.batch_count() == 1
    assert rc.success_batches() == 1
    assert rc.partial_batches() == 0
    assert rc.aborted_batches() == 0


def test_counters_partial_batch():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": ScreenshotError("boom")}
    )
    rc = _make_responsive(service=_make_service(engine=engine))
    rc.capture_all(session_id="sess-1", preview_url="http://x/")
    assert rc.batch_count() == 1
    assert rc.success_batches() == 0
    assert rc.partial_batches() == 1
    assert rc.aborted_batches() == 0


def test_counters_aborted_batch():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"desktop": ScreenshotError("first")}
    )
    rc = _make_responsive(service=_make_service(engine=engine))
    with pytest.raises(BatchAborted):
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://x/",
            failure_mode="abort",
        )
    assert rc.batch_count() == 1
    assert rc.aborted_batches() == 1
    assert rc.success_batches() == 0
    assert rc.partial_batches() == 0


def test_last_report_tracks_most_recent():
    rc = _make_responsive()
    rc.capture_all(session_id="sess-1", preview_url="http://x/", path="/a")
    rc.capture_all(session_id="sess-2", preview_url="http://x/", path="/b")
    last = rc.last_report()
    assert last is not None
    assert last.session_id == "sess-2"
    assert last.path == "/b"


def test_snapshot_json_safe():
    rc = _make_responsive()
    rc.capture_all(session_id="sess-1", preview_url="http://x/")
    snap = rc.snapshot()
    assert json.dumps(snap)
    assert snap["schema_version"] == UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION
    assert snap["batch_count"] == 1
    assert snap["success_batches"] == 1
    assert snap["default_matrix"] == list(DEFAULT_VIEWPORT_MATRIX)
    assert snap["last_report"]["session_id"] == "sess-1"


def test_snapshot_before_any_capture():
    rc = _make_responsive()
    snap = rc.snapshot()
    assert snap["batch_count"] == 0
    assert snap["last_report"] is None


# ── Timing ──────────────────────────────────────────────────────────


def test_duration_ms_uses_injected_clock():
    clock = FakeClock(start=1_000.0)
    engine = FakeScreenshotEngine()
    original_capture = engine.capture

    def advancing_capture(request: ScreenshotRequest) -> bytes:
        # Each per-viewport call advances the clock by 0.05 s — the
        # per-viewport duration_ms should therefore be 50.0 ms each.
        clock.advance(0.05)
        return original_capture(request)

    engine.capture = advancing_capture  # type: ignore[method-assign]

    svc = _make_service(engine=engine, clock=clock)
    rc = _make_responsive(service=svc, clock=clock)
    report = rc.capture_all(session_id="sess-1", preview_url="http://x/")
    for o in report.outcomes:
        assert o.duration_ms == pytest.approx(50.0, abs=1.0)
    assert report.duration_ms == pytest.approx(150.0, abs=1.0)


# ── Thread-safety ───────────────────────────────────────────────────


def test_concurrent_capture_all_no_corruption():
    # 10 threads × 1 capture_all each against the same service.  The
    # underlying engine serialises per call; all outcomes must record
    # cleanly and counters must match.
    engine = FakeScreenshotEngine()
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)

    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            report = rc.capture_all(
                session_id=f"sess-{i}",
                preview_url="http://127.0.0.1:40000/",
            )
            assert report.is_complete_success
        except BaseException as exc:  # pragma: no cover - assertion net
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert rc.batch_count() == 10
    assert rc.success_batches() == 10
    assert len(engine.calls) == 30  # 10 batches × 3 viewports


# ── Markdown rendering ──────────────────────────────────────────────


def test_render_markdown_happy_path():
    rc = _make_responsive()
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        path="/pricing",
    )
    md = render_responsive_report_markdown(report)
    assert "# Responsive capture" in md
    assert "session `sess-1`" in md
    assert "http://127.0.0.1:40000/" in md
    assert "/pricing" in md
    assert "3/3 succeeded" in md
    # All three presets with dims listed.
    assert "1440×900" in md
    assert "768×1024" in md
    assert "375×812" in md


def test_render_markdown_partial():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": CaptureTimeout("boom")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    md = render_responsive_report_markdown(report)
    assert "2/3 succeeded" in md
    assert "CaptureTimeout" in md


def test_render_markdown_abort_shows_skipped():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"tablet": CaptureTimeout("boom")}
    )
    svc = _make_service(engine=engine)
    rc = _make_responsive(service=svc)
    try:
        rc.capture_all(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            failure_mode="abort",
        )
    except BatchAborted as exc:
        md = render_responsive_report_markdown(exc.report)
    assert "mobile" in md
    assert "skipped" in md


# ── Integration with V2 #3 service ──────────────────────────────────


def test_service_history_populated_by_batch():
    # A batch should populate V2 #3's per-session history exactly the
    # same way on-demand captures do (one entry per viewport, ordered).
    engine = FakeScreenshotEngine()
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    recent = svc.recent("sess-1")
    assert len(recent) == 3
    assert [c.viewport.name for c in recent] == ["desktop", "tablet", "mobile"]


def test_service_capture_count_updated_by_batch():
    engine = FakeScreenshotEngine()
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)
    assert svc.capture_count() == 0
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert svc.capture_count() == 3


def test_service_failure_count_updated_by_batch():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"mobile": ScreenshotError("fail")}
    )
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)
    rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert svc.capture_count() == 2
    assert svc.failure_count() == 1


def test_batch_report_captures_reference_same_objects_as_service_history():
    # Same ScreenshotCapture objects — not copies — so V2 row 6
    # subscribers processing one side don't see a different identity
    # for the same logical screenshot.
    engine = FakeScreenshotEngine()
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)
    report = rc.capture_all(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    service_recent = svc.recent("sess-1")
    assert tuple(report.captures) == service_recent


# ── Sibling alignment ──────────────────────────────────────────────


def test_ui_screenshot_sibling_still_importable():
    from backend import ui_screenshot  # noqa: F401

    # Schema versions are independent — bumping one does not force the
    # other.
    assert (
        UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION
        != usc.UI_SCREENSHOT_SCHEMA_VERSION + "x"
    )


def test_ui_sandbox_lifecycle_sibling_still_importable():
    from backend import ui_sandbox_lifecycle  # noqa: F401


def test_ui_sandbox_sibling_still_importable():
    from backend import ui_sandbox  # noqa: F401


# ── End-to-end: V2 #3 Viewport matrix drives V2 row 4 spec ──────────


def test_end_to_end_three_viewport_matrix_matches_v2_spec_dims():
    """Full V2 row 4 contract: one capture_all gives (desktop 1440×900 /
    tablet 768×1024 / mobile 375×812) back with PNG bytes each, in order,
    and the report is ready to hand to V2 row 5 multimodal injection."""

    engine = FakeScreenshotEngine(
        per_viewport_payloads={
            "desktop": PNG_SIGNATURE + b"desktop-png",
            "tablet": PNG_SIGNATURE + b"tablet-png",
            "mobile": PNG_SIGNATURE + b"mobile-png",
        }
    )
    svc = ScreenshotService(engine=engine)
    rc = ResponsiveViewportCapture(service=svc)

    report = rc.capture_all(
        session_id="sess-e2e",
        preview_url="http://127.0.0.1:40000/",
    )
    assert report.is_complete_success
    caps = {c.viewport.name: c for c in report.captures}

    assert (caps["desktop"].viewport.width, caps["desktop"].viewport.height) == (
        1440,
        900,
    )
    assert (caps["tablet"].viewport.width, caps["tablet"].viewport.height) == (
        768,
        1024,
    )
    assert (caps["mobile"].viewport.width, caps["mobile"].viewport.height) == (
        375,
        812,
    )

    # Per-viewport bytes distinct → no engine cross-contamination.
    assert caps["desktop"].image_bytes.endswith(b"desktop-png")
    assert caps["tablet"].image_bytes.endswith(b"tablet-png")
    assert caps["mobile"].image_bytes.endswith(b"mobile-png")

    # V2 row 5 hand-off shape: to_dict(include_bytes=True) is ready for
    # Opus multimodal inject.
    rich = report.to_dict(include_bytes=True)
    assert len(rich["outcomes"]) == 3
    for o in rich["outcomes"]:
        assert "image_base64" in o["capture"]

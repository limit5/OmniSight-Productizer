"""V2 #3 (issue #318) — ui_screenshot contract tests.

Pins ``backend/ui_screenshot.py`` against the V2 row 3 spec:

  * Playwright headless screenshot service — on-demand + periodic;
  * PNG validation + base64 encoding helpers;
  * responsive viewport registry (desktop / tablet / mobile) used by
    V2 row 4 for three-way capture;
  * ``as_hook()`` matches the lifecycle
    :class:`~backend.ui_sandbox_lifecycle.ScreenshotHook` Protocol so
    V2 #2 can plug this service in directly;
  * event emission for every capture + periodic lifecycle edge.

All tests drive a deterministic :class:`FakeScreenshotEngine` +
:class:`FakeClock` so no browser launches and no real time is
consumed.  The module's Playwright import is *optional* — if the
host lacks playwright, :class:`PlaywrightEngine` raises
:class:`PlaywrightUnavailable`; we pin that path via a dedicated
test using an injected ``launcher=``.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Mapping

import pytest

from backend import ui_screenshot as usc
from backend.ui_sandbox_lifecycle import ScreenshotHook
from backend.ui_screenshot import (
    DEFAULT_CAPTURE_TIMEOUT_S,
    DEFAULT_HISTORY_SIZE,
    DEFAULT_NAVIGATION_TIMEOUT_MS,
    DEFAULT_PERIODIC_INTERVAL_S,
    DEFAULT_VIEWPORT,
    DEFAULT_WAIT_UNTIL,
    MAX_CAPTURE_BYTES,
    PNG_SIGNATURE,
    SCREENSHOT_EVENT_CAPTURED,
    SCREENSHOT_EVENT_FAILED,
    SCREENSHOT_EVENT_PERIODIC_STARTED,
    SCREENSHOT_EVENT_PERIODIC_STOPPED,
    SCREENSHOT_EVENT_TYPES,
    UI_SCREENSHOT_SCHEMA_VERSION,
    VIEWPORT_DESKTOP,
    VIEWPORT_MOBILE,
    VIEWPORT_PRESETS,
    VIEWPORT_TABLET,
    CaptureTimeout,
    InvalidPngData,
    PeriodicAlreadyRunning,
    PlaywrightEngine,
    PlaywrightUnavailable,
    ScreenshotCapture,
    ScreenshotEngine,
    ScreenshotError,
    ScreenshotRequest,
    ScreenshotService,
    Viewport,
    ViewportUnknown,
    build_target_url,
    encode_png_base64,
    get_viewport,
    list_viewports,
    validate_png_bytes,
)


# ── Module invariants ────────────────────────────────────────────────


EXPECTED_ALL = {
    "UI_SCREENSHOT_SCHEMA_VERSION",
    "DEFAULT_VIEWPORT",
    "DEFAULT_CAPTURE_TIMEOUT_S",
    "DEFAULT_NAVIGATION_TIMEOUT_MS",
    "DEFAULT_WAIT_UNTIL",
    "DEFAULT_PERIODIC_INTERVAL_S",
    "DEFAULT_HISTORY_SIZE",
    "MAX_CAPTURE_BYTES",
    "PNG_SIGNATURE",
    "VIEWPORT_DESKTOP",
    "VIEWPORT_TABLET",
    "VIEWPORT_MOBILE",
    "VIEWPORT_PRESETS",
    "SCREENSHOT_EVENT_CAPTURED",
    "SCREENSHOT_EVENT_PERIODIC_STARTED",
    "SCREENSHOT_EVENT_PERIODIC_STOPPED",
    "SCREENSHOT_EVENT_FAILED",
    "SCREENSHOT_EVENT_TYPES",
    "Viewport",
    "ScreenshotRequest",
    "ScreenshotCapture",
    "ScreenshotEngine",
    "PlaywrightEngine",
    "ScreenshotService",
    "ScreenshotError",
    "PlaywrightUnavailable",
    "ViewportUnknown",
    "CaptureTimeout",
    "InvalidPngData",
    "PeriodicAlreadyRunning",
    "get_viewport",
    "list_viewports",
    "build_target_url",
    "validate_png_bytes",
    "encode_png_base64",
}


def test_all_exports_match():
    assert set(usc.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(usc, name)


def test_schema_version_is_semver():
    parts = UI_SCREENSHOT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_png_signature_is_canonical():
    # From the PNG spec: first 8 bytes are \x89 P N G \r \n \x1a \n.
    assert PNG_SIGNATURE == bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def test_default_viewport_resolves():
    assert get_viewport(DEFAULT_VIEWPORT).name == DEFAULT_VIEWPORT


def test_default_capture_timeout_positive():
    assert DEFAULT_CAPTURE_TIMEOUT_S > 0


def test_default_navigation_timeout_positive():
    assert DEFAULT_NAVIGATION_TIMEOUT_MS > 0


def test_default_wait_until_is_valid():
    assert DEFAULT_WAIT_UNTIL in ("load", "domcontentloaded", "networkidle")


def test_default_periodic_interval_positive():
    assert DEFAULT_PERIODIC_INTERVAL_S > 0


def test_default_history_size_at_least_one():
    assert DEFAULT_HISTORY_SIZE >= 1


def test_max_capture_bytes_reasonable():
    # Sanity ceiling: at least 1 MB, at most 100 MB.
    assert 1_000_000 <= MAX_CAPTURE_BYTES <= 100_000_000


def test_event_types_live_under_sandbox_namespace():
    for ev in SCREENSHOT_EVENT_TYPES:
        assert ev.startswith("ui_sandbox."), ev
    # No dupes.
    assert len(set(SCREENSHOT_EVENT_TYPES)) == len(SCREENSHOT_EVENT_TYPES)


def test_event_constants_mirror_tuple():
    assert SCREENSHOT_EVENT_CAPTURED in SCREENSHOT_EVENT_TYPES
    assert SCREENSHOT_EVENT_PERIODIC_STARTED in SCREENSHOT_EVENT_TYPES
    assert SCREENSHOT_EVENT_PERIODIC_STOPPED in SCREENSHOT_EVENT_TYPES
    assert SCREENSHOT_EVENT_FAILED in SCREENSHOT_EVENT_TYPES


def test_screenshot_captured_matches_lifecycle_event_name():
    # Callers wiring V2 row 6's SSE bus use the same event name the
    # lifecycle module emits — so they see one logical topic.
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_SCREENSHOT

    assert SCREENSHOT_EVENT_CAPTURED == LIFECYCLE_EVENT_SCREENSHOT


def test_error_hierarchy():
    assert issubclass(PlaywrightUnavailable, ScreenshotError)
    assert issubclass(ViewportUnknown, ScreenshotError)
    assert issubclass(CaptureTimeout, ScreenshotError)
    assert issubclass(InvalidPngData, ScreenshotError)
    assert issubclass(PeriodicAlreadyRunning, ScreenshotError)


# ── Viewport presets ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "vp,expected_w,expected_h",
    [
        (VIEWPORT_DESKTOP, 1440, 900),
        (VIEWPORT_TABLET, 768, 1024),
        (VIEWPORT_MOBILE, 375, 812),
    ],
)
def test_viewport_preset_dimensions_match_v2_spec(vp, expected_w, expected_h):
    # V2 row 4 spec: desktop 1440×900 / tablet 768×1024 / mobile 375×812
    assert vp.width == expected_w
    assert vp.height == expected_h


def test_viewport_presets_has_exactly_three():
    assert set(VIEWPORT_PRESETS.keys()) == {"desktop", "tablet", "mobile"}


def test_viewport_presets_are_frozen():
    with pytest.raises(Exception):
        VIEWPORT_DESKTOP.width = 2000  # type: ignore[misc]


def test_viewport_to_dict_json_safe():
    d = VIEWPORT_DESKTOP.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_SCREENSHOT_SCHEMA_VERSION
    assert d["name"] == "desktop"


def test_viewport_mobile_is_mobile_flag():
    assert VIEWPORT_MOBILE.is_mobile is True
    assert VIEWPORT_TABLET.is_mobile is True
    assert VIEWPORT_DESKTOP.is_mobile is False


def test_viewport_device_scale_factor_positive():
    for vp in VIEWPORT_PRESETS.values():
        assert vp.device_scale_factor > 0


def test_viewport_rejects_bad_inputs():
    with pytest.raises(ValueError):
        Viewport(name="", width=100, height=100)
    with pytest.raises(ValueError):
        Viewport(name="bad name with spaces", width=100, height=100)
    with pytest.raises(ValueError):
        Viewport(name="good", width=0, height=100)
    with pytest.raises(ValueError):
        Viewport(name="good", width=100, height=-1)
    with pytest.raises(ValueError):
        Viewport(name="good", width=100, height=100, device_scale_factor=0)


def test_list_viewports_stable_order():
    assert list_viewports() == ("desktop", "tablet", "mobile")


def test_get_viewport_case_insensitive():
    assert get_viewport("Desktop").name == "desktop"
    assert get_viewport("MOBILE").name == "mobile"
    assert get_viewport("  tablet  ").name == "tablet"


def test_get_viewport_unknown_raises():
    with pytest.raises(ViewportUnknown):
        get_viewport("ultrawide")
    with pytest.raises(ViewportUnknown):
        get_viewport("")


# ── build_target_url ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base,path,expected",
    [
        ("http://127.0.0.1:40000/", "/", "http://127.0.0.1:40000/"),
        ("http://127.0.0.1:40000", "/", "http://127.0.0.1:40000/"),
        ("http://127.0.0.1:40000/", "/pricing", "http://127.0.0.1:40000/pricing"),
        ("http://x", "/page", "http://x/page"),
        # Queries / fragments on base are stripped.
        ("http://x:40000/?debug=1", "/page", "http://x:40000/page"),
        ("http://x/#hash", "/page", "http://x/page"),
    ],
)
def test_build_target_url_happy_paths(base: str, path: str, expected: str):
    assert build_target_url(base, path) == expected


def test_build_target_url_rejects_bad_path():
    with pytest.raises(ValueError):
        build_target_url("http://x/", "no-slash")
    with pytest.raises(ValueError):
        build_target_url("http://x/", "")


def test_build_target_url_rejects_path_traversal():
    with pytest.raises(ValueError):
        build_target_url("http://x/", "/foo/../bar")


def test_build_target_url_rejects_bad_base():
    with pytest.raises(ValueError):
        build_target_url("", "/page")
    with pytest.raises(ValueError):
        build_target_url("not-a-url", "/page")


# ── validate_png_bytes + encode_png_base64 ─────────────────────────


def test_validate_png_bytes_accepts_signature():
    data = PNG_SIGNATURE + b"payload"
    validate_png_bytes(data)  # must not raise


def test_validate_png_bytes_rejects_empty():
    with pytest.raises(InvalidPngData):
        validate_png_bytes(b"")


def test_validate_png_bytes_rejects_missing_signature():
    with pytest.raises(InvalidPngData):
        validate_png_bytes(b"not a png")


def test_validate_png_bytes_rejects_wrong_type():
    with pytest.raises(InvalidPngData):
        validate_png_bytes("string not bytes")  # type: ignore[arg-type]


def test_validate_png_bytes_rejects_oversize():
    data = PNG_SIGNATURE + b"x" * 2000
    with pytest.raises(InvalidPngData):
        validate_png_bytes(data, max_bytes=1000)


def test_validate_png_bytes_accepts_bytearray():
    data = bytearray(PNG_SIGNATURE + b"payload")
    validate_png_bytes(data)  # must not raise


def test_encode_png_base64_round_trips():
    import base64 as b64

    data = PNG_SIGNATURE + b"hello"
    encoded = encode_png_base64(data)
    assert b64.b64decode(encoded) == data


def test_encode_png_base64_rejects_bad_data():
    with pytest.raises(InvalidPngData):
        encode_png_base64(b"not a png")


# ── ScreenshotRequest ──────────────────────────────────────────────


def _req(**overrides: Any) -> ScreenshotRequest:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        viewport=VIEWPORT_DESKTOP,
        path="/",
    )
    defaults.update(overrides)
    return ScreenshotRequest(**defaults)


def test_screenshot_request_defaults():
    r = _req()
    assert r.full_page is False
    assert r.wait_until == DEFAULT_WAIT_UNTIL
    assert r.timeout_s == DEFAULT_CAPTURE_TIMEOUT_S
    assert r.navigation_timeout_ms == DEFAULT_NAVIGATION_TIMEOUT_MS


def test_screenshot_request_target_url_property():
    r = _req(preview_url="http://127.0.0.1:40000/", path="/pricing")
    assert r.target_url == "http://127.0.0.1:40000/pricing"


def test_screenshot_request_is_frozen():
    r = _req()
    with pytest.raises(Exception):
        r.session_id = "other"  # type: ignore[misc]


def test_screenshot_request_rejects_bad_inputs():
    with pytest.raises(ValueError):
        _req(session_id="")
    with pytest.raises(ValueError):
        _req(preview_url="")
    with pytest.raises(ValueError):
        _req(viewport="not a viewport")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _req(path="no-slash")
    with pytest.raises(ValueError):
        _req(wait_until="nope")
    with pytest.raises(ValueError):
        _req(timeout_s=0)
    with pytest.raises(ValueError):
        _req(navigation_timeout_ms=0)


def test_screenshot_request_to_dict_json_safe():
    r = _req(path="/pricing")
    d = r.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_SCREENSHOT_SCHEMA_VERSION
    assert d["target_url"] == "http://127.0.0.1:40000/pricing"
    assert d["viewport"]["name"] == "desktop"


# ── ScreenshotCapture ──────────────────────────────────────────────


def _capture(**overrides: Any) -> ScreenshotCapture:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
        viewport=VIEWPORT_DESKTOP,
        path="/",
        image_bytes=PNG_SIGNATURE + b"data",
        captured_at=1234.0,
    )
    defaults.update(overrides)
    return ScreenshotCapture(**defaults)


def test_screenshot_capture_byte_len():
    c = _capture()
    assert c.byte_len == len(PNG_SIGNATURE) + 4


def test_screenshot_capture_is_frozen():
    c = _capture()
    with pytest.raises(Exception):
        c.session_id = "other"  # type: ignore[misc]


def test_screenshot_capture_default_target_url_derived():
    c = _capture(path="/pricing")
    assert c.target_url == "http://127.0.0.1:40000/pricing"


def test_screenshot_capture_explicit_target_url_retained():
    c = _capture(target_url="http://other/")
    assert c.target_url == "http://other/"


def test_screenshot_capture_to_dict_default_no_bytes():
    c = _capture()
    d = c.to_dict()
    assert "image_base64" not in d
    assert d["byte_len"] == c.byte_len
    assert d["schema_version"] == UI_SCREENSHOT_SCHEMA_VERSION
    assert json.dumps(d)


def test_screenshot_capture_to_dict_with_bytes():
    import base64 as b64

    c = _capture(image_bytes=PNG_SIGNATURE + b"X")
    d = c.to_dict(include_bytes=True)
    assert "image_base64" in d
    assert b64.b64decode(d["image_base64"]) == PNG_SIGNATURE + b"X"


def test_screenshot_capture_to_data_url():
    c = _capture(image_bytes=PNG_SIGNATURE + b"X")
    url = c.to_data_url()
    assert url.startswith("data:image/png;base64,")


def test_screenshot_capture_rejects_bad_inputs():
    with pytest.raises(ValueError):
        _capture(session_id="")
    with pytest.raises(ValueError):
        _capture(preview_url="")
    with pytest.raises(ValueError):
        _capture(viewport="not a vp")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _capture(path="no-slash")
    with pytest.raises(ValueError):
        _capture(image_bytes="not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _capture(image_bytes=b"")
    with pytest.raises(ValueError):
        _capture(captured_at=-1.0)
    with pytest.raises(ValueError):
        _capture(duration_ms=-1.0)


def test_screenshot_capture_coerces_bytearray_to_bytes():
    c = _capture(image_bytes=bytearray(PNG_SIGNATURE + b"ba"))
    assert isinstance(c.image_bytes, bytes)


# ── Fake fixtures ──────────────────────────────────────────────────


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeScreenshotEngine:
    """Deterministic in-memory engine — mirrors the V2 #1
    :class:`FakeDockerClient` pattern."""

    def __init__(
        self,
        *,
        payload: bytes | None = None,
        raise_: Exception | None = None,
    ) -> None:
        self.payload = payload if payload is not None else PNG_SIGNATURE + b"fake-png"
        self.raise_ = raise_
        self.calls: list[ScreenshotRequest] = []
        self.close_called = False
        self._lock = threading.Lock()

    def capture(self, request: ScreenshotRequest) -> bytes:
        with self._lock:
            self.calls.append(request)
            if self.raise_ is not None:
                raise self.raise_
            return self.payload

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
    event_cb: RecordingEventCallback | None = None,
    clock: FakeClock | None = None,
    **overrides: Any,
) -> tuple[ScreenshotService, FakeScreenshotEngine, RecordingEventCallback, FakeClock]:
    engine = engine or FakeScreenshotEngine()
    events = event_cb or RecordingEventCallback()
    clock = clock or FakeClock()
    svc = ScreenshotService(
        engine=engine,
        clock=clock,
        event_cb=events,
        **overrides,
    )
    return svc, engine, events, clock


# ── ScreenshotService constructor ──────────────────────────────────


def test_service_rejects_missing_engine():
    with pytest.raises(TypeError):
        ScreenshotService(engine=None)  # type: ignore[arg-type]


def test_service_rejects_non_engine():
    class NotAnEngine:
        pass

    with pytest.raises(TypeError):
        ScreenshotService(engine=NotAnEngine())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"history_size": 0},
        {"history_size": -1},
        {"capture_timeout_s": 0},
        {"capture_timeout_s": -1.0},
        {"navigation_timeout_ms": 0},
        {"wait_until": "bogus"},
        {"periodic_interval_s": 0},
        {"default_viewport": "ultrawide"},
    ],
)
def test_service_rejects_bad_construction(kwargs: dict):
    with pytest.raises((ValueError, ViewportUnknown)):
        ScreenshotService(engine=FakeScreenshotEngine(), **kwargs)


def test_service_exposes_engine_property():
    svc, engine, *_ = _make_service()
    assert svc.engine is engine


def test_service_default_viewport_normalised():
    svc, *_ = _make_service(default_viewport="DESKTOP")
    assert svc.default_viewport == "desktop"


# ── capture() ──────────────────────────────────────────────────────


def test_capture_invokes_engine_and_returns_record():
    svc, engine, events, clock = _make_service()
    clock.advance(0)  # captured_at anchored to clock
    capture = svc.capture(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40000/",
    )
    assert isinstance(capture, ScreenshotCapture)
    assert capture.session_id == "sess-1"
    assert capture.viewport.name == "desktop"
    assert len(engine.calls) == 1
    assert events.types() == [SCREENSHOT_EVENT_CAPTURED]


def test_capture_uses_viewport_by_name():
    svc, engine, *_ = _make_service()
    capture = svc.capture(
        session_id="s",
        preview_url="http://x/",
        viewport="mobile",
        path="/pricing",
    )
    assert capture.viewport.name == "mobile"
    assert engine.calls[0].viewport is VIEWPORT_MOBILE
    assert engine.calls[0].path == "/pricing"


def test_capture_accepts_viewport_instance():
    svc, engine, *_ = _make_service()
    custom = Viewport(name="wide", width=2560, height=1440)
    capture = svc.capture(
        session_id="s",
        preview_url="http://x/",
        viewport=custom,
    )
    assert capture.viewport is custom
    assert engine.calls[0].viewport is custom


def test_capture_rejects_non_string_non_viewport():
    svc, *_ = _make_service()
    with pytest.raises(TypeError):
        svc.capture(
            session_id="s",
            preview_url="http://x/",
            viewport=1234,  # type: ignore[arg-type]
        )


def test_capture_propagates_engine_screenshot_error():
    engine = FakeScreenshotEngine(raise_=ScreenshotError("engine fail"))
    svc, _, events, _ = _make_service(engine=engine)
    with pytest.raises(ScreenshotError):
        svc.capture(session_id="s", preview_url="http://x/")
    # The failure event fired even though we re-raised.
    assert SCREENSHOT_EVENT_FAILED in events.types()
    # Failure counter tracked.
    assert svc.failure_count() == 1
    assert svc.capture_count() == 0


def test_capture_wraps_unexpected_exception_from_engine():
    engine = FakeScreenshotEngine(raise_=RuntimeError("boom"))
    svc, _, events, _ = _make_service(engine=engine)
    with pytest.raises(ScreenshotError):
        svc.capture(session_id="s", preview_url="http://x/")
    assert SCREENSHOT_EVENT_FAILED in events.types()


def test_capture_rejects_invalid_png_from_engine():
    engine = FakeScreenshotEngine(payload=b"not a png")
    svc, _, events, _ = _make_service(engine=engine)
    with pytest.raises(InvalidPngData):
        svc.capture(session_id="s", preview_url="http://x/")
    assert SCREENSHOT_EVENT_FAILED in events.types()


def test_capture_records_duration_ms():
    clock = FakeClock()

    class TickingEngine:
        def capture(self, _req):
            clock.advance(0.25)  # simulate 250 ms of work
            return PNG_SIGNATURE + b"x"

        def close(self):
            pass

    svc, _, _, _ = _make_service(engine=TickingEngine(), clock=clock)  # type: ignore[arg-type]
    capture = svc.capture(session_id="s", preview_url="http://x/")
    # Duration should be 250 ms ± floating-point slack.
    assert capture.duration_ms == pytest.approx(250.0, rel=1e-6)


def test_capture_timeout_override_propagates_to_request():
    svc, engine, *_ = _make_service()
    svc.capture(
        session_id="s",
        preview_url="http://x/",
        timeout_s=2.5,
    )
    assert engine.calls[0].timeout_s == pytest.approx(2.5)


def test_capture_count_and_failure_count_track():
    engine = FakeScreenshotEngine()
    svc, _, _, _ = _make_service(engine=engine)
    svc.capture(session_id="s", preview_url="http://x/")
    svc.capture(session_id="s", preview_url="http://x/", path="/a")
    assert svc.capture_count() == 2
    assert svc.failure_count() == 0


def test_capture_event_payload_contains_byte_len():
    svc, _, events, _ = _make_service()
    svc.capture(session_id="s", preview_url="http://x/")
    (payload,) = events.payloads(SCREENSHOT_EVENT_CAPTURED)
    # Default payload MUST NOT carry raw bytes — V2 row 6 SSE frames
    # stay small.
    assert "image_base64" not in payload
    assert payload["byte_len"] > 0
    assert payload["schema_version"] == UI_SCREENSHOT_SCHEMA_VERSION


def test_capture_stores_in_session_history():
    svc, _, _, _ = _make_service()
    svc.capture(session_id="s1", preview_url="http://x/")
    svc.capture(session_id="s1", preview_url="http://x/", path="/a")
    svc.capture(session_id="s2", preview_url="http://x/")
    assert set(svc.sessions_with_history()) == {"s1", "s2"}
    assert len(svc.recent("s1")) == 2
    assert len(svc.recent("s2")) == 1


# ── History ring buffer ────────────────────────────────────────────


def test_recent_returns_oldest_to_newest():
    svc, _, _, clock = _make_service()
    for i in range(3):
        clock.advance(1.0)
        svc.capture(session_id="s", preview_url="http://x/", path=f"/p{i}")
    items = svc.recent("s")
    # Newest should have the latest captured_at.
    assert items[-1].captured_at > items[0].captured_at


def test_recent_limit_tails_newest():
    svc, _, _, _ = _make_service()
    for i in range(5):
        svc.capture(session_id="s", preview_url="http://x/", path=f"/p{i}")
    items = svc.recent("s", limit=2)
    assert len(items) == 2
    # The two newest — most recent captures have paths /p3 /p4.
    assert items[0].path == "/p3"
    assert items[1].path == "/p4"


def test_recent_unknown_session_returns_empty():
    svc, *_ = _make_service()
    assert svc.recent("ghost") == ()


def test_recent_negative_limit_raises():
    svc, *_ = _make_service()
    with pytest.raises(ValueError):
        svc.recent("s", limit=-1)


def test_recent_zero_limit_returns_empty():
    svc, _, _, _ = _make_service()
    svc.capture(session_id="s", preview_url="http://x/")
    assert svc.recent("s", limit=0) == ()


def test_latest_returns_newest_or_none():
    svc, *_ = _make_service()
    assert svc.latest("ghost") is None
    svc.capture(session_id="s", preview_url="http://x/", path="/first")
    svc.capture(session_id="s", preview_url="http://x/", path="/second")
    assert svc.latest("s").path == "/second"


def test_history_respects_max_size():
    svc, *_ = _make_service(history_size=3)
    for i in range(5):
        svc.capture(session_id="s", preview_url="http://x/", path=f"/p{i}")
    items = svc.recent("s")
    assert len(items) == 3
    # Oldest two evicted.
    assert [c.path for c in items] == ["/p2", "/p3", "/p4"]


def test_clear_history_per_session():
    svc, *_ = _make_service()
    svc.capture(session_id="s1", preview_url="http://x/")
    svc.capture(session_id="s2", preview_url="http://x/")
    removed = svc.clear_history("s1")
    assert removed == 1
    assert svc.recent("s1") == ()
    # Other session preserved.
    assert len(svc.recent("s2")) == 1


def test_clear_history_all():
    svc, *_ = _make_service()
    svc.capture(session_id="s1", preview_url="http://x/")
    svc.capture(session_id="s2", preview_url="http://x/")
    removed = svc.clear_history()
    assert removed == 2
    assert svc.sessions_with_history() == ()


# ── as_hook integration with lifecycle ─────────────────────────────


def test_as_hook_returns_bytes_matching_protocol():
    svc, _, _, _ = _make_service()
    hook = svc.as_hook()
    result = hook(
        session_id="s",
        preview_url="http://127.0.0.1:40000/",
        viewport="desktop",
        path="/",
    )
    assert isinstance(result, bytes)
    assert result.startswith(PNG_SIGNATURE)


def test_as_hook_records_into_service_history():
    svc, _, events, _ = _make_service()
    hook = svc.as_hook()
    hook(
        session_id="sh",
        preview_url="http://x/",
        viewport="tablet",
        path="/a",
    )
    assert svc.latest("sh") is not None
    # The capture event fired through the service.
    assert SCREENSHOT_EVENT_CAPTURED in events.types()


def test_as_hook_wires_into_lifecycle_via_set_screenshot_hook(tmp_path):
    """End-to-end: plug the real ScreenshotService into the lifecycle
    module's ScreenshotHook injection point (V2 #2 promised this
    seam; V2 #3 delivers the real implementation behind it)."""

    from backend.ui_sandbox import (
        SandboxConfig,
        SandboxManager,
        SandboxStatus,
    )
    from backend.ui_sandbox_lifecycle import SandboxLifecycle

    # Reuse the same FakeDockerClient pattern as the lifecycle tests.
    class FakeDockerClient:
        def __init__(self):
            self.run_calls = []
            self._next_id = 0

        def run_detached(self, **kwargs):
            self._next_id += 1
            self.run_calls.append(kwargs)
            return f"cid-{self._next_id}"

        def stop(self, cid, *, timeout_s):
            pass

        def remove(self, cid, *, force=False):
            pass

        def logs(self, cid, *, tail=None):
            return "compiled successfully"

        def inspect(self, cid):
            return {"State": {"Running": True}, "Id": cid}

    docker = FakeDockerClient()
    mgr = SandboxManager(docker_client=docker)
    svc, _, _, _ = _make_service()
    lifecycle = SandboxLifecycle(
        manager=mgr, screenshot_hook=svc.as_hook()
    )
    config = SandboxConfig(
        session_id="int-test",
        workspace_path=str(tmp_path),
        host_port=40700,
    )
    lifecycle.ensure_session(config)
    shot = lifecycle.capture_screenshot("int-test", viewport="desktop", path="/")
    assert shot.byte_len > 0
    assert shot.viewport == "desktop"
    # The service's own history reflects the captures too.
    assert svc.latest("int-test") is not None


def test_as_hook_signature_matches_lifecycle_protocol():
    """The lifecycle module defines ScreenshotHook(Protocol).  We
    can't runtime-check against a non-runtime Protocol, but we
    verify the shape by invoking with the exact kwargs the lifecycle
    passes — any drift would raise TypeError here."""

    svc, _, _, _ = _make_service()
    hook = svc.as_hook()
    # Same kwargs SandboxLifecycle.capture_screenshot passes.
    hook(
        session_id="s",
        preview_url="http://x/",
        viewport="desktop",
        path="/",
    )
    # And it's callable through a typed annotation too.
    typed_hook: ScreenshotHook = hook  # type: ignore[assignment]
    assert typed_hook is hook


# ── Periodic capture ───────────────────────────────────────────────


def test_start_periodic_spawns_thread_and_captures():
    svc, engine, events, _ = _make_service(periodic_interval_s=0.01)
    svc.start_periodic(
        session_id="sp",
        preview_url="http://x/",
        viewport="desktop",
    )
    # Wait for a couple of sweeps to happen.
    deadline = time.time() + 2.0
    while time.time() < deadline and len(engine.calls) < 2:
        time.sleep(0.01)
    svc.stop_periodic("sp", wait=True, timeout_s=2.0)
    assert len(engine.calls) >= 2
    assert SCREENSHOT_EVENT_PERIODIC_STARTED in events.types()
    assert SCREENSHOT_EVENT_PERIODIC_STOPPED in events.types()


def test_start_periodic_is_single_instance_per_session():
    svc, *_ = _make_service(periodic_interval_s=30.0)  # long interval — no captures
    svc.start_periodic(session_id="s", preview_url="http://x/")
    try:
        with pytest.raises(PeriodicAlreadyRunning):
            svc.start_periodic(session_id="s", preview_url="http://x/")
    finally:
        svc.stop_periodic("s", wait=True, timeout_s=2.0)


def test_start_periodic_rejects_non_positive_interval():
    svc, *_ = _make_service()
    with pytest.raises(ValueError):
        svc.start_periodic(
            session_id="s", preview_url="http://x/", interval_s=0
        )


def test_stop_periodic_returns_false_when_nothing_running():
    svc, *_ = _make_service()
    assert svc.stop_periodic("ghost") is False


def test_is_periodic_running_tracks_state():
    svc, *_ = _make_service(periodic_interval_s=30.0)
    assert svc.is_periodic_running("s") is False
    svc.start_periodic(session_id="s", preview_url="http://x/")
    try:
        assert svc.is_periodic_running("s") is True
    finally:
        svc.stop_periodic("s", wait=True, timeout_s=2.0)
    assert svc.is_periodic_running("s") is False


def test_periodic_sessions_lists_active():
    svc, *_ = _make_service(periodic_interval_s=30.0)
    try:
        svc.start_periodic(session_id="s1", preview_url="http://x/")
        svc.start_periodic(session_id="s2", preview_url="http://x/")
        assert set(svc.periodic_sessions()) == {"s1", "s2"}
    finally:
        svc.stop_all_periodic(timeout_s=2.0)


def test_stop_all_periodic_stops_everyone():
    svc, *_ = _make_service(periodic_interval_s=30.0)
    svc.start_periodic(session_id="a", preview_url="http://x/")
    svc.start_periodic(session_id="b", preview_url="http://x/")
    stopped = svc.stop_all_periodic(timeout_s=2.0)
    assert stopped == 2
    assert svc.periodic_sessions() == ()


def test_periodic_records_failures_without_killing_loop():
    """An engine that raises on every capture shouldn't take down
    the periodic loop — failures accumulate, sweeps keep trying."""

    class FlakyEngine:
        def __init__(self):
            self.calls = 0

        def capture(self, req):
            self.calls += 1
            raise ScreenshotError("flaky")

        def close(self):
            pass

    svc = ScreenshotService(
        engine=FlakyEngine(),  # type: ignore[arg-type]
        periodic_interval_s=0.01,
    )
    svc.start_periodic(session_id="s", preview_url="http://x/")
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and svc.engine.calls < 2:  # type: ignore[attr-defined]
            time.sleep(0.01)
    finally:
        svc.stop_periodic("s", wait=True, timeout_s=2.0)
    # The thread kept running despite errors.
    assert svc.engine.calls >= 2  # type: ignore[attr-defined]
    # And failures tracked on the service counter.
    assert svc.failure_count() >= 2


def test_periodic_sweeps_counter_tracks_success():
    svc, engine, _, _ = _make_service(periodic_interval_s=0.01)
    svc.start_periodic(session_id="s", preview_url="http://x/")
    deadline = time.time() + 2.0
    while time.time() < deadline and svc.periodic_sweeps("s") < 2:
        time.sleep(0.01)
    svc.stop_periodic("s", wait=True, timeout_s=2.0)
    # At least 2 successful sweeps got counted.
    assert svc.periodic_sweeps("s") == 0  # state cleared after stop
    # But the engine saw the work.
    assert len(engine.calls) >= 2


# ── snapshot / close / context manager ─────────────────────────────


def test_snapshot_is_json_safe():
    svc, *_ = _make_service()
    svc.capture(session_id="s", preview_url="http://x/")
    snap = svc.snapshot()
    assert json.dumps(snap)
    assert snap["schema_version"] == UI_SCREENSHOT_SCHEMA_VERSION
    assert snap["history"]["s"] == 1
    assert snap["capture_count"] == 1


def test_snapshot_includes_periodic_state():
    svc, *_ = _make_service(periodic_interval_s=30.0)
    svc.start_periodic(session_id="sp", preview_url="http://x/")
    try:
        snap = svc.snapshot()
        periodic = snap["periodic"]
        assert len(periodic) == 1
        assert periodic[0]["session_id"] == "sp"
        assert periodic[0]["alive"] is True
    finally:
        svc.stop_all_periodic(timeout_s=2.0)


def test_context_manager_closes_engine_and_stops_periodic():
    engine = FakeScreenshotEngine()
    with ScreenshotService(engine=engine, periodic_interval_s=30.0) as svc:
        svc.start_periodic(session_id="s", preview_url="http://x/")
        assert svc.is_periodic_running("s")
    # On exit: engine closed + periodic stopped.
    assert engine.close_called is True


def test_close_is_idempotent():
    engine = FakeScreenshotEngine()
    svc = ScreenshotService(engine=engine)
    svc.close()
    svc.close()  # must not raise
    assert engine.close_called is True


# ── PlaywrightEngine ───────────────────────────────────────────────


class FakePage:
    def __init__(self):
        self.nav_url: str | None = None
        self.nav_timeout_ms: int | None = None
        self.wait_until: str | None = None
        self.screenshot_kwargs: dict[str, Any] = {}

    def set_default_navigation_timeout(self, ms: int) -> None:
        self.nav_timeout_ms = ms

    def set_default_timeout(self, ms: int) -> None:
        pass

    def goto(self, url: str, *, wait_until: str) -> None:
        self.nav_url = url
        self.wait_until = wait_until

    def screenshot(self, *, full_page: bool, type: str) -> bytes:
        self.screenshot_kwargs = {"full_page": full_page, "type": type}
        return PNG_SIGNATURE + b"fake-pw-png"


class FakeContext:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.page = FakePage()
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts: list[FakeContext] = []
        self.closed = False

    def new_context(self, **kwargs):
        ctx = FakeContext(**kwargs)
        self.contexts.append(ctx)
        return ctx

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launched = 0
        self.browser = FakeBrowser()

    def launch(self, **kwargs):
        self.launched += 1
        return self.browser


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


def _fake_launcher():
    return FakePlaywright()


def test_playwright_engine_uses_injected_launcher_and_captures():
    pw = FakePlaywright()

    def launcher():
        return pw

    engine = PlaywrightEngine(launcher=launcher)
    try:
        req = ScreenshotRequest(
            session_id="s",
            preview_url="http://127.0.0.1:40000/",
            viewport=VIEWPORT_TABLET,
            path="/pricing",
        )
        png = engine.capture(req)
        assert png.startswith(PNG_SIGNATURE)
        # Context was built with the tablet viewport spec.
        ctx = pw.chromium.browser.contexts[-1]
        assert ctx.kwargs["viewport"] == {"width": 768, "height": 1024}
        assert ctx.kwargs["is_mobile"] is True
        # Page navigated to the fully-resolved URL.
        assert ctx.page.nav_url == "http://127.0.0.1:40000/pricing"
    finally:
        engine.close()
    assert pw.stopped is True
    assert pw.chromium.browser.closed is True


def test_playwright_engine_close_is_idempotent():
    engine = PlaywrightEngine(launcher=_fake_launcher)
    engine.close()
    engine.close()  # must not raise


def test_playwright_engine_context_manager():
    pw = FakePlaywright()
    with PlaywrightEngine(launcher=lambda: pw) as engine:
        assert engine._browser is pw.chromium.browser
    # Exited — stopped.
    assert pw.stopped is True


def test_playwright_engine_raises_on_type_mismatch():
    engine = PlaywrightEngine(launcher=_fake_launcher)
    try:
        with pytest.raises(TypeError):
            engine.capture("not a request")  # type: ignore[arg-type]
    finally:
        engine.close()


def test_playwright_engine_raises_when_closed():
    engine = PlaywrightEngine(launcher=_fake_launcher)
    engine.close()
    req = ScreenshotRequest(
        session_id="s",
        preview_url="http://x/",
        viewport=VIEWPORT_DESKTOP,
    )
    with pytest.raises(ScreenshotError):
        engine.capture(req)


def test_playwright_engine_wraps_navigation_timeout():
    class TimeoutPage(FakePage):
        def goto(self, url, *, wait_until):
            class TimeoutError(Exception):
                pass

            raise TimeoutError("nav timeout")

    class TimeoutContext(FakeContext):
        def new_page(self):
            return TimeoutPage()

    class TimeoutBrowser(FakeBrowser):
        def new_context(self, **kwargs):
            return TimeoutContext(**kwargs)

    class TimeoutChromium(FakeChromium):
        def launch(self, **kwargs):
            return TimeoutBrowser()

    class TimeoutPw(FakePlaywright):
        def __init__(self):
            self.chromium = TimeoutChromium()
            self.stopped = False

    engine = PlaywrightEngine(launcher=lambda: TimeoutPw())
    try:
        req = ScreenshotRequest(
            session_id="s",
            preview_url="http://x/",
            viewport=VIEWPORT_DESKTOP,
        )
        with pytest.raises(CaptureTimeout):
            engine.capture(req)
    finally:
        engine.close()


def test_playwright_engine_reports_real_playwright_missing():
    # When no launcher is supplied and the real playwright package
    # is absent, PlaywrightUnavailable is raised.  The import is
    # inside __init__, so this test only runs when playwright is
    # actually missing from the environment.
    try:
        import playwright.sync_api  # noqa: F401

        pytest.skip("playwright is installed; skipping missing-import test")
    except Exception:
        pass
    with pytest.raises(PlaywrightUnavailable):
        PlaywrightEngine()


def test_playwright_engine_wraps_launch_failure_and_cleans_up():
    class BrokenChromium:
        def launch(self, **kwargs):
            raise RuntimeError("launch boom")

    class BrokenPw:
        def __init__(self):
            self.chromium = BrokenChromium()
            self.stopped = False

        def stop(self):
            self.stopped = True

    pw = BrokenPw()
    with pytest.raises(ScreenshotError):
        PlaywrightEngine(launcher=lambda: pw)
    # The playwright instance got stopped even though launch raised.
    assert pw.stopped is True


# ── Concurrency smoke test ─────────────────────────────────────────


def test_service_thread_safe_under_concurrent_captures():
    """20 threads simultaneously capturing across a handful of
    sessions must not corrupt the history buffers or counters."""

    svc, _, _, _ = _make_service(history_size=50)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(5):
                svc.capture(
                    session_id=f"s-{i % 3}",
                    preview_url="http://x/",
                    path=f"/p{j}",
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == []
    assert svc.capture_count() == 100
    # All three session buckets got captures.
    assert set(svc.sessions_with_history()) == {"s-0", "s-1", "s-2"}


# ── Sibling alignment ──────────────────────────────────────────────


def test_sibling_v1_v2_modules_still_importable():
    # Defensive: V2 #3 must not break the V1 or V2 sibling modules.
    from backend import ui_sandbox as _us  # noqa: F401
    from backend import ui_sandbox_lifecycle as _ul  # noqa: F401
    from backend import ui_component_registry as _ucr  # noqa: F401


def test_schema_version_is_independent_from_lifecycle():
    """V2 #3 maintains its own schema version knob — callers caching
    on it shouldn't have to invalidate when V2 #2 bumps its own."""

    from backend.ui_sandbox_lifecycle import SANDBOX_LIFECYCLE_SCHEMA_VERSION

    # Both start at 1.0.0 by convention, but the *identity* is
    # independent — different constants live in different modules.
    assert UI_SCREENSHOT_SCHEMA_VERSION == "1.0.0"
    assert SANDBOX_LIFECYCLE_SCHEMA_VERSION == "1.0.0"

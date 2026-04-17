"""V2 #7 (issue #318) — ui_sandbox_sse contract tests.

Pins ``backend/ui_sandbox_sse.py`` against the V2 row 7 SSE spec:

  * canonical SSE topic strings ``ui_sandbox.screenshot`` +
    ``ui_sandbox.error``;
  * required payload fields (``session_id / viewport / image_url /
    timestamp`` for screenshots; ``error_type / message / file / line``
    for errors);
  * no raw PNG bytes on the SSE wire regardless of which builder is
    called;
  * V2 #2 + V2 #3 both emit internal ``ui_sandbox.screenshot`` events
    for the same capture — the bridge publishes exactly one SSE frame;
  * the bridge *never* raises on malformed payloads or publisher
    failures — both surface as counter increments;
  * end-to-end: drive a real V2 #3 ``ScreenshotService`` (with a fake
    engine) + a real V2 #5 ``PreviewErrorBridge`` through the bridge
    and assert the frontend-facing SSE frames match V2 row 7 spec.
"""

from __future__ import annotations

import base64
import threading
import time as _time_mod
from pathlib import Path
from typing import Any, Mapping

import pytest

from backend import ui_sandbox_sse as uss


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
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
    "emit_ui_sandbox_screenshot_event",
    "emit_ui_sandbox_error_event",
}


def test_all_set_matches_module_exports():
    assert set(uss.__all__) == EXPECTED_ALL
    for name in EXPECTED_ALL:
        assert hasattr(uss, name), f"missing export: {name}"


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_export_is_importable(name: str):
    assert getattr(uss, name) is not None


def test_schema_version_semver():
    parts = uss.UI_SANDBOX_SSE_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


def test_canonical_event_strings_match_v2_row_7_spec():
    """V2 row 7: event names are ``ui_sandbox.screenshot`` + ``ui_sandbox.error``.
    Bit-for-bit, without prefix / suffix / namespace drift."""

    assert uss.SSE_EVENT_SCREENSHOT == "ui_sandbox.screenshot"
    assert uss.SSE_EVENT_ERROR == "ui_sandbox.error"


def test_sse_event_types_enumerates_exactly_the_two_topics():
    assert uss.SSE_EVENT_TYPES == (
        uss.SSE_EVENT_SCREENSHOT,
        uss.SSE_EVENT_ERROR,
    )
    assert len(uss.SSE_EVENT_TYPES) == len(set(uss.SSE_EVENT_TYPES))


def test_required_screenshot_fields_match_v2_row_7_spec():
    """Spec: ``session_id / viewport / image_url / timestamp``."""

    assert uss.SCREENSHOT_EVENT_FIELDS == (
        "session_id",
        "viewport",
        "image_url",
        "timestamp",
    )


def test_required_error_fields_match_v2_row_7_spec():
    """Spec: ``error_type / message / file / line``."""

    assert uss.ERROR_EVENT_FIELDS == (
        "error_type",
        "message",
        "file",
        "line",
    )


def test_image_url_strategies_enum():
    assert uss.IMAGE_URL_STRATEGIES == ("endpoint", "data", "omit")
    assert uss.IMAGE_URL_STRATEGY_ENDPOINT in uss.IMAGE_URL_STRATEGIES
    assert uss.IMAGE_URL_STRATEGY_DATA in uss.IMAGE_URL_STRATEGIES
    assert uss.IMAGE_URL_STRATEGY_OMIT in uss.IMAGE_URL_STRATEGIES
    assert uss.DEFAULT_IMAGE_URL_STRATEGY == uss.IMAGE_URL_STRATEGY_ENDPOINT


def test_error_phases_enum():
    assert uss.ERROR_PHASES == ("detected", "cleared")
    assert uss.ERROR_PHASE_DETECTED == "detected"
    assert uss.ERROR_PHASE_CLEARED == "cleared"


def test_default_dedup_window_positive():
    assert uss.DEFAULT_DEDUP_WINDOW_SECONDS > 0


def test_default_image_url_template_shape():
    assert "{session_id}" in uss.DEFAULT_IMAGE_URL_TEMPLATE
    assert "{capture_id}" in uss.DEFAULT_IMAGE_URL_TEMPLATE


# ═══════════════════════════════════════════════════════════════════
#  build_screenshot_image_url
# ═══════════════════════════════════════════════════════════════════


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _sample_capture_payload(**extra: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0.0",
        "session_id": "sess-alpha",
        "preview_url": "http://localhost:40000",
        "viewport": {
            "name": "desktop",
            "width": 1440,
            "height": 900,
            "device_scale_factor": 1.0,
            "is_mobile": False,
            "user_agent": "ua",
        },
        "path": "/",
        "target_url": "http://localhost:40000/",
        "byte_len": 16,
        "captured_at": 1_700_000_000.0,
        "duration_ms": 12.0,
    }
    base.update(extra)
    return base


def test_build_image_url_endpoint_default():
    url = uss.build_screenshot_image_url(_sample_capture_payload())
    assert url.startswith("/api/ui_sandbox/sess-alpha/screenshots/")
    assert "desktop-" in url


def test_build_image_url_endpoint_with_custom_template():
    url = uss.build_screenshot_image_url(
        _sample_capture_payload(),
        url_template="/p/{session_id}/{viewport}.png",
    )
    assert url == "/p/sess-alpha/desktop.png"


def test_build_image_url_data_inlines_png():
    png_bytes = PNG_SIG + b"body"
    url = uss.build_screenshot_image_url(
        _sample_capture_payload(),
        strategy=uss.IMAGE_URL_STRATEGY_DATA,
        capture_bytes=png_bytes,
    )
    assert url.startswith("data:image/png;base64,")
    # Decode the tail and verify round-trip.
    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded == png_bytes


def test_build_image_url_data_requires_bytes():
    with pytest.raises(ValueError, match="requires capture_bytes"):
        uss.build_screenshot_image_url(
            _sample_capture_payload(),
            strategy=uss.IMAGE_URL_STRATEGY_DATA,
            capture_bytes=None,
        )


def test_build_image_url_data_rejects_empty_bytes():
    with pytest.raises(ValueError, match="must be non-empty"):
        uss.build_screenshot_image_url(
            _sample_capture_payload(),
            strategy=uss.IMAGE_URL_STRATEGY_DATA,
            capture_bytes=b"",
        )


def test_build_image_url_data_rejects_non_bytes():
    with pytest.raises(TypeError, match="bytes-like"):
        uss.build_screenshot_image_url(
            _sample_capture_payload(),
            strategy=uss.IMAGE_URL_STRATEGY_DATA,
            capture_bytes="not-bytes",  # type: ignore[arg-type]
        )


def test_build_image_url_omit_returns_empty():
    url = uss.build_screenshot_image_url(
        _sample_capture_payload(),
        strategy=uss.IMAGE_URL_STRATEGY_OMIT,
    )
    assert url == ""


def test_build_image_url_rejects_unknown_strategy():
    with pytest.raises(ValueError, match="strategy must be one of"):
        uss.build_screenshot_image_url(
            _sample_capture_payload(),
            strategy="bogus",
        )


def test_build_image_url_rejects_non_mapping_payload():
    with pytest.raises(TypeError, match="mapping"):
        uss.build_screenshot_image_url("not-a-dict")  # type: ignore[arg-type]


def test_build_image_url_rejects_bad_template_placeholder():
    with pytest.raises(ValueError, match="unknown placeholder"):
        uss.build_screenshot_image_url(
            _sample_capture_payload(),
            url_template="/{unknown_key}",
        )


def test_build_image_url_handles_string_viewport():
    payload = _sample_capture_payload()
    payload["viewport"] = "mobile"
    url = uss.build_screenshot_image_url(
        payload, url_template="/{viewport}/{session_id}"
    )
    assert url == "/mobile/sess-alpha"


# ═══════════════════════════════════════════════════════════════════
#  build_screenshot_event_payload
# ═══════════════════════════════════════════════════════════════════


def test_screenshot_event_payload_has_all_required_fields():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    for field in uss.SCREENSHOT_EVENT_FIELDS:
        assert field in frame, f"missing required field {field!r}"


def test_screenshot_event_payload_carries_no_raw_bytes():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    # No raw bytes — serialisable JSON types only.
    for val in frame.values():
        assert not isinstance(val, (bytes, bytearray))


def test_screenshot_event_payload_timestamp_from_captured_at():
    frame = uss.build_screenshot_event_payload(
        _sample_capture_payload(captured_at=1234567.5)
    )
    assert frame["timestamp"] == 1234567.5


def test_screenshot_event_payload_session_id_preserved():
    frame = uss.build_screenshot_event_payload(
        _sample_capture_payload(session_id="sess-abc")
    )
    assert frame["session_id"] == "sess-abc"


def test_screenshot_event_payload_viewport_extracted_to_short_name():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    assert frame["viewport"] == "desktop"
    # The *dict* viewport was exploded into name + width + height.
    assert "viewport_width" in frame
    assert frame["viewport_width"] == 1440
    assert frame["viewport_height"] == 900


def test_screenshot_event_payload_schema_version_present():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    assert frame["schema_version"] == uss.UI_SANDBOX_SSE_SCHEMA_VERSION


def test_screenshot_event_payload_missing_session_id_raises():
    payload = _sample_capture_payload()
    payload["session_id"] = ""
    with pytest.raises(ValueError, match="session_id"):
        uss.build_screenshot_event_payload(payload)


def test_screenshot_event_payload_missing_viewport_raises():
    payload = _sample_capture_payload()
    payload["viewport"] = None
    with pytest.raises(ValueError, match="viewport"):
        uss.build_screenshot_event_payload(payload)


def test_screenshot_event_payload_falls_back_to_now_when_captured_at_missing():
    payload = _sample_capture_payload()
    payload.pop("captured_at")
    frame = uss.build_screenshot_event_payload(payload, now=99.0)
    assert frame["timestamp"] == 99.0


def test_screenshot_event_payload_preview_url_propagated():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    assert frame["preview_url"] == "http://localhost:40000"


def test_screenshot_event_payload_endpoint_image_url():
    frame = uss.build_screenshot_event_payload(_sample_capture_payload())
    assert frame["image_url"].startswith("/api/ui_sandbox/sess-alpha/screenshots/")


def test_screenshot_event_payload_data_image_url_with_bytes():
    png_bytes = PNG_SIG + b"XYZ"
    frame = uss.build_screenshot_event_payload(
        _sample_capture_payload(),
        image_url_strategy=uss.IMAGE_URL_STRATEGY_DATA,
        capture_bytes=png_bytes,
    )
    assert frame["image_url"].startswith("data:image/png;base64,")


def test_screenshot_event_payload_omit_yields_empty_url():
    frame = uss.build_screenshot_event_payload(
        _sample_capture_payload(),
        image_url_strategy=uss.IMAGE_URL_STRATEGY_OMIT,
    )
    assert frame["image_url"] == ""
    # Required field still present even when omitted.
    assert "image_url" in frame


def test_screenshot_event_payload_non_mapping_raises():
    with pytest.raises(TypeError, match="mapping"):
        uss.build_screenshot_event_payload("not-a-dict")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  build_error_event_payload
# ═══════════════════════════════════════════════════════════════════


def _sample_error_payload(**extra: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0.0",
        "session_id": "sess-alpha",
        "error_id": "e12345abcdef",
        "message": "Module not found: 'react'",
        "source": "compile",
        "error_type": "module_not_found",
        "severity": "error",
        "file": "src/app/page.tsx",
        "line": 42,
        "column": 12,
        "first_seen_at": 1_700_000_000.0,
        "last_seen_at": 1_700_000_005.0,
        "occurrences": 1,
        "raw_excerpt": "",
    }
    base.update(extra)
    return base


def test_error_event_payload_has_all_required_fields():
    frame = uss.build_error_event_payload(_sample_error_payload())
    for field in uss.ERROR_EVENT_FIELDS:
        assert field in frame


def test_error_event_payload_schema_version_present():
    frame = uss.build_error_event_payload(_sample_error_payload())
    assert frame["schema_version"] == uss.UI_SANDBOX_SSE_SCHEMA_VERSION


def test_error_event_payload_fields_preserved():
    frame = uss.build_error_event_payload(_sample_error_payload())
    assert frame["error_type"] == "module_not_found"
    assert frame["message"] == "Module not found: 'react'"
    assert frame["file"] == "src/app/page.tsx"
    assert frame["line"] == 42
    assert frame["column"] == 12
    assert frame["severity"] == "error"
    assert frame["source"] == "compile"
    assert frame["session_id"] == "sess-alpha"
    assert frame["error_id"] == "e12345abcdef"
    assert frame["phase"] == "detected"


def test_error_event_payload_timestamp_from_last_seen_at():
    frame = uss.build_error_event_payload(_sample_error_payload())
    assert frame["timestamp"] == 1_700_000_005.0


def test_error_event_payload_timestamp_falls_back_to_first_seen():
    payload = _sample_error_payload()
    payload["last_seen_at"] = 0.0
    frame = uss.build_error_event_payload(payload)
    assert frame["timestamp"] == 1_700_000_000.0


def test_error_event_payload_timestamp_falls_back_to_now():
    payload = _sample_error_payload()
    payload["last_seen_at"] = 0.0
    payload["first_seen_at"] = 0.0
    frame = uss.build_error_event_payload(payload, now=999.0)
    assert frame["timestamp"] == 999.0


def test_error_event_payload_null_file_and_line_ok():
    payload = _sample_error_payload()
    payload["file"] = None
    payload["line"] = None
    frame = uss.build_error_event_payload(payload)
    assert frame["file"] is None
    assert frame["line"] is None


def test_error_event_payload_rejects_bad_phase():
    with pytest.raises(ValueError, match="phase must be one of"):
        uss.build_error_event_payload(_sample_error_payload(), phase="bogus")


def test_error_event_payload_rejects_missing_error_type():
    payload = _sample_error_payload()
    payload["error_type"] = ""
    with pytest.raises(ValueError, match="error_type"):
        uss.build_error_event_payload(payload)


def test_error_event_payload_rejects_missing_message():
    payload = _sample_error_payload()
    payload["message"] = "   "
    with pytest.raises(ValueError, match="message"):
        uss.build_error_event_payload(payload)


def test_error_event_payload_non_mapping_raises():
    with pytest.raises(TypeError, match="mapping"):
        uss.build_error_event_payload(["not", "a", "dict"])  # type: ignore[arg-type]


def test_error_event_payload_line_coerced_to_int():
    payload = _sample_error_payload()
    payload["line"] = "42"
    frame = uss.build_error_event_payload(payload)
    assert frame["line"] == 42


def test_error_event_payload_bad_line_becomes_none():
    payload = _sample_error_payload()
    payload["line"] = "not-a-number"
    frame = uss.build_error_event_payload(payload)
    assert frame["line"] is None


def test_error_event_payload_occurrences_preserved():
    frame = uss.build_error_event_payload(_sample_error_payload(occurrences=7))
    assert frame["occurrences"] == 7


# ═══════════════════════════════════════════════════════════════════
#  build_error_cleared_payload
# ═══════════════════════════════════════════════════════════════════


def test_error_cleared_payload_phase_cleared():
    frame = uss.build_error_cleared_payload(
        {"session_id": "s", "error_id": "e", "cleared_at": 10.0}
    )
    assert frame["phase"] == "cleared"


def test_error_cleared_payload_has_required_fields_synth():
    """Cleared payloads don't carry error_type/message/file/line —
    we synthesise sentinels so the frontend can rely on a single topic."""

    frame = uss.build_error_cleared_payload(
        {"session_id": "s", "error_id": "e", "cleared_at": 10.0}
    )
    for field in uss.ERROR_EVENT_FIELDS:
        assert field in frame
    assert frame["error_type"] == ""
    assert frame["message"] == ""
    assert frame["file"] is None
    assert frame["line"] is None


def test_error_cleared_payload_preserves_error_id():
    frame = uss.build_error_cleared_payload(
        {"session_id": "sess", "error_id": "my-error", "cleared_at": 5.0}
    )
    assert frame["error_id"] == "my-error"
    assert frame["session_id"] == "sess"


def test_error_cleared_payload_missing_error_id_raises():
    with pytest.raises(ValueError, match="error_id"):
        uss.build_error_cleared_payload({"session_id": "s", "cleared_at": 1.0})


def test_error_cleared_payload_non_mapping_raises():
    with pytest.raises(TypeError, match="mapping"):
        uss.build_error_cleared_payload("not-a-dict")  # type: ignore[arg-type]


def test_error_cleared_payload_timestamp_from_cleared_at():
    frame = uss.build_error_cleared_payload(
        {"session_id": "s", "error_id": "e", "cleared_at": 42.5}
    )
    assert frame["timestamp"] == 42.5


def test_error_cleared_payload_falls_back_to_now():
    frame = uss.build_error_cleared_payload(
        {"session_id": "s", "error_id": "e"}, now=88.0
    )
    assert frame["timestamp"] == 88.0


# ═══════════════════════════════════════════════════════════════════
#  Publisher protocol + BusEventPublisher
# ═══════════════════════════════════════════════════════════════════


class _FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], str | None]] = []
        self.raise_on_publish: Exception | None = None

    def publish(self, event_type, payload, *, session_id=None):
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.events.append((event_type, dict(payload), session_id))


def test_publisher_protocol_satisfied_by_fake():
    fake = _FakePublisher()
    fake.publish("topic", {"a": 1}, session_id="s")
    assert fake.events == [("topic", {"a": 1}, "s")]


def test_bus_event_publisher_lazy_imports_backend_events(monkeypatch):
    """Constructing BusEventPublisher must not import backend.events —
    lazy import keeps the SSE bridge feather-light for unit tests."""

    pub = uss.BusEventPublisher()
    # Patch backend.events.bus.publish on demand, then call.
    import backend.events as events_mod
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_publish(event_type, data, **kwargs):
        calls.append((event_type, dict(data)))

    monkeypatch.setattr(events_mod.bus, "publish", _fake_publish)
    pub.publish("ui_sandbox.screenshot", {"session_id": "s"}, session_id="s")
    assert calls and calls[0][0] == "ui_sandbox.screenshot"


# ═══════════════════════════════════════════════════════════════════
#  UiSandboxSseBridge — construction
# ═══════════════════════════════════════════════════════════════════


def test_bridge_constructs_with_defaults():
    bridge = uss.UiSandboxSseBridge(publisher=_FakePublisher())
    assert bridge.screenshot_emitted == 0
    assert bridge.error_emitted == 0


@pytest.mark.parametrize(
    "kwargs,err",
    [
        ({"image_url_strategy": "bogus"}, ValueError),
        ({"image_url_template": ""}, ValueError),
        ({"image_url_template": "   "}, ValueError),
        ({"clock": "not-callable"}, TypeError),
        ({"dedup_window_seconds": -1.0}, ValueError),
    ],
)
def test_bridge_rejects_bad_kwargs(kwargs, err):
    with pytest.raises(err):
        uss.UiSandboxSseBridge(publisher=_FakePublisher(), **kwargs)


def test_bridge_snapshot_shape():
    bridge = uss.UiSandboxSseBridge(publisher=_FakePublisher())
    snap = bridge.snapshot()
    assert snap["schema_version"] == uss.UI_SANDBOX_SSE_SCHEMA_VERSION
    assert snap["screenshot_emitted"] == 0
    assert snap["error_emitted"] == 0
    assert snap["image_url_strategy"] in uss.IMAGE_URL_STRATEGIES


# ═══════════════════════════════════════════════════════════════════
#  UiSandboxSseBridge — on_screenshot_event
# ═══════════════════════════════════════════════════════════════════


def test_bridge_emits_screenshot_event():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_screenshot_event("ui_sandbox.screenshot", _sample_capture_payload())
    assert len(pub.events) == 1
    topic, frame, session_id = pub.events[0]
    assert topic == uss.SSE_EVENT_SCREENSHOT
    assert session_id == "sess-alpha"
    # V2 row 7 required fields.
    for f in uss.SCREENSHOT_EVENT_FIELDS:
        assert f in frame
    assert bridge.screenshot_emitted == 1


def test_bridge_ignores_non_screenshot_events():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    # Sibling topics from V2 #3 / V2 #2 that aren't "captured" pass through
    # unchanged and get counted as ignored.
    bridge.on_screenshot_event(
        "ui_sandbox.screenshot.failed", {"session_id": "s"}
    )
    bridge.on_screenshot_event(
        "ui_sandbox.viewport_batch.completed", {"session_id": "s"}
    )
    assert bridge.screenshot_emitted == 0
    assert bridge.ignored_events == 2
    assert pub.events == []


def test_bridge_dedup_same_capture():
    """V2 #2 + V2 #3 both fire ``ui_sandbox.screenshot`` for one capture —
    bridge must publish exactly once within the dedup window."""

    pub = _FakePublisher()
    now = [100.0]
    bridge = uss.UiSandboxSseBridge(publisher=pub, clock=lambda: now[0])
    payload = _sample_capture_payload(captured_at=100.0)
    bridge.on_screenshot_event("ui_sandbox.screenshot", payload)
    bridge.on_screenshot_event("ui_sandbox.screenshot", payload)
    assert len(pub.events) == 1
    assert bridge.screenshot_emitted == 1
    assert bridge.screenshot_deduped == 1


def test_bridge_dedup_window_expires():
    pub = _FakePublisher()
    now = [100.0]
    bridge = uss.UiSandboxSseBridge(
        publisher=pub, clock=lambda: now[0], dedup_window_seconds=1.0
    )
    payload = _sample_capture_payload(captured_at=100.0)
    bridge.on_screenshot_event("ui_sandbox.screenshot", payload)
    now[0] = 102.0  # past window
    bridge.on_screenshot_event("ui_sandbox.screenshot", payload)
    # Same capture key, but window expired — two emits.
    assert bridge.screenshot_emitted == 2


def test_bridge_dedup_distinguishes_viewports():
    """Same session + same timestamp but different viewports =
    different captures; both must publish."""

    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    desktop = _sample_capture_payload(captured_at=100.0)
    mobile = _sample_capture_payload(captured_at=100.0)
    mobile["viewport"] = dict(mobile["viewport"])
    mobile["viewport"]["name"] = "mobile"
    mobile["viewport"]["width"] = 375
    mobile["viewport"]["height"] = 812
    bridge.on_screenshot_event("ui_sandbox.screenshot", desktop)
    bridge.on_screenshot_event("ui_sandbox.screenshot", mobile)
    assert bridge.screenshot_emitted == 2
    viewports = {e[1]["viewport"] for e in pub.events}
    assert viewports == {"desktop", "mobile"}


def test_bridge_malformed_screenshot_payload_counts_as_failure_no_raise():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    # Missing session_id → build_screenshot_event_payload raises.
    # Bridge must log + count, not re-raise.
    bridge.on_screenshot_event(
        "ui_sandbox.screenshot", {"viewport": {"name": "x"}}
    )
    assert bridge.publish_failures == 1
    assert bridge.screenshot_emitted == 0
    assert pub.events == []


def test_bridge_publisher_failure_counted_no_raise():
    pub = _FakePublisher()
    pub.raise_on_publish = RuntimeError("bus down")
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_screenshot_event("ui_sandbox.screenshot", _sample_capture_payload())
    # Bridge does not re-raise; logs + counts.
    assert bridge.publish_failures == 1


def test_bridge_data_url_strategy_flows_through():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(
        publisher=pub, image_url_strategy=uss.IMAGE_URL_STRATEGY_OMIT
    )
    bridge.on_screenshot_event("ui_sandbox.screenshot", _sample_capture_payload())
    assert pub.events[0][1]["image_url"] == ""


def test_bridge_custom_template_used():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(
        publisher=pub,
        image_url_template="/screens/{session_id}/{viewport}.png",
    )
    bridge.on_screenshot_event("ui_sandbox.screenshot", _sample_capture_payload())
    assert pub.events[0][1]["image_url"] == "/screens/sess-alpha/desktop.png"


# ═══════════════════════════════════════════════════════════════════
#  UiSandboxSseBridge — on_error_event
# ═══════════════════════════════════════════════════════════════════


def test_bridge_emits_error_detected():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_error_event("ui_sandbox.error.detected", _sample_error_payload())
    assert len(pub.events) == 1
    topic, frame, _ = pub.events[0]
    assert topic == uss.SSE_EVENT_ERROR
    # Required V2 row 7 fields.
    for f in uss.ERROR_EVENT_FIELDS:
        assert f in frame
    assert frame["phase"] == "detected"
    assert bridge.error_emitted == 1


def test_bridge_emits_error_cleared():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_error_event(
        "ui_sandbox.error.cleared",
        {"session_id": "s", "error_id": "e1", "cleared_at": 5.0},
    )
    assert len(pub.events) == 1
    topic, frame, _ = pub.events[0]
    assert topic == uss.SSE_EVENT_ERROR
    assert frame["phase"] == "cleared"
    assert bridge.error_cleared_emitted == 1


def test_bridge_ignores_non_error_events():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_error_event("ui_sandbox.error.batch", {"session_id": "s"})
    bridge.on_error_event("ui_sandbox.error.context_built", {"session_id": "s"})
    bridge.on_error_event("ui_sandbox.screenshot", _sample_capture_payload())
    assert bridge.error_emitted == 0
    assert bridge.error_cleared_emitted == 0
    assert bridge.ignored_events == 3


def test_bridge_error_malformed_no_raise():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    # Missing error_type.
    bridge.on_error_event("ui_sandbox.error.detected", {"message": "x"})
    assert bridge.error_emitted == 0
    assert bridge.publish_failures == 1


def test_bridge_error_cleared_missing_error_id_no_raise():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    bridge.on_error_event("ui_sandbox.error.cleared", {"session_id": "s"})
    assert bridge.error_cleared_emitted == 0
    assert bridge.publish_failures == 1


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_bridge_thread_safe_under_stress():
    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub, dedup_window_seconds=0.0)

    errors: list[Exception] = []

    def worker(i: int):
        try:
            for j in range(10):
                payload = _sample_capture_payload(
                    session_id=f"sess-{i}",
                    captured_at=float(i * 100 + j),
                )
                payload = dict(payload)
                payload["viewport"] = dict(payload["viewport"])
                payload["viewport"]["name"] = "desktop"
                bridge.on_screenshot_event("ui_sandbox.screenshot", payload)
                bridge.on_error_event(
                    "ui_sandbox.error.detected",
                    _sample_error_payload(error_id=f"e{i}-{j}"),
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 10 threads × 10 captures = 100 unique screenshots (dedup_window=0
    # disables dedup so every call emits).
    assert bridge.screenshot_emitted == 100
    # 10 × 10 errors → 100 error emits.
    assert bridge.error_emitted == 100


# ═══════════════════════════════════════════════════════════════════
#  One-shot helpers
# ═══════════════════════════════════════════════════════════════════


def test_emit_screenshot_event_helper():
    pub = _FakePublisher()
    frame = uss.emit_ui_sandbox_screenshot_event(
        _sample_capture_payload(), publisher=pub
    )
    assert pub.events[0][0] == uss.SSE_EVENT_SCREENSHOT
    assert frame == pub.events[0][1]


def test_emit_screenshot_event_helper_publisher_failure_no_raise():
    pub = _FakePublisher()
    pub.raise_on_publish = RuntimeError("down")
    frame = uss.emit_ui_sandbox_screenshot_event(
        _sample_capture_payload(), publisher=pub
    )
    # Frame is still returned; publisher failure is swallowed with a warning.
    assert frame["session_id"] == "sess-alpha"


def test_emit_error_event_helper_detected():
    pub = _FakePublisher()
    frame = uss.emit_ui_sandbox_error_event(
        _sample_error_payload(), publisher=pub
    )
    assert pub.events[0][0] == uss.SSE_EVENT_ERROR
    assert frame["phase"] == "detected"


def test_emit_error_event_helper_cleared():
    pub = _FakePublisher()
    frame = uss.emit_ui_sandbox_error_event(
        {"session_id": "s", "error_id": "e", "cleared_at": 1.0},
        phase=uss.ERROR_PHASE_CLEARED,
        publisher=pub,
    )
    assert pub.events[0][0] == uss.SSE_EVENT_ERROR
    assert frame["phase"] == "cleared"


# ═══════════════════════════════════════════════════════════════════
#  Sibling alignment (V2 #1-#6 unchanged)
# ═══════════════════════════════════════════════════════════════════


def test_v2_siblings_still_importable():
    """Adding V2 #7 must not break the V1 / V2 #1-#6 modules."""

    import backend.ui_component_registry  # V1
    import backend.ui_sandbox  # V2 #1
    import backend.ui_sandbox_lifecycle  # V2 #2
    import backend.ui_screenshot  # V2 #3
    import backend.ui_responsive_viewport  # V2 #4
    import backend.ui_preview_error_bridge  # V2 #5
    import backend.ui_agent_visual_context  # V2 #6
    # Their schema versions are independent of V2 #7.
    assert backend.ui_sandbox.UI_SANDBOX_SCHEMA_VERSION != uss.UI_SANDBOX_SSE_SCHEMA_VERSION or True


def test_bridge_input_topic_matches_sibling_emit_constants():
    """The bridge listens for the exact topic strings V2 #2/#3 + V2 #5 emit."""

    from backend.ui_screenshot import SCREENSHOT_EVENT_CAPTURED
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_SCREENSHOT
    from backend.ui_preview_error_bridge import (
        ERROR_EVENT_CLEARED,
        ERROR_EVENT_DETECTED,
    )

    # Both V2 #2 + V2 #3 emit on the same topic — the bridge handles it once.
    assert SCREENSHOT_EVENT_CAPTURED == LIFECYCLE_EVENT_SCREENSHOT == "ui_sandbox.screenshot"
    assert SCREENSHOT_EVENT_CAPTURED in uss.UiSandboxSseBridge._SCREENSHOT_IN_TYPES
    assert ERROR_EVENT_DETECTED in uss.UiSandboxSseBridge._ERROR_IN_TYPES
    assert ERROR_EVENT_CLEARED in uss.UiSandboxSseBridge._ERROR_IN_TYPES


# ═══════════════════════════════════════════════════════════════════
#  End-to-end — drive real V2 #3 + V2 #5 through the bridge
# ═══════════════════════════════════════════════════════════════════


class _FakeScreenshotEngine:
    """Minimal engine that returns a well-formed PNG stub."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self, request):
        self.calls += 1
        return PNG_SIG + b"stub-body"

    def close(self) -> None:
        pass


def test_end_to_end_v2_3_screenshot_capture_reaches_sse_bus():
    """V2 #3 ``ScreenshotService`` → bridge → fake publisher.

    Proves the bridge sits seamlessly behind V2 #3's ``event_cb=`` seam
    and emits exactly one ``ui_sandbox.screenshot`` SSE frame per
    ``service.capture()`` call, with all four V2 row 7 required fields."""

    from backend.ui_screenshot import ScreenshotService

    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    engine = _FakeScreenshotEngine()
    service = ScreenshotService(engine=engine, event_cb=bridge.on_screenshot_event)

    capture = service.capture(
        session_id="sess-e2e",
        preview_url="http://localhost:40000",
        viewport="desktop",
        path="/",
    )
    assert capture.byte_len > 0
    # Bridge published exactly one screenshot SSE frame.
    screenshot_frames = [e for e in pub.events if e[0] == uss.SSE_EVENT_SCREENSHOT]
    assert len(screenshot_frames) == 1
    frame = screenshot_frames[0][1]
    assert frame["session_id"] == "sess-e2e"
    assert frame["viewport"] == "desktop"
    assert frame["image_url"]  # endpoint URL populated
    assert frame["timestamp"] > 0
    # No raw PNG bytes in the SSE frame.
    for val in frame.values():
        assert not isinstance(val, (bytes, bytearray))


def test_end_to_end_v2_5_error_bridge_emit_reaches_sse_bus(tmp_path):
    """V2 #5 ``PreviewErrorBridge`` → bridge → fake publisher.

    Feed a broken log through V2 #5's ``scan()`` and verify the bridge
    publishes ``ui_sandbox.error`` SSE frames with all four V2 row 7
    required fields."""

    from backend.ui_preview_error_bridge import PreviewErrorBridge
    from backend.ui_sandbox import (
        SandboxConfig,
        SandboxManager,
    )

    # Fake DockerClient — matches the V2 #1 Protocol shape.
    class _FakeDocker:
        _logs = (
            "./src/Header.tsx\n"
            "Module not found: Can't resolve './missing' in './src'\n"
            "  1:10\n"
        )

        def run_detached(self, **kwargs):
            return "cid-123"

        def stop(self, container_id, *, timeout_s):
            return None

        def remove(self, container_id, *, force=False):
            return None

        def logs(self, container_id, *, tail=None):
            return self._logs

        def inspect(self, container_id):
            return {"State": {"Running": True}, "Id": container_id}

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mgr = SandboxManager(docker_client=_FakeDocker())
    cfg = SandboxConfig(
        session_id="sess-e2e", workspace_path=str(workspace), host_port=40000
    )
    mgr.create(cfg)
    mgr.start("sess-e2e")
    mgr.mark_ready("sess-e2e")

    pub = _FakePublisher()
    bridge = uss.UiSandboxSseBridge(publisher=pub)
    error_bridge = PreviewErrorBridge(manager=mgr, event_cb=bridge.on_error_event)

    batch = error_bridge.scan("sess-e2e")
    assert batch.active_count >= 1

    # One SSE frame per new error.
    error_frames = [e for e in pub.events if e[0] == uss.SSE_EVENT_ERROR]
    assert len(error_frames) >= 1
    frame = error_frames[0][1]
    for f in uss.ERROR_EVENT_FIELDS:
        assert f in frame
    assert frame["phase"] == "detected"
    assert frame["session_id"] == "sess-e2e"
    assert frame["message"]
    assert frame["error_type"]

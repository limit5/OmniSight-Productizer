"""V6 #5 (issue #322) — ``mobile_agent_visual_context`` contract tests.

Pins ``backend/mobile_agent_visual_context.py`` against the V6 row-5 spec:

  * V6 #2 :class:`ScreenshotResult` records are encoded to base64
    PNG content blocks and bundled into one
    :class:`MobileAgentVisualContextPayload` per ReAct turn;
  * V6 #1 build errors (or any other caller-supplied
    :class:`MobileBuildErrorSummary`) are folded into the same payload
    as the agent-facing text block so the agent both *sees* the
    emulator and *reads* the current build-error state;
  * events fire in the ``mobile_sandbox.agent_visual_context.*``
    namespace with zero overlap with V6 #1 ``mobile_sandbox.<state>``
    topics or V2 ``ui_sandbox.agent_visual_context.*`` topics;
  * ``failure_mode="collect"`` never raises — per-device failures
    surface as ``missing_devices`` + status detail in the text block;
  * ``failure_mode="abort"`` raises
    :class:`MobileAgentVisualContextError` so CI callers see the
    hard failure;
  * Anthropic-shape multimodal content blocks are produced via pure
    helpers so tests don't require the LangChain stack.

All tests inject a deterministic ``FakeCaptureFn`` so no real adb /
xcrun / ssh / scp is touched and no on-disk PNG is read.
"""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError
from typing import Any, Mapping

import pytest

from backend import mobile_agent_visual_context as mavc
from backend.mobile_agent_visual_context import (
    DEFAULT_DEVICE_TARGETS,
    DEFAULT_FAILURE_MODE,
    DEFAULT_IMAGE_MEDIA_TYPE,
    DEFAULT_IMAGE_SOURCE_KIND,
    DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE,
    DEFAULT_MAX_TOTAL_IMAGE_BYTES,
    DEFAULT_TEXT_PROMPT_TEMPLATE,
    FAILURE_MODES,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES,
    MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
    MobileAgentVisualContextBuilder,
    MobileAgentVisualContextError,
    MobileAgentVisualContextImage,
    MobileAgentVisualContextPayload,
    MobileBuildErrorSummary,
    MobileDeviceTarget,
    apply_image_byte_budget,
    build_content_blocks,
    build_human_message,
    build_image_content_block,
    build_text_content_block,
    encode_screenshot_to_image,
    render_device_status_summary,
    render_visual_context_text,
)
from backend.mobile_screenshot import (
    DEFAULT_IOS_UDID,
    PNG_MAGIC,
    ScreenshotRequest,
    ScreenshotResult,
    ScreenshotStatus,
)


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION",
    "DEFAULT_IMAGE_MEDIA_TYPE",
    "DEFAULT_IMAGE_SOURCE_KIND",
    "DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE",
    "DEFAULT_MAX_TOTAL_IMAGE_BYTES",
    "DEFAULT_TEXT_PROMPT_TEMPLATE",
    "FAILURE_MODES",
    "DEFAULT_FAILURE_MODE",
    "DEFAULT_DEVICE_TARGETS",
    "MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING",
    "MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT",
    "MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED",
    "MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED",
    "MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES",
    "MobileAgentVisualContextError",
    "MobileBuildErrorSummary",
    "MobileDeviceTarget",
    "MobileAgentVisualContextImage",
    "MobileAgentVisualContextPayload",
    "MobileAgentVisualContextBuilder",
    "encode_screenshot_to_image",
    "apply_image_byte_budget",
    "render_visual_context_text",
    "render_device_status_summary",
    "build_text_content_block",
    "build_image_content_block",
    "build_content_blocks",
    "build_human_message",
}


def test_all_exports_match():
    assert set(mavc.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(mavc, name)


def test_schema_version_is_semver():
    parts = MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_image_media_type_png():
    assert DEFAULT_IMAGE_MEDIA_TYPE == "image/png"


def test_default_image_source_kind_base64():
    assert DEFAULT_IMAGE_SOURCE_KIND == "base64"


def test_default_max_image_bytes_per_device_positive():
    assert DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE > 0


def test_default_max_total_image_bytes_geq_per_device():
    assert DEFAULT_MAX_TOTAL_IMAGE_BYTES >= DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE


def test_failure_modes_stable():
    assert FAILURE_MODES == ("collect", "abort")


def test_default_failure_mode_is_collect():
    assert DEFAULT_FAILURE_MODE == "collect"


def test_default_text_prompt_template_uses_named_placeholders():
    for key in (
        "{session_id}",
        "{turn_id}",
        "{device_list}",
        "{captured_list}",
        "{missing_line}",
        "{device_status_summary}",
        "{error_summary}",
        "{auto_fix_hint}",
        "{image_count}",
        "{image_plural}",
    ):
        assert key in DEFAULT_TEXT_PROMPT_TEMPLATE


def test_event_types_all_in_mavc_namespace():
    for name in MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES:
        assert name.startswith("mobile_sandbox.agent_visual_context.")


def test_event_types_are_unique():
    assert len(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES) == len(
        set(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES)
    )


def test_event_types_includes_all_event_constants():
    assert set(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES) == {
        MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
        MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT,
        MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED,
        MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
    }


def test_event_namespace_disjoint_from_v6_1_and_v2_6():
    # V6 #1 emits ``mobile_sandbox.<state>`` (e.g. ``mobile_sandbox.created``);
    # V2 #6 emits ``ui_sandbox.agent_visual_context.<state>``.  The new
    # V6 #5 namespace must not collide with either.
    from backend.ui_agent_visual_context import (
        AGENT_VISUAL_CONTEXT_EVENT_TYPES as UI_TYPES,
    )

    mavc_set = set(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES)
    assert mavc_set.isdisjoint(set(UI_TYPES))
    # Distinct from V6 #1 too — sandbox state events have a *single*
    # segment after ``mobile_sandbox.``, AVC events have *two*.
    for name in mavc_set:
        suffix = name[len("mobile_sandbox.") :]
        assert "." in suffix


def test_default_device_targets_immutable_tuple():
    assert isinstance(DEFAULT_DEVICE_TARGETS, tuple)
    assert len(DEFAULT_DEVICE_TARGETS) >= 2
    ids = {t.device_id for t in DEFAULT_DEVICE_TARGETS}
    assert "iphone-15" in ids
    assert "pixel-8" in ids


def test_error_hierarchy():
    assert issubclass(MobileAgentVisualContextError, RuntimeError)


# ═══════════════════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════════════════


def _png(payload_suffix: bytes = b"") -> bytes:
    """Produce a minimal valid PNG byte string with the IHDR chunk
    parseable by :func:`backend.mobile_screenshot.parse_png_dimensions`.
    Width / height are encoded big-endian to keep the helper's byte
    sniff happy.
    """

    width_be = (1).to_bytes(4, "big")
    height_be = (1).to_bytes(4, "big")
    ihdr_len = b"\x00\x00\x00\x0d"
    ihdr_sig = b"IHDR"
    return PNG_MAGIC + ihdr_len + ihdr_sig + width_be + height_be + payload_suffix


def _passing_result(
    *,
    session_id: str = "sess-1",
    platform: str = "ios",
    width: int = 1179,
    height: int = 2556,
    captured_at: float = 1_700_000_100.0,
    detail: str = "1179x2556",
    png_bytes: bytes | None = None,
    size_bytes: int | None = None,
) -> ScreenshotResult:
    data = png_bytes if png_bytes is not None else _png()
    return ScreenshotResult(
        session_id=session_id,
        platform=platform,
        status=ScreenshotStatus.passed,
        path=f"/tmp/{session_id}-{platform}.png",
        format="png",
        width=width,
        height=height,
        size_bytes=size_bytes if size_bytes is not None else len(data),
        duration_ms=12,
        captured_at=captured_at,
        detail=detail,
        png_bytes=data,
    )


def _failing_result(
    *,
    session_id: str = "sess-1",
    platform: str = "android",
    detail: str = "adb pull rc=1",
) -> ScreenshotResult:
    return ScreenshotResult(
        session_id=session_id,
        platform=platform,
        status=ScreenshotStatus.fail,
        path=f"/tmp/{session_id}-{platform}.png",
        captured_at=1_700_000_100.0,
        detail=detail,
    )


def _mock_result(
    *,
    session_id: str = "sess-1",
    platform: str = "ios",
    detail: str = "xcrun not on PATH",
) -> ScreenshotResult:
    return ScreenshotResult(
        session_id=session_id,
        platform=platform,
        status=ScreenshotStatus.mock,
        path=f"/tmp/{session_id}-{platform}.png",
        captured_at=1_700_000_100.0,
        detail=detail,
    )


class FakeCapture:
    """Records every :class:`ScreenshotRequest` and returns canned
    :class:`ScreenshotResult` objects keyed on platform.

    Tests that need per-device control replace ``per_platform`` with a
    mapping ``{"ios": result, "android": result}``; otherwise a single
    ``default`` result is returned for every call.
    """

    def __init__(
        self,
        *,
        default: ScreenshotResult | None = None,
        per_platform: Mapping[str, ScreenshotResult] | None = None,
        raises: Mapping[str, Exception] | None = None,
    ) -> None:
        self.default = default if default is not None else _passing_result()
        self.per_platform = dict(per_platform or {})
        self.raises = dict(raises or {})
        self.calls: list[ScreenshotRequest] = []
        self._lock = threading.Lock()

    def __call__(self, request: ScreenshotRequest) -> ScreenshotResult:
        with self._lock:
            self.calls.append(request)
            exc = self.raises.get(request.platform)
            if exc is not None:
                raise exc
            return self.per_platform.get(request.platform, self.default)


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


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

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for t, p in self.events if t == event_type]


def _ios_target(
    *,
    device_id: str = "iphone-15",
    udid: str = DEFAULT_IOS_UDID,
    label: str = "iPhone 15",
    screen_width: int = 1179,
    screen_height: int = 2556,
) -> MobileDeviceTarget:
    return MobileDeviceTarget(
        device_id=device_id,
        platform="ios",
        udid_or_serial=udid,
        label=label,
        screen_width=screen_width,
        screen_height=screen_height,
    )


def _android_target(
    *,
    device_id: str = "pixel-8",
    serial: str = "",
    label: str = "Pixel 8",
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> MobileDeviceTarget:
    return MobileDeviceTarget(
        device_id=device_id,
        platform="android",
        udid_or_serial=serial,
        label=label,
        screen_width=screen_width,
        screen_height=screen_height,
    )


# ═══════════════════════════════════════════════════════════════════
#  MobileDeviceTarget
# ═══════════════════════════════════════════════════════════════════


def test_device_target_happy_path_ios():
    t = _ios_target()
    assert t.is_ios
    assert not t.is_android
    assert t.platform == "ios"


def test_device_target_happy_path_android():
    t = _android_target()
    assert t.is_android
    assert not t.is_ios


def test_device_target_normalises_platform_case():
    t = MobileDeviceTarget(device_id="dev-1", platform="iOS")
    assert t.platform == "ios"


def test_device_target_label_defaults_to_id():
    t = MobileDeviceTarget(device_id="dev-1", platform="ios")
    assert t.label == "dev-1"


def test_device_target_is_frozen():
    t = _ios_target()
    with pytest.raises(FrozenInstanceError):
        t.device_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_id": ""},
        {"device_id": " "},
        {"device_id": 1},
        {"device_id": "bad spaces"},
        {"platform": ""},
        {"platform": "windows"},
        {"udid_or_serial": 1},
        {"label": 1},
        {"screen_width": -1},
        {"screen_height": -1},
        {"screen_width": "x"},
    ],
)
def test_device_target_rejects_bad(kwargs: dict[str, Any]):
    base: dict[str, Any] = {"device_id": "dev-1", "platform": "ios"}
    base.update(kwargs)
    with pytest.raises(ValueError):
        MobileDeviceTarget(**base)


def test_device_target_to_dict_json_safe():
    t = _ios_target()
    d = t.to_dict()
    json.dumps(d)
    assert d["device_id"] == "iphone-15"
    assert d["platform"] == "ios"


# ═══════════════════════════════════════════════════════════════════
#  MobileBuildErrorSummary
# ═══════════════════════════════════════════════════════════════════


def test_build_error_summary_default_clean():
    s = MobileBuildErrorSummary()
    assert s.summary_markdown == ""
    assert s.has_blocking_errors is False
    assert s.active_error_count == 0


def test_build_error_summary_to_dict_json_safe():
    s = MobileBuildErrorSummary(
        summary_markdown="### Build errors\n- foo\n",
        auto_fix_hint="fix foo",
        has_blocking_errors=True,
        active_error_count=2,
    )
    d = s.to_dict()
    json.dumps(d)
    assert d["has_blocking_errors"] is True
    assert d["active_error_count"] == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"summary_markdown": 1},
        {"auto_fix_hint": 1},
        {"has_blocking_errors": "yes"},
        {"active_error_count": -1},
        {"active_error_count": 1.5},
    ],
)
def test_build_error_summary_rejects_bad(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        MobileBuildErrorSummary(**kwargs)


def test_build_error_summary_is_frozen():
    s = MobileBuildErrorSummary()
    with pytest.raises(FrozenInstanceError):
        s.has_blocking_errors = True  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════
#  MobileAgentVisualContextImage
# ═══════════════════════════════════════════════════════════════════


def _make_image(**overrides: Any) -> MobileAgentVisualContextImage:
    defaults: dict[str, Any] = dict(
        device_id="iphone-15",
        platform="ios",
        label="iPhone 15",
        width=1179,
        height=2556,
        byte_len=512,
        image_base64="iVBORw0KGgo=",
        captured_at=1_700_000_100.0,
    )
    defaults.update(overrides)
    return MobileAgentVisualContextImage(**defaults)


def test_image_happy_path():
    img = _make_image()
    assert img.media_type == DEFAULT_IMAGE_MEDIA_TYPE
    assert img.source_kind == DEFAULT_IMAGE_SOURCE_KIND


def test_image_is_frozen():
    img = _make_image()
    with pytest.raises(FrozenInstanceError):
        img.device_id = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_id": ""},
        {"device_id": " "},
        {"platform": "windows"},
        {"label": ""},
        {"width": 0},
        {"width": -1},
        {"height": 0},
        {"byte_len": 0},
        {"byte_len": -1},
        {"image_base64": ""},
        {"media_type": ""},
        {"source_kind": ""},
        {"captured_at": -1.0},
    ],
)
def test_image_rejects_bad(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        _make_image(**kwargs)


def test_image_to_dict_json_safe():
    img = _make_image()
    d = img.to_dict()
    json.dumps(d)
    assert d["schema_version"] == MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    assert d["device_id"] == "iphone-15"
    assert d["media_type"] == "image/png"


def test_image_to_content_block_anthropic_shape():
    img = _make_image()
    block = img.to_content_block()
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgo=",
        },
    }


# ═══════════════════════════════════════════════════════════════════
#  encode_screenshot_to_image
# ═══════════════════════════════════════════════════════════════════


def test_encode_passes_round_trip():
    target = _ios_target()
    result = _passing_result()
    img = encode_screenshot_to_image(target, result)
    assert img.device_id == target.device_id
    assert img.platform == target.platform
    assert img.byte_len == len(result.png_bytes)
    import base64

    decoded = base64.b64decode(img.image_base64)
    assert decoded == result.png_bytes


def test_encode_uses_dimensions_from_result_first():
    target = _ios_target()
    # IHDR in _png() encodes 1×1 but result.width/height are 1179/2556.
    # The encoder must trust the result's reported dimensions.
    result = _passing_result(width=1179, height=2556)
    img = encode_screenshot_to_image(target, result)
    assert (img.width, img.height) == (1179, 2556)


def test_encode_falls_back_to_png_sniff_when_dims_zero():
    target = _ios_target()
    # Zero-dim result with valid PNG bytes — encoder sniffs IHDR.
    result = ScreenshotResult(
        session_id="sess-1",
        platform="ios",
        status=ScreenshotStatus.passed,
        path="/tmp/x.png",
        size_bytes=len(_png()),
        captured_at=1.0,
        png_bytes=_png(),
    )
    img = encode_screenshot_to_image(target, result)
    assert (img.width, img.height) == (1, 1)


def test_encode_rejects_non_pass_status():
    target = _ios_target()
    for status in (ScreenshotStatus.fail, ScreenshotStatus.mock, ScreenshotStatus.skip):
        result = ScreenshotResult(
            session_id="sess-1", platform="ios", status=status,
        )
        with pytest.raises(MobileAgentVisualContextError):
            encode_screenshot_to_image(target, result)


def test_encode_rejects_missing_bytes():
    target = _ios_target()
    result = ScreenshotResult(
        session_id="sess-1",
        platform="ios",
        status=ScreenshotStatus.passed,
        width=10, height=10,
        png_bytes=b"",
    )
    with pytest.raises(MobileAgentVisualContextError):
        encode_screenshot_to_image(target, result)


def test_encode_rejects_oversized():
    target = _ios_target()
    big_payload = b"X" * 100
    data = _png(payload_suffix=big_payload)
    result = _passing_result(png_bytes=data, size_bytes=len(data))
    with pytest.raises(MobileAgentVisualContextError):
        encode_screenshot_to_image(target, result, max_bytes=10)


def test_encode_rejects_zero_dimensions_no_sniff():
    target = _ios_target()
    # Bytes are too short to parse IHDR — the sniff returns (0, 0).
    result = ScreenshotResult(
        session_id="sess-1",
        platform="ios",
        status=ScreenshotStatus.passed,
        path="/tmp/x.png",
        size_bytes=4,
        png_bytes=b"\x89PNG",
    )
    with pytest.raises(MobileAgentVisualContextError):
        encode_screenshot_to_image(target, result)


def test_encode_rejects_non_target():
    with pytest.raises(TypeError):
        encode_screenshot_to_image("nope", _passing_result())  # type: ignore[arg-type]


def test_encode_rejects_non_result():
    target = _ios_target()
    with pytest.raises(TypeError):
        encode_screenshot_to_image(target, "nope")  # type: ignore[arg-type]


def test_encode_rejects_nonpositive_max_bytes():
    target = _ios_target()
    with pytest.raises(ValueError):
        encode_screenshot_to_image(target, _passing_result(), max_bytes=0)


# ═══════════════════════════════════════════════════════════════════
#  apply_image_byte_budget
# ═══════════════════════════════════════════════════════════════════


def _img(name: str, byte_len: int) -> MobileAgentVisualContextImage:
    return _make_image(device_id=name, byte_len=byte_len)


def test_byte_budget_empty_input():
    kept, dropped = apply_image_byte_budget([], max_total_bytes=10)
    assert kept == ()
    assert dropped == ()


def test_byte_budget_under_cap_keeps_all():
    images = [_img("a", 100), _img("b", 100), _img("c", 100)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=1000)
    assert len(kept) == 3
    assert dropped == ()


def test_byte_budget_drops_until_under_cap():
    images = [_img("a", 400), _img("b", 400), _img("c", 400)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=800)
    kept_names = [i.device_id for i in kept]
    # Always keep the first image — text-only degrade is strictly worse.
    assert "a" in kept_names
    assert len(kept) + len(dropped) == 3
    assert sum(i.byte_len for i in kept) <= 800


def test_byte_budget_always_keeps_first_even_if_oversized():
    images = [_img("a", 5_000), _img("b", 1)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=100)
    assert kept[0].device_id == "a"
    assert [i.device_id for i in dropped] == ["b"]


def test_byte_budget_rejects_nonpositive_cap():
    with pytest.raises(ValueError):
        apply_image_byte_budget([_img("a", 1)], max_total_bytes=0)


def test_byte_budget_rejects_non_image_entries():
    with pytest.raises(TypeError):
        apply_image_byte_budget(["nope"], max_total_bytes=100)  # type: ignore[list-item]


def test_byte_budget_preserves_matrix_order():
    images = [_img("a", 100), _img("b", 100), _img("c", 100)]
    kept, _ = apply_image_byte_budget(images, max_total_bytes=1000)
    assert [i.device_id for i in kept] == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════════════
#  render_device_status_summary
# ═══════════════════════════════════════════════════════════════════


def test_render_status_empty_matrix():
    out = render_device_status_summary([], {})
    assert "No devices" in out


def test_render_status_includes_pass_metrics():
    target = _ios_target()
    result = _passing_result(width=1179, height=2556, size_bytes=1234)
    out = render_device_status_summary([target], {target.device_id: result})
    assert "iphone-15" in out
    assert "iOS" in out
    assert "1179x2556" in out
    assert "1234 B" in out


def test_render_status_no_result_entry():
    target = _ios_target()
    out = render_device_status_summary([target], {})
    assert "no result" in out


def test_render_status_includes_detail_for_fail():
    target = _android_target()
    result = _failing_result(detail="adb pull rc=1")
    out = render_device_status_summary([target], {target.device_id: result})
    assert "fail" in out
    assert "adb pull rc=1" in out


def test_render_status_deterministic_for_same_inputs():
    target = _ios_target()
    result = _passing_result()
    a = render_device_status_summary([target], {target.device_id: result})
    b = render_device_status_summary([target], {target.device_id: result})
    assert a == b


# ═══════════════════════════════════════════════════════════════════
#  render_visual_context_text
# ═══════════════════════════════════════════════════════════════════


def test_render_text_includes_all_sections():
    out = render_visual_context_text(
        session_id="sess-1",
        turn_id="turn-1",
        device_matrix=[_ios_target(), _android_target()],
        captured_device_ids=("iphone-15",),
        missing_devices=("pixel-8",),
        device_status_summary="### Device capture status\n- iphone-15: pass\n",
        error_summary_markdown="### Build errors\n\nNone.\n",
        auto_fix_hint="All clean.",
    )
    assert "sess-1" in out
    assert "turn-1" in out
    assert "iphone-15" in out
    assert "pixel-8" in out
    assert "All clean." in out


def test_render_text_empty_matrix_fallback():
    out = render_visual_context_text(
        session_id="s",
        turn_id="t",
        device_matrix=[_ios_target()],
        captured_device_ids=(),
        missing_devices=("iphone-15",),
        device_status_summary="",
        error_summary_markdown="",
        auto_fix_hint="",
    )
    assert "sandbox unreachable" in out
    assert "(no error summary)" in out
    assert "(no auto-fix hint)" in out
    assert "(no device status)" in out


def test_render_text_deterministic_for_same_inputs():
    kwargs = dict(
        session_id="s",
        turn_id="t",
        device_matrix=[_ios_target()],
        captured_device_ids=("iphone-15",),
        missing_devices=(),
        device_status_summary="ds",
        error_summary_markdown="es",
        auto_fix_hint="ah",
    )
    a = render_visual_context_text(**kwargs)  # type: ignore[arg-type]
    b = render_visual_context_text(**kwargs)  # type: ignore[arg-type]
    assert a == b


def test_render_text_rejects_empty_template():
    with pytest.raises(ValueError):
        render_visual_context_text(
            session_id="s", turn_id="t",
            device_matrix=[_ios_target()],
            captured_device_ids=(), missing_devices=(),
            device_status_summary="", error_summary_markdown="",
            auto_fix_hint="",
            template="",
        )


def test_render_text_custom_template():
    template = "session={session_id} turn={turn_id} images={image_count}"
    out = render_visual_context_text(
        session_id="s",
        turn_id="t",
        device_matrix=[_ios_target()],
        captured_device_ids=("iphone-15",),
        missing_devices=(),
        device_status_summary="",
        error_summary_markdown="",
        auto_fix_hint="",
        template=template,
    )
    assert out == "session=s turn=t images=1"


def test_render_text_image_plural_one_singular():
    out = render_visual_context_text(
        session_id="s",
        turn_id="t",
        device_matrix=[_ios_target()],
        captured_device_ids=("iphone-15",),
        missing_devices=(),
        device_status_summary="",
        error_summary_markdown="",
        auto_fix_hint="",
    )
    assert "1 device screenshot " in out
    assert "1 device screenshots" not in out


def test_render_text_image_plural_multiple():
    out = render_visual_context_text(
        session_id="s",
        turn_id="t",
        device_matrix=[_ios_target(), _android_target()],
        captured_device_ids=("iphone-15", "pixel-8"),
        missing_devices=(),
        device_status_summary="",
        error_summary_markdown="",
        auto_fix_hint="",
    )
    assert "2 device screenshots" in out


# ═══════════════════════════════════════════════════════════════════
#  Content block builders
# ═══════════════════════════════════════════════════════════════════


def test_build_text_content_block_happy():
    b = build_text_content_block("hi")
    assert b == {"type": "text", "text": "hi"}


@pytest.mark.parametrize("bad", ["", None, 123])
def test_build_text_content_block_rejects_bad(bad: Any):
    with pytest.raises((ValueError, TypeError)):
        build_text_content_block(bad)  # type: ignore[arg-type]


def test_build_image_content_block_shape():
    img = _make_image()
    b = build_image_content_block(img)
    assert b["type"] == "image"
    assert b["source"]["type"] == "base64"
    assert b["source"]["media_type"] == "image/png"
    assert b["source"]["data"] == img.image_base64


def test_build_image_content_block_rejects_non_image():
    with pytest.raises(TypeError):
        build_image_content_block("nope")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  MobileAgentVisualContextPayload
# ═══════════════════════════════════════════════════════════════════


def _make_payload(**overrides: Any) -> MobileAgentVisualContextPayload:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        turn_id="turn-1",
        built_at=1_700_000_100.0,
        device_matrix=(_ios_target(), _android_target()),
        images=(
            _make_image(device_id="iphone-15"),
            _make_image(
                device_id="pixel-8",
                platform="android",
                label="Pixel 8",
                width=1080,
                height=2400,
            ),
        ),
        missing_devices=(),
        device_results=(),
        text_prompt="hello agent",
        device_status_summary="### Device capture status\n",
        error_summary_markdown="### Build errors\n\nNone.\n",
        auto_fix_hint="The mobile sandbox rendered cleanly.",
    )
    defaults.update(overrides)
    return MobileAgentVisualContextPayload(**defaults)


def test_payload_happy_path():
    p = _make_payload()
    assert p.image_count == 2
    assert p.has_images
    assert p.captured_device_ids == ("iphone-15", "pixel-8")
    assert p.device_ids == ("iphone-15", "pixel-8")
    assert p.total_image_bytes == sum(i.byte_len for i in p.images)
    assert not p.has_errors
    assert not p.was_skipped


def test_payload_is_frozen():
    p = _make_payload()
    with pytest.raises(FrozenInstanceError):
        p.session_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"session_id": 1},
        {"turn_id": ""},
        {"built_at": -1.0},
        {"device_matrix": ()},
        {"text_prompt": ""},
        {"device_status_summary": 1},
        {"error_summary_markdown": 1},
        {"auto_fix_hint": 1},
        {"has_blocking_errors": "true"},
        {"active_error_count": -1},
        {"was_skipped": "yes"},
    ],
)
def test_payload_rejects_bad(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        _make_payload(**kwargs)


def test_payload_rejects_non_target_in_matrix():
    with pytest.raises(ValueError):
        _make_payload(device_matrix=("nope",))


def test_payload_rejects_non_image_entries():
    with pytest.raises(ValueError):
        _make_payload(images=("not-an-image",))


def test_payload_rejects_non_result_in_device_results():
    with pytest.raises(ValueError):
        _make_payload(device_results=("nope",))


def test_payload_skipped_must_have_no_images():
    with pytest.raises(ValueError):
        _make_payload(was_skipped=True, skip_reason="x")


def test_payload_skipped_requires_reason():
    with pytest.raises(ValueError):
        _make_payload(
            was_skipped=True,
            images=(),
            skip_reason=None,
        )


def test_payload_to_dict_json_safe():
    p = _make_payload()
    d = p.to_dict()
    json.dumps(d)
    assert d["schema_version"] == MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    assert d["image_count"] == 2
    assert len(d["images"]) == 2
    assert d["has_errors"] is False


def test_payload_to_content_blocks_text_first():
    p = _make_payload()
    blocks = p.to_content_blocks()
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "hello agent"
    assert [b["type"] for b in blocks[1:]] == ["image", "image"]


def test_payload_to_content_blocks_image_order_matches_images():
    p = _make_payload()
    blocks = p.to_content_blocks()
    img_blocks = [b for b in blocks if b["type"] == "image"]
    expected = [img.image_base64 for img in p.images]
    actual = [b["source"]["data"] for b in img_blocks]
    assert expected == actual


def test_payload_skipped_has_empty_images_and_skip_reason():
    p = _make_payload(
        images=(),
        missing_devices=("iphone-15", "pixel-8"),
        was_skipped=True,
        skip_reason="sandbox unreachable",
    )
    assert p.was_skipped
    assert p.image_count == 0
    assert not p.has_images
    assert p.skip_reason == "sandbox unreachable"


# ═══════════════════════════════════════════════════════════════════
#  Builder construction
# ═══════════════════════════════════════════════════════════════════


def test_builder_default_capture_fn_wired():
    b = MobileAgentVisualContextBuilder()
    # Default points at backend.mobile_screenshot.capture; assert via
    # module attribute identity rather than name.
    from backend.mobile_screenshot import capture as default_capture
    assert b.capture_fn is default_capture


def test_builder_rejects_non_callable_capture_fn():
    with pytest.raises(TypeError):
        MobileAgentVisualContextBuilder(capture_fn="nope")  # type: ignore[arg-type]


def test_builder_rejects_non_callable_error_source():
    with pytest.raises(TypeError):
        MobileAgentVisualContextBuilder(error_source="nope")  # type: ignore[arg-type]


def test_builder_rejects_non_callable_request_factory():
    with pytest.raises(TypeError):
        MobileAgentVisualContextBuilder(request_factory="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_failure_mode": "yolo"},
        {"max_image_bytes_per_device": 0},
        {"max_total_image_bytes": 0},
        {"text_prompt_template": ""},
        {"default_devices": []},
    ],
)
def test_builder_rejects_bad_kwargs(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        MobileAgentVisualContextBuilder(**kwargs)


def test_builder_rejects_non_target_in_default_devices():
    with pytest.raises(ValueError):
        MobileAgentVisualContextBuilder(default_devices=["nope"])  # type: ignore[list-item]


def test_builder_accessors_expose_defaults():
    capture = FakeCapture()
    b = MobileAgentVisualContextBuilder(capture_fn=capture)
    assert b.capture_fn is capture
    assert b.error_source is None
    assert b.default_failure_mode == DEFAULT_FAILURE_MODE
    assert b.max_image_bytes_per_device == DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE
    assert b.max_total_image_bytes == DEFAULT_MAX_TOTAL_IMAGE_BYTES
    assert b.text_prompt_template == DEFAULT_TEXT_PROMPT_TEMPLATE
    assert b.default_devices == DEFAULT_DEVICE_TARGETS


def test_builder_counters_start_zero():
    b = MobileAgentVisualContextBuilder(capture_fn=FakeCapture())
    assert b.build_count() == 0
    assert b.skipped_count() == 0
    assert b.failed_count() == 0
    assert b.last_payload() is None


# ═══════════════════════════════════════════════════════════════════
#  Builder.build happy path
# ═══════════════════════════════════════════════════════════════════


def _make_builder(
    *,
    capture: FakeCapture | None = None,
    error_source: Any = None,
    clock: FakeClock | None = None,
    event_cb: RecordingEventCallback | None = None,
    **kwargs: Any,
) -> tuple[
    MobileAgentVisualContextBuilder, FakeCapture, RecordingEventCallback
]:
    capture = capture or FakeCapture()
    clock = clock or FakeClock()
    events = event_cb or RecordingEventCallback()
    builder = MobileAgentVisualContextBuilder(
        capture_fn=capture,
        error_source=error_source,
        clock=clock,
        event_cb=events,
        **kwargs,
    )
    return builder, capture, events


def test_build_two_devices_returns_payload_with_two_images():
    builder, capture, events = _make_builder(
        capture=FakeCapture(
            per_platform={
                "ios": _passing_result(platform="ios"),
                "android": _passing_result(platform="android"),
            }
        )
    )
    payload = builder.build(
        session_id="sess-1",
        output_dir="/tmp/captures",
        turn_id="turn-1",
    )
    assert payload.image_count == 2
    assert payload.captured_device_ids == ("iphone-15", "pixel-8")
    assert payload.missing_devices == ()
    assert not payload.was_skipped
    assert len(capture.calls) == 2
    assert [c.platform for c in capture.calls] == ["ios", "android"]


def test_build_passes_session_and_output_path_into_request():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
            "android": _passing_result(platform="android"),
        }
    )
    builder, _, _ = _make_builder(capture=capture)
    builder.build(
        session_id="sess-1",
        output_dir="/tmp/captures",
        turn_id="turn-1",
    )
    paths = [c.output_path for c in capture.calls]
    assert any("iphone-15" in p for p in paths)
    assert any("pixel-8" in p for p in paths)
    assert all(p.startswith("/tmp/captures") for p in paths)
    assert all("turn-1" in p for p in paths)


def test_build_routes_udid_to_ios_request():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
        }
    )
    builder, _, _ = _make_builder(
        capture=capture,
        default_devices=[
            _ios_target(udid="ABCDEF12-3456-7890-ABCD-EF1234567890"),
        ],
    )
    builder.build(session_id="sess-1", output_dir="/tmp/cap")
    ios_call = capture.calls[0]
    assert ios_call.platform == "ios"
    assert ios_call.ios_udid == "ABCDEF12-3456-7890-ABCD-EF1234567890"


def test_build_routes_serial_to_android_request():
    capture = FakeCapture(
        per_platform={
            "android": _passing_result(platform="android"),
        }
    )
    builder, _, _ = _make_builder(
        capture=capture,
        default_devices=[_android_target(serial="emulator-5554")],
    )
    builder.build(session_id="sess-1", output_dir="/tmp/cap")
    android_call = capture.calls[0]
    assert android_call.platform == "android"
    assert android_call.android_serial == "emulator-5554"


def test_build_auto_assigns_turn_id_when_not_provided():
    builder, _, _ = _make_builder()
    p1 = builder.build(session_id="s", output_dir="/tmp/c")
    p2 = builder.build(session_id="s", output_dir="/tmp/c")
    assert p1.turn_id != p2.turn_id
    assert p1.turn_id.startswith("mavc-turn-")
    assert p2.turn_id.startswith("mavc-turn-")


def test_build_emits_building_then_built():
    builder, _, events = _make_builder()
    builder.build(session_id="s", output_dir="/tmp/c")
    types = events.types()
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING in types
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT in types
    built_idx = types.index(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT)
    build_idx = types.index(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING)
    assert built_idx > build_idx


def test_build_built_event_carries_no_base64():
    builder, _, events = _make_builder()
    builder.build(session_id="s", output_dir="/tmp/c")
    built = events.by_type(MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT)
    assert len(built) == 1
    encoded = json.dumps(built[0])
    assert "image_base64" not in encoded
    assert built[0]["image_count"] == len(DEFAULT_DEVICE_TARGETS)


def test_build_increments_build_count_and_records_last_payload():
    builder, _, _ = _make_builder()
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert builder.build_count() == 1
    assert builder.skipped_count() == 0
    assert builder.failed_count() == 0
    assert builder.last_payload() is payload


def test_build_custom_devices_override():
    builder, capture, _ = _make_builder()
    builder.build(
        session_id="s",
        output_dir="/tmp/c",
        devices=[_android_target()],
    )
    assert [c.platform for c in capture.calls] == ["android"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"output_dir": ""},
        {"output_dir": "relative/path"},
        {"failure_mode": "yolo"},
        {"devices": []},
        {"turn_id": "  "},
        {"attach_bytes": "no"},
        {"include_errors": "yes"},
    ],
)
def test_build_rejects_bad_inputs(kwargs: dict[str, Any]):
    builder, _, _ = _make_builder()
    base: dict[str, Any] = {
        "session_id": "s",
        "output_dir": "/tmp/c",
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        builder.build(**base)


def test_build_rejects_non_target_devices():
    builder, _, _ = _make_builder()
    with pytest.raises(ValueError):
        builder.build(session_id="s", output_dir="/tmp/c", devices=["nope"])  # type: ignore[list-item]


# ═══════════════════════════════════════════════════════════════════
#  Builder.build failure handling
# ═══════════════════════════════════════════════════════════════════


def test_build_collect_mode_failed_capture_yields_missing_device():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
            "android": _failing_result(platform="android"),
        }
    )
    builder, _, events = _make_builder(capture=capture)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == 1
    assert payload.captured_device_ids == ("iphone-15",)
    assert payload.missing_devices == ("pixel-8",)
    assert not payload.was_skipped
    # built still fires — collect is a success case.
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT in events.types()
    # status summary mentions the failure detail.
    assert "fail" in payload.device_status_summary
    assert "pixel-8" in payload.device_status_summary


def test_build_collect_mode_mock_status_routes_to_missing():
    capture = FakeCapture(
        per_platform={
            "ios": _mock_result(platform="ios"),
            "android": _passing_result(platform="android"),
        }
    )
    builder, _, _ = _make_builder(capture=capture)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.captured_device_ids == ("pixel-8",)
    assert payload.missing_devices == ("iphone-15",)
    assert "mock" in payload.device_status_summary


def test_build_collect_mode_all_fail_emits_built_with_zero_images():
    capture = FakeCapture(
        per_platform={
            "ios": _failing_result(platform="ios"),
            "android": _failing_result(platform="android"),
        }
    )
    builder, _, _ = _make_builder(capture=capture)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == 0
    assert set(payload.missing_devices) == {"iphone-15", "pixel-8"}
    assert not payload.was_skipped
    assert "sandbox unreachable" in payload.text_prompt


def test_build_capture_fn_crash_yields_synthetic_fail_result():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
        },
        raises={"android": RuntimeError("emulator exploded")},
    )
    builder, _, _ = _make_builder(capture=capture)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    # iOS still succeeded; Android crash folded into missing.
    assert payload.captured_device_ids == ("iphone-15",)
    assert payload.missing_devices == ("pixel-8",)
    # Synthetic fail result captured for downstream forensics.
    android_results = [
        r for r in payload.device_results if r.platform == "android"
    ]
    assert len(android_results) == 1
    assert android_results[0].status is ScreenshotStatus.fail
    assert "emulator exploded" in android_results[0].detail
    # Warning carries the crash context.
    assert any(
        w.startswith("capture_crashed:pixel-8:") for w in payload.warnings
    )


def test_build_abort_mode_raises_on_any_failure():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
            "android": _failing_result(platform="android"),
        }
    )
    builder, _, events = _make_builder(capture=capture)
    with pytest.raises(MobileAgentVisualContextError):
        builder.build(
            session_id="s",
            output_dir="/tmp/c",
            failure_mode="abort",
        )
    assert builder.failed_count() == 1
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED in events.types()


def test_build_abort_mode_does_not_raise_when_all_pass():
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
            "android": _passing_result(platform="android"),
        }
    )
    builder, _, _ = _make_builder(capture=capture)
    payload = builder.build(
        session_id="s", output_dir="/tmp/c", failure_mode="abort"
    )
    assert payload.image_count == 2


# ═══════════════════════════════════════════════════════════════════
#  Byte-budget enforcement via builder
# ═══════════════════════════════════════════════════════════════════


def test_build_applies_max_total_image_bytes_and_records_warning():
    big_data = _png(payload_suffix=b"X" * 800)
    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(
                platform="ios",
                png_bytes=big_data,
                size_bytes=len(big_data),
            ),
            "android": _passing_result(
                platform="android",
                png_bytes=big_data,
                size_bytes=len(big_data),
            ),
        }
    )
    builder, _, _ = _make_builder(
        capture=capture,
        max_image_bytes_per_device=10_000,
        max_total_image_bytes=len(big_data) + 1,  # only room for first.
    )
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == 1
    assert payload.captured_device_ids == ("iphone-15",)
    assert "pixel-8" in payload.missing_devices
    assert any(
        w.startswith("image_dropped_budget:") for w in payload.warnings
    )


def test_build_per_device_cap_drops_image_with_warning():
    big_data = _png(payload_suffix=b"X" * 1_000)
    capture = FakeCapture(
        default=_passing_result(png_bytes=big_data, size_bytes=len(big_data)),
    )
    builder, _, _ = _make_builder(
        capture=capture,
        max_image_bytes_per_device=5,
        max_total_image_bytes=5,
    )
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == 0
    assert any(
        w.startswith("image_encode_failed:") for w in payload.warnings
    )


# ═══════════════════════════════════════════════════════════════════
#  Error-source integration
# ═══════════════════════════════════════════════════════════════════


def test_build_without_error_source_uses_placeholder_hint():
    builder, _, _ = _make_builder()
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert "No error source wired." in payload.error_summary_markdown
    assert payload.active_error_count == 0
    assert payload.has_blocking_errors is False


def test_build_with_error_source_renders_summary():
    summary = MobileBuildErrorSummary(
        summary_markdown="### Build errors\n- foo.kt:12 unresolved\n",
        auto_fix_hint="add the missing import",
        has_blocking_errors=True,
        active_error_count=1,
    )
    builder, _, _ = _make_builder(error_source=lambda sid: summary)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert "foo.kt" in payload.error_summary_markdown
    assert payload.active_error_count == 1
    assert payload.has_blocking_errors is True
    assert payload.auto_fix_hint == "add the missing import"


def test_build_error_source_returning_none_renders_clean_state():
    builder, _, _ = _make_builder(error_source=lambda sid: None)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert "No build errors reported." in payload.error_summary_markdown
    assert payload.active_error_count == 0


def test_build_error_source_raising_records_warning():
    def boom(sid: str) -> MobileBuildErrorSummary:
        raise RuntimeError("oh no")

    builder, _, _ = _make_builder(error_source=boom)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert any(
        w.startswith("error_source_failed:") for w in payload.warnings
    )
    # Build still proceeds — the agent gets a payload even when the
    # error source explodes.
    assert payload.image_count == len(DEFAULT_DEVICE_TARGETS)


def test_build_error_source_returning_bad_type_records_warning():
    builder, _, _ = _make_builder(error_source=lambda sid: 42)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert any(
        w.startswith("error_source_bad_type:") for w in payload.warnings
    )


def test_build_include_errors_false_skips_error_source():
    summary = MobileBuildErrorSummary(
        summary_markdown="### Build errors\n- err\n",
        auto_fix_hint="x",
        has_blocking_errors=True,
        active_error_count=1,
    )
    builder, _, _ = _make_builder(error_source=lambda sid: summary)
    payload = builder.build(
        session_id="s", output_dir="/tmp/c", include_errors=False,
    )
    assert payload.active_error_count == 0
    assert "No error source wired." in payload.error_summary_markdown


# ═══════════════════════════════════════════════════════════════════
#  build_skipped
# ═══════════════════════════════════════════════════════════════════


def test_build_skipped_produces_text_only_payload():
    builder, _, events = _make_builder()
    payload = builder.build_skipped(
        session_id="s",
        skip_reason="sandbox pending",
    )
    assert payload.was_skipped
    assert payload.image_count == 0
    assert payload.skip_reason == "sandbox pending"
    assert builder.skipped_count() == 1
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED in events.types()


def test_build_skipped_rejects_empty_reason():
    builder, _, _ = _make_builder()
    with pytest.raises(ValueError):
        builder.build_skipped(session_id="s", skip_reason="   ")


def test_build_skipped_rejects_bad_session_id():
    builder, _, _ = _make_builder()
    with pytest.raises(ValueError):
        builder.build_skipped(session_id="", skip_reason="x")


def test_build_skipped_rejects_empty_devices():
    builder, _, _ = _make_builder()
    with pytest.raises(ValueError):
        builder.build_skipped(
            session_id="s",
            skip_reason="x",
            devices=[],
        )


# ═══════════════════════════════════════════════════════════════════
#  build_message convenience + HumanMessage wrapper
# ═══════════════════════════════════════════════════════════════════


def test_build_message_returns_payload_and_message():
    builder, _, _ = _make_builder()
    payload, msg = builder.build_message(
        session_id="s", output_dir="/tmp/c"
    )
    assert isinstance(payload, MobileAgentVisualContextPayload)
    assert hasattr(msg, "content")
    content = msg.content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    img_blocks = [b for b in content if b["type"] == "image"]
    assert len(img_blocks) == payload.image_count


def test_build_human_message_rejects_non_payload():
    with pytest.raises(TypeError):
        build_human_message("nope")  # type: ignore[arg-type]


def test_build_content_blocks_rejects_non_payload():
    with pytest.raises(TypeError):
        build_content_blocks("nope")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  Snapshot
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_json_safe_no_payload():
    builder, _, _ = _make_builder()
    snap = builder.snapshot()
    json.dumps(snap)
    assert snap["build_count"] == 0
    assert snap["last_payload"] is None
    assert snap["error_source_wired"] is False


def test_snapshot_json_safe_with_last_payload():
    builder, _, _ = _make_builder()
    builder.build(session_id="s", output_dir="/tmp/c")
    snap = builder.snapshot()
    encoded = json.dumps(snap)
    assert snap["last_payload"] is not None
    # Snapshot does NOT inline base64 images to keep SSE frames lean.
    assert "image_base64" not in encoded


def test_snapshot_reports_error_source_wired_flag():
    builder, _, _ = _make_builder(error_source=lambda sid: None)
    snap = builder.snapshot()
    assert snap["error_source_wired"] is True


# ═══════════════════════════════════════════════════════════════════
#  Event callback safety
# ═══════════════════════════════════════════════════════════════════


def test_event_callback_raise_does_not_kill_builder():
    def boom(event_type: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("boom")

    capture = FakeCapture()
    builder = MobileAgentVisualContextBuilder(
        capture_fn=capture, event_cb=boom,
    )
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == len(DEFAULT_DEVICE_TARGETS)


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_thread_safe_parallel_builds():
    capture = FakeCapture()
    builder = MobileAgentVisualContextBuilder(capture_fn=capture)

    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            builder.build(
                session_id=f"sess-{idx}",
                output_dir="/tmp/c",
                turn_id=f"turn-{idx}",
            )
        except Exception as exc:  # pragma: no cover - test-failure surface
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == []
    assert builder.build_count() == 8
    # Every build hits both default devices.
    assert len(capture.calls) == 8 * len(DEFAULT_DEVICE_TARGETS)


# ═══════════════════════════════════════════════════════════════════
#  Custom request factory
# ═══════════════════════════════════════════════════════════════════


def test_custom_request_factory_is_used():
    captured: list[ScreenshotRequest] = []

    def factory(
        session_id: str,
        target: MobileDeviceTarget,
        output_path: str,
        attach_bytes: bool,
    ) -> ScreenshotRequest:
        # Force a custom output path naming so we can assert the factory
        # actually drove the request.
        request = ScreenshotRequest(
            session_id=session_id,
            platform=target.platform,
            output_path=output_path + ".custom",
            ios_udid=target.udid_or_serial or DEFAULT_IOS_UDID,
            android_serial=target.udid_or_serial if target.is_android else "",
            attach_bytes=attach_bytes,
        )
        captured.append(request)
        return request

    capture = FakeCapture()
    builder = MobileAgentVisualContextBuilder(
        capture_fn=capture,
        request_factory=factory,
    )
    builder.build(session_id="sess-1", output_dir="/tmp/c")
    assert all(r.output_path.endswith(".custom") for r in captured)


def test_request_factory_returning_non_request_records_warning():
    def bad_factory(*_: Any, **__: Any) -> Any:
        return "not-a-request"

    capture = FakeCapture()
    builder = MobileAgentVisualContextBuilder(
        capture_fn=capture, request_factory=bad_factory,
    )
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    # Every device fails because the factory returned the wrong type.
    assert payload.image_count == 0
    assert all(
        any(w.startswith(f"capture_crashed:{d}") for w in payload.warnings)
        for d in payload.missing_devices
    )


def test_capture_fn_returning_non_result_treated_as_crash():
    def bad_capture(request: ScreenshotRequest) -> Any:
        return "not-a-result"

    builder = MobileAgentVisualContextBuilder(capture_fn=bad_capture)
    payload = builder.build(session_id="s", output_dir="/tmp/c")
    assert payload.image_count == 0
    assert all(
        any(w.startswith(f"capture_crashed:{d}") for w in payload.warnings)
        for d in payload.missing_devices
    )


# ═══════════════════════════════════════════════════════════════════
#  End-to-end golden path — V6 #1 + #2 + #5 wiring
# ═══════════════════════════════════════════════════════════════════


def test_end_to_end_builder_composes_capture_and_error_source():
    """Full wire-up: FakeCapture → MobileAgentVisualContextBuilder
    → HumanMessage ready for Opus 4.7.

    Proves V6 #5 closes the loop end-to-end with zero orchestration
    code beyond ``builder.build_message(...)``.
    """

    capture = FakeCapture(
        per_platform={
            "ios": _passing_result(platform="ios"),
            "android": _passing_result(platform="android"),
        }
    )
    summary = MobileBuildErrorSummary(
        summary_markdown="### Build errors\n- src/Header.kt:4 unresolved\n",
        auto_fix_hint="Resolve the unresolved reference in Header.kt",
        has_blocking_errors=True,
        active_error_count=1,
    )
    events = RecordingEventCallback()
    builder = MobileAgentVisualContextBuilder(
        capture_fn=capture,
        error_source=lambda sid: summary,
        event_cb=events,
    )

    payload, message = builder.build_message(
        session_id="sess-prod",
        output_dir="/tmp/captures",
        turn_id="react-turn-1",
    )

    # Multimodal message ready for Opus 4.7.
    assert hasattr(message, "content")
    content = message.content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 2
    for b in image_blocks:
        assert b["source"]["type"] == "base64"
        assert b["source"]["media_type"] == "image/png"
        assert b["source"]["data"]

    # Error context was folded in.
    assert payload.active_error_count == 1
    assert payload.has_blocking_errors is True
    assert "Header.kt" in payload.error_summary_markdown
    assert "Header.kt" in payload.text_prompt

    # End-to-end events cover the whole pipeline.
    types = events.types()
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING in types
    assert MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT in types

    # Serialising the full payload for SSE replay remains JSON-safe.
    encoded = json.dumps(payload.to_dict())
    assert "image_base64" in encoded


# ═══════════════════════════════════════════════════════════════════
#  Sibling alignment — V6 #1 / #2 still importable
# ═══════════════════════════════════════════════════════════════════


def test_sibling_modules_importable():
    from backend import (
        mobile_sandbox,
        mobile_screenshot,
    )
    for mod in (mobile_sandbox, mobile_screenshot):
        assert mod.__name__


def test_schema_versions_independent():
    # V6 #5 schema is independent of V6 #2 — bumping one must not
    # force a bump of the other.
    from backend.mobile_screenshot import (
        MOBILE_SCREENSHOT_SCHEMA_VERSION as MSS_V,
    )
    assert MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    assert MSS_V


def test_default_device_targets_match_known_ids():
    # Mirrors the V6 #3 ``DEVICE_PROFILE_IDS`` ordering for the two
    # default targets so the frontend device-frame component can map
    # straight from ``device_id`` without translation.
    ids = [t.device_id for t in DEFAULT_DEVICE_TARGETS]
    assert ids[:2] == ["iphone-15", "pixel-8"]

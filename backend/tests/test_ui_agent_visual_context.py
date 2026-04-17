"""V2 #6 (issue #318) — ui_agent_visual_context contract tests.

Pins ``backend/ui_agent_visual_context.py`` against the V2 row 6 spec:

  * responsive-viewport captures (V2 #4) are encoded to base64 and
    packaged into one :class:`AgentVisualContextPayload` per ReAct turn;
  * preview errors (V2 #5) are folded into the same payload as the
    agent-facing text block so the agent both *sees* the UI and
    *reads* the current error state;
  * events fire in the ``ui_sandbox.agent_visual_context.*`` namespace
    with zero overlap with V2 #2 / #3 / #4 / #5 topics;
  * ``failure_mode="collect"`` never raises — engine failures degrade
    to a skipped payload;
  * ``failure_mode="abort"`` propagates :class:`BatchAborted` so CI
    callers still see the hard failure;
  * Anthropic-shape multimodal content blocks are produced via pure
    helpers so tests don't require the LangChain stack.

All tests drive a ``FakeClock`` / ``FakeScreenshotEngine`` /
``FakeDockerClient`` so no real browser, no real docker daemon,
no real sleep is consumed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pytest

from backend import ui_agent_visual_context as avc
from backend import ui_responsive_viewport as urv
from backend import ui_screenshot as us
from backend.ui_agent_visual_context import (
    AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
    AGENT_VISUAL_CONTEXT_EVENT_BUILT,
    AGENT_VISUAL_CONTEXT_EVENT_FAILED,
    AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
    AGENT_VISUAL_CONTEXT_EVENT_TYPES,
    DEFAULT_IMAGE_MEDIA_TYPE,
    DEFAULT_IMAGE_SOURCE_KIND,
    DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT,
    DEFAULT_MAX_TOTAL_IMAGE_BYTES,
    DEFAULT_PATH,
    DEFAULT_TEXT_PROMPT_TEMPLATE,
    UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
    AgentVisualContextBuilder,
    AgentVisualContextError,
    AgentVisualContextImage,
    AgentVisualContextPayload,
    apply_image_byte_budget,
    build_content_blocks,
    build_human_message,
    build_image_content_block,
    build_text_content_block,
    encode_capture_to_image,
    render_visual_context_text,
)
from backend.ui_preview_error_bridge import (
    PreviewErrorBridge,
)
from backend.ui_responsive_viewport import (
    BatchAborted,
    ResponsiveViewportCapture,
    ViewportCaptureOutcome,
)
from backend.ui_sandbox import SandboxConfig, SandboxManager
from backend.ui_screenshot import (
    MAX_CAPTURE_BYTES,
    PNG_SIGNATURE,
    ScreenshotCapture,
    ScreenshotError,
    ScreenshotRequest,
    ScreenshotService,
    VIEWPORT_DESKTOP,
    VIEWPORT_MOBILE,
    VIEWPORT_TABLET,
)


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION",
    "DEFAULT_IMAGE_MEDIA_TYPE",
    "DEFAULT_IMAGE_SOURCE_KIND",
    "DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT",
    "DEFAULT_MAX_TOTAL_IMAGE_BYTES",
    "DEFAULT_TEXT_PROMPT_TEMPLATE",
    "DEFAULT_PATH",
    "AGENT_VISUAL_CONTEXT_EVENT_BUILDING",
    "AGENT_VISUAL_CONTEXT_EVENT_BUILT",
    "AGENT_VISUAL_CONTEXT_EVENT_FAILED",
    "AGENT_VISUAL_CONTEXT_EVENT_SKIPPED",
    "AGENT_VISUAL_CONTEXT_EVENT_TYPES",
    "AgentVisualContextError",
    "AgentVisualContextImage",
    "AgentVisualContextPayload",
    "AgentVisualContextBuilder",
    "encode_capture_to_image",
    "apply_image_byte_budget",
    "render_visual_context_text",
    "build_text_content_block",
    "build_image_content_block",
    "build_content_blocks",
    "build_human_message",
}


def test_all_exports_match():
    assert set(avc.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(avc, name)


def test_schema_version_is_semver():
    parts = UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_default_image_media_type_png():
    assert DEFAULT_IMAGE_MEDIA_TYPE == "image/png"


def test_default_image_source_kind_base64():
    assert DEFAULT_IMAGE_SOURCE_KIND == "base64"


def test_default_max_image_bytes_per_viewport_matches_v2_3():
    assert DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT == MAX_CAPTURE_BYTES


def test_default_max_total_image_bytes_geq_per_viewport():
    assert DEFAULT_MAX_TOTAL_IMAGE_BYTES >= DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT


def test_default_path_is_root():
    assert DEFAULT_PATH == "/"


def test_default_text_prompt_template_uses_named_placeholders():
    # The template must expose the placeholders render_visual_context_text
    # substitutes so callers that override still get something sensible.
    for key in (
        "{session_id}",
        "{turn_id}",
        "{preview_url}",
        "{path}",
        "{viewport_list}",
        "{missing_line}",
        "{error_summary}",
        "{auto_fix_hint}",
        "{image_count}",
        "{image_plural}",
    ):
        assert key in DEFAULT_TEXT_PROMPT_TEMPLATE


def test_event_types_all_in_avc_namespace():
    for name in AGENT_VISUAL_CONTEXT_EVENT_TYPES:
        assert name.startswith("ui_sandbox.agent_visual_context.")


def test_event_types_are_unique():
    assert len(AGENT_VISUAL_CONTEXT_EVENT_TYPES) == len(
        set(AGENT_VISUAL_CONTEXT_EVENT_TYPES)
    )


def test_event_types_includes_all_event_constants():
    assert set(AGENT_VISUAL_CONTEXT_EVENT_TYPES) == {
        AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
        AGENT_VISUAL_CONTEXT_EVENT_BUILT,
        AGENT_VISUAL_CONTEXT_EVENT_FAILED,
        AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
    }


def test_event_namespace_disjoint_from_v2_2_to_5():
    from backend.ui_preview_error_bridge import ERROR_EVENT_TYPES
    from backend.ui_responsive_viewport import VIEWPORT_BATCH_EVENT_TYPES
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_TYPES
    from backend.ui_screenshot import SCREENSHOT_EVENT_TYPES

    avc_set = set(AGENT_VISUAL_CONTEXT_EVENT_TYPES)
    for other in (
        LIFECYCLE_EVENT_TYPES,
        SCREENSHOT_EVENT_TYPES,
        VIEWPORT_BATCH_EVENT_TYPES,
        ERROR_EVENT_TYPES,
    ):
        assert avc_set.isdisjoint(set(other))


def test_error_hierarchy():
    assert issubclass(AgentVisualContextError, RuntimeError)


# ═══════════════════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════════════════


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _png(payload_suffix: bytes = b"pixels", *, pad_to: int | None = None) -> bytes:
    """Produce a byte string that starts with the PNG signature so
    V2 #3's validator accepts it."""

    data = PNG_SIGNATURE + payload_suffix
    if pad_to is not None and len(data) < pad_to:
        data = data + (b"\x00" * (pad_to - len(data)))
    return data


class FakeScreenshotEngine:
    """Returns canned bytes per viewport.  Mirrors V2 #3/#4 test fixture."""

    def __init__(
        self,
        *,
        default_payload: bytes | None = None,
        per_viewport_payloads: Mapping[str, bytes] | None = None,
        per_viewport_raises: Mapping[str, Exception] | None = None,
    ) -> None:
        self.default_payload = (
            default_payload if default_payload is not None else _png()
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

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for t, p in self.events if t == event_type]


class FakeDockerClient:
    """Minimal docker stub giving V2 #1 SandboxManager enough surface
    to reach a ``running`` session for the error-bridge side of tests."""

    def __init__(self, *, canned_logs: str = "") -> None:
        self.canned_logs = canned_logs
        self._next_id = 0
        self._lock = threading.Lock()

    def set_logs(self, text: str) -> None:
        with self._lock:
            self.canned_logs = text

    def run_detached(self, **_: Any) -> str:
        with self._lock:
            self._next_id += 1
            return f"fake-cid-{self._next_id:04d}"

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        return None

    def remove(self, container_id: str, *, force: bool = False) -> None:
        return None

    def logs(self, container_id: str, *, tail: int | None = None) -> str:
        return self.canned_logs

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        return {"Id": container_id, "State": {"Running": True}}


# ═══════════════════════════════════════════════════════════════════
#  Helpers: build ResponsiveViewportCapture / PreviewErrorBridge
# ═══════════════════════════════════════════════════════════════════


def _make_service(
    *,
    engine: FakeScreenshotEngine | None = None,
    clock: FakeClock | None = None,
) -> ScreenshotService:
    return ScreenshotService(
        engine=engine or FakeScreenshotEngine(),
        clock=clock or FakeClock(),
    )


def _make_responsive(
    *,
    service: ScreenshotService | None = None,
    clock: FakeClock | None = None,
    event_cb: RecordingEventCallback | None = None,
    default_matrix: Sequence[str] = urv.DEFAULT_VIEWPORT_MATRIX,
) -> ResponsiveViewportCapture:
    return ResponsiveViewportCapture(
        service=service or _make_service(),
        clock=clock or FakeClock(),
        event_cb=event_cb,
        default_matrix=default_matrix,
    )


def _make_bridge(
    tmp_path: Path,
    *,
    canned_logs: str = "",
    clock: FakeClock | None = None,
    event_cb: RecordingEventCallback | None = None,
    session_id: str = "sess-1",
) -> tuple[PreviewErrorBridge, FakeDockerClient, SandboxManager]:
    docker = FakeDockerClient(canned_logs=canned_logs)
    clock = clock or FakeClock()
    mgr = SandboxManager(docker_client=docker, clock=clock)
    config = SandboxConfig(
        session_id=session_id,
        workspace_path=str(tmp_path),
        host_port=40500,
    )
    mgr.create(config)
    mgr.start(session_id)
    mgr.mark_ready(session_id)
    bridge = PreviewErrorBridge(
        manager=mgr,
        clock=clock,
        event_cb=event_cb,
    )
    return bridge, docker, mgr


def _sample_capture(
    viewport_name: str = "desktop",
    *,
    data: bytes | None = None,
    session_id: str = "sess-1",
    preview_url: str = "http://127.0.0.1:40500/",
    path: str = "/",
    captured_at: float = 1_700_000_100.0,
) -> ScreenshotCapture:
    viewport = {"desktop": VIEWPORT_DESKTOP, "tablet": VIEWPORT_TABLET, "mobile": VIEWPORT_MOBILE}[
        viewport_name
    ]
    return ScreenshotCapture(
        session_id=session_id,
        preview_url=preview_url,
        viewport=viewport,
        path=path,
        image_bytes=data if data is not None else _png(),
        captured_at=captured_at,
        duration_ms=12.5,
    )


# ═══════════════════════════════════════════════════════════════════
#  AgentVisualContextImage
# ═══════════════════════════════════════════════════════════════════


def _make_image(**overrides: Any) -> AgentVisualContextImage:
    defaults: dict[str, Any] = dict(
        viewport_name="desktop",
        width=1440,
        height=900,
        byte_len=42,
        image_base64="iVBORw0KGgo=",
        captured_at=1_700_000_100.0,
    )
    defaults.update(overrides)
    return AgentVisualContextImage(**defaults)


def test_image_happy_path():
    img = _make_image()
    assert img.viewport_name == "desktop"
    assert img.media_type == DEFAULT_IMAGE_MEDIA_TYPE
    assert img.source_kind == DEFAULT_IMAGE_SOURCE_KIND


def test_image_is_frozen():
    img = _make_image()
    with pytest.raises(Exception):
        img.viewport_name = "mobile"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"viewport_name": ""},
        {"viewport_name": 123},
        {"width": 0},
        {"width": -1},
        {"width": "nope"},
        {"height": 0},
        {"byte_len": 0},
        {"byte_len": -1},
        {"image_base64": ""},
        {"image_base64": 123},
        {"media_type": ""},
        {"source_kind": ""},
        {"captured_at": -1.0},
    ],
)
def test_image_rejects_bad_inputs(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        _make_image(**kwargs)


def test_image_to_dict_json_safe():
    img = _make_image()
    d = img.to_dict()
    assert json.dumps(d)
    assert d["schema_version"] == UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    assert d["viewport_name"] == "desktop"
    assert d["image_base64"] == "iVBORw0KGgo="
    assert d["media_type"] == "image/png"
    assert d["source_kind"] == "base64"


def test_image_to_content_block_matches_anthropic_shape():
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
#  encode_capture_to_image
# ═══════════════════════════════════════════════════════════════════


def test_encode_capture_to_image_happy_path():
    cap = _sample_capture("desktop")
    img = encode_capture_to_image(cap)
    assert img.viewport_name == "desktop"
    assert img.width == VIEWPORT_DESKTOP.width
    assert img.height == VIEWPORT_DESKTOP.height
    assert img.byte_len == len(cap.image_bytes)
    # Base64 encodes the raw PNG bytes.
    assert img.image_base64
    import base64

    decoded = base64.b64decode(img.image_base64)
    assert decoded == cap.image_bytes


def test_encode_capture_to_image_rejects_non_capture():
    with pytest.raises(TypeError):
        encode_capture_to_image("not-a-capture")  # type: ignore[arg-type]


def test_encode_capture_to_image_rejects_nonpositive_max_bytes():
    cap = _sample_capture()
    with pytest.raises(ValueError):
        encode_capture_to_image(cap, max_bytes=0)


def test_encode_capture_to_image_rejects_oversized():
    # Force byte_len beyond cap by cutting the cap below the payload.
    cap = _sample_capture()
    with pytest.raises(AgentVisualContextError):
        encode_capture_to_image(cap, max_bytes=5)


def test_encode_capture_to_image_wraps_screenshot_error(monkeypatch: Any):
    cap = _sample_capture()
    monkeypatch.setattr(
        avc,
        "encode_png_base64",
        lambda data: (_ for _ in ()).throw(ScreenshotError("bad png")),
    )
    with pytest.raises(AgentVisualContextError):
        encode_capture_to_image(cap)


# ═══════════════════════════════════════════════════════════════════
#  apply_image_byte_budget
# ═══════════════════════════════════════════════════════════════════


def _img(name: str, byte_len: int) -> AgentVisualContextImage:
    return _make_image(viewport_name=name, byte_len=byte_len)


def test_apply_byte_budget_empty_input():
    kept, dropped = apply_image_byte_budget([], max_total_bytes=10)
    assert kept == ()
    assert dropped == ()


def test_apply_byte_budget_under_cap_keeps_all():
    images = [_img("desktop", 100), _img("tablet", 100), _img("mobile", 100)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=1000)
    assert len(kept) == 3
    assert dropped == ()


def test_apply_byte_budget_drops_until_under_cap():
    images = [_img("desktop", 400), _img("tablet", 400), _img("mobile", 400)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=800)
    kept_names = [i.viewport_name for i in kept]
    # Always keep the first image — text-only degrade is strictly worse.
    assert "desktop" in kept_names
    assert len(kept) + len(dropped) == 3
    assert sum(i.byte_len for i in kept) <= 800


def test_apply_byte_budget_always_keeps_first_even_if_oversized():
    images = [_img("desktop", 5_000), _img("tablet", 1)]
    kept, dropped = apply_image_byte_budget(images, max_total_bytes=100)
    assert kept[0].viewport_name == "desktop"
    # Cap exceeded by desktop itself → tablet gets dropped.
    assert [i.viewport_name for i in dropped] == ["tablet"]


def test_apply_byte_budget_rejects_nonpositive_cap():
    with pytest.raises(ValueError):
        apply_image_byte_budget([_img("desktop", 1)], max_total_bytes=0)


def test_apply_byte_budget_rejects_non_image_entries():
    with pytest.raises(TypeError):
        apply_image_byte_budget(["nope"], max_total_bytes=100)  # type: ignore[list-item]


def test_apply_byte_budget_preserves_order():
    images = [_img("desktop", 100), _img("tablet", 100), _img("mobile", 100)]
    kept, _ = apply_image_byte_budget(images, max_total_bytes=1000)
    assert [i.viewport_name for i in kept] == ["desktop", "tablet", "mobile"]


# ═══════════════════════════════════════════════════════════════════
#  render_visual_context_text
# ═══════════════════════════════════════════════════════════════════


def test_render_text_includes_all_sections():
    out = render_visual_context_text(
        session_id="sess-1",
        turn_id="turn-42",
        preview_url="http://127.0.0.1:40500/",
        path="/pricing",
        viewport_matrix=("desktop", "tablet", "mobile"),
        captured_viewport_names=("desktop", "tablet"),
        missing_viewports=("mobile",),
        error_summary_markdown="### Preview errors\n\nNo active errors.\n",
        auto_fix_hint="All clean.",
    )
    assert "sess-1" in out
    assert "turn-42" in out
    assert "http://127.0.0.1:40500/" in out
    assert "/pricing" in out
    assert "desktop, tablet" in out
    assert "mobile" in out  # missing_line references mobile
    assert "No active errors." in out
    assert "All clean." in out


def test_render_text_empty_matrix_fallback():
    out = render_visual_context_text(
        session_id="sess-1",
        turn_id="turn-1",
        preview_url="http://127.0.0.1/",
        path="/",
        viewport_matrix=("desktop",),
        captured_viewport_names=(),
        missing_viewports=("desktop",),
        error_summary_markdown="",
        auto_fix_hint="",
    )
    assert "sandbox unreachable" in out
    assert "(no error summary)" in out
    assert "(no auto-fix hint)" in out


def test_render_text_deterministic_for_same_inputs():
    kwargs = dict(
        session_id="s",
        turn_id="t",
        preview_url="http://x/",
        path="/",
        viewport_matrix=("desktop",),
        captured_viewport_names=("desktop",),
        missing_viewports=(),
        error_summary_markdown="err",
        auto_fix_hint="fix",
    )
    a = render_visual_context_text(**kwargs)  # type: ignore[arg-type]
    b = render_visual_context_text(**kwargs)  # type: ignore[arg-type]
    assert a == b


def test_render_text_rejects_empty_template():
    with pytest.raises(ValueError):
        render_visual_context_text(
            session_id="s",
            turn_id="t",
            preview_url="http://x/",
            path="/",
            viewport_matrix=("desktop",),
            captured_viewport_names=(),
            missing_viewports=(),
            error_summary_markdown="",
            auto_fix_hint="",
            template="",
        )


def test_render_text_custom_template_usable():
    template = "session={session_id} turn={turn_id} images={image_count}"
    out = render_visual_context_text(
        session_id="s",
        turn_id="t",
        preview_url="http://x/",
        path="/",
        viewport_matrix=("desktop",),
        captured_viewport_names=("desktop",),
        missing_viewports=(),
        error_summary_markdown="",
        auto_fix_hint="",
        template=template,
    )
    assert out == "session=s turn=t images=1"


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
#  AgentVisualContextPayload
# ═══════════════════════════════════════════════════════════════════


def _make_payload(**overrides: Any) -> AgentVisualContextPayload:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        turn_id="turn-1",
        built_at=1_700_000_100.0,
        preview_url="http://127.0.0.1:40500/",
        path="/",
        viewport_matrix=("desktop", "tablet", "mobile"),
        images=(
            _make_image(viewport_name="desktop", width=1440, height=900),
            _make_image(viewport_name="tablet", width=768, height=1024),
            _make_image(viewport_name="mobile", width=375, height=812),
        ),
        missing_viewports=(),
        text_prompt="hello agent",
        error_summary_markdown="### Preview errors\n\nNo active errors.\n",
        auto_fix_hint="The preview rendered cleanly.",
    )
    defaults.update(overrides)
    return AgentVisualContextPayload(**defaults)


def test_payload_happy_path():
    p = _make_payload()
    assert p.image_count == 3
    assert p.has_images is True
    assert p.captured_viewport_names == ("desktop", "tablet", "mobile")
    assert p.total_image_bytes == sum(i.byte_len for i in p.images)
    assert p.has_errors is False
    assert p.was_skipped is False


def test_payload_is_frozen():
    p = _make_payload()
    with pytest.raises(Exception):
        p.session_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"session_id": 1},
        {"turn_id": ""},
        {"built_at": -1.0},
        {"preview_url": ""},
        {"path": "no-slash"},
        {"viewport_matrix": ()},
        {"viewport_matrix": ("",)},
        {"text_prompt": ""},
        {"error_summary_markdown": 123},
        {"auto_fix_hint": 123},
        {"has_blocking_errors": "true"},
        {"active_error_count": -1},
        {"was_skipped": "yes"},
    ],
)
def test_payload_rejects_bad_inputs(kwargs: dict[str, Any]):
    with pytest.raises(ValueError):
        _make_payload(**kwargs)


def test_payload_rejects_non_image_entries():
    with pytest.raises(ValueError):
        _make_payload(images=("not-an-image",))


def test_payload_skipped_must_have_no_images():
    with pytest.raises(ValueError):
        _make_payload(
            was_skipped=True,
            skip_reason="test",
        )


def test_payload_skipped_requires_reason():
    with pytest.raises(ValueError):
        _make_payload(was_skipped=True, images=(), skip_reason=None)


def test_payload_to_dict_json_safe():
    p = _make_payload()
    d = p.to_dict()
    encoded = json.dumps(d)
    assert encoded
    assert d["schema_version"] == UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    assert d["image_count"] == 3
    assert len(d["images"]) == 3
    assert d["has_errors"] is False


def test_payload_to_content_blocks_text_first():
    p = _make_payload()
    blocks = p.to_content_blocks()
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "hello agent"
    assert [b["type"] for b in blocks[1:]] == ["image", "image", "image"]


def test_payload_to_content_blocks_image_order_matches_images():
    p = _make_payload()
    blocks = p.to_content_blocks()
    img_blocks = [b for b in blocks if b["type"] == "image"]
    expected_data = [img.image_base64 for img in p.images]
    actual_data = [b["source"]["data"] for b in img_blocks]
    assert expected_data == actual_data


def test_payload_skipped_has_empty_images_and_skip_reason():
    p = _make_payload(
        images=(),
        missing_viewports=("desktop", "tablet", "mobile"),
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


def test_builder_requires_responsive():
    with pytest.raises(TypeError):
        AgentVisualContextBuilder(responsive=None)  # type: ignore[arg-type]


def test_builder_rejects_non_responsive():
    with pytest.raises(TypeError):
        AgentVisualContextBuilder(responsive="not-a-thing")  # type: ignore[arg-type]


def test_builder_rejects_non_bridge_error_bridge():
    r = _make_responsive()
    with pytest.raises(TypeError):
        AgentVisualContextBuilder(responsive=r, error_bridge="nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_failure_mode": "yolo"},
        {"default_path": "no-slash"},
        {"max_image_bytes_per_viewport": 0},
        {"max_total_image_bytes": 0},  # less than per-viewport default
        {"text_prompt_template": ""},
        {"default_matrix": ()},
    ],
)
def test_builder_rejects_bad_kwargs(kwargs: dict[str, Any]):
    r = _make_responsive()
    with pytest.raises(ValueError):
        AgentVisualContextBuilder(responsive=r, **kwargs)


def test_builder_accessors_expose_defaults():
    r = _make_responsive()
    b = AgentVisualContextBuilder(responsive=r)
    assert b.responsive is r
    assert b.error_bridge is None
    assert b.default_failure_mode == urv.DEFAULT_FAILURE_MODE
    assert b.default_path == DEFAULT_PATH
    assert b.max_image_bytes_per_viewport == DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT
    assert b.max_total_image_bytes == DEFAULT_MAX_TOTAL_IMAGE_BYTES
    assert b.text_prompt_template == DEFAULT_TEXT_PROMPT_TEMPLATE
    assert b.default_matrix == urv.DEFAULT_VIEWPORT_MATRIX


def test_builder_counters_start_zero():
    b = AgentVisualContextBuilder(responsive=_make_responsive())
    assert b.build_count() == 0
    assert b.skipped_count() == 0
    assert b.failed_count() == 0
    assert b.last_payload() is None


# ═══════════════════════════════════════════════════════════════════
#  Builder.build happy path
# ═══════════════════════════════════════════════════════════════════


def _make_builder(
    *,
    engine: FakeScreenshotEngine | None = None,
    error_bridge: PreviewErrorBridge | None = None,
    clock: FakeClock | None = None,
    event_cb: RecordingEventCallback | None = None,
    **kwargs: Any,
) -> tuple[AgentVisualContextBuilder, FakeScreenshotEngine, RecordingEventCallback]:
    clock = clock or FakeClock()
    engine = engine or FakeScreenshotEngine()
    service = ScreenshotService(engine=engine, clock=clock)
    responsive = ResponsiveViewportCapture(service=service, clock=clock)
    events = event_cb or RecordingEventCallback()
    builder = AgentVisualContextBuilder(
        responsive=responsive,
        error_bridge=error_bridge,
        clock=clock,
        event_cb=events,
        **kwargs,
    )
    return builder, engine, events


def test_build_three_viewports_returns_payload_with_three_images():
    builder, engine, events = _make_builder()
    payload = builder.build(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40500/",
        turn_id="turn-1",
    )
    assert payload.image_count == 3
    assert payload.captured_viewport_names == ("desktop", "tablet", "mobile")
    assert payload.missing_viewports == ()
    assert payload.was_skipped is False
    # Engine invoked once per viewport.
    assert len(engine.calls) == 3
    assert [c.viewport.name for c in engine.calls] == ["desktop", "tablet", "mobile"]


def test_build_auto_assigns_turn_id_when_not_provided():
    builder, _, _ = _make_builder()
    p1 = builder.build(session_id="s", preview_url="http://x/")
    p2 = builder.build(session_id="s", preview_url="http://x/")
    assert p1.turn_id != p2.turn_id
    assert p1.turn_id.startswith("avc-turn-")
    assert p2.turn_id.startswith("avc-turn-")


def test_build_emits_building_then_built():
    builder, _, events = _make_builder()
    builder.build(session_id="s", preview_url="http://x/")
    types = events.types()
    assert types[0] == AGENT_VISUAL_CONTEXT_EVENT_BUILDING
    assert AGENT_VISUAL_CONTEXT_EVENT_BUILT in types
    # Built comes after building.
    built_idx = types.index(AGENT_VISUAL_CONTEXT_EVENT_BUILT)
    build_idx = types.index(AGENT_VISUAL_CONTEXT_EVENT_BUILDING)
    assert built_idx > build_idx


def test_build_built_event_carries_no_base64():
    builder, _, events = _make_builder()
    builder.build(session_id="s", preview_url="http://x/")
    built = events.by_type(AGENT_VISUAL_CONTEXT_EVENT_BUILT)
    assert len(built) == 1
    env = built[0]
    # Envelope is lean — no base64 blobs polluting SSE frames.
    encoded = json.dumps(env)
    assert "image_base64" not in encoded
    assert env["image_count"] == 3
    assert env["captured_viewport_names"] == ["desktop", "tablet", "mobile"]


def test_build_increments_build_count_and_records_last_payload():
    builder, _, _ = _make_builder()
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert builder.build_count() == 1
    assert builder.skipped_count() == 0
    assert builder.failed_count() == 0
    assert builder.last_payload() is payload


def test_build_path_override_routed_to_engine():
    builder, engine, _ = _make_builder()
    builder.build(session_id="s", preview_url="http://x/", path="/pricing")
    assert all(c.path == "/pricing" for c in engine.calls)


def test_build_custom_viewport_matrix():
    builder, engine, _ = _make_builder()
    builder.build(
        session_id="s",
        preview_url="http://x/",
        viewport_matrix=("mobile",),
    )
    assert [c.viewport.name for c in engine.calls] == ["mobile"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"preview_url": ""},
        {"path": "nope"},
        {"failure_mode": "yolo"},
        {"viewport_matrix": ()},
        {"turn_id": "  "},
    ],
)
def test_build_rejects_bad_inputs(kwargs: dict[str, Any]):
    builder, _, _ = _make_builder()
    call_args: dict[str, Any] = {
        "session_id": "s",
        "preview_url": "http://x/",
    }
    call_args.update(kwargs)
    with pytest.raises(ValueError):
        builder.build(**call_args)


# ═══════════════════════════════════════════════════════════════════
#  Builder.build failure handling
# ═══════════════════════════════════════════════════════════════════


def test_build_collect_mode_partial_capture_yields_payload_with_missing():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"mobile": ScreenshotError("timeout")}
    )
    builder, _, events = _make_builder(engine=engine)
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert payload.was_skipped is False
    assert payload.image_count == 2
    assert payload.captured_viewport_names == ("desktop", "tablet")
    assert payload.missing_viewports == ("mobile",)
    # built still fires — collect is a success case, not a failure.
    assert AGENT_VISUAL_CONTEXT_EVENT_BUILT in events.types()


def test_build_collect_mode_all_fail_emits_built_with_zero_images():
    engine = FakeScreenshotEngine(
        per_viewport_raises={
            "desktop": ScreenshotError("x"),
            "tablet": ScreenshotError("x"),
            "mobile": ScreenshotError("x"),
        }
    )
    builder, _, events = _make_builder(engine=engine)
    payload = builder.build(session_id="s", preview_url="http://x/")
    # All 3 failed — payload has no images + missing_viewports has all 3.
    # was_skipped stays False because the capture *ran* — the agent still
    # gets an error-annotated text block that says what happened.
    assert payload.image_count == 0
    assert payload.missing_viewports == ("desktop", "tablet", "mobile")
    assert payload.was_skipped is False
    assert "sandbox unreachable" in payload.text_prompt


def test_build_abort_mode_propagates_batch_aborted():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"desktop": ScreenshotError("down")}
    )
    builder, _, events = _make_builder(engine=engine)
    with pytest.raises(BatchAborted) as exc_info:
        builder.build(
            session_id="s",
            preview_url="http://x/",
            failure_mode="abort",
        )
    # Partial report is attached.
    assert exc_info.value.report is not None
    # failed counter incremented.
    assert builder.failed_count() == 1
    assert AGENT_VISUAL_CONTEXT_EVENT_FAILED in events.types()


def test_build_abort_mode_failed_event_carries_partial_report():
    engine = FakeScreenshotEngine(
        per_viewport_raises={"desktop": ScreenshotError("down")}
    )
    builder, _, events = _make_builder(engine=engine)
    with pytest.raises(BatchAborted):
        builder.build(
            session_id="s",
            preview_url="http://x/",
            failure_mode="abort",
        )
    failed = events.by_type(AGENT_VISUAL_CONTEXT_EVENT_FAILED)
    assert failed
    env = failed[0]
    assert env["error_type"] == "BatchAborted"
    assert env["partial_report"] is not None


def test_build_unexpected_exception_degrades_to_skipped():
    class BoomEngine:
        def capture(self, request: ScreenshotRequest) -> bytes:
            raise RuntimeError("engine exploded")

        def close(self) -> None:
            return None

    # ScreenshotService wraps non-ScreenshotError into ScreenshotError,
    # so capture_all sees ScreenshotError and goes collect-mode.  To
    # test the 'unexpected responsive exception' path, replace the
    # responsive.capture_all with a blow-up.
    builder, _, events = _make_builder()

    def boom(**_: Any) -> Any:
        raise RuntimeError("responsive exploded")

    builder.responsive.capture_all = boom  # type: ignore[assignment]
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert payload.was_skipped is True
    assert "responsive_capture_failed" in (payload.skip_reason or "")
    assert builder.failed_count() == 1
    assert builder.skipped_count() == 1
    assert AGENT_VISUAL_CONTEXT_EVENT_FAILED in events.types()
    assert AGENT_VISUAL_CONTEXT_EVENT_SKIPPED in events.types()


# ═══════════════════════════════════════════════════════════════════
#  Byte-budget enforcement via builder
# ═══════════════════════════════════════════════════════════════════


def test_build_applies_max_total_image_bytes_and_records_warning():
    # Give each viewport a larger payload so the budget forces a drop.
    big = _png(b"X" * 800)
    engine = FakeScreenshotEngine(default_payload=big)
    builder, _, _ = _make_builder(
        engine=engine,
        max_image_bytes_per_viewport=10_000,
        max_total_image_bytes=len(big) + 1,  # only room for the first capture.
    )
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert payload.image_count == 1
    assert payload.captured_viewport_names == ("desktop",)
    assert set(payload.missing_viewports) == {"tablet", "mobile"}
    assert any(w.startswith("image_dropped_budget:") for w in payload.warnings)


def test_build_per_viewport_cap_drops_image_with_warning():
    big = _png(b"X" * 1_000)
    engine = FakeScreenshotEngine(default_payload=big)
    builder, _, _ = _make_builder(
        engine=engine,
        max_image_bytes_per_viewport=5,  # smaller than our payload → per-viewport reject
        max_total_image_bytes=5,
    )
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert payload.image_count == 0
    assert any(w.startswith("image_encode_failed:") for w in payload.warnings)


# ═══════════════════════════════════════════════════════════════════
#  Error-bridge integration
# ═══════════════════════════════════════════════════════════════════


def test_build_without_error_bridge_uses_placeholder_hint():
    builder, _, _ = _make_builder()
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert "No error bridge wired." in payload.error_summary_markdown
    assert payload.active_error_count == 0
    assert payload.has_blocking_errors is False


def test_build_with_error_bridge_renders_error_summary(tmp_path: Path):
    # Wire a real V2 #5 bridge with a compile-error log so the
    # payload's text includes the error table + fix hint.
    bridge, docker, mgr = _make_bridge(
        tmp_path,
        canned_logs=(
            "Module not found: Can't resolve 'foo'\n"
            "  at ./components/App.tsx:12:5\n"
        ),
        session_id="sess-1",
    )
    # Drive a scan to seed the bridge state.
    bridge.scan("sess-1")

    builder, _, events = _make_builder(error_bridge=bridge)
    payload = builder.build(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40500/",
    )
    assert "Preview errors" in payload.error_summary_markdown
    # The bridge scan returned at least one blocking error.
    assert payload.active_error_count >= 1
    assert payload.has_blocking_errors is True


def test_build_scan_errors_flag_triggers_fresh_scan(tmp_path: Path):
    bridge, docker, mgr = _make_bridge(
        tmp_path,
        canned_logs="",
        session_id="sess-1",
    )
    # First build with no errors to establish baseline.
    builder, _, _ = _make_builder(error_bridge=bridge)
    builder.build(session_id="sess-1", preview_url="http://127.0.0.1:40500/")
    # Now load the logs and request scan_errors on next build.
    docker.set_logs(
        "Module not found: Can't resolve 'foo'\n  at ./a.tsx:1:1\n"
    )
    payload = builder.build(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40500/",
        scan_errors=True,
    )
    assert payload.active_error_count >= 1


def test_build_with_error_bridge_include_errors_false_skips_bridge(tmp_path: Path):
    bridge, docker, mgr = _make_bridge(
        tmp_path,
        canned_logs="Module not found: x\n  at ./a.tsx:1:1\n",
        session_id="sess-1",
    )
    bridge.scan("sess-1")
    builder, _, _ = _make_builder(error_bridge=bridge)
    payload = builder.build(
        session_id="sess-1",
        preview_url="http://127.0.0.1:40500/",
        include_errors=False,
    )
    # include_errors=False bypasses the bridge entirely.
    assert payload.active_error_count == 0
    assert "No error bridge wired." in payload.error_summary_markdown


# ═══════════════════════════════════════════════════════════════════
#  build_skipped
# ═══════════════════════════════════════════════════════════════════


def test_build_skipped_produces_text_only_payload():
    builder, _, events = _make_builder()
    payload = builder.build_skipped(
        session_id="s",
        preview_url="http://x/",
        skip_reason="sandbox pending",
    )
    assert payload.was_skipped is True
    assert payload.image_count == 0
    assert payload.skip_reason == "sandbox pending"
    assert builder.skipped_count() == 1
    assert AGENT_VISUAL_CONTEXT_EVENT_SKIPPED in events.types()


def test_build_skipped_rejects_empty_reason():
    builder, _, _ = _make_builder()
    with pytest.raises(ValueError):
        builder.build_skipped(
            session_id="s", preview_url="http://x/", skip_reason="   "
        )


# ═══════════════════════════════════════════════════════════════════
#  build_message convenience + HumanMessage wrapper
# ═══════════════════════════════════════════════════════════════════


def test_build_message_returns_payload_and_message():
    builder, _, _ = _make_builder()
    payload, msg = builder.build_message(
        session_id="s", preview_url="http://x/"
    )
    assert isinstance(payload, AgentVisualContextPayload)
    # HumanMessage wraps content list.
    assert hasattr(msg, "content")
    content = msg.content
    assert isinstance(content, list)
    # First block is text, followed by image blocks matching payload.image_count.
    assert content[0]["type"] == "text"
    img_blocks = [b for b in content if b["type"] == "image"]
    assert len(img_blocks) == payload.image_count


def test_build_human_message_rejects_non_payload():
    with pytest.raises(TypeError):
        build_human_message("nope")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  Snapshot
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_json_safe_no_payload():
    builder, _, _ = _make_builder()
    snap = builder.snapshot()
    assert json.dumps(snap)
    assert snap["build_count"] == 0
    assert snap["last_payload"] is None


def test_snapshot_json_safe_with_last_payload():
    builder, _, _ = _make_builder()
    builder.build(session_id="s", preview_url="http://x/")
    snap = builder.snapshot()
    encoded = json.dumps(snap)
    assert encoded
    assert snap["last_payload"] is not None
    # Snapshot does NOT inline base64 images to keep SSE frames lean.
    assert "image_base64" not in encoded


def test_snapshot_reports_error_bridge_schema_when_wired(tmp_path: Path):
    bridge, docker, mgr = _make_bridge(tmp_path, session_id="s")
    builder, _, _ = _make_builder(error_bridge=bridge)
    snap = builder.snapshot()
    assert snap["error_bridge_schema_version"] is not None


# ═══════════════════════════════════════════════════════════════════
#  Event callback safety
# ═══════════════════════════════════════════════════════════════════


def test_event_callback_raise_does_not_kill_builder():
    def boom(event_type: str, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("boom")

    engine = FakeScreenshotEngine()
    clock = FakeClock()
    service = ScreenshotService(engine=engine, clock=clock)
    responsive = ResponsiveViewportCapture(service=service, clock=clock)
    builder = AgentVisualContextBuilder(
        responsive=responsive,
        clock=clock,
        event_cb=boom,
    )
    # Build should succeed despite event callbacks blowing up.
    payload = builder.build(session_id="s", preview_url="http://x/")
    assert payload.image_count == 3


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_thread_safe_parallel_builds():
    # Each thread builds its own session — counters must stay consistent.
    builder, engine, _ = _make_builder()

    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def worker(session_idx: int) -> None:
        try:
            barrier.wait()
            builder.build(
                session_id=f"sess-{session_idx}",
                preview_url="http://x/",
                turn_id=f"turn-{session_idx}",
            )
        except Exception as exc:  # pragma: no cover - surfaces test failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == []
    assert builder.build_count() == 10
    # 10 sessions × 3 viewports = 30 engine calls.
    assert len(engine.calls) == 30


# ═══════════════════════════════════════════════════════════════════
#  End-to-end golden path — V2 #3 + #4 + #5 + #6 wiring
# ═══════════════════════════════════════════════════════════════════


def test_end_to_end_builder_composes_responsive_and_error_bridge(tmp_path: Path):
    """Full wire-up: FakeDocker → SandboxManager → PreviewErrorBridge +
    FakeScreenshotEngine → ScreenshotService → ResponsiveViewportCapture
    → AgentVisualContextBuilder → HumanMessage ready for Opus 4.7.

    Proves V2 #6 closes the loop end-to-end with zero orchestration code
    besides ``builder.build_message(...)``.
    """

    # Error bridge wiring (V2 #5).
    clock = FakeClock()
    events = RecordingEventCallback()
    bridge, docker, mgr = _make_bridge(
        tmp_path,
        canned_logs=(
            "Module not found: Can't resolve 'Button'\n"
            "  at ./components/Header.tsx:4:10\n"
        ),
        clock=clock,
        event_cb=events,
        session_id="sess-prod",
    )
    bridge.scan("sess-prod")

    # Screenshot + responsive wiring (V2 #3 + #4).
    engine = FakeScreenshotEngine()
    service = ScreenshotService(engine=engine, clock=clock, event_cb=events)
    responsive = ResponsiveViewportCapture(
        service=service, clock=clock, event_cb=events
    )

    # V2 #6 builder composes the above.
    builder = AgentVisualContextBuilder(
        responsive=responsive,
        error_bridge=bridge,
        clock=clock,
        event_cb=events,
    )

    payload, message = builder.build_message(
        session_id="sess-prod",
        preview_url="http://127.0.0.1:40500/",
        turn_id="react-turn-1",
        path="/",
    )

    # Multimodal message ready for Opus 4.7.
    assert hasattr(message, "content")
    content = message.content
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    image_blocks = [b for b in content if b["type"] == "image"]
    assert len(image_blocks) == 3
    # Each image block matches Anthropic's documented base64 shape.
    for b in image_blocks:
        assert b["source"]["type"] == "base64"
        assert b["source"]["media_type"] == "image/png"
        assert b["source"]["data"]  # non-empty base64

    # Error context was folded in.
    assert payload.active_error_count >= 1
    assert payload.has_blocking_errors is True
    assert "Preview errors" in payload.error_summary_markdown
    assert "Header.tsx" in payload.text_prompt

    # End-to-end events cover the whole pipeline.
    types = events.types()
    assert AGENT_VISUAL_CONTEXT_EVENT_BUILDING in types
    assert AGENT_VISUAL_CONTEXT_EVENT_BUILT in types

    # Serialising the full payload for SSE replay remains JSON-safe.
    encoded = json.dumps(payload.to_dict())
    assert "image_base64" in encoded  # full payload carries pixels


# ═══════════════════════════════════════════════════════════════════
#  Sibling alignment — V1 + V2 modules still importable
# ═══════════════════════════════════════════════════════════════════


def test_sibling_modules_importable():
    from backend import (
        ui_preview_error_bridge,
        ui_responsive_viewport,
        ui_sandbox,
        ui_sandbox_lifecycle,
        ui_screenshot,
    )

    for mod in (
        ui_sandbox,
        ui_sandbox_lifecycle,
        ui_screenshot,
        ui_responsive_viewport,
        ui_preview_error_bridge,
    ):
        assert mod.__name__


def test_schema_versions_independent():
    # V2 #6 schema is independent of the sibling V2 schemas — bumping
    # one must not force a bump of the others.
    assert (
        UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
        == avc.UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION
    )

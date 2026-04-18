"""V7 row 1732 (#323 first bullet) — mobile_annotation_context contract tests.

Pins ``backend/mobile_annotation_context.py`` against the V7 row spec:

  * every mobile operator annotation from the frontend
    (``mobile-visual-annotator.tsx``) is parsed and validated with the
    exact payload shape the frontend emits;
  * :class:`MobileAnnotationContextBuilder` produces one
    :class:`MobileAnnotationBundle` per turn with a ready-to-inject
    text prompt that routes the agent to SwiftUI / Compose / Flutter /
    RN vocabulary;
  * events fire in the ``ui_sandbox.mobile_annotation_context.*``
    namespace with zero overlap with the web ``ui_annotation_context``
    topics;
  * malformed wire input produces :class:`MobileAnnotationContextError`
    (parseable by FastAPI's default ``ValueError`` → 422 handler);
  * empty annotation lists are legitimate and produce an ``empty``
    event rather than raising.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

import pytest

from backend import mobile_annotation_context as mac
from backend.mobile_annotation_context import (
    ANNOTATION_TYPE_CLICK,
    ANNOTATION_TYPE_RECT,
    ANNOTATION_TYPES,
    DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE,
    FRAMEWORK_TO_FILE_EXT,
    MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION,
    MOBILE_ANNOTATION_EVENT_BUILDING,
    MOBILE_ANNOTATION_EVENT_BUILT,
    MOBILE_ANNOTATION_EVENT_EMPTY,
    MOBILE_ANNOTATION_EVENT_TYPES,
    MOBILE_FRAMEWORKS,
    MOBILE_PLATFORMS,
    PLATFORM_TO_FRAMEWORK,
    MobileAnnotationBundle,
    MobileAnnotationContextBuilder,
    MobileAnnotationContextError,
    MobileAnnotationPayload,
    build_mobile_text_content_block,
    mobile_annotation_from_dict,
    mobile_annotations_from_list,
    render_mobile_annotation_entry,
    render_mobile_annotations_markdown,
    resolve_file_ext,
    resolve_framework,
)


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


class EventRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.append((event_type, dict(data)))

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._events)


def make_raw(
    *,
    kind: str = ANNOTATION_TYPE_RECT,
    platform: str = "ios",
    device: str = "iphone-15",
    screen_width: int = 1179,
    screen_height: int = 2556,
    box_x: float = 0.1,
    box_y: float = 0.2,
    box_w: float = 0.3,
    box_h: float = 0.4,
    component_hint: str | None = "sendButton",
    comment: str = "Tighten spacing",
) -> dict[str, Any]:
    framework = PLATFORM_TO_FRAMEWORK[platform]
    file_ext = FRAMEWORK_TO_FILE_EXT[framework]
    if kind == ANNOTATION_TYPE_CLICK:
        box_w = 0.0
        box_h = 0.0
    native_x = round(box_x * screen_width)
    native_y = round(box_y * screen_height)
    native_w = round(box_w * screen_width)
    native_h = round(box_h * screen_height)
    # clamp
    native_w = min(native_w, screen_width - native_x)
    native_h = min(native_h, screen_height - native_y)
    return {
        "type": kind,
        "platform": platform,
        "framework": framework,
        "fileExt": file_ext,
        "device": device,
        "screenWidth": screen_width,
        "screenHeight": screen_height,
        "boundingBox": {"x": box_x, "y": box_y, "w": box_w, "h": box_h},
        "nativePixelBox": {
            "x": native_x,
            "y": native_y,
            "w": native_w,
            "h": native_h,
        },
        "componentHint": component_hint,
        "comment": comment,
    }


# ═══════════════════════════════════════════════════════════════════
#  Constants + resolvers
# ═══════════════════════════════════════════════════════════════════


class TestConstants:
    def test_schema_version_is_semver(self):
        parts = MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        for p in parts:
            assert p.isdigit()

    def test_platforms_and_frameworks(self):
        assert MOBILE_PLATFORMS == ("ios", "android", "flutter", "react-native")
        assert MOBILE_FRAMEWORKS == (
            "swiftui",
            "jetpack-compose",
            "flutter",
            "react-native",
        )

    def test_platform_mapping_covers_every_platform(self):
        for p in MOBILE_PLATFORMS:
            assert p in PLATFORM_TO_FRAMEWORK

    def test_framework_file_ext_is_distinct(self):
        exts = list(FRAMEWORK_TO_FILE_EXT.values())
        assert len(set(exts)) == len(exts)

    def test_annotation_types(self):
        assert ANNOTATION_TYPES == (ANNOTATION_TYPE_CLICK, ANNOTATION_TYPE_RECT)

    def test_events_are_disjoint_from_web_channel(self):
        from backend.ui_annotation_context import ANNOTATION_CONTEXT_EVENT_TYPES

        web = set(ANNOTATION_CONTEXT_EVENT_TYPES)
        mobile = set(MOBILE_ANNOTATION_EVENT_TYPES)
        assert web.isdisjoint(mobile)


class TestResolvers:
    def test_resolve_framework(self):
        assert resolve_framework("ios") == "swiftui"
        assert resolve_framework("android") == "jetpack-compose"
        assert resolve_framework("flutter") == "flutter"
        assert resolve_framework("react-native") == "react-native"

    def test_resolve_framework_rejects_unknown(self):
        with pytest.raises(MobileAnnotationContextError, match="platform must be"):
            resolve_framework("windows-phone")

    def test_resolve_file_ext(self):
        assert resolve_file_ext("swiftui") == ".swift"
        assert resolve_file_ext("jetpack-compose") == ".kt"
        assert resolve_file_ext("flutter") == ".dart"
        assert resolve_file_ext("react-native") == ".tsx"

    def test_resolve_file_ext_rejects_unknown(self):
        with pytest.raises(MobileAnnotationContextError, match="framework must be"):
            resolve_file_ext("qt")


# ═══════════════════════════════════════════════════════════════════
#  Parsing
# ═══════════════════════════════════════════════════════════════════


class TestParsing:
    def test_round_trips_a_valid_rect_payload(self):
        raw = make_raw()
        p = mobile_annotation_from_dict(raw)
        assert p.type == "rect"
        assert p.platform == "ios"
        assert p.framework == "swiftui"
        assert p.file_ext == ".swift"
        assert p.device == "iphone-15"
        assert p.screen_width == 1179
        assert p.screen_height == 2556
        assert p.component_hint == "sendButton"
        assert p.comment == "Tighten spacing"
        out = p.to_dict()
        # output key ordering matches frontend wire shape
        assert list(out.keys())[:6] == [
            "type",
            "platform",
            "framework",
            "fileExt",
            "device",
            "screenWidth",
        ]

    def test_click_annotation_must_have_zero_size_box(self):
        raw = make_raw(kind=ANNOTATION_TYPE_CLICK, box_x=0.5, box_y=0.5)
        p = mobile_annotation_from_dict(raw)
        assert p.type == "click"
        assert p.box_w == 0.0
        assert p.box_h == 0.0
        assert p.native_w == 0
        assert p.native_h == 0

    def test_rect_must_not_be_zero_size(self):
        raw = make_raw()
        raw["boundingBox"]["w"] = 0.0
        raw["boundingBox"]["h"] = 0.0
        raw["nativePixelBox"]["w"] = 0
        raw["nativePixelBox"]["h"] = 0
        with pytest.raises(MobileAnnotationContextError, match="non-zero"):
            mobile_annotation_from_dict(raw)

    def test_click_must_not_have_non_zero_box(self):
        raw = make_raw(kind=ANNOTATION_TYPE_CLICK, box_x=0.5, box_y=0.5)
        raw["boundingBox"]["w"] = 0.1  # attempt to slip through
        raw["nativePixelBox"]["w"] = 100
        with pytest.raises(MobileAnnotationContextError, match="zero-size"):
            mobile_annotation_from_dict(raw)

    def test_missing_required_keys_raise(self):
        raw = make_raw()
        del raw["platform"]
        with pytest.raises(MobileAnnotationContextError, match="missing required"):
            mobile_annotation_from_dict(raw)

    def test_unknown_platform_rejected(self):
        raw = make_raw()
        raw["platform"] = "bada"
        with pytest.raises(MobileAnnotationContextError, match="platform must be"):
            mobile_annotation_from_dict(raw)

    def test_framework_must_match_platform(self):
        raw = make_raw(platform="ios")
        raw["framework"] = "jetpack-compose"  # mismatched
        with pytest.raises(MobileAnnotationContextError, match="does not match"):
            mobile_annotation_from_dict(raw)

    def test_file_ext_must_match_framework(self):
        raw = make_raw()
        raw["fileExt"] = ".kt"  # wrong for swiftui
        with pytest.raises(MobileAnnotationContextError, match="does not match"):
            mobile_annotation_from_dict(raw)

    def test_native_pixel_box_must_fit_screen(self):
        raw = make_raw()
        raw["nativePixelBox"]["x"] = 2000  # > screenWidth
        with pytest.raises(MobileAnnotationContextError, match="exceeds screenWidth"):
            mobile_annotation_from_dict(raw)

    def test_bounding_box_out_of_range(self):
        raw = make_raw()
        raw["boundingBox"]["x"] = 1.5
        with pytest.raises(MobileAnnotationContextError, match=r"\[0, 1\]"):
            mobile_annotation_from_dict(raw)

    def test_component_hint_may_be_null(self):
        raw = make_raw(component_hint=None)
        p = mobile_annotation_from_dict(raw)
        assert p.component_hint is None

    def test_component_hint_empty_string_rejected(self):
        raw = make_raw(component_hint="   ")
        with pytest.raises(MobileAnnotationContextError, match="non-empty"):
            mobile_annotation_from_dict(raw)

    def test_extra_keys_are_ignored(self):
        raw = make_raw()
        raw["futureField"] = {"anything": 1}
        p = mobile_annotation_from_dict(raw)  # must not raise
        assert p.type == "rect"

    def test_from_list_preserves_order(self):
        a = make_raw(comment="first")
        b = make_raw(comment="second", platform="android", device="pixel-8")
        payloads = mobile_annotations_from_list([a, b])
        assert len(payloads) == 2
        assert payloads[0].comment == "first"
        assert payloads[1].platform == "android"

    def test_from_list_rejects_non_sequence(self):
        with pytest.raises(MobileAnnotationContextError, match="sequence"):
            mobile_annotations_from_list("not a list")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  Rendering
# ═══════════════════════════════════════════════════════════════════


class TestRendering:
    def test_render_rect_entry_includes_pct_and_native_px(self):
        p = mobile_annotation_from_dict(make_raw())
        out = render_mobile_annotation_entry(label=1, payload=p)
        assert "1. [rect] swiftui (.swift) on iphone-15" in out
        assert "accessibilityIdentifier: `sendButton`" in out
        assert 'comment="Tighten spacing"' in out
        assert "x=10.0%" in out
        assert "y=20.0%" in out
        assert "screen 1179×2556px" in out

    def test_render_click_entry_hides_width_height(self):
        p = mobile_annotation_from_dict(
            make_raw(kind=ANNOTATION_TYPE_CLICK, box_x=0.1, box_y=0.2)
        )
        out = render_mobile_annotation_entry(label=2, payload=p)
        assert "[click]" in out
        # click must NOT print the w/h line; must print the single x=y= line + nativePixelPoint
        assert "nativePixelPoint:" in out
        assert "nativePixelBox" not in out

    def test_render_framework_specific_hint_label(self):
        # Compose → Modifier.testTag
        raw = make_raw(platform="android", device="pixel-8", component_hint="send_btn")
        p = mobile_annotation_from_dict(raw)
        out = render_mobile_annotation_entry(label=1, payload=p)
        assert "Modifier.testTag: `send_btn`" in out

        # Flutter → Widget Key
        raw = make_raw(platform="flutter", device="pixel-8", component_hint="SendButton")
        p = mobile_annotation_from_dict(raw)
        out = render_mobile_annotation_entry(label=1, payload=p)
        assert "Widget Key / class: `SendButton`" in out

        # RN → testID
        raw = make_raw(platform="react-native", device="galaxy-tab", component_hint="send")
        p = mobile_annotation_from_dict(raw)
        out = render_mobile_annotation_entry(label=1, payload=p)
        assert "testID: `send`" in out

    def test_render_without_comment_and_hint(self):
        raw = make_raw(component_hint=None, comment="")
        p = mobile_annotation_from_dict(raw)
        out = render_mobile_annotation_entry(label=1, payload=p)
        assert "(none — infer from screenshot)" in out
        assert "(no comment)" in out

    def test_render_markdown_for_empty_list(self):
        assert (
            render_mobile_annotations_markdown([])
            == "No operator annotations this turn."
        )

    def test_render_markdown_byte_stable(self):
        payloads = [
            mobile_annotation_from_dict(make_raw(comment="a")),
            mobile_annotation_from_dict(make_raw(comment="b", platform="android", device="pixel-8")),
        ]
        first = render_mobile_annotations_markdown(payloads)
        second = render_mobile_annotations_markdown(payloads)
        assert first == second
        # Labels are 1-based in input order.
        assert first.startswith("1. [rect] swiftui")
        assert "\n2. [rect] jetpack-compose" in first

    def test_label_must_be_positive_int(self):
        p = mobile_annotation_from_dict(make_raw())
        with pytest.raises(MobileAnnotationContextError):
            render_mobile_annotation_entry(label=0, payload=p)


# ═══════════════════════════════════════════════════════════════════
#  Bundle
# ═══════════════════════════════════════════════════════════════════


class TestBundle:
    def _mk(self, **kwargs: Any) -> MobileAnnotationBundle:
        payloads = kwargs.pop("payloads", ())
        return MobileAnnotationBundle(
            session_id=kwargs.pop("session_id", "sess-1"),
            turn_id=kwargs.pop("turn_id", "mob-turn-1"),
            built_at=kwargs.pop("built_at", 1_700_000_000.0),
            payloads=tuple(payloads),
            text_prompt=kwargs.pop("text_prompt", "prompt"),
            annotation_body_markdown=kwargs.pop("annotation_body_markdown", "body"),
            warnings=kwargs.pop("warnings", ()),
        )

    def test_counts_and_properties(self):
        payloads = [
            mobile_annotation_from_dict(make_raw()),
            mobile_annotation_from_dict(
                make_raw(
                    kind=ANNOTATION_TYPE_CLICK,
                    box_x=0.5,
                    box_y=0.5,
                    component_hint=None,
                    comment="",
                )
            ),
            mobile_annotation_from_dict(
                make_raw(platform="android", device="pixel-8", component_hint="btn")
            ),
        ]
        bundle = self._mk(payloads=payloads)
        assert bundle.annotation_count == 3
        assert bundle.rect_count == 2
        assert bundle.click_count == 1
        assert bundle.hint_count == 2
        assert bundle.commented_count == 2
        assert bundle.platforms == ("ios", "android")
        assert bundle.frameworks == ("swiftui", "jetpack-compose")
        assert bundle.file_exts == (".swift", ".kt")
        assert bundle.devices == ("iphone-15", "pixel-8")

    def test_to_dict_has_schema_version_and_payload_list(self):
        bundle = self._mk(payloads=[mobile_annotation_from_dict(make_raw())])
        d = bundle.to_dict()
        assert d["schema_version"] == MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION
        assert d["annotation_count"] == 1
        assert d["payloads"][0]["platform"] == "ios"

    def test_rejects_empty_session_id(self):
        with pytest.raises(MobileAnnotationContextError):
            self._mk(session_id="")

    def test_rejects_negative_built_at(self):
        with pytest.raises(MobileAnnotationContextError):
            self._mk(built_at=-1)

    def test_rejects_wrong_payload_type(self):
        with pytest.raises(MobileAnnotationContextError):
            self._mk(payloads=[{"not": "a payload"}])  # type: ignore[list-item]

    def test_build_text_content_block_shape(self):
        bundle = self._mk(payloads=[mobile_annotation_from_dict(make_raw())])
        block = build_mobile_text_content_block(bundle)
        assert block == {"type": "text", "text": "prompt"}


# ═══════════════════════════════════════════════════════════════════
#  Builder
# ═══════════════════════════════════════════════════════════════════


class TestBuilder:
    def test_happy_path_emits_building_then_built(self):
        events = EventRecorder()
        clock = iter([1000.0, 1001.0])
        builder = MobileAnnotationContextBuilder(
            session_id="sess-abc",
            on_event=events,
            clock=lambda: next(clock),
        )
        bundle = builder.build([make_raw()])
        assert bundle.session_id == "sess-abc"
        assert bundle.turn_id == "mob-turn-1"
        assert bundle.annotation_count == 1
        assert bundle.built_at == 1000.0
        # Prompt template substituted
        assert "sess-abc" in bundle.text_prompt
        assert "mob-turn-1" in bundle.text_prompt
        assert "swiftui" in bundle.text_prompt
        assert ".swift" in bundle.text_prompt
        assert "iphone-15" in bundle.text_prompt
        assert "1. [rect] swiftui" in bundle.text_prompt
        recorded = events.events()
        assert recorded[0][0] == MOBILE_ANNOTATION_EVENT_BUILDING
        assert recorded[-1][0] == MOBILE_ANNOTATION_EVENT_BUILT

    def test_empty_build_emits_empty_event(self):
        events = EventRecorder()
        builder = MobileAnnotationContextBuilder(
            session_id="sess-empty",
            on_event=events,
            clock=lambda: 2000.0,
        )
        bundle = builder.build([])
        assert bundle.annotation_count == 0
        assert bundle.text_prompt.strip() != ""
        # Event transitioned to EMPTY
        types = [ev[0] for ev in events.events()]
        assert MOBILE_ANNOTATION_EVENT_EMPTY in types
        assert MOBILE_ANNOTATION_EVENT_BUILT not in types

    def test_build_accepts_already_parsed_payloads(self):
        builder = MobileAnnotationContextBuilder(session_id="s")
        parsed = mobile_annotation_from_dict(make_raw())
        bundle = builder.build([parsed])
        assert bundle.annotation_count == 1

    def test_build_rejects_bad_entry_type(self):
        builder = MobileAnnotationContextBuilder(session_id="s")
        with pytest.raises(MobileAnnotationContextError):
            builder.build([123])  # type: ignore[list-item]

    def test_build_rejects_malformed_dict(self):
        builder = MobileAnnotationContextBuilder(session_id="s")
        bad = make_raw()
        del bad["boundingBox"]
        with pytest.raises(MobileAnnotationContextError):
            builder.build([bad])

    def test_multi_platform_emits_warning(self):
        events = EventRecorder()
        builder = MobileAnnotationContextBuilder(
            session_id="sess-multi",
            on_event=events,
        )
        bundle = builder.build(
            [
                make_raw(),
                make_raw(platform="android", device="pixel-8"),
            ]
        )
        assert len(bundle.warnings) == 1
        assert "multiple platforms" in bundle.warnings[0]
        # warning also appears in the 'built' event payload
        built = [
            ev for ev in events.events() if ev[0] == MOBILE_ANNOTATION_EVENT_BUILT
        ]
        assert built and built[-1][1]["warnings"]

    def test_turn_id_override(self):
        builder = MobileAnnotationContextBuilder(session_id="s")
        bundle = builder.build([make_raw()], turn_id="custom-42")
        assert bundle.turn_id == "custom-42"

    def test_turn_counter_increments_monotonically(self):
        builder = MobileAnnotationContextBuilder(session_id="s")
        builder.build([])
        builder.build([])
        assert builder.turn_counter == 2

    def test_template_substitution_is_deterministic(self):
        builder = MobileAnnotationContextBuilder(session_id="s", clock=lambda: 100.0)
        one = builder.build([make_raw()]).text_prompt
        two = builder.build([make_raw()], turn_id=builder.last_bundle.turn_id).text_prompt
        # Same content + same turn_id → same prompt
        assert one.replace("mob-turn-1", "TURN") == two.replace(
            builder.last_bundle.turn_id, "TURN"
        )

    def test_builder_rejects_empty_session_id(self):
        with pytest.raises(MobileAnnotationContextError):
            MobileAnnotationContextBuilder(session_id="")

    def test_event_callback_exception_does_not_leak(self, caplog):
        def boom(event_type: str, data: Mapping[str, Any]) -> None:
            raise RuntimeError("callback blew up")

        builder = MobileAnnotationContextBuilder(session_id="s", on_event=boom)
        # Should not raise even though the callback always throws.
        with caplog.at_level("ERROR"):
            builder.build([])

    def test_default_template_is_referenced_as_default(self):
        assert (
            "{annotation_body}"
            in DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE
        )
        assert "{session_id}" in DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE
        assert "{platforms}" in DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE

"""WP.13 (OP-1507) — computer_use_actor contract tests.

Pins ``backend/computer_use_actor.py`` against the WP.13 spec:

  * Region + PixelBudget invariants (positive dims, half-open
    containment, union semantics).
  * Pixel budget enforced *before* the engine is called — a denied
    action must not reach :class:`FakeActorEngine.calls`.
  * Keyboard surface rejects unknown keys, unknown modifiers, and
    control characters outside the ``\\n`` / ``\\t`` allow list.
  * Mouse surface rejects unknown buttons.
  * Cross-platform :func:`detect_platform` returns the design-doc
    enumerated value for every host signal mix.
  * :func:`actor_tool_schema` returns the four named tools the LLM
    dispatch path will route on (``computer_screenshot`` /
    ``computer_click`` / ``computer_type`` / ``computer_key``).
  * History is bounded; frames + actions are dispatched in
    sequence-numbered order.
  * Event emission delivers one event per dispatched action; emitter
    exceptions never bubble out of the actor surface.
"""
from __future__ import annotations

from typing import Any, Mapping

import pytest

from backend import computer_use_actor as cua
from backend.computer_use_actor import (
    ACTOR_EVENT_CLICK,
    ACTOR_EVENT_KEY,
    ACTOR_EVENT_SCREENSHOT,
    ACTOR_EVENT_TYPE,
    ACTOR_EVENT_TYPES,
    COMPUTER_USE_ACTOR_SCHEMA_VERSION,
    DEFAULT_HISTORY_SIZE,
    KNOWN_KEYS,
    KNOWN_MODIFIERS,
    MAX_REGION_PIXELS,
    MAX_TYPE_TEXT_LENGTH,
    MOUSE_BUTTON_LEFT,
    MOUSE_BUTTON_MIDDLE,
    MOUSE_BUTTON_RIGHT,
    MOUSE_BUTTON_VALUES,
    PLATFORM_MACOS,
    PLATFORM_VALUES,
    PLATFORM_WAYLAND,
    PLATFORM_WINDOWS,
    PLATFORM_X11,
    RGBA_BYTES_PER_PIXEL,
    ActorAction,
    ActorError,
    ActorFrame,
    ButtonUnknown,
    ComputerUseActor,
    EngineUnavailable,
    FakeActorEngine,
    KeyUnknown,
    PixelBudget,
    PlatformProbe,
    PlatformUnsupported,
    Region,
    RegionInvalid,
    RegionOutOfBudget,
    TextInvalid,
    actor_tool_schema,
    detect_platform,
    validate_rgba_frame,
)


# ── Module invariants ────────────────────────────────────────────


def test_schema_version_is_semver():
    parts = COMPUTER_USE_ACTOR_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_platform_values_match_design_doc():
    assert PLATFORM_VALUES == ("x11", "wayland", "macos", "windows")


def test_mouse_button_values_complete():
    assert MOUSE_BUTTON_VALUES == ("left", "right", "middle")


def test_known_keys_includes_navigation_and_function_keys():
    for k in ("enter", "tab", "escape", "up", "down", "left", "right", "f1", "f12"):
        assert k in KNOWN_KEYS


def test_known_modifiers_set():
    assert KNOWN_MODIFIERS >= {"ctrl", "alt", "shift", "cmd", "meta", "super"}


def test_event_type_set_matches_constants():
    assert ACTOR_EVENT_TYPES == {
        ACTOR_EVENT_SCREENSHOT,
        ACTOR_EVENT_CLICK,
        ACTOR_EVENT_TYPE,
        ACTOR_EVENT_KEY,
    }


def test_error_hierarchy_all_subclass_actor_error():
    for cls in (
        RegionInvalid,
        RegionOutOfBudget,
        KeyUnknown,
        ButtonUnknown,
        TextInvalid,
        PlatformUnsupported,
        EngineUnavailable,
    ):
        assert issubclass(cls, ActorError)


def test_module_exports_stable_surface():
    expected = {
        "Region",
        "PixelBudget",
        "ActorFrame",
        "ActorAction",
        "ActorEngine",
        "ComputerUseActor",
        "FakeActorEngine",
        "PlatformProbe",
        "ActorError",
        "RegionInvalid",
        "RegionOutOfBudget",
        "KeyUnknown",
        "ButtonUnknown",
        "TextInvalid",
        "PlatformUnsupported",
        "EngineUnavailable",
        "detect_platform",
        "actor_tool_schema",
        "validate_rgba_frame",
    }
    assert expected.issubset(set(cua.__all__))


# ── Region ───────────────────────────────────────────────────────


def test_region_rejects_negative_origin():
    with pytest.raises(RegionInvalid):
        Region(-1, 0, 10, 10)
    with pytest.raises(RegionInvalid):
        Region(0, -1, 10, 10)


def test_region_rejects_zero_or_negative_dimensions():
    with pytest.raises(RegionInvalid):
        Region(0, 0, 0, 10)
    with pytest.raises(RegionInvalid):
        Region(0, 0, 10, -5)


def test_region_rejects_oversize_pixels():
    side = int(MAX_REGION_PIXELS ** 0.5) + 1
    with pytest.raises(RegionInvalid):
        Region(0, 0, side, side)


def test_region_contains_point_is_half_open():
    r = Region(10, 20, 5, 5)
    assert r.contains_point(10, 20)
    assert r.contains_point(14, 24)
    assert not r.contains_point(15, 24)
    assert not r.contains_point(10, 25)
    assert not r.contains_point(9, 20)


def test_region_contains_region_strict():
    outer = Region(0, 0, 100, 100)
    inner = Region(10, 10, 80, 80)
    overlap = Region(50, 50, 100, 100)
    assert outer.contains_region(inner)
    assert not outer.contains_region(overlap)


def test_region_to_dict_shape():
    r = Region(1, 2, 3, 4)
    assert r.to_dict() == {"x": 1, "y": 2, "width": 3, "height": 4}


# ── PixelBudget ──────────────────────────────────────────────────


def test_pixel_budget_empty_denies_everything():
    b = PixelBudget()
    assert not b.allows_point(0, 0)
    assert not b.allows_region(Region(0, 0, 1, 1))
    assert b.total_pixels == 0


def test_pixel_budget_union_semantics():
    b = PixelBudget(regions=(Region(0, 0, 10, 10), Region(100, 100, 5, 5)))
    assert b.allows_point(5, 5)
    assert b.allows_point(102, 102)
    assert not b.allows_point(50, 50)
    assert b.total_pixels == 10 * 10 + 5 * 5


def test_pixel_budget_unrestricted_grants_whole_display():
    b = PixelBudget.unrestricted(1920, 1080)
    assert b.allows_point(0, 0)
    assert b.allows_point(1919, 1079)
    assert not b.allows_point(1920, 1080)


def test_pixel_budget_rejects_non_region_members():
    with pytest.raises(RegionInvalid):
        PixelBudget(regions=("not a region",))  # type: ignore[arg-type]


# ── ActorFrame / ActorAction ─────────────────────────────────────


def test_actor_frame_to_dict_has_byte_length_not_payload():
    r = Region(0, 0, 2, 2)
    frame = ActorFrame(
        schema_version=COMPUTER_USE_ACTOR_SCHEMA_VERSION,
        bringup_session_id="hd-1",
        region=r,
        rgba=b"\x00" * (r.pixels * RGBA_BYTES_PER_PIXEL),
        timestamp=1.0,
        sequence=1,
    )
    d = frame.to_dict()
    assert "rgba" not in d
    assert d["rgba_byte_length"] == r.pixels * RGBA_BYTES_PER_PIXEL
    assert d["bringup_session_id"] == "hd-1"
    assert d["region"] == r.to_dict()


def test_actor_action_to_dict_copies_payload():
    payload = {"x": 1, "y": 2, "button": "left"}
    a = ActorAction(
        schema_version=COMPUTER_USE_ACTOR_SCHEMA_VERSION,
        bringup_session_id="hd-1",
        kind="click",
        payload=payload,
        timestamp=2.0,
        sequence=3,
    )
    d = a.to_dict()
    assert d["payload"] == payload
    # confirm copy — mutating the returned dict must not bleed back
    d["payload"]["x"] = 999
    assert a.payload["x"] == 1


# ── validate_rgba_frame ──────────────────────────────────────────


def test_validate_rgba_frame_accepts_correctly_sized_buffer():
    r = Region(0, 0, 4, 4)
    validate_rgba_frame(b"\x00" * (4 * 4 * RGBA_BYTES_PER_PIXEL), r)


def test_validate_rgba_frame_rejects_mismatch():
    r = Region(0, 0, 4, 4)
    with pytest.raises(ActorError):
        validate_rgba_frame(b"\x00" * 7, r)


# ── ComputerUseActor: screenshot ─────────────────────────────────


def _make_actor(
    *,
    budget: PixelBudget | None = None,
    emit=None,
    history_size: int = DEFAULT_HISTORY_SIZE,
    clock=None,
) -> tuple[ComputerUseActor, FakeActorEngine]:
    engine = FakeActorEngine()
    actor = ComputerUseActor(
        engine,
        bringup_session_id="hd-1",
        budget=budget if budget is not None else PixelBudget.unrestricted(800, 600),
        history_size=history_size,
        emit=emit,
        clock=clock,
    )
    return actor, engine


def test_actor_rejects_empty_session_id():
    with pytest.raises(ValueError):
        ComputerUseActor(
            FakeActorEngine(),
            bringup_session_id="",
            budget=PixelBudget.unrestricted(100, 100),
        )


def test_screenshot_returns_rgba_frame():
    actor, engine = _make_actor()
    frame = actor.take_screenshot(Region(0, 0, 10, 10))
    assert frame.region == Region(0, 0, 10, 10)
    assert len(frame.rgba) == 10 * 10 * RGBA_BYTES_PER_PIXEL
    assert frame.bringup_session_id == "hd-1"
    assert frame.sequence == 1
    assert engine.calls == [("take_screenshot", (Region(0, 0, 10, 10),))]


def test_screenshot_default_region_is_first_budget_region():
    actor, _ = _make_actor(
        budget=PixelBudget(regions=(Region(5, 5, 10, 10), Region(100, 0, 20, 20)))
    )
    frame = actor.take_screenshot()
    assert frame.region == Region(5, 5, 10, 10)


def test_screenshot_outside_budget_rejected_before_engine_call():
    actor, engine = _make_actor(budget=PixelBudget(regions=(Region(0, 0, 100, 100),)))
    with pytest.raises(RegionOutOfBudget):
        actor.take_screenshot(Region(200, 200, 10, 10))
    assert engine.calls == []


def test_screenshot_with_empty_budget_raises():
    actor, _ = _make_actor(budget=PixelBudget())
    with pytest.raises(RegionOutOfBudget):
        actor.take_screenshot()


# ── ComputerUseActor: click ──────────────────────────────────────


def test_click_inside_budget_dispatches_to_engine():
    actor, engine = _make_actor()
    action = actor.click(50, 50, "right")
    assert action.kind == "click"
    assert action.payload == {"x": 50, "y": 50, "button": "right"}
    assert engine.calls == [("click", (50, 50, "right"))]


def test_click_default_button_is_left():
    actor, engine = _make_actor()
    action = actor.click(10, 10)
    assert action.payload["button"] == MOUSE_BUTTON_LEFT
    assert engine.calls == [("click", (10, 10, "left"))]


def test_click_outside_budget_rejected_before_engine_call():
    actor, engine = _make_actor(budget=PixelBudget(regions=(Region(0, 0, 10, 10),)))
    with pytest.raises(RegionOutOfBudget):
        actor.click(100, 100)
    assert engine.calls == []


def test_click_unknown_button_rejected():
    actor, engine = _make_actor()
    with pytest.raises(ButtonUnknown):
        actor.click(10, 10, "fourth")  # type: ignore[arg-type]
    assert engine.calls == []


@pytest.mark.parametrize("button", [MOUSE_BUTTON_LEFT, MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE])
def test_click_accepts_all_canonical_buttons(button):
    actor, engine = _make_actor()
    actor.click(10, 10, button)
    assert engine.calls[-1] == ("click", (10, 10, button))


# ── ComputerUseActor: keyboard ───────────────────────────────────


def test_press_key_known_key_dispatches():
    actor, engine = _make_actor()
    action = actor.press_key("enter")
    assert action.kind == "key"
    assert action.payload == {"key": "enter", "modifiers": []}
    assert engine.calls == [("press_key", ("enter", ()))]


def test_press_key_with_modifiers_dispatches_tuple():
    actor, engine = _make_actor()
    action = actor.press_key("f5", modifiers=("ctrl", "shift"))
    assert action.payload["modifiers"] == ["ctrl", "shift"]
    assert engine.calls == [("press_key", ("f5", ("ctrl", "shift")))]


def test_press_key_unknown_key_rejected():
    actor, engine = _make_actor()
    with pytest.raises(KeyUnknown):
        actor.press_key("not_a_key")
    assert engine.calls == []


def test_press_key_unknown_modifier_rejected():
    actor, engine = _make_actor()
    with pytest.raises(KeyUnknown):
        actor.press_key("enter", modifiers=("hyper",))
    assert engine.calls == []


# ── ComputerUseActor: type_text ──────────────────────────────────


def test_type_text_dispatches_text_and_records_length():
    actor, engine = _make_actor()
    action = actor.type_text("hello\n\tworld")
    assert action.kind == "type"
    assert action.payload == {"text_length": len("hello\n\tworld")}
    assert engine.calls == [("type_text", ("hello\n\tworld",))]


def test_type_text_rejects_oversize_input():
    actor, engine = _make_actor()
    with pytest.raises(TextInvalid):
        actor.type_text("a" * (MAX_TYPE_TEXT_LENGTH + 1))
    assert engine.calls == []


def test_type_text_rejects_disallowed_control_characters():
    actor, engine = _make_actor()
    with pytest.raises(TextInvalid):
        actor.type_text("ok\x1bevil")
    with pytest.raises(TextInvalid):
        actor.type_text("ok\x7fdel")
    assert engine.calls == []


def test_type_text_accepts_unicode():
    actor, engine = _make_actor()
    actor.type_text("héllo 世界 ✓")
    assert engine.calls[-1][1][0] == "héllo 世界 ✓"


# ── History + sequencing ─────────────────────────────────────────


def test_actions_get_monotonic_sequence_across_kinds():
    actor, _ = _make_actor()
    actor.take_screenshot(Region(0, 0, 5, 5))
    actor.click(10, 10)
    actor.type_text("hi")
    actor.press_key("enter")
    sequences = [a.sequence for a in actor.recent_actions()]
    assert sequences == [1, 2, 3, 4]


def test_history_is_bounded():
    actor, _ = _make_actor(history_size=3)
    for i in range(5):
        actor.click(i, i)
    actions = actor.recent_actions()
    assert len(actions) == 3
    # last three sequences kept
    assert [a.sequence for a in actions] == [3, 4, 5]


def test_recent_frames_returns_immutable_tuple():
    actor, _ = _make_actor()
    actor.take_screenshot(Region(0, 0, 4, 4))
    frames = actor.recent_frames()
    assert isinstance(frames, tuple)
    assert len(frames) == 1


def test_latest_frame_returns_none_when_empty():
    actor, _ = _make_actor()
    assert actor.latest_frame() is None
    actor.take_screenshot(Region(0, 0, 2, 2))
    latest = actor.latest_frame()
    assert latest is not None
    assert latest.sequence == 1


# ── Event emission ───────────────────────────────────────────────


def test_each_action_emits_one_event():
    events: list[tuple[str, Mapping[str, Any]]] = []
    actor, _ = _make_actor(emit=lambda e, p: events.append((e, p)))
    actor.take_screenshot(Region(0, 0, 4, 4))
    actor.click(10, 10)
    actor.type_text("hi")
    actor.press_key("escape")
    assert [e for e, _ in events] == [
        ACTOR_EVENT_SCREENSHOT,
        ACTOR_EVENT_CLICK,
        ACTOR_EVENT_TYPE,
        ACTOR_EVENT_KEY,
    ]


def test_emit_exception_does_not_bubble():
    def bad_emit(_event: str, _payload: Mapping[str, Any]) -> None:
        raise RuntimeError("downstream consumer crashed")

    actor, _ = _make_actor(emit=bad_emit)
    # Must NOT raise — the action surface must stay usable even if the
    # workbench SSE bus is misbehaving.
    actor.click(10, 10)


# ── detect_platform ──────────────────────────────────────────────


def test_detect_platform_macos():
    assert detect_platform(PlatformProbe(sys_platform="darwin")) == PLATFORM_MACOS


def test_detect_platform_windows():
    assert detect_platform(PlatformProbe(sys_platform="win32")) == PLATFORM_WINDOWS
    assert detect_platform(PlatformProbe(sys_platform="cygwin")) == PLATFORM_WINDOWS


def test_detect_platform_wayland_wins_over_x11():
    probe = PlatformProbe(
        sys_platform="linux", wayland_display="wayland-0", x11_display=":0"
    )
    assert detect_platform(probe) == PLATFORM_WAYLAND


def test_detect_platform_x11_when_only_display_set():
    probe = PlatformProbe(sys_platform="linux", x11_display=":0")
    assert detect_platform(probe) == PLATFORM_X11


def test_detect_platform_linux_headless_raises():
    with pytest.raises(PlatformUnsupported):
        detect_platform(PlatformProbe(sys_platform="linux"))


def test_detect_platform_unknown_raises():
    with pytest.raises(PlatformUnsupported):
        detect_platform(PlatformProbe(sys_platform="plan9"))


# ── actor_tool_schema ────────────────────────────────────────────


def test_actor_tool_schema_lists_four_named_tools():
    schemas = actor_tool_schema()
    names = [s["name"] for s in schemas]
    assert names == [
        "computer_screenshot",
        "computer_click",
        "computer_type",
        "computer_key",
    ]


def test_actor_tool_schema_click_enum_matches_buttons():
    click = next(s for s in actor_tool_schema() if s["name"] == "computer_click")
    button_prop = click["input_schema"]["properties"]["button"]
    assert set(button_prop["enum"]) == set(MOUSE_BUTTON_VALUES)


def test_actor_tool_schema_key_enum_matches_known_keys():
    key = next(s for s in actor_tool_schema() if s["name"] == "computer_key")
    key_prop = key["input_schema"]["properties"]["key"]
    assert set(key_prop["enum"]) == set(KNOWN_KEYS)


def test_actor_tool_schema_type_has_max_length_cap():
    typ = next(s for s in actor_tool_schema() if s["name"] == "computer_type")
    text_prop = typ["input_schema"]["properties"]["text"]
    assert text_prop["maxLength"] == MAX_TYPE_TEXT_LENGTH


# ── FakeActorEngine ──────────────────────────────────────────────


def test_fake_engine_screenshot_size_matches_region():
    engine = FakeActorEngine()
    rgba = engine.take_screenshot(Region(0, 0, 7, 11))
    assert len(rgba) == 7 * 11 * RGBA_BYTES_PER_PIXEL


def test_fake_engine_records_all_calls():
    engine = FakeActorEngine()
    engine.take_screenshot(Region(0, 0, 2, 2))
    engine.click(10, 20, "left")
    engine.type_text("hi")
    engine.press_key("enter", ("ctrl",))
    kinds = [c[0] for c in engine.calls]
    assert kinds == ["take_screenshot", "click", "type_text", "press_key"]


def test_fake_engine_default_platform_is_x11():
    assert FakeActorEngine().platform == PLATFORM_X11


def test_actor_platform_reflects_engine_platform():
    engine = FakeActorEngine(platform=PLATFORM_WAYLAND)
    actor = ComputerUseActor(
        engine,
        bringup_session_id="hd-2",
        budget=PixelBudget.unrestricted(100, 100),
    )
    assert actor.platform == PLATFORM_WAYLAND

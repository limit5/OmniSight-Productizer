"""V3 #2 (issue #319) — ui_annotation_context contract tests.

Pins ``backend/ui_annotation_context.py`` against the V3 row 2 spec:

  * every operator annotation from the frontend overlay (V3 #1) is
    converted to the exact payload shape TODO row #319 #2 pins:
    ``{type, cssSelector, boundingBox, comment}``;
  * :class:`AnnotationContextBuilder` produces one
    :class:`AnnotationAgentContextPayload` per ReAct turn with a
    ready-to-inject text prompt and optional LangChain
    ``HumanMessage`` wrapper;
  * events fire in the ``ui_sandbox.annotation_context.*`` namespace
    with zero overlap with the V2 #2 – #6 topics;
  * malformed wire input produces :class:`AnnotationContextError`
    (parseable by FastAPI's default ``ValueError`` → 422 handler);
  * empty annotation lists are legitimate and produce an ``empty``
    event rather than raising.

All tests are side-effect free — ``clock`` is injected for
determinism and the LangChain ``HumanMessage`` wrapper is exercised
only when the adapter is importable.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Mapping

import pytest

from backend import ui_annotation_context as uac
from backend.ui_annotation_context import (
    ANNOTATION_CONTEXT_EVENT_BUILDING,
    ANNOTATION_CONTEXT_EVENT_BUILT,
    ANNOTATION_CONTEXT_EVENT_EMPTY,
    ANNOTATION_CONTEXT_EVENT_TYPES,
    ANNOTATION_TYPE_CLICK,
    ANNOTATION_TYPE_RECT,
    ANNOTATION_TYPES,
    DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE,
    UI_ANNOTATION_CONTEXT_SCHEMA_VERSION,
    AnnotationAgentContextPayload,
    AnnotationContextBuilder,
    AnnotationContextError,
    NormalizedBoundingBox,
    VisualAnnotation,
    VisualAnnotationAgentPayload,
    annotation_from_dict,
    annotation_to_agent_payload,
    annotations_from_list,
    build_content_blocks,
    build_human_message,
    build_text_content_block,
    clamp_normalized,
    render_annotation_entry,
    render_annotations_markdown,
)


# ═══════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════


class EventRecorder:
    """Thread-safe list of ``(event_type, data)`` tuples for
    assertions against the callback firehose."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, event_type: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._events.append((event_type, dict(data)))

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return list(self._events)

    def by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [d for et, d in self._events if et == event_type]

    def types(self) -> list[str]:
        with self._lock:
            return [et for et, _ in self._events]


def _rect_dict(
    ann_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    comment: str = "",
    css_selector: str | None = None,
    label: int | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": ann_id,
        "type": "rect",
        "boundingBox": {"x": x, "y": y, "w": w, "h": h},
        "comment": comment,
        "cssSelector": css_selector,
    }
    if label is not None:
        out["label"] = label
    if created_at is not None:
        out["createdAt"] = created_at
    if updated_at is not None:
        out["updatedAt"] = updated_at
    return out


def _click_dict(
    ann_id: str,
    x: float,
    y: float,
    *,
    comment: str = "",
    css_selector: str | None = None,
    label: int | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": ann_id,
        "type": "click",
        "boundingBox": {"x": x, "y": y, "w": 0, "h": 0},
        "comment": comment,
        "cssSelector": css_selector,
    }
    if label is not None:
        out["label"] = label
    return out


def _builder(*, event_cb: Callable | None = None) -> AnnotationContextBuilder:
    """Build an ``AnnotationContextBuilder`` with a frozen clock."""

    clock_state = {"t": 1000.0}

    def clock() -> float:
        return clock_state["t"]

    return AnnotationContextBuilder(clock=clock, event_cb=event_cb)


# ═══════════════════════════════════════════════════════════════════
#  Module invariants
# ═══════════════════════════════════════════════════════════════════


EXPECTED_ALL = {
    "UI_ANNOTATION_CONTEXT_SCHEMA_VERSION",
    "ANNOTATION_TYPE_CLICK",
    "ANNOTATION_TYPE_RECT",
    "ANNOTATION_TYPES",
    "DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE",
    "ANNOTATION_CONTEXT_EVENT_BUILDING",
    "ANNOTATION_CONTEXT_EVENT_BUILT",
    "ANNOTATION_CONTEXT_EVENT_EMPTY",
    "ANNOTATION_CONTEXT_EVENT_TYPES",
    "AnnotationContextError",
    "NormalizedBoundingBox",
    "VisualAnnotation",
    "VisualAnnotationAgentPayload",
    "AnnotationAgentContextPayload",
    "AnnotationContextBuilder",
    "annotation_to_agent_payload",
    "annotation_from_dict",
    "annotations_from_list",
    "clamp_normalized",
    "render_annotation_entry",
    "render_annotations_markdown",
    "build_text_content_block",
    "build_content_blocks",
    "build_human_message",
}


def test_all_exports_match():
    assert set(uac.__all__) == EXPECTED_ALL


@pytest.mark.parametrize("name", sorted(EXPECTED_ALL))
def test_each_export_exists(name: str):
    assert hasattr(uac, name)


def test_schema_version_is_semver():
    parts = UI_ANNOTATION_CONTEXT_SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_annotation_types_constant():
    assert ANNOTATION_TYPE_CLICK == "click"
    assert ANNOTATION_TYPE_RECT == "rect"
    assert ANNOTATION_TYPES == ("click", "rect")


def test_default_template_has_named_placeholders():
    for key in (
        "{session_id}",
        "{turn_id}",
        "{annotation_count}",
        "{annotation_body}",
    ):
        assert key in DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE


def test_event_types_all_in_annotation_namespace():
    for name in ANNOTATION_CONTEXT_EVENT_TYPES:
        assert name.startswith("ui_sandbox.annotation_context.")


def test_event_types_are_unique():
    assert len(ANNOTATION_CONTEXT_EVENT_TYPES) == len(
        set(ANNOTATION_CONTEXT_EVENT_TYPES)
    )


def test_event_types_includes_all_event_constants():
    assert set(ANNOTATION_CONTEXT_EVENT_TYPES) == {
        ANNOTATION_CONTEXT_EVENT_BUILDING,
        ANNOTATION_CONTEXT_EVENT_BUILT,
        ANNOTATION_CONTEXT_EVENT_EMPTY,
    }


def test_event_namespace_disjoint_from_v2_2_to_6():
    from backend.ui_agent_visual_context import AGENT_VISUAL_CONTEXT_EVENT_TYPES
    from backend.ui_preview_error_bridge import ERROR_EVENT_TYPES
    from backend.ui_responsive_viewport import VIEWPORT_BATCH_EVENT_TYPES
    from backend.ui_sandbox_lifecycle import LIFECYCLE_EVENT_TYPES
    from backend.ui_screenshot import SCREENSHOT_EVENT_TYPES

    ours = set(ANNOTATION_CONTEXT_EVENT_TYPES)
    for other in (
        LIFECYCLE_EVENT_TYPES,
        SCREENSHOT_EVENT_TYPES,
        VIEWPORT_BATCH_EVENT_TYPES,
        ERROR_EVENT_TYPES,
        AGENT_VISUAL_CONTEXT_EVENT_TYPES,
    ):
        assert ours.isdisjoint(set(other))


def test_error_class_is_value_error_subclass():
    # FastAPI / pydantic default handlers already know how to turn
    # ValueError into a 422 — subclassing saves a layer of wiring.
    assert issubclass(AnnotationContextError, ValueError)


# ═══════════════════════════════════════════════════════════════════
#  clamp_normalized
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.0, 0.0),
        (0.5, 0.5),
        (1.0, 1.0),
        (-0.1, 0.0),
        (1.5, 1.0),
        (0, 0.0),
        (1, 1.0),
    ],
)
def test_clamp_normalized_happy(raw: float, expected: float):
    assert clamp_normalized(raw) == expected


def test_clamp_normalized_handles_infinity():
    assert clamp_normalized(float("inf")) == 0.0
    assert clamp_normalized(float("-inf")) == 0.0


def test_clamp_normalized_handles_nan():
    assert clamp_normalized(float("nan")) == 0.0


def test_clamp_normalized_rejects_non_number():
    with pytest.raises(TypeError):
        clamp_normalized("0.5")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  NormalizedBoundingBox
# ═══════════════════════════════════════════════════════════════════


def test_bounding_box_happy():
    bb = NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
    assert bb.x == 0.1 and bb.y == 0.2 and bb.w == 0.3 and bb.h == 0.4
    assert bb.to_dict() == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


def test_bounding_box_click_point_is_point():
    assert NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0).is_point is True


def test_bounding_box_rect_is_not_point():
    assert NormalizedBoundingBox(x=0.0, y=0.0, w=0.1, h=0.1).is_point is False


@pytest.mark.parametrize(
    "x,y,w,h",
    [
        (-0.1, 0.0, 0.0, 0.0),
        (0.0, -0.1, 0.0, 0.0),
        (1.1, 0.0, 0.0, 0.0),
        (0.0, 1.1, 0.0, 0.0),
        (0.0, 0.0, 1.1, 0.0),
        (0.0, 0.0, 0.0, 1.1),
    ],
)
def test_bounding_box_rejects_out_of_range(x, y, w, h):
    with pytest.raises(AnnotationContextError):
        NormalizedBoundingBox(x=x, y=y, w=w, h=h)


def test_bounding_box_rejects_nan():
    with pytest.raises(AnnotationContextError):
        NormalizedBoundingBox(x=float("nan"), y=0.0, w=0.0, h=0.0)


def test_bounding_box_rejects_xplusw_overflow():
    with pytest.raises(AnnotationContextError):
        NormalizedBoundingBox(x=0.8, y=0.0, w=0.3, h=0.0)


def test_bounding_box_rejects_yplush_overflow():
    with pytest.raises(AnnotationContextError):
        NormalizedBoundingBox(x=0.0, y=0.8, w=0.0, h=0.3)


def test_bounding_box_rejects_non_numeric():
    with pytest.raises(AnnotationContextError):
        NormalizedBoundingBox(x="0.1", y=0.0, w=0.0, h=0.0)  # type: ignore[arg-type]


def test_bounding_box_is_frozen():
    bb = NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        bb.x = 0.9  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════
#  VisualAnnotation
# ═══════════════════════════════════════════════════════════════════


def test_visual_annotation_rect_happy():
    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="narrower",
        css_selector="#card",
        label=1,
        created_at="2026-04-18T00:00:00Z",
        updated_at="2026-04-18T00:00:00Z",
    )
    assert ann.type == "rect"
    assert ann.bounding_box.is_point is False
    d = ann.to_dict()
    assert d["type"] == "rect"
    assert d["boundingBox"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    assert d["cssSelector"] == "#card"
    assert d["label"] == 1


def test_visual_annotation_click_happy():
    ann = VisualAnnotation(
        id="a2",
        type="click",
        bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
        comment="",
    )
    assert ann.bounding_box.is_point
    assert ann.css_selector is None
    d = ann.to_dict()
    assert d["type"] == "click"
    assert "label" not in d  # label omitted when None
    assert "createdAt" not in d


def test_visual_annotation_rejects_empty_id():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
        )


def test_visual_annotation_rejects_unknown_type():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="scribble",  # type: ignore[arg-type]
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
        )


def test_visual_annotation_rejects_click_with_size():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="click",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
        )


def test_visual_annotation_rejects_rect_without_size():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.0, h=0.0),
        )


def test_visual_annotation_rejects_blank_css_selector():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            css_selector="   ",
        )


def test_visual_annotation_rejects_non_positive_label():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            label=0,
        )


def test_visual_annotation_rejects_bool_label():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            label=True,  # type: ignore[arg-type]
        )


def test_visual_annotation_rejects_non_string_comment():
    with pytest.raises(AnnotationContextError):
        VisualAnnotation(
            id="a1",
            type="rect",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            comment=123,  # type: ignore[arg-type]
        )


# ═══════════════════════════════════════════════════════════════════
#  annotation_from_dict / annotations_from_list
# ═══════════════════════════════════════════════════════════════════


def test_annotation_from_dict_rect_happy():
    raw = _rect_dict(
        "a1", 0.1, 0.2, 0.3, 0.4, comment="hi", css_selector="#c", label=3
    )
    ann = annotation_from_dict(raw)
    assert ann.id == "a1"
    assert ann.type == "rect"
    assert ann.bounding_box.x == 0.1
    assert ann.label == 3
    assert ann.css_selector == "#c"


def test_annotation_from_dict_click_happy():
    raw = _click_dict("a2", 0.5, 0.6, comment="")
    ann = annotation_from_dict(raw)
    assert ann.type == "click"
    assert ann.bounding_box.is_point
    assert ann.comment == ""


def test_annotation_from_dict_default_comment_missing():
    raw = {
        "id": "a3",
        "type": "rect",
        "boundingBox": {"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1},
    }
    ann = annotation_from_dict(raw)
    assert ann.comment == ""


def test_annotation_from_dict_ignores_unknown_keys():
    # Forward-compat: future frontend keys must not make the parser
    # throw.  They just get dropped.
    raw = _rect_dict("a1", 0.0, 0.0, 0.1, 0.1)
    raw["someFutureField"] = {"nested": True}
    ann = annotation_from_dict(raw)
    assert ann.id == "a1"


@pytest.mark.parametrize("missing", ["id", "type", "boundingBox"])
def test_annotation_from_dict_missing_required(missing: str):
    raw = _rect_dict("a1", 0.0, 0.0, 0.1, 0.1)
    del raw[missing]
    with pytest.raises(AnnotationContextError):
        annotation_from_dict(raw)


def test_annotation_from_dict_boundingbox_missing_key():
    raw = _rect_dict("a1", 0.0, 0.0, 0.1, 0.1)
    del raw["boundingBox"]["w"]
    with pytest.raises(AnnotationContextError):
        annotation_from_dict(raw)


def test_annotation_from_dict_boundingbox_not_mapping():
    raw = _rect_dict("a1", 0.0, 0.0, 0.1, 0.1)
    raw["boundingBox"] = [0.0, 0.0, 0.1, 0.1]  # type: ignore[assignment]
    with pytest.raises(AnnotationContextError):
        annotation_from_dict(raw)


def test_annotation_from_dict_rejects_non_mapping():
    with pytest.raises(AnnotationContextError):
        annotation_from_dict([])  # type: ignore[arg-type]


def test_annotations_from_list_preserves_order():
    raws = [
        _rect_dict("a1", 0.0, 0.0, 0.1, 0.1),
        _click_dict("a2", 0.5, 0.5),
        _rect_dict("a3", 0.2, 0.2, 0.1, 0.1),
    ]
    parsed = annotations_from_list(raws)
    assert [a.id for a in parsed] == ["a1", "a2", "a3"]


def test_annotations_from_list_rejects_non_sequence():
    with pytest.raises(AnnotationContextError):
        annotations_from_list("nope")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
#  annotation_to_agent_payload — THE TODO-row shape contract
# ═══════════════════════════════════════════════════════════════════


def test_agent_payload_field_set_matches_todo_row():
    """Hard-pin the exact field set TODO row #319 #2 requires:
    ``{type, cssSelector, boundingBox, comment}``.  Any drift is a
    contract break with the V3 #1 frontend pure helper.
    """

    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="test",
        css_selector="#c",
    )
    payload = annotation_to_agent_payload(ann)
    d = payload.to_dict()
    assert set(d.keys()) == {"type", "cssSelector", "boundingBox", "comment"}


def test_agent_payload_field_order_matches_todo_row():
    """Order matters for SSE dedup / golden-test parity.  Pin it."""

    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="test",
        css_selector="#c",
    )
    payload = annotation_to_agent_payload(ann)
    d = payload.to_dict()
    assert list(d.keys()) == ["type", "cssSelector", "boundingBox", "comment"]


def test_agent_payload_bounding_box_is_a_copy():
    """Mutating the annotation must not leak into a previously-built
    payload.  Frontend contract mirrored from V3 #1
    ``annotationToAgentPayload``."""

    bb = NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
    ann = VisualAnnotation(id="a1", type="rect", bounding_box=bb, comment="")
    payload = annotation_to_agent_payload(ann)
    assert payload.bounding_box is not bb
    assert payload.bounding_box.x == 0.1


def test_agent_payload_null_css_selector_survives():
    ann = VisualAnnotation(
        id="a1",
        type="click",
        bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
    )
    payload = annotation_to_agent_payload(ann)
    assert payload.css_selector is None
    d = payload.to_dict()
    assert d["cssSelector"] is None


def test_agent_payload_rejects_non_annotation():
    with pytest.raises(TypeError):
        annotation_to_agent_payload({"id": "a1"})  # type: ignore[arg-type]


def test_agent_payload_roundtrip_is_identity_across_json():
    """JSON round-trip through frontend-shaped dict then back to
    payload must be identity on the four agent-payload fields."""

    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="hello",
        css_selector="#card",
    )
    direct = annotation_to_agent_payload(ann)

    ann_dict = ann.to_dict()
    # Push through a JSON layer as if it came from the wire.
    wire = json.loads(json.dumps(ann_dict))
    parsed = annotation_from_dict(wire)
    roundtripped = annotation_to_agent_payload(parsed)

    assert direct.to_dict() == roundtripped.to_dict()


# ═══════════════════════════════════════════════════════════════════
#  VisualAnnotationAgentPayload
# ═══════════════════════════════════════════════════════════════════


def test_visual_annotation_agent_payload_rejects_bad_type():
    with pytest.raises(AnnotationContextError):
        VisualAnnotationAgentPayload(
            type="scribble",  # type: ignore[arg-type]
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            comment="",
        )


def test_visual_annotation_agent_payload_rejects_non_string_comment():
    with pytest.raises(AnnotationContextError):
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            comment=None,  # type: ignore[arg-type]
        )


def test_visual_annotation_agent_payload_rejects_blank_css():
    with pytest.raises(AnnotationContextError):
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector=" ",
            bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
            comment="",
        )


# ═══════════════════════════════════════════════════════════════════
#  render_annotation_entry / render_annotations_markdown
# ═══════════════════════════════════════════════════════════════════


def test_render_entry_rect_with_selector_and_comment():
    payload = VisualAnnotationAgentPayload(
        type="rect",
        css_selector="#card-0",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="make narrower",
    )
    rendered = render_annotation_entry(label=1, payload=payload)
    assert rendered == (
        '1. [rect] css=`#card-0` comment="make narrower"\n'
        "   - boundingBox: x=10.0% y=20.0% w=30.0% h=40.0%"
    )


def test_render_entry_click_without_selector_or_comment():
    payload = VisualAnnotationAgentPayload(
        type="click",
        css_selector=None,
        bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
        comment="",
    )
    rendered = render_annotation_entry(label=2, payload=payload)
    assert rendered == (
        "2. [click] css=(none) comment=(no comment)\n"
        "   - boundingBox: x=50.0% y=50.0%"
    )


def test_render_entry_strips_whitespace_comment():
    payload = VisualAnnotationAgentPayload(
        type="click",
        css_selector=None,
        bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
        comment="   ",
    )
    rendered = render_annotation_entry(label=1, payload=payload)
    assert "(no comment)" in rendered


def test_render_entry_rejects_non_positive_label():
    payload = VisualAnnotationAgentPayload(
        type="click",
        css_selector=None,
        bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
        comment="",
    )
    with pytest.raises(AnnotationContextError):
        render_annotation_entry(label=0, payload=payload)


def test_render_markdown_empty_payloads():
    assert render_annotations_markdown([]) == "No operator annotations this turn."


def test_render_markdown_auto_labels():
    payloads = [
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0.0, y=0.0, w=0.1, h=0.1),
            comment="one",
        ),
        VisualAnnotationAgentPayload(
            type="click",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
            comment="two",
        ),
    ]
    rendered = render_annotations_markdown(payloads)
    assert rendered.startswith("### Operator annotations\n")
    assert '1. [rect] css=(none) comment="one"' in rendered
    assert '2. [click] css=(none) comment="two"' in rendered


def test_render_markdown_respects_explicit_labels():
    payloads = [
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0.0, y=0.0, w=0.1, h=0.1),
            comment="",
        ),
    ]
    rendered = render_annotations_markdown(payloads, labels=[7])
    assert "7. [rect]" in rendered


def test_render_markdown_label_length_mismatch():
    payloads = [
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0.0, y=0.0, w=0.1, h=0.1),
            comment="",
        ),
    ]
    with pytest.raises(AnnotationContextError):
        render_annotations_markdown(payloads, labels=[1, 2])


def test_render_markdown_output_is_deterministic():
    """Run the render twice on the same input — must be byte-equal."""

    payloads = [
        VisualAnnotationAgentPayload(
            type="rect",
            css_selector="#c",
            bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
            comment="a",
        ),
        VisualAnnotationAgentPayload(
            type="click",
            css_selector=None,
            bounding_box=NormalizedBoundingBox(x=0.5, y=0.5, w=0.0, h=0.0),
            comment="b",
        ),
    ]
    first = render_annotations_markdown(payloads)
    second = render_annotations_markdown(payloads)
    assert first == second


# ═══════════════════════════════════════════════════════════════════
#  build_text_content_block / build_content_blocks
# ═══════════════════════════════════════════════════════════════════


def test_text_content_block_shape():
    block = build_text_content_block("hello")
    assert block == {"type": "text", "text": "hello"}


def test_text_content_block_rejects_empty():
    with pytest.raises(AnnotationContextError):
        build_text_content_block("")


def test_text_content_block_rejects_non_string():
    with pytest.raises(AnnotationContextError):
        build_text_content_block(None)  # type: ignore[arg-type]


def test_build_content_blocks_text_only():
    builder = _builder()
    payload = builder.build(
        session_id="sess",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="c")],
    )
    blocks = build_content_blocks(payload)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    # The block must carry the rendered text_prompt verbatim.
    assert blocks[0]["text"] == payload.text_prompt


def test_build_content_blocks_rejects_wrong_type():
    with pytest.raises(TypeError):
        build_content_blocks({"text_prompt": "nope"})  # type: ignore[arg-type]


def test_build_content_blocks_via_method_mirrors_function():
    builder = _builder()
    payload = builder.build(session_id="sess", turn_id="t1", annotations=[])
    assert payload.to_content_blocks() == build_content_blocks(payload)


# ═══════════════════════════════════════════════════════════════════
#  AnnotationContextBuilder
# ═══════════════════════════════════════════════════════════════════


def test_builder_rejects_blank_session_id():
    builder = _builder()
    with pytest.raises(AnnotationContextError):
        builder.build(session_id="", turn_id="t1", annotations=[])


def test_builder_rejects_blank_turn_id():
    builder = _builder()
    with pytest.raises(AnnotationContextError):
        builder.build(session_id="s", turn_id="  ", annotations=[])


def test_builder_auto_assigns_turn_id():
    builder = _builder()
    payload1 = builder.build(session_id="s", annotations=[])
    payload2 = builder.build(session_id="s", annotations=[])
    assert payload1.turn_id == "annotation-turn-000001"
    assert payload2.turn_id == "annotation-turn-000002"


def test_builder_accepts_dict_inputs():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.1, 0.2, 0.3, 0.4, comment="hi", css_selector="#c"),
        ],
    )
    assert payload.annotation_count == 1
    assert payload.agent_payloads[0].css_selector == "#c"


def test_builder_accepts_visual_annotation_inputs():
    builder = _builder()
    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="hi",
    )
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[ann],
    )
    assert payload.annotation_count == 1
    assert payload.annotations[0] is ann


def test_builder_accepts_mixed_inputs():
    builder = _builder()
    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0.1, y=0.2, w=0.3, h=0.4),
        comment="one",
    )
    dict_ann = _click_dict("a2", 0.5, 0.5, comment="two")
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[ann, dict_ann],
    )
    assert payload.annotation_count == 2
    assert payload.annotations[0].id == "a1"
    assert payload.annotations[1].id == "a2"


def test_builder_propagates_parse_error_with_index():
    builder = _builder()
    good = _rect_dict("a1", 0.0, 0.0, 0.1, 0.1)
    bad = _rect_dict("a2", 0.0, 0.0, 0.0, 0.0)  # zero-size rect → invalid
    with pytest.raises(AnnotationContextError, match=r"annotations\[1\]"):
        builder.build(
            session_id="s",
            turn_id="t1",
            annotations=[good, bad],
        )


def test_builder_rejects_bad_entry_type():
    builder = _builder()
    with pytest.raises(AnnotationContextError):
        builder.build(
            session_id="s",
            turn_id="t1",
            annotations=["not-an-annotation"],  # type: ignore[list-item]
        )


def test_builder_counts_by_type():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.0, 0.0, 0.1, 0.1, css_selector="#c"),
            _rect_dict("a2", 0.2, 0.2, 0.1, 0.1, comment="hi"),
            _click_dict("a3", 0.5, 0.5, comment="go"),
        ],
    )
    assert payload.rect_count == 2
    assert payload.click_count == 1
    assert payload.selector_count == 1
    assert payload.commented_count == 2  # "hi" + "go"


def test_builder_text_prompt_embeds_placeholders():
    builder = _builder()
    payload = builder.build(
        session_id="sess-xyz",
        turn_id="react-42",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="x")],
    )
    assert "sess-xyz" in payload.text_prompt
    assert "react-42" in payload.text_prompt
    assert "Annotation count: 1" in payload.text_prompt
    assert payload.annotation_body_markdown in payload.text_prompt


def test_builder_empty_annotations_produces_stable_prompt():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[],
    )
    assert payload.has_annotations is False
    assert payload.annotation_count == 0
    assert payload.annotation_body_markdown == "No operator annotations this turn."
    assert "No operator annotations this turn." in payload.text_prompt


def test_builder_preserves_frontend_labels():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="", label=7),
            _rect_dict("a2", 0.2, 0.2, 0.1, 0.1, comment="", label=8),
        ],
    )
    assert "7. [rect]" in payload.annotation_body_markdown
    assert "8. [rect]" in payload.annotation_body_markdown


def test_builder_fallback_labels_when_frontend_omits():
    builder = _builder()
    # No label provided — server assigns 1..N.
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.0, 0.0, 0.1, 0.1),
            _click_dict("a2", 0.5, 0.5),
        ],
    )
    assert "1. [rect]" in payload.annotation_body_markdown
    assert "2. [click]" in payload.annotation_body_markdown


def test_builder_build_count_tracks_non_empty_only():
    builder = _builder()
    builder.build(session_id="s", turn_id="t1", annotations=[])
    builder.build(
        session_id="s",
        turn_id="t2",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1)],
    )
    builder.build(session_id="s", turn_id="t3", annotations=[])
    assert builder.build_count() == 1
    assert builder.empty_count() == 2


def test_builder_last_payload_roundtrips():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="c")],
    )
    assert builder.last_payload() is payload


def test_builder_stores_built_at_from_clock():
    clock_state = {"t": 4242.0}

    def clock() -> float:
        return clock_state["t"]

    builder = AnnotationContextBuilder(clock=clock)
    payload = builder.build(session_id="s", turn_id="t1", annotations=[])
    assert payload.built_at == 4242.0


def test_builder_ctor_rejects_non_callable_clock():
    with pytest.raises(TypeError):
        AnnotationContextBuilder(clock="nope")  # type: ignore[arg-type]


def test_builder_ctor_rejects_empty_template():
    with pytest.raises(AnnotationContextError):
        AnnotationContextBuilder(text_prompt_template="")


def test_builder_custom_template():
    tpl = "S={session_id} T={turn_id} N={annotation_count} BODY={annotation_body}"
    builder = AnnotationContextBuilder(
        clock=lambda: 1.0,
        text_prompt_template=tpl,
    )
    payload = builder.build(
        session_id="sess",
        turn_id="turn",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi")],
    )
    assert payload.text_prompt.startswith("S=sess T=turn N=1 BODY=")


# ═══════════════════════════════════════════════════════════════════
#  Events
# ═══════════════════════════════════════════════════════════════════


def test_events_fire_on_non_empty_build():
    recorder = EventRecorder()
    builder = _builder(event_cb=recorder)
    builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi")],
    )
    types = recorder.types()
    assert ANNOTATION_CONTEXT_EVENT_BUILDING in types
    assert ANNOTATION_CONTEXT_EVENT_BUILT in types
    assert ANNOTATION_CONTEXT_EVENT_EMPTY not in types


def test_events_fire_on_empty_build():
    recorder = EventRecorder()
    builder = _builder(event_cb=recorder)
    builder.build(session_id="s", turn_id="t1", annotations=[])
    types = recorder.types()
    assert ANNOTATION_CONTEXT_EVENT_BUILDING in types
    assert ANNOTATION_CONTEXT_EVENT_BUILT not in types
    assert ANNOTATION_CONTEXT_EVENT_EMPTY in types


def test_events_built_carries_counts():
    recorder = EventRecorder()
    builder = _builder(event_cb=recorder)
    builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi", css_selector="#c"),
            _click_dict("a2", 0.5, 0.5),
        ],
    )
    built = recorder.by_type(ANNOTATION_CONTEXT_EVENT_BUILT)
    assert len(built) == 1
    body = built[0]
    assert body["annotation_count"] == 2
    assert body["rect_count"] == 1
    assert body["click_count"] == 1
    assert body["selector_count"] == 1
    assert body["commented_count"] == 1
    assert body["has_annotations"] is True
    assert body["schema_version"] == UI_ANNOTATION_CONTEXT_SCHEMA_VERSION


def test_events_empty_carries_schema_version():
    recorder = EventRecorder()
    builder = _builder(event_cb=recorder)
    builder.build(session_id="s", turn_id="t1", annotations=[])
    empty = recorder.by_type(ANNOTATION_CONTEXT_EVENT_EMPTY)
    assert len(empty) == 1
    assert empty[0]["schema_version"] == UI_ANNOTATION_CONTEXT_SCHEMA_VERSION
    assert empty[0]["session_id"] == "s"
    assert empty[0]["turn_id"] == "t1"


def test_event_callback_exception_does_not_propagate():
    def raising_cb(event_type: str, data: Mapping[str, Any]) -> None:
        raise RuntimeError("callback boom")

    builder = _builder(event_cb=raising_cb)
    # Must not raise — the builder swallows callback exceptions.
    payload = builder.build(session_id="s", turn_id="t1", annotations=[])
    assert payload is not None


# ═══════════════════════════════════════════════════════════════════
#  Snapshot
# ═══════════════════════════════════════════════════════════════════


def test_snapshot_before_any_build():
    builder = _builder()
    snap = builder.snapshot()
    assert snap["schema_version"] == UI_ANNOTATION_CONTEXT_SCHEMA_VERSION
    assert snap["build_count"] == 0
    assert snap["empty_count"] == 0
    assert snap["turn_counter"] == 0
    assert snap["last_payload"] is None


def test_snapshot_after_build():
    builder = _builder()
    builder.build(
        session_id="sess",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi")],
    )
    snap = builder.snapshot()
    assert snap["build_count"] == 1
    assert snap["last_payload"] is not None
    last = snap["last_payload"]
    assert last["session_id"] == "sess"
    assert last["turn_id"] == "t1"
    assert last["annotation_count"] == 1
    assert last["has_annotations"] is True
    # Snapshot elides full payloads — no agent_payloads / annotations keys.
    assert "agent_payloads" not in last
    assert "annotations" not in last


def test_snapshot_is_json_safe():
    builder = _builder()
    builder.build(session_id="s", turn_id="t1", annotations=[])
    snap = builder.snapshot()
    # Must serialise without raising.
    json.dumps(snap)


# ═══════════════════════════════════════════════════════════════════
#  Payload serialisation
# ═══════════════════════════════════════════════════════════════════


def test_payload_to_dict_is_json_safe():
    builder = _builder()
    payload = builder.build(
        session_id="s",
        turn_id="t1",
        annotations=[
            _rect_dict("a1", 0.1, 0.2, 0.3, 0.4, comment="hi", css_selector="#c"),
            _click_dict("a2", 0.5, 0.5, comment="go"),
        ],
    )
    d = payload.to_dict()
    # Round-trip through JSON.
    revived = json.loads(json.dumps(d))
    assert revived["schema_version"] == UI_ANNOTATION_CONTEXT_SCHEMA_VERSION
    assert revived["annotation_count"] == 2
    assert revived["agent_payloads"][0]["type"] == "rect"
    assert revived["agent_payloads"][0]["cssSelector"] == "#c"
    assert revived["agent_payloads"][0]["boundingBox"] == {
        "x": 0.1,
        "y": 0.2,
        "w": 0.3,
        "h": 0.4,
    }
    assert revived["agent_payloads"][0]["comment"] == "hi"


def test_payload_construction_length_mismatch():
    # Direct construction — builder always matches lengths but a
    # downstream caller might drift.  Pin the invariant.
    ann = VisualAnnotation(
        id="a1",
        type="rect",
        bounding_box=NormalizedBoundingBox(x=0, y=0, w=0.1, h=0.1),
        comment="",
    )
    with pytest.raises(AnnotationContextError):
        AnnotationAgentContextPayload(
            session_id="s",
            turn_id="t",
            built_at=1.0,
            annotations=(ann,),
            agent_payloads=(),  # Length mismatch.
            text_prompt="x",
            annotation_body_markdown="",
        )


def test_payload_has_annotations_false_for_empty():
    builder = _builder()
    payload = builder.build(session_id="s", turn_id="t1", annotations=[])
    assert payload.has_annotations is False
    assert payload.annotation_count == 0


# ═══════════════════════════════════════════════════════════════════
#  build_message / HumanMessage wrapper
# ═══════════════════════════════════════════════════════════════════


def test_build_message_returns_tuple():
    builder = _builder()
    payload, message = builder.build_message(
        session_id="s",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi")],
    )
    assert isinstance(payload, AnnotationAgentContextPayload)
    assert message is not None


def test_build_message_content_blocks_match_payload():
    builder = _builder()
    payload, message = builder.build_message(
        session_id="s",
        turn_id="t1",
        annotations=[_rect_dict("a1", 0.0, 0.0, 0.1, 0.1, comment="hi")],
    )
    # HumanMessage.content is the content-block list we built.
    assert hasattr(message, "content")
    assert message.content == build_content_blocks(payload)


def test_build_human_message_rejects_wrong_type():
    with pytest.raises(TypeError):
        build_human_message({"text_prompt": "nope"})  # type: ignore[arg-type]


def test_build_human_message_is_lazy():
    """``build_human_message`` imports LangChain lazily.  We can't
    easily assert "not imported before this call" without poisoning
    sys.modules, but we can assert the import works and the result
    shape is right."""

    builder = _builder()
    payload = builder.build(session_id="s", turn_id="t1", annotations=[])
    message = build_human_message(payload)
    assert message.content == build_content_blocks(payload)


# ═══════════════════════════════════════════════════════════════════
#  Thread safety
# ═══════════════════════════════════════════════════════════════════


def test_builder_thread_safe_concurrent_build():
    """Concurrent ``build`` calls never interleave turn ids or drop
    events."""

    recorder = EventRecorder()
    builder = AnnotationContextBuilder(
        clock=lambda: time.time(),
        event_cb=recorder,
    )
    errors: list[Exception] = []

    def run() -> None:
        try:
            for i in range(10):
                builder.build(
                    session_id=f"sess-{i}",
                    annotations=[_rect_dict(f"a-{i}", 0.0, 0.0, 0.1, 0.1)],
                )
        except Exception as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 4 threads × 10 builds = 40 non-empty builds.
    assert builder.build_count() == 40
    # Turn ids must be unique — otherwise we'd see two threads stamping
    # the same counter value.
    built = recorder.by_type(ANNOTATION_CONTEXT_EVENT_BUILT)
    turn_ids = [e["turn_id"] for e in built]
    assert len(turn_ids) == len(set(turn_ids)) == 40


# ═══════════════════════════════════════════════════════════════════
#  Sibling schema versioning — disjoint from V2 #6
# ═══════════════════════════════════════════════════════════════════


def test_schema_version_is_independent_from_v2_6():
    """V3 #2 ships with its own schema_version; bumping one module
    must not require bumping the other.  Pin the sibling version is
    still ``1.0.0`` so V3 #2's first revision doesn't accidentally
    drag V2 #6 along."""

    from backend.ui_agent_visual_context import (
        UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
    )

    assert UI_ANNOTATION_CONTEXT_SCHEMA_VERSION == "1.0.0"
    assert UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION == "1.0.0"


# ═══════════════════════════════════════════════════════════════════
#  Frontend shape parity with V3 #1 (contract twin)
# ═══════════════════════════════════════════════════════════════════


def test_frontend_wire_shape_parses_cleanly():
    """A payload byte-equivalent to what
    ``components/omnisight/visual-annotator.tsx`` emits must parse
    without any adapter.  Pin the exact field names V3 #1 uses."""

    # This is the exact shape V3 #1's defaultAnnotatorIdFactory +
    # onAnnotationsChange produce.  If any key name drifts, this test
    # fails — catching the contract break before it reaches the wire.
    wire = {
        "id": "ann-lkj3-xy78",
        "type": "rect",
        "boundingBox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "comment": "make this narrower",
        "cssSelector": None,
        "label": 1,
        "createdAt": "2026-04-18T12:34:56.789Z",
        "updatedAt": "2026-04-18T12:34:56.789Z",
    }
    ann = annotation_from_dict(wire)
    agent_payload = annotation_to_agent_payload(ann).to_dict()
    # Exact TODO row #319 #2 shape:
    assert agent_payload == {
        "type": "rect",
        "cssSelector": None,
        "boundingBox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        "comment": "make this narrower",
    }

"""V3 #2 (issue #319) — Annotation → agent context injection.

Closes the "operator feedback" half of the V3 visual iteration loop:
every time the operator marks up the sandbox preview with
:component:`VisualAnnotator` (V3 #1, ``components/omnisight/visual-
annotator.tsx``) the frontend hands the annotation list to the agent
loop via ``{type, cssSelector, boundingBox, comment}`` payloads, and
this module is the server-side consumer.

Where this sits in the V3 stack
--------------------------------

V3 #1 ``components/omnisight/visual-annotator.tsx`` owns the overlay.
It already exports a pure helper ``annotationToAgentPayload`` that
flattens a ``VisualAnnotation`` into the exact
``VisualAnnotationAgentPayload`` shape the TODO row #319 #2 pins::

    {
      type: "click" | "rect",
      cssSelector: string | null,
      boundingBox: { x, y, w, h },   // normalised [0, 1]
      comment: string,
    }

**V3 #2 (this module)** mirrors that pure helper on the Python side
and adds the machinery needed to inject the annotation batch into
the agent's next ReAct turn:

  * :class:`VisualAnnotation` + :class:`VisualAnnotationAgentPayload`
    are the server-side twins of the frontend types.  Field names are
    byte-stable against the frontend payload so a JSON-over-the-wire
    round trip is identity.
  * :func:`annotation_to_agent_payload` / :func:`annotation_from_dict`
    are the pure converters (no I/O, no clock, no LangChain).
  * :func:`render_annotations_markdown` renders the payload list as a
    deterministic markdown block that reads well inside an Anthropic
    multimodal message — labels are 1-based, boundingBox coords are
    rendered as percentages, click / rect discriminator is explicit.
  * :class:`AnnotationContextBuilder` is the per-turn factory: given
    a list of annotations, produce an :class:`AnnotationAgentContext
    Payload` with a ready-to-inject text prompt and (optionally) a
    LangChain ``HumanMessage`` wrapper.  Emits events on the
    ``ui_sandbox.annotation_context.*`` namespace so V2 row 7 SSE
    bridge can surface operator annotations in real time.

Relationship to V2 #6 (``ui_agent_visual_context.py``)
------------------------------------------------------

V2 #6 owns the *visual* half of the per-turn context — screenshots +
preview errors.  V3 #2 owns the *operator feedback* half — the
annotation overlay.  Two modules, disjoint responsibilities, same
multimodal-content-block vocabulary.  The agent loop typically sends
**both** as part of the next ReAct HumanMessage: V2 #6 goes first
(screenshots + error summary), V3 #2 follows with the annotation
markdown.  The shared vocabulary means either module can be swapped
in isolation without breaking the other.

Design decisions
----------------

* **Pure-function core.**  ``annotation_to_agent_payload`` and
  ``render_annotations_markdown`` are side-effect free so the
  frontend can drive them via test fixtures or the backend can fold
  them into any orchestration layer (LangGraph, a plain FastAPI
  route, a CLI).  Only :class:`AnnotationContextBuilder` holds
  state (turn counter, build counter, last-payload snapshot, optional
  event callback).
* **Lossy-safe parsing.**  ``annotation_from_dict`` is strict about
  shape so malformed operator input from the wire gets a clear
  error message instead of a silent no-op.  Unknown extra keys are
  ignored (forward-compat with future frontend fields).
* **Empty-list handling.**  Building with zero annotations is
  legitimate — the operator may hit "submit" on an empty canvas.
  The payload carries ``has_annotations=False`` and the text block
  reads "No operator annotations this turn" so the agent's prompt
  layout stays stable.  An ``empty`` event is emitted so downstream
  analytics can count no-op turns.
* **Determinism.**  The markdown block is produced from sorted
  inputs (stable order → golden tests can pin the exact bytes).
  Input order from the frontend is preserved — operators expect
  label #1 to come first — but labels are auto-assigned on the
  server side if the frontend omits them (e.g. if an annotation
  was persisted before V3 #1 added the label field).
* **Event namespace.**  ``ui_sandbox.annotation_context.*`` — three
  topics ``building`` / ``built`` / ``empty``.  Disjoint from the
  V2 row 2 – 7 namespaces so the SSE bus (V2 row 7) can subscribe
  on prefix.
* **LangChain firewall respected.**  :func:`build_human_message`
  lazy-imports ``HumanMessage`` from :mod:`backend.llm_adapter` so
  this module is import-cheap for callers that only want the text
  block or raw content-block dict list.

Contract (pinned by ``backend/tests/test_ui_annotation_context.py``)
--------------------------------------------------------------------

* :data:`UI_ANNOTATION_CONTEXT_SCHEMA_VERSION` is semver; bump on
  shape changes to :class:`VisualAnnotation.to_dict` /
  :class:`VisualAnnotationAgentPayload.to_dict` /
  :class:`AnnotationAgentContextPayload.to_dict` /
  :func:`build_content_blocks` output.
* Field names on the agent payload (``type`` / ``cssSelector`` /
  ``boundingBox`` / ``comment``) match TODO row #319 #2 exactly.
* :meth:`AnnotationContextBuilder.build` never raises for well-formed
  input — malformed inputs raise :class:`AnnotationContextError`.
* :func:`build_content_blocks` returns a list whose first element is
  always the text block.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


__all__ = [
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
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on shape changes to any ``to_dict`` / ``build_content_blocks``
#: output in this module.  Paired with
#: :data:`UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION` for sibling V2 #6 —
#: the two modules version independently.
UI_ANNOTATION_CONTEXT_SCHEMA_VERSION = "1.0.0"

#: Annotation type discriminators — byte-stable against the frontend
#: ``VisualAnnotationType`` union in
#: ``components/omnisight/visual-annotator.tsx``.
ANNOTATION_TYPE_CLICK = "click"
ANNOTATION_TYPE_RECT = "rect"

#: Full roster of legal annotation types.  Parse failures list this
#: in the error message for fast operator feedback.
ANNOTATION_TYPES: tuple[str, ...] = (ANNOTATION_TYPE_CLICK, ANNOTATION_TYPE_RECT)


#: Deterministic text-block template.  Uses ``str.format`` with named
#: placeholders so callers can override via the builder ctor.  The
#: body is rendered by :func:`render_annotations_markdown` and
#: substituted into ``{annotation_body}``.
DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE = (
    "The operator has marked up the live preview for session "
    "`{session_id}` (turn `{turn_id}`).\n"
    "Annotation count: {annotation_count}\n"
    "\n"
    "{annotation_body}\n"
    "\n"
    "Treat each annotation as a directive for the next code change."
    "  ``rect`` annotations outline a region that needs attention; "
    "``click`` annotations pin a specific element.  The ``comment`` "
    "is the operator's own words — prefer it over inferring intent "
    "from pixels alone.  When a ``cssSelector`` is present the "
    "frontend element inspector has already identified the DOM node; "
    "target that selector first before guessing."
)


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


ANNOTATION_CONTEXT_EVENT_BUILDING = "ui_sandbox.annotation_context.building"
ANNOTATION_CONTEXT_EVENT_BUILT = "ui_sandbox.annotation_context.built"
ANNOTATION_CONTEXT_EVENT_EMPTY = "ui_sandbox.annotation_context.empty"


#: Full roster of topics emitted by this module — V2 row 7 SSE bus
#: subscribes on the ``ui_sandbox.annotation_context.`` prefix.
ANNOTATION_CONTEXT_EVENT_TYPES: tuple[str, ...] = (
    ANNOTATION_CONTEXT_EVENT_BUILDING,
    ANNOTATION_CONTEXT_EVENT_BUILT,
    ANNOTATION_CONTEXT_EVENT_EMPTY,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class AnnotationContextError(ValueError):
    """Base class for ``ui_annotation_context`` parse / build errors.

    Subclasses :class:`ValueError` because every raise site is a
    shape-level rejection of bad input — callers that already catch
    ``ValueError`` (e.g. a FastAPI handler) get sensible defaults
    without extra wiring.
    """


# ───────────────────────────────────────────────────────────────────
#  Records
# ───────────────────────────────────────────────────────────────────


def clamp_normalized(n: float) -> float:
    """Clamp a number into ``[0, 1]`` — robust against NaN / Infinity.

    Mirrors the frontend ``clampNormalized`` helper in
    ``components/omnisight/visual-annotator.tsx``.  Kept here so the
    server can reject payloads that somehow escaped the frontend's
    clamping (e.g. a caller hand-crafting a JSON body).
    """

    if not isinstance(n, (int, float)):
        raise TypeError("n must be an int or float")
    value = float(n)
    if not math.isfinite(value):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@dataclass(frozen=True)
class NormalizedBoundingBox:
    """A ``(x, y, w, h)`` rect in normalised ``[0, 1]`` coordinates.

    Matches the frontend ``NormalizedBoundingBox`` interface exactly.
    Click annotations use ``w=0`` and ``h=0``.
    """

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            raw = getattr(self, name)
            if not isinstance(raw, (int, float)):
                raise AnnotationContextError(f"boundingBox.{name} must be a number")
            value = float(raw)
            if not math.isfinite(value):
                raise AnnotationContextError(
                    f"boundingBox.{name} must be finite, got {raw!r}"
                )
            if value < 0.0 or value > 1.0:
                raise AnnotationContextError(
                    f"boundingBox.{name} must be in [0, 1], got {raw!r}"
                )
            object.__setattr__(self, name, value)
        # Ensure the box stays inside the unit square after translation.
        if self.x + self.w > 1.0 + 1e-9:
            raise AnnotationContextError(
                f"boundingBox.x + boundingBox.w must be <= 1, got "
                f"{self.x} + {self.w} = {self.x + self.w}"
            )
        if self.y + self.h > 1.0 + 1e-9:
            raise AnnotationContextError(
                f"boundingBox.y + boundingBox.h must be <= 1, got "
                f"{self.y} + {self.h} = {self.y + self.h}"
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "x": float(self.x),
            "y": float(self.y),
            "w": float(self.w),
            "h": float(self.h),
        }

    @property
    def is_point(self) -> bool:
        """True for click-style (zero-size) boxes."""

        return self.w == 0.0 and self.h == 0.0


@dataclass(frozen=True)
class VisualAnnotation:
    """Server-side twin of the frontend ``VisualAnnotation`` record.

    Field names match the frontend interface byte-for-byte so a
    JSON round-trip through
    :func:`annotation_to_agent_payload` →
    :func:`annotation_from_dict` is identity.
    """

    id: str
    type: str
    bounding_box: NormalizedBoundingBox
    comment: str = ""
    css_selector: str | None = None
    label: int | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise AnnotationContextError("id must be a non-empty string")
        if self.type not in ANNOTATION_TYPES:
            raise AnnotationContextError(
                f"type must be one of {ANNOTATION_TYPES}, got {self.type!r}"
            )
        if not isinstance(self.bounding_box, NormalizedBoundingBox):
            raise AnnotationContextError(
                "bounding_box must be a NormalizedBoundingBox"
            )
        if not isinstance(self.comment, str):
            raise AnnotationContextError("comment must be a string")
        if self.css_selector is not None and (
            not isinstance(self.css_selector, str) or not self.css_selector.strip()
        ):
            raise AnnotationContextError(
                "css_selector must be a non-empty string or None"
            )
        if self.label is not None:
            if not isinstance(self.label, int) or isinstance(self.label, bool):
                raise AnnotationContextError("label must be a positive int or None")
            if self.label < 1:
                raise AnnotationContextError("label must be >= 1")
        # Click annotations must be zero-sized.  Rect annotations must
        # have non-zero area — a zero-area rect should have been
        # demoted to a click by the frontend.  We enforce that on the
        # wire so a buggy caller produces a clear error.
        if self.type == ANNOTATION_TYPE_CLICK and not self.bounding_box.is_point:
            raise AnnotationContextError(
                "click annotations must have zero-size boundingBox"
            )
        if self.type == ANNOTATION_TYPE_RECT and self.bounding_box.is_point:
            raise AnnotationContextError(
                "rect annotations must have non-zero boundingBox"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "boundingBox": self.bounding_box.to_dict(),
            "comment": self.comment,
            "cssSelector": self.css_selector,
        }
        if self.label is not None:
            out["label"] = int(self.label)
        if self.created_at is not None:
            out["createdAt"] = self.created_at
        if self.updated_at is not None:
            out["updatedAt"] = self.updated_at
        return out


@dataclass(frozen=True)
class VisualAnnotationAgentPayload:
    """The exact shape TODO row #319 #2 pins — emitted per annotation.

    Matches the frontend ``VisualAnnotationAgentPayload`` interface in
    ``components/omnisight/visual-annotator.tsx``::

        {type, cssSelector, boundingBox, comment}

    Any drift here is a contract break with V3 #1 — the contract tests
    pin both the field set and their order in the JSON output so a
    regression is caught at the assertion layer before it reaches
    the wire.
    """

    type: str
    css_selector: str | None
    bounding_box: NormalizedBoundingBox
    comment: str

    def __post_init__(self) -> None:
        if self.type not in ANNOTATION_TYPES:
            raise AnnotationContextError(
                f"type must be one of {ANNOTATION_TYPES}, got {self.type!r}"
            )
        if self.css_selector is not None and (
            not isinstance(self.css_selector, str) or not self.css_selector.strip()
        ):
            raise AnnotationContextError(
                "cssSelector must be a non-empty string or None"
            )
        if not isinstance(self.bounding_box, NormalizedBoundingBox):
            raise AnnotationContextError(
                "boundingBox must be a NormalizedBoundingBox"
            )
        if not isinstance(self.comment, str):
            raise AnnotationContextError("comment must be a string")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict with the exact field ordering TODO row pins.

        Field order in Python dicts is insertion-ordered since 3.7, so
        the caller gets a deterministic ``json.dumps`` output — useful
        for SSE frame dedup and golden tests.
        """

        return {
            "type": self.type,
            "cssSelector": self.css_selector,
            "boundingBox": self.bounding_box.to_dict(),
            "comment": self.comment,
        }


@dataclass(frozen=True)
class AnnotationAgentContextPayload:
    """One turn's operator-annotation bundle for the agent.

    Parallels :class:`AgentVisualContextPayload` from V2 #6: a text
    block ready for injection into the next ReAct HumanMessage, plus
    structured data (the annotations + their agent-payload twins) for
    anyone who wants to drive their own serialisation.
    """

    session_id: str
    turn_id: str
    built_at: float
    annotations: tuple[VisualAnnotation, ...]
    agent_payloads: tuple[VisualAnnotationAgentPayload, ...]
    text_prompt: str
    annotation_body_markdown: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise AnnotationContextError("session_id must be a non-empty string")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise AnnotationContextError("turn_id must be a non-empty string")
        if not isinstance(self.built_at, (int, float)) or self.built_at < 0:
            raise AnnotationContextError("built_at must be a non-negative number")
        if len(self.annotations) != len(self.agent_payloads):
            raise AnnotationContextError(
                "annotations and agent_payloads must have matching length"
            )
        for ann in self.annotations:
            if not isinstance(ann, VisualAnnotation):
                raise AnnotationContextError(
                    "annotations entries must be VisualAnnotation"
                )
        for payload in self.agent_payloads:
            if not isinstance(payload, VisualAnnotationAgentPayload):
                raise AnnotationContextError(
                    "agent_payloads entries must be VisualAnnotationAgentPayload"
                )
        if not isinstance(self.text_prompt, str) or not self.text_prompt:
            raise AnnotationContextError("text_prompt must be a non-empty string")
        if not isinstance(self.annotation_body_markdown, str):
            raise AnnotationContextError(
                "annotation_body_markdown must be a string"
            )
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise AnnotationContextError(
                    "warnings entries must be non-empty strings"
                )
        object.__setattr__(self, "annotations", tuple(self.annotations))
        object.__setattr__(self, "agent_payloads", tuple(self.agent_payloads))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def annotation_count(self) -> int:
        return len(self.annotations)

    @property
    def has_annotations(self) -> bool:
        return bool(self.annotations)

    @property
    def click_count(self) -> int:
        return sum(1 for a in self.annotations if a.type == ANNOTATION_TYPE_CLICK)

    @property
    def rect_count(self) -> int:
        return sum(1 for a in self.annotations if a.type == ANNOTATION_TYPE_RECT)

    @property
    def selector_count(self) -> int:
        return sum(1 for a in self.annotations if a.css_selector)

    @property
    def commented_count(self) -> int:
        return sum(1 for a in self.annotations if a.comment.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_ANNOTATION_CONTEXT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "built_at": float(self.built_at),
            "annotation_count": self.annotation_count,
            "click_count": self.click_count,
            "rect_count": self.rect_count,
            "selector_count": self.selector_count,
            "commented_count": self.commented_count,
            "has_annotations": self.has_annotations,
            "annotations": [a.to_dict() for a in self.annotations],
            "agent_payloads": [p.to_dict() for p in self.agent_payloads],
            "text_prompt": self.text_prompt,
            "annotation_body_markdown": self.annotation_body_markdown,
            "warnings": list(self.warnings),
        }

    def to_content_blocks(self) -> list[dict[str, Any]]:
        """Return the multimodal content-block list — text only.

        Unlike V2 #6 there's no image in this bundle: annotations are
        metadata *about* the live preview, not a replacement for it.
        Callers that want both typically concatenate V2 #6's content
        blocks with ours.
        """

        return build_content_blocks(self)


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def annotation_to_agent_payload(
    annotation: VisualAnnotation,
) -> VisualAnnotationAgentPayload:
    """Flatten a :class:`VisualAnnotation` into the wire-shape the
    TODO row #319 #2 pins.

    Mirrors the frontend ``annotationToAgentPayload`` helper byte-for-
    byte: the boundingBox is copied (never shared) so the caller can
    freely mutate either side.
    """

    if not isinstance(annotation, VisualAnnotation):
        raise TypeError("annotation must be a VisualAnnotation")
    return VisualAnnotationAgentPayload(
        type=annotation.type,
        css_selector=annotation.css_selector,
        bounding_box=NormalizedBoundingBox(
            x=annotation.bounding_box.x,
            y=annotation.bounding_box.y,
            w=annotation.bounding_box.w,
            h=annotation.bounding_box.h,
        ),
        comment=annotation.comment,
    )


def annotation_from_dict(raw: Mapping[str, Any]) -> VisualAnnotation:
    """Parse a frontend-shaped dict into a :class:`VisualAnnotation`.

    Accepts the exact keys the frontend emits (``id`` / ``type`` /
    ``boundingBox`` / ``comment`` / ``cssSelector`` / ``label`` /
    ``createdAt`` / ``updatedAt``).  Missing ``comment`` defaults to
    ``""`` — the frontend canonicalises comment to ``""`` but an
    operator hand-crafting a JSON body should not have to.
    Unknown extra keys are ignored (forward-compat with future V3
    frontend fields).

    Raises :class:`AnnotationContextError` for missing required keys or
    type mismatches.
    """

    if not isinstance(raw, Mapping):
        raise AnnotationContextError("annotation record must be a mapping")
    try:
        ann_id = raw["id"]
    except KeyError as exc:
        raise AnnotationContextError("annotation missing 'id'") from exc
    try:
        ann_type = raw["type"]
    except KeyError as exc:
        raise AnnotationContextError("annotation missing 'type'") from exc
    try:
        bb_raw = raw["boundingBox"]
    except KeyError as exc:
        raise AnnotationContextError("annotation missing 'boundingBox'") from exc
    if not isinstance(bb_raw, Mapping):
        raise AnnotationContextError("boundingBox must be an object")
    try:
        bb = NormalizedBoundingBox(
            x=bb_raw["x"],
            y=bb_raw["y"],
            w=bb_raw["w"],
            h=bb_raw["h"],
        )
    except KeyError as exc:
        raise AnnotationContextError(
            f"boundingBox missing key: {exc.args[0]}"
        ) from exc
    comment = raw.get("comment", "")
    if not isinstance(comment, str):
        raise AnnotationContextError("comment must be a string")
    css_selector_raw = raw.get("cssSelector")
    if css_selector_raw is not None and (
        not isinstance(css_selector_raw, str) or not css_selector_raw.strip()
    ):
        raise AnnotationContextError(
            "cssSelector must be a non-empty string or null"
        )
    label_raw = raw.get("label")
    if label_raw is not None:
        if isinstance(label_raw, bool) or not isinstance(label_raw, int):
            raise AnnotationContextError("label must be a positive int or null")
    created_at_raw = raw.get("createdAt")
    if created_at_raw is not None and not isinstance(created_at_raw, str):
        raise AnnotationContextError("createdAt must be a string or null")
    updated_at_raw = raw.get("updatedAt")
    if updated_at_raw is not None and not isinstance(updated_at_raw, str):
        raise AnnotationContextError("updatedAt must be a string or null")
    return VisualAnnotation(
        id=ann_id,
        type=ann_type,
        bounding_box=bb,
        comment=comment,
        css_selector=css_selector_raw,
        label=label_raw,
        created_at=created_at_raw,
        updated_at=updated_at_raw,
    )


def annotations_from_list(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[VisualAnnotation, ...]:
    """Parse a list of frontend-shaped dicts into a tuple of
    :class:`VisualAnnotation`.

    Preserves input order (operators expect their first-drawn
    annotation to stay #1).
    """

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise AnnotationContextError("annotations must be a sequence")
    return tuple(annotation_from_dict(item) for item in raw)


def _format_pct(value: float) -> str:
    """Format a normalised coordinate as a percentage with 1 dp.

    Golden-test friendly — always two or three chars for integer
    percentages, ``NN.N%`` for fractional.
    """

    return f"{value * 100:.1f}%"


def render_annotation_entry(
    *,
    label: int,
    payload: VisualAnnotationAgentPayload,
) -> str:
    """Render a single annotation as a markdown list entry.

    Format (byte-stable)::

        1. [rect] css=`#card-0` comment="make narrower"
           - boundingBox: x=10.0% y=20.0% w=30.0% h=40.0%

    or for click annotations without a selector::

        2. [click] css=(none) comment="(no comment)"
           - boundingBox: x=12.3% y=45.6%
    """

    if not isinstance(label, int) or label < 1 or isinstance(label, bool):
        raise AnnotationContextError("label must be a positive int")
    if not isinstance(payload, VisualAnnotationAgentPayload):
        raise TypeError("payload must be a VisualAnnotationAgentPayload")

    css = (
        f"`{payload.css_selector}`"
        if payload.css_selector
        else "(none)"
    )
    comment_body = payload.comment.strip()
    comment = f'"{comment_body}"' if comment_body else "(no comment)"
    box = payload.bounding_box
    if payload.type == ANNOTATION_TYPE_CLICK:
        box_line = (
            f"   - boundingBox: x={_format_pct(box.x)} y={_format_pct(box.y)}"
        )
    else:
        box_line = (
            f"   - boundingBox: "
            f"x={_format_pct(box.x)} y={_format_pct(box.y)} "
            f"w={_format_pct(box.w)} h={_format_pct(box.h)}"
        )
    header = f"{label}. [{payload.type}] css={css} comment={comment}"
    return f"{header}\n{box_line}"


def render_annotations_markdown(
    payloads: Sequence[VisualAnnotationAgentPayload],
    *,
    labels: Sequence[int] | None = None,
) -> str:
    """Render a list of payloads as a markdown block for the ReAct
    prompt.

    When ``labels`` is given it must have the same length as
    ``payloads``.  When omitted, labels auto-assign from 1.
    """

    payloads_tuple = tuple(payloads)
    if labels is not None:
        labels_tuple = tuple(labels)
        if len(labels_tuple) != len(payloads_tuple):
            raise AnnotationContextError(
                "labels must have same length as payloads"
            )
    else:
        labels_tuple = tuple(range(1, len(payloads_tuple) + 1))

    if not payloads_tuple:
        return "No operator annotations this turn."

    lines = ["### Operator annotations", ""]
    for lbl, payload in zip(labels_tuple, payloads_tuple):
        lines.append(render_annotation_entry(label=lbl, payload=payload))
    return "\n".join(lines)


def build_text_content_block(text: str) -> dict[str, Any]:
    """Produce an Anthropic ``{"type":"text","text":...}`` block."""

    if not isinstance(text, str) or not text:
        raise AnnotationContextError("text must be a non-empty string")
    return {"type": "text", "text": text}


def build_content_blocks(
    payload: AnnotationAgentContextPayload,
) -> list[dict[str, Any]]:
    """Flatten a payload into a content-block list — text only.

    Mirrors :func:`backend.ui_agent_visual_context.build_content_blocks`
    in structure so callers can concatenate the two lists into a
    single multimodal HumanMessage without special-casing.
    """

    if not isinstance(payload, AnnotationAgentContextPayload):
        raise TypeError("payload must be an AnnotationAgentContextPayload")
    return [build_text_content_block(payload.text_prompt)]


def build_human_message(payload: AnnotationAgentContextPayload) -> Any:
    """Wrap ``payload`` as a LangChain ``HumanMessage``.

    Lazy-imports :func:`backend.llm_adapter.HumanMessage` so this
    module stays import-cheap for callers that only need the raw
    content-block list (e.g. SSE serialisation).
    """

    if not isinstance(payload, AnnotationAgentContextPayload):
        raise TypeError("payload must be an AnnotationAgentContextPayload")
    from backend.llm_adapter import HumanMessage  # noqa: WPS433 - lazy import

    return HumanMessage(content=build_content_blocks(payload))


# ───────────────────────────────────────────────────────────────────
#  Builder
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class AnnotationContextBuilder:
    """Per-turn annotation context factory.

    Stateless apart from counters + last-payload snapshot (used by
    SSE operator dashboards) and a turn counter (used when the caller
    doesn't supply an explicit ``turn_id``).  Thread-safe — all state
    access is serialised on an ``RLock``.

    Typical wire-up::

        builder = AnnotationContextBuilder(event_cb=sse_bus.emit)
        payload, message = builder.build_message(
            session_id="sess-1",
            turn_id="react-42",
            annotations=[
                {"id": "ann-1", "type": "rect", ...},
                ...,
            ],
        )
        response = llm_adapter.invoke_chat([system, visual_msg, message])

    The builder never talks to Docker or Playwright — it's a pure
    serialisation layer — so no sleep / clock dependency exists
    beyond the timestamp stamped onto the payload.  Tests inject a
    ``clock=lambda: 123.0`` for determinism.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        text_prompt_template: str = DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(text_prompt_template, str) or not text_prompt_template:
            raise AnnotationContextError(
                "text_prompt_template must be a non-empty string"
            )
        self._clock = clock
        self._event_cb = event_cb
        self._text_prompt_template = text_prompt_template

        self._lock = threading.RLock()
        self._turn_counter = 0
        self._build_count = 0
        self._empty_count = 0
        self._last_payload: AnnotationAgentContextPayload | None = None

    # ─────────────── Accessors ───────────────

    @property
    def text_prompt_template(self) -> str:
        return self._text_prompt_template

    def build_count(self) -> int:
        with self._lock:
            return self._build_count

    def empty_count(self) -> int:
        with self._lock:
            return self._empty_count

    def last_payload(self) -> AnnotationAgentContextPayload | None:
        with self._lock:
            return self._last_payload

    # ─────────────── Core API ───────────────

    def build(
        self,
        *,
        session_id: str,
        annotations: Sequence[VisualAnnotation | Mapping[str, Any]] = (),
        turn_id: str | None = None,
    ) -> AnnotationAgentContextPayload:
        """Build one turn's annotation context.

        ``annotations`` may be either a list of parsed
        :class:`VisualAnnotation` instances (typical when the caller
        already did the parsing) or a list of frontend-shaped dicts.
        Mixing the two is allowed — each entry is normalised
        independently.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise AnnotationContextError("session_id must be a non-empty string")
        effective_turn_id = self._resolve_turn_id(turn_id)

        parsed: list[VisualAnnotation] = []
        for idx, item in enumerate(annotations):
            if isinstance(item, VisualAnnotation):
                parsed.append(item)
            elif isinstance(item, Mapping):
                try:
                    parsed.append(annotation_from_dict(item))
                except AnnotationContextError as exc:
                    raise AnnotationContextError(
                        f"annotations[{idx}]: {exc}"
                    ) from exc
            else:
                raise AnnotationContextError(
                    f"annotations[{idx}] must be VisualAnnotation or mapping, "
                    f"got {type(item).__name__}"
                )

        # Assign 1-based labels for the markdown block, preserving
        # the frontend label where present so the operator sees the
        # same numbering end-to-end.  Otherwise fall back to index+1.
        labels: list[int] = []
        for idx, ann in enumerate(parsed):
            labels.append(ann.label if ann.label is not None else idx + 1)

        self._emit(
            ANNOTATION_CONTEXT_EVENT_BUILDING,
            {
                "session_id": session_id,
                "turn_id": effective_turn_id,
                "annotation_count": len(parsed),
                "at": float(self._clock()),
            },
        )

        agent_payloads = tuple(annotation_to_agent_payload(a) for a in parsed)
        annotation_body = render_annotations_markdown(
            agent_payloads, labels=labels
        )

        text_prompt = self._text_prompt_template.format(
            session_id=session_id,
            turn_id=effective_turn_id,
            annotation_count=len(parsed),
            annotation_body=annotation_body,
        )

        now = self._clock()
        payload = AnnotationAgentContextPayload(
            session_id=session_id,
            turn_id=effective_turn_id,
            built_at=float(now),
            annotations=tuple(parsed),
            agent_payloads=agent_payloads,
            text_prompt=text_prompt,
            annotation_body_markdown=annotation_body,
            warnings=(),
        )

        with self._lock:
            if payload.has_annotations:
                self._build_count += 1
            else:
                self._empty_count += 1
            self._last_payload = payload

        if payload.has_annotations:
            self._emit(
                ANNOTATION_CONTEXT_EVENT_BUILT,
                self._envelope_for_event(payload),
            )
        else:
            self._emit(
                ANNOTATION_CONTEXT_EVENT_EMPTY,
                {
                    "session_id": session_id,
                    "turn_id": effective_turn_id,
                    "at": float(now),
                    "schema_version": UI_ANNOTATION_CONTEXT_SCHEMA_VERSION,
                },
            )
        return payload

    def build_message(
        self,
        *,
        session_id: str,
        annotations: Sequence[VisualAnnotation | Mapping[str, Any]] = (),
        turn_id: str | None = None,
    ) -> tuple[AnnotationAgentContextPayload, Any]:
        """Convenience: build payload then return
        ``(payload, HumanMessage)`` so callers can shove the message
        straight into ``llm_adapter.invoke_chat``.
        """

        payload = self.build(
            session_id=session_id,
            annotations=annotations,
            turn_id=turn_id,
        )
        return payload, build_human_message(payload)

    # ─────────────── Snapshot ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe operator snapshot — counters + last payload
        metadata.  The last payload's annotation agent-payload dicts
        are *not* inlined to keep the snapshot lean; callers that
        want the full bundle go through :meth:`last_payload`.
        """

        with self._lock:
            last = self._last_payload
            last_summary: dict[str, Any] | None = None
            if last is not None:
                last_summary = {
                    "session_id": last.session_id,
                    "turn_id": last.turn_id,
                    "built_at": float(last.built_at),
                    "annotation_count": last.annotation_count,
                    "click_count": last.click_count,
                    "rect_count": last.rect_count,
                    "selector_count": last.selector_count,
                    "commented_count": last.commented_count,
                    "has_annotations": last.has_annotations,
                    "warning_count": len(last.warnings),
                }
            return {
                "schema_version": UI_ANNOTATION_CONTEXT_SCHEMA_VERSION,
                "build_count": int(self._build_count),
                "empty_count": int(self._empty_count),
                "turn_counter": int(self._turn_counter),
                "last_payload": last_summary,
                "now": float(self._clock()),
            }

    # ─────────────── Internal plumbing ───────────────

    def _resolve_turn_id(self, turn_id: str | None) -> str:
        if turn_id is not None:
            if not isinstance(turn_id, str) or not turn_id.strip():
                raise AnnotationContextError(
                    "turn_id must be a non-empty string or None"
                )
            return turn_id
        with self._lock:
            self._turn_counter += 1
            return f"annotation-turn-{self._turn_counter:06d}"

    def _envelope_for_event(
        self, payload: AnnotationAgentContextPayload
    ) -> dict[str, Any]:
        """Build the ``built`` event payload.

        Deliberately elides the full annotation bodies — SSE subscribers
        that need pixels go through :meth:`last_payload`.
        """

        return {
            "schema_version": UI_ANNOTATION_CONTEXT_SCHEMA_VERSION,
            "session_id": payload.session_id,
            "turn_id": payload.turn_id,
            "built_at": float(payload.built_at),
            "annotation_count": payload.annotation_count,
            "click_count": payload.click_count,
            "rect_count": payload.rect_count,
            "selector_count": payload.selector_count,
            "commented_count": payload.commented_count,
            "has_annotations": payload.has_annotations,
            "warning_count": len(payload.warnings),
        }

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(data))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "ui_annotation_context event callback raised: %s", exc
            )

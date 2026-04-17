"""V2 #6 (issue #318) — Agent visual context injection.

Closes the visual half of the ReAct auto-fix loop: every turn, capture
the sandbox's live preview across the responsive viewport matrix,
encode each PNG to base64, bundle preview errors (from V2 #5) as a
text block, and emit a multimodal message payload that Opus 4.7 can
consume so the agent literally *sees* what the UI looks like.

Where this sits in the V2 stack
--------------------------------

V2 #1 ``ui_sandbox.py`` owns Docker lifecycle primitives.
V2 #2 ``ui_sandbox_lifecycle.py`` orchestrates session-level ensure /
hot-reload / screenshot / teardown.
V2 #3 ``ui_screenshot.py`` is the Playwright engine + single-viewport
service.
V2 #4 ``ui_responsive_viewport.py`` captures the three-viewport matrix
(desktop / tablet / mobile) and returns a
:class:`~backend.ui_responsive_viewport.ResponsiveCaptureReport`.
V2 #5 ``ui_preview_error_bridge.py`` turns dev-server logs into a
:class:`~backend.ui_preview_error_bridge.AgentContextPayload` (error
summary + auto-fix hint).

**V2 #6 (this module)** composes V2 #4 + V2 #5 into the exact shape
Anthropic's multimodal endpoint expects:

  * one ``{"type": "text", "text": ...}`` block summarising the
    preview URL, current route, and preview errors;
  * one ``{"type": "image", "source": {"type": "base64",
    "media_type": "image/png", "data": ...}}`` block per captured
    viewport (up to three — desktop / tablet / mobile).

The orchestration layer (agent loop) asks this module for a
:class:`AgentVisualContextPayload` each ReAct turn, calls
:meth:`AgentVisualContextPayload.to_content_blocks` to get the raw
list of content dicts, and either wraps them in a
``HumanMessage`` via :func:`build_human_message` or hands them
straight to ``llm_adapter.invoke_chat`` / ``stream_chat`` via a
pre-built ``HumanMessage``.

Design decisions
----------------

* **Composition over inheritance.**  :class:`AgentVisualContextBuilder`
  *holds* a :class:`ResponsiveViewportCapture` and (optionally) a
  :class:`PreviewErrorBridge`.  Mirrors V2 #2 / #3 / #4 / #5.
* **Pure content blocks.**  The multimodal message shape is produced
  as ``list[dict]`` by pure helpers; the LangChain ``HumanMessage``
  wrapper is built in a separate function that lazy-imports
  :mod:`backend.llm_adapter` so the core module stays test-friendly
  without pulling in the LangChain graph.
* **Event namespace.**  ``ui_sandbox.agent_visual_context.*`` — four
  topics ``building`` / ``built`` / ``failed`` / ``skipped``.
  Distinct from V2 #2 / #3 / #4 / #5 namespaces so the SSE bus (V2
  row 7) can subscribe on prefix.
* **Byte budgeting.**  Opus 4.7 accepts large multimodal messages but
  SSE frames / token bills balloon fast.
  :data:`DEFAULT_MAX_TOTAL_IMAGE_BYTES` caps the aggregate PNG payload
  per turn; overflow drops the largest captures first (keeping at
  least the first viewport so the agent never gets a text-only
  response).
* **Errors are opt-in.**  Callers that don't wire
  :class:`PreviewErrorBridge` still get a well-formed visual context —
  the text block reads "preview rendered cleanly" and no error
  summary is included.
* **Skipped payloads.**  When the sandbox is down / not ready /
  unreachable the builder produces a *skipped* payload: text-only,
  no images, ``was_skipped=True``.  The agent loop never has to
  branch on "can I get a visual context" vs "can I build a turn".
* **Deterministic rendering.**  The text block is built by a pure
  helper from sorted inputs so golden tests can pin the exact
  string.  The turn_id + built_at travel through so downstream
  auditing ties the multimodal message back to its source turn.
* **Graceful failure.**  :meth:`AgentVisualContextBuilder.build`
  wraps the responsive capture in try/except.  A non-
  :class:`BatchAborted` failure (e.g. V2 #3 engine crash) emits
  ``ui_sandbox.agent_visual_context.failed`` and falls back to a
  skipped payload.  :class:`BatchAborted` bubbles up when
  ``failure_mode="abort"`` so abort-mode callers still see the
  partial report.

Contract (pinned by ``backend/tests/test_ui_agent_visual_context.py``)
---------------------------------------------------------------------

* :data:`UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION` is semver; bump on
  shape changes to :class:`AgentVisualContextImage.to_dict` /
  :class:`AgentVisualContextPayload.to_dict` / ``to_content_blocks``.
* Event names live in the ``ui_sandbox.agent_visual_context.*``
  namespace and never collide with V2 #2-#5.
* :meth:`AgentVisualContextBuilder.build` never raises in
  ``failure_mode="collect"``; failures surface as skipped payloads.
* :meth:`AgentVisualContextPayload.to_content_blocks` returns a list
  whose first element is always the text block; images follow in
  viewport-matrix order.
* Image content blocks use Anthropic's documented base64 shape —
  ``{"type": "image", "source": {"type": "base64", "media_type":
  "image/png", "data": "<b64>"}}``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from backend.ui_preview_error_bridge import (
    UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION,
    AgentContextPayload,
    PreviewErrorBridge,
)
from backend.ui_responsive_viewport import (
    DEFAULT_FAILURE_MODE,
    FAILURE_MODES,
    BatchAborted,
    ResponsiveCaptureReport,
    ResponsiveViewportCapture,
)
from backend.ui_screenshot import (
    MAX_CAPTURE_BYTES,
    ScreenshotCapture,
    ScreenshotError,
    encode_png_base64,
)

logger = logging.getLogger(__name__)


__all__ = [
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
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on shape changes to :class:`AgentVisualContextImage.to_dict`
#: / :class:`AgentVisualContextPayload.to_dict` /
#: :func:`build_content_blocks` output.
UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION = "1.0.0"

#: Anthropic multimodal media-type for PNG captures.  V2 #3 validates
#: PNG signatures before base64 encoding so we can pin this exactly.
DEFAULT_IMAGE_MEDIA_TYPE = "image/png"

#: Anthropic-documented source kind.  The base64 path keeps the
#: entire multimodal message self-contained — no external URLs, no
#: Files-API round trips.
DEFAULT_IMAGE_SOURCE_KIND = "base64"

#: Upper bound on a single captured viewport's PNG bytes.  Mirrors
#: V2 #3 :data:`MAX_CAPTURE_BYTES` so behaviour is consistent top
#: to bottom of the stack.
DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT = MAX_CAPTURE_BYTES

#: Aggregate cap across every image in one payload.  Three 1440×900
#: Next.js dev-server pages come in well under 30 MB; the cap
#: prevents runaway output from flooding the multimodal endpoint.
DEFAULT_MAX_TOTAL_IMAGE_BYTES = MAX_CAPTURE_BYTES * 3

#: Default route captured when callers don't specify one.
DEFAULT_PATH = "/"

#: Deterministic text-block template.  Uses Python ``str.format`` with
#: named placeholders so callers can override via the builder ctor.
DEFAULT_TEXT_PROMPT_TEMPLATE = (
    "You are reviewing the live sandbox preview for session "
    "`{session_id}` (turn `{turn_id}`).\n"
    "Preview URL: {preview_url}\n"
    "Current route: {path}\n"
    "Captured viewports: {viewport_list}\n"
    "{missing_line}"
    "\n"
    "{error_summary}\n"
    "\n"
    "{auto_fix_hint}\n"
    "\n"
    "Inspect the attached {image_count} viewport screenshot"
    "{image_plural} and make targeted code changes that resolve "
    "any listed errors and align the rendered UI with the design "
    "intent.  After your edits the sandbox will hot-reload and the "
    "next turn will include a fresh multimodal capture."
)


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


AGENT_VISUAL_CONTEXT_EVENT_BUILDING = "ui_sandbox.agent_visual_context.building"
AGENT_VISUAL_CONTEXT_EVENT_BUILT = "ui_sandbox.agent_visual_context.built"
AGENT_VISUAL_CONTEXT_EVENT_FAILED = "ui_sandbox.agent_visual_context.failed"
AGENT_VISUAL_CONTEXT_EVENT_SKIPPED = "ui_sandbox.agent_visual_context.skipped"


#: Full roster of topics emitted by this module — V2 row 7 SSE bus
#: subscribes on the ``ui_sandbox.agent_visual_context.`` prefix.
AGENT_VISUAL_CONTEXT_EVENT_TYPES: tuple[str, ...] = (
    AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
    AGENT_VISUAL_CONTEXT_EVENT_BUILT,
    AGENT_VISUAL_CONTEXT_EVENT_FAILED,
    AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class AgentVisualContextError(RuntimeError):
    """Base class for ``ui_agent_visual_context`` errors."""


# ───────────────────────────────────────────────────────────────────
#  Records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentVisualContextImage:
    """One viewport's multimodal-ready image record.

    Holds the base64-encoded PNG plus enough metadata for
    downstream SSE subscribers and for the text summary to reference
    viewports by name / dimensions.
    """

    viewport_name: str
    width: int
    height: int
    byte_len: int
    image_base64: str
    media_type: str = DEFAULT_IMAGE_MEDIA_TYPE
    source_kind: str = DEFAULT_IMAGE_SOURCE_KIND
    captured_at: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.viewport_name, str) or not self.viewport_name.strip():
            raise ValueError("viewport_name must be a non-empty string")
        if not isinstance(self.width, int) or self.width < 1:
            raise ValueError("width must be a positive int")
        if not isinstance(self.height, int) or self.height < 1:
            raise ValueError("height must be a positive int")
        if not isinstance(self.byte_len, int) or self.byte_len < 1:
            raise ValueError("byte_len must be a positive int")
        if not isinstance(self.image_base64, str) or not self.image_base64:
            raise ValueError("image_base64 must be a non-empty string")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("media_type must be a non-empty string")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise ValueError("source_kind must be a non-empty string")
        if self.captured_at < 0:
            raise ValueError("captured_at must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "viewport_name": self.viewport_name,
            "width": int(self.width),
            "height": int(self.height),
            "byte_len": int(self.byte_len),
            "image_base64": self.image_base64,
            "media_type": self.media_type,
            "source_kind": self.source_kind,
            "captured_at": float(self.captured_at),
        }

    def to_content_block(self) -> dict[str, Any]:
        """Render as an Anthropic multimodal ``{"type":"image",...}`` block."""

        return build_image_content_block(self)


@dataclass(frozen=True)
class AgentVisualContextPayload:
    """One turn's visual + error context bundle for the agent.

    Shape is JSON-safe via :meth:`to_dict` (base64 images included
    by design — the whole point of this payload is to ship pixels).
    Multimodal message construction happens via
    :meth:`to_content_blocks` or :func:`build_human_message`.
    """

    session_id: str
    turn_id: str
    built_at: float
    preview_url: str
    path: str
    viewport_matrix: tuple[str, ...]
    images: tuple[AgentVisualContextImage, ...]
    missing_viewports: tuple[str, ...]
    text_prompt: str
    error_summary_markdown: str
    auto_fix_hint: str
    has_blocking_errors: bool = False
    active_error_count: int = 0
    was_skipped: bool = False
    skip_reason: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("turn_id must be a non-empty string")
        if self.built_at < 0:
            raise ValueError("built_at must be non-negative")
        if not isinstance(self.preview_url, str) or not self.preview_url.strip():
            raise ValueError("preview_url must be a non-empty string")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
        if not isinstance(self.viewport_matrix, tuple) or not self.viewport_matrix:
            raise ValueError("viewport_matrix must be a non-empty tuple")
        for name in self.viewport_matrix:
            if not isinstance(name, str) or not name:
                raise ValueError("viewport_matrix entries must be non-empty strings")
        for img in self.images:
            if not isinstance(img, AgentVisualContextImage):
                raise ValueError("images entries must be AgentVisualContextImage")
        for name in self.missing_viewports:
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "missing_viewports entries must be non-empty strings"
                )
        if not isinstance(self.text_prompt, str) or not self.text_prompt:
            raise ValueError("text_prompt must be a non-empty string")
        if not isinstance(self.error_summary_markdown, str):
            raise ValueError("error_summary_markdown must be a string")
        if not isinstance(self.auto_fix_hint, str):
            raise ValueError("auto_fix_hint must be a string")
        if not isinstance(self.has_blocking_errors, bool):
            raise ValueError("has_blocking_errors must be bool")
        if not isinstance(self.active_error_count, int) or self.active_error_count < 0:
            raise ValueError("active_error_count must be a non-negative int")
        if not isinstance(self.was_skipped, bool):
            raise ValueError("was_skipped must be bool")
        if self.skip_reason is not None and (
            not isinstance(self.skip_reason, str) or not self.skip_reason.strip()
        ):
            raise ValueError("skip_reason must be non-empty string or None")
        if self.was_skipped and self.images:
            raise ValueError("skipped payload must have no images")
        if self.was_skipped and self.skip_reason is None:
            raise ValueError("skipped payload requires skip_reason")
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise ValueError("warnings entries must be non-empty strings")
        object.__setattr__(self, "viewport_matrix", tuple(self.viewport_matrix))
        object.__setattr__(self, "images", tuple(self.images))
        object.__setattr__(self, "missing_viewports", tuple(self.missing_viewports))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    @property
    def total_image_bytes(self) -> int:
        return sum(img.byte_len for img in self.images)

    @property
    def has_errors(self) -> bool:
        return self.active_error_count > 0

    @property
    def captured_viewport_names(self) -> tuple[str, ...]:
        return tuple(img.viewport_name for img in self.images)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "built_at": float(self.built_at),
            "preview_url": self.preview_url,
            "path": self.path,
            "viewport_matrix": list(self.viewport_matrix),
            "captured_viewport_names": list(self.captured_viewport_names),
            "missing_viewports": list(self.missing_viewports),
            "image_count": self.image_count,
            "total_image_bytes": self.total_image_bytes,
            "images": [img.to_dict() for img in self.images],
            "text_prompt": self.text_prompt,
            "error_summary_markdown": self.error_summary_markdown,
            "auto_fix_hint": self.auto_fix_hint,
            "has_blocking_errors": bool(self.has_blocking_errors),
            "active_error_count": int(self.active_error_count),
            "has_errors": self.has_errors,
            "was_skipped": bool(self.was_skipped),
            "skip_reason": self.skip_reason,
            "warnings": list(self.warnings),
        }

    def to_content_blocks(self) -> list[dict[str, Any]]:
        """Return the multimodal content block list — text first, then
        one image block per captured viewport, in matrix order."""

        return build_content_blocks(self)


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def encode_capture_to_image(
    capture: ScreenshotCapture,
    *,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT,
) -> AgentVisualContextImage:
    """Convert a V2 #3 :class:`ScreenshotCapture` to a multimodal-ready
    :class:`AgentVisualContextImage`.

    Raises :class:`AgentVisualContextError` if the capture is oversized
    — the builder catches this per-image and records a warning so a
    single oversized viewport doesn't sink the whole payload.
    """

    if not isinstance(capture, ScreenshotCapture):
        raise TypeError("capture must be a ScreenshotCapture")
    if not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive int")
    if capture.byte_len > max_bytes:
        raise AgentVisualContextError(
            f"capture for viewport {capture.viewport.name!r} is "
            f"{capture.byte_len} bytes, exceeds per-viewport cap {max_bytes}"
        )
    try:
        data = encode_png_base64(capture.image_bytes)
    except ScreenshotError as exc:
        raise AgentVisualContextError(
            f"failed to base64-encode capture for {capture.viewport.name!r}: {exc}"
        ) from exc
    return AgentVisualContextImage(
        viewport_name=capture.viewport.name,
        width=int(capture.viewport.width),
        height=int(capture.viewport.height),
        byte_len=int(capture.byte_len),
        image_base64=data,
        media_type=DEFAULT_IMAGE_MEDIA_TYPE,
        source_kind=DEFAULT_IMAGE_SOURCE_KIND,
        captured_at=float(capture.captured_at),
    )


def apply_image_byte_budget(
    images: Sequence[AgentVisualContextImage],
    *,
    max_total_bytes: int,
) -> tuple[tuple[AgentVisualContextImage, ...], tuple[AgentVisualContextImage, ...]]:
    """Trim ``images`` so aggregate ``byte_len`` fits under
    ``max_total_bytes``.

    Drops the largest captures first but always keeps at least the
    first image (matrix-order) so the agent never gets a text-only
    payload when images were actually captured.  Returns the
    ``(kept, dropped)`` tuples in original (matrix) order.
    """

    if not isinstance(max_total_bytes, int) or max_total_bytes < 1:
        raise ValueError("max_total_bytes must be a positive int")
    images_tuple = tuple(images)
    for img in images_tuple:
        if not isinstance(img, AgentVisualContextImage):
            raise TypeError("images entries must be AgentVisualContextImage")
    total = sum(img.byte_len for img in images_tuple)
    if total <= max_total_bytes:
        return images_tuple, ()

    if not images_tuple:
        return (), ()

    # Always keep the first image — a text-only payload is strictly
    # worse than one-image-and-warnings.
    kept_indices: set[int] = {0}
    remaining_budget = max_total_bytes - images_tuple[0].byte_len
    # Greedy: consider the rest in matrix order, include if it fits.
    # Matrix order matters because V2 #4 uses desktop → tablet → mobile
    # which maps to the deterministic text block that references
    # them in that order.
    for idx in range(1, len(images_tuple)):
        img = images_tuple[idx]
        if img.byte_len <= remaining_budget:
            kept_indices.add(idx)
            remaining_budget -= img.byte_len
    kept = tuple(images_tuple[i] for i in range(len(images_tuple)) if i in kept_indices)
    dropped = tuple(
        images_tuple[i] for i in range(len(images_tuple)) if i not in kept_indices
    )
    return kept, dropped


def render_visual_context_text(
    *,
    session_id: str,
    turn_id: str,
    preview_url: str,
    path: str,
    viewport_matrix: Sequence[str],
    captured_viewport_names: Sequence[str],
    missing_viewports: Sequence[str],
    error_summary_markdown: str,
    auto_fix_hint: str,
    template: str = DEFAULT_TEXT_PROMPT_TEMPLATE,
) -> str:
    """Render the deterministic text block shown to the agent.

    Uses ``str.format``-style templating so callers can override via
    :attr:`AgentVisualContextBuilder.text_prompt_template`.  All
    placeholder values are rendered byte-stable so golden tests can
    pin the output.
    """

    if not isinstance(template, str) or not template:
        raise ValueError("template must be a non-empty string")

    image_count = len(captured_viewport_names)
    image_plural = "s" if image_count != 1 else ""
    if captured_viewport_names:
        viewport_list = ", ".join(captured_viewport_names)
    else:
        viewport_list = "(none — sandbox unreachable)"
    if missing_viewports:
        missing_line = (
            f"Viewports not captured this turn: "
            f"{', '.join(missing_viewports)}.\n"
        )
    else:
        missing_line = ""
    return template.format(
        session_id=session_id,
        turn_id=turn_id,
        preview_url=preview_url,
        path=path,
        viewport_list=viewport_list,
        missing_line=missing_line,
        error_summary=error_summary_markdown.rstrip("\n") or "(no error summary)",
        auto_fix_hint=auto_fix_hint.rstrip("\n") or "(no auto-fix hint)",
        image_count=image_count,
        image_plural=image_plural,
    )


def build_text_content_block(text: str) -> dict[str, Any]:
    """Produce an Anthropic ``{"type":"text","text":...}`` block."""

    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    return {"type": "text", "text": text}


def build_image_content_block(image: AgentVisualContextImage) -> dict[str, Any]:
    """Produce an Anthropic ``{"type":"image","source":{...}}`` block.

    Shape matches the documented base64-source format — the LangChain
    Anthropic adapter forwards unknown top-level keys to the SDK, so
    extra fields (viewport_name etc.) go in a sibling ``metadata``
    dict that downstream filters can strip if they want a strict
    block.
    """

    if not isinstance(image, AgentVisualContextImage):
        raise TypeError("image must be an AgentVisualContextImage")
    return {
        "type": "image",
        "source": {
            "type": image.source_kind,
            "media_type": image.media_type,
            "data": image.image_base64,
        },
    }


def build_content_blocks(payload: AgentVisualContextPayload) -> list[dict[str, Any]]:
    """Flatten a payload into a content-block list — text first, then
    one image per captured viewport in matrix order."""

    if not isinstance(payload, AgentVisualContextPayload):
        raise TypeError("payload must be an AgentVisualContextPayload")
    blocks: list[dict[str, Any]] = [build_text_content_block(payload.text_prompt)]
    for img in payload.images:
        blocks.append(build_image_content_block(img))
    return blocks


def build_human_message(payload: AgentVisualContextPayload) -> Any:
    """Wrap ``payload`` as a LangChain ``HumanMessage``.

    Lazy-imports :func:`backend.llm_adapter.HumanMessage` so this
    module stays import-cheap for callers that only need the raw
    content-block list (e.g. SSE serialisation).
    """

    if not isinstance(payload, AgentVisualContextPayload):
        raise TypeError("payload must be an AgentVisualContextPayload")
    from backend.llm_adapter import HumanMessage  # noqa: WPS433 - lazy import

    return HumanMessage(content=build_content_blocks(payload))


# ───────────────────────────────────────────────────────────────────
#  Builder
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]


class AgentVisualContextBuilder:
    """Per-turn multimodal context factory.

    Composition-over-inheritance — this class *has* a
    :class:`ResponsiveViewportCapture` (required) and optionally a
    :class:`PreviewErrorBridge`.  Mirrors V2 #2 / #3 / #4 / #5 layering.

    Typical wire-up::

        builder = AgentVisualContextBuilder(
            responsive=responsive_capture,
            error_bridge=preview_error_bridge,
            event_cb=sse_bus.emit,
        )
        payload = builder.build(
            session_id="sess-1",
            preview_url="http://127.0.0.1:40000/",
            turn_id="react-42",
            path="/pricing",
        )
        message = build_human_message(payload)
        response = llm_adapter.invoke_chat([system, message])

    Thread-safe — counters + last-payload access is guarded by an
    ``RLock``.  The underlying
    :class:`~ResponsiveViewportCapture` and
    :class:`~PreviewErrorBridge` already serialise their own internal
    state so concurrent :meth:`build` calls against different
    sessions are safe; same-session concurrency serialises at the
    V2 #3 engine layer.
    """

    def __init__(
        self,
        *,
        responsive: ResponsiveViewportCapture,
        error_bridge: PreviewErrorBridge | None = None,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        default_matrix: Sequence[str] | None = None,
        default_failure_mode: str = DEFAULT_FAILURE_MODE,
        default_path: str = DEFAULT_PATH,
        max_image_bytes_per_viewport: int = DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT,
        max_total_image_bytes: int = DEFAULT_MAX_TOTAL_IMAGE_BYTES,
        text_prompt_template: str = DEFAULT_TEXT_PROMPT_TEMPLATE,
    ) -> None:
        if responsive is None:
            raise TypeError("responsive must be a ResponsiveViewportCapture")
        if not hasattr(responsive, "capture_all"):
            raise TypeError("responsive must implement capture_all(...)")
        if error_bridge is not None and not isinstance(
            error_bridge, PreviewErrorBridge
        ):
            raise TypeError(
                "error_bridge must be a PreviewErrorBridge or None"
            )
        if default_failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"default_failure_mode must be one of {FAILURE_MODES}, "
                f"got {default_failure_mode!r}"
            )
        if not isinstance(default_path, str) or not default_path.startswith("/"):
            raise ValueError("default_path must start with '/'")
        if (
            not isinstance(max_image_bytes_per_viewport, int)
            or max_image_bytes_per_viewport < 1
        ):
            raise ValueError("max_image_bytes_per_viewport must be a positive int")
        if not isinstance(max_total_image_bytes, int) or max_total_image_bytes < 1:
            raise ValueError("max_total_image_bytes must be a positive int")
        if not isinstance(text_prompt_template, str) or not text_prompt_template:
            raise ValueError("text_prompt_template must be a non-empty string")
        if default_matrix is None:
            default_matrix_tuple = tuple(responsive.default_matrix)
        else:
            default_matrix_tuple = tuple(default_matrix)
            if not default_matrix_tuple:
                raise ValueError("default_matrix must not be empty")
            for name in default_matrix_tuple:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(
                        "default_matrix entries must be non-empty strings"
                    )

        self._responsive = responsive
        self._error_bridge = error_bridge
        self._clock = clock
        self._event_cb = event_cb
        self._default_matrix: tuple[str, ...] = default_matrix_tuple
        self._default_failure_mode = default_failure_mode
        self._default_path = default_path
        self._max_image_bytes_per_viewport = int(max_image_bytes_per_viewport)
        self._max_total_image_bytes = int(max_total_image_bytes)
        self._text_prompt_template = text_prompt_template

        self._lock = threading.RLock()
        self._turn_counter = 0
        self._build_count = 0
        self._skipped_count = 0
        self._failed_count = 0
        self._last_payload: AgentVisualContextPayload | None = None

    # ─────────────── Accessors ───────────────

    @property
    def responsive(self) -> ResponsiveViewportCapture:
        return self._responsive

    @property
    def error_bridge(self) -> PreviewErrorBridge | None:
        return self._error_bridge

    @property
    def default_matrix(self) -> tuple[str, ...]:
        return self._default_matrix

    @property
    def default_failure_mode(self) -> str:
        return self._default_failure_mode

    @property
    def default_path(self) -> str:
        return self._default_path

    @property
    def max_image_bytes_per_viewport(self) -> int:
        return self._max_image_bytes_per_viewport

    @property
    def max_total_image_bytes(self) -> int:
        return self._max_total_image_bytes

    @property
    def text_prompt_template(self) -> str:
        return self._text_prompt_template

    def build_count(self) -> int:
        with self._lock:
            return self._build_count

    def skipped_count(self) -> int:
        with self._lock:
            return self._skipped_count

    def failed_count(self) -> int:
        with self._lock:
            return self._failed_count

    def last_payload(self) -> AgentVisualContextPayload | None:
        with self._lock:
            return self._last_payload

    # ─────────────── Core API ───────────────

    def build(
        self,
        *,
        session_id: str,
        preview_url: str,
        turn_id: str | None = None,
        path: str | None = None,
        viewport_matrix: Sequence[str] | None = None,
        failure_mode: str | None = None,
        full_page: bool = False,
        include_errors: bool = True,
        scan_errors: bool = False,
    ) -> AgentVisualContextPayload:
        """Build one turn's multimodal visual + error context.

        ``failure_mode="collect"`` (default): a partial responsive
        capture (e.g. mobile viewport timed out) produces a payload
        with the viewports that succeeded and ``missing_viewports``
        listing the rest.  The builder never raises.

        ``failure_mode="abort"``: if any viewport fails,
        :class:`BatchAborted` propagates (carrying the partial
        report on ``exc.report``).  Callers opt in when they want
        a hard gate (e.g. CI capturing a golden baseline).

        When ``scan_errors=True`` and ``error_bridge`` is wired, the
        builder first calls :meth:`PreviewErrorBridge.scan` so the
        agent sees the latest state of compile / runtime errors.
        Default is ``False`` so callers that already run a
        ``start_watch`` background loop don't double-scan.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(preview_url, str) or not preview_url.strip():
            raise ValueError("preview_url must be a non-empty string")
        effective_path = path if path is not None else self._default_path
        if not isinstance(effective_path, str) or not effective_path.startswith("/"):
            raise ValueError("path must start with '/'")
        effective_failure_mode = (
            failure_mode if failure_mode is not None else self._default_failure_mode
        )
        if effective_failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"failure_mode must be one of {FAILURE_MODES}, got "
                f"{effective_failure_mode!r}"
            )
        effective_matrix = (
            tuple(viewport_matrix)
            if viewport_matrix is not None
            else self._default_matrix
        )
        if not effective_matrix:
            raise ValueError("viewport_matrix must not be empty")
        effective_turn_id = self._resolve_turn_id(turn_id)

        self._emit(
            AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
            {
                "session_id": session_id,
                "turn_id": effective_turn_id,
                "preview_url": preview_url,
                "path": effective_path,
                "viewport_matrix": list(effective_matrix),
                "failure_mode": effective_failure_mode,
                "at": float(self._clock()),
            },
        )

        # ---- Preview errors (V2 #5) ----
        error_payload: AgentContextPayload | None = None
        warnings: list[str] = []
        if include_errors and self._error_bridge is not None:
            if scan_errors:
                try:
                    self._error_bridge.scan(session_id)
                except Exception as exc:  # pragma: no cover - scan is defensive
                    warnings.append(f"error_scan_failed: {exc}")
            try:
                error_payload = self._error_bridge.build_agent_context(
                    session_id, turn_id=effective_turn_id
                )
            except Exception as exc:
                warnings.append(f"error_context_failed: {exc}")

        # ---- Responsive capture (V2 #4) ----
        try:
            report = self._responsive.capture_all(
                session_id=session_id,
                preview_url=preview_url,
                path=effective_path,
                matrix=list(effective_matrix),
                failure_mode=effective_failure_mode,
                full_page=full_page,
            )
        except BatchAborted as exc:
            # abort-mode — partial report is attached; fold whatever
            # we got into a skipped-style payload but still raise so
            # callers that chose abort see the failure.
            self._emit(
                AGENT_VISUAL_CONTEXT_EVENT_FAILED,
                {
                    "session_id": session_id,
                    "turn_id": effective_turn_id,
                    "preview_url": preview_url,
                    "path": effective_path,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "at": float(self._clock()),
                    "partial_report": (
                        exc.report.to_dict() if exc.report is not None else None
                    ),
                },
            )
            with self._lock:
                self._failed_count += 1
            raise
        except Exception as exc:
            # Responsive capture died for a non-abort reason — engine
            # crash, mis-wired stub, etc.  Fall back to a skipped
            # payload so the agent loop still gets a well-formed
            # turn input.
            with self._lock:
                self._failed_count += 1
            self._emit(
                AGENT_VISUAL_CONTEXT_EVENT_FAILED,
                {
                    "session_id": session_id,
                    "turn_id": effective_turn_id,
                    "preview_url": preview_url,
                    "path": effective_path,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc) or type(exc).__name__,
                    "at": float(self._clock()),
                },
            )
            return self._build_skipped_internal(
                session_id=session_id,
                turn_id=effective_turn_id,
                preview_url=preview_url,
                path=effective_path,
                viewport_matrix=effective_matrix,
                skip_reason=(
                    f"responsive_capture_failed: {type(exc).__name__}: "
                    f"{str(exc) or type(exc).__name__}"
                ),
                error_payload=error_payload,
                warnings=tuple(warnings),
            )

        # ---- Convert captures → multimodal images ----
        images: list[AgentVisualContextImage] = []
        for outcome in report.outcomes:
            if not outcome.success or outcome.capture is None:
                continue
            try:
                img = encode_capture_to_image(
                    outcome.capture,
                    max_bytes=self._max_image_bytes_per_viewport,
                )
            except AgentVisualContextError as exc:
                warnings.append(f"image_encode_failed:{outcome.viewport_name}:{exc}")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(
                    f"image_encode_unexpected:{outcome.viewport_name}:{exc}"
                )
                continue
            images.append(img)

        # ---- Apply aggregate byte budget ----
        kept_images, dropped_images = apply_image_byte_budget(
            images, max_total_bytes=self._max_total_image_bytes
        )
        for dropped in dropped_images:
            warnings.append(
                f"image_dropped_budget:{dropped.viewport_name}:{dropped.byte_len}"
            )

        captured_names = tuple(img.viewport_name for img in kept_images)
        missing = tuple(n for n in effective_matrix if n not in captured_names)

        # ---- Render text block ----
        if error_payload is not None:
            error_summary = error_payload.summary_markdown
            auto_fix_hint = error_payload.auto_fix_hint
            has_blocking = error_payload.has_blocking_errors
            active_error_count = error_payload.error_count
        else:
            error_summary = "### Preview errors\n\nNo error bridge wired.\n"
            auto_fix_hint = (
                "Preview errors are not being tracked this turn — "
                "proceed with the next design task."
            )
            has_blocking = False
            active_error_count = 0

        text_prompt = render_visual_context_text(
            session_id=session_id,
            turn_id=effective_turn_id,
            preview_url=preview_url,
            path=effective_path,
            viewport_matrix=effective_matrix,
            captured_viewport_names=captured_names,
            missing_viewports=missing,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            template=self._text_prompt_template,
        )

        now = self._clock()
        payload = AgentVisualContextPayload(
            session_id=session_id,
            turn_id=effective_turn_id,
            built_at=now,
            preview_url=preview_url,
            path=effective_path,
            viewport_matrix=effective_matrix,
            images=tuple(kept_images),
            missing_viewports=missing,
            text_prompt=text_prompt,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            has_blocking_errors=has_blocking,
            active_error_count=active_error_count,
            was_skipped=False,
            skip_reason=None,
            warnings=tuple(warnings),
        )

        with self._lock:
            self._build_count += 1
            self._last_payload = payload

        self._emit(
            AGENT_VISUAL_CONTEXT_EVENT_BUILT,
            self._envelope_for_event(payload, report=report),
        )
        return payload

    def build_skipped(
        self,
        *,
        session_id: str,
        preview_url: str,
        skip_reason: str,
        turn_id: str | None = None,
        path: str | None = None,
        viewport_matrix: Sequence[str] | None = None,
    ) -> AgentVisualContextPayload:
        """Produce a text-only payload — sandbox unreachable / idle /
        teardown pending.

        Emits ``ui_sandbox.agent_visual_context.skipped`` so SSE
        subscribers can render a placeholder in the UI.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(preview_url, str) or not preview_url.strip():
            raise ValueError("preview_url must be a non-empty string")
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            raise ValueError("skip_reason must be a non-empty string")
        effective_path = path if path is not None else self._default_path
        if not isinstance(effective_path, str) or not effective_path.startswith("/"):
            raise ValueError("path must start with '/'")
        effective_matrix = (
            tuple(viewport_matrix)
            if viewport_matrix is not None
            else self._default_matrix
        )
        if not effective_matrix:
            raise ValueError("viewport_matrix must not be empty")
        effective_turn_id = self._resolve_turn_id(turn_id)

        return self._build_skipped_internal(
            session_id=session_id,
            turn_id=effective_turn_id,
            preview_url=preview_url,
            path=effective_path,
            viewport_matrix=effective_matrix,
            skip_reason=skip_reason,
            error_payload=None,
            warnings=(),
        )

    def _build_skipped_internal(
        self,
        *,
        session_id: str,
        turn_id: str,
        preview_url: str,
        path: str,
        viewport_matrix: Sequence[str],
        skip_reason: str,
        error_payload: AgentContextPayload | None,
        warnings: tuple[str, ...],
    ) -> AgentVisualContextPayload:
        if error_payload is not None:
            error_summary = error_payload.summary_markdown
            auto_fix_hint = error_payload.auto_fix_hint
            has_blocking = error_payload.has_blocking_errors
            active_error_count = error_payload.error_count
        else:
            error_summary = (
                "### Preview errors\n\nPreview capture skipped — no error "
                "snapshot available.\n"
            )
            auto_fix_hint = (
                "Visual context is skipped this turn — proceed from "
                "code state and retry the capture after the next "
                "hot-reload."
            )
            has_blocking = False
            active_error_count = 0

        matrix_tuple = tuple(viewport_matrix)
        missing = matrix_tuple  # Nothing captured, everything missing.
        text_prompt = render_visual_context_text(
            session_id=session_id,
            turn_id=turn_id,
            preview_url=preview_url,
            path=path,
            viewport_matrix=matrix_tuple,
            captured_viewport_names=(),
            missing_viewports=missing,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            template=self._text_prompt_template,
        )

        now = self._clock()
        payload = AgentVisualContextPayload(
            session_id=session_id,
            turn_id=turn_id,
            built_at=now,
            preview_url=preview_url,
            path=path,
            viewport_matrix=matrix_tuple,
            images=(),
            missing_viewports=missing,
            text_prompt=text_prompt,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            has_blocking_errors=has_blocking,
            active_error_count=active_error_count,
            was_skipped=True,
            skip_reason=skip_reason,
            warnings=warnings,
        )

        with self._lock:
            self._skipped_count += 1
            self._last_payload = payload

        self._emit(
            AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "preview_url": preview_url,
                "path": path,
                "skip_reason": skip_reason,
                "viewport_matrix": list(matrix_tuple),
                "at": float(now),
                "schema_version": UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            },
        )
        return payload

    # ─────────────── Multimodal shortcuts ───────────────

    def build_message(
        self,
        *,
        session_id: str,
        preview_url: str,
        **kwargs: Any,
    ) -> tuple[AgentVisualContextPayload, Any]:
        """Convenience: build payload then return
        ``(payload, HumanMessage)`` so callers can shove the message
        straight into ``llm_adapter.invoke_chat``.
        """

        payload = self.build(session_id=session_id, preview_url=preview_url, **kwargs)
        return payload, build_human_message(payload)

    # ─────────────── Snapshot ───────────────

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe operator snapshot — counters + last payload
        metadata.  The last payload's images are *not* inlined to keep
        the snapshot lean; callers that want pixels go through
        :meth:`last_payload`.
        """

        with self._lock:
            last = self._last_payload
            last_summary: dict[str, Any] | None = None
            if last is not None:
                last_summary = {
                    "session_id": last.session_id,
                    "turn_id": last.turn_id,
                    "built_at": float(last.built_at),
                    "path": last.path,
                    "image_count": last.image_count,
                    "total_image_bytes": last.total_image_bytes,
                    "captured_viewport_names": list(last.captured_viewport_names),
                    "missing_viewports": list(last.missing_viewports),
                    "has_errors": last.has_errors,
                    "has_blocking_errors": bool(last.has_blocking_errors),
                    "was_skipped": bool(last.was_skipped),
                    "skip_reason": last.skip_reason,
                    "warning_count": len(last.warnings),
                }
            return {
                "schema_version": UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
                "error_bridge_schema_version": (
                    UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION
                    if self._error_bridge is not None
                    else None
                ),
                "default_matrix": list(self._default_matrix),
                "default_failure_mode": self._default_failure_mode,
                "default_path": self._default_path,
                "max_image_bytes_per_viewport": self._max_image_bytes_per_viewport,
                "max_total_image_bytes": self._max_total_image_bytes,
                "build_count": int(self._build_count),
                "skipped_count": int(self._skipped_count),
                "failed_count": int(self._failed_count),
                "turn_counter": int(self._turn_counter),
                "last_payload": last_summary,
                "now": float(self._clock()),
            }

    # ─────────────── Internal plumbing ───────────────

    def _resolve_turn_id(self, turn_id: str | None) -> str:
        if turn_id is not None:
            if not isinstance(turn_id, str) or not turn_id.strip():
                raise ValueError("turn_id must be a non-empty string or None")
            return turn_id
        with self._lock:
            self._turn_counter += 1
            return f"avc-turn-{self._turn_counter:06d}"

    def _envelope_for_event(
        self,
        payload: AgentVisualContextPayload,
        *,
        report: ResponsiveCaptureReport | None = None,
    ) -> dict[str, Any]:
        """Build the ``built`` event payload.

        Deliberately *does not* inline image base64 — SSE subscribers
        would choke on hundreds of KB per frame.  Callers that need
        images use :meth:`AgentVisualContextPayload.to_dict` directly.
        """

        envelope: dict[str, Any] = {
            "schema_version": UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "session_id": payload.session_id,
            "turn_id": payload.turn_id,
            "built_at": float(payload.built_at),
            "preview_url": payload.preview_url,
            "path": payload.path,
            "viewport_matrix": list(payload.viewport_matrix),
            "captured_viewport_names": list(payload.captured_viewport_names),
            "missing_viewports": list(payload.missing_viewports),
            "image_count": payload.image_count,
            "total_image_bytes": payload.total_image_bytes,
            "has_errors": payload.has_errors,
            "has_blocking_errors": bool(payload.has_blocking_errors),
            "active_error_count": int(payload.active_error_count),
            "warning_count": len(payload.warnings),
        }
        if report is not None:
            envelope["responsive_success_count"] = int(report.success_count)
            envelope["responsive_failure_count"] = int(report.failure_count)
            envelope["responsive_is_complete_success"] = bool(
                report.is_complete_success
            )
        return envelope

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(data))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "ui_agent_visual_context event callback raised: %s", exc
            )

"""V6 #5 (issue #322) — Mobile agent visual context injection.

Closes the visual half of the **mobile** ReAct auto-fix loop: every
turn, capture one PNG per device target via :mod:`backend.mobile_screenshot`
(V6 #2), encode each PNG to base64, optionally bundle the latest build
errors (from :mod:`backend.mobile_sandbox` V6 #1 — or any other
caller-supplied source) as a text block, and emit a multimodal message
payload that Opus 4.7 can consume so the agent literally *sees* the
emulator/simulator after every edit.

Where this sits in the V6 stack
-------------------------------

* V6 #1 ``mobile_sandbox.py`` owns the per-session
  ``build → install → run → screenshot`` lifecycle and surfaces
  ``BuildError`` records on its ``MobileSandboxInstance``.
* V6 #2 ``mobile_screenshot.py`` is the standalone
  ``capture(ScreenshotRequest) -> ScreenshotResult`` primitive that
  turns ``adb shell screencap`` / ``xcrun simctl io screenshot`` into
  one deterministic call.
* V6 #3 ``components/omnisight/device-frame.tsx`` renders one PNG
  inside an iPhone / Pixel / iPad CSS frame.
* V6 #4 ``components/omnisight/device-grid.tsx`` fans the same PNG
  across 6+ device frames in a responsive grid.

**V6 #5 (this module)** composes V6 #1 + V6 #2 into the exact shape
Anthropic's multimodal endpoint expects:

  * one ``{"type": "text", "text": ...}`` block summarising the session,
    the device matrix that was captured, build-error context, and the
    self-evaluation hint;
  * one ``{"type": "image", "source": {"type": "base64",
    "media_type": "image/png", "data": ...}}`` block per *successfully
    captured* device target.

The orchestration layer (mobile agent loop) asks this module for a
:class:`MobileAgentVisualContextPayload` each ReAct turn, calls
:meth:`MobileAgentVisualContextPayload.to_content_blocks` to get the
raw list of content dicts, and either wraps them in a ``HumanMessage``
via :func:`build_human_message` or hands them straight to
``llm_adapter.invoke_chat`` / ``stream_chat`` via a pre-built
``HumanMessage``.

Design decisions
----------------

* **Composition over inheritance.**
  :class:`MobileAgentVisualContextBuilder` *holds* a screenshot
  ``capture_fn`` (defaults to :func:`backend.mobile_screenshot.capture`)
  and (optionally) a pluggable ``error_source`` callable so wiring in
  build-error context from V6 #1 is one line, but the module remains
  testable in pure isolation.
* **Per-device targets.**  Mobile UIs have to be evaluated on every
  hardware variant (Dynamic Island vs. physical home button vs.
  hole-punch vs. fold).  We model the matrix as
  :class:`MobileDeviceTarget` records — each one carries its own
  ``platform``, ``udid_or_serial``, native ``screen_width`` /
  ``screen_height``, and a stable ``device_id`` that mirrors the
  frontend ``DEVICE_PROFILE_IDS`` (V6 #3).
* **Pure content blocks.**  The multimodal message shape is produced
  as ``list[dict]`` by pure helpers; the LangChain ``HumanMessage``
  wrapper is built in a separate function that lazy-imports
  :mod:`backend.llm_adapter` so the core module stays test-friendly
  without pulling in the LangChain graph.
* **Event namespace.**  ``mobile_sandbox.agent_visual_context.*`` —
  four topics ``building`` / ``built`` / ``failed`` / ``skipped``.
  Distinct from V2 ``ui_sandbox.agent_visual_context.*`` and V6 #1
  ``mobile_sandbox.<state>`` namespaces so the SSE bus can subscribe
  on prefix without colliding with web-side traffic.
* **Status awareness.**  V6 #2 ``ScreenshotResult`` has four terminal
  statuses (``pass`` / ``fail`` / ``skip`` / ``mock``).  Only ``pass``
  produces a multimodal image; ``fail`` / ``mock`` / ``skip`` go into
  ``missing_devices`` with a ``device_status_summary`` line so the
  agent reads "iPhone 15 capture failed: adb timed out" instead of
  silently losing one of its visual references.
* **Byte budgeting.**  Mobile screenshots are physically larger than
  web (1179×2556 ≈ 1.5 MB after PNG compression).
  :data:`DEFAULT_MAX_TOTAL_IMAGE_BYTES` caps the aggregate PNG payload
  per turn; overflow drops the largest captures first while always
  keeping the first image so the agent never gets a text-only
  response.
* **Errors are opt-in.**  Callers that don't wire an ``error_source``
  still get a well-formed visual context — the text block reads
  "no build errors tracked" and no error summary is included.
* **Skipped payloads.**  When the sandbox is down / not ready /
  unreachable the builder produces a *skipped* payload: text-only,
  no images, ``was_skipped=True``.  The agent loop never has to
  branch on "can I get a visual context" vs. "can I build a turn".
* **Deterministic rendering.**  The text block is built by a pure
  helper from sorted inputs so golden tests can pin the exact
  string.  The ``turn_id`` + ``built_at`` travel through so
  downstream auditing ties the multimodal message back to its
  source turn.
* **Graceful failure.**  :meth:`MobileAgentVisualContextBuilder.build`
  wraps each capture in try/except.  A capture-function crash falls
  back to ``status="fail"`` for that one device — the rest of the
  matrix still runs, the agent still gets visual context for the
  devices that worked.  A truly unrecoverable failure (capture_fn
  itself raised at the dispatch layer) emits
  ``mobile_sandbox.agent_visual_context.failed`` and falls back to a
  skipped payload.

Contract (pinned by ``backend/tests/test_mobile_agent_visual_context.py``)
--------------------------------------------------------------------------

* :data:`MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION` is semver; bump
  on shape changes to :class:`MobileAgentVisualContextImage.to_dict`
  / :class:`MobileAgentVisualContextPayload.to_dict` /
  :func:`build_content_blocks` output.
* Event names live in the ``mobile_sandbox.agent_visual_context.*``
  namespace and never collide with V6 #1 ``mobile_sandbox.<state>``
  topics or V2 ``ui_sandbox.agent_visual_context.*`` topics.
* :meth:`MobileAgentVisualContextBuilder.build` never raises in
  ``failure_mode="collect"``; failures surface as a payload with
  ``missing_devices`` populated or as a skipped payload.
* :meth:`MobileAgentVisualContextPayload.to_content_blocks` returns
  a list whose first element is always the text block; images
  follow in matrix order.
* Image content blocks use Anthropic's documented base64 shape —
  ``{"type": "image", "source": {"type": "base64", "media_type":
  "image/png", "data": "<b64>"}}``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from backend.mobile_screenshot import (
    DEFAULT_IOS_UDID,
    MOBILE_SCREENSHOT_SCHEMA_VERSION,
    ScreenshotRequest,
    ScreenshotResult,
    ScreenshotStatus,
    capture as default_screenshot_capture,
    parse_png_dimensions,
)

logger = logging.getLogger(__name__)


__all__ = [
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
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────


#: Bump on shape changes to :class:`MobileAgentVisualContextImage.to_dict`
#: / :class:`MobileAgentVisualContextPayload.to_dict` /
#: :func:`build_content_blocks` output.
MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION = "1.0.0"

#: Anthropic multimodal media-type for PNG captures.  V6 #2
#: validates PNG signatures via ``parse_png_dimensions`` so we can pin
#: this exactly.
DEFAULT_IMAGE_MEDIA_TYPE = "image/png"

#: Anthropic-documented source kind.  The base64 path keeps the entire
#: multimodal message self-contained — no external URLs, no Files-API
#: round trips.
DEFAULT_IMAGE_SOURCE_KIND = "base64"

#: Per-device byte cap.  Mobile native screenshots run 0.4–2 MB after
#: PNG compression depending on resolution; 5 MB gives plenty of
#: headroom for tablet form factors without exposing the model to a
#: pathological 50 MB capture.
DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE = 5 * 1024 * 1024

#: Aggregate cap across every image in one payload.  Six devices ×
#: ~2 MB ≈ 12 MB; 30 MB lets callers fan out to 6+ device matrix
#: without the cap kicking in for typical mobile pages.
DEFAULT_MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024

#: Failure-mode vocabulary.  ``collect`` (default) folds capture
#: failures into the payload; ``abort`` raises
#: :class:`MobileAgentVisualContextError` if any device fails so CI
#: callers can hard-fail the turn.
FAILURE_MODES: tuple[str, ...] = ("collect", "abort")

#: Default failure mode — agents use ``collect`` so a single dead
#: emulator never sinks the whole ReAct turn.
DEFAULT_FAILURE_MODE = "collect"


# ───────────────────────────────────────────────────────────────────
#  Default device matrix
# ───────────────────────────────────────────────────────────────────


# Forward declaration — :class:`MobileDeviceTarget` is defined further
# down; the const tuple is built at module load *after* the class.
DEFAULT_DEVICE_TARGETS: tuple["MobileDeviceTarget", ...] = ()


#: Deterministic text-block template.  Uses Python ``str.format`` with
#: named placeholders so callers can override via the builder ctor.
DEFAULT_TEXT_PROMPT_TEMPLATE = (
    "You are reviewing the live mobile sandbox for session "
    "`{session_id}` (turn `{turn_id}`).\n"
    "Device matrix: {device_list}\n"
    "Captured devices: {captured_list}\n"
    "{missing_line}"
    "\n"
    "{device_status_summary}\n"
    "\n"
    "{error_summary}\n"
    "\n"
    "{auto_fix_hint}\n"
    "\n"
    "Inspect the attached {image_count} device screenshot"
    "{image_plural} and make targeted code changes that resolve "
    "any listed build errors and align the rendered UI with the "
    "design intent on every captured device.  After your edits the "
    "sandbox will rebuild and the next turn will include a fresh "
    "multimodal capture across the same matrix."
)


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING = (
    "mobile_sandbox.agent_visual_context.building"
)
MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT = (
    "mobile_sandbox.agent_visual_context.built"
)
MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED = (
    "mobile_sandbox.agent_visual_context.failed"
)
MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED = (
    "mobile_sandbox.agent_visual_context.skipped"
)


#: Full roster of topics emitted by this module — SSE bus subscribes
#: on the ``mobile_sandbox.agent_visual_context.`` prefix.
MOBILE_AGENT_VISUAL_CONTEXT_EVENT_TYPES: tuple[str, ...] = (
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED,
    MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileAgentVisualContextError(RuntimeError):
    """Base class for ``mobile_agent_visual_context`` errors.  Routers
    can catch this single type to translate every failure into one
    structured HTTP / event payload."""


# ───────────────────────────────────────────────────────────────────
#  Records
# ───────────────────────────────────────────────────────────────────


_SAFE_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_SUPPORTED_TARGET_PLATFORMS: frozenset[str] = frozenset({"ios", "android"})


@dataclass(frozen=True)
class MobileDeviceTarget:
    """One row of the device matrix the agent reviews each turn.

    Carries the routing data needed to dispatch a V6 #2
    :class:`backend.mobile_screenshot.ScreenshotRequest` (``platform``
    + ``udid_or_serial``) plus the descriptive metadata the text block
    references (``label``, ``device_id``, native pixel dimensions).

    Frozen + deterministic — two identical targets produce
    byte-identical state on disk and in the prompt cache.
    """

    device_id: str
    platform: str
    udid_or_serial: str = ""
    label: str = ""
    screen_width: int = 0
    screen_height: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        if not _SAFE_DEVICE_ID_RE.fullmatch(self.device_id):
            raise ValueError(
                "device_id must match [A-Za-z0-9_.-]{1,64} — got "
                f"{self.device_id!r}"
            )
        plat = (self.platform or "").strip().lower()
        if plat not in _SUPPORTED_TARGET_PLATFORMS:
            raise ValueError(
                f"platform must be one of {sorted(_SUPPORTED_TARGET_PLATFORMS)!r}"
                f" — got {self.platform!r}"
            )
        object.__setattr__(self, "platform", plat)
        if not isinstance(self.udid_or_serial, str):
            raise ValueError("udid_or_serial must be a string")
        if not isinstance(self.label, str):
            raise ValueError("label must be a string")
        if not isinstance(self.screen_width, int) or self.screen_width < 0:
            raise ValueError("screen_width must be a non-negative int")
        if not isinstance(self.screen_height, int) or self.screen_height < 0:
            raise ValueError("screen_height must be a non-negative int")
        if not self.label:
            object.__setattr__(self, "label", self.device_id)

    @property
    def is_ios(self) -> bool:
        return self.platform == "ios"

    @property
    def is_android(self) -> bool:
        return self.platform == "android"

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "platform": self.platform,
            "udid_or_serial": self.udid_or_serial,
            "label": self.label,
            "screen_width": int(self.screen_width),
            "screen_height": int(self.screen_height),
        }


# Now that :class:`MobileDeviceTarget` is bound, build the default
# matrix.  Mirrors the IDs used by V6 #3 ``DEVICE_PROFILES`` so the
# frontend's device-frame component can map straight to
# ``MobileAgentVisualContextImage.device_id`` without translation.
DEFAULT_DEVICE_TARGETS = (
    MobileDeviceTarget(
        device_id="iphone-15",
        platform="ios",
        udid_or_serial=DEFAULT_IOS_UDID,
        label="iPhone 15",
        screen_width=1179,
        screen_height=2556,
    ),
    MobileDeviceTarget(
        device_id="pixel-8",
        platform="android",
        udid_or_serial="",
        label="Pixel 8",
        screen_width=1080,
        screen_height=2400,
    ),
)


@dataclass(frozen=True)
class MobileBuildErrorSummary:
    """Caller-supplied summary of build / runtime errors for the turn.

    Lightweight value type so the builder does not have to depend on
    :mod:`backend.mobile_sandbox` directly — the orchestration layer
    builds this from ``MobileSandboxInstance.snapshot()`` (or from a
    Gradle / Xcode log scrape) and hands it to the builder.

    Frozen + JSON-safe via :meth:`to_dict`.
    """

    summary_markdown: str = ""
    auto_fix_hint: str = ""
    has_blocking_errors: bool = False
    active_error_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.summary_markdown, str):
            raise ValueError("summary_markdown must be a string")
        if not isinstance(self.auto_fix_hint, str):
            raise ValueError("auto_fix_hint must be a string")
        if not isinstance(self.has_blocking_errors, bool):
            raise ValueError("has_blocking_errors must be bool")
        if (
            not isinstance(self.active_error_count, int)
            or self.active_error_count < 0
        ):
            raise ValueError("active_error_count must be a non-negative int")

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_markdown": self.summary_markdown,
            "auto_fix_hint": self.auto_fix_hint,
            "has_blocking_errors": bool(self.has_blocking_errors),
            "active_error_count": int(self.active_error_count),
        }


@dataclass(frozen=True)
class MobileAgentVisualContextImage:
    """One device's multimodal-ready image record.

    Holds the base64-encoded PNG plus enough metadata for downstream
    SSE subscribers and for the text summary to reference devices by
    label / dimensions.
    """

    device_id: str
    platform: str
    label: str
    width: int
    height: int
    byte_len: int
    image_base64: str
    media_type: str = DEFAULT_IMAGE_MEDIA_TYPE
    source_kind: str = DEFAULT_IMAGE_SOURCE_KIND
    captured_at: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        if self.platform not in _SUPPORTED_TARGET_PLATFORMS:
            raise ValueError(
                f"platform must be one of {sorted(_SUPPORTED_TARGET_PLATFORMS)!r}"
            )
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("label must be a non-empty string")
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
            "schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "device_id": self.device_id,
            "platform": self.platform,
            "label": self.label,
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
class MobileAgentVisualContextPayload:
    """One turn's mobile visual + error context bundle for the agent.

    Shape is JSON-safe via :meth:`to_dict` (base64 images included
    by design — the whole point of this payload is to ship pixels).
    Multimodal message construction happens via
    :meth:`to_content_blocks` or :func:`build_human_message`.
    """

    session_id: str
    turn_id: str
    built_at: float
    device_matrix: tuple[MobileDeviceTarget, ...]
    images: tuple[MobileAgentVisualContextImage, ...]
    missing_devices: tuple[str, ...]
    device_results: tuple[ScreenshotResult, ...]
    text_prompt: str
    device_status_summary: str
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
        if not isinstance(self.device_matrix, tuple) or not self.device_matrix:
            raise ValueError("device_matrix must be a non-empty tuple")
        for t in self.device_matrix:
            if not isinstance(t, MobileDeviceTarget):
                raise ValueError(
                    "device_matrix entries must be MobileDeviceTarget"
                )
        for img in self.images:
            if not isinstance(img, MobileAgentVisualContextImage):
                raise ValueError(
                    "images entries must be MobileAgentVisualContextImage"
                )
        for name in self.missing_devices:
            if not isinstance(name, str) or not name:
                raise ValueError(
                    "missing_devices entries must be non-empty strings"
                )
        for r in self.device_results:
            if not isinstance(r, ScreenshotResult):
                raise ValueError(
                    "device_results entries must be ScreenshotResult"
                )
        if not isinstance(self.text_prompt, str) or not self.text_prompt:
            raise ValueError("text_prompt must be a non-empty string")
        if not isinstance(self.device_status_summary, str):
            raise ValueError("device_status_summary must be a string")
        if not isinstance(self.error_summary_markdown, str):
            raise ValueError("error_summary_markdown must be a string")
        if not isinstance(self.auto_fix_hint, str):
            raise ValueError("auto_fix_hint must be a string")
        if not isinstance(self.has_blocking_errors, bool):
            raise ValueError("has_blocking_errors must be bool")
        if (
            not isinstance(self.active_error_count, int)
            or self.active_error_count < 0
        ):
            raise ValueError("active_error_count must be a non-negative int")
        if not isinstance(self.was_skipped, bool):
            raise ValueError("was_skipped must be bool")
        if self.skip_reason is not None and (
            not isinstance(self.skip_reason, str) or not self.skip_reason.strip()
        ):
            raise ValueError("skip_reason must be a non-empty string or None")
        if self.was_skipped and self.images:
            raise ValueError("skipped payload must have no images")
        if self.was_skipped and self.skip_reason is None:
            raise ValueError("skipped payload requires skip_reason")
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise ValueError("warnings entries must be non-empty strings")
        object.__setattr__(self, "device_matrix", tuple(self.device_matrix))
        object.__setattr__(self, "images", tuple(self.images))
        object.__setattr__(self, "missing_devices", tuple(self.missing_devices))
        object.__setattr__(self, "device_results", tuple(self.device_results))
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
    def captured_device_ids(self) -> tuple[str, ...]:
        return tuple(img.device_id for img in self.images)

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(t.device_id for t in self.device_matrix)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "built_at": float(self.built_at),
            "device_matrix": [t.to_dict() for t in self.device_matrix],
            "device_ids": list(self.device_ids),
            "captured_device_ids": list(self.captured_device_ids),
            "missing_devices": list(self.missing_devices),
            "image_count": self.image_count,
            "total_image_bytes": self.total_image_bytes,
            "images": [img.to_dict() for img in self.images],
            "device_results": [r.to_dict() for r in self.device_results],
            "text_prompt": self.text_prompt,
            "device_status_summary": self.device_status_summary,
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
        one image block per captured device, in matrix order."""

        return build_content_blocks(self)


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def encode_screenshot_to_image(
    target: MobileDeviceTarget,
    result: ScreenshotResult,
    *,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE,
) -> MobileAgentVisualContextImage:
    """Convert a V6 #2 :class:`ScreenshotResult` into a multimodal-ready
    :class:`MobileAgentVisualContextImage`.

    Only ``status == ScreenshotStatus.passed`` results produce a
    valid image — ``mock`` / ``fail`` / ``skip`` results raise
    :class:`MobileAgentVisualContextError` so the caller routes them
    into ``missing_devices`` instead.

    Raises :class:`MobileAgentVisualContextError` when the capture is
    oversized, when the bytes are missing, or when the status is
    non-passing — the builder catches this per-device and records a
    warning so a single bad capture doesn't sink the whole payload.
    """

    if not isinstance(target, MobileDeviceTarget):
        raise TypeError("target must be a MobileDeviceTarget")
    if not isinstance(result, ScreenshotResult):
        raise TypeError("result must be a ScreenshotResult")
    if not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive int")
    if result.status is not ScreenshotStatus.passed:
        raise MobileAgentVisualContextError(
            f"capture for {target.device_id!r} did not pass — status="
            f"{result.status.value}"
        )
    if not result.png_bytes:
        raise MobileAgentVisualContextError(
            f"capture for {target.device_id!r} carries no png_bytes; "
            "set ScreenshotRequest.attach_bytes=True"
        )
    byte_len = len(result.png_bytes)
    if byte_len > max_bytes:
        raise MobileAgentVisualContextError(
            f"capture for {target.device_id!r} is {byte_len} bytes, "
            f"exceeds per-device cap {max_bytes}"
        )
    width = int(result.width or 0)
    height = int(result.height or 0)
    if width <= 0 or height <= 0:
        sniffed_w, sniffed_h = parse_png_dimensions(result.png_bytes)
        if sniffed_w > 0 and sniffed_h > 0:
            width, height = sniffed_w, sniffed_h
    if width <= 0 or height <= 0:
        raise MobileAgentVisualContextError(
            f"capture for {target.device_id!r} has no dimensions"
        )
    try:
        encoded = base64.b64encode(bytes(result.png_bytes)).decode("ascii")
    except Exception as exc:  # pragma: no cover - defensive
        raise MobileAgentVisualContextError(
            f"failed to base64-encode capture for {target.device_id!r}: {exc}"
        ) from exc
    return MobileAgentVisualContextImage(
        device_id=target.device_id,
        platform=target.platform,
        label=target.label,
        width=width,
        height=height,
        byte_len=byte_len,
        image_base64=encoded,
        media_type=DEFAULT_IMAGE_MEDIA_TYPE,
        source_kind=DEFAULT_IMAGE_SOURCE_KIND,
        captured_at=float(result.captured_at),
    )


def apply_image_byte_budget(
    images: Sequence[MobileAgentVisualContextImage],
    *,
    max_total_bytes: int,
) -> tuple[
    tuple[MobileAgentVisualContextImage, ...],
    tuple[MobileAgentVisualContextImage, ...],
]:
    """Trim ``images`` so aggregate ``byte_len`` fits under
    ``max_total_bytes``.

    Always keeps at least the first image (matrix-order) so the agent
    never gets a text-only payload when images were actually captured.
    Returns the ``(kept, dropped)`` tuples in original (matrix) order.
    """

    if not isinstance(max_total_bytes, int) or max_total_bytes < 1:
        raise ValueError("max_total_bytes must be a positive int")
    images_tuple = tuple(images)
    for img in images_tuple:
        if not isinstance(img, MobileAgentVisualContextImage):
            raise TypeError(
                "images entries must be MobileAgentVisualContextImage"
            )
    total = sum(img.byte_len for img in images_tuple)
    if total <= max_total_bytes:
        return images_tuple, ()

    if not images_tuple:
        return (), ()

    # Always keep the first image — text-only degrade is strictly worse.
    kept_indices: set[int] = {0}
    remaining_budget = max_total_bytes - images_tuple[0].byte_len
    # Greedy: consider the rest in matrix order, include if it fits.
    for idx in range(1, len(images_tuple)):
        img = images_tuple[idx]
        if img.byte_len <= remaining_budget:
            kept_indices.add(idx)
            remaining_budget -= img.byte_len
    kept = tuple(
        images_tuple[i] for i in range(len(images_tuple)) if i in kept_indices
    )
    dropped = tuple(
        images_tuple[i]
        for i in range(len(images_tuple))
        if i not in kept_indices
    )
    return kept, dropped


def render_device_status_summary(
    targets: Sequence[MobileDeviceTarget],
    results_by_id: Mapping[str, ScreenshotResult],
) -> str:
    """Render a stable per-device status block referenced by the text
    prompt.

    Output shape::

        ### Device capture status
        - iphone-15 (iOS, iPhone 15): pass — 1179x2556 480000 B
        - pixel-8 (Android, Pixel 8): mock — adb not on PATH

    Deterministic for matching inputs; safe when the matrix is empty.
    """

    if not targets:
        return "### Device capture status\n\nNo devices in the matrix.\n"
    lines = ["### Device capture status\n"]
    for target in targets:
        result = results_by_id.get(target.device_id)
        if result is None:
            lines.append(
                f"- {target.device_id} ({_human_platform(target.platform)},"
                f" {target.label}): no result\n"
            )
            continue
        head = (
            f"- {target.device_id} ({_human_platform(target.platform)},"
            f" {target.label}): {result.status.value}"
        )
        body_parts: list[str] = []
        if result.status is ScreenshotStatus.passed and result.width and result.height:
            body_parts.append(
                f"{int(result.width)}x{int(result.height)} {int(result.size_bytes)} B"
            )
        if result.detail:
            body_parts.append(result.detail)
        if body_parts:
            lines.append(f"{head} — {' — '.join(body_parts)}\n")
        else:
            lines.append(f"{head}\n")
    return "".join(lines)


def _human_platform(platform: str) -> str:
    if platform == "ios":
        return "iOS"
    if platform == "android":
        return "Android"
    return platform


def render_visual_context_text(
    *,
    session_id: str,
    turn_id: str,
    device_matrix: Sequence[MobileDeviceTarget],
    captured_device_ids: Sequence[str],
    missing_devices: Sequence[str],
    device_status_summary: str,
    error_summary_markdown: str,
    auto_fix_hint: str,
    template: str = DEFAULT_TEXT_PROMPT_TEMPLATE,
) -> str:
    """Render the deterministic text block shown to the agent.

    Uses ``str.format``-style templating so callers can override via
    :attr:`MobileAgentVisualContextBuilder.text_prompt_template`.  All
    placeholder values are rendered byte-stable so golden tests can
    pin the output.
    """

    if not isinstance(template, str) or not template:
        raise ValueError("template must be a non-empty string")

    image_count = len(captured_device_ids)
    image_plural = "s" if image_count != 1 else ""
    if device_matrix:
        device_list = ", ".join(t.device_id for t in device_matrix)
    else:
        device_list = "(empty)"
    if captured_device_ids:
        captured_list = ", ".join(captured_device_ids)
    else:
        captured_list = "(none — sandbox unreachable)"
    if missing_devices:
        missing_line = (
            f"Devices not captured this turn: "
            f"{', '.join(missing_devices)}.\n"
        )
    else:
        missing_line = ""
    return template.format(
        session_id=session_id,
        turn_id=turn_id,
        device_list=device_list,
        captured_list=captured_list,
        missing_line=missing_line,
        device_status_summary=device_status_summary.rstrip("\n")
        or "(no device status)",
        error_summary=error_summary_markdown.rstrip("\n")
        or "(no error summary)",
        auto_fix_hint=auto_fix_hint.rstrip("\n") or "(no auto-fix hint)",
        image_count=image_count,
        image_plural=image_plural,
    )


def build_text_content_block(text: str) -> dict[str, Any]:
    """Produce an Anthropic ``{"type":"text","text":...}`` block."""

    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    return {"type": "text", "text": text}


def build_image_content_block(
    image: MobileAgentVisualContextImage,
) -> dict[str, Any]:
    """Produce an Anthropic ``{"type":"image","source":{...}}`` block.

    Shape matches the documented base64-source format — extra metadata
    (device_id, label) lives on the parent payload, not on the content
    block, so the LangChain Anthropic adapter can forward this dict
    verbatim to the SDK.
    """

    if not isinstance(image, MobileAgentVisualContextImage):
        raise TypeError("image must be a MobileAgentVisualContextImage")
    return {
        "type": "image",
        "source": {
            "type": image.source_kind,
            "media_type": image.media_type,
            "data": image.image_base64,
        },
    }


def build_content_blocks(
    payload: MobileAgentVisualContextPayload,
) -> list[dict[str, Any]]:
    """Flatten a payload into a content-block list — text first, then
    one image per captured device in matrix order."""

    if not isinstance(payload, MobileAgentVisualContextPayload):
        raise TypeError("payload must be a MobileAgentVisualContextPayload")
    blocks: list[dict[str, Any]] = [
        build_text_content_block(payload.text_prompt)
    ]
    for img in payload.images:
        blocks.append(build_image_content_block(img))
    return blocks


def build_human_message(payload: MobileAgentVisualContextPayload) -> Any:
    """Wrap ``payload`` as a LangChain ``HumanMessage``.

    Lazy-imports :func:`backend.llm_adapter.HumanMessage` so this
    module stays import-cheap for callers that only need the raw
    content-block list (e.g. SSE serialisation).
    """

    if not isinstance(payload, MobileAgentVisualContextPayload):
        raise TypeError("payload must be a MobileAgentVisualContextPayload")
    from backend.llm_adapter import HumanMessage  # noqa: WPS433 - lazy import

    return HumanMessage(content=build_content_blocks(payload))


# ───────────────────────────────────────────────────────────────────
#  Builder
# ───────────────────────────────────────────────────────────────────


EventCallback = Callable[[str, Mapping[str, Any]], None]
ScreenshotCaptureFn = Callable[[ScreenshotRequest], ScreenshotResult]
ErrorSourceFn = Callable[[str], "MobileBuildErrorSummary | None"]
RequestFactoryFn = Callable[
    [str, MobileDeviceTarget, str, bool], ScreenshotRequest
]


def _default_request_factory(
    session_id: str,
    target: MobileDeviceTarget,
    output_path: str,
    attach_bytes: bool,
) -> ScreenshotRequest:
    """Construct a V6 #2 :class:`ScreenshotRequest` from a target.

    Routes ``udid_or_serial`` to the right field per platform and
    forces ``attach_bytes=True`` by default so the encoder can read
    bytes off the result without re-reading from disk.
    """

    if target.is_ios:
        return ScreenshotRequest(
            session_id=session_id,
            platform="ios",
            output_path=output_path,
            ios_udid=target.udid_or_serial or DEFAULT_IOS_UDID,
            attach_bytes=attach_bytes,
        )
    return ScreenshotRequest(
        session_id=session_id,
        platform="android",
        output_path=output_path,
        android_serial=target.udid_or_serial,
        attach_bytes=attach_bytes,
    )


class MobileAgentVisualContextBuilder:
    """Per-turn multimodal context factory for the mobile agent loop.

    Composition-over-inheritance — this class *holds* a
    ``capture_fn`` (defaults to :func:`backend.mobile_screenshot.capture`)
    and (optionally) an ``error_source`` callable.  Tests inject a
    fake ``capture_fn`` that returns deterministic
    :class:`ScreenshotResult` records without ever touching a real
    emulator.

    Typical wire-up::

        builder = MobileAgentVisualContextBuilder(
            event_cb=sse_bus.emit,
            error_source=lambda sid: build_summary_for(
                manager.get(sid)
            ),
        )
        payload = builder.build(
            session_id="sess-1",
            output_dir="/var/run/omnisight/captures",
            turn_id="react-42",
        )
        message = build_human_message(payload)
        response = llm_adapter.invoke_chat([system, message])

    Thread-safe — counters + last-payload access is guarded by an
    ``RLock``.
    """

    def __init__(
        self,
        *,
        capture_fn: ScreenshotCaptureFn = default_screenshot_capture,
        error_source: ErrorSourceFn | None = None,
        request_factory: RequestFactoryFn = _default_request_factory,
        clock: Callable[[], float] = time.time,
        event_cb: EventCallback | None = None,
        default_devices: Sequence[MobileDeviceTarget] | None = None,
        default_failure_mode: str = DEFAULT_FAILURE_MODE,
        max_image_bytes_per_device: int = DEFAULT_MAX_IMAGE_BYTES_PER_DEVICE,
        max_total_image_bytes: int = DEFAULT_MAX_TOTAL_IMAGE_BYTES,
        text_prompt_template: str = DEFAULT_TEXT_PROMPT_TEMPLATE,
    ) -> None:
        if not callable(capture_fn):
            raise TypeError("capture_fn must be callable")
        if error_source is not None and not callable(error_source):
            raise TypeError("error_source must be callable or None")
        if not callable(request_factory):
            raise TypeError("request_factory must be callable")
        if default_failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"default_failure_mode must be one of {FAILURE_MODES!r}, "
                f"got {default_failure_mode!r}"
            )
        if (
            not isinstance(max_image_bytes_per_device, int)
            or max_image_bytes_per_device < 1
        ):
            raise ValueError("max_image_bytes_per_device must be a positive int")
        if not isinstance(max_total_image_bytes, int) or max_total_image_bytes < 1:
            raise ValueError("max_total_image_bytes must be a positive int")
        if not isinstance(text_prompt_template, str) or not text_prompt_template:
            raise ValueError("text_prompt_template must be a non-empty string")
        if default_devices is None:
            default_targets = tuple(DEFAULT_DEVICE_TARGETS)
        else:
            default_targets = tuple(default_devices)
            if not default_targets:
                raise ValueError("default_devices must not be empty")
            for t in default_targets:
                if not isinstance(t, MobileDeviceTarget):
                    raise ValueError(
                        "default_devices entries must be MobileDeviceTarget"
                    )

        self._capture_fn = capture_fn
        self._error_source = error_source
        self._request_factory = request_factory
        self._clock = clock
        self._event_cb = event_cb
        self._default_devices: tuple[MobileDeviceTarget, ...] = default_targets
        self._default_failure_mode = default_failure_mode
        self._max_image_bytes_per_device = int(max_image_bytes_per_device)
        self._max_total_image_bytes = int(max_total_image_bytes)
        self._text_prompt_template = text_prompt_template

        self._lock = threading.RLock()
        self._turn_counter = 0
        self._build_count = 0
        self._skipped_count = 0
        self._failed_count = 0
        self._last_payload: MobileAgentVisualContextPayload | None = None

    # ─────────────── Accessors ───────────────

    @property
    def capture_fn(self) -> ScreenshotCaptureFn:
        return self._capture_fn

    @property
    def error_source(self) -> ErrorSourceFn | None:
        return self._error_source

    @property
    def request_factory(self) -> RequestFactoryFn:
        return self._request_factory

    @property
    def default_devices(self) -> tuple[MobileDeviceTarget, ...]:
        return self._default_devices

    @property
    def default_failure_mode(self) -> str:
        return self._default_failure_mode

    @property
    def max_image_bytes_per_device(self) -> int:
        return self._max_image_bytes_per_device

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

    def last_payload(self) -> MobileAgentVisualContextPayload | None:
        with self._lock:
            return self._last_payload

    # ─────────────── Core API ───────────────

    def build(
        self,
        *,
        session_id: str,
        output_dir: str,
        turn_id: str | None = None,
        devices: Sequence[MobileDeviceTarget] | None = None,
        failure_mode: str | None = None,
        attach_bytes: bool = True,
        include_errors: bool = True,
    ) -> MobileAgentVisualContextPayload:
        """Build one turn's mobile multimodal visual + error context.

        ``failure_mode="collect"`` (default): per-device capture
        failures are recorded in ``missing_devices`` + the device
        status summary; the builder never raises.

        ``failure_mode="abort"``: if any device fails capture (status
        is not ``pass``), :class:`MobileAgentVisualContextError`
        propagates so CI callers can hard-fail the turn.

        ``attach_bytes=False`` keeps the on-disk PNG path but skips
        the bytes — useful when the agent only needs to know which
        captures succeeded (the next turn's visual context will pull
        bytes via the standard path).
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ValueError("output_dir must be a non-empty string")
        if not os.path.isabs(output_dir):
            raise ValueError(
                f"output_dir must be absolute — got {output_dir!r}"
            )
        if not isinstance(attach_bytes, bool):
            raise ValueError("attach_bytes must be bool")
        if not isinstance(include_errors, bool):
            raise ValueError("include_errors must be bool")
        effective_failure_mode = (
            failure_mode if failure_mode is not None else self._default_failure_mode
        )
        if effective_failure_mode not in FAILURE_MODES:
            raise ValueError(
                f"failure_mode must be one of {FAILURE_MODES!r}, got "
                f"{effective_failure_mode!r}"
            )
        effective_devices = (
            tuple(devices) if devices is not None else self._default_devices
        )
        if not effective_devices:
            raise ValueError("devices must not be empty")
        for t in effective_devices:
            if not isinstance(t, MobileDeviceTarget):
                raise ValueError(
                    "devices entries must be MobileDeviceTarget"
                )
        effective_turn_id = self._resolve_turn_id(turn_id)

        self._emit(
            MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILDING,
            {
                "session_id": session_id,
                "turn_id": effective_turn_id,
                "output_dir": output_dir,
                "device_ids": [t.device_id for t in effective_devices],
                "failure_mode": effective_failure_mode,
                "at": float(self._clock()),
            },
        )

        # ---- Build error context (V6 #1 plug-in) ----
        error_summary, auto_fix_hint, has_blocking, active_count, warnings = (
            self._collect_error_context(
                session_id=session_id, include_errors=include_errors
            )
        )

        # ---- Per-device capture loop ----
        results: list[ScreenshotResult] = []
        results_by_id: dict[str, ScreenshotResult] = {}
        images: list[MobileAgentVisualContextImage] = []
        missing: list[str] = []
        capture_warnings: list[str] = []

        for target in effective_devices:
            output_path = os.path.join(
                output_dir,
                f"{session_id}-{target.device_id}-{effective_turn_id}.png",
            )
            try:
                request = self._request_factory(
                    session_id, target, output_path, attach_bytes
                )
                if not isinstance(request, ScreenshotRequest):
                    raise TypeError(
                        "request_factory must return a ScreenshotRequest, got "
                        f"{type(request)!r}"
                    )
                result = self._capture_fn(request)
                if not isinstance(result, ScreenshotResult):
                    raise TypeError(
                        "capture_fn must return a ScreenshotResult, got "
                        f"{type(result)!r}"
                    )
            except Exception as exc:
                # Per-device crash — fold into a synthetic fail result so
                # the rest of the matrix still proceeds.
                capture_warnings.append(
                    f"capture_crashed:{target.device_id}:{type(exc).__name__}:{exc}"
                )
                result = ScreenshotResult(
                    session_id=session_id,
                    platform=target.platform,
                    status=ScreenshotStatus.fail,
                    path=output_path,
                    captured_at=float(self._clock()),
                    detail=f"capture_fn raised {type(exc).__name__}: {exc}",
                )
            results.append(result)
            results_by_id[target.device_id] = result

            if result.status is ScreenshotStatus.passed:
                try:
                    img = encode_screenshot_to_image(
                        target,
                        result,
                        max_bytes=self._max_image_bytes_per_device,
                    )
                except MobileAgentVisualContextError as exc:
                    capture_warnings.append(
                        f"image_encode_failed:{target.device_id}:{exc}"
                    )
                    missing.append(target.device_id)
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    capture_warnings.append(
                        f"image_encode_unexpected:{target.device_id}:{exc}"
                    )
                    missing.append(target.device_id)
                    continue
                images.append(img)
            else:
                missing.append(target.device_id)

        # ---- Apply aggregate byte budget ----
        kept_images, dropped_images = apply_image_byte_budget(
            images, max_total_bytes=self._max_total_image_bytes
        )
        for dropped in dropped_images:
            capture_warnings.append(
                f"image_dropped_budget:{dropped.device_id}:{dropped.byte_len}"
            )
            missing.append(dropped.device_id)

        # ---- Abort-mode hard-stop ----
        if effective_failure_mode == "abort" and missing:
            self._emit(
                MOBILE_AGENT_VISUAL_CONTEXT_EVENT_FAILED,
                {
                    "session_id": session_id,
                    "turn_id": effective_turn_id,
                    "output_dir": output_dir,
                    "missing_devices": list(missing),
                    "at": float(self._clock()),
                },
            )
            with self._lock:
                self._failed_count += 1
            raise MobileAgentVisualContextError(
                "abort-mode capture failure for devices: "
                f"{', '.join(missing)}"
            )

        captured_ids = tuple(img.device_id for img in kept_images)

        # ---- Render text block ----
        device_status = render_device_status_summary(
            effective_devices, results_by_id
        )
        text_prompt = render_visual_context_text(
            session_id=session_id,
            turn_id=effective_turn_id,
            device_matrix=effective_devices,
            captured_device_ids=captured_ids,
            missing_devices=tuple(missing),
            device_status_summary=device_status,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            template=self._text_prompt_template,
        )

        now = self._clock()
        all_warnings = tuple(warnings) + tuple(capture_warnings)
        payload = MobileAgentVisualContextPayload(
            session_id=session_id,
            turn_id=effective_turn_id,
            built_at=now,
            device_matrix=effective_devices,
            images=tuple(kept_images),
            missing_devices=tuple(missing),
            device_results=tuple(results),
            text_prompt=text_prompt,
            device_status_summary=device_status,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            has_blocking_errors=has_blocking,
            active_error_count=active_count,
            was_skipped=False,
            skip_reason=None,
            warnings=all_warnings,
        )

        with self._lock:
            self._build_count += 1
            self._last_payload = payload

        self._emit(
            MOBILE_AGENT_VISUAL_CONTEXT_EVENT_BUILT,
            self._envelope_for_event(payload),
        )
        return payload

    def build_skipped(
        self,
        *,
        session_id: str,
        skip_reason: str,
        turn_id: str | None = None,
        devices: Sequence[MobileDeviceTarget] | None = None,
    ) -> MobileAgentVisualContextPayload:
        """Produce a text-only payload — sandbox unreachable / idle /
        teardown pending.

        Emits ``mobile_sandbox.agent_visual_context.skipped`` so SSE
        subscribers can render a placeholder in the UI.
        """

        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            raise ValueError("skip_reason must be a non-empty string")
        effective_devices = (
            tuple(devices) if devices is not None else self._default_devices
        )
        if not effective_devices:
            raise ValueError("devices must not be empty")
        for t in effective_devices:
            if not isinstance(t, MobileDeviceTarget):
                raise ValueError(
                    "devices entries must be MobileDeviceTarget"
                )
        effective_turn_id = self._resolve_turn_id(turn_id)

        return self._build_skipped_internal(
            session_id=session_id,
            turn_id=effective_turn_id,
            devices=effective_devices,
            skip_reason=skip_reason,
            error_summary=None,
            auto_fix_hint=None,
            has_blocking=False,
            active_count=0,
            warnings=(),
        )

    def _build_skipped_internal(
        self,
        *,
        session_id: str,
        turn_id: str,
        devices: Sequence[MobileDeviceTarget],
        skip_reason: str,
        error_summary: str | None,
        auto_fix_hint: str | None,
        has_blocking: bool,
        active_count: int,
        warnings: tuple[str, ...],
    ) -> MobileAgentVisualContextPayload:
        if error_summary is None:
            error_summary = (
                "### Build errors\n\nMobile capture skipped — no error "
                "snapshot available.\n"
            )
        if auto_fix_hint is None:
            auto_fix_hint = (
                "Visual context is skipped this turn — proceed from "
                "code state and retry the capture after the next "
                "rebuild."
            )

        device_tuple = tuple(devices)
        missing = tuple(t.device_id for t in device_tuple)
        device_status = render_device_status_summary(device_tuple, {})
        text_prompt = render_visual_context_text(
            session_id=session_id,
            turn_id=turn_id,
            device_matrix=device_tuple,
            captured_device_ids=(),
            missing_devices=missing,
            device_status_summary=device_status,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            template=self._text_prompt_template,
        )

        now = self._clock()
        payload = MobileAgentVisualContextPayload(
            session_id=session_id,
            turn_id=turn_id,
            built_at=now,
            device_matrix=device_tuple,
            images=(),
            missing_devices=missing,
            device_results=(),
            text_prompt=text_prompt,
            device_status_summary=device_status,
            error_summary_markdown=error_summary,
            auto_fix_hint=auto_fix_hint,
            has_blocking_errors=has_blocking,
            active_error_count=active_count,
            was_skipped=True,
            skip_reason=skip_reason,
            warnings=warnings,
        )

        with self._lock:
            self._skipped_count += 1
            self._last_payload = payload

        self._emit(
            MOBILE_AGENT_VISUAL_CONTEXT_EVENT_SKIPPED,
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "skip_reason": skip_reason,
                "device_ids": list(missing),
                "at": float(now),
                "schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            },
        )
        return payload

    # ─────────────── Multimodal shortcuts ───────────────

    def build_message(
        self,
        *,
        session_id: str,
        output_dir: str,
        **kwargs: Any,
    ) -> tuple[MobileAgentVisualContextPayload, Any]:
        """Convenience: build payload then return
        ``(payload, HumanMessage)`` so callers can shove the message
        straight into ``llm_adapter.invoke_chat``.
        """

        payload = self.build(
            session_id=session_id, output_dir=output_dir, **kwargs
        )
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
                    "image_count": last.image_count,
                    "total_image_bytes": last.total_image_bytes,
                    "captured_device_ids": list(last.captured_device_ids),
                    "missing_devices": list(last.missing_devices),
                    "has_errors": last.has_errors,
                    "has_blocking_errors": bool(last.has_blocking_errors),
                    "was_skipped": bool(last.was_skipped),
                    "skip_reason": last.skip_reason,
                    "warning_count": len(last.warnings),
                }
            return {
                "schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
                "screenshot_schema_version": MOBILE_SCREENSHOT_SCHEMA_VERSION,
                "default_device_ids": [
                    t.device_id for t in self._default_devices
                ],
                "default_failure_mode": self._default_failure_mode,
                "max_image_bytes_per_device": self._max_image_bytes_per_device,
                "max_total_image_bytes": self._max_total_image_bytes,
                "build_count": int(self._build_count),
                "skipped_count": int(self._skipped_count),
                "failed_count": int(self._failed_count),
                "turn_counter": int(self._turn_counter),
                "error_source_wired": self._error_source is not None,
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
            return f"mavc-turn-{self._turn_counter:06d}"

    def _collect_error_context(
        self, *, session_id: str, include_errors: bool
    ) -> tuple[str, str, bool, int, list[str]]:
        warnings: list[str] = []
        if not include_errors or self._error_source is None:
            return (
                "### Build errors\n\nNo error source wired.\n",
                "Build errors are not being tracked this turn — "
                "proceed with the next mobile design task.",
                False,
                0,
                warnings,
            )
        try:
            summary = self._error_source(session_id)
        except Exception as exc:
            warnings.append(f"error_source_failed: {exc}")
            return (
                "### Build errors\n\nError source raised — see warnings.\n",
                "Build errors could not be retrieved this turn.",
                False,
                0,
                warnings,
            )
        if summary is None:
            return (
                "### Build errors\n\nNo build errors reported.\n",
                "Mobile sandbox reported a clean build.",
                False,
                0,
                warnings,
            )
        if not isinstance(summary, MobileBuildErrorSummary):
            warnings.append(
                f"error_source_bad_type:{type(summary).__name__}"
            )
            return (
                "### Build errors\n\nError source returned an invalid type.\n",
                "Build errors could not be parsed this turn.",
                False,
                0,
                warnings,
            )
        text = (
            summary.summary_markdown
            or "### Build errors\n\nNo build errors reported.\n"
        )
        hint = (
            summary.auto_fix_hint
            or "Mobile sandbox reported a clean build."
        )
        return text, hint, bool(summary.has_blocking_errors), int(
            summary.active_error_count
        ), warnings

    def _envelope_for_event(
        self, payload: MobileAgentVisualContextPayload
    ) -> dict[str, Any]:
        """Build the ``built`` event payload.

        Deliberately *does not* inline image base64 — SSE subscribers
        would choke on hundreds of KB per frame.  Callers that need
        images use :meth:`MobileAgentVisualContextPayload.to_dict`
        directly.
        """

        return {
            "schema_version": MOBILE_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION,
            "session_id": payload.session_id,
            "turn_id": payload.turn_id,
            "built_at": float(payload.built_at),
            "device_ids": list(payload.device_ids),
            "captured_device_ids": list(payload.captured_device_ids),
            "missing_devices": list(payload.missing_devices),
            "image_count": payload.image_count,
            "total_image_bytes": payload.total_image_bytes,
            "has_errors": payload.has_errors,
            "has_blocking_errors": bool(payload.has_blocking_errors),
            "active_error_count": int(payload.active_error_count),
            "warning_count": len(payload.warnings),
        }

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._event_cb is None:
            return
        try:
            self._event_cb(event_type, dict(data))
        except Exception as exc:  # pragma: no cover - callback must not kill us
            logger.warning(
                "mobile_agent_visual_context event callback raised: %s", exc
            )

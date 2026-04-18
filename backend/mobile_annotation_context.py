"""V7 row 1732 (#323 first bullet) — Mobile annotation → agent context.

Server-side twin of the frontend Mobile visual annotator
(``components/omnisight/mobile-visual-annotator.tsx``).  Every time the
operator marks up the device-frame screenshot with a rect or click
pin, the frontend emits a payload list shaped like::

    {
      type: "click" | "rect",
      platform: "ios" | "android" | "flutter" | "react-native",
      framework: "swiftui" | "jetpack-compose" | "flutter" | "react-native",
      fileExt: ".swift" | ".kt" | ".dart" | ".tsx",
      device: DeviceProfileId,
      screenWidth: int,          # native pixels, portrait
      screenHeight: int,         # native pixels, portrait
      boundingBox: {x, y, w, h}, # normalised [0, 1]
      nativePixelBox: {x, y, w, h},
      componentHint: string | null,
      comment: string,
    }

This module parses that payload, validates it, and produces a
framework-specific markdown context block the mobile agent can drop
straight into its next ReAct turn.  Unlike V3 #2's web
``ui_annotation_context.py`` — which only discriminates on
``click`` / ``rect`` — we also steer the agent toward the right *file
type* and *identifier vocabulary* (SwiftUI accessibility identifier,
Compose test-tag, Flutter ``Key``, RN ``testID``) so one annotation
batch translates into one coherent set of native edits.

Why a separate module rather than extending ``ui_annotation_context``
---------------------------------------------------------------------

* The web side operates on CSS selectors and DOM element inspection.
  The mobile side has no DOM — it has native view hierarchies.  Mixing
  the two would force every annotation entry to carry optional fields
  for both vocabularies and make the prompt template branch on
  platform, which defeats the point of a pinned template.
* The mobile payload carries native-pixel geometry the web side has
  no analog for.  Co-locating mobile-specific helpers keeps the web
  module lean.
* Two modules, two sets of contract tests, two SSE event namespaces.
  A regression on one side cannot destabilise the other.

Design decisions
----------------

* **Pure-function core.**  Parse / transform / render are side-effect
  free; only :class:`MobileAnnotationContextBuilder` holds state.
* **Framework-aware markdown.**  The rendered block speaks the
  target platform's dialect: SwiftUI annotations reference
  ``accessibilityIdentifier``; Compose speaks ``Modifier.testTag``;
  Flutter names ``Key`` / widget classes; RN expects ``testID``.
  The agent skill already knows these conventions, so the markdown
  cues are hints, not hard contracts.
* **Strict validation.**  Any shape violation (unknown platform,
  bad box, negative pixel) raises
  :class:`MobileAnnotationContextError` — the FastAPI router layer
  catches this (it's a ``ValueError`` subclass) and returns 422.
* **Deterministic output.**  Two calls with byte-identical input
  produce byte-identical markdown — required for the Anthropic
  prompt cache.
* **Empty-list is legitimate.**  The operator may submit with zero
  annotations ("just re-render") — the builder returns an ``empty``
  event instead of raising.  Downstream analytics can count no-op
  turns.

Contract (pinned by ``backend/tests/test_mobile_annotation_context.py``)
-----------------------------------------------------------------------

* :data:`MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION` is semver.  Bump
  on shape changes to any ``to_dict`` or ``render_*`` output.
* Field names on the payload (``platform`` / ``framework`` /
  ``fileExt`` / ``device`` / ``screenWidth`` / ``screenHeight`` /
  ``boundingBox`` / ``nativePixelBox`` / ``componentHint`` /
  ``comment`` / ``type``) match the frontend byte-for-byte.
* :meth:`MobileAnnotationContextBuilder.build` never raises for
  well-formed input — malformed input raises
  :class:`MobileAnnotationContextError`.
* :func:`render_annotations_markdown` is byte-stable — golden tests
  pin the exact output.
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
    "MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION",
    "MOBILE_PLATFORMS",
    "MOBILE_FRAMEWORKS",
    "PLATFORM_TO_FRAMEWORK",
    "FRAMEWORK_TO_FILE_EXT",
    "ANNOTATION_TYPE_CLICK",
    "ANNOTATION_TYPE_RECT",
    "ANNOTATION_TYPES",
    "DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE",
    "MOBILE_ANNOTATION_EVENT_BUILDING",
    "MOBILE_ANNOTATION_EVENT_BUILT",
    "MOBILE_ANNOTATION_EVENT_EMPTY",
    "MOBILE_ANNOTATION_EVENT_TYPES",
    "MobileAnnotationContextError",
    "MobileAnnotationPayload",
    "MobileAnnotationBundle",
    "MobileAnnotationContextBuilder",
    "mobile_annotation_from_dict",
    "mobile_annotations_from_list",
    "render_mobile_annotation_entry",
    "render_mobile_annotations_markdown",
    "build_mobile_text_content_block",
    "resolve_framework",
    "resolve_file_ext",
]


# ───────────────────────────────────────────────────────────────────
#  Constants
# ───────────────────────────────────────────────────────────────────

#: Bump on shape changes to any ``to_dict`` or ``render_*`` output in
#: this module.  Independent of the web-side schema version —
#: regressions cannot destabilise the web channel.
MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION = "1.0.0"


#: Workspace-side platform vocabulary — matches the ``id`` field of
#: ``DEFAULT_MOBILE_PLATFORMS`` in ``workspace-navigation-sidebar.tsx``.
MOBILE_PLATFORMS: tuple[str, ...] = ("ios", "android", "flutter", "react-native")

#: Framework identifiers the agent-side skills key off.
MOBILE_FRAMEWORKS: tuple[str, ...] = (
    "swiftui",
    "jetpack-compose",
    "flutter",
    "react-native",
)

#: Platform → framework routing.
PLATFORM_TO_FRAMEWORK: dict[str, str] = {
    "ios": "swiftui",
    "android": "jetpack-compose",
    "flutter": "flutter",
    "react-native": "react-native",
}

#: Framework → canonical source-file extension.
FRAMEWORK_TO_FILE_EXT: dict[str, str] = {
    "swiftui": ".swift",
    "jetpack-compose": ".kt",
    "flutter": ".dart",
    "react-native": ".tsx",
}


ANNOTATION_TYPE_CLICK = "click"
ANNOTATION_TYPE_RECT = "rect"
ANNOTATION_TYPES: tuple[str, ...] = (ANNOTATION_TYPE_CLICK, ANNOTATION_TYPE_RECT)


#: Default text-block template.  ``str.format`` with named placeholders
#: so callers can override via the builder ctor.  ``{annotation_body}``
#: is substituted with :func:`render_mobile_annotations_markdown`.
DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE = (
    "The operator has marked up a live device-frame screenshot for "
    "session `{session_id}` (turn `{turn_id}`).\n"
    "Target platform(s): {platforms}\n"
    "Target framework(s): {frameworks}\n"
    "Source file extension(s): {file_exts}\n"
    "Device(s): {devices}\n"
    "Annotation count: {annotation_count}\n"
    "\n"
    "{annotation_body}\n"
    "\n"
    "Treat each annotation as a directive for the next *native* code "
    "change.  ``rect`` annotations outline a region of the rendered "
    "screen; ``click`` annotations pin a specific element.  The "
    "``componentHint`` field (when present) carries the operator's "
    "best guess for the native identifier — SwiftUI "
    "``accessibilityIdentifier``, Compose ``Modifier.testTag``, "
    "Flutter ``Key`` or widget class, or React Native ``testID``.  "
    "Prefer that hint over re-parsing the screenshot.  Modify the "
    "source file matching the framework's canonical extension — "
    "do not add web-layer markup (no HTML, no CSS, no Tailwind) to "
    "the patch."
)


# ───────────────────────────────────────────────────────────────────
#  Events
# ───────────────────────────────────────────────────────────────────


MOBILE_ANNOTATION_EVENT_BUILDING = "ui_sandbox.mobile_annotation_context.building"
MOBILE_ANNOTATION_EVENT_BUILT = "ui_sandbox.mobile_annotation_context.built"
MOBILE_ANNOTATION_EVENT_EMPTY = "ui_sandbox.mobile_annotation_context.empty"


MOBILE_ANNOTATION_EVENT_TYPES: tuple[str, ...] = (
    MOBILE_ANNOTATION_EVENT_BUILDING,
    MOBILE_ANNOTATION_EVENT_BUILT,
    MOBILE_ANNOTATION_EVENT_EMPTY,
)


# ───────────────────────────────────────────────────────────────────
#  Errors
# ───────────────────────────────────────────────────────────────────


class MobileAnnotationContextError(ValueError):
    """Parse / build errors in ``mobile_annotation_context``.

    Subclasses :class:`ValueError` so FastAPI's default 422 handler
    picks it up without extra wiring.
    """


# ───────────────────────────────────────────────────────────────────
#  Pure helpers
# ───────────────────────────────────────────────────────────────────


def resolve_framework(platform: str) -> str:
    """Resolve workspace-platform → framework id.

    Raises :class:`MobileAnnotationContextError` for unknown platforms
    so a frontend sending a stale/custom value gets a clear 422 instead
    of silently falling back to SwiftUI.
    """

    try:
        return PLATFORM_TO_FRAMEWORK[platform]
    except KeyError as exc:
        raise MobileAnnotationContextError(
            f"platform must be one of {MOBILE_PLATFORMS}, got {platform!r}"
        ) from exc


def resolve_file_ext(framework: str) -> str:
    """Canonical source-file extension for a framework."""

    try:
        return FRAMEWORK_TO_FILE_EXT[framework]
    except KeyError as exc:
        raise MobileAnnotationContextError(
            f"framework must be one of {MOBILE_FRAMEWORKS}, got {framework!r}"
        ) from exc


def _clamp_normalized(value: float) -> float:
    if not isinstance(value, (int, float)):
        raise MobileAnnotationContextError("coordinate must be a number")
    f = float(value)
    if not math.isfinite(f):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _check_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise MobileAnnotationContextError(f"{name} must be a non-negative int")
    if not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise MobileAnnotationContextError(f"{name} must be a non-negative int")
    if value < 0:
        raise MobileAnnotationContextError(f"{name} must be >= 0")
    return value


# ───────────────────────────────────────────────────────────────────
#  Records
# ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MobileAnnotationPayload:
    """Parsed single mobile annotation.

    Field names mirror the frontend
    ``MobileVisualAnnotationAgentPayload`` interface.  Rebuilt as a
    frozen dataclass so the builder can sort / dedupe without
    defensive copies.
    """

    type: str
    platform: str
    framework: str
    file_ext: str
    device: str
    screen_width: int
    screen_height: int
    box_x: float
    box_y: float
    box_w: float
    box_h: float
    native_x: int
    native_y: int
    native_w: int
    native_h: int
    component_hint: str | None = None
    comment: str = ""

    def __post_init__(self) -> None:
        if self.type not in ANNOTATION_TYPES:
            raise MobileAnnotationContextError(
                f"type must be one of {ANNOTATION_TYPES}, got {self.type!r}"
            )
        if self.platform not in MOBILE_PLATFORMS:
            raise MobileAnnotationContextError(
                f"platform must be one of {MOBILE_PLATFORMS}, got {self.platform!r}"
            )
        if self.framework not in MOBILE_FRAMEWORKS:
            raise MobileAnnotationContextError(
                f"framework must be one of {MOBILE_FRAMEWORKS}, got {self.framework!r}"
            )
        expected_fw = PLATFORM_TO_FRAMEWORK[self.platform]
        if self.framework != expected_fw:
            raise MobileAnnotationContextError(
                f"framework {self.framework!r} does not match platform "
                f"{self.platform!r} (expected {expected_fw!r})"
            )
        expected_ext = FRAMEWORK_TO_FILE_EXT[self.framework]
        if self.file_ext != expected_ext:
            raise MobileAnnotationContextError(
                f"fileExt {self.file_ext!r} does not match framework "
                f"{self.framework!r} (expected {expected_ext!r})"
            )
        if not isinstance(self.device, str) or not self.device.strip():
            raise MobileAnnotationContextError(
                "device must be a non-empty string"
            )
        if self.screen_width <= 0 or self.screen_height <= 0:
            raise MobileAnnotationContextError(
                "screenWidth / screenHeight must be positive"
            )
        for name, val in (
            ("boundingBox.x", self.box_x),
            ("boundingBox.y", self.box_y),
            ("boundingBox.w", self.box_w),
            ("boundingBox.h", self.box_h),
        ):
            if not isinstance(val, (int, float)):
                raise MobileAnnotationContextError(f"{name} must be a number")
            if not math.isfinite(float(val)):
                raise MobileAnnotationContextError(f"{name} must be finite")
            if not (0.0 <= float(val) <= 1.0):
                raise MobileAnnotationContextError(
                    f"{name} must be in [0, 1], got {val}"
                )
        # Click annotations must be zero-sized.  Rect must be non-zero.
        is_point = self.box_w == 0.0 and self.box_h == 0.0
        if self.type == ANNOTATION_TYPE_CLICK and not is_point:
            raise MobileAnnotationContextError(
                "click annotations must have zero-size boundingBox"
            )
        if self.type == ANNOTATION_TYPE_RECT and is_point:
            raise MobileAnnotationContextError(
                "rect annotations must have non-zero boundingBox"
            )
        # Native-pixel box sanity: non-negative, within screen.
        for name, val in (
            ("nativePixelBox.x", self.native_x),
            ("nativePixelBox.y", self.native_y),
            ("nativePixelBox.w", self.native_w),
            ("nativePixelBox.h", self.native_h),
        ):
            if val < 0:
                raise MobileAnnotationContextError(f"{name} must be >= 0")
        if self.native_x + self.native_w > self.screen_width:
            raise MobileAnnotationContextError(
                f"nativePixelBox.x + .w = {self.native_x + self.native_w} "
                f"exceeds screenWidth {self.screen_width}"
            )
        if self.native_y + self.native_h > self.screen_height:
            raise MobileAnnotationContextError(
                f"nativePixelBox.y + .h = {self.native_y + self.native_h} "
                f"exceeds screenHeight {self.screen_height}"
            )
        if self.component_hint is not None:
            if not isinstance(self.component_hint, str):
                raise MobileAnnotationContextError(
                    "componentHint must be a string or null"
                )
            if not self.component_hint.strip():
                raise MobileAnnotationContextError(
                    "componentHint must be non-empty or null"
                )
        if not isinstance(self.comment, str):
            raise MobileAnnotationContextError("comment must be a string")

    @property
    def is_point(self) -> bool:
        return self.box_w == 0.0 and self.box_h == 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "platform": self.platform,
            "framework": self.framework,
            "fileExt": self.file_ext,
            "device": self.device,
            "screenWidth": int(self.screen_width),
            "screenHeight": int(self.screen_height),
            "boundingBox": {
                "x": float(self.box_x),
                "y": float(self.box_y),
                "w": float(self.box_w),
                "h": float(self.box_h),
            },
            "nativePixelBox": {
                "x": int(self.native_x),
                "y": int(self.native_y),
                "w": int(self.native_w),
                "h": int(self.native_h),
            },
            "componentHint": self.component_hint,
            "comment": self.comment,
        }


def mobile_annotation_from_dict(raw: Mapping[str, Any]) -> MobileAnnotationPayload:
    """Parse a frontend-shaped dict into :class:`MobileAnnotationPayload`.

    Accepts the exact keys the Mobile annotator emits.  Unknown extra
    keys are ignored (forward-compat).  Missing required keys raise
    :class:`MobileAnnotationContextError`.
    """

    if not isinstance(raw, Mapping):
        raise MobileAnnotationContextError("payload must be a mapping")
    required = (
        "type",
        "platform",
        "framework",
        "fileExt",
        "device",
        "screenWidth",
        "screenHeight",
        "boundingBox",
        "nativePixelBox",
    )
    for key in required:
        if key not in raw:
            raise MobileAnnotationContextError(f"payload missing required key: {key!r}")

    box = raw["boundingBox"]
    if not isinstance(box, Mapping):
        raise MobileAnnotationContextError("boundingBox must be an object")
    native = raw["nativePixelBox"]
    if not isinstance(native, Mapping):
        raise MobileAnnotationContextError("nativePixelBox must be an object")

    for key in ("x", "y", "w", "h"):
        if key not in box:
            raise MobileAnnotationContextError(f"boundingBox missing {key!r}")
        if key not in native:
            raise MobileAnnotationContextError(f"nativePixelBox missing {key!r}")

    screen_w = _check_non_negative_int(raw["screenWidth"], "screenWidth")
    screen_h = _check_non_negative_int(raw["screenHeight"], "screenHeight")
    if screen_w <= 0 or screen_h <= 0:
        raise MobileAnnotationContextError(
            "screenWidth / screenHeight must be positive"
        )
    native_x = _check_non_negative_int(native["x"], "nativePixelBox.x")
    native_y = _check_non_negative_int(native["y"], "nativePixelBox.y")
    native_w = _check_non_negative_int(native["w"], "nativePixelBox.w")
    native_h = _check_non_negative_int(native["h"], "nativePixelBox.h")

    component_hint = raw.get("componentHint")
    if component_hint is not None and not isinstance(component_hint, str):
        raise MobileAnnotationContextError(
            "componentHint must be a string or null"
        )
    comment = raw.get("comment", "")
    if not isinstance(comment, str):
        raise MobileAnnotationContextError("comment must be a string")

    # Strict validation — do NOT clamp.  The frontend already clamps into
    # [0, 1]; anything out of range is a bug or a hand-crafted payload
    # that we'd rather reject with a clear 422 than silently accept.
    for key in ("x", "y", "w", "h"):
        val = box[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise MobileAnnotationContextError(
                f"boundingBox.{key} must be a number"
            )
        if not math.isfinite(float(val)):
            raise MobileAnnotationContextError(
                f"boundingBox.{key} must be finite"
            )

    return MobileAnnotationPayload(
        type=raw["type"],
        platform=raw["platform"],
        framework=raw["framework"],
        file_ext=raw["fileExt"],
        device=raw["device"],
        screen_width=screen_w,
        screen_height=screen_h,
        box_x=float(box["x"]),
        box_y=float(box["y"]),
        box_w=float(box["w"]),
        box_h=float(box["h"]),
        native_x=native_x,
        native_y=native_y,
        native_w=native_w,
        native_h=native_h,
        component_hint=component_hint,
        comment=comment,
    )


def mobile_annotations_from_list(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[MobileAnnotationPayload, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise MobileAnnotationContextError("annotations must be a sequence")
    return tuple(mobile_annotation_from_dict(item) for item in raw)


# ───────────────────────────────────────────────────────────────────
#  Rendering
# ───────────────────────────────────────────────────────────────────


def _format_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _hint_label(framework: str) -> str:
    if framework == "swiftui":
        return "accessibilityIdentifier"
    if framework == "jetpack-compose":
        return "Modifier.testTag"
    if framework == "flutter":
        return "Widget Key / class"
    if framework == "react-native":
        return "testID"
    return "componentHint"


def render_mobile_annotation_entry(
    *,
    label: int,
    payload: MobileAnnotationPayload,
) -> str:
    """Render a single annotation as a markdown list entry.

    Framework-aware — the ``hint`` line speaks SwiftUI / Compose /
    Flutter / RN vocabulary.  Byte-stable output so golden tests can
    pin the exact string.
    """

    if not isinstance(label, int) or isinstance(label, bool) or label < 1:
        raise MobileAnnotationContextError("label must be a positive int")
    if not isinstance(payload, MobileAnnotationPayload):
        raise TypeError("payload must be a MobileAnnotationPayload")

    hint_label = _hint_label(payload.framework)
    hint_value = (
        f"`{payload.component_hint}`" if payload.component_hint else "(none — infer from screenshot)"
    )
    comment = payload.comment.strip()
    comment_text = f'"{comment}"' if comment else "(no comment)"

    lines: list[str] = []
    lines.append(
        f"{label}. [{payload.type}] {payload.framework} ({payload.file_ext}) "
        f"on {payload.device} — {hint_label}: {hint_value} comment={comment_text}"
    )
    if payload.type == ANNOTATION_TYPE_RECT:
        lines.append(
            "   - boundingBox: "
            f"x={_format_pct(payload.box_x)} y={_format_pct(payload.box_y)} "
            f"w={_format_pct(payload.box_w)} h={_format_pct(payload.box_h)}"
        )
        lines.append(
            "   - nativePixelBox: "
            f"x={payload.native_x}px y={payload.native_y}px "
            f"w={payload.native_w}px h={payload.native_h}px "
            f"(screen {payload.screen_width}×{payload.screen_height}px)"
        )
    else:  # click
        lines.append(
            "   - boundingBox: "
            f"x={_format_pct(payload.box_x)} y={_format_pct(payload.box_y)}"
        )
        lines.append(
            "   - nativePixelPoint: "
            f"x={payload.native_x}px y={payload.native_y}px "
            f"(screen {payload.screen_width}×{payload.screen_height}px)"
        )
    return "\n".join(lines)


def render_mobile_annotations_markdown(
    payloads: Sequence[MobileAnnotationPayload],
) -> str:
    """Render the whole annotation list as a markdown block.

    Empty list → ``"No operator annotations this turn."``.  Labels are
    1-based and assigned in input order.  Output is byte-stable across
    runs with the same inputs.
    """

    if not payloads:
        return "No operator annotations this turn."
    entries = [
        render_mobile_annotation_entry(label=i + 1, payload=p)
        for i, p in enumerate(payloads)
    ]
    return "\n".join(entries)


@dataclass(frozen=True)
class MobileAnnotationBundle:
    """One turn's mobile-annotation bundle for the agent."""

    session_id: str
    turn_id: str
    built_at: float
    payloads: tuple[MobileAnnotationPayload, ...]
    text_prompt: str
    annotation_body_markdown: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise MobileAnnotationContextError("session_id must be a non-empty string")
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise MobileAnnotationContextError("turn_id must be a non-empty string")
        if not isinstance(self.built_at, (int, float)) or self.built_at < 0:
            raise MobileAnnotationContextError("built_at must be a non-negative number")
        for p in self.payloads:
            if not isinstance(p, MobileAnnotationPayload):
                raise MobileAnnotationContextError(
                    "payloads entries must be MobileAnnotationPayload"
                )
        if not isinstance(self.text_prompt, str) or not self.text_prompt:
            raise MobileAnnotationContextError("text_prompt must be a non-empty string")
        if not isinstance(self.annotation_body_markdown, str):
            raise MobileAnnotationContextError(
                "annotation_body_markdown must be a string"
            )
        for w in self.warnings:
            if not isinstance(w, str) or not w:
                raise MobileAnnotationContextError(
                    "warnings entries must be non-empty strings"
                )
        object.__setattr__(self, "payloads", tuple(self.payloads))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    @property
    def annotation_count(self) -> int:
        return len(self.payloads)

    @property
    def has_annotations(self) -> bool:
        return bool(self.payloads)

    @property
    def click_count(self) -> int:
        return sum(1 for p in self.payloads if p.type == ANNOTATION_TYPE_CLICK)

    @property
    def rect_count(self) -> int:
        return sum(1 for p in self.payloads if p.type == ANNOTATION_TYPE_RECT)

    @property
    def platforms(self) -> tuple[str, ...]:
        """Unique platforms in insertion order."""
        seen: list[str] = []
        for p in self.payloads:
            if p.platform not in seen:
                seen.append(p.platform)
        return tuple(seen)

    @property
    def frameworks(self) -> tuple[str, ...]:
        seen: list[str] = []
        for p in self.payloads:
            if p.framework not in seen:
                seen.append(p.framework)
        return tuple(seen)

    @property
    def file_exts(self) -> tuple[str, ...]:
        seen: list[str] = []
        for p in self.payloads:
            if p.file_ext not in seen:
                seen.append(p.file_ext)
        return tuple(seen)

    @property
    def devices(self) -> tuple[str, ...]:
        seen: list[str] = []
        for p in self.payloads:
            if p.device not in seen:
                seen.append(p.device)
        return tuple(seen)

    @property
    def hint_count(self) -> int:
        return sum(1 for p in self.payloads if p.component_hint)

    @property
    def commented_count(self) -> int:
        return sum(1 for p in self.payloads if p.comment.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MOBILE_ANNOTATION_CONTEXT_SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "built_at": float(self.built_at),
            "annotation_count": self.annotation_count,
            "click_count": self.click_count,
            "rect_count": self.rect_count,
            "hint_count": self.hint_count,
            "commented_count": self.commented_count,
            "has_annotations": self.has_annotations,
            "platforms": list(self.platforms),
            "frameworks": list(self.frameworks),
            "file_exts": list(self.file_exts),
            "devices": list(self.devices),
            "payloads": [p.to_dict() for p in self.payloads],
            "text_prompt": self.text_prompt,
            "annotation_body_markdown": self.annotation_body_markdown,
            "warnings": list(self.warnings),
        }


def build_mobile_text_content_block(bundle: MobileAnnotationBundle) -> dict[str, Any]:
    """Return the multimodal text content block for this bundle."""

    if not isinstance(bundle, MobileAnnotationBundle):
        raise TypeError("bundle must be a MobileAnnotationBundle")
    return {"type": "text", "text": bundle.text_prompt}


# ───────────────────────────────────────────────────────────────────
#  Builder
# ───────────────────────────────────────────────────────────────────


class MobileAnnotationContextBuilder:
    """Per-turn factory producing :class:`MobileAnnotationBundle`.

    Mirrors :class:`backend.ui_annotation_context.AnnotationContextBuilder`:
    holds a session id, a turn counter, an event callback, and an
    optional text-prompt template override.  Thread-safe — every
    mutation takes ``self._lock`` so concurrent builds on the same
    session produce consistent turn ids.
    """

    def __init__(
        self,
        *,
        session_id: str,
        text_prompt_template: str = DEFAULT_MOBILE_ANNOTATION_TEXT_PROMPT_TEMPLATE,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise MobileAnnotationContextError(
                "session_id must be a non-empty string"
            )
        if not isinstance(text_prompt_template, str) or not text_prompt_template:
            raise MobileAnnotationContextError(
                "text_prompt_template must be a non-empty string"
            )
        self._session_id = session_id
        self._template = text_prompt_template
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._turn_counter = 0
        self._last_bundle: MobileAnnotationBundle | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turn_counter(self) -> int:
        with self._lock:
            return self._turn_counter

    @property
    def last_bundle(self) -> MobileAnnotationBundle | None:
        with self._lock:
            return self._last_bundle

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event_type, dict(data))
        except Exception:  # pragma: no cover - callback should never leak
            logger.exception("mobile_annotation_context event callback failed")

    def build(
        self,
        payloads: Sequence[MobileAnnotationPayload | Mapping[str, Any]],
        *,
        turn_id: str | None = None,
    ) -> MobileAnnotationBundle:
        with self._lock:
            self._turn_counter += 1
            effective_turn = turn_id or f"mob-turn-{self._turn_counter}"

        # Accept either already-parsed payloads or raw frontend dicts.
        parsed: list[MobileAnnotationPayload] = []
        for item in payloads:
            if isinstance(item, MobileAnnotationPayload):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(mobile_annotation_from_dict(item))
            else:
                raise MobileAnnotationContextError(
                    "payload entries must be MobileAnnotationPayload or dict"
                )

        self._emit(
            MOBILE_ANNOTATION_EVENT_BUILDING,
            {
                "session_id": self._session_id,
                "turn_id": effective_turn,
                "annotation_count": len(parsed),
            },
        )

        body = render_mobile_annotations_markdown(parsed)

        warnings: list[str] = []
        # Multi-platform warning — the skill expects one platform per
        # turn; mixing is legal but produces a warning so the agent
        # does not silently patch Swift while the operator meant Kotlin.
        platforms = tuple(dict.fromkeys(p.platform for p in parsed))
        if len(platforms) > 1:
            warnings.append(
                f"Annotations span multiple platforms ({', '.join(platforms)}) "
                "— agent will patch each framework's source file separately."
            )

        frameworks = tuple(dict.fromkeys(p.framework for p in parsed))
        file_exts = tuple(dict.fromkeys(p.file_ext for p in parsed))
        devices = tuple(dict.fromkeys(p.device for p in parsed))

        text_prompt = self._template.format(
            session_id=self._session_id,
            turn_id=effective_turn,
            platforms=", ".join(platforms) if platforms else "(none)",
            frameworks=", ".join(frameworks) if frameworks else "(none)",
            file_exts=", ".join(file_exts) if file_exts else "(none)",
            devices=", ".join(devices) if devices else "(none)",
            annotation_count=len(parsed),
            annotation_body=body,
        )

        bundle = MobileAnnotationBundle(
            session_id=self._session_id,
            turn_id=effective_turn,
            built_at=float(self._clock()),
            payloads=tuple(parsed),
            text_prompt=text_prompt,
            annotation_body_markdown=body,
            warnings=tuple(warnings),
        )
        with self._lock:
            self._last_bundle = bundle

        event = (
            MOBILE_ANNOTATION_EVENT_BUILT if parsed else MOBILE_ANNOTATION_EVENT_EMPTY
        )
        self._emit(
            event,
            {
                "session_id": self._session_id,
                "turn_id": effective_turn,
                "annotation_count": len(parsed),
                "platforms": list(platforms),
                "frameworks": list(frameworks),
                "file_exts": list(file_exts),
                "devices": list(devices),
                "warnings": list(warnings),
            },
        )
        return bundle

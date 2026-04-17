"""V5 #3 (issue #321) — Figma MCP → mobile code pipeline.

Bridges the Figma MCP server (``mcp__claude_ai_Figma__get_design_context``)
into three-platform mobile code emission — **SwiftUI views** (iOS 16+),
**Jetpack Compose Material 3** components, and **Flutter 3.22+** widgets —
for the V5 Mobile UI Designer agent
(``configs/roles/mobile-ui-designer.md``).

Why this module exists
----------------------

The Figma MCP returns React + Tailwind reference code enriched with
design variables, a screenshot, and contextual hints. The Figma docs
explicitly call that output a **reference, not final code** — the
agent must adapt it to the target project's stack.

For the V5 mobile track, that adaptation means translating the Figma
reference into Swift / Kotlin / Dart.  The sibling modules pin the
fact side:

* :mod:`backend.mobile_component_registry` tells the agent **which**
  SwiftUI views / Compose components / Flutter widgets are canonical
  (``NavigationStack`` not ``NavigationView``, ``NavigationBar`` (M3)
  not ``BottomNavigationBar`` (M2), …);
* :mod:`backend.design_token_loader` surfaces live design tokens —
  colours, spacing, radius, typography — from the project's
  ``globals.css``; mobile generators map the Figma values onto these
  (never inline hex / dp / pt / sp);
* the sibling :mod:`backend.figma_to_ui` handles the **web** output
  (React + TSX) for the same input shape, so this module intentionally
  mirrors its public surface (``normalize_node_id`` /
  ``from_mcp_response`` / ``run_figma_to_*``) and the agent learns one
  contract once.

This module provides the mobile-specific glue:

* :func:`from_mcp_response` parses the MCP payload (raw dict,
  stringified JSON, or ``{"content":[{type:"text", text:"..."}]}``
  envelope) into a validated :class:`MobileFigmaDesignContext`;
* :func:`extract_from_context` surfaces observed hex colours /
  spacing values / radii / shadows / typography / component-tree
  hints / imports from the reference code so the generation prompt
  can instruct the model to **map them onto design tokens + the
  mobile component registry**;
* :func:`build_mobile_generation_prompt` renders a byte-stable
  prompt injecting (a) the Figma context, (b) the extraction, (c)
  the mobile component registry block, (d) the design tokens block,
  and (e) the caller brief — the only non-deterministic step in the
  pipeline is the LLM call itself;
* :func:`generate_mobile_from_figma` is the end-to-end entry: context →
  extraction → prompt → LLM → three-platform code extraction →
  :class:`MobileFigmaGenerationResult`.  All failure modes surface as
  ``warnings`` rather than tracebacks.

Contract (pinned by ``backend/tests/test_figma_to_mobile.py``)
--------------------------------------------------------------

* :data:`FIGMA_MOBILE_SCHEMA_VERSION` bumps when the ``to_dict()``
  shape of any exported dataclass changes.
* :data:`TARGET_PLATFORMS` == ``("swiftui","compose","flutter")`` —
  the same three-platform contract enforced by
  :mod:`backend.mobile_component_registry`.
* Each platform maps to exactly one fenced-language:
  :data:`PLATFORM_LANGS` == ``{"swiftui":"swift","compose":"kotlin","flutter":"dart"}``.
* :class:`MobileFigmaToken`, :class:`MobileFigmaDesignContext`,
  :class:`MobileFigmaExtraction`, :class:`MobileCodeOutputs`,
  :class:`MobileFigmaGenerationResult` are frozen, validated, and
  JSON-serialisable via ``to_dict``.
* :func:`normalize_node_id` accepts both ``"123:456"`` and
  ``"123-456"`` (MCP url shape) and canonicalises to the colon form.
* :func:`from_mcp_response` tolerates missing / malformed fields and
  surfaces them as warnings stashed on
  ``metadata["_parse_warnings"]`` rather than raising — the pipeline
  must keep running on partial context.
* :func:`build_mobile_generation_prompt` is pure: same inputs →
  byte-identical prompt across calls.
* :func:`extract_mobile_code_from_response` recognises fenced
  ``swift`` / ``kotlin`` / ``dart`` blocks (and the common aliases:
  ``swiftui`` → swift, ``kt`` / ``compose`` → kotlin, ``flutter`` →
  dart).  Unknown fences are ignored.
* Every pipeline entry point returns a well-formed
  :class:`MobileFigmaGenerationResult` (or ``to_dict`` payload) even
  on failure — the caller inspects ``warnings`` to decide retry /
  escalate / surface-to-operator.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from backend.design_token_loader import (
    DesignTokens,
    load_design_tokens,
    render_agent_context_block as render_design_tokens_block,
)
from backend.mobile_component_registry import (
    PLATFORMS as MOBILE_PLATFORMS,
    render_agent_context_block as render_mobile_registry_block,
)
from backend.vision_to_ui import (
    MAX_IMAGE_BYTES,
    SUPPORTED_MIME_TYPES,
    VisionImage,
    build_multimodal_message as _build_multimodal_message,
    validate_image,
)

logger = logging.getLogger(__name__)


__all__ = [
    "FIGMA_MOBILE_SCHEMA_VERSION",
    "DEFAULT_FIGMA_MOBILE_MODEL",
    "DEFAULT_FIGMA_MOBILE_PROVIDER",
    "TARGET_PLATFORMS",
    "PLATFORM_LANGS",
    "TOKEN_KINDS",
    "MobileFigmaToken",
    "MobileFigmaDesignContext",
    "MobileFigmaExtraction",
    "MobileCodeOutputs",
    "MobileFigmaGenerationResult",
    "normalize_node_id",
    "normalize_file_key",
    "canonical_figma_source",
    "from_mcp_response",
    "validate_figma_mobile_context",
    "extract_from_context",
    "build_mobile_generation_prompt",
    "build_multimodal_message",
    "extract_mobile_code_from_response",
    "generate_mobile_from_figma",
    "run_figma_to_mobile",
]


# Bump when the shape of any ``to_dict()`` payload changes — callers
# cache prompts / responses keyed off this version.
FIGMA_MOBILE_SCHEMA_VERSION = "1.0.0"

#: Default LLM model for Figma → mobile generation.  Opus 4.7 because
#: adapting a React+Tailwind reference into **three** platform-native
#: component trees is a reasoning task (map Figma ``text-2xl`` →
#: ``Theme.of(context).textTheme.headlineSmall`` / SwiftUI ``.headline``
#: / Compose ``MaterialTheme.typography.headlineSmall``).
DEFAULT_FIGMA_MOBILE_MODEL = "claude-opus-4-7"
DEFAULT_FIGMA_MOBILE_PROVIDER = "anthropic"

#: Three target platforms — aligned with
#: :data:`backend.mobile_component_registry.PLATFORMS`.  The mobile
#: designer emits all three by default; the Edit complexity auto-router
#: may narrow with a ``platforms=`` filter for single-target prompts
#: ("用 SwiftUI").
TARGET_PLATFORMS: tuple[str, ...] = MOBILE_PLATFORMS

#: Platform → fenced-language mapping for :func:`extract_mobile_code_from_response`.
PLATFORM_LANGS: Mapping[str, str] = MappingProxyType({
    "swiftui": "swift",
    "compose": "kotlin",
    "flutter": "dart",
})

#: Fenced-language aliases consumers commonly emit.  Order matters
#: only for priority inside one platform (first match wins).
_PLATFORM_LANG_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "swiftui": ("swift", "swiftui"),
    "compose": ("kotlin", "kt", "compose"),
    "flutter": ("dart", "flutter"),
})

#: Fixed kinds accepted by :class:`MobileFigmaToken`.  Aligned with
#: :data:`backend.design_token_loader.KINDS`.
TOKEN_KINDS: tuple[str, ...] = (
    "color",
    "spacing",
    "radius",
    "font",
    "shadow",
    "other",
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MobileFigmaToken:
    """One design value surfaced from the Figma reference code.

    ``name`` is a stable handle (the original Figma variable name when
    available, otherwise ``observed-color-1`` / ``observed-spacing-1`` /
    …).  ``value`` is the raw CSS value as emitted by Figma.  The
    mobile generator is expected to **map** these onto the project's
    design tokens + the mobile component registry — not inline them.
    """

    name: str
    value: str
    kind: str = "other"

    def __post_init__(self) -> None:
        if self.kind not in TOKEN_KINDS:
            raise ValueError(
                f"Unknown MobileFigmaToken kind {self.kind!r}; "
                f"must be one of {TOKEN_KINDS}"
            )
        if not self.name:
            raise ValueError("MobileFigmaToken.name must be non-empty")
        if self.value is None:
            raise ValueError("MobileFigmaToken.value must not be None")


@dataclass(frozen=True)
class MobileFigmaDesignContext:
    """Validated inputs from an MCP ``get_design_context`` response.

    Mirrors :class:`backend.figma_to_ui.FigmaDesignContext` — the MCP
    response is the same shape regardless of whether the caller plans
    to emit web TSX or mobile Swift / Kotlin / Dart.  The difference is
    entirely downstream in the prompt + code-extraction step.
    """

    file_key: str
    node_id: str
    code: str = ""
    screenshot: VisionImage | None = None
    variables: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    asset_urls: Mapping[str, str] = field(default_factory=dict)
    source: str | None = None

    def __post_init__(self) -> None:
        if not self.file_key:
            raise ValueError(
                "MobileFigmaDesignContext.file_key must be non-empty"
            )
        if not self.node_id:
            raise ValueError(
                "MobileFigmaDesignContext.node_id must be non-empty"
            )
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "asset_urls", MappingProxyType(dict(self.asset_urls)))
        if self.screenshot is not None and not isinstance(
            self.screenshot, VisionImage
        ):
            raise TypeError(
                "MobileFigmaDesignContext.screenshot must be a VisionImage"
            )

    @property
    def has_screenshot(self) -> bool:
        return self.screenshot is not None

    def to_dict(self) -> dict:
        return {
            "schema_version": FIGMA_MOBILE_SCHEMA_VERSION,
            "file_key": self.file_key,
            "node_id": self.node_id,
            "code": self.code,
            "has_screenshot": self.has_screenshot,
            "screenshot_mime": (
                self.screenshot.mime_type if self.screenshot else None
            ),
            "screenshot_bytes": (
                self.screenshot.size_bytes if self.screenshot else 0
            ),
            "variables": dict(self.variables),
            "metadata": dict(self.metadata),
            "asset_urls": dict(self.asset_urls),
            "source": self.source,
        }


@dataclass(frozen=True)
class MobileFigmaExtraction:
    """Structured hints distilled from the Figma reference code.

    All tuples are *sorted + de-duplicated* for byte-stable prompt
    rendering.  ``parse_succeeded`` is ``False`` only on total failure
    (empty code) — the pipeline still returns a well-formed extraction
    so downstream prompt construction cannot raise.
    """

    design_tokens: tuple[MobileFigmaToken, ...] = ()
    color_values: tuple[str, ...] = ()
    spacing_values: tuple[str, ...] = ()
    typography: tuple[str, ...] = ()
    radii: tuple[str, ...] = ()
    shadows: tuple[str, ...] = ()
    component_hierarchy: tuple[str, ...] = ()
    imported_components: tuple[str, ...] = ()
    screenshot_attached: bool = False
    parse_succeeded: bool = True
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "schema_version": FIGMA_MOBILE_SCHEMA_VERSION,
            "design_tokens": [asdict(t) for t in self.design_tokens],
            "color_values": list(self.color_values),
            "spacing_values": list(self.spacing_values),
            "typography": list(self.typography),
            "radii": list(self.radii),
            "shadows": list(self.shadows),
            "component_hierarchy": list(self.component_hierarchy),
            "imported_components": list(self.imported_components),
            "screenshot_attached": self.screenshot_attached,
            "parse_succeeded": self.parse_succeeded,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MobileCodeOutputs:
    """Three-platform code emitted by one LLM round.

    Each field is the raw, platform-native source — SwiftUI for
    ``swift``, Jetpack Compose Material 3 for ``kotlin``, Flutter 3.22+
    for ``dart``.  Missing platforms surface as empty strings plus an
    entry in :class:`MobileFigmaGenerationResult.warnings` (e.g.
    ``"swiftui_missing"``) so the caller can decide whether to re-prompt
    or ship a partial diff.
    """

    swift: str = ""
    kotlin: str = ""
    dart: str = ""

    @property
    def platform_map(self) -> Mapping[str, str]:
        """Return ``{"swiftui": swift, "compose": kotlin, "flutter": dart}``."""
        return MappingProxyType({
            "swiftui": self.swift,
            "compose": self.kotlin,
            "flutter": self.dart,
        })

    @property
    def is_complete(self) -> bool:
        """True when all three platforms have non-empty code."""
        return bool(self.swift.strip() and self.kotlin.strip() and self.dart.strip())

    def missing_platforms(self) -> tuple[str, ...]:
        """Return a sorted tuple of platform ids that emitted no code."""
        missing = [p for p, code in self.platform_map.items() if not code.strip()]
        return tuple(sorted(missing))

    def to_dict(self) -> dict:
        return {
            "swift": self.swift,
            "kotlin": self.kotlin,
            "dart": self.dart,
            "is_complete": self.is_complete,
            "missing_platforms": list(self.missing_platforms()),
        }


@dataclass(frozen=True)
class MobileFigmaGenerationResult:
    """End-to-end output of :func:`generate_mobile_from_figma`."""

    context: MobileFigmaDesignContext
    extraction: MobileFigmaExtraction
    outputs: MobileCodeOutputs = field(default_factory=MobileCodeOutputs)
    raw_response: str = ""
    warnings: tuple[str, ...] = ()
    model: str | None = None
    provider: str | None = None

    @property
    def is_complete(self) -> bool:
        """All three platforms emitted non-empty code."""
        return self.outputs.is_complete

    def to_dict(self) -> dict:
        return {
            "schema_version": FIGMA_MOBILE_SCHEMA_VERSION,
            "context": self.context.to_dict(),
            "extraction": self.extraction.to_dict(),
            "outputs": self.outputs.to_dict(),
            "raw_response": self.raw_response,
            "warnings": list(self.warnings),
            "model": self.model,
            "provider": self.provider,
            "is_complete": self.is_complete,
        }


# ── Node-id / file-key normalisation ─────────────────────────────────


_NODE_ID_RE = re.compile(r"^-?\d+[:\-]-?\d+$")


def normalize_node_id(raw: str) -> str:
    """Return the canonical ``"123:456"`` form of a Figma node id.

    Accepts ``"123:456"`` (Figma API form) and ``"123-456"`` (url form —
    ``?node-id=123-456``).  Raises ``ValueError`` for empty / malformed
    input.
    """
    if raw is None:
        raise ValueError("node_id must not be None")
    stripped = str(raw).strip()
    if not stripped:
        raise ValueError("node_id must be non-empty")
    if not _NODE_ID_RE.match(stripped):
        raise ValueError(
            f"Invalid node_id {raw!r}; expected 'A:B' or 'A-B' "
            "(digits, optional leading '-')"
        )
    if ":" in stripped:
        return stripped
    if stripped.startswith("-"):
        idx = stripped.find("-", 1)
    else:
        idx = stripped.find("-")
    if idx < 0:  # defensive — pattern guarantees a split
        raise ValueError(f"Invalid node_id {raw!r}")
    return stripped[:idx] + ":" + stripped[idx + 1 :]


def normalize_file_key(raw: str) -> str:
    """Return the canonical file-key form (trim + validate non-empty)."""
    if raw is None:
        raise ValueError("file_key must not be None")
    stripped = str(raw).strip()
    if not stripped:
        raise ValueError("file_key must be non-empty")
    if "/" in stripped or " " in stripped:
        raise ValueError(
            f"Invalid file_key {raw!r}; expected an opaque token "
            "(no slashes / whitespace). If you have a URL, extract the "
            "<fileKey> segment from figma.com/design/<fileKey>/<fileName>."
        )
    return stripped


def canonical_figma_source(file_key: str, node_id: str) -> str:
    """Return a deterministic ``figma.com/design/…`` URL-ish handle."""
    return (
        "figma.com/design/"
        f"{file_key}?node-id={node_id.replace(':', '-')}"
    )


# ── MCP response parsing ─────────────────────────────────────────────


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _coerce_mcp_payload(response: Any) -> tuple[dict, tuple[str, ...]]:
    """Flatten an MCP response into a single ``dict`` for parsing."""
    warnings: list[str] = []
    if response is None:
        warnings.append("mcp_response_missing")
        return {}, tuple(warnings)

    if isinstance(response, str):
        parsed = _try_json(response)
        if parsed is None:
            warnings.append("mcp_response_not_json")
            return {}, tuple(warnings)
        response = parsed

    if not isinstance(response, Mapping):
        warnings.append("mcp_response_not_object")
        return {}, tuple(warnings)

    # Unwrap an MCP "content" list (``{"content":[{type:"text",text:"..."}]}``).
    if "content" in response and isinstance(response.get("content"), list):
        text_parts: list[str] = []
        for block in response["content"]:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text") or ""
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            inner = "\n".join(text_parts)
            parsed = _try_json(inner)
            if isinstance(parsed, Mapping):
                return dict(parsed), tuple(warnings)

    return dict(response), tuple(warnings)


_CODE_KEYS = ("code", "source", "snippet", "react", "react_code")
_SCREENSHOT_KEYS = ("screenshot", "image", "preview", "screenshot_base64")
_SCREENSHOT_MIME_KEYS = ("screenshot_mime", "mime_type", "screenshotMime")
_VARIABLES_KEYS = ("variables", "design_variables", "tokens", "variable_defs")
_METADATA_KEYS = ("metadata", "meta", "info", "annotations")
_ASSET_KEYS = ("asset_urls", "assets", "download_urls", "images")


def _pick(obj: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _coerce_mapping(obj: Any) -> dict[str, Any]:
    """Turn anything that looks vaguely map-like into a string-keyed dict."""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return {str(k): v for k, v in obj.items()}
    if isinstance(obj, str):
        parsed = _try_json(obj)
        if isinstance(parsed, Mapping):
            return {str(k): v for k, v in parsed.items()}
        return {}
    return {}


def _coerce_screenshot(
    raw: Any,
    mime_type: str,
) -> tuple[VisionImage | None, list[str]]:
    """Best-effort: accept raw bytes, data-URL, or base64 string."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            return validate_image(bytes(raw), mime_type), []
        except (TypeError, ValueError) as exc:
            logger.debug("figma_to_mobile screenshot bytes rejected: %s", exc)
            return None, ["screenshot_invalid"]
    if not isinstance(raw, str):
        return None, ["screenshot_invalid"]
    payload = raw.strip()
    if not payload:
        return None, []
    if payload.startswith("data:"):
        header, _, body = payload.partition(",")
        if ";base64" in header:
            declared = header.split(":", 1)[1].split(";", 1)[0].strip()
            if declared:
                mime_type = declared
            payload = body
        else:
            return None, ["screenshot_invalid"]
    try:
        decoded = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError, base64.binascii.Error):
        return None, ["screenshot_invalid"]
    if not decoded:
        return None, []
    if len(decoded) > MAX_IMAGE_BYTES:
        return None, ["screenshot_too_large"]
    if mime_type not in SUPPORTED_MIME_TYPES:
        return None, ["screenshot_unsupported_mime"]
    try:
        return validate_image(decoded, mime_type), []
    except (TypeError, ValueError) as exc:
        logger.debug("figma_to_mobile screenshot validation failed: %s", exc)
        return None, ["screenshot_invalid"]


def from_mcp_response(
    response: Any,
    *,
    file_key: str,
    node_id: str,
    screenshot_mime_hint: str | None = None,
) -> MobileFigmaDesignContext:
    """Parse an MCP ``get_design_context`` response into a mobile context.

    Defensive by design — missing fields become empty tokens / empty
    mappings, and every parse hiccup is stashed under
    ``metadata["_parse_warnings"]`` so the downstream pipeline can
    surface them without a side-channel.
    """
    fk = normalize_file_key(file_key)
    nid = normalize_node_id(node_id)

    payload, parse_warnings = _coerce_mcp_payload(response)

    code = _pick(payload, _CODE_KEYS)
    code_str = str(code).strip() if code is not None else ""

    screenshot_raw = _pick(payload, _SCREENSHOT_KEYS)
    screenshot_mime = (
        _pick(payload, _SCREENSHOT_MIME_KEYS)
        or screenshot_mime_hint
        or "image/png"
    )
    screenshot: VisionImage | None = None
    screenshot_warnings: list[str] = []
    if screenshot_raw:
        screenshot, extra = _coerce_screenshot(screenshot_raw, screenshot_mime)
        screenshot_warnings.extend(extra)

    variables = _coerce_mapping(_pick(payload, _VARIABLES_KEYS) or {})
    metadata = _coerce_mapping(_pick(payload, _METADATA_KEYS) or {})
    asset_urls_raw = _coerce_mapping(_pick(payload, _ASSET_KEYS) or {})
    asset_urls = {
        str(k): str(v)
        for k, v in asset_urls_raw.items()
        if isinstance(v, (str, bytes)) or v is not None
    }

    warnings = list(parse_warnings) + screenshot_warnings
    if not code_str and not screenshot:
        warnings.append("figma_context_empty")

    metadata_out: dict[str, Any] = dict(metadata)
    if warnings:
        existing = metadata_out.get("_parse_warnings")
        if existing is None:
            metadata_out["_parse_warnings"] = list(warnings)
        else:
            metadata_out["_parse_warnings"] = list(existing) + list(warnings)

    return MobileFigmaDesignContext(
        file_key=fk,
        node_id=nid,
        code=code_str,
        screenshot=screenshot,
        variables=variables,
        metadata=metadata_out,
        asset_urls=asset_urls,
        source=canonical_figma_source(fk, nid),
    )


def validate_figma_mobile_context(
    *,
    file_key: str,
    node_id: str,
    code: str = "",
    screenshot: VisionImage | None = None,
    variables: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    asset_urls: Mapping[str, str] | None = None,
) -> MobileFigmaDesignContext:
    """Build a :class:`MobileFigmaDesignContext` from pre-parsed pieces."""
    fk = normalize_file_key(file_key)
    nid = normalize_node_id(node_id)
    return MobileFigmaDesignContext(
        file_key=fk,
        node_id=nid,
        code=code or "",
        screenshot=screenshot,
        variables=variables or {},
        metadata=metadata or {},
        asset_urls=asset_urls or {},
        source=canonical_figma_source(fk, nid),
    )


# ── Extraction: tokens / colours / spacing / hierarchy ───────────────


_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_RGB_COLOR_RE = re.compile(r"rgba?\(\s*[^)]*\)", re.IGNORECASE)
_HSL_COLOR_RE = re.compile(r"hsla?\(\s*[^)]*\)", re.IGNORECASE)
_OKLCH_COLOR_RE = re.compile(r"oklch\(\s*[^)]*\)", re.IGNORECASE)
_OKLAB_COLOR_RE = re.compile(r"okl?ab\(\s*[^)]*\)", re.IGNORECASE)
_CSS_VAR_RE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)(?:\s*,\s*[^)]*)?\)")

_PX_VALUE_RE = re.compile(r"(?<![\w.-])(-?\d+(?:\.\d+)?)px\b")
_REM_VALUE_RE = re.compile(r"(?<![\w.-])(-?\d+(?:\.\d+)?)rem\b")
_ARBITRARY_SPACING_RE = re.compile(
    r"\b(?:p|px|py|pt|pr|pb|pl|m|mx|my|mt|mr|mb|ml|gap(?:-x|-y)?|"
    r"space-x|space-y|w|h|min-w|min-h|max-w|max-h|top|right|bottom|left)-\[(-?\d+(?:\.\d+)?(?:px|rem|em|%))\]"
)
_SCALE_SPACING_RE = re.compile(
    r"\b(p[xytrbl]?|m[xytrbl]?|gap(?:-x|-y)?|space-[xy])-(\d+(?:\.\d+)?)\b"
)

_ROUNDED_RE = re.compile(r"\brounded(?:-[trblse])?(?:-(?:sm|md|lg|xl|2xl|3xl|full|none))?\b")
_ROUNDED_ARBITRARY_RE = re.compile(
    r"\brounded(?:-[trblse])?-\[(-?\d+(?:\.\d+)?(?:px|rem|em|%))\]"
)
_BORDER_RADIUS_CSS_RE = re.compile(
    r"border-radius\s*:\s*([^;]+);?", re.IGNORECASE
)

_SHADOW_UTILITY_RE = re.compile(r"\bshadow(?:-(?:sm|md|lg|xl|2xl|inner|none))?\b")
_BOX_SHADOW_CSS_RE = re.compile(r"box-shadow\s*:\s*([^;]+);?", re.IGNORECASE)
_BOX_SHADOW_JSX_RE = re.compile(r"boxShadow\s*:\s*[\"']([^\"']+)[\"']")

_FONT_SIZE_UTILITY_RE = re.compile(
    r"\btext-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl|8xl|9xl)\b"
)
_FONT_WEIGHT_UTILITY_RE = re.compile(
    r"\bfont-(thin|extralight|light|normal|medium|semibold|bold|extrabold|black)\b"
)
_FONT_SIZE_CSS_RE = re.compile(r"font-size\s*:\s*([^;]+);?", re.IGNORECASE)
_FONT_WEIGHT_CSS_RE = re.compile(r"font-weight\s*:\s*([^;]+);?", re.IGNORECASE)
_FONT_FAMILY_CSS_RE = re.compile(r"font-family\s*:\s*([^;]+);?", re.IGNORECASE)
_FONT_SIZE_JSX_RE = re.compile(r"fontSize\s*:\s*[\"']([^\"']+)[\"']")
_FONT_WEIGHT_JSX_RE = re.compile(r"fontWeight\s*:\s*[\"']([^\"']+)[\"']")
_FONT_FAMILY_JSX_RE = re.compile(r"fontFamily\s*:\s*[\"']([^\"']+)[\"']")

_JSX_OPEN_TAG_RE = re.compile(r"<([A-Z][A-Za-z0-9_.]*)[\s/>]")
_IMPORT_FROM_RE = re.compile(
    r"import\s+(?:type\s+)?(\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


def _dedupe_sorted(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        if raw is None:
            continue
        v = str(raw).strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return tuple(sorted(out))


def _stringify_variable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        # Figma often exposes ``{"r":0.22,"g":0.74,"b":0.97}`` for colours.
        if {"r", "g", "b"} <= set(value.keys()):
            r = int(round(float(value.get("r", 0)) * 255))
            g = int(round(float(value.get("g", 0)) * 255))
            b = int(round(float(value.get("b", 0)) * 255))
            return f"#{r:02x}{g:02x}{b:02x}"
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _classify_variable_kind(name: str, value: str) -> str:
    lname = name.lower()
    if lname.startswith("shadow") or "/shadow" in lname:
        return "shadow"
    if any(
        lname.startswith(p) or f"/{p}" in lname
        for p in ("color", "colour", "fg", "bg", "border", "stroke", "fill")
    ):
        return "color"
    if any(
        lname.startswith(p) or f"/{p}" in lname
        for p in ("space", "spacing", "gap", "padding", "margin", "size")
    ):
        return "spacing"
    if lname.startswith("radius") or "/radius" in lname or "corner" in lname:
        return "radius"
    if any(
        lname.startswith(p) or f"/{p}" in lname
        for p in ("font", "type", "text", "typography")
    ):
        return "font"
    val = value.strip()
    if (
        _HEX_COLOR_RE.fullmatch(val)
        or _RGB_COLOR_RE.fullmatch(val)
        or _HSL_COLOR_RE.fullmatch(val)
    ):
        return "color"
    if val.endswith(("px", "rem", "em")):
        return "spacing"
    return "other"


def _variables_to_tokens(
    variables: Mapping[str, Any],
) -> tuple[MobileFigmaToken, ...]:
    """Coerce a Figma variables map into :class:`MobileFigmaToken` list."""
    out: list[MobileFigmaToken] = []
    for raw_name, raw_val in variables.items():
        name = str(raw_name).strip()
        if not name:
            continue
        value = _stringify_variable(raw_val)
        kind = _classify_variable_kind(name, value)
        try:
            out.append(MobileFigmaToken(name=name, value=value, kind=kind))
        except ValueError:
            continue
    out.sort(key=lambda t: (t.kind, t.name))
    return tuple(out)


def _synthesize_tokens(
    colors: Sequence[str],
    spacing: Sequence[str],
    radii: Sequence[str],
    shadows: Sequence[str],
    typography: Sequence[str],
) -> tuple[MobileFigmaToken, ...]:
    """Produce synthetic observation tokens from bare extraction lists."""
    out: list[MobileFigmaToken] = []
    for idx, value in enumerate(colors):
        out.append(MobileFigmaToken(
            name=f"observed-color-{idx + 1}", value=value, kind="color",
        ))
    for idx, value in enumerate(spacing):
        out.append(MobileFigmaToken(
            name=f"observed-spacing-{idx + 1}", value=value, kind="spacing",
        ))
    for idx, value in enumerate(radii):
        out.append(MobileFigmaToken(
            name=f"observed-radius-{idx + 1}", value=value, kind="radius",
        ))
    for idx, value in enumerate(shadows):
        out.append(MobileFigmaToken(
            name=f"observed-shadow-{idx + 1}", value=value, kind="shadow",
        ))
    for idx, value in enumerate(typography):
        out.append(MobileFigmaToken(
            name=f"observed-typography-{idx + 1}", value=value, kind="font",
        ))
    return tuple(out)


def _combine_tokens(
    *groups: tuple[MobileFigmaToken, ...],
) -> tuple[MobileFigmaToken, ...]:
    seen: set[tuple[str, str, str]] = set()
    out: list[MobileFigmaToken] = []
    for group in groups:
        for t in group:
            key = (t.kind, t.name, t.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda t: (t.kind, t.name, t.value))
    return tuple(out)


def extract_from_context(
    context: MobileFigmaDesignContext,
) -> MobileFigmaExtraction:
    """Pull design tokens / colours / spacing / JSX hierarchy from ``context``."""
    code = context.code or ""

    if not code.strip():
        return MobileFigmaExtraction(
            design_tokens=_variables_to_tokens(context.variables),
            screenshot_attached=context.has_screenshot,
            parse_succeeded=False,
            warnings=("empty_code",),
        )

    colors = _dedupe_sorted(
        [m.group(0) for m in _HEX_COLOR_RE.finditer(code)]
        + [m.group(0).lower() for m in _RGB_COLOR_RE.finditer(code)]
        + [m.group(0).lower() for m in _HSL_COLOR_RE.finditer(code)]
        + [m.group(0).lower() for m in _OKLCH_COLOR_RE.finditer(code)]
        + [m.group(0).lower() for m in _OKLAB_COLOR_RE.finditer(code)]
        + [f"var({m.group(1)})" for m in _CSS_VAR_RE.finditer(code)]
    )

    spacing_values = _dedupe_sorted(
        [f"{m.group(1)}px" for m in _PX_VALUE_RE.finditer(code)]
        + [f"{m.group(1)}rem" for m in _REM_VALUE_RE.finditer(code)]
        + [m.group(1) for m in _ARBITRARY_SPACING_RE.finditer(code)]
        + [f"scale:{m.group(1)}-{m.group(2)}"
           for m in _SCALE_SPACING_RE.finditer(code)]
    )

    radii = _dedupe_sorted(
        [m.group(0) for m in _ROUNDED_RE.finditer(code) if m.group(0) != "rounded"]
        + [m.group(0) for m in _ROUNDED_ARBITRARY_RE.finditer(code)]
        + [m.group(1).strip() for m in _BORDER_RADIUS_CSS_RE.finditer(code)]
    )

    shadows = _dedupe_sorted(
        [m.group(0) for m in _SHADOW_UTILITY_RE.finditer(code) if m.group(0) != "shadow"]
        + [m.group(1).strip() for m in _BOX_SHADOW_CSS_RE.finditer(code)]
        + [m.group(1).strip() for m in _BOX_SHADOW_JSX_RE.finditer(code)]
    )

    typography = _dedupe_sorted(
        [f"text-{m.group(1)}" for m in _FONT_SIZE_UTILITY_RE.finditer(code)]
        + [f"font-{m.group(1)}" for m in _FONT_WEIGHT_UTILITY_RE.finditer(code)]
        + [f"size:{m.group(1).strip()}" for m in _FONT_SIZE_CSS_RE.finditer(code)]
        + [f"weight:{m.group(1).strip()}" for m in _FONT_WEIGHT_CSS_RE.finditer(code)]
        + [f"family:{m.group(1).strip()}" for m in _FONT_FAMILY_CSS_RE.finditer(code)]
        + [f"size:{m.group(1).strip()}" for m in _FONT_SIZE_JSX_RE.finditer(code)]
        + [f"weight:{m.group(1).strip()}" for m in _FONT_WEIGHT_JSX_RE.finditer(code)]
        + [f"family:{m.group(1).strip()}" for m in _FONT_FAMILY_JSX_RE.finditer(code)]
    )

    jsx_tags = _dedupe_sorted(
        m.group(1) for m in _JSX_OPEN_TAG_RE.finditer(code)
    )

    imports = _dedupe_sorted(
        m.group(2) for m in _IMPORT_FROM_RE.finditer(code)
    )

    design_tokens = _combine_tokens(
        _variables_to_tokens(context.variables),
        _synthesize_tokens(colors, spacing_values, radii, shadows, typography),
    )

    return MobileFigmaExtraction(
        design_tokens=design_tokens,
        color_values=colors,
        spacing_values=spacing_values,
        typography=typography,
        radii=radii,
        shadows=shadows,
        component_hierarchy=jsx_tags,
        imported_components=imports,
        screenshot_attached=context.has_screenshot,
        parse_succeeded=True,
        warnings=(),
    )


# ── Prompt construction (deterministic) ──────────────────────────────


_MOBILE_PROMPT_HEADER = (
    "# Mobile generation — Figma MCP → SwiftUI + Compose + Flutter\n"
    "You are the OmniSight Mobile UI Designer.  The block below is the\n"
    "reference output of the Figma MCP `get_design_context` tool for a\n"
    "specific node — originally emitted as React + Tailwind.  Treat it\n"
    "as a REFERENCE for intent and layout, NOT final code.  Your job\n"
    "is to rebuild this surface as three platform-native mobile\n"
    "component trees:\n"
    "  * SwiftUI view (iOS 16+)\n"
    "  * Jetpack Compose composable (Material 3, compileSdk 35 / minSdk 24)\n"
    "  * Flutter widget (3.22+)\n"
    "Before emitting, call `backend.mobile_component_registry.\n"
    "get_mobile_components()` — the block labelled 'Mobile component\n"
    "registry' below is its rendered form.  Pick components from it\n"
    "only; never resurrect deprecated APIs (NavigationView,\n"
    "BottomNavigationBar M2, ObservableObject, …) from training memory."
)

_MOBILE_PROMPT_RULES = (
    "## Generation rules (MUST follow)\n"
    "1. Emit THREE fenced code blocks — one per platform — in this\n"
    "   exact order, with these exact fence languages:\n"
    "   ```swift   (SwiftUI)\n"
    "   ```kotlin  (Jetpack Compose)\n"
    "   ```dart    (Flutter)\n"
    "   No prose between blocks beyond a single `// Platform:` comment\n"
    "   header inside each block.  Do not emit any other fences.\n"
    "2. Three platforms must be semantically equivalent — same layout\n"
    "   intent, same component hierarchy, same interaction contract.\n"
    "   Differences are only where the platform idiom requires\n"
    "   (NavigationStack vs. NavigationBar vs. go_router).\n"
    "3. NEVER hard-code hex / pt / dp / sp.  Map Figma values onto:\n"
    "   * SwiftUI  — SF Symbols + .font(.headline/.body) + system\n"
    "                semantic colours (Color.primary, etc.) + \n"
    "                design token constants referenced by name only.\n"
    "   * Compose  — MaterialTheme.colorScheme.* + \n"
    "                MaterialTheme.typography.* + 4dp spacing grid.\n"
    "   * Flutter  — Theme.of(context).colorScheme.* + \n"
    "                Theme.of(context).textTheme.* + \n"
    "                MaterialTheme spacing tokens.\n"
    "   If no matching token exists, reference a TODO token name and\n"
    "   leave a `// TODO(token)` comment — do NOT invent a hex.\n"
    "4. Replace the React/Tailwind reference's absolute positioning\n"
    "   (``top-[…] left-[…] w-[1440px]``) with platform-idiomatic\n"
    "   adaptive layout:\n"
    "   * SwiftUI  — VStack/HStack/ZStack + size-class via\n"
    "                @Environment(\\.horizontalSizeClass).\n"
    "   * Compose  — Column/Row/Box + WindowSizeClass.\n"
    "   * Flutter  — Column/Row/Stack + MediaQuery.sizeOf(context).\n"
    "   Every root surface must handle safe-area (safeAreaInset / \n"
    "   WindowInsets / MediaQuery.viewPaddingOf).\n"
    "5. Touch targets ≥ 44×44 pt (iOS) / 48×48 dp (Android/Flutter).\n"
    "6. A11y baseline:\n"
    "   * SwiftUI  — .accessibilityLabel on icon-only Buttons,\n"
    "                .accessibilityHint where useful,\n"
    "                Dynamic Type via semantic .font(...).\n"
    "   * Compose  — contentDescription on IconButton / Icon,\n"
    "                semantics {} for custom widgets.\n"
    "   * Flutter  — Semantics(label: …) on icon-only widgets,\n"
    "                Tooltip + semanticLabel on bare Icons.\n"
    "7. Dark-mode parity — rely on platform semantic colours (never\n"
    "   `Color(red:green:blue:)` / `Color(0xFF…)` / `Color(int)`).\n"
    "8. Output shape — three fenced blocks only, no prose after the\n"
    "   last block."
)


def build_mobile_generation_prompt(
    *,
    context: MobileFigmaDesignContext,
    extraction: MobileFigmaExtraction | None = None,
    project_root: Path | str | None,
    brief: str | None = None,
    tokens: DesignTokens | None = None,
    platforms: Sequence[str] | None = None,
) -> str:
    """Return a deterministic three-platform generation prompt.

    The prompt interpolates the sibling registry + tokens blocks
    verbatim — both are themselves deterministic, so the whole prompt
    is byte-stable for a given (context, brief, project state,
    platforms).
    """
    if extraction is None:
        extraction = extract_from_context(context)

    plats: tuple[str, ...]
    if platforms is None:
        plats = TARGET_PLATFORMS
    else:
        plats = tuple(platforms)
        for p in plats:
            if p not in TARGET_PLATFORMS:
                raise ValueError(
                    f"Unknown platform {p!r}; must be one of {TARGET_PLATFORMS}"
                )
        if not plats:
            raise ValueError("platforms must be non-empty when supplied")

    registry_block = render_mobile_registry_block(platforms=plats)
    if tokens is not None:
        tokens_block = tokens.to_agent_context()
    else:
        tokens_block = render_design_tokens_block(project_root=project_root)

    context_block = _render_context_block(context)
    extraction_block = _render_extraction_block(extraction)
    reference_code_block = _render_reference_code_block(context.code)
    platforms_block = _render_platforms_block(plats)

    brief_block = (
        f"## Caller brief\n{brief.strip()}"
        if brief and brief.strip()
        else "## Caller brief\n(none)"
    )

    sections = [
        _MOBILE_PROMPT_HEADER,
        platforms_block,
        context_block,
        extraction_block,
        reference_code_block,
        registry_block,
        tokens_block,
        brief_block,
        _MOBILE_PROMPT_RULES,
    ]
    return "\n\n".join(section.strip() for section in sections).strip() + "\n"


def _render_platforms_block(platforms: Sequence[str]) -> str:
    lines = ["## Target platforms"]
    for plat in platforms:
        lang = PLATFORM_LANGS.get(plat, "")
        lines.append(f"- {plat} (fenced as ```{lang})")
    return "\n".join(lines)


def _render_context_block(context: MobileFigmaDesignContext) -> str:
    lines: list[str] = ["## Figma source"]
    lines.append(f"- file_key: `{context.file_key}`")
    lines.append(f"- node_id: `{context.node_id}`")
    lines.append(f"- url: {context.source or '(none)'}")
    lines.append(
        f"- screenshot: "
        f"{'attached (multimodal)' if context.has_screenshot else 'not attached'}"
    )
    lines.append(f"- variables: {len(context.variables)} exposed by MCP")
    lines.append(f"- assets: {len(context.asset_urls)} download URL(s)")
    return "\n".join(lines)


def _render_extraction_block(extraction: MobileFigmaExtraction) -> str:
    lines: list[str] = ["## Figma extraction"]

    def _list(header: str, items: Sequence[str], limit: int = 24) -> None:
        if not items:
            lines.append(f"{header}: (none detected)")
            return
        shown = items[:limit]
        more = "" if len(items) <= limit else f" (+{len(items) - limit} more)"
        lines.append(f"{header}:")
        for item in shown:
            lines.append(f"  - {item}")
        if more:
            lines.append(f"  … {more.strip()}")

    _list("Colours seen", extraction.color_values)
    _list("Spacing values seen", extraction.spacing_values)
    _list("Radii seen", extraction.radii)
    _list("Shadows seen", extraction.shadows)
    _list("Typography seen", extraction.typography)
    _list("Components in reference tree", extraction.component_hierarchy)
    _list("Imports in reference", extraction.imported_components)

    if extraction.design_tokens:
        lines.append("Design-variable tokens (from MCP + synthesised):")
        for token in extraction.design_tokens[:32]:
            lines.append(f"  - [{token.kind}] {token.name} = {token.value}")
        if len(extraction.design_tokens) > 32:
            lines.append(f"  … (+{len(extraction.design_tokens) - 32} more)")
    else:
        lines.append("Design-variable tokens: (none)")

    if extraction.warnings:
        lines.append("Parse warnings: " + ", ".join(extraction.warnings))
    return "\n".join(lines)


#: Hard cap on reference-code bytes interpolated into the prompt. Figma
#: reference snippets can be kilobytes long; past ~8 KB the model loses
#: the thread of the surrounding rules, so truncate explicitly.
_REFERENCE_CODE_CAP = 8_000


def _render_reference_code_block(code: str) -> str:
    cleaned = (code or "").rstrip()
    if not cleaned:
        return "## Figma reference code\n(MCP returned no reference code)"
    if len(cleaned) > _REFERENCE_CODE_CAP:
        head = cleaned[: _REFERENCE_CODE_CAP]
        dropped = len(cleaned) - _REFERENCE_CODE_CAP
        cleaned = head + f"\n// … (truncated {dropped} bytes)\n"
    return (
        "## Figma reference code\n"
        "(treat as reference, not final)\n"
        "```tsx\n"
        f"{cleaned}\n"
        "```"
    )


# ── Multimodal message ───────────────────────────────────────────────


def build_multimodal_message(
    context: MobileFigmaDesignContext,
    prompt: str,
) -> Any:
    """Return a LangChain ``HumanMessage`` for the given context.

    If the context carries a screenshot, the message uses the same
    ``[text, image]`` shape as
    :func:`backend.vision_to_ui.build_multimodal_message`.  Otherwise a
    text-only ``HumanMessage`` is returned — the LLM can still reason
    about the reference code.
    """
    if context.has_screenshot:
        assert context.screenshot is not None
        return _build_multimodal_message(context.screenshot, prompt)
    from backend.llm_adapter import HumanMessage
    return HumanMessage(content=prompt)


# ── Response parsing ─────────────────────────────────────────────────


#: Matches an opening fence with an optional language tag.  Supports
#: both ``` and ~~~ fences (the Figma MCP → Anthropic path has been
#: observed to use both).
_FENCE_OPEN_RE = re.compile(
    r"(?P<fence>```+|~~~+)\s*(?P<lang>[A-Za-z0-9_+#-]*)\s*\n"
)


def _iter_fenced_blocks(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(lang, body)`` tuples for each fenced block in ``text``.

    ``lang`` is lower-cased and stripped; empty when the fence has no
    language tag.  Unterminated fences are ignored.
    """
    pos = 0
    while pos < len(text):
        m = _FENCE_OPEN_RE.search(text, pos)
        if not m:
            return
        fence = m.group("fence")
        lang = (m.group("lang") or "").strip().lower()
        body_start = m.end()
        # Close fence must match length (```) (3+ backticks may be used
        # to allow nested backticks inside).  Match on the exact fence
        # string followed by newline / end of string.
        close_re = re.compile(
            r"(?m)^" + re.escape(fence) + r"\s*(?:\n|\Z)"
        )
        cm = close_re.search(text, body_start)
        if not cm:
            return
        body = text[body_start: cm.start()]
        yield lang, body
        pos = cm.end()


def extract_mobile_code_from_response(response_text: str) -> MobileCodeOutputs:
    """Return a :class:`MobileCodeOutputs` from a model response.

    The response is expected to contain three fenced blocks, one per
    platform (``swift`` / ``kotlin`` / ``dart``).  Common aliases are
    also accepted:

    * ``swift``, ``swiftui``        → SwiftUI
    * ``kotlin``, ``kt``, ``compose`` → Jetpack Compose
    * ``dart``, ``flutter``         → Flutter

    If the same language appears multiple times, the first block wins.
    Unknown fence languages are ignored (the model may emit an
    explanatory ```text block — we don't treat that as an error).
    """
    if not response_text:
        return MobileCodeOutputs()

    found: dict[str, str] = {}
    for lang, body in _iter_fenced_blocks(response_text):
        for plat, aliases in _PLATFORM_LANG_ALIASES.items():
            if plat in found:
                continue
            if lang in aliases:
                found[plat] = body.strip("\n")
                break

    return MobileCodeOutputs(
        swift=found.get("swiftui", ""),
        kotlin=found.get("compose", ""),
        dart=found.get("flutter", ""),
    )


# ── Pipeline entry points ────────────────────────────────────────────


ChatInvoker = Callable[[list], str]
"""An injectable chat invocation: given a list of LangChain messages,
return the assistant text.  Tests wire in a fake; production wires
:func:`backend.llm_adapter.invoke_chat` (partially applied)."""


def _default_invoker(
    *,
    provider: str | None,
    model: str | None,
    llm: Any | None,
) -> ChatInvoker:
    """Return a chat invoker bound to the requested provider/model."""
    from backend.llm_adapter import invoke_chat

    def _invoke(messages: list) -> str:
        try:
            return invoke_chat(
                messages,
                provider=provider,
                model=model,
                llm=llm,
            )
        except Exception as exc:  # defensive — surface as warning, not crash
            logger.warning("figma_to_mobile chat invocation failed: %s", exc)
            return ""

    return _invoke


def _dedupe_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def generate_mobile_from_figma(
    context: MobileFigmaDesignContext,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    provider: str | None = DEFAULT_FIGMA_MOBILE_PROVIDER,
    model: str | None = DEFAULT_FIGMA_MOBILE_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    extraction: MobileFigmaExtraction | None = None,
    platforms: Sequence[str] | None = None,
) -> MobileFigmaGenerationResult:
    """End-to-end: Figma context → extraction → three-platform code.

    Graceful fallback contract:
      * if ``invoker`` returns ``""``, the result has
        ``warnings=("llm_unavailable",)`` and empty outputs;
      * if the model response can be parsed but one or more platform
        fences are missing, the result carries ``"<plat>_missing"``
        warnings (``"swiftui_missing"`` / ``"compose_missing"`` /
        ``"flutter_missing"``) AND the non-empty outputs are returned
        so the caller can ship a partial diff if they want;
      * if the context carries ``code == ""`` and no screenshot, the
        extraction's ``empty_code`` warning propagates and we still
        call the LLM — the model may rely on design variables alone.
    """
    if not isinstance(context, MobileFigmaDesignContext):
        raise TypeError("context must be a MobileFigmaDesignContext")

    warnings: list[str] = []
    if extraction is None:
        extraction = extract_from_context(context)
    for w in extraction.warnings:
        if w not in warnings:
            warnings.append(w)
    md_parse_warnings = context.metadata.get("_parse_warnings") or ()
    if isinstance(md_parse_warnings, (list, tuple)):
        for w in md_parse_warnings:
            s = str(w)
            if s and s not in warnings:
                warnings.append(s)

    invoke = invoker or _default_invoker(
        provider=provider, model=model, llm=llm,
    )

    tokens = load_design_tokens(project_root) if project_root else None
    prompt = build_mobile_generation_prompt(
        context=context,
        extraction=extraction,
        project_root=project_root,
        brief=brief,
        tokens=tokens,
        platforms=platforms,
    )
    message = build_multimodal_message(context, prompt)

    response_text = invoke([message])
    if not response_text:
        warnings.append("llm_unavailable")
        return MobileFigmaGenerationResult(
            context=context,
            extraction=extraction,
            outputs=MobileCodeOutputs(),
            raw_response="",
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    outputs = extract_mobile_code_from_response(response_text)
    targeted = tuple(platforms) if platforms else TARGET_PLATFORMS
    for plat in targeted:
        if not outputs.platform_map[plat].strip():
            warnings.append(f"{plat}_missing")

    return MobileFigmaGenerationResult(
        context=context,
        extraction=extraction,
        outputs=outputs,
        raw_response=response_text,
        warnings=tuple(_dedupe_preserve(warnings)),
        model=model,
        provider=provider,
    )


def run_figma_to_mobile(
    *,
    file_key: str,
    node_id: str,
    mcp_response: Any = None,
    context: MobileFigmaDesignContext | None = None,
    brief: str | None = None,
    project_root: Path | str | None = None,
    provider: str | None = DEFAULT_FIGMA_MOBILE_PROVIDER,
    model: str | None = DEFAULT_FIGMA_MOBILE_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    platforms: Sequence[str] | None = None,
) -> dict:
    """Agent-callable entry — returns a JSON-safe dict.

    Exactly one of ``mcp_response`` or ``context`` must be supplied.
    """
    if (mcp_response is None) == (context is None):
        raise ValueError(
            "run_figma_to_mobile requires exactly one of mcp_response / context"
        )
    if context is None:
        context = from_mcp_response(
            mcp_response, file_key=file_key, node_id=node_id,
        )
    else:
        if context.file_key != normalize_file_key(file_key):
            raise ValueError(
                "context.file_key does not match file_key argument"
            )
        if context.node_id != normalize_node_id(node_id):
            raise ValueError(
                "context.node_id does not match node_id argument"
            )

    result = generate_mobile_from_figma(
        context,
        brief=brief,
        project_root=project_root,
        provider=provider,
        model=model,
        llm=llm,
        invoker=invoker,
        platforms=platforms,
    )
    return result.to_dict()

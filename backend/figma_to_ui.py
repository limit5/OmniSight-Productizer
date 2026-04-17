"""V1 #6 (issue #317) — Figma MCP → UI code pipeline.

Bridges the Figma MCP server (``mcp__claude_ai_Figma__get_design_context``)
into the OmniSight UI Designer's generation path. The MCP returns a
reference code string, a screenshot, and contextual metadata (design
variables, download URLs, annotations) for a given ``(fileKey, nodeId)``
pair. This module turns that raw response into:

  1. a validated :class:`FigmaDesignContext` — the inputs our LLM
     prompt will interpolate;
  2. a :class:`FigmaExtraction` — distilled design tokens + component
     hierarchy + spacing / typography values pulled out of the
     reference code by deterministic regex scanners;
  3. and, in the full pipeline, a :class:`FigmaGenerationResult` —
     extraction + generated TSX + :class:`LintReport` from the sibling
     :mod:`backend.component_consistency_linter`, optionally after one
     mechanical auto-fix pass.

Why this module exists
----------------------

The Figma MCP produces *React + Tailwind* code enriched with hints, but
the Figma server docs explicitly call that output a **reference, not
final code**: the agent must adapt to the target project's stack,
components, and token system. That adaptation is exactly the
responsibility the UI Designer skill pins on us — the sibling modules
already hold the fact side:

* :mod:`backend.ui_component_registry` tells the agent **what** shadcn
  components are installed;
* :mod:`backend.design_token_loader` tells the agent **how** to style
  them (live tokens from ``globals.css``);
* :mod:`backend.component_consistency_linter` enforces the
  anti-pattern gate on the final TSX.

This module provides the Figma-specific glue:

* ``get_design_context`` isn't a *reader* function; it's an LLM-driven
  extraction tool call from an MCP server. The Python backend can't
  invoke it directly (MCP lives in the agent harness). Instead, the
  agent passes the MCP response here via :func:`from_mcp_response`,
  and we parse, validate, and feed it downstream.
* The reference code frequently inlines hex colours / hard-pinned
  palette classes / absolute-positioning. :func:`extract_from_context`
  surfaces those as structured hints so the generation prompt can
  instruct the model to **map them onto the project's design tokens**
  instead of copying the raw values.
* The pipeline is deterministic where it can be (prompt construction
  is byte-stable — prompt-cache friendly) and graceful where it can't
  (LLM failures surface as ``warnings=("llm_unavailable",)`` rather
  than tracebacks mid-prompt).

Contract (pinned by ``backend/tests/test_figma_to_ui.py``)
-----------------------------------------------------------

* :data:`FIGMA_SCHEMA_VERSION` bumps when ``to_dict()`` shape changes.
* :class:`FigmaDesignContext` / :class:`FigmaExtraction` /
  :class:`FigmaGenerationResult` / :class:`FigmaToken` are frozen,
  validated, and JSON-serialisable.
* :func:`normalize_node_id` accepts both ``"123:456"`` and
  ``"123-456"`` (MCP url shape) and returns the canonical ``"123:456"``.
* :func:`from_mcp_response` tolerates the common MCP response shapes
  (plain dict, wrapped ``content=[{type:"text", text:"..."}]`` list,
  stringified JSON) and surfaces missing fields as warnings rather
  than raising — the pipeline must keep running on partial context.
* :func:`build_figma_generation_prompt` is pure: same inputs →
  byte-identical prompt across calls.
* Every pipeline entry point returns a well-formed
  :class:`FigmaGenerationResult` (or :meth:`~FigmaGenerationResult.to_dict`
  payload) even on failure — callers inspect ``warnings`` to decide
  retry / escalate / surface-to-operator.
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

from backend.component_consistency_linter import (
    LINTER_SCHEMA_VERSION,
    LintReport,
    auto_fix_code,
    lint_code,
)
from backend.design_token_loader import (
    DesignTokens,
    load_design_tokens,
    render_agent_context_block as render_design_tokens_block,
)
from backend.ui_component_registry import (
    render_agent_context_block as render_registry_block,
)
from backend.vision_to_ui import (
    MAX_IMAGE_BYTES,
    SUPPORTED_MIME_TYPES,
    VisionImage,
    build_multimodal_message as _build_multimodal_message,
    extract_tsx_from_response,
    validate_image,
)

logger = logging.getLogger(__name__)


__all__ = [
    "FIGMA_SCHEMA_VERSION",
    "DEFAULT_FIGMA_MODEL",
    "DEFAULT_FIGMA_PROVIDER",
    "TOKEN_KINDS",
    "FigmaToken",
    "FigmaDesignContext",
    "FigmaExtraction",
    "FigmaGenerationResult",
    "normalize_node_id",
    "normalize_file_key",
    "canonical_figma_source",
    "from_mcp_response",
    "validate_figma_context",
    "extract_from_context",
    "build_figma_generation_prompt",
    "build_multimodal_message",
    "generate_ui_from_figma",
    "run_figma_to_ui",
]


# Bump when the shape of a FigmaDesignContext / FigmaExtraction /
# FigmaGenerationResult dict changes — callers cache prompts / responses
# keyed off this version.
FIGMA_SCHEMA_VERSION = "1.0.0"

#: Default LLM model for Figma-driven generation. Opus 4.7 because the
#: reference-code adaptation step benefits from the deeper model —
#: translating hex → token and absolute-positioning → flex/grid is a
#: reasoning task, not a transcription task.
DEFAULT_FIGMA_MODEL = "claude-opus-4-7"
DEFAULT_FIGMA_PROVIDER = "anthropic"

#: Fixed kinds accepted by :class:`FigmaToken`. Keep in sync with
#: :data:`backend.design_token_loader.KINDS` plus ``other``.
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
class FigmaToken:
    """One design value surfaced from the Figma reference code.

    ``name`` is a human-readable handle (either the original variable
    name from the Figma response, or a synthetic handle assigned by
    :func:`extract_from_context`); ``value`` is the raw CSS value as
    emitted by Figma. The agent is expected to **map** these onto the
    project's design tokens (:mod:`backend.design_token_loader`), not
    to inline them.
    """

    name: str
    value: str
    kind: str = "other"

    def __post_init__(self) -> None:
        if self.kind not in TOKEN_KINDS:
            raise ValueError(
                f"Unknown FigmaToken kind {self.kind!r}; "
                f"must be one of {TOKEN_KINDS}"
            )
        if not self.name:
            raise ValueError("FigmaToken.name must be non-empty")
        if self.value is None:
            raise ValueError("FigmaToken.value must not be None")


@dataclass(frozen=True)
class FigmaDesignContext:
    """Validated inputs from an MCP ``get_design_context`` response.

    Fields:
        file_key: the Figma file key (opaque string).
        node_id: canonical ``"123:456"`` node id.
        code: the reference code string emitted by MCP (usually React +
            Tailwind, sometimes enriched with token comments).
        screenshot: optional :class:`VisionImage` of the node.
        variables: design-variable map exposed by the MCP
            (e.g. ``{"color/primary": "#38bdf8"}``); may be empty.
        metadata: arbitrary JSON-safe dict from the MCP (annotations,
            frame sizes, code-connect mappings, …).
        asset_urls: ``{"asset_name": "https://…"}`` map returned for
            the image assets referenced in the code.
        source: derived ``"figma.com/design/<fk>?node-id=<nid>"``
            human-readable URL-ish tag for logs.
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
            raise ValueError("FigmaDesignContext.file_key must be non-empty")
        if not self.node_id:
            raise ValueError("FigmaDesignContext.node_id must be non-empty")
        # Freeze inner mappings — MappingProxyType forbids mutation.
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "asset_urls", MappingProxyType(dict(self.asset_urls)))
        if self.screenshot is not None and not isinstance(
            self.screenshot, VisionImage
        ):
            raise TypeError(
                "FigmaDesignContext.screenshot must be a VisionImage"
            )

    @property
    def has_screenshot(self) -> bool:
        return self.screenshot is not None

    def to_dict(self) -> dict:
        return {
            "schema_version": FIGMA_SCHEMA_VERSION,
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
class FigmaExtraction:
    """What we distilled out of a :class:`FigmaDesignContext`.

    All tuples are *sorted + de-duplicated* for byte-stable prompt
    rendering. ``parse_succeeded`` is ``False`` only on total failure
    (e.g. empty code) — the pipeline still returns a well-formed
    extraction so downstream prompt construction cannot raise.
    """

    design_tokens: tuple[FigmaToken, ...] = ()
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
            "schema_version": FIGMA_SCHEMA_VERSION,
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
class FigmaGenerationResult:
    """End-to-end output of :func:`generate_ui_from_figma`."""

    context: FigmaDesignContext
    extraction: FigmaExtraction
    tsx_code: str = ""
    lint_report: LintReport = field(default_factory=LintReport)
    pre_fix_lint_report: LintReport | None = None
    auto_fix_applied: bool = False
    warnings: tuple[str, ...] = ()
    model: str | None = None
    provider: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.lint_report.is_clean and bool(self.tsx_code.strip())

    def to_dict(self) -> dict:
        return {
            "schema_version": FIGMA_SCHEMA_VERSION,
            "linter_schema_version": LINTER_SCHEMA_VERSION,
            "context": self.context.to_dict(),
            "extraction": self.extraction.to_dict(),
            "tsx_code": self.tsx_code,
            "lint_report": self.lint_report.to_dict(),
            "pre_fix_lint_report": (
                self.pre_fix_lint_report.to_dict()
                if self.pre_fix_lint_report is not None
                else None
            ),
            "auto_fix_applied": self.auto_fix_applied,
            "warnings": list(self.warnings),
            "model": self.model,
            "provider": self.provider,
            "is_clean": self.is_clean,
        }


# ── Node-id / file-key normalisation ─────────────────────────────────


_NODE_ID_RE = re.compile(r"^-?\d+[:\-]-?\d+$")


def normalize_node_id(raw: str) -> str:
    """Return the canonical ``"123:456"`` form of a Figma node id.

    Accepts ``"123:456"`` (colon form used by the Figma API) and
    ``"123-456"`` (url form — what appears in ``?node-id=123-456``).

    Raises:
        ValueError: if ``raw`` is empty or doesn't match the pattern.
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
    # Accept either separator; canonicalise to ':' — but only replace
    # the *first* dash that follows a leading digit / '-' run. Strings
    # like "-1-2" must map to "-1:2", not ":1-2".
    if ":" in stripped:
        return stripped
    # Find the split dash: it's the dash that's not a leading sign.
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
    """Return a deterministic human-readable Figma URL-ish handle."""
    # URL shape uses '-' as the node-id separator.
    return (
        "figma.com/design/"
        f"{file_key}?node-id={node_id.replace(':', '-')}"
    )


# ── MCP response parsing ─────────────────────────────────────────────


def _coerce_mcp_payload(response: Any) -> tuple[dict, tuple[str, ...]]:
    """Flatten an MCP response into a single ``dict`` for parsing.

    The Figma MCP returns either a bare JSON dict, a string blob
    containing JSON, or a wrapped ``{"content": [{type:"text", text:
    "..."}]}`` envelope. Return ``({}, warnings)`` on total failure so
    the caller surfaces the shape issue rather than raising.
    """
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

    # Unwrap an MCP "content" list: if `content` is a list of
    # `{type: "text", text: "<json blob>"}` blocks, concatenate the
    # text blocks and try to parse.
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
        # Fall through — treat as raw dict (content wrapper may also
        # carry top-level metadata).

    return dict(response), tuple(warnings)


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


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


def from_mcp_response(
    response: Any,
    *,
    file_key: str,
    node_id: str,
    screenshot_mime_hint: str | None = None,
) -> FigmaDesignContext:
    """Parse an MCP ``get_design_context`` response into a context.

    The function is defensive — missing fields are treated as empty,
    not as errors. The agent passes whatever MCP returned; we pull
    what we can, attach warnings about what we couldn't, and let the
    downstream pipeline decide whether to still invoke the LLM.

    Args:
        response: raw MCP payload (dict / JSON string / content-wrapped).
        file_key: caller-asserted file key (MCP doesn't always echo).
        node_id: caller-asserted node id (same).
        screenshot_mime_hint: used when the MCP omits an explicit mime
            type. Defaults to ``"image/png"`` — what MCP ships today.

    Returns:
        :class:`FigmaDesignContext` — well-formed even on partial input.
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

    variables_raw = _pick(payload, _VARIABLES_KEYS) or {}
    variables = _coerce_mapping(variables_raw)

    metadata_raw = _pick(payload, _METADATA_KEYS) or {}
    metadata = _coerce_mapping(metadata_raw)

    asset_urls_raw = _pick(payload, _ASSET_KEYS) or {}
    asset_urls = {
        str(k): str(v)
        for k, v in _coerce_mapping(asset_urls_raw).items()
        if isinstance(v, (str, bytes)) or v is not None
    }

    warnings = list(parse_warnings) + screenshot_warnings
    if not code_str and not screenshot:
        warnings.append("figma_context_empty")

    # Stash parse warnings under metadata so downstream code can show
    # them without needing a side-channel.
    metadata_out: dict[str, Any] = dict(metadata)
    if warnings:
        existing = metadata_out.get("_parse_warnings")
        if existing is None:
            metadata_out["_parse_warnings"] = list(warnings)
        else:
            metadata_out["_parse_warnings"] = list(existing) + list(warnings)

    return FigmaDesignContext(
        file_key=fk,
        node_id=nid,
        code=code_str,
        screenshot=screenshot,
        variables=variables,
        metadata=metadata_out,
        asset_urls=asset_urls,
        source=canonical_figma_source(fk, nid),
    )


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
            logger.debug("figma screenshot bytes rejected: %s", exc)
            return None, ["screenshot_invalid"]
    if not isinstance(raw, str):
        return None, ["screenshot_invalid"]
    payload = raw.strip()
    if not payload:
        return None, []
    # Data-URL unwrap: ``data:image/png;base64,AAAA…``
    if payload.startswith("data:"):
        header, _, body = payload.partition(",")
        if ";base64" in header:
            declared = header.split(":", 1)[1].split(";", 1)[0].strip()
            if declared:
                mime_type = declared
            payload = body
        else:
            # Un-base64'd data URLs are too rare to support; bail.
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
        logger.debug("figma screenshot validation failed: %s", exc)
        return None, ["screenshot_invalid"]


def validate_figma_context(
    *,
    file_key: str,
    node_id: str,
    code: str = "",
    screenshot: VisionImage | None = None,
    variables: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    asset_urls: Mapping[str, str] | None = None,
) -> FigmaDesignContext:
    """Build a :class:`FigmaDesignContext` from pre-parsed pieces."""
    fk = normalize_file_key(file_key)
    nid = normalize_node_id(node_id)
    return FigmaDesignContext(
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


# Hex colours: 3 / 4 / 6 / 8 digit forms. Matching at word boundary to
# avoid matching inside an ID attribute like `id="abc123"`.
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b")
_RGB_COLOR_RE = re.compile(r"rgba?\(\s*[^)]*\)", re.IGNORECASE)
_HSL_COLOR_RE = re.compile(r"hsla?\(\s*[^)]*\)", re.IGNORECASE)
_OKLCH_COLOR_RE = re.compile(r"oklch\(\s*[^)]*\)", re.IGNORECASE)
_OKLAB_COLOR_RE = re.compile(r"okl?ab\(\s*[^)]*\)", re.IGNORECASE)
_CSS_VAR_RE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)(?:\s*,\s*[^)]*)?\)")

# Spacing values in inline styles / arbitrary Tailwind utilities.
_PX_VALUE_RE = re.compile(r"(?<![\w.-])(-?\d+(?:\.\d+)?)px\b")
_REM_VALUE_RE = re.compile(r"(?<![\w.-])(-?\d+(?:\.\d+)?)rem\b")
_ARBITRARY_SPACING_RE = re.compile(
    r"\b(?:p|px|py|pt|pr|pb|pl|m|mx|my|mt|mr|mb|ml|gap(?:-x|-y)?|"
    r"space-x|space-y|w|h|min-w|min-h|max-w|max-h|top|right|bottom|left)-\[(-?\d+(?:\.\d+)?(?:px|rem|em|%))\]"
)

# Tailwind spacing utilities (`p-4`, `gap-2`) — scan but keep a small
# keep-list to avoid grabbing every class.
_SCALE_SPACING_RE = re.compile(
    r"\b(p[xytrbl]?|m[xytrbl]?|gap(?:-x|-y)?|space-[xy])-(\d+(?:\.\d+)?)\b"
)

_ROUNDED_RE = re.compile(r"\brounded(?:-[trblse])?(?:-(?:sm|md|lg|xl|2xl|3xl|full|none))?\b")
_ROUNDED_ARBITRARY_RE = re.compile(r"\brounded(?:-[trblse])?-\[(-?\d+(?:\.\d+)?(?:px|rem|em|%))\]")
_BORDER_RADIUS_CSS_RE = re.compile(r"border-radius\s*:\s*([^;]+);?", re.IGNORECASE)

_SHADOW_UTILITY_RE = re.compile(r"\bshadow(?:-(?:sm|md|lg|xl|2xl|inner|none))?\b")
_BOX_SHADOW_CSS_RE = re.compile(r"box-shadow\s*:\s*([^;]+);?", re.IGNORECASE)
# Figma MCP emits JSX inline styles (camelCase, quoted values):
# ``boxShadow: "0 4px 24px rgba(0,0,0,0.35)"``.
_BOX_SHADOW_JSX_RE = re.compile(
    r"boxShadow\s*:\s*[\"']([^\"']+)[\"']"
)

_FONT_SIZE_UTILITY_RE = re.compile(r"\btext-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl|8xl|9xl)\b")
_FONT_WEIGHT_UTILITY_RE = re.compile(
    r"\bfont-(thin|extralight|light|normal|medium|semibold|bold|extrabold|black)\b"
)
_FONT_SIZE_CSS_RE = re.compile(r"font-size\s*:\s*([^;]+);?", re.IGNORECASE)
_FONT_WEIGHT_CSS_RE = re.compile(r"font-weight\s*:\s*([^;]+);?", re.IGNORECASE)
_FONT_FAMILY_CSS_RE = re.compile(r"font-family\s*:\s*([^;]+);?", re.IGNORECASE)
# JSX inline camelCase equivalents.
_FONT_SIZE_JSX_RE = re.compile(r"fontSize\s*:\s*[\"']([^\"']+)[\"']")
_FONT_WEIGHT_JSX_RE = re.compile(r"fontWeight\s*:\s*[\"']([^\"']+)[\"']")
_FONT_FAMILY_JSX_RE = re.compile(r"fontFamily\s*:\s*[\"']([^\"']+)[\"']")

_JSX_OPEN_TAG_RE = re.compile(r"<([A-Z][A-Za-z0-9_.]*)[\s/>]")
_IMPORT_FROM_RE = re.compile(
    r"import\s+(?:type\s+)?(\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+[\"']([^\"']+)[\"']",
    re.MULTILINE,
)


def extract_from_context(context: FigmaDesignContext) -> FigmaExtraction:
    """Pull design tokens / colours / spacing / JSX hierarchy from ``context``."""
    code = context.code or ""
    warnings: list[str] = []

    if not code.strip():
        return FigmaExtraction(
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
        + [f"scale:{m.group(1)}-{m.group(2)}" for m in _SCALE_SPACING_RE.finditer(code)]
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

    return FigmaExtraction(
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
        warnings=tuple(warnings),
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


def _variables_to_tokens(
    variables: Mapping[str, Any],
) -> tuple[FigmaToken, ...]:
    """Coerce a Figma variables map into :class:`FigmaToken` list."""
    out: list[FigmaToken] = []
    for raw_name, raw_val in variables.items():
        name = str(raw_name).strip()
        if not name:
            continue
        value = _stringify_variable(raw_val)
        kind = _classify_variable_kind(name, value)
        try:
            out.append(FigmaToken(name=name, value=value, kind=kind))
        except ValueError:
            continue
    out.sort(key=lambda t: (t.kind, t.name))
    return tuple(out)


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
    if any(
        lname.startswith(p) or f"/{p}" in lname
        for p in ("color", "colour", "fg", "bg", "border", "stroke", "fill", "shadow")
    ) and "shadow" not in lname[:8]:
        if lname.startswith("shadow") or "/shadow" in lname:
            return "shadow"
        return "color"
    if lname.startswith("shadow") or "/shadow" in lname:
        return "shadow"
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
    # Fall back to value shape.
    val = value.strip()
    if _HEX_COLOR_RE.fullmatch(val) or _RGB_COLOR_RE.fullmatch(val) or _HSL_COLOR_RE.fullmatch(val):
        return "color"
    if val.endswith(("px", "rem", "em")):
        return "spacing"
    return "other"


def _synthesize_tokens(
    colors: Sequence[str],
    spacing: Sequence[str],
    radii: Sequence[str],
    shadows: Sequence[str],
    typography: Sequence[str],
) -> tuple[FigmaToken, ...]:
    """Produce synthetic tokens from bare extraction lists.

    These are *observations* (e.g. ``"#38bdf8"`` seen in the reference
    code); the name is a stable handle so the prompt can reference
    them, but the agent still maps them onto the real design tokens.
    """
    out: list[FigmaToken] = []
    for idx, value in enumerate(colors):
        out.append(FigmaToken(name=f"observed-color-{idx + 1}", value=value, kind="color"))
    for idx, value in enumerate(spacing):
        out.append(FigmaToken(name=f"observed-spacing-{idx + 1}", value=value, kind="spacing"))
    for idx, value in enumerate(radii):
        out.append(FigmaToken(name=f"observed-radius-{idx + 1}", value=value, kind="radius"))
    for idx, value in enumerate(shadows):
        out.append(FigmaToken(name=f"observed-shadow-{idx + 1}", value=value, kind="shadow"))
    for idx, value in enumerate(typography):
        out.append(FigmaToken(name=f"observed-typography-{idx + 1}", value=value, kind="font"))
    return tuple(out)


def _combine_tokens(
    *groups: tuple[FigmaToken, ...],
) -> tuple[FigmaToken, ...]:
    seen: set[tuple[str, str, str]] = set()
    out: list[FigmaToken] = []
    for group in groups:
        for t in group:
            key = (t.kind, t.name, t.value)
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    out.sort(key=lambda t: (t.kind, t.name, t.value))
    return tuple(out)


# ── Prompt construction (deterministic) ──────────────────────────────


_FIGMA_PROMPT_HEADER = (
    "# UI generation — Figma MCP → shadcn/ui + Tailwind\n"
    "You are the OmniSight UI Designer. The block below is the\n"
    "reference output of the Figma MCP `get_design_context` tool for\n"
    "a specific node. Treat it as a REFERENCE — not final code.\n"
    "Your job is to rebuild this component in React + TSX using the\n"
    "project's installed shadcn/ui primitives and design tokens.\n"
    "You will be linted by backend.component_consistency_linter — a\n"
    "clean lint pass is the acceptance gate."
)

_FIGMA_PROMPT_RULES = (
    "## Generation rules (MUST follow)\n"
    "1. Output a single self-contained React TSX component. Imports\n"
    "   are limited to the shadcn primitives listed above plus `cn`\n"
    "   from `@/lib/utils`.\n"
    "2. Map the Figma reference colours / spacing / radii onto the\n"
    "   project's design tokens. NEVER inline a hex colour, never\n"
    "   pin a Tailwind palette class (e.g. `bg-slate-900`). If no\n"
    "   matching token exists, pick the closest semantic one and\n"
    "   leave a TODO comment — do NOT invent a hex.\n"
    "3. Replace raw <button> / <input> / <textarea> / <select> /\n"
    "   <dialog> / <progress> / <div onClick> from the Figma\n"
    "   reference with the shadcn primitive equivalent. Absolute\n"
    "   positioning from Figma (`top-[…] left-[…]`) must be\n"
    "   rewritten as flex / grid / stack.\n"
    "4. Responsive: mobile-first base + sm/md/lg/xl/2xl. Drop any\n"
    "   fixed `w-[1440px]` canvas widths the Figma reference\n"
    "   carries.\n"
    "5. Respect WAI-ARIA: icon-only buttons get aria-label; form\n"
    "   inputs get <Label htmlFor> or wrap in <Field>.\n"
    "6. This project is dark-only (html { color-scheme: dark }). Do\n"
    "   NOT emit `dark:` prefixes and do NOT write light fallbacks.\n"
    "7. Output MUST be a single fenced code block:\n"
    "   ```tsx\n"
    "   /* code */\n"
    "   ```\n"
    "   No prose before or after."
)


def build_figma_generation_prompt(
    *,
    context: FigmaDesignContext,
    extraction: FigmaExtraction | None = None,
    project_root: Path | str | None,
    brief: str | None = None,
    tokens: DesignTokens | None = None,
) -> str:
    """Return a deterministic TSX-generation prompt.

    The prompt interpolates the sibling registry + tokens blocks
    verbatim — both are themselves deterministic, so the whole prompt
    is byte-stable for a given (context, brief, project state).
    """
    if extraction is None:
        extraction = extract_from_context(context)

    registry_block = render_registry_block(project_root=project_root)
    if tokens is not None:
        tokens_block = tokens.to_agent_context()
    else:
        tokens_block = render_design_tokens_block(project_root=project_root)

    context_block = _render_context_block(context)
    extraction_block = _render_extraction_block(extraction)

    reference_code_block = _render_reference_code_block(context.code)

    brief_block = ""
    if brief and brief.strip():
        brief_block = f"## Caller brief\n{brief.strip()}"
    else:
        brief_block = "## Caller brief\n(none)"

    sections = [
        _FIGMA_PROMPT_HEADER,
        context_block,
        extraction_block,
        reference_code_block,
        registry_block,
        tokens_block,
        brief_block,
        _FIGMA_PROMPT_RULES,
    ]
    return "\n\n".join(section.strip() for section in sections).strip() + "\n"


def _render_context_block(context: FigmaDesignContext) -> str:
    lines: list[str] = ["## Figma source"]
    lines.append(f"- file_key: `{context.file_key}`")
    lines.append(f"- node_id: `{context.node_id}`")
    lines.append(f"- url: {context.source or '(none)'}")
    lines.append(
        f"- screenshot: {'attached (multimodal)' if context.has_screenshot else 'not attached'}"
    )
    lines.append(
        f"- variables: {len(context.variables)} exposed by MCP"
    )
    lines.append(
        f"- assets: {len(context.asset_urls)} download URL(s)"
    )
    return "\n".join(lines)


def _render_extraction_block(extraction: FigmaExtraction) -> str:
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
#: the thread of the surrounding rules, so truncate explicitly and tell
#: the model we truncated.
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
    context: FigmaDesignContext,
    prompt: str,
) -> Any:
    """Return a LangChain ``HumanMessage`` for the given context.

    If the context carries a screenshot, the message uses the same
    ``[text, image]`` shape as :func:`backend.vision_to_ui.build_multimodal_message`.
    Otherwise a text-only ``HumanMessage`` is returned — the LLM can
    still reason about the reference code.
    """
    if context.has_screenshot:
        assert context.screenshot is not None
        return _build_multimodal_message(context.screenshot, prompt)
    from backend.llm_adapter import HumanMessage
    return HumanMessage(content=prompt)


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
            logger.warning("figma_to_ui chat invocation failed: %s", exc)
            return ""

    return _invoke


def generate_ui_from_figma(
    context: FigmaDesignContext,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    provider: str | None = DEFAULT_FIGMA_PROVIDER,
    model: str | None = DEFAULT_FIGMA_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
    extraction: FigmaExtraction | None = None,
) -> FigmaGenerationResult:
    """End-to-end: Figma context → extraction → TSX → lint → auto-fix.

    Graceful fallback contract:
      * if ``invoker`` returns ``""``, the result has
        ``warnings=("llm_unavailable",)`` and empty ``tsx_code``;
      * if generation succeeds but no TSX block can be extracted,
        ``warnings`` includes ``"tsx_missing"`` and ``tsx_code`` is
        the raw response so a human can inspect;
      * if the context carries ``code == ""`` and no screenshot, the
        extraction's ``empty_code`` warning propagates and we still
        call the LLM — the model may rely on design variables alone.
    """
    if not isinstance(context, FigmaDesignContext):
        raise TypeError("context must be a FigmaDesignContext")

    warnings: list[str] = []
    if extraction is None:
        extraction = extract_from_context(context)
    # Bubble up parse warnings so the caller can surface them without
    # having to crack open the metadata dict.
    for w in extraction.warnings:
        if w not in warnings:
            warnings.append(w)
    # Also bubble up any MCP-parse warnings stashed on metadata.
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
    prompt = build_figma_generation_prompt(
        context=context,
        extraction=extraction,
        project_root=project_root,
        brief=brief,
        tokens=tokens,
    )
    message = build_multimodal_message(context, prompt)

    response_text = invoke([message])
    if not response_text:
        warnings.append("llm_unavailable")
        return FigmaGenerationResult(
            context=context,
            extraction=extraction,
            tsx_code="",
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    tsx = extract_tsx_from_response(response_text)
    if not tsx:
        warnings.append("tsx_missing")
        return FigmaGenerationResult(
            context=context,
            extraction=extraction,
            tsx_code=response_text,
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    initial_report = lint_code(tsx, source="figma_to_ui.tsx")
    fixed_tsx = tsx
    applied = False
    final_report = initial_report

    if auto_fix and not initial_report.is_clean:
        fixed_tsx, _remaining = auto_fix_code(tsx)
        if fixed_tsx != tsx:
            applied = True
            final_report = lint_code(fixed_tsx, source="figma_to_ui.tsx")

    return FigmaGenerationResult(
        context=context,
        extraction=extraction,
        tsx_code=fixed_tsx,
        lint_report=final_report,
        pre_fix_lint_report=initial_report if applied else None,
        auto_fix_applied=applied,
        warnings=tuple(_dedupe_preserve(warnings)),
        model=model,
        provider=provider,
    )


def _dedupe_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def run_figma_to_ui(
    *,
    file_key: str,
    node_id: str,
    mcp_response: Any = None,
    context: FigmaDesignContext | None = None,
    brief: str | None = None,
    project_root: Path | str | None = None,
    provider: str | None = DEFAULT_FIGMA_PROVIDER,
    model: str | None = DEFAULT_FIGMA_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
) -> dict:
    """Agent-callable entry — returns a JSON-safe dict.

    Exactly one of ``mcp_response`` or ``context`` must be supplied.
    When the caller passes ``mcp_response``, the raw MCP payload is
    parsed via :func:`from_mcp_response`; when ``context`` is supplied,
    it is used directly (useful in tests and in agent loops that
    already normalised the MCP output).
    """
    if (mcp_response is None) == (context is None):
        raise ValueError(
            "run_figma_to_ui requires exactly one of mcp_response / context"
        )
    if context is None:
        context = from_mcp_response(
            mcp_response, file_key=file_key, node_id=node_id,
        )
    else:
        # Ensure caller-supplied context matches the asserted keys.
        if context.file_key != normalize_file_key(file_key):
            raise ValueError(
                "context.file_key does not match file_key argument"
            )
        if context.node_id != normalize_node_id(node_id):
            raise ValueError(
                "context.node_id does not match node_id argument"
            )

    result = generate_ui_from_figma(
        context,
        brief=brief,
        project_root=project_root,
        provider=provider,
        model=model,
        llm=llm,
        invoker=invoker,
        auto_fix=auto_fix,
    )
    return result.to_dict()

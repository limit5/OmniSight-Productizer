"""V1 #5 (issue #317) — Screenshot / hand-drawn sketch → UI code pipeline.

Accepts a raster image (PNG / JPEG / GIF / WEBP), hands it to a
multimodal LLM (Opus 4.7 by default — see
:data:`DEFAULT_VISION_MODEL`), and returns:

  1. a structured :class:`VisionAnalysis` — what the model sees
     (layout skeleton, dominant colours, component hints, suggested
     shadcn primitives);
  2. and, in the full pipeline, a :class:`VisionGenerationResult` —
     analysis + generated TSX + :class:`LintReport` from the sibling
     :mod:`backend.component_consistency_linter`, optionally after one
     mechanical auto-fix pass.

Why this module exists
----------------------

The UI Designer skill (``configs/roles/ui-designer.md``) names this
module explicitly in its sibling table: *"screenshot → I take the
multimodal result and rebuild it as a shadcn primitive tree, not an
absolute-positioned div soup."*  The sibling modules already pin the
fact-side:

* :mod:`backend.ui_component_registry` tells the agent **what** shadcn
  components are installed, with canonical example TSX;
* :mod:`backend.design_token_loader` tells the agent **how** to style
  them (live colour / radius / spacing tokens from the project's
  ``globals.css``);
* :mod:`backend.component_consistency_linter` enforces the
  anti-pattern gate on whatever TSX the agent emits.

This module glues them to a vision input: the generation prompt is
**deterministic** (byte-identical across calls — prompt-cache stable)
because it interpolates the sibling-produced context blocks
verbatim.  The LLM call is the only non-deterministic step.

Contract (pinned by ``backend/tests/test_vision_to_ui.py``)
------------------------------------------------------------

* :data:`SUPPORTED_MIME_TYPES` is the exact set Anthropic's multimodal
  endpoint accepts today.  ``validate_image`` rejects anything else.
* :data:`MAX_IMAGE_BYTES` = 5 MiB — the Anthropic API hard limit for
  base64-inline images.
* :class:`VisionAnalysis` / :class:`VisionGenerationResult` are frozen
  and JSON-serialisable.
* :func:`build_multimodal_message` produces a LangChain-style
  ``HumanMessage`` whose ``content`` is a ``[{text…}, {image…}]``
  list.  The image block uses the Anthropic base64 image-source
  dictionary shape (which ``langchain_anthropic`` passes through).
* :func:`build_vision_analysis_prompt` and
  :func:`build_ui_generation_prompt` are pure, deterministic
  functions: same inputs → byte-identical strings.
* :func:`parse_vision_analysis` tolerates three common response
  shapes: fenced ``json`` block, bare JSON, or prose with inline key
  markers.  On total failure it returns a minimal ``VisionAnalysis``
  with ``parse_succeeded=False`` rather than raising.
* :func:`extract_tsx_from_response` extracts a fenced TSX block
  (``tsx`` / ``jsx`` / ``tsx react`` variants) or falls back to the
  largest ``<…>``-like span.
* Every pipeline entry point — :func:`analyze_screenshot`,
  :func:`generate_ui_from_vision`, :func:`run_vision_to_ui` — is
  graceful: if the LLM is not configured (``invoke_chat`` returns
  ``""``), the result carries ``warnings=["llm_unavailable"]`` and
  an empty analysis / empty TSX, never a traceback.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from backend import llm_adapter
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

logger = logging.getLogger(__name__)


__all__ = [
    "VISION_SCHEMA_VERSION",
    "DEFAULT_VISION_MODEL",
    "DEFAULT_VISION_PROVIDER",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_MIME_TYPES",
    "VisionImage",
    "VisionAnalysis",
    "VisionGenerationResult",
    "validate_image",
    "build_multimodal_message",
    "build_vision_analysis_prompt",
    "build_ui_generation_prompt",
    "parse_vision_analysis",
    "extract_tsx_from_response",
    "analyze_screenshot",
    "generate_ui_from_vision",
    "run_vision_to_ui",
]


# Bump when the shape of a VisionAnalysis / VisionGenerationResult dict
# changes — callers cache contexts keyed off this.
VISION_SCHEMA_VERSION = "1.0.0"

#: Default multimodal model (Opus 4.7 — the most capable vision model
#: currently available to this project).  Callers can override by
#: passing ``model=`` or a pre-built ``llm``.
DEFAULT_VISION_MODEL = "claude-opus-4-7"
DEFAULT_VISION_PROVIDER = "anthropic"

#: Anthropic's hard limit for inline (base64) images.  Anything larger
#: must be uploaded via the Files API — out of scope for this module.
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB

#: Image mime types the Anthropic multimodal endpoint accepts.
SUPPORTED_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})


# File-header magic bytes for the supported formats.  Used by
# :func:`validate_image` to cross-check ``mime_type`` against the
# actual payload so a caller can't silently mislabel a JPEG as PNG.
_MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # followed by size + "WEBP" — checked below
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class VisionImage:
    """Validated image payload.

    :attr:`data` is the raw bytes, :attr:`mime_type` is one of
    :data:`SUPPORTED_MIME_TYPES`.  Use :func:`validate_image` to
    construct — the dataclass itself re-validates to catch mis-use.
    """

    data: bytes
    mime_type: str
    source: str | None = None  # filename / URL / description for logs

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)):
            raise TypeError("VisionImage.data must be bytes")
        if self.mime_type not in SUPPORTED_MIME_TYPES:
            raise ValueError(
                f"Unsupported mime type {self.mime_type!r}; "
                f"must be one of {sorted(SUPPORTED_MIME_TYPES)}"
            )
        if not self.data:
            raise ValueError("VisionImage.data must be non-empty")
        if len(self.data) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image exceeds {MAX_IMAGE_BYTES} bytes "
                f"(got {len(self.data)}); use the Files API for larger uploads."
            )

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def to_base64(self) -> str:
        """Return the payload as a plain (no padding-stripped) base64 str."""
        return base64.standard_b64encode(bytes(self.data)).decode("ascii")


@dataclass(frozen=True)
class VisionAnalysis:
    """Structured model observation of one screenshot / sketch.

    The fields below are the **minimum** we extract; the model may
    emit extra keys (kept in :attr:`extras`) but consumers should
    rely only on the named fields for behavioural logic.
    """

    layout_summary: str = ""
    color_observations: tuple[str, ...] = ()
    detected_components: tuple[str, ...] = ()
    suggested_primitives: tuple[str, ...] = ()
    accessibility_notes: tuple[str, ...] = ()
    raw_text: str = ""
    parse_succeeded: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": VISION_SCHEMA_VERSION,
            "layout_summary": self.layout_summary,
            "color_observations": list(self.color_observations),
            "detected_components": list(self.detected_components),
            "suggested_primitives": list(self.suggested_primitives),
            "accessibility_notes": list(self.accessibility_notes),
            "raw_text": self.raw_text,
            "parse_succeeded": self.parse_succeeded,
            "extras": dict(self.extras),
        }


@dataclass(frozen=True)
class VisionGenerationResult:
    """End-to-end output of :func:`generate_ui_from_vision`.

    ``tsx_code`` is the final (optionally auto-fixed) source.
    ``lint_report`` is the :class:`LintReport` **on the final code** —
    if auto-fix ran, violations here reflect the fixed source, not the
    pre-fix draft (see :attr:`pre_fix_lint_report`).
    """

    analysis: VisionAnalysis
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
            "schema_version": VISION_SCHEMA_VERSION,
            "linter_schema_version": LINTER_SCHEMA_VERSION,
            "analysis": self.analysis.to_dict(),
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


# ── Image validation ─────────────────────────────────────────────────


def _sniff_mime(data: bytes) -> str | None:
    """Return the mime type inferred from the payload, or None."""
    for magic, mime in _MAGIC_PREFIXES:
        if data.startswith(magic):
            if mime == "image/webp":
                # RIFF-prefixed files need "WEBP" at offset 8 to qualify.
                if len(data) >= 12 and data[8:12] == b"WEBP":
                    return "image/webp"
                continue
            return mime
    return None


def validate_image(
    data: bytes,
    mime_type: str,
    *,
    source: str | None = None,
) -> VisionImage:
    """Validate an image payload and return a :class:`VisionImage`.

    Raises:
        TypeError: if ``data`` is not bytes-like.
        ValueError: if ``mime_type`` is unsupported, payload is empty
            or too large, or the declared mime disagrees with the
            sniffed one.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data must be bytes")
    mime_norm = (mime_type or "").strip().lower()
    if mime_norm == "image/jpg":
        mime_norm = "image/jpeg"
    if mime_norm not in SUPPORTED_MIME_TYPES:
        raise ValueError(
            f"Unsupported mime type {mime_type!r}; "
            f"must be one of {sorted(SUPPORTED_MIME_TYPES)}"
        )
    if not data:
        raise ValueError("image payload is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"image payload {len(data)} bytes exceeds {MAX_IMAGE_BYTES} "
            "(use the Files API for larger uploads)"
        )
    sniffed = _sniff_mime(bytes(data[:16]))
    if sniffed is not None and sniffed != mime_norm:
        raise ValueError(
            f"declared mime {mime_norm!r} disagrees with payload "
            f"(sniffed as {sniffed!r}); refusing to mislabel"
        )
    return VisionImage(data=bytes(data), mime_type=mime_norm, source=source)


# ── Multimodal message assembly ──────────────────────────────────────


def build_multimodal_message(
    image: VisionImage,
    prompt: str,
) -> "HumanMessage":  # type: ignore[name-defined]  # noqa: F821
    """Return a ``HumanMessage`` whose content is ``[text, image]``.

    The image block uses Anthropic's documented base64 source shape.
    ``langchain_anthropic`` forwards unknown ``type`` values to the
    SDK — *don't* pretty-print the content before handing it to an
    LLM; the dict shape is what the SDK looks for.
    """
    from backend.llm_adapter import HumanMessage

    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.mime_type,
                "data": image.to_base64(),
            },
        },
    ]
    return HumanMessage(content=content)


# ── Prompt construction (deterministic) ──────────────────────────────


_ANALYSIS_INSTRUCTIONS = (
    "You are the OmniSight UI Designer's vision front-end. A user\n"
    "just handed you a screenshot or hand-drawn sketch of a web UI.\n"
    "Describe what you see so the downstream generator can rebuild\n"
    "it with shadcn/ui + Tailwind primitives.\n"
    "\n"
    "Respond with a single JSON object (no prose outside it) with\n"
    "these exact keys:\n"
    '  "layout_summary": one-paragraph description of the overall\n'
    "      structure (header / sidebar / grid / columns / footer)\n"
    '  "color_observations": array of strings — dominant colours and\n'
    "      where they appear (e.g. \"dark slate background, cyan\n"
    "      accent buttons\"). Do NOT invent hex codes.\n"
    '  "detected_components": array of strings — concrete UI widgets\n'
    "      you recognise (e.g. \"primary CTA button\", \"data table\n"
    "      with sortable columns\", \"radio group\", \"tab bar\").\n"
    '  "suggested_primitives": array of shadcn/ui component names\n'
    "      (lowercase, registry names) the generator should consider,\n"
    "      e.g. [\"button\", \"tabs\", \"card\", \"input\"].\n"
    '  "accessibility_notes": array of short notes about a11y\n'
    "      concerns you see (e.g. \"carousel — ensure pause\n"
    "      control\", \"icon-only button — needs aria-label\").\n"
    "\n"
    "Do not hallucinate copy text that isn't visible; describe\n"
    "placeholder regions as \"[headline]\" / \"[body paragraph]\".\n"
    "Do not return hex colours; the downstream generator maps your\n"
    "observations onto the project's design tokens."
)


def build_vision_analysis_prompt(hint: str | None = None) -> str:
    """Return the deterministic analysis prompt.

    ``hint`` is folded in verbatim — callers controlling cache-key
    stability should canonicalise it first.
    """
    extra = ""
    if hint and hint.strip():
        extra = "\n\nAdditional context from the caller:\n" + hint.strip()
    return _ANALYSIS_INSTRUCTIONS + extra


def build_ui_generation_prompt(
    *,
    analysis: VisionAnalysis,
    project_root: Path | str | None,
    brief: str | None,
    tokens: DesignTokens | None = None,
) -> str:
    """Return a deterministic TSX-generation prompt.

    The prompt interpolates the sibling registry + tokens blocks
    verbatim — both are themselves deterministic, so the whole prompt
    is byte-stable for a given (analysis, brief, project state).
    """
    registry_block = render_registry_block(project_root=project_root)
    if tokens is not None:
        tokens_block = tokens.to_agent_context()
    else:
        tokens_block = render_design_tokens_block(project_root=project_root)

    brief_block = ""
    if brief and brief.strip():
        brief_block = (
            "\n## Caller brief\n"
            f"{brief.strip()}\n"
        )

    analysis_block = _render_analysis_block(analysis)

    rules = (
        "## Generation rules (MUST follow)\n"
        "1. Emit a single self-contained React TSX component, no\n"
        "   external imports besides the shadcn primitives listed\n"
        "   above and `cn` from `@/lib/utils`.\n"
        "2. Never emit raw <button>/<input>/<textarea>/<select>/\n"
        "   <dialog>/<progress> — use the shadcn primitive. Wrap\n"
        "   handlers with the component's own props.\n"
        "3. Use design-token utilities only (bg-background,\n"
        "   text-foreground, bg-primary, …). Never inline a hex\n"
        "   colour; never pin a Tailwind palette class such as\n"
        "   bg-slate-900.\n"
        "4. Mobile-first responsive: base + sm/md/lg/xl/2xl.\n"
        "5. Respect WAI-ARIA: icon-only buttons get aria-label;\n"
        "   form inputs get <Label htmlFor> or wrap in <Field>.\n"
        "6. This project is dark-only (html { color-scheme: dark }).\n"
        "   Do NOT emit `dark:` prefixes and do NOT write light\n"
        "   fallbacks.\n"
        "7. Output MUST be a single fenced code block:\n"
        "   ```tsx\n"
        "   /* code */\n"
        "   ```\n"
        "   No prose before or after."
    )

    return "\n\n".join([
        "# UI generation — vision → shadcn/ui + Tailwind\n"
        "You are the OmniSight UI Designer. Produce a React + TSX\n"
        "component that rebuilds the screenshot/sketch below using\n"
        "the project's installed shadcn/ui primitives and design\n"
        "tokens. You will be linted by\n"
        "backend.component_consistency_linter — a clean lint pass is\n"
        "the acceptance gate.",
        analysis_block,
        registry_block,
        tokens_block,
        brief_block.strip() or "## Caller brief\n(none)",
        rules,
    ]).strip() + "\n"


def _render_analysis_block(analysis: VisionAnalysis) -> str:
    """Compact, deterministic rendering of a :class:`VisionAnalysis`."""
    lines: list[str] = ["## Vision analysis"]
    lines.append(
        f"Layout: {analysis.layout_summary.strip() or '(not extracted)'}"
    )

    def _bullet(header: str, items: Sequence[str]) -> None:
        if not items:
            lines.append(f"{header}: (none noted)")
            return
        lines.append(f"{header}:")
        for item in items:
            item = item.strip()
            if item:
                lines.append(f"  - {item}")

    _bullet("Colours observed", tuple(analysis.color_observations))
    _bullet("Components detected", tuple(analysis.detected_components))
    _bullet("Suggested shadcn primitives",
            tuple(analysis.suggested_primitives))
    _bullet("A11y notes", tuple(analysis.accessibility_notes))
    return "\n".join(lines)


# ── Response parsing ─────────────────────────────────────────────────


_FENCE_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _first_fenced_block(
    text: str,
    *,
    langs: Sequence[str] | None = None,
) -> str | None:
    """Return the body of the first matching fenced block, or None."""
    for match in _FENCE_RE.finditer(text):
        lang = (match.group("lang") or "").lower().strip()
        if langs is None or any(lang.startswith(wanted) for wanted in langs):
            return match.group("body").rstrip()
    return None


def _coerce_str_list(value: Any) -> tuple[str, ...]:
    """Coerce a JSON value into a tuple of trimmed non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        # Split on newlines / bullets if we got a free-form blob.
        parts = [p.strip(" \t-*•") for p in value.splitlines()]
        return tuple(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return tuple(out)
    return (str(value).strip(),) if str(value).strip() else ()


_ANALYSIS_KEY_ALIASES = {
    "layout_summary": ("layout_summary", "layout", "summary"),
    "color_observations": (
        "color_observations", "colors", "colour_observations", "colours",
    ),
    "detected_components": (
        "detected_components", "components", "widgets", "elements",
    ),
    "suggested_primitives": (
        "suggested_primitives", "primitives", "shadcn", "shadcn_components",
    ),
    "accessibility_notes": (
        "accessibility_notes", "a11y", "a11y_notes", "accessibility",
    ),
}


def _pick(obj: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def parse_vision_analysis(
    response_text: str,
    *,
    raw_extras_keys: Sequence[str] | None = None,
) -> VisionAnalysis:
    """Parse a model response into a :class:`VisionAnalysis`.

    Accepts three shapes in order of preference:

    1. a fenced ```json …``` block;
    2. a bare JSON object (first ``{``-balanced span);
    3. prose — salvage key lines via regex.

    On total failure returns an empty analysis with the raw text
    preserved in ``raw_text`` and ``parse_succeeded=False``.
    """
    raw_text = response_text or ""
    if not raw_text.strip():
        return VisionAnalysis(raw_text="")

    obj: Mapping[str, Any] | None = None

    fenced = _first_fenced_block(raw_text, langs=("json",))
    if fenced:
        obj = _try_json(fenced)

    if obj is None:
        obj = _try_json(raw_text)

    if obj is None:
        span = _balanced_json_span(raw_text)
        if span is not None:
            obj = _try_json(span)

    if not isinstance(obj, Mapping):
        return _salvage_prose(raw_text)

    layout = _pick(obj, _ANALYSIS_KEY_ALIASES["layout_summary"]) or ""
    extras: dict[str, Any] = {}
    if raw_extras_keys:
        for key in raw_extras_keys:
            if key in obj:
                extras[key] = obj[key]

    return VisionAnalysis(
        layout_summary=str(layout).strip(),
        color_observations=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["color_observations"])
        ),
        detected_components=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["detected_components"])
        ),
        suggested_primitives=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["suggested_primitives"])
        ),
        accessibility_notes=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["accessibility_notes"])
        ),
        raw_text=raw_text,
        parse_succeeded=True,
        extras=extras,
    )


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _balanced_json_span(text: str) -> str | None:
    """Return the first balanced ``{…}`` span in ``text`` (naive)."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start: idx + 1]
    return None


_PROSE_LINE_RES = {
    "layout_summary": re.compile(
        r"(?im)^(?:layout(?:\s+summary)?|overall)\s*[:\-]\s*(.+)$"
    ),
    "color_observations": re.compile(
        r"(?im)^(?:colou?rs?(?:\s+observed)?)\s*[:\-]\s*(.+)$"
    ),
    "detected_components": re.compile(
        r"(?im)^(?:components?|widgets?|elements?)\s*[:\-]\s*(.+)$"
    ),
    "suggested_primitives": re.compile(
        r"(?im)^(?:(?:suggested\s+)?(?:shadcn|primitives?))\s*[:\-]\s*(.+)$"
    ),
    "accessibility_notes": re.compile(
        r"(?im)^(?:a11y|accessibility(?:\s+notes)?)\s*[:\-]\s*(.+)$"
    ),
}


def _salvage_prose(text: str) -> VisionAnalysis:
    """Best-effort recovery when the model didn't return JSON."""
    picked: dict[str, Any] = {}
    for key, regex in _PROSE_LINE_RES.items():
        m = regex.search(text)
        if m:
            picked[key] = m.group(1).strip()

    # Free-form bullet lists next to the headers are a common shape —
    # we don't try to parse them, we just put the line summary.
    if not picked:
        return VisionAnalysis(raw_text=text)

    return VisionAnalysis(
        layout_summary=str(picked.get("layout_summary", "")).strip(),
        color_observations=_coerce_str_list(picked.get("color_observations")),
        detected_components=_coerce_str_list(picked.get("detected_components")),
        suggested_primitives=_coerce_str_list(picked.get("suggested_primitives")),
        accessibility_notes=_coerce_str_list(picked.get("accessibility_notes")),
        raw_text=text,
        parse_succeeded=False,  # salvaged, not trusted
    )


# ── TSX extraction ───────────────────────────────────────────────────


def extract_tsx_from_response(response_text: str) -> str:
    """Return the TSX body from a model response.

    Preference order:

    1. fenced ```tsx … ``` block (also ``jsx`` / ``tsx react`` / ``ts``);
    2. the largest balanced-JSX span starting with ``<`` and ending
       with ``>`` (best-effort fallback);
    3. empty string.
    """
    if not response_text:
        return ""
    fenced = _first_fenced_block(
        response_text,
        langs=("tsx", "jsx", "ts", "typescript", "javascript"),
    )
    if fenced is not None:
        return fenced.strip() + "\n" if fenced.strip() else ""

    # Fallback: any fenced block (lang-less) that contains a JSX tag.
    any_fenced = _first_fenced_block(response_text)
    if any_fenced and "<" in any_fenced and ">" in any_fenced:
        return any_fenced.strip() + "\n"

    # Last resort — slice from the first `<` to the last `>`.
    first_lt = response_text.find("<")
    last_gt = response_text.rfind(">")
    if first_lt >= 0 and last_gt > first_lt:
        span = response_text[first_lt: last_gt + 1].strip()
        if span:
            return span + "\n"
    return ""


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

    def _invoke(messages: list) -> str:
        try:
            return llm_adapter.invoke_chat(
                messages,
                provider=provider,
                model=model,
                llm=llm,
            )
        except Exception as exc:  # defensive — surface as warning, not crash
            logger.warning("vision_to_ui chat invocation failed: %s", exc)
            return ""

    return _invoke


def analyze_screenshot(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_PROVIDER,
    model: str | None = DEFAULT_VISION_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
) -> VisionAnalysis:
    """Run the vision-only half of the pipeline.

    Returns a :class:`VisionAnalysis` even on failure — callers
    inspect ``parse_succeeded`` and ``raw_text`` to decide whether to
    retry with a bigger model.  If the LLM is not configured,
    ``raw_text`` will be empty and ``parse_succeeded=False``.
    """
    image = (
        image_data
        if isinstance(image_data, VisionImage)
        else validate_image(image_data, mime_type or "")
    )
    prompt = build_vision_analysis_prompt(hint)
    message = build_multimodal_message(image, prompt)
    invoke = invoker or _default_invoker(
        provider=provider, model=model, llm=llm,
    )
    response_text = invoke([message])
    return parse_vision_analysis(response_text)


def generate_ui_from_vision(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_PROVIDER,
    model: str | None = DEFAULT_VISION_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
    analysis: VisionAnalysis | None = None,
) -> VisionGenerationResult:
    """End-to-end: image → analysis → TSX → lint (optionally auto-fix).

    Graceful fallback contract:
      * if ``invoker`` (resolved or supplied) returns ``""``, the
        result has ``warnings=("llm_unavailable",)`` and empty
        ``tsx_code`` — callers can surface a "configure provider"
        banner rather than crashing.
      * if generation succeeds but the TSX block can't be extracted,
        ``warnings`` includes ``"tsx_missing"`` and ``tsx_code`` is
        the raw response so a human can inspect.
    """
    image = (
        image_data
        if isinstance(image_data, VisionImage)
        else validate_image(image_data, mime_type or "")
    )
    invoke = invoker or _default_invoker(
        provider=provider, model=model, llm=llm,
    )

    warnings: list[str] = []

    # Step 1 — analyse.
    if analysis is None:
        analysis_prompt = build_vision_analysis_prompt(hint)
        analysis_msg = build_multimodal_message(image, analysis_prompt)
        analysis_text = invoke([analysis_msg])
        if not analysis_text:
            warnings.append("llm_unavailable")
            return VisionGenerationResult(
                analysis=VisionAnalysis(raw_text=""),
                tsx_code="",
                lint_report=LintReport(),
                pre_fix_lint_report=None,
                auto_fix_applied=False,
                warnings=tuple(warnings),
                model=model,
                provider=provider,
            )
        analysis = parse_vision_analysis(analysis_text)
        if not analysis.parse_succeeded:
            warnings.append("analysis_parse_failed")

    # Step 2 — generate TSX.
    tokens = load_design_tokens(project_root) if project_root else None
    gen_prompt = build_ui_generation_prompt(
        analysis=analysis,
        project_root=project_root,
        brief=brief,
        tokens=tokens,
    )
    gen_msg = build_multimodal_message(image, gen_prompt)
    gen_text = invoke([gen_msg])
    if not gen_text:
        warnings.append("llm_unavailable")
        return VisionGenerationResult(
            analysis=analysis,
            tsx_code="",
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(warnings),
            model=model,
            provider=provider,
        )

    tsx = extract_tsx_from_response(gen_text)
    if not tsx:
        warnings.append("tsx_missing")
        return VisionGenerationResult(
            analysis=analysis,
            tsx_code=gen_text,
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=tuple(warnings),
            model=model,
            provider=provider,
        )

    # Step 3 — lint (and optionally one round of mechanical auto-fix).
    initial_report = lint_code(tsx, source="vision_to_ui.tsx")
    fixed_tsx = tsx
    applied = False
    final_report = initial_report

    if auto_fix and not initial_report.is_clean:
        fixed_tsx, remaining = auto_fix_code(tsx)
        if fixed_tsx != tsx:
            applied = True
            final_report = lint_code(fixed_tsx, source="vision_to_ui.tsx")

    return VisionGenerationResult(
        analysis=analysis,
        tsx_code=fixed_tsx,
        lint_report=final_report,
        pre_fix_lint_report=initial_report if applied else None,
        auto_fix_applied=applied,
        warnings=tuple(warnings),
        model=model,
        provider=provider,
    )


def run_vision_to_ui(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_PROVIDER,
    model: str | None = DEFAULT_VISION_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    auto_fix: bool = True,
) -> dict:
    """Agent-callable entry point — returns a JSON-safe dict.

    Exposed on the UI Designer skill's tool surface.  The return
    value is the :meth:`VisionGenerationResult.to_dict` payload,
    including ``analysis``, ``tsx_code``, ``lint_report`` and
    ``warnings`` so the agent can decide whether to self-repair or
    escalate.
    """
    result = generate_ui_from_vision(
        image_data,
        mime_type=mime_type,
        brief=brief,
        project_root=project_root,
        hint=hint,
        provider=provider,
        model=model,
        llm=llm,
        invoker=invoker,
        auto_fix=auto_fix,
    )
    return result.to_dict()

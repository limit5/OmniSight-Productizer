"""V5 #4 (issue #321) — Screenshot / hand-drawn sketch → mobile code pipeline.

Bridges a raster image (an app screenshot, a Figma export, or a phone-snap of a
whiteboard sketch) into three-platform mobile code emission — **SwiftUI views**
(iOS 16+), **Jetpack Compose Material 3** components, and **Flutter 3.22+**
widgets — for the V5 Mobile UI Designer agent
(``configs/roles/mobile-ui-designer.md``).

Why this module exists
----------------------

The Mobile UI Designer skill names this module explicitly in its sibling
table: *"screenshot → I take the multimodal result and rebuild it as a
SwiftUI / Compose / Flutter component tree, not a translated React+Tailwind
soup."*  The sibling modules already pin the fact-side:

* :mod:`backend.mobile_component_registry` tells the agent **which** SwiftUI /
  Compose / Flutter primitives are canonical (``NavigationStack`` not
  ``NavigationView``, ``NavigationBar`` (M3) not ``BottomNavigationBar`` (M2),
  …) — its ``render_agent_context_block`` is interpolated into every
  generation prompt;
* :mod:`backend.design_token_loader` surfaces live design tokens from the
  project's ``globals.css`` so the mobile generators can map screenshot
  observations onto **token names**, never inline hex / pt / dp / sp;
* :mod:`backend.vision_to_ui` already validates the image, owns the
  multimodal-message shape, and pins the ``MAX_IMAGE_BYTES`` /
  ``SUPPORTED_MIME_TYPES`` contract — this module re-uses those primitives so
  the agent learns one image-validation contract once;
* :mod:`backend.figma_to_mobile` already pinned the **three-platform code
  extraction** contract (fenced ``swift`` / ``kotlin`` / ``dart`` blocks with
  the common aliases — ``swiftui`` / ``kt`` / ``compose`` / ``flutter``) — we
  re-export :func:`extract_mobile_code_from_response` and the
  :class:`MobileCodeOutputs` dataclass so a downstream consumer can swap a
  Figma input for a screenshot input without any other change.

This module provides the screenshot-specific glue:

* :func:`build_vision_mobile_analysis_prompt` is a pure deterministic prompt
  asking the multimodal model to describe the image in **mobile** terms
  (suggested SwiftUI / Compose / Flutter primitives, dynamic-type / touch-target
  notes — *not* shadcn registry names);
* :func:`parse_mobile_vision_analysis` tolerates fenced JSON / bare JSON / prose
  fallback — same shape as :func:`backend.vision_to_ui.parse_vision_analysis`
  but with three platform-specific suggestion fields;
* :func:`build_mobile_generation_prompt_from_vision` renders a byte-stable
  prompt injecting (a) the analysis, (b) the mobile component registry block,
  (c) the design tokens block, and (d) the caller brief;
* :func:`generate_mobile_from_vision` is the end-to-end entry: image → analysis
  → prompt → LLM → three-platform code extraction →
  :class:`MobileVisionGenerationResult`.  All failure modes surface as
  ``warnings`` rather than tracebacks.

Contract (pinned by ``backend/tests/test_vision_to_mobile.py``)
---------------------------------------------------------------

* :data:`VISION_MOBILE_SCHEMA_VERSION` bumps when the ``to_dict()`` shape of
  any exported dataclass changes.
* :data:`TARGET_PLATFORMS` == ``("swiftui","compose","flutter")`` — kept in
  sync with :mod:`backend.mobile_component_registry` (a contract test
  enforces).
* :data:`PLATFORM_LANGS` == ``{"swiftui":"swift","compose":"kotlin","flutter":"dart"}``
  re-exported from :mod:`backend.figma_to_mobile`.
* :class:`MobileVisionAnalysis` and :class:`MobileVisionGenerationResult` are
  frozen, validated, and JSON-serialisable via ``to_dict``.
* :func:`build_vision_mobile_analysis_prompt` and
  :func:`build_mobile_generation_prompt_from_vision` are pure: same inputs →
  byte-identical strings.
* :func:`parse_mobile_vision_analysis` accepts fenced ``json`` / bare JSON /
  prose; on total failure returns an empty analysis with the raw text
  preserved and ``parse_succeeded=False`` rather than raising.
* :func:`extract_mobile_code_from_response` (re-exported) recognises
  ``swift`` / ``kotlin`` / ``dart`` fences plus the common aliases.
* Every pipeline entry point — :func:`analyze_mobile_screenshot`,
  :func:`generate_mobile_from_vision`, :func:`run_vision_to_mobile` — is
  graceful: if the LLM returns ``""`` the result carries
  ``warnings=("llm_unavailable",)`` and empty outputs, never a traceback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from backend.design_token_loader import (
    DesignTokens,
    load_design_tokens,
    render_agent_context_block as render_design_tokens_block,
)
from backend.figma_to_mobile import (
    PLATFORM_LANGS,
    MobileCodeOutputs,
    extract_mobile_code_from_response,
)
from backend.mobile_component_registry import (
    PLATFORMS as MOBILE_PLATFORMS,
    render_agent_context_block as render_mobile_registry_block,
)
from backend.vision_to_ui import (
    MAX_IMAGE_BYTES,
    SUPPORTED_MIME_TYPES,
    VisionImage,
    build_multimodal_message,
    validate_image,
)

logger = logging.getLogger(__name__)


__all__ = [
    "VISION_MOBILE_SCHEMA_VERSION",
    "DEFAULT_VISION_MOBILE_MODEL",
    "DEFAULT_VISION_MOBILE_PROVIDER",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_MIME_TYPES",
    "TARGET_PLATFORMS",
    "PLATFORM_LANGS",
    "VisionImage",
    "MobileCodeOutputs",
    "MobileVisionAnalysis",
    "MobileVisionGenerationResult",
    "validate_image",
    "build_multimodal_message",
    "build_vision_mobile_analysis_prompt",
    "build_mobile_generation_prompt_from_vision",
    "parse_mobile_vision_analysis",
    "extract_mobile_code_from_response",
    "analyze_mobile_screenshot",
    "generate_mobile_from_vision",
    "run_vision_to_mobile",
]


# Bump when the shape of any ``to_dict()`` payload changes — callers cache
# prompts / responses keyed off this version.
VISION_MOBILE_SCHEMA_VERSION = "1.0.0"

#: Default multimodal model.  Opus 4.7 because adapting one screenshot into
#: three platform-native component trees is a reasoning task on top of vision
#: (map a tabbar in a screenshot → SwiftUI ``TabView``, Compose
#: ``NavigationBar``, Flutter ``NavigationBar`` (M3) — not ``BottomNavigationBar``
#: (M2) which the model would eagerly recall otherwise).
DEFAULT_VISION_MOBILE_MODEL = "claude-opus-4-7"
DEFAULT_VISION_MOBILE_PROVIDER = "anthropic"

#: Three target platforms — aligned with
#: :data:`backend.mobile_component_registry.PLATFORMS`.
TARGET_PLATFORMS: tuple[str, ...] = MOBILE_PLATFORMS


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MobileVisionAnalysis:
    """Structured model observation of one screenshot / sketch in mobile terms.

    The fields below are the **minimum** we extract; the model may emit extra
    keys (kept in :attr:`extras` when the caller asks for them) but consumers
    should rely only on the named fields for behavioural logic.

    Note the *three* platform-specific suggestion lists — the analysis prompt
    asks the model to recommend canonical primitives from
    :mod:`backend.mobile_component_registry` per platform so the downstream
    generation prompt has structured hints to anchor its output.
    """

    layout_summary: str = ""
    color_observations: tuple[str, ...] = ()
    detected_components: tuple[str, ...] = ()
    suggested_swiftui: tuple[str, ...] = ()
    suggested_compose: tuple[str, ...] = ()
    suggested_flutter: tuple[str, ...] = ()
    accessibility_notes: tuple[str, ...] = ()
    raw_text: str = ""
    parse_succeeded: bool = False
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze ``extras`` so callers can't mutate analysis state in-place.
        if not isinstance(self.extras, MappingProxyType):
            object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))

    @property
    def has_any_suggestions(self) -> bool:
        return bool(
            self.suggested_swiftui
            or self.suggested_compose
            or self.suggested_flutter
        )

    def suggestions_for(self, platform: str) -> tuple[str, ...]:
        """Return the suggestion list for ``platform`` (raises on unknown)."""
        if platform == "swiftui":
            return self.suggested_swiftui
        if platform == "compose":
            return self.suggested_compose
        if platform == "flutter":
            return self.suggested_flutter
        raise ValueError(
            f"Unknown platform {platform!r}; must be one of {TARGET_PLATFORMS}"
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": VISION_MOBILE_SCHEMA_VERSION,
            "layout_summary": self.layout_summary,
            "color_observations": list(self.color_observations),
            "detected_components": list(self.detected_components),
            "suggested_swiftui": list(self.suggested_swiftui),
            "suggested_compose": list(self.suggested_compose),
            "suggested_flutter": list(self.suggested_flutter),
            "accessibility_notes": list(self.accessibility_notes),
            "raw_text": self.raw_text,
            "parse_succeeded": self.parse_succeeded,
            "extras": dict(self.extras),
        }


@dataclass(frozen=True)
class MobileVisionGenerationResult:
    """End-to-end output of :func:`generate_mobile_from_vision`."""

    analysis: MobileVisionAnalysis
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
            "schema_version": VISION_MOBILE_SCHEMA_VERSION,
            "analysis": self.analysis.to_dict(),
            "outputs": self.outputs.to_dict(),
            "raw_response": self.raw_response,
            "warnings": list(self.warnings),
            "model": self.model,
            "provider": self.provider,
            "is_complete": self.is_complete,
        }


# ── Prompt construction (deterministic) ──────────────────────────────


_ANALYSIS_INSTRUCTIONS = (
    "You are the OmniSight Mobile UI Designer's vision front-end. A user just\n"
    "handed you a screenshot or hand-drawn sketch of a mobile UI surface\n"
    "(iOS / Android / cross-platform — figure it out from the chrome).\n"
    "Describe what you see so the downstream generator can rebuild it as\n"
    "three platform-native component trees:\n"
    "  * SwiftUI view (iOS 16+)\n"
    "  * Jetpack Compose composable (Material 3)\n"
    "  * Flutter widget (3.22+)\n"
    "\n"
    "Respond with a single JSON object (no prose outside it) with these\n"
    "exact keys:\n"
    '  "layout_summary": one-paragraph description of the overall structure\n'
    "      (status bar / nav bar / tab bar / hero / list / FAB / safe-area /\n"
    "      bottom-sheet, etc.). Note whether it is phone (compact) or tablet\n"
    "      (medium / expanded) sized.\n"
    '  "color_observations": array of strings — dominant colours and where\n'
    "      they appear (e.g. \"dark surface background, blue accent FAB\").\n"
    "      Do NOT invent hex codes; the generator maps your observations onto\n"
    "      the project's design tokens.\n"
    '  "detected_components": array of strings — concrete UI widgets you\n'
    "      recognise (e.g. \"primary CTA button\", \"segmented tab bar\",\n"
    "      \"swipeable card list\", \"floating action button\").\n"
    '  "suggested_swiftui": array of canonical SwiftUI view names from the\n'
    "      mobile component registry (e.g. [\"NavigationStack\",\n"
    "      \"List\", \"Button\"]). Prefer iOS-16+ APIs — never\n"
    "      \"NavigationView\" or \"ObservableObject\".\n"
    '  "suggested_compose": array of canonical Jetpack Compose Material 3\n'
    "      component names (e.g. [\"Scaffold\", \"NavigationBar\",\n"
    "      \"FilledButton\"]). Never \"BottomNavigation\" (M2).\n"
    '  "suggested_flutter": array of canonical Flutter 3.22+ widget names\n'
    "      (e.g. [\"Scaffold\", \"NavigationBar\", \"FilledButton\"]).\n"
    "      Prefer Material 3 widgets; \"BottomNavigationBar\" is legacy.\n"
    '  "accessibility_notes": array of short notes about a11y concerns (e.g.\n'
    "      \"icon-only FAB — needs accessibilityLabel / contentDescription\",\n"
    "      \"text colour low contrast on light surface\", \"touch target\n"
    "      ≥ 44 pt / 48 dp on small icons\").\n"
    "\n"
    "Do not hallucinate copy text that isn't visible; describe placeholder\n"
    "regions as \"[headline]\" / \"[body paragraph]\".  Do not return hex\n"
    "colours; the downstream generator maps your observations onto the\n"
    "project's design tokens."
)


def build_vision_mobile_analysis_prompt(hint: str | None = None) -> str:
    """Return the deterministic mobile-analysis prompt.

    ``hint`` is folded in verbatim — callers controlling cache-key stability
    should canonicalise it first.  Empty / whitespace-only hints are
    equivalent to ``None``.
    """
    extra = ""
    if hint and hint.strip():
        extra = "\n\nAdditional context from the caller:\n" + hint.strip()
    return _ANALYSIS_INSTRUCTIONS + extra


_GENERATION_HEADER = (
    "# Mobile generation — Screenshot → SwiftUI + Compose + Flutter\n"
    "You are the OmniSight Mobile UI Designer.  The image attached to this\n"
    "message is a screenshot / hand-drawn sketch of a mobile UI surface.\n"
    "The structured analysis below is what your vision front-end already\n"
    "extracted.  Your job is to rebuild this surface as three platform-native\n"
    "mobile component trees:\n"
    "  * SwiftUI view (iOS 16+)\n"
    "  * Jetpack Compose composable (Material 3, compileSdk 35 / minSdk 24)\n"
    "  * Flutter widget (3.22+)\n"
    "Pick components from the 'Mobile component registry' block below only;\n"
    "never resurrect deprecated APIs (NavigationView, BottomNavigationBar M2,\n"
    "ObservableObject, …) from training memory."
)


_GENERATION_RULES = (
    "## Generation rules (MUST follow)\n"
    "1. Emit THREE fenced code blocks — one per platform — in this exact\n"
    "   order, with these exact fence languages:\n"
    "   ```swift   (SwiftUI)\n"
    "   ```kotlin  (Jetpack Compose)\n"
    "   ```dart    (Flutter)\n"
    "   No prose between blocks beyond a single `// Platform:` comment\n"
    "   header inside each block.  Do not emit any other fences.\n"
    "2. Three platforms must be semantically equivalent — same layout intent,\n"
    "   same component hierarchy, same interaction contract. Differences are\n"
    "   only where the platform idiom requires (NavigationStack vs.\n"
    "   NavigationBar vs. go_router).\n"
    "3. NEVER hard-code hex / pt / dp / sp.  Map screenshot observations onto:\n"
    "   * SwiftUI  — SF Symbols + .font(.headline/.body) + system semantic\n"
    "                colours (Color.primary, etc.) + design token constants\n"
    "                referenced by name only.\n"
    "   * Compose  — MaterialTheme.colorScheme.* +\n"
    "                MaterialTheme.typography.* + 4dp spacing grid.\n"
    "   * Flutter  — Theme.of(context).colorScheme.* +\n"
    "                Theme.of(context).textTheme.* + design token spacing\n"
    "                constants.\n"
    "   If no matching token exists, reference a TODO token name and leave a\n"
    "   `// TODO(token)` comment — do NOT invent a hex.\n"
    "4. Build adaptive layout — no absolute pixel positioning:\n"
    "   * SwiftUI  — VStack/HStack/ZStack + size-class via\n"
    "                @Environment(\\.horizontalSizeClass).\n"
    "   * Compose  — Column/Row/Box + WindowSizeClass.\n"
    "   * Flutter  — Column/Row/Stack + MediaQuery.sizeOf(context).\n"
    "   Every root surface must handle safe-area (safeAreaInset /\n"
    "   WindowInsets / MediaQuery.viewPaddingOf).\n"
    "5. Touch targets ≥ 44×44 pt (iOS) / 48×48 dp (Android / Flutter).\n"
    "6. A11y baseline:\n"
    "   * SwiftUI  — .accessibilityLabel on icon-only Buttons,\n"
    "                .accessibilityHint where useful, Dynamic Type via\n"
    "                semantic .font(...).\n"
    "   * Compose  — contentDescription on IconButton / Icon, semantics {}\n"
    "                for custom widgets.\n"
    "   * Flutter  — Semantics(label: …) on icon-only widgets, Tooltip +\n"
    "                semanticLabel on bare Icons.\n"
    "7. Dark-mode parity — rely on platform semantic colours (never\n"
    "   `Color(red:green:blue:)` / `Color(0xFF…)` / `Color(int)`).\n"
    "8. Output shape — three fenced blocks only, no prose after the last\n"
    "   block."
)


def build_mobile_generation_prompt_from_vision(
    *,
    analysis: MobileVisionAnalysis,
    project_root: Path | str | None,
    brief: str | None = None,
    tokens: DesignTokens | None = None,
    platforms: Sequence[str] | None = None,
) -> str:
    """Return a deterministic three-platform generation prompt.

    The prompt interpolates the sibling registry + tokens blocks verbatim —
    both are themselves deterministic, so the whole prompt is byte-stable for
    a given (analysis, brief, project state, platforms).
    """
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

    analysis_block = _render_analysis_block(analysis, plats)
    platforms_block = _render_platforms_block(plats)

    brief_block = (
        f"## Caller brief\n{brief.strip()}"
        if brief and brief.strip()
        else "## Caller brief\n(none)"
    )

    sections = [
        _GENERATION_HEADER,
        platforms_block,
        analysis_block,
        registry_block,
        tokens_block,
        brief_block,
        _GENERATION_RULES,
    ]
    return "\n\n".join(section.strip() for section in sections).strip() + "\n"


def _render_platforms_block(platforms: Sequence[str]) -> str:
    lines = ["## Target platforms"]
    for plat in platforms:
        lang = PLATFORM_LANGS.get(plat, "")
        lines.append(f"- {plat} (fenced as ```{lang})")
    return "\n".join(lines)


def _render_analysis_block(
    analysis: MobileVisionAnalysis,
    platforms: Sequence[str],
) -> str:
    """Compact, deterministic rendering of a :class:`MobileVisionAnalysis`."""
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
            text = item.strip()
            if text:
                lines.append(f"  - {text}")

    _bullet("Colours observed", tuple(analysis.color_observations))
    _bullet("Components detected", tuple(analysis.detected_components))

    plat_label = {
        "swiftui": "Suggested SwiftUI views",
        "compose": "Suggested Compose components",
        "flutter": "Suggested Flutter widgets",
    }
    for plat in platforms:
        suggestions = analysis.suggestions_for(plat)
        _bullet(plat_label[plat], tuple(suggestions))

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
        parts = [p.strip(" \t-*\u2022") for p in value.splitlines()]
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
    s = str(value).strip()
    return (s,) if s else ()


_ANALYSIS_KEY_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "layout_summary": ("layout_summary", "layout", "summary"),
    "color_observations": (
        "color_observations", "colors", "colour_observations", "colours",
    ),
    "detected_components": (
        "detected_components", "components", "widgets", "elements",
    ),
    "suggested_swiftui": (
        "suggested_swiftui", "swiftui", "ios", "ios_components",
        "swift_components",
    ),
    "suggested_compose": (
        "suggested_compose", "compose", "android",
        "android_components", "kotlin_components",
    ),
    "suggested_flutter": (
        "suggested_flutter", "flutter", "dart_components",
        "flutter_components",
    ),
    "accessibility_notes": (
        "accessibility_notes", "a11y", "a11y_notes", "accessibility",
    ),
})


def _pick(obj: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
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


_PROSE_LINE_RES: Mapping[str, re.Pattern[str]] = MappingProxyType({
    "layout_summary": re.compile(
        r"(?im)^(?:layout(?:\s+summary)?|overall)\s*[:\-]\s*(.+)$"
    ),
    "color_observations": re.compile(
        r"(?im)^(?:colou?rs?(?:\s+observed)?)\s*[:\-]\s*(.+)$"
    ),
    "detected_components": re.compile(
        r"(?im)^(?:components?|widgets?|elements?)\s*[:\-]\s*(.+)$"
    ),
    "suggested_swiftui": re.compile(
        r"(?im)^(?:swiftui|ios)\s*[:\-]\s*(.+)$"
    ),
    "suggested_compose": re.compile(
        r"(?im)^(?:compose|android)\s*[:\-]\s*(.+)$"
    ),
    "suggested_flutter": re.compile(
        r"(?im)^(?:flutter|dart)\s*[:\-]\s*(.+)$"
    ),
    "accessibility_notes": re.compile(
        r"(?im)^(?:a11y|accessibility(?:\s+notes)?)\s*[:\-]\s*(.+)$"
    ),
})


def _salvage_prose(text: str) -> MobileVisionAnalysis:
    """Best-effort recovery when the model didn't return JSON."""
    picked: dict[str, Any] = {}
    for key, regex in _PROSE_LINE_RES.items():
        m = regex.search(text)
        if m:
            picked[key] = m.group(1).strip()

    if not picked:
        return MobileVisionAnalysis(raw_text=text)

    return MobileVisionAnalysis(
        layout_summary=str(picked.get("layout_summary", "")).strip(),
        color_observations=_coerce_str_list(picked.get("color_observations")),
        detected_components=_coerce_str_list(picked.get("detected_components")),
        suggested_swiftui=_coerce_str_list(picked.get("suggested_swiftui")),
        suggested_compose=_coerce_str_list(picked.get("suggested_compose")),
        suggested_flutter=_coerce_str_list(picked.get("suggested_flutter")),
        accessibility_notes=_coerce_str_list(picked.get("accessibility_notes")),
        raw_text=text,
        parse_succeeded=False,  # salvaged, not trusted
    )


def parse_mobile_vision_analysis(
    response_text: str,
    *,
    raw_extras_keys: Sequence[str] | None = None,
) -> MobileVisionAnalysis:
    """Parse a model response into a :class:`MobileVisionAnalysis`.

    Accepts three shapes in order of preference:

    1. a fenced ```json …``` block;
    2. a bare JSON object (first ``{``-balanced span);
    3. prose — salvage key lines via regex.

    On total failure returns an empty analysis with the raw text preserved
    in ``raw_text`` and ``parse_succeeded=False``.
    """
    raw_text = response_text or ""
    if not raw_text.strip():
        return MobileVisionAnalysis(raw_text="")

    obj: Mapping[str, Any] | None = None

    fenced = _first_fenced_block(raw_text, langs=("json",))
    if fenced:
        candidate = _try_json(fenced)
        if isinstance(candidate, Mapping):
            obj = candidate

    if obj is None:
        candidate = _try_json(raw_text)
        if isinstance(candidate, Mapping):
            obj = candidate

    if obj is None:
        span = _balanced_json_span(raw_text)
        if span is not None:
            candidate = _try_json(span)
            if isinstance(candidate, Mapping):
                obj = candidate

    if obj is None:
        return _salvage_prose(raw_text)

    layout = _pick(obj, _ANALYSIS_KEY_ALIASES["layout_summary"]) or ""
    extras: dict[str, Any] = {}
    if raw_extras_keys:
        for key in raw_extras_keys:
            if key in obj:
                extras[key] = obj[key]

    return MobileVisionAnalysis(
        layout_summary=str(layout).strip(),
        color_observations=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["color_observations"])
        ),
        detected_components=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["detected_components"])
        ),
        suggested_swiftui=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["suggested_swiftui"])
        ),
        suggested_compose=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["suggested_compose"])
        ),
        suggested_flutter=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["suggested_flutter"])
        ),
        accessibility_notes=_coerce_str_list(
            _pick(obj, _ANALYSIS_KEY_ALIASES["accessibility_notes"])
        ),
        raw_text=raw_text,
        parse_succeeded=True,
        extras=extras,
    )


# ── Pipeline entry points ────────────────────────────────────────────


ChatInvoker = Callable[[list], str]
"""An injectable chat invocation: given a list of LangChain messages, return
the assistant text.  Tests wire in a fake; production wires
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
            logger.warning("vision_to_mobile chat invocation failed: %s", exc)
            return ""

    return _invoke


def _dedupe_preserve(items):
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def analyze_mobile_screenshot(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_MOBILE_PROVIDER,
    model: str | None = DEFAULT_VISION_MOBILE_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
) -> MobileVisionAnalysis:
    """Run the vision-only half of the pipeline.

    Returns a :class:`MobileVisionAnalysis` even on failure — callers inspect
    ``parse_succeeded`` and ``raw_text`` to decide whether to retry with a
    bigger model.  If the LLM is not configured, ``raw_text`` will be empty
    and ``parse_succeeded=False``.
    """
    image = (
        image_data
        if isinstance(image_data, VisionImage)
        else validate_image(image_data, mime_type or "")
    )
    prompt = build_vision_mobile_analysis_prompt(hint)
    message = build_multimodal_message(image, prompt)
    invoke = invoker or _default_invoker(
        provider=provider, model=model, llm=llm,
    )
    response_text = invoke([message])
    return parse_mobile_vision_analysis(response_text)


def generate_mobile_from_vision(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_MOBILE_PROVIDER,
    model: str | None = DEFAULT_VISION_MOBILE_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    analysis: MobileVisionAnalysis | None = None,
    platforms: Sequence[str] | None = None,
) -> MobileVisionGenerationResult:
    """End-to-end: image → analysis → three-platform code.

    Graceful fallback contract:
      * if ``invoker`` returns ``""`` on the analysis call, the result has
        ``warnings=("llm_unavailable",)`` and empty outputs;
      * if analysis succeeds but generation returns ``""``, the result has
        ``warnings=("llm_unavailable",)`` and the analysis is preserved;
      * if generation succeeds but one or more platform fences are missing,
        the result carries ``"<plat>_missing"`` warnings AND the non-empty
        outputs are returned so the caller can ship a partial diff.
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
        analysis_prompt = build_vision_mobile_analysis_prompt(hint)
        analysis_msg = build_multimodal_message(image, analysis_prompt)
        analysis_text = invoke([analysis_msg])
        if not analysis_text:
            warnings.append("llm_unavailable")
            return MobileVisionGenerationResult(
                analysis=MobileVisionAnalysis(raw_text=""),
                outputs=MobileCodeOutputs(),
                raw_response="",
                warnings=tuple(_dedupe_preserve(warnings)),
                model=model,
                provider=provider,
            )
        analysis = parse_mobile_vision_analysis(analysis_text)
        if not analysis.parse_succeeded:
            warnings.append("analysis_parse_failed")

    # Step 2 — generate.
    tokens = load_design_tokens(project_root) if project_root else None
    gen_prompt = build_mobile_generation_prompt_from_vision(
        analysis=analysis,
        project_root=project_root,
        brief=brief,
        tokens=tokens,
        platforms=platforms,
    )
    gen_msg = build_multimodal_message(image, gen_prompt)
    gen_text = invoke([gen_msg])
    if not gen_text:
        warnings.append("llm_unavailable")
        return MobileVisionGenerationResult(
            analysis=analysis,
            outputs=MobileCodeOutputs(),
            raw_response="",
            warnings=tuple(_dedupe_preserve(warnings)),
            model=model,
            provider=provider,
        )

    outputs = extract_mobile_code_from_response(gen_text)
    targeted = tuple(platforms) if platforms else TARGET_PLATFORMS
    for plat in targeted:
        if not outputs.platform_map[plat].strip():
            warnings.append(f"{plat}_missing")

    return MobileVisionGenerationResult(
        analysis=analysis,
        outputs=outputs,
        raw_response=gen_text,
        warnings=tuple(_dedupe_preserve(warnings)),
        model=model,
        provider=provider,
    )


def run_vision_to_mobile(
    image_data: bytes | VisionImage,
    mime_type: str | None = None,
    *,
    brief: str | None = None,
    project_root: Path | str | None = None,
    hint: str | None = None,
    provider: str | None = DEFAULT_VISION_MOBILE_PROVIDER,
    model: str | None = DEFAULT_VISION_MOBILE_MODEL,
    llm: Any | None = None,
    invoker: ChatInvoker | None = None,
    platforms: Sequence[str] | None = None,
) -> dict:
    """Agent-callable entry — returns a JSON-safe dict.

    Exposed on the Mobile UI Designer skill's tool surface.  The return value
    is the :meth:`MobileVisionGenerationResult.to_dict` payload, including
    ``analysis``, ``outputs``, ``warnings`` and ``is_complete`` so the agent
    can decide whether to self-repair or escalate.
    """
    result = generate_mobile_from_vision(
        image_data,
        mime_type=mime_type,
        brief=brief,
        project_root=project_root,
        hint=hint,
        provider=provider,
        model=model,
        llm=llm,
        invoker=invoker,
        platforms=platforms,
    )
    return result.to_dict()

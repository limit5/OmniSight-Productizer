"""V1 #8 (issue #317) — Edit complexity auto-router.

Classifies the *size* of a UI edit request and picks the right model
budget for it, without spending a single LLM token on the decision
itself.

Why this module exists
----------------------

The UI Designer skill (``configs/roles/ui-designer.md``) §"6 步 SOP"
step 2 reads::

    small edit  (text / color / spacing) → Haiku 路徑（< 3s）
    large edit  (layout / new page)      → Opus 路徑（深思）
    ※ 由 Edit complexity auto-router 決定，不在此 skill 內

The *agent* should stay agnostic of which Claude model is behind the
invoker; this module is the **dispatch layer** the runtime consults
*before* handing the prompt to the skill.  Small copy/colour/spacing
tweaks don't need Opus 4.7 — Haiku 4.5 is ~10× cheaper and ~5× faster
and the skill rules + linter gate already ensure quality on both
paths.  Layout rewrites and multi-component new pages benefit from
Opus reasoning budget.

Contract (pinned by ``backend/tests/test_edit_complexity_router.py``)
--------------------------------------------------------------------

* :class:`EditComplexity` is a **frozen** three-bucket enum:
  ``small`` / ``medium`` / ``large``.  Bucket borders are pinned by
  test — changing a bucket boundary ships as a new major
  :data:`EDIT_ROUTER_SCHEMA_VERSION`.
* :func:`classify_prompt` is a **pure function** of ``(prompt,
  context)`` — same inputs produce byte-identical output (no clock,
  no RNG, no hidden state).  This lets callers cache the routing
  decision and makes A/B analysis tractable.
* :func:`route` returns a :class:`EditRouteDecision` whose
  ``complexity``, ``model``, ``provider``, ``reasons``, ``signals``
  round-trip through ``to_dict`` without information loss.
* The decision **never** calls the LLM.  If the caller passes a
  blank prompt the router still yields a well-formed decision with
  ``complexity="medium"`` and ``reasons=("empty_prompt",)`` — it does
  *not* raise.  Medium is the safe default because we don't know
  whether the user meant "do nothing" (→ small, but nothing to do)
  or "start a whole new page" (→ large).
* Overrides: the caller may force a bucket by passing
  ``complexity=`` to :func:`route` (kept in ``reasons`` as
  ``caller_override``) or flip individual context flags
  (``has_image`` / ``has_figma`` / ``has_url`` / ``has_existing_code``)
  without re-parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


__all__ = [
    "EDIT_ROUTER_SCHEMA_VERSION",
    "DEFAULT_PROVIDER",
    "DEFAULT_SMALL_MODEL",
    "DEFAULT_MEDIUM_MODEL",
    "DEFAULT_LARGE_MODEL",
    "COMPLEXITY_TO_MODEL",
    "EXPECTED_LATENCY_MS",
    "EditComplexity",
    "EditSignals",
    "EditRouteDecision",
    "classify_prompt",
    "route",
    "run_edit_router",
    "render_decision_markdown",
]


#: Bump when the shape of :class:`EditRouteDecision` / :class:`EditSignals`
#: changes — callers may cache routing decisions keyed on this.
EDIT_ROUTER_SCHEMA_VERSION = "1.0.0"

#: Anthropic is the only provider this project routes through for
#: UI-generation tasks (the UI Designer skill is Claude-specific).
DEFAULT_PROVIDER = "anthropic"

#: Haiku 4.5 — ~2s p50 on short prompts.  Right for pure string/colour/
#: spacing swaps where the agent needs to emit a diff, not a plan.
DEFAULT_SMALL_MODEL = "claude-haiku-4-5"

#: Sonnet 4.6 — mid-budget for component-local edits (one or two
#: primitives, no layout change).  Kept as its own bucket so the router
#: doesn't stair-step the whole catalogue every time a user asks for a
#: slightly bigger tweak.
DEFAULT_MEDIUM_MODEL = "claude-sonnet-4-6"

#: Opus 4.7 — reserved for layout refactors, new pages, and anything
#: multimodal (screenshot / Figma / URL reference).  Sibling modules
#: :mod:`backend.vision_to_ui`, :mod:`backend.figma_to_ui`, and
#: :mod:`backend.url_to_reference` already pin Opus 4.7 as their
#: default; this router respects that invariant.
DEFAULT_LARGE_MODEL = "claude-opus-4-7"

#: Fixed mapping bucket → (provider, model).  Exposed so callers can
#: read it without importing the enum to rebuild the table.
COMPLEXITY_TO_MODEL: Mapping[str, tuple[str, str]] = MappingProxyType({
    "small": (DEFAULT_PROVIDER, DEFAULT_SMALL_MODEL),
    "medium": (DEFAULT_PROVIDER, DEFAULT_MEDIUM_MODEL),
    "large": (DEFAULT_PROVIDER, DEFAULT_LARGE_MODEL),
})

#: Rough p50 expectations (ms) surfaced so ops / UI can budget timeouts
#: against the routing decision.  Not a hard SLA — the LLM's own latency
#: dominates variance.
EXPECTED_LATENCY_MS: Mapping[str, int] = MappingProxyType({
    "small": 3_000,
    "medium": 8_000,
    "large": 20_000,
})


class EditComplexity(str, Enum):
    """Three-bucket complexity enum.

    Subclasses :class:`str` so ``decision.complexity`` can be JSON-serialised
    verbatim without a custom encoder.
    """

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# ── Keyword catalogues ───────────────────────────────────────────────
#
# Heuristics are deliberately coarse — a user who says "change the
# button colour to blue" is unambiguously small; a user who says
# "redesign the whole dashboard" is unambiguously large.  The middle
# bucket catches everything in between.
#
# Keep rules **bilingual** (English + 繁中) because this project ships
# with mixed-language UX copy and operator prompts.

# "small" intents: pure surface-level edits (text / colour / spacing).
_SMALL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("copy_tweak", re.compile(
        r"(rename|relabel|typo|reword|rewrite\s+(?:the\s+)?(?:copy|label|text)|"
        r"change\s+(?:the\s+)?(?:text|label|title|wording|copy|string|heading)|"
        r"update\s+(?:the\s+)?(?:text|label|title|wording|copy|string)|"
        r"fix\s+(?:the\s+)?(?:typo|wording|copy)|capitalize|"
        r"改字|改文案|改標題|改標籤|改文字|錯字|修字|文案|文字調整|措辭)",
        re.IGNORECASE,
    )),
    ("color_tweak", re.compile(
        r"(recolor|tint|shade|"
        r"(?:change|swap|update|adjust|set)\s+(?:the\s+)?(?:\w+\s+){0,3}?colou?r|"
        r"(?:colou?r)\s+(?:to|should\s+be)|"
        r"make\s+it\s+(?:red|blue|green|yellow|"
        r"purple|orange|pink|black|white|gray|grey|dark|light|bright|muted)|"
        r"darken|lighten|saturate|desaturate|"
        r"改色|改顏色|換色|調色|換個顏色|調個顏色|偏暗|偏亮)",
        re.IGNORECASE,
    )),
    ("spacing_tweak", re.compile(
        r"(spacing|padding|margin|gap|gutter|whitespace|tighter|looser|"
        r"increase\s+(?:the\s+)?(?:spacing|padding|margin|gap)|"
        r"decrease\s+(?:the\s+)?(?:spacing|padding|margin|gap)|"
        r"compact|denser|roomier|"
        r"間距|留白|內距|外距|寬鬆一點|緊湊一點|擁擠)",
        re.IGNORECASE,
    )),
    ("size_tweak", re.compile(
        r"(slightly\s+(?:bigger|smaller|larger)|a\s+bit\s+(?:bigger|smaller|larger)|"
        r"font\s+size|icon\s+size|make\s+it\s+(?:bigger|smaller|larger)\s+(?:a\s+bit|slightly)|"
        r"微調|稍微大一點|稍微小一點|再大一點|再小一點|字級|字型大小)",
        re.IGNORECASE,
    )),
    ("minor_marker", re.compile(
        r"(minor\s+(?:tweak|edit|change|fix)|just\s+(?:change|fix|update)|"
        r"small\s+(?:tweak|edit|change|fix)|tiny\s+(?:tweak|edit|change|fix)|"
        r"quick\s+(?:tweak|edit|change|fix)|"
        r"小改|微調|小修|稍微改|只改|只要改)",
        re.IGNORECASE,
    )),
)

# "large" intents: structural rewrites / new pages / multi-component work.
_LARGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("layout_refactor", re.compile(
        r"(redesign|revamp|overhaul|restructure|rearrange|reorganise|reorganize|"
        r"re-?architect|re-?layout|change\s+(?:the\s+)?layout|new\s+layout|"
        r"rework\s+(?:the\s+)?layout|grid\s+system|column\s+structure|"
        r"重構|重新設計|翻新|改版|改架構|換佈局|重排|重組)",
        re.IGNORECASE,
    )),
    ("new_page", re.compile(
        r"(new\s+page|add\s+(?:a\s+)?page|build\s+(?:a\s+)?page|"
        r"create\s+(?:a\s+)?page|design\s+(?:a\s+)?page|"
        r"landing\s+page|pricing\s+page|settings\s+page|dashboard|"
        r"checkout\s+flow|onboarding\s+flow|auth\s+flow|"
        r"新頁面|新增頁面|建立頁面|做一個(?:.{0,6}?)頁面|設計(?:.{0,6}?)頁面|定價頁|儀表板|登入流程)",
        re.IGNORECASE,
    )),
    ("multi_section", re.compile(
        r"(multi[-\s]?section|several\s+sections|multiple\s+sections|"
        r"hero\s+.*\s+features\s+.*\s+pricing|three\s+tiers|four\s+tiers|"
        r"3\s+tiers|4\s+tiers|three\s+plans|four\s+plans|"
        r"多個區塊|三個方案|四個方案|多欄|多 section)",
        re.IGNORECASE,
    )),
    ("major_marker", re.compile(
        r"(major\s+(?:rewrite|change|refactor)|wholesale\s+(?:rewrite|change)|"
        r"end[-\s]?to[-\s]?end\s+(?:rewrite|redesign)|ground[-\s]?up|"
        r"from\s+scratch|complete\s+(?:rewrite|redesign|overhaul)|"
        r"大改|整個|全部重寫|從頭|打掉重做)",
        re.IGNORECASE,
    )),
    ("state_wiring", re.compile(
        r"(state\s+management|redux|zustand|context\s+provider|"
        r"data\s+fetching|server\s+action|route\s+handler|"
        r"multi[-\s]?step\s+form|wizard|tabs?\s+with\s+state|"
        r"狀態管理|資料流|API\s*串接|多步驟表單)",
        re.IGNORECASE,
    )),
)

# shadcn primitive vocabulary — used to count *how many* distinct
# components the prompt mentions.  More primitives ≈ bigger surface
# area = more reasoning.  Kept conservative (canonical component names
# only, not every sub-part) so a prompt like "tweak the CardHeader
# padding" still counts as 1, not 2.
_SHADCN_PRIMITIVES: frozenset[str] = frozenset({
    "accordion", "alert", "alert-dialog", "alertdialog", "aspectratio", "aspect-ratio",
    "avatar", "badge", "breadcrumb", "button", "buttongroup", "button-group",
    "calendar", "card", "carousel", "chart", "checkbox", "collapsible",
    "combobox", "command", "contextmenu", "context-menu", "datepicker", "date-picker",
    "dialog", "drawer", "dropdownmenu", "dropdown-menu", "field",
    "form", "hovercard", "hover-card", "input", "inputgroup", "input-group",
    "inputotp", "input-otp", "label", "menubar", "navigationmenu", "navigation-menu",
    "pagination", "popover", "progress", "radiogroup", "radio-group", "resizable",
    "scrollarea", "scroll-area", "select", "separator", "sheet",
    "sidebar", "skeleton", "slider", "sonner", "switch", "table",
    "tabs", "textarea", "toast", "toggle", "togglegroup", "toggle-group",
    "tooltip",
})

# Action-verb counter — "and then also add …" style prompts stack work.
_ACTION_VERB_RE = re.compile(
    r"\b(add|remove|delete|rename|swap|replace|move|rearrange|restructure|"
    r"refactor|rewrite|redesign|tweak|fix|update|change|adjust|insert|"
    r"introduce|wire|hook|connect|show|hide|toggle|align|center|centre|stack|"
    r"split|merge|group|ungroup|extract|inline|paginate|animate|implement|"
    r"build|create|design|make|compose)\b",
    re.IGNORECASE,
)

# Conjunction counter — a prompt with 3+ conjunctions probably packs
# several unrelated edits.
_CONJUNCTION_RE = re.compile(
    r"(,|;|、|和|以及|並且|and then|also|additionally|furthermore|moreover)",
    re.IGNORECASE,
)


# Thresholds (tuned by contract tests — change the number, update the
# test).  Keeping them as module-level constants (not magic numbers)
# so that a maintainer can see the policy surface at a glance.

#: Prompts ≤ this many tokens (≈ words) with only small signals bypass
#: the conjunction gate and land in the small bucket.
SMALL_WORD_CEILING = 20

#: Prompts ≥ this many tokens tend to describe multi-step work and
#: default to at least medium unless they're pure copy/colour edits.
LARGE_WORD_FLOOR = 60

#: Number of distinct shadcn primitives that lifts a prompt to large.
LARGE_PRIMITIVE_COUNT = 3

#: Number of action verbs after which we consider the prompt multi-step.
MEDIUM_ACTION_FLOOR = 3
LARGE_ACTION_FLOOR = 5

#: Conjunction count that lifts a prompt's complexity by one bucket.
HEAVY_CONJUNCTION_COUNT = 4


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class EditSignals:
    """Evidence the classifier collected from ``(prompt, context)``.

    Serialisable for telemetry / A/B analysis.  Callers should treat
    the fields as read-only.
    """

    word_count: int = 0
    small_hits: tuple[str, ...] = ()
    large_hits: tuple[str, ...] = ()
    component_mentions: tuple[str, ...] = ()
    action_verb_count: int = 0
    conjunction_count: int = 0
    has_image: bool = False
    has_figma: bool = False
    has_url: bool = False
    has_existing_code: bool = False

    def __post_init__(self) -> None:
        if self.word_count < 0:
            raise ValueError("word_count must be non-negative")
        if self.action_verb_count < 0:
            raise ValueError("action_verb_count must be non-negative")
        if self.conjunction_count < 0:
            raise ValueError("conjunction_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "word_count": self.word_count,
            "small_hits": list(self.small_hits),
            "large_hits": list(self.large_hits),
            "component_mentions": list(self.component_mentions),
            "action_verb_count": self.action_verb_count,
            "conjunction_count": self.conjunction_count,
            "has_image": self.has_image,
            "has_figma": self.has_figma,
            "has_url": self.has_url,
            "has_existing_code": self.has_existing_code,
        }


@dataclass(frozen=True)
class EditRouteDecision:
    """The routing decision — bucket + model + why.

    ``reasons`` is an ordered tuple of short machine-readable tags
    (``"copy_tweak"``, ``"multimodal_context"``, ``"caller_override"``, …)
    that the orchestrator can surface to operators / logs; they're the
    single source of truth for *why* a prompt landed in this bucket.
    """

    complexity: str
    provider: str
    model: str
    reasons: tuple[str, ...]
    signals: EditSignals
    prompt: str = ""
    expected_latency_ms: int = 0

    def __post_init__(self) -> None:
        if self.complexity not in COMPLEXITY_TO_MODEL:
            raise ValueError(
                f"unknown complexity {self.complexity!r}; "
                f"must be one of {sorted(COMPLEXITY_TO_MODEL)}"
            )
        if not self.provider:
            raise ValueError("provider must be non-empty")
        if not self.model:
            raise ValueError("model must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EDIT_ROUTER_SCHEMA_VERSION,
            "complexity": self.complexity,
            "provider": self.provider,
            "model": self.model,
            "reasons": list(self.reasons),
            "signals": self.signals.to_dict(),
            "prompt": self.prompt,
            "expected_latency_ms": self.expected_latency_ms,
        }


# ── Signal extraction ────────────────────────────────────────────────


def _count_words(text: str) -> int:
    """Approximate word count — splits on whitespace and CJK graphemes.

    English words are straightforward; Chinese input has no spaces so
    we count CJK code points individually (each glyph ≈ one "token" for
    the purposes of this heuristic — a 10-character Chinese sentence
    *is* comparable in complexity to a 10-word English sentence).
    """
    if not text:
        return 0
    # Strip markdown code fences before counting — a huge pasted code
    # block shouldn't blow the word-count out of the water.
    stripped = re.sub(r"```[\s\S]*?```", " ", text)
    ascii_words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_'\-]*", stripped))
    cjk_glyphs = len(re.findall(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]", stripped))
    return ascii_words + cjk_glyphs


def _find_components(text: str) -> tuple[str, ...]:
    """Return the sorted, deduped set of shadcn primitives mentioned."""
    if not text:
        return ()
    hits: set[str] = set()
    # Match `<ComponentName`, `<component-name`, PascalCase bare
    # identifiers, or lowercase `component-name` bare tokens.  We
    # normalise to lowercase with internal hyphens stripped before
    # lookup so both "AlertDialog" and "alert-dialog" land on the same
    # catalogue key.
    candidates: list[str] = []
    for match in re.finditer(
        r"<\s*([A-Z][A-Za-z]*|[a-z][a-z-]*[a-z])",
        text,
    ):
        candidates.append(match.group(1))
    for match in re.finditer(
        r"\b([A-Z][a-zA-Z]+(?:[A-Z][a-zA-Z]+)?)\b",
        text,
    ):
        candidates.append(match.group(1))
    # Bare lowercase form only matches *hyphenated* tokens.  Single
    # lowercase words like "input" / "form" / "field" are common
    # English nouns and would over-match; JSX tag form (`<Input>`)
    # and PascalCase (`Input`) already cover the intentional cases.
    for match in re.finditer(
        r"\b([a-z]+(?:-[a-z]+)+)\b",
        text,
    ):
        candidates.append(match.group(1))
    for token in candidates:
        token = (token or "").strip()
        if not token:
            continue
        key = token.lower().replace("_", "-")
        if key in _SHADCN_PRIMITIVES:
            hits.add(key)
            continue
        # PascalCase → hyphen form for matcher; e.g. "AlertDialog" → "alertdialog"
        key2 = re.sub(r"[^a-z0-9]", "", key)
        if key2 in _SHADCN_PRIMITIVES:
            hits.add(key2)
    return tuple(sorted(hits))


def _find_small_hits(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(name for name, pat in _SMALL_PATTERNS if pat.search(text))


def _find_large_hits(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(name for name, pat in _LARGE_PATTERNS if pat.search(text))


def _count_action_verbs(text: str) -> int:
    if not text:
        return 0
    return len(_ACTION_VERB_RE.findall(text))


def _count_conjunctions(text: str) -> int:
    if not text:
        return 0
    return len(_CONJUNCTION_RE.findall(text))


def _extract_signals(
    prompt: str,
    *,
    has_image: bool,
    has_figma: bool,
    has_url: bool,
    has_existing_code: bool,
) -> EditSignals:
    text = prompt or ""
    return EditSignals(
        word_count=_count_words(text),
        small_hits=_find_small_hits(text),
        large_hits=_find_large_hits(text),
        component_mentions=_find_components(text),
        action_verb_count=_count_action_verbs(text),
        conjunction_count=_count_conjunctions(text),
        has_image=bool(has_image),
        has_figma=bool(has_figma),
        has_url=bool(has_url),
        has_existing_code=bool(has_existing_code),
    )


# ── Classifier ───────────────────────────────────────────────────────


def _score(
    signals: EditSignals,
) -> tuple[str, tuple[str, ...]]:
    """Return ``(complexity, reasons)`` from the evidence.

    Order of precedence (highest first):

    1. Multimodal context (image / figma / url) — always large; these
       channels arrive with structural intent by definition.
    2. Prompt-level ``major_marker`` / ``new_page`` / ``layout_refactor``
       keywords — the user is asking for a rewrite.
    3. Primitive-count ≥ :data:`LARGE_PRIMITIVE_COUNT` — touching many
       components tends to mean touching layout.
    4. ``word_count`` ≥ :data:`LARGE_WORD_FLOOR` with any large signal
       or ≥ :data:`LARGE_ACTION_FLOOR` action verbs.
    5. Pure ``small_hits`` with no large signal and ``word_count`` ≤
       :data:`SMALL_WORD_CEILING` → small.
    6. Medium default for mixed / ambiguous input.
    """
    reasons: list[str] = []

    if signals.has_image:
        reasons.append("multimodal_image")
    if signals.has_figma:
        reasons.append("multimodal_figma")
    if signals.has_url:
        reasons.append("multimodal_url")

    # Rule 1 — multimodal context locks the bucket to large.
    if signals.has_image or signals.has_figma or signals.has_url:
        for name in signals.large_hits:
            reasons.append(f"large:{name}")
        return "large", tuple(reasons)

    # Rule 2 — explicit rewrite / new-page markers.
    hard_large = {"major_marker", "new_page", "layout_refactor"}
    matched_hard = [h for h in signals.large_hits if h in hard_large]
    if matched_hard:
        for name in matched_hard:
            reasons.append(f"large:{name}")
        # Include any other large hits too so reasons stay informative.
        for name in signals.large_hits:
            if name not in hard_large:
                reasons.append(f"large:{name}")
        return "large", tuple(reasons)

    # Rule 3 — touching many primitives ≈ touching layout.
    if len(signals.component_mentions) >= LARGE_PRIMITIVE_COUNT:
        reasons.append(
            f"many_primitives:{len(signals.component_mentions)}"
        )
        for name in signals.large_hits:
            reasons.append(f"large:{name}")
        return "large", tuple(reasons)

    # Rule 4 — long prompt + any structural signal.
    if signals.word_count >= LARGE_WORD_FLOOR and (
        signals.large_hits or signals.action_verb_count >= LARGE_ACTION_FLOOR
    ):
        reasons.append(f"long_prompt:{signals.word_count}")
        for name in signals.large_hits:
            reasons.append(f"large:{name}")
        if signals.action_verb_count >= LARGE_ACTION_FLOOR:
            reasons.append(f"many_actions:{signals.action_verb_count}")
        return "large", tuple(reasons)

    # Rule 5 — pure small signals in a short prompt.
    is_short = signals.word_count <= SMALL_WORD_CEILING
    is_very_short = signals.word_count <= SMALL_WORD_CEILING // 2
    has_small = bool(signals.small_hits)
    has_large = bool(signals.large_hits)
    many_conjunctions = signals.conjunction_count >= HEAVY_CONJUNCTION_COUNT

    if has_small and not has_large and is_short and not many_conjunctions:
        for name in signals.small_hits:
            reasons.append(f"small:{name}")
        if is_very_short:
            reasons.append(f"very_short_prompt:{signals.word_count}")
        return "small", tuple(reasons)

    # Rule 5b — extremely short prompt with no large signal and an
    # action verb falls to small too (e.g. "rename the button").
    if (
        is_very_short
        and not has_large
        and signals.action_verb_count <= 1
        and len(signals.component_mentions) <= 1
    ):
        if has_small:
            for name in signals.small_hits:
                reasons.append(f"small:{name}")
        else:
            reasons.append(f"very_short_prompt:{signals.word_count}")
        return "small", tuple(reasons)

    # Rule 6 — medium catch-all.  Surface whatever evidence we saw so
    # the operator can see why we didn't commit.
    if has_large:
        for name in signals.large_hits:
            reasons.append(f"weak_large:{name}")
    if has_small:
        for name in signals.small_hits:
            reasons.append(f"weak_small:{name}")
    if signals.action_verb_count >= MEDIUM_ACTION_FLOOR:
        reasons.append(f"medium_actions:{signals.action_verb_count}")
    if signals.component_mentions:
        reasons.append(
            f"components:{len(signals.component_mentions)}"
        )
    if not reasons:
        reasons.append("ambiguous")
    return "medium", tuple(reasons)


def classify_prompt(
    prompt: str,
    *,
    has_image: bool = False,
    has_figma: bool = False,
    has_url: bool = False,
    has_existing_code: bool = False,
) -> tuple[str, EditSignals, tuple[str, ...]]:
    """Classify a prompt → ``(complexity, signals, reasons)``.

    Pure function: same inputs yield byte-identical outputs.  Never
    raises on blank / ``None``-equivalent input.
    """
    if prompt is None:
        prompt = ""
    signals = _extract_signals(
        prompt,
        has_image=has_image,
        has_figma=has_figma,
        has_url=has_url,
        has_existing_code=has_existing_code,
    )
    if not prompt.strip() and not (
        signals.has_image or signals.has_figma or signals.has_url
    ):
        return "medium", signals, ("empty_prompt",)
    complexity, reasons = _score(signals)
    return complexity, signals, reasons


# ── Public API ───────────────────────────────────────────────────────


def route(
    prompt: str,
    *,
    has_image: bool = False,
    has_figma: bool = False,
    has_url: bool = False,
    has_existing_code: bool = False,
    complexity: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> EditRouteDecision:
    """Route a prompt to ``(provider, model)`` based on edit complexity.

    Positional ``prompt`` is the user's natural-language edit request
    (possibly blank, possibly just "do something").

    ``has_image`` / ``has_figma`` / ``has_url`` signal that a
    multimodal reference is attached — those always route to large
    because they imply structural reconstruction (see sibling modules
    :mod:`backend.vision_to_ui`, :mod:`backend.figma_to_ui`,
    :mod:`backend.url_to_reference`).

    ``has_existing_code`` lets a caller mark "this is an edit to an
    existing file" — it nudges ambiguous prompts slightly toward small
    (editing is usually narrower than greenfield generation).

    ``complexity`` is a hard override (``"small"`` / ``"medium"`` /
    ``"large"``).  Caller-supplied bucket wins; the heuristic still
    collects signals so the decision carries full provenance.

    ``provider`` / ``model`` override the routed model (e.g. ops wants
    to pin everything to Sonnet during an incident).  Unknown values
    are accepted verbatim — the router records them but doesn't guess.
    """
    bucket, signals, reasons = classify_prompt(
        prompt,
        has_image=has_image,
        has_figma=has_figma,
        has_url=has_url,
        has_existing_code=has_existing_code,
    )

    override_reasons: list[str] = []
    if complexity is not None:
        if complexity not in COMPLEXITY_TO_MODEL:
            raise ValueError(
                f"unknown complexity override {complexity!r}; "
                f"must be one of {sorted(COMPLEXITY_TO_MODEL)}"
            )
        if complexity != bucket:
            override_reasons.append(f"caller_override:{bucket}→{complexity}")
        else:
            override_reasons.append("caller_override_confirm")
        bucket = complexity
    elif has_existing_code and bucket == "medium" and not signals.large_hits:
        # Soft nudge: for edits to existing files with no structural
        # signal, prefer small — editing is narrower than creating.
        small_leaning = (
            signals.small_hits
            or signals.word_count <= SMALL_WORD_CEILING
        )
        if small_leaning:
            bucket = "small"
            override_reasons.append("existing_code_nudge")

    default_provider, default_model = COMPLEXITY_TO_MODEL[bucket]
    final_provider = provider or default_provider
    final_model = model or default_model
    if provider:
        override_reasons.append(f"provider_override:{provider}")
    if model:
        override_reasons.append(f"model_override:{model}")

    all_reasons = tuple(list(reasons) + override_reasons)

    return EditRouteDecision(
        complexity=bucket,
        provider=final_provider,
        model=final_model,
        reasons=all_reasons,
        signals=signals,
        prompt=prompt or "",
        expected_latency_ms=EXPECTED_LATENCY_MS[bucket],
    )


def run_edit_router(
    prompt: str,
    *,
    has_image: bool = False,
    has_figma: bool = False,
    has_url: bool = False,
    has_existing_code: bool = False,
    complexity: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Agent-callable entry point: returns a JSON-safe decision dict.

    Mirrors the shape other V1 pipeline modules expose
    (``run_vision_to_ui`` / ``run_figma_to_ui`` / ``run_url_to_reference``)
    so the UI Designer skill's tool table can list this alongside its
    siblings without a bespoke adapter.
    """
    decision = route(
        prompt,
        has_image=has_image,
        has_figma=has_figma,
        has_url=has_url,
        has_existing_code=has_existing_code,
        complexity=complexity,
        provider=provider,
        model=model,
    )
    return decision.to_dict()


def render_decision_markdown(decision: EditRouteDecision) -> str:
    """Deterministic human-readable render of a :class:`EditRouteDecision`.

    Useful for SSE / operator-visible logs.  Byte-identical across
    calls for the same input — safe to include in prompt caches.
    """
    s = decision.signals
    lines = [
        f"# Edit router decision (schema {EDIT_ROUTER_SCHEMA_VERSION})",
        "",
        f"- **complexity**: `{decision.complexity}`",
        f"- **provider:model**: `{decision.provider}:{decision.model}`",
        f"- **expected_latency_ms**: {decision.expected_latency_ms}",
        f"- **reasons**: {', '.join(decision.reasons) or '(none)'}",
        "",
        "## Signals",
        f"- word_count: {s.word_count}",
        f"- small_hits: {', '.join(s.small_hits) or '(none)'}",
        f"- large_hits: {', '.join(s.large_hits) or '(none)'}",
        f"- component_mentions: {', '.join(s.component_mentions) or '(none)'}",
        f"- action_verb_count: {s.action_verb_count}",
        f"- conjunction_count: {s.conjunction_count}",
        f"- has_image: {s.has_image}",
        f"- has_figma: {s.has_figma}",
        f"- has_url: {s.has_url}",
        f"- has_existing_code: {s.has_existing_code}",
    ]
    return "\n".join(lines) + "\n"

"""W11.5 #XXX — L2 LLM content classifier.

Layer 2 of the W11 5-layer defense-in-depth pipeline. Runs **after**
``clone_site()`` produces a populated :class:`backend.web.site_cloner.CloneSpec`
and **before** the L3 transformer (W11.6) rewrites bytes. Its job is to
read the *meaning* of the captured content and assign a ``risk_level``
that downstream policy gates can branch on.

Where it slots into the W11 pipeline
------------------------------------
The full router pattern is::

    decision = await check_machine_refusal_pre_capture(url)        # L1
    if decision.refused: raise MachineRefusedError(...)

    capture = await source.capture(url, ...)

    decision = check_machine_refusal_post_capture(capture)         # L1
    if decision.refused: raise MachineRefusedError(...)

    spec = build_clone_spec_from_capture(capture)                  # W11.3

    classification = await classify_clone_spec(spec)               # L2 ← this row
    assert_clone_spec_safe(spec, classification=classification)    # raises ContentRiskError

    rewritten = transform_clone_spec(spec, classification)         # L3 (W11.6)

    write_manifest(spec, classification, rewritten)                # L4 (W11.7)
    rate_limiter.consume(tenant, target)                           # L5 (W11.8)

L2 sees text + metadata only — never bytes (W11.6 enforces that
structurally upstream). The classifier returns a :class:`RiskClassification`
the calling router persists into the W11.7 manifest + W11.12 audit row,
so a refusal decision is always traceable to (a) the model that produced
it, (b) the categories that fired, (c) the per-category reasons.

Risk levels (lowest → highest)
------------------------------
* ``low``      — clean marketing / portfolio / open-source documentation;
                 no flags. Default outcome for the happy path.
* ``medium``   — tone or topic that warrants a transformer pass but does
                 not block (corporate landing page with PII surfaces,
                 niche regulated topic without explicit advice, etc.).
* ``high``     — content the L3 transformer alone cannot safely rewrite:
                 brand-impersonation surfaces (login forms, payment UI),
                 explicit professional advice (legal / medical / financial),
                 paywalled / DRM-protected primary content. Router refuses
                 with ``ContentRiskError`` unless the operator has set
                 ``OMNISIGHT_CLONE_RISK_THRESHOLD=critical`` to widen.
* ``critical`` — content we never clone: phishing-bait, explicit adult
                 material, illegal goods. Always refuses.

Risk categories (orthogonal flags)
----------------------------------
Multiple categories can fire in one classification. The aggregate
``risk_level`` is the **maximum** level across every fired category.

* ``brand_impersonation`` — recognisable brand identity (logo / tagline /
  copyrighted slogan / trademarked product name).
* ``regulated_advice``    — medical / legal / tax / financial guidance
  served as primary content.
* ``paywalled``           — subscription gate, login wall, or DRM marker
  detected on the captured surface.
* ``adult``               — explicit sexual / pornographic content.
* ``illegal``             — sale of illegal goods, weapons, controlled
  substances, exploitation material.
* ``phishing``            — credential-harvest surface impersonating a
  known brand, suspicious login form, fake payment page.
* ``personal_data``       — surfaces that collect / display third-party
  PII as primary content (directory, doxxing target, scraped database).
* ``clean``               — no flags fired (returned in classifications
  with ``risk_level == 'low'`` for explicitness — the absence of a flag
  is itself a recordable signal).

Two-tier evaluation (heuristic prefilter → LLM)
-----------------------------------------------
1. **Heuristic prefilter** (:func:`heuristic_risk_signals`) — pure-python
   keyword sweep over the spec's text surfaces. Catches the obvious
   ``casino`` / ``porn`` / ``credit-card-required`` markers without
   spending an LLM call. Always runs first.
2. **LLM classifier** (:class:`ClassifierLLM` Protocol — default
   :class:`LangchainClassifierLLM`) — sees the prefilter result and
   the prepared spec excerpt, returns a JSON envelope with the final
   ``risk_level`` + categories + per-category reasons. Reuses
   ``get_cheapest_model()`` so the call lands on Haiku 4.5 / DeepSeek /
   Groq tiers — the row spec calls out "Haiku/Gemini Flash" but in
   practice we honour whatever the operator's cheapest-pref chain has
   credentials for.

The two tiers are **additive**: a heuristic ``critical`` short-circuits
the LLM call entirely (we already know we're refusing). A heuristic
``low`` still runs the LLM so a content shape the keywords missed gets
a second look.

Fail-closed semantics
---------------------
When the LLM is unavailable (token freeze / no API key / circuit
breaker open / parse failure) :func:`classify_clone_spec` defaults to
``risk_level='high'`` with reason ``"classifier_unavailable"`` so the
router refuses by default. Operators that need a different posture set
``fail_open=True`` at the call site (audit row will record the override).

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable constants (risk-level tuple,
category tuple, keyword frozensets, prompt strings, compiled regexes).
The default :class:`LangchainClassifierLLM` constructs its underlying
LangChain chat model lazily via :func:`backend.agents.llm.get_cheapest_model`
which itself owns a per-worker cache (already audited in that module).
Cross-worker consistency: trivially answer #1 — every worker derives the
same constants + same prompt template from source. Operators that want
to coordinate per-tenant classification quotas plug their own
``ClassifierLLM`` implementation in.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — every entry point is a pure function over an in-memory
:class:`CloneSpec` plus, optionally, a single round-trip to the
configured LLM. No shared writable state, no parallel-vs-serial timing
dependence.

Production Readiness Gate §158
------------------------------
No new pip dependencies (langchain + cheapest-model preference list
already in production image via :mod:`backend.agents.llm`). No image
rebuild needed.

Inspired by firecrawl/open-lovable (MIT). The full attribution + license
text lands in the W11.13 row.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from backend.web.site_cloner import CloneSpec, SiteClonerError

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

#: Risk levels in *ascending* order of severity. The aggregate level of
#: a classification is ``max(level)`` over every fired category.
RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "critical")

#: Position of each risk level in ``RISK_LEVELS`` — used by
#: :func:`_max_level` to compare two strings without an enum import.
_RISK_LEVEL_INDEX: Mapping[str, int] = {
    level: idx for idx, level in enumerate(RISK_LEVELS)
}

#: The risk threshold at which :func:`assert_clone_spec_safe` raises by
#: default. ``high`` matches the production policy: the L3 transformer
#: cannot safely rewrite high-risk content, so the router blocks.
DEFAULT_REFUSAL_THRESHOLD: str = "high"

#: Recognised category flags. A classification may fire any subset of
#: these; the aggregate ``risk_level`` is the maximum across all fired
#: categories. ``"clean"`` is special — it appears alone when no risk
#: flag fires (so the audit log records "we evaluated and found nothing"
#: distinct from "we never evaluated").
RISK_CATEGORIES: Tuple[str, ...] = (
    "brand_impersonation",
    "regulated_advice",
    "paywalled",
    "adult",
    "illegal",
    "phishing",
    "personal_data",
    "clean",
)

#: Hard cap on the prompt input we hand to the cheap classifier model.
#: Haiku 4.5's 200K context could swallow much more, but every byte we
#: send burns operator quota and pushes the per-classification cost up.
#: 8 KiB of curated CloneSpec excerpts is enough to assess intent — the
#: LLM is reading the *outline*, not the full body copy.
MAX_PROMPT_INPUT_CHARS: int = 8_000

#: Cap on the per-category reasons string the LLM may return. Hard
#: truncation is enforced post-parse so a hallucinated 50-page reason
#: cannot bloat the audit log row or the operator-facing 451 body.
MAX_REASON_CHARS: int = 280

#: Cap on the number of distinct reasons we keep per classification.
#: Real content rarely fires more than 2–3 categories at once; the cap
#: defends against LLM token-spam without truncating real signal.
MAX_REASONS: int = 8

#: Default ``risk_level`` returned when the LLM is unavailable AND
#: ``fail_open=False`` (the default). Picked at ``high`` so the router's
#: ``DEFAULT_REFUSAL_THRESHOLD == "high"`` gate blocks. Operators that
#: explicitly opt in to "best effort" classification pass
#: ``fail_open=True`` and accept that an LLM-down state will surface as
#: ``risk_level='low'`` instead.
_FAIL_CLOSED_LEVEL: str = "high"
_FAIL_OPEN_LEVEL: str = "low"

#: Default model identifier we record on the classification when the
#: caller doesn't pass an explicit ``llm``. Surfaced in the audit log as
#: the ``model`` field. Real classifications from the cheapest-model
#: chain will overwrite this with the picked provider's id; this string
#: is only the fall-through label when no cheapest-model lookup runs.
DEFAULT_CLASSIFIER_MODEL: str = "cheapest-llm-chain"

#: Heuristic keyword surface — lower-cased substring matches against the
#: spec excerpt. Any hit fires the named category; the aggregate level
#: is then the max across fired categories. Tuned for **precision** over
#: recall: the LLM tier picks up the recall side.
#:
#: Each tuple is ``(category, level, keywords...)``. Keywords are matched
#: case-insensitively as plain substrings of the excerpt — phrase order
#: + punctuation are not significant.
#:
#: Module-global audit: this is an immutable nested tuple — every worker
#: derives the identical ruleset from source (SOP answer #1). Operators
#: that want a custom keyword list pass their own
#: :class:`ClassifierLLM` whose ``classify_text`` returns the desired
#: result.
_HEURISTIC_RULES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "adult",
        "critical",
        (
            "xxx", "porn", "pornography", "explicit sex", "hardcore",
            "adult only", "adults only", "nsfw", "onlyfans",
            "escort service", "live cams",
        ),
    ),
    (
        "illegal",
        "critical",
        (
            "buy cocaine", "buy heroin", "child porn", "csam",
            "weapons for sale", "firearms shipped",
            "fake passports", "stolen credit cards",
            "money laundering service", "hire a hitman",
            "illicit drugs",
        ),
    ),
    (
        "phishing",
        "critical",
        (
            "verify your account immediately",
            "your account has been suspended",
            "click here to confirm your password",
            "re-enter your credit card",
            "urgent action required",
            "limited-time security alert",
            "we have detected unusual activity",
        ),
    ),
    (
        "regulated_advice",
        "high",
        (
            "medical advice", "diagnose your", "prescribe medication",
            "legal advice", "attorney-client", "tax advice",
            "investment advice", "financial planning consultation",
            "this is not a substitute for professional",
        ),
    ),
    (
        "paywalled",
        "high",
        (
            "subscribe to read", "subscribers only", "members only",
            "paid subscription required", "log in to continue reading",
            "this article is for subscribers", "premium content",
            "sign in to unlock",
        ),
    ),
    (
        "personal_data",
        "medium",
        (
            "personal records database", "people search",
            "background check report", "ssn lookup",
            "find someone's address", "phone number lookup",
            "criminal records lookup",
        ),
    ),
    (
        "brand_impersonation",
        "medium",
        (
            "official login portal", "verify your apple id",
            "verify your microsoft account", "verify your google account",
            "verify your amazon account", "verify your paypal",
            "log in to your bank", "online banking sign in",
        ),
    ),
)

#: System prompt the LLM tier runs against. Pinned as a module constant
#: so prompt drift is a code-reviewable diff. The prompt instructs the
#: model to emit a strict JSON envelope; :func:`_parse_llm_envelope`
#: tolerates code-fence wrapping + leading prose so a chatty model
#: doesn't break the parse.
#:
#: The envelope shape we contract on::
#:
#:     {
#:       "risk_level": "low" | "medium" | "high" | "critical",
#:       "categories": [
#:         {
#:           "name": "<one of RISK_CATEGORIES>",
#:           "level": "low" | "medium" | "high" | "critical",
#:           "reason": "<one short sentence>"
#:         }
#:       ]
#:     }
#:
#: The model is told that "clean" is the correct response when no flag
#: fires, so a happy-path landing page returns
#: ``{"risk_level":"low","categories":[{"name":"clean",...}]}``.
LLM_SYSTEM_PROMPT: str = """\
You are a content-risk classifier for an automated website cloner.
Given an excerpt of a target page (title, hero, sections, footer, meta
description) you decide whether the page can be safely cloned and
transformed by a downstream productizer pipeline.

Risk levels (ascending): low, medium, high, critical.
- low      = generic marketing / portfolio / docs. Safe to clone.
- medium   = niche tone that benefits from a rewrite pass but does not
             block. Personal-data directories, mild brand surfaces.
- high     = blocks unless an operator has explicitly widened the
             threshold. Regulated advice (medical / legal / financial /
             tax), paywalled primary content, brand-impersonation
             login UI.
- critical = always refused. Adult / pornographic content, illegal
             goods, phishing-bait surfaces.

Risk categories (you may flag any subset; "clean" appears ALONE when no
other category fires):
brand_impersonation, regulated_advice, paywalled, adult, illegal,
phishing, personal_data, clean.

Output STRICT JSON only — no prose, no markdown fences. Shape:

{
  "risk_level": "low|medium|high|critical",
  "categories": [
    {"name": "<category>", "level": "low|medium|high|critical",
     "reason": "<one short sentence, plain ASCII, ≤ 200 chars>"}
  ]
}

The aggregate "risk_level" MUST equal the maximum "level" across the
"categories" array. Return at most 8 category entries. If unsure,
prefer the higher level.
"""

#: User-prompt template. ``{excerpt}`` is the only placeholder; never
#: f-string this directly with untrusted input — :func:`_render_user_prompt`
#: does the substitution after sanitising the excerpt.
LLM_USER_PROMPT_TEMPLATE: str = """\
Page excerpt to classify (heuristic prefilter has already flagged
{prefilter_summary}):

---
{excerpt}
---

Return the JSON envelope described in the system prompt.
"""

#: Pre-compiled fenced-code regex used by :func:`_parse_llm_envelope`
#: to strip `````json`` / ``````` wrappers.
_FENCED_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)

#: Greedy "first balanced JSON object" extractor — used as a fallback
#: when the model emits prose around the JSON instead of (or in
#: addition to) a code fence.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ── Errors ──────────────────────────────────────────────────────────────


class ContentClassifierError(SiteClonerError):
    """Base class for everything raised by ``content_classifier``.

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so a
    single ``except SiteClonerError`` in the calling router catches both
    layers; the W11.12 audit row uses ``isinstance`` to assign the
    finer bucket.
    """


class ContentRiskError(ContentClassifierError):
    """Raised by :func:`assert_clone_spec_safe` when the classification's
    aggregate ``risk_level`` meets or exceeds the refusal threshold.

    Carries the full :class:`RiskClassification` so the calling router
    can echo per-category reasons into the audit log + the operator-
    facing 451 / 403 response body.
    """

    def __init__(
        self,
        classification: "RiskClassification",
        *,
        threshold: str,
    ) -> None:
        self.classification = classification
        self.threshold = threshold
        joined = "; ".join(classification.reasons) or "no specific reason"
        super().__init__(
            f"refused: risk_level={classification.risk_level!r} "
            f"(threshold={threshold!r}): {joined}"
        )


class ClassifierUnavailableError(ContentClassifierError):
    """Raised internally by :class:`LangchainClassifierLLM` when no
    cheapest-model entry has credentials AND ``get_llm()`` itself
    returned ``None`` (token freeze / circuit breaker / no keys).

    :func:`classify_clone_spec` catches this and translates to a
    fail-closed classification (``risk_level='high'``,
    reason ``classifier_unavailable``) unless ``fail_open=True``.
    """


# ── Data structures ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RiskScore:
    """One per-category entry in a :class:`RiskClassification`.

    Attributes:
        category: One of :data:`RISK_CATEGORIES`.
        level: One of :data:`RISK_LEVELS`. The aggregate
            ``risk_level`` of the surrounding classification is the
            maximum across every score's level.
        reason: One short sentence describing why this category fired.
            Truncated to :data:`MAX_REASON_CHARS` chars.
    """

    category: str
    level: str
    reason: str


@dataclass(frozen=True)
class RiskClassification:
    """Aggregate output of :func:`classify_clone_spec`.

    A ``RiskClassification`` is *additive*: the heuristic prefilter
    produces an interim classification, the LLM tier produces a second,
    and :func:`merge_risk_classifications` combines them so the audit
    log records every signal that fired regardless of layer.

    Attributes:
        risk_level: One of :data:`RISK_LEVELS`. The maximum across
            every score's ``level``.
        scores: Tuple of per-category :class:`RiskScore` entries.
            Empty iff the classifier ran AND no category fired —
            distinct from "the classifier never ran" (caller branches
            on ``signals_used``).
        model: Identifier of the LLM (or ``"heuristic"``) that produced
            the classification. Pinned in the W11.7 manifest + W11.12
            audit row.
        signals_used: Stable identifiers of the layers that contributed
            (``"heuristic"`` / ``"llm"`` / ``"fail_closed"`` /
            ``"fail_open"``). Empty list AND ``risk_level == 'low'``
            means "no checks ran" — distinct from "checks ran and all
            passed".
        prefilter_only: ``True`` when the LLM tier was deliberately
            skipped (heuristic prefilter already produced a critical
            verdict). Lets the audit log distinguish "we saved an LLM
            call by short-circuit" from "we skipped because the LLM was
            down".
    """

    risk_level: str
    scores: Tuple[RiskScore, ...]
    model: str
    signals_used: Tuple[str, ...] = ()
    prefilter_only: bool = False

    @property
    def categories(self) -> Tuple[str, ...]:
        """Stable-ordered tuple of category names that fired."""
        return tuple(s.category for s in self.scores)

    @property
    def reasons(self) -> Tuple[str, ...]:
        """Per-category reason strings in the same order as ``scores``."""
        return tuple(s.reason for s in self.scores)

    @property
    def is_clean(self) -> bool:
        """``True`` iff the only score (if any) is the ``clean`` flag."""
        if not self.scores:
            return True
        return all(s.category == "clean" for s in self.scores)


# ── Backend protocol ───────────────────────────────────────────────────


@runtime_checkable
class ClassifierLLM(Protocol):
    """Pluggable LLM backend for the L2 classifier.

    Default :class:`LangchainClassifierLLM` routes via
    :func:`backend.agents.llm.get_cheapest_model`. Tests / air-gap setups
    substitute a fake that returns a canned envelope without spending an
    LLM call.

    The protocol is deliberately tiny — single async method, one string
    in (the rendered prompt), one string out (the raw model response).
    Envelope parsing lives in :func:`_parse_llm_envelope` so adapters
    don't have to know the JSON shape.
    """

    name: str
    """Stable identifier emitted into ``RiskClassification.model``."""

    async def classify_text(self, prompt: str, *, system: str) -> str: ...


# ── Helpers ────────────────────────────────────────────────────────────


def _max_level(a: str, b: str) -> str:
    """Return the larger of two risk levels (string-comparison via
    :data:`_RISK_LEVEL_INDEX`)."""
    ai = _RISK_LEVEL_INDEX.get(a, -1)
    bi = _RISK_LEVEL_INDEX.get(b, -1)
    return a if ai >= bi else b


def _truncate(s: str, limit: int) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _normalize_text(s: str) -> str:
    return " ".join((s or "").split())


def _coerce_level(s: object) -> Optional[str]:
    """Lower-case + strip; return ``None`` if not in :data:`RISK_LEVELS`."""
    if not isinstance(s, str):
        return None
    norm = s.strip().lower()
    return norm if norm in _RISK_LEVEL_INDEX else None


def _coerce_category(s: object) -> Optional[str]:
    """Lower-case + strip; return ``None`` if not in :data:`RISK_CATEGORIES`."""
    if not isinstance(s, str):
        return None
    norm = s.strip().lower()
    return norm if norm in RISK_CATEGORIES else None


def _spec_excerpt(spec: CloneSpec) -> str:
    """Render a curated text excerpt of ``spec`` for prompt input.

    The excerpt deliberately omits raw bytes / asset URLs / colour
    tokens — they carry no semantic risk signal. We include:

        title
        meta description / og:description / twitter:description
        hero (heading + tagline + cta label)
        nav link labels (top 12)
        sections[*].heading + summary (top 6 sections)
        footer text + footer link labels (top 12)

    Caps each surface so even a pathological page can't blow past
    :data:`MAX_PROMPT_INPUT_CHARS`. The caller (heuristic + LLM) sees
    exactly the same text so a heuristic match guarantees the LLM
    *could* have seen the trigger phrase.
    """
    lines: list[str] = []

    if spec.title:
        lines.append(f"TITLE: {_normalize_text(spec.title)}")

    meta = spec.meta or {}
    for key in ("description", "og:description", "twitter:description"):
        val = meta.get(key)
        if val:
            lines.append(f"META {key}: {_normalize_text(val)[:500]}")
            break  # one description is enough for intent

    hero = spec.hero or {}
    hero_bits: list[str] = []
    if hero.get("heading"):
        hero_bits.append(_normalize_text(str(hero["heading"])))
    if hero.get("tagline"):
        hero_bits.append(_normalize_text(str(hero["tagline"])))
    cta = hero.get("cta") if isinstance(hero, dict) else None
    if isinstance(cta, dict) and cta.get("label"):
        hero_bits.append(f"CTA[{_normalize_text(str(cta['label']))}]")
    if hero_bits:
        lines.append("HERO: " + " | ".join(hero_bits))

    nav_labels = [
        _normalize_text(str(item.get("label", "")))
        for item in (spec.nav or [])[:12]
        if isinstance(item, dict) and item.get("label")
    ]
    if nav_labels:
        lines.append("NAV: " + " · ".join(nav_labels))

    for idx, section in enumerate((spec.sections or [])[:6]):
        if not isinstance(section, dict):
            continue
        heading = _normalize_text(str(section.get("heading") or ""))
        summary = _normalize_text(str(section.get("summary") or ""))
        if heading or summary:
            lines.append(
                f"SECTION[{idx}] {heading[:120]}: {summary[:400]}"
            )

    footer = spec.footer or {}
    if isinstance(footer, dict):
        ftext = _normalize_text(str(footer.get("text", "")))
        if ftext:
            lines.append(f"FOOTER TEXT: {ftext[:400]}")
        flink_labels = [
            _normalize_text(str(item.get("label", "")))
            for item in (footer.get("links") or [])[:12]
            if isinstance(item, dict) and item.get("label")
        ]
        if flink_labels:
            lines.append("FOOTER LINKS: " + " · ".join(flink_labels))

    excerpt = "\n".join(lines).strip()
    return _truncate(excerpt, MAX_PROMPT_INPUT_CHARS)


# ── Heuristic prefilter ────────────────────────────────────────────────


def heuristic_risk_signals(spec: CloneSpec) -> RiskClassification:
    """Pure-python keyword sweep over :func:`_spec_excerpt`.

    Returns a :class:`RiskClassification` with ``model='heuristic'`` and
    ``signals_used=('heuristic',)``. Every fired rule contributes one
    :class:`RiskScore`; the aggregate level is the max across fired
    rules. When nothing matches the result is the canonical "clean"
    classification (``risk_level='low'``,
    ``scores=(RiskScore('clean','low','no heuristic flags fired'),)``).

    This function never raises. It is safe to call before the LLM tier
    so the caller can short-circuit on a heuristic ``critical`` (no
    point burning an LLM call when we already know we're refusing).

    Module-global audit: rules table is an immutable nested tuple at
    module scope. Each call constructs a fresh classification —
    no shared mutable state.
    """
    excerpt_lc = _spec_excerpt(spec).lower()
    if not excerpt_lc:
        # Empty spec → cannot classify. Caller should branch on
        # ``signals_used == ('heuristic',)`` + clean to decide whether
        # to skip the LLM tier (we recommend running it anyway in case
        # the spec was constructed without an excerpt-bearing surface).
        return RiskClassification(
            risk_level="low",
            scores=(RiskScore("clean", "low", "no excerpt to scan"),),
            model="heuristic",
            signals_used=("heuristic",),
            prefilter_only=False,
        )

    fired: list[RiskScore] = []
    seen_categories: set[str] = set()
    aggregate_level = "low"

    for category, level, keywords in _HEURISTIC_RULES:
        if category in seen_categories:
            continue
        for kw in keywords:
            if kw in excerpt_lc:
                reason = (
                    f"heuristic match: keyword {kw!r} present in excerpt"
                )
                fired.append(RiskScore(
                    category=category,
                    level=level,
                    reason=_truncate(reason, MAX_REASON_CHARS),
                ))
                seen_categories.add(category)
                aggregate_level = _max_level(aggregate_level, level)
                break  # one keyword is enough to fire this category

    if not fired:
        return RiskClassification(
            risk_level="low",
            scores=(RiskScore(
                "clean", "low", "no heuristic flags fired",
            ),),
            model="heuristic",
            signals_used=("heuristic",),
            prefilter_only=False,
        )

    return RiskClassification(
        risk_level=aggregate_level,
        scores=tuple(fired[:MAX_REASONS]),
        model="heuristic",
        signals_used=("heuristic",),
        prefilter_only=False,
    )


# ── LLM tier ────────────────────────────────────────────────────────────


class LangchainClassifierLLM:
    """Default :class:`ClassifierLLM` backed by
    :func:`backend.agents.llm.get_cheapest_model`.

    Routes the classification to the cheapest configured tier (Haiku 4.5
    / DeepSeek / OpenRouter / Groq, in that preference order) so a
    flagship Opus key is never burned on a single-classification call.
    The W11.5 row spec calls out "Haiku/Gemini Flash"; in practice we
    honour whatever the operator's cheapest-pref chain has credentials
    for — Haiku 4.5 is the second entry on the list and is hit first
    when no DeepSeek key is configured.

    Lazy init: the underlying LangChain client is constructed on first
    ``classify_text`` call so importing this module never spins up an
    LLM connection. ``get_cheapest_model`` itself owns the per-worker
    cache (already audited in that module).

    Raises :class:`ClassifierUnavailableError` from ``classify_text``
    when the cheapest-model chain returns ``None`` (token freeze /
    circuit breaker open / no keys). :func:`classify_clone_spec`
    catches and translates to the fail-closed classification.
    """

    name: str = DEFAULT_CLASSIFIER_MODEL

    def __init__(self) -> None:
        self._llm: Any = None  # lazy-initialised

    def _get_llm(self) -> Any:
        if self._llm is None:
            from backend.agents.llm import get_cheapest_model

            llm = get_cheapest_model()
            if llm is None:
                raise ClassifierUnavailableError(
                    "no LLM available for content classification "
                    "(token freeze / no provider credentials / "
                    "all providers in cooldown)"
                )
            self._llm = llm
            # Update ``name`` to the actual picked model for the audit
            # row so operators reading the manifest see Haiku-4 vs
            # DeepSeek-chat etc. — not just the cheapest-chain alias.
            picked = getattr(llm, "model_name", None) or getattr(
                llm, "model", None,
            )
            if picked:
                self.name = str(picked)
        return self._llm

    async def classify_text(self, prompt: str, *, system: str) -> str:
        llm = self._get_llm()
        # LangChain BaseChatModel exposes ``ainvoke`` for async + a
        # message-list contract. We avoid ``langchain_core.messages``
        # imports here to keep the adapter firewall — the chat model
        # accepts ``[("system", ...), ("user", ...)]`` tuples on every
        # supported langchain-* version we ship.
        try:
            resp = await llm.ainvoke([
                ("system", system),
                ("user", prompt),
            ])
        except Exception as exc:  # pragma: no cover - provider error
            raise ClassifierUnavailableError(
                f"LLM call failed: {exc!s}"
            ) from exc
        # ``ainvoke`` returns a LangChain message; ``.content`` is the
        # string we want. Some providers / older adapters return the
        # raw string directly — accept both.
        content = getattr(resp, "content", resp)
        if isinstance(content, list):
            # langchain-anthropic >= 0.3 returns a list of content blocks
            # for multi-modal responses; concatenate text blocks.
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content") or ""
                    if text:
                        parts.append(str(text))
                else:
                    parts.append(str(block))
            content = "\n".join(parts)
        return str(content) if content is not None else ""


# ── Envelope parsing ───────────────────────────────────────────────────


def _parse_llm_envelope(raw: str) -> Optional[dict]:
    """Best-effort parse of the LLM's response into the envelope dict.

    Strategies tried in order:

        1. ``json.loads`` on the raw string.
        2. Strip `````json`` / ``````` fences and retry.
        3. Greedy first-balanced-``{...}`` extraction and retry.

    Returns the parsed dict on success, ``None`` if every strategy fails.
    Never raises — :func:`classify_clone_spec` translates ``None`` into
    the fail-closed classification with reason ``"llm_parse_failed"``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None

    candidates: list[str] = [raw.strip()]
    fence_match = _FENCED_BLOCK_RE.search(raw)
    if fence_match:
        candidates.append(fence_match.group("body").strip())
    obj_match = _JSON_OBJECT_RE.search(raw)
    if obj_match:
        candidates.append(obj_match.group(0).strip())

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _envelope_to_classification(
    envelope: dict,
    *,
    model: str,
    signals_used: Tuple[str, ...],
) -> RiskClassification:
    """Translate a parsed envelope into a :class:`RiskClassification`.

    Lenient parser:

        * Unknown ``risk_level`` strings → fall back to ``low`` and
          re-derive the aggregate from the categories.
        * Unknown ``categories[*].name`` entries are dropped silently
          (we never invent category flags).
        * Per-category ``level`` is coerced into :data:`RISK_LEVELS`;
          unparseable levels default to the parent ``risk_level``.
        * Reasons are truncated to :data:`MAX_REASON_CHARS`.
        * Empty / missing ``categories`` array + a usable
          ``risk_level`` → synthesise a single ``("clean", level, "")``
          score so the classification is still well-formed.

    The aggregate ``risk_level`` is **always** recomputed from the
    accepted scores so the parsed dict cannot lie about its own max.
    """
    raw_scores = envelope.get("categories")
    if not isinstance(raw_scores, list):
        raw_scores = []

    scores: list[RiskScore] = []
    seen_categories: set[str] = set()
    for entry in raw_scores:
        if not isinstance(entry, dict):
            continue
        category = _coerce_category(entry.get("name"))
        if category is None or category in seen_categories:
            continue
        level = _coerce_level(entry.get("level")) or _coerce_level(
            envelope.get("risk_level")
        ) or "low"
        reason = entry.get("reason") or entry.get("explanation") or ""
        if not isinstance(reason, str):
            reason = str(reason)
        scores.append(RiskScore(
            category=category,
            level=level,
            reason=_truncate(_normalize_text(reason), MAX_REASON_CHARS),
        ))
        seen_categories.add(category)
        if len(scores) >= MAX_REASONS:
            break

    if not scores:
        # No categories survived parsing; honour ``risk_level`` if set
        # but rebrand as ``clean`` so the audit row records "we ran
        # the LLM, nothing fired".
        level = _coerce_level(envelope.get("risk_level")) or "low"
        scores = [RiskScore(
            "clean", level, "llm produced no category entries",
        )]

    aggregate = "low"
    for s in scores:
        aggregate = _max_level(aggregate, s.level)

    return RiskClassification(
        risk_level=aggregate,
        scores=tuple(scores),
        model=model,
        signals_used=signals_used,
        prefilter_only=False,
    )


def _render_user_prompt(
    spec: CloneSpec,
    *,
    prefilter: RiskClassification,
) -> str:
    """Render the LLM user prompt with the spec excerpt + prefilter
    summary substituted in. Does no f-string interpolation on attacker
    strings (the format is positional)."""
    excerpt = _spec_excerpt(spec)
    if prefilter.is_clean:
        prefilter_summary = "no risk categories"
    else:
        prefilter_summary = ", ".join(
            f"{s.category}={s.level}" for s in prefilter.scores
        ) or "no risk categories"
    return LLM_USER_PROMPT_TEMPLATE.format(
        prefilter_summary=prefilter_summary,
        excerpt=excerpt or "(empty excerpt)",
    )


# ── Public entry points ────────────────────────────────────────────────


async def classify_clone_spec(
    spec: CloneSpec,
    *,
    llm: Optional[ClassifierLLM] = None,
    fail_open: bool = False,
    skip_heuristic: bool = False,
) -> RiskClassification:
    """Run the full L2 pipeline over ``spec`` and return the aggregate
    classification.

    Pipeline:

        1. Heuristic prefilter (:func:`heuristic_risk_signals`). If the
           heuristic returns ``critical`` we short-circuit — there's no
           value in spending an LLM call to confirm a refusal.
        2. LLM classification through ``llm`` (default
           :class:`LangchainClassifierLLM`). Renders
           :data:`LLM_SYSTEM_PROMPT` + the templated user prompt with
           the spec excerpt + prefilter summary.
        3. Merge the two classifications (:func:`merge_risk_classifications`)
           so the audit row records every signal that fired.

    Failure modes (all surface as a returned classification — never
    raises ``ContentClassifierError`` directly):

        * LLM unavailable (``ClassifierUnavailableError`` from default
          backend): fall back to heuristic-only result. When the
          heuristic is also clean AND ``fail_open=False`` (default),
          the returned classification is ``high`` with reason
          ``classifier_unavailable``. With ``fail_open=True`` we trust
          the heuristic clean.
        * LLM parse failure (model returned non-JSON / empty): same
          fallback as above with reason ``llm_parse_failed``.

    Args:
        spec: Populated :class:`CloneSpec` from W11.3.
        llm: Optional :class:`ClassifierLLM` override — tests pass a
            fake; air-gap deployments may pass a self-hosted backend.
            ``None`` (default) constructs a :class:`LangchainClassifierLLM`
            which routes via :func:`get_cheapest_model`.
        fail_open: When ``True`` an LLM-down state surfaces as the
            heuristic's verdict (clean if no keywords fired) instead of
            the fail-closed ``high``. Audit log records the override
            via ``signals_used = ('heuristic', 'fail_open')``.
        skip_heuristic: When ``True`` the heuristic prefilter is not
            consulted (LLM always runs, no short-circuit). Mostly useful
            for unit tests of the LLM tier in isolation.

    Returns:
        A populated :class:`RiskClassification`. Never raises.

    Module-global audit: classifies one spec at a time, owns no shared
    mutable state. Cross-worker consistency: trivially answer #1 — the
    same (spec, prompt template, model) inputs produce equivalent
    outputs on every worker (LLM-side variance is provider-side, not
    classifier-side).
    """
    if not isinstance(spec, CloneSpec):
        raise ContentClassifierError(
            f"spec must be CloneSpec, got {type(spec).__name__}"
        )

    if skip_heuristic:
        prefilter = RiskClassification(
            risk_level="low",
            scores=(),
            model="heuristic",
            signals_used=(),
            prefilter_only=False,
        )
    else:
        prefilter = heuristic_risk_signals(spec)

    # Short-circuit on a heuristic critical — no point burning an LLM
    # call to confirm a refusal we're already going to make.
    if prefilter.risk_level == "critical":
        return RiskClassification(
            risk_level=prefilter.risk_level,
            scores=prefilter.scores,
            model=prefilter.model,
            signals_used=prefilter.signals_used,
            prefilter_only=True,
        )

    backend = llm if llm is not None else LangchainClassifierLLM()
    user_prompt = _render_user_prompt(spec, prefilter=prefilter)

    try:
        raw = await backend.classify_text(
            user_prompt, system=LLM_SYSTEM_PROMPT,
        )
    except ClassifierUnavailableError as exc:
        return _fail_closed_or_open(
            prefilter=prefilter,
            reason=f"classifier_unavailable: {exc!s}",
            fail_open=fail_open,
            model=getattr(backend, "name", DEFAULT_CLASSIFIER_MODEL),
        )

    envelope = _parse_llm_envelope(raw)
    if envelope is None:
        return _fail_closed_or_open(
            prefilter=prefilter,
            reason="llm_parse_failed: model output was not valid JSON",
            fail_open=fail_open,
            model=getattr(backend, "name", DEFAULT_CLASSIFIER_MODEL),
        )

    llm_result = _envelope_to_classification(
        envelope,
        model=getattr(backend, "name", DEFAULT_CLASSIFIER_MODEL),
        signals_used=("llm",),
    )

    if skip_heuristic:
        return llm_result
    return merge_risk_classifications(prefilter, llm_result)


def _fail_closed_or_open(
    *,
    prefilter: RiskClassification,
    reason: str,
    fail_open: bool,
    model: str,
) -> RiskClassification:
    """Build the fallback classification when the LLM tier could not
    contribute.

    ``fail_open=False`` (production default) → escalate to
    :data:`_FAIL_CLOSED_LEVEL` so the router refuses, regardless of
    what the heuristic concluded. The heuristic's signals are still
    recorded so the audit row preserves them.

    ``fail_open=True`` → trust the heuristic verdict; the audit row
    records the override via ``signals_used`` so a later policy review
    can spot operators that widened the gate.
    """
    if fail_open:
        return RiskClassification(
            risk_level=prefilter.risk_level,
            scores=prefilter.scores,
            model=model,
            signals_used=tuple(prefilter.signals_used) + ("fail_open",),
            prefilter_only=False,
        )

    extra = RiskScore(
        category="clean" if prefilter.is_clean else prefilter.scores[0].category,
        level=_FAIL_CLOSED_LEVEL,
        reason=_truncate(reason, MAX_REASON_CHARS),
    )
    # Drop the prefilter's "clean" stub when we're escalating — it
    # would confuse the audit row by claiming both clean and high.
    base_scores = tuple(s for s in prefilter.scores if s.category != "clean")
    return RiskClassification(
        risk_level=_FAIL_CLOSED_LEVEL,
        scores=base_scores + (extra,),
        model=model,
        signals_used=tuple(prefilter.signals_used) + ("fail_closed",),
        prefilter_only=False,
    )


def merge_risk_classifications(
    *classifications: RiskClassification,
) -> RiskClassification:
    """Combine multiple classifications into one.

    The aggregate ``risk_level`` is the max across every input. The
    scores are concatenated with category-level dedupe (later entries
    win on tie — so an LLM verdict on a category overrides the
    heuristic's match for the same category, which is the right
    precedence: the LLM saw richer context). ``signals_used`` is
    additively merged in input order. ``model`` is the last input's
    model (so the audit row records the most authoritative source).

    Empty arg list raises ``ContentClassifierError`` — callers should
    pass at least one classification.
    """
    if not classifications:
        raise ContentClassifierError(
            "merge_risk_classifications needs at least one input"
        )

    aggregate = "low"
    seen_categories: dict[str, RiskScore] = {}
    signals: list[str] = []
    seen_signals: set[str] = set()
    last_model = classifications[-1].model

    for c in classifications:
        aggregate = _max_level(aggregate, c.risk_level)
        for s in c.scores:
            seen_categories[s.category] = s
        for sig in c.signals_used:
            if sig not in seen_signals:
                signals.append(sig)
                seen_signals.add(sig)

    # Drop the synthetic "clean" entry when any other category fired —
    # "clean + brand_impersonation" is incoherent.
    if len(seen_categories) > 1 and "clean" in seen_categories:
        seen_categories.pop("clean")

    return RiskClassification(
        risk_level=aggregate,
        scores=tuple(seen_categories.values())[:MAX_REASONS],
        model=last_model,
        signals_used=tuple(signals),
        prefilter_only=any(c.prefilter_only for c in classifications),
    )


def assert_clone_spec_safe(
    spec: CloneSpec,
    *,
    classification: Optional[RiskClassification] = None,
    threshold: str = DEFAULT_REFUSAL_THRESHOLD,
) -> RiskClassification:
    """Raise :class:`ContentRiskError` when the classification's
    ``risk_level`` meets or exceeds ``threshold``.

    When ``classification`` is ``None`` the caller must have already
    invoked :func:`classify_clone_spec` and persisted the result — this
    sync helper deliberately does NOT trigger an LLM call (it would
    couple a sync surface to an async I/O dependency). Callers that
    want one-shot semantics should ``await classify_clone_spec(spec)``
    + call this on the result.

    Returns the (possibly heuristic-only) classification on success so
    the caller can persist it into the W11.7 manifest + W11.12 audit
    row in the same step.
    """
    if classification is None:
        # Heuristic-only check: the caller skipped the async path so
        # we run the cheap pure-python prefilter ourselves. Suitable
        # for the W11.8 rate-limit gate which already runs sync.
        classification = heuristic_risk_signals(spec)

    if classification.risk_level not in _RISK_LEVEL_INDEX:
        raise ContentClassifierError(
            f"classification.risk_level {classification.risk_level!r} "
            f"is not in {RISK_LEVELS}"
        )
    if threshold not in _RISK_LEVEL_INDEX:
        raise ContentClassifierError(
            f"threshold {threshold!r} is not in {RISK_LEVELS}"
        )

    if _RISK_LEVEL_INDEX[classification.risk_level] >= _RISK_LEVEL_INDEX[threshold]:
        raise ContentRiskError(classification, threshold=threshold)

    return classification


__all__ = [
    "ClassifierLLM",
    "ClassifierUnavailableError",
    "ContentClassifierError",
    "ContentRiskError",
    "DEFAULT_CLASSIFIER_MODEL",
    "DEFAULT_REFUSAL_THRESHOLD",
    "LLM_SYSTEM_PROMPT",
    "LLM_USER_PROMPT_TEMPLATE",
    "LangchainClassifierLLM",
    "MAX_PROMPT_INPUT_CHARS",
    "MAX_REASONS",
    "MAX_REASON_CHARS",
    "RISK_CATEGORIES",
    "RISK_LEVELS",
    "RiskClassification",
    "RiskScore",
    "assert_clone_spec_safe",
    "classify_clone_spec",
    "heuristic_risk_signals",
    "merge_risk_classifications",
]

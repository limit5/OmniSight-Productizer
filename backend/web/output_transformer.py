"""W11.6 #XXX — L3 Output transformation (the most critical defense layer).

Layer 3 of the W11 5-layer defense-in-depth pipeline. Runs **after** the
W11.5 L2 classifier has cleared the spec for transformation and **before**
the W11.7 manifest writer pins the artefacts. Three structural disciplines
land here, each independently necessary for the cloned output to be a
*transformation* of the source instead of a redistribution:

1. **Never copy bytes** — :func:`assert_no_copied_bytes` is an invariant
   gate the calling router runs on the input :class:`CloneSpec` *and* on
   the produced :class:`TransformedSpec`. It refuses ``data:`` URIs,
   ``base64,`` inline payloads, and any field carrying raw bytes.
   :class:`backend.web.site_cloner.RawCapture` already constrains the
   capture to URL strings (see W11.1 row docstring), so this layer is
   primarily a *defense in depth* check that future capture-backend
   changes can't silently regress the no-bytes invariant.
2. **Text LLM rewrite** — :func:`transform_clone_spec` runs the cheapest
   configured chat model (``backend.agents.llm.get_cheapest_model`` —
   Haiku 4.5 / DeepSeek / Groq cheapest-pref chain) over the spec's
   semantic surfaces (title / hero / nav labels / section summaries /
   footer text) and emits a fully rewritten copy so the produced clone is
   inspired by the source rather than a verbatim duplicate. The LLM is
   instructed to preserve intent and structure but rewrite the specific
   wording. Failure modes (no provider key, parse error, network blip)
   degrade to the deterministic ``_heuristic_rewrite`` path so the
   pipeline still produces an artefact (with a recorded warning so the
   audit row in W11.12 sees the degraded mode).
3. **Image placeholders** — :func:`apply_image_placeholders` replaces
   every ``spec.images`` entry with a deterministic placeholder record
   (a public placeholder URL such as ``https://placehold.co/...``, alt
   text inherited from the source img tag if present, and the original
   URL preserved in a ``source_url`` field for traceability). The
   transformer **never** fetches, copies, or embeds the source image
   bytes. Operators that need a self-hosted placeholder host set
   :data:`PLACEHOLDER_PROVIDER` via env knob.

Where it slots into the W11 pipeline
------------------------------------
The full router contract is::

    decision = await check_machine_refusal_pre_capture(url)        # L1
    capture  = await source.capture(url, ...)                      # W11.2
    decision = check_machine_refusal_post_capture(capture)         # L1
    spec     = build_clone_spec_from_capture(capture)              # W11.3
    classification = await classify_clone_spec(spec)               # L2
    assert_clone_spec_safe(spec, classification=classification)    # L2

    transformed = await transform_clone_spec(                      # L3 ← this row
        spec, classification=classification,
    )
    assert_no_copied_bytes(transformed)                            # L3 invariant

    write_manifest(spec, classification, transformed)              # L4 (W11.7)
    rate_limiter.consume(tenant, target)                           # L5 (W11.8)

L3 sees text + URLs only — never bytes — and produces a structured
:class:`TransformedSpec` the W11.7 manifest pins and the W11.9 framework
adapter (Next / Nuxt / Astro) consumes to scaffold the output project.

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable constants (system / user
prompts, default placeholder provider, compiled regex, placeholder
dimension table). The default :class:`LangchainTextRewriteLLM`
constructs its underlying LangChain chat model lazily via
:func:`backend.agents.llm.get_cheapest_model`, which itself owns a
per-worker cache (already audited in that module). Cross-worker
consistency: trivially answer #1 — every worker derives the same
constants + same prompt template from source. Operators that want
per-tenant rewrite quotas plug their own ``TextRewriteLLM`` in.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — every entry point is a pure function over an in-memory
:class:`CloneSpec` plus, optionally, a single round-trip to the
configured LLM. No shared writable state, no parallel-vs-serial timing
dependence.

Production Readiness Gate §158
------------------------------
No new pip dependencies (langchain + cheapest-model preference list
already in production image via :mod:`backend.agents.llm`; rest is
stdlib). No image rebuild needed.

Inspired by firecrawl/open-lovable (MIT). The full attribution + license
text live in ``LICENSES/open-lovable-mit.txt`` (W11.13).
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
from urllib.parse import quote_plus

from backend.web.content_classifier import (
    RISK_LEVELS,
    ContentRiskError,
    RiskClassification,
    _RISK_LEVEL_INDEX,
)
from backend.web.site_cloner import CloneSpec, SiteClonerError

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

#: Stable identifier emitted into ``TransformedSpec.model`` when the
#: caller doesn't pass an explicit ``llm``. Surfaced in the audit log as
#: the ``model`` field; real rewrites from the cheapest-model chain will
#: overwrite this with the picked provider's id, this string is only the
#: fall-through label when no cheapest-model lookup runs.
DEFAULT_REWRITE_MODEL: str = "cheapest-llm-chain"

#: Hard cap on the rewrite input we hand to the cheap rewrite model.
#: Same budget as the L2 classifier — 8 KiB of curated CloneSpec excerpts
#: is enough to rewrite intent without blowing the operator's per-call
#: spend. The LLM is rewriting the *outline*, not the full body copy.
MAX_REWRITE_INPUT_CHARS: int = 8_000

#: Cap on the per-section / footer / hero text the rewrite envelope is
#: allowed to return. Hard truncation is enforced post-parse so a
#: hallucinated 50-page section cannot bloat the manifest or the
#: downstream framework adapter's prompt budget.
MAX_REWRITE_TEXT_CHARS: int = 1_200

#: Cap on the number of sections / nav links / images the rewrite
#: envelope may return. Real landing pages have ≤ 50 of any of these in
#: 99 % of cases; the cap defends against LLM token-spam from a chatty
#: model.
MAX_REWRITTEN_LIST_ITEMS: int = 50

#: Default placeholder image provider. ``placehold.co`` is a free public
#: SVG-on-demand host that returns a coloured rectangle with optional
#: text — perfect for a "this image was here" placeholder. Operators
#: that need an air-gap-friendly self-hosted equivalent set
#: ``OMNISIGHT_CLONE_PLACEHOLDER_PROVIDER=https://placeholder.internal/...``
#: at deploy time and ``transform_clone_spec(placeholder_provider=...)``
#: picks it up.
PLACEHOLDER_PROVIDER: str = "https://placehold.co"

#: Default placeholder dimensions (width × height in CSS pixels) used
#: when the source image's intrinsic size is unknown — which is always,
#: because the spec records URLs + alt text only (never bytes).
DEFAULT_PLACEHOLDER_WIDTH: int = 800
DEFAULT_PLACEHOLDER_HEIGHT: int = 600

#: Text strings that, if present in any image record's URL field,
#: indicate the cloner has accidentally inlined byte data. A URL of any
#: shape is fine; ``data:image/...;base64,...`` is not. ``assert_no_copied_bytes``
#: pattern-matches these prefixes case-insensitively and raises.
_BYTES_LEAK_PREFIXES: Tuple[str, ...] = (
    "data:",
    "base64,",
)

#: Field names whose values, if present on an image record, indicate
#: byte data has been smuggled in. Future capture-backend regressions
#: that attach an ``inline_bytes`` field would be caught here even
#: before the URL pattern check fires.
_BYTES_LEAK_FIELDS: frozenset[str] = frozenset({
    "bytes", "blob", "base64", "inline", "inline_bytes", "raw", "data",
})

#: System prompt the rewrite tier runs against. Pinned as a module
#: constant so prompt drift is a code-reviewable diff. The prompt
#: instructs the model to:
#:
#:   * Preserve the spec's structural intent (heading / sections / nav /
#:     footer remain non-empty if they were non-empty in the input).
#:   * Rewrite the specific copy — paraphrase, generalise away the
#:     source brand, soften any first-person language.
#:   * Output a strict JSON envelope shaped like
#:     :class:`TransformedSpec` — :func:`_parse_rewrite_envelope`
#:     tolerates code-fence wrapping + leading prose so a chatty model
#:     doesn't break the parse.
LLM_REWRITE_SYSTEM_PROMPT: str = """\
You are a content-rewrite engine for an automated website cloner.
Given an excerpt of a target page (title, hero, nav labels, sections,
footer text) you produce a *transformed* version that preserves the
structural intent but rewrites the specific copy so the output is not a
verbatim duplicate of the source.

Hard rules:
- Strip recognisable brand names, product names, trademarks. Replace
  with generic equivalents (e.g. "Acme" → "Our Company").
- Preserve heading / section structure: an input with three sections
  becomes an output with three rewritten sections.
- Keep wording neutral and generic. Avoid first-person testimonials,
  specific pricing, dated claims.
- Never invent contact info, addresses, or PII.
- Output STRICT JSON only — no prose, no markdown fences. Shape:

{
  "title": "<rewritten title or empty string>",
  "hero": {
    "heading": "<rewritten H1 or empty>",
    "tagline": "<rewritten tagline or empty>",
    "cta_label": "<rewritten CTA label or empty>"
  },
  "nav": [{"label": "<rewritten label>"} ...],
  "sections": [
    {"heading": "<rewritten>", "summary": "<rewritten ≤ 1000 chars>"} ...
  ],
  "footer": {"text": "<rewritten footer copy or empty>"}
}

Each rewritten string MUST be ASCII-safe and ≤ 1000 chars.
"""

#: User-prompt template. ``{excerpt}`` is the only placeholder; never
#: f-string this with untrusted input — :func:`_render_rewrite_prompt`
#: does the substitution after sanitising the excerpt.
LLM_REWRITE_USER_PROMPT_TEMPLATE: str = """\
Page excerpt to rewrite (the L2 classifier cleared this spec at risk
level {risk_level}, categories: {categories}):

---
{excerpt}
---

Return the JSON envelope described in the system prompt.
"""

#: Pre-compiled fenced-code regex used by :func:`_parse_rewrite_envelope`
#: to strip ```json``` / ``` wrappers.
_FENCED_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)

#: Greedy "first balanced JSON object" extractor — used as a fallback
#: when the model emits prose around the JSON instead of (or in
#: addition to) a code fence.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Max-aggregate level (inclusive) at which the transformer still runs.
#: Above this, :func:`transform_clone_spec` raises
#: :class:`ContentRiskError` defensively (the L2 row already raises at
#: ``high`` by default; this gate is a belt-and-braces refusal in case
#: the caller forgot to invoke ``assert_clone_spec_safe`` or widened
#: their threshold without thinking through L3 implications).
MAX_TRANSFORM_RISK_LEVEL: str = "high"


# ── Errors ──────────────────────────────────────────────────────────────


class OutputTransformerError(SiteClonerError):
    """Base class for everything raised by ``output_transformer``.

    Subclass of :class:`backend.web.site_cloner.SiteClonerError` so a
    single ``except SiteClonerError`` in the calling router catches L1 /
    L2 / L3 errors uniformly; the W11.12 audit row uses ``isinstance``
    to assign the finer bucket.
    """


class BytesLeakError(OutputTransformerError):
    """Raised by :func:`assert_no_copied_bytes` when an image record (or
    any other carrier) contains raw bytes / a ``data:`` URI / a
    base64-encoded inline payload.

    The transformer treats this as a structural defect of the upstream
    capture — it indicates a regression in the W11.1 / W11.2 layers, not
    a content issue. Distinct from :class:`ContentRiskError` so the
    audit row can flag operator engineering effort, not policy review.
    """


class RewriteUnavailableError(OutputTransformerError):
    """Raised internally by :class:`LangchainTextRewriteLLM` when no
    cheapest-model entry has credentials AND ``get_cheapest_model()``
    itself returned ``None`` (token freeze / circuit breaker / no keys).

    :func:`transform_clone_spec` catches this and translates to a
    deterministic heuristic-rewrite fallback (recorded in the audit row
    via ``signals_used = ('heuristic',)`` so operators see the
    degradation).
    """


# ── Data structures ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TransformedSpec:
    """Output of :func:`transform_clone_spec`.

    Mirrors :class:`backend.web.site_cloner.CloneSpec`'s shape but with
    every text surface rewritten and every image record replaced by a
    placeholder. Frozen so downstream code (W11.7 manifest pinning,
    W11.9 framework adapter) cannot mutate after the L3 gate has run.

    Attributes:
        source_url: The validated, normalised URL the original spec was
            built from. Pinned through L3 so the W11.7 manifest can
            cite provenance.
        fetched_at: ISO-8601 UTC timestamp inherited from the original
            capture.
        backend: Capture backend identifier inherited from the original
            spec.
        title: Rewritten ``<title>`` text (or empty string if the
            original was empty).
        meta: ``{key: rewritten_value}`` for the spec's most semantic
            meta tags. Most meta entries are dropped — the transformer
            keeps ``description`` / ``og:description`` / ``og:title`` /
            ``twitter:description`` only, all rewritten. Rationale:
            ``og:url`` / ``canonical`` etc. carry source identity and
            must not survive into the clone.
        hero: ``{"heading": str, "tagline": str, "cta_label": str}`` or
            ``None`` when the source had no hero.
        nav: List of ``{"label": str}`` records (no hrefs — those are
            emitted by the framework adapter against the rewritten
            structure).
        sections: List of ``{"heading": str, "summary": str}`` records.
        footer: ``{"text": str}`` or ``None``.
        images: List of placeholder records produced by
            :func:`apply_image_placeholders`. Each is
            ``{"url": placeholder_url, "alt": str, "kind": "placeholder",
              "source_url": original_source_url}``. ``source_url`` is
            kept for traceability — the framework adapter never fetches
            it; the audit row records what was replaced.
        colors: Original colour tokens from the source — design tokens
            are not rewritten (a colour palette is functional, not
            content). Pass-through.
        fonts: Original font tokens — same rationale as ``colors``.
        spacing: Original spacing tokens — same rationale.
        warnings: Non-fatal issues encountered during transformation
            (e.g. ``"rewrite_llm_unavailable"``, ``"data_uri_dropped"``).
        signals_used: Stable identifiers of the layers that contributed
            (``"llm"`` / ``"heuristic"`` / ``"placeholder"``). Empty
            tuple is impossible — at minimum the placeholder pass runs.
        model: Identifier of the rewrite model (``"heuristic"`` when the
            LLM was unavailable). Pinned in the W11.7 manifest.
        transformations: Stable list of transformation kinds applied
            (``"text_rewrite"`` / ``"image_placeholder"`` /
            ``"bytes_strip"``). Lets the audit row enumerate exactly
            what the L3 layer changed.
    """

    source_url: str
    fetched_at: str
    backend: str

    title: str
    meta: Mapping[str, str]
    hero: Optional[Mapping[str, str]]
    nav: Tuple[Mapping[str, str], ...]
    sections: Tuple[Mapping[str, str], ...]
    footer: Optional[Mapping[str, str]]
    images: Tuple[Mapping[str, str], ...]
    colors: Tuple[str, ...]
    fonts: Tuple[str, ...]
    spacing: Mapping[str, Any]

    warnings: Tuple[str, ...]
    signals_used: Tuple[str, ...]
    model: str
    transformations: Tuple[str, ...]


# ── Backend protocol ───────────────────────────────────────────────────


@runtime_checkable
class TextRewriteLLM(Protocol):
    """Pluggable LLM backend for the L3 text rewrite tier.

    Default :class:`LangchainTextRewriteLLM` routes via
    :func:`backend.agents.llm.get_cheapest_model`. Tests / air-gap setups
    substitute a fake that returns a canned envelope without spending an
    LLM call.

    The protocol mirrors :class:`backend.web.content_classifier.ClassifierLLM`
    intentionally — single async method, one string in (the rendered
    prompt), one string out (the raw model response). Envelope parsing
    lives in :func:`_parse_rewrite_envelope` so adapters don't have to
    know the JSON shape.
    """

    name: str
    """Stable identifier emitted into ``TransformedSpec.model``."""

    async def rewrite_text(self, prompt: str, *, system: str) -> str: ...


# ── Helpers ────────────────────────────────────────────────────────────


def _normalize_text(s: object) -> str:
    if not isinstance(s, str):
        return ""
    return " ".join(s.split())


def _truncate(s: str, limit: int) -> str:
    s = s or ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _ascii_safe(s: str) -> str:
    """Strip non-ASCII control characters (NUL etc.) but preserve
    ordinary unicode (e.g. accented letters). Real-world rewritten copy
    almost always survives ASCII; this filter just catches the rare
    case of a model emitting a control byte."""
    if not s:
        return ""
    return "".join(ch for ch in s if ch == "\n" or ord(ch) >= 0x20)


def _spec_excerpt_for_rewrite(spec: CloneSpec) -> str:
    """Render the curated rewrite-input excerpt of ``spec``.

    Mirrors :func:`backend.web.content_classifier._spec_excerpt` —
    text-only surfaces only (title / meta description / hero / nav /
    sections / footer). Image URLs / colour tokens / fonts / spacing
    are deliberately omitted from the LLM prompt so the rewrite tier
    cannot accidentally smuggle bytes into the output (defense-in-depth
    of the W11.6 "never copy bytes" invariant).
    """
    lines: list[str] = []

    if spec.title:
        lines.append(f"TITLE: {_normalize_text(spec.title)}")

    meta = spec.meta or {}
    for key in ("description", "og:description", "twitter:description"):
        val = meta.get(key)
        if val:
            lines.append(f"META {key}: {_normalize_text(val)[:500]}")
            break

    hero = spec.hero or {}
    hero_bits: list[str] = []
    if isinstance(hero, dict):
        if hero.get("heading"):
            hero_bits.append(f"HERO_H1: {_normalize_text(str(hero['heading']))}")
        if hero.get("tagline"):
            hero_bits.append(f"HERO_TAGLINE: {_normalize_text(str(hero['tagline']))}")
        cta = hero.get("cta")
        if isinstance(cta, dict) and cta.get("label"):
            hero_bits.append(f"HERO_CTA: {_normalize_text(str(cta['label']))}")
    lines.extend(hero_bits)

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
            lines.append(f"SECTION[{idx}] {heading[:120]}: {summary[:400]}")

    footer = spec.footer or {}
    if isinstance(footer, dict):
        ftext = _normalize_text(str(footer.get("text", "")))
        if ftext:
            lines.append(f"FOOTER: {ftext[:400]}")
        flink_labels = [
            _normalize_text(str(item.get("label", "")))
            for item in (footer.get("links") or [])[:12]
            if isinstance(item, dict) and item.get("label")
        ]
        if flink_labels:
            lines.append("FOOTER_LINKS: " + " · ".join(flink_labels))

    excerpt = "\n".join(lines).strip()
    return _truncate(excerpt, MAX_REWRITE_INPUT_CHARS)


def _render_rewrite_prompt(
    spec: CloneSpec,
    *,
    classification: Optional[RiskClassification],
) -> str:
    """Render the LLM user prompt with the spec excerpt + classification
    context substituted in. Does no f-string interpolation on attacker
    strings (the format is positional)."""
    excerpt = _spec_excerpt_for_rewrite(spec)
    if classification is None:
        risk_level = "unknown"
        categories = "none"
    else:
        risk_level = classification.risk_level
        cat_names = [s.category for s in classification.scores] or ["clean"]
        categories = ", ".join(cat_names)
    return LLM_REWRITE_USER_PROMPT_TEMPLATE.format(
        risk_level=risk_level,
        categories=categories,
        excerpt=excerpt or "(empty excerpt)",
    )


# ── Bytes-leak invariant ───────────────────────────────────────────────


def _is_bytes_leak_url(url: object) -> bool:
    """Return ``True`` iff ``url`` is a string carrying inline byte data.

    Catches:
      * ``data:image/png;base64,...`` and any other ``data:`` URI.
      * Strings that begin with ``base64,`` (rare hand-rolled inline).
      * Non-string values (the spec contract is URL-string only;
        anything else is a structural defect).
    """
    if url is None:
        return False
    if not isinstance(url, str):
        return True  # structural defect — refuse upstream regression
    norm = url.strip().lower()
    if not norm:
        return False
    return any(norm.startswith(prefix) for prefix in _BYTES_LEAK_PREFIXES)


def assert_no_copied_bytes(
    spec_or_transformed: object,
) -> None:
    """Invariant gate: refuse if any image / asset record carries inline
    bytes.

    Accepts either a :class:`CloneSpec` (run on the *input* before L3)
    or a :class:`TransformedSpec` (run on the *output* before L4 pins
    the manifest). Both shapes have an ``images`` list/tuple of records;
    this function walks the records and checks for ``data:`` URIs,
    ``base64`` payloads, and any field listed in
    :data:`_BYTES_LEAK_FIELDS`.

    Raises:
        BytesLeakError: when a leak is detected. The message names the
            offending field / record so operators can trace which capture
            backend regressed.
        OutputTransformerError: when the input is not a CloneSpec /
            TransformedSpec instance (the caller has the wrong contract).
    """
    if not isinstance(spec_or_transformed, (CloneSpec, TransformedSpec)):
        raise OutputTransformerError(
            f"assert_no_copied_bytes expects CloneSpec or TransformedSpec, "
            f"got {type(spec_or_transformed).__name__}"
        )

    images = spec_or_transformed.images or ()
    for idx, rec in enumerate(images):
        if not isinstance(rec, Mapping):
            raise BytesLeakError(
                f"image[{idx}] is {type(rec).__name__}, expected mapping"
            )
        for forbidden in _BYTES_LEAK_FIELDS:
            if forbidden in rec:
                raise BytesLeakError(
                    f"image[{idx}] carries forbidden inline-bytes "
                    f"field {forbidden!r} — capture backend regression"
                )
        url = rec.get("url")
        if _is_bytes_leak_url(url):
            preview = (str(url) if url is not None else "")[:64]
            raise BytesLeakError(
                f"image[{idx}].url is a {url and 'data: URI' or 'non-string'} "
                f"({preview!r}) — refusing to clone inline bytes"
            )
        # Source-URL preservation is allowed but must be a string and
        # must not be a data: URI either.
        src = rec.get("source_url")
        if src is not None and _is_bytes_leak_url(src):
            preview = (str(src))[:64]
            raise BytesLeakError(
                f"image[{idx}].source_url carries inline bytes "
                f"({preview!r})"
            )


# ── Image placeholder substitution ─────────────────────────────────────


def _placeholder_url(
    *,
    provider: str,
    width: int,
    height: int,
    label: str = "",
) -> str:
    """Render a placeholder image URL.

    ``placehold.co`` accepts ``{provider}/{w}x{h}?text={label}``; other
    public providers (placekitten / dummyimage / picsum) take similar
    shapes. Operators with a self-hosted provider that doesn't follow
    this convention pass a custom :class:`TextRewriteLLM` that emits
    pre-substituted placeholder URLs in its envelope.
    """
    p = (provider or PLACEHOLDER_PROVIDER).rstrip("/")
    w = max(16, int(width))
    h = max(16, int(height))
    label_snippet = (label or "").strip()
    if label_snippet:
        return f"{p}/{w}x{h}?text={quote_plus(label_snippet[:80])}"
    return f"{p}/{w}x{h}"


def apply_image_placeholders(
    spec: CloneSpec,
    *,
    provider: str = PLACEHOLDER_PROVIDER,
    width: int = DEFAULT_PLACEHOLDER_WIDTH,
    height: int = DEFAULT_PLACEHOLDER_HEIGHT,
) -> Tuple[Mapping[str, str], ...]:
    """Replace every ``spec.images`` entry with a placeholder record.

    The returned records are immutable frozen-dict-like mappings (real
    ``dict`` values inside a tuple — Python's frozen-dict isn't part of
    the stdlib, but :class:`TransformedSpec` is frozen so the outer
    container can't be reassigned). Each record has shape::

        {
            "url":        placeholder URL (NOT the source URL),
            "alt":        rewritten alt text (the source's alt, sanitised),
            "kind":       "placeholder",
            "source_url": the original source URL (for traceability),
            "width":      "<int>",
            "height":     "<int>"
        }

    Records whose source URL would have been a ``data:`` URI are dropped
    silently (the W11.3 populator already drops them, but we belt-and-
    brace this layer too). The returned tuple length is capped at
    :data:`MAX_REWRITTEN_LIST_ITEMS`.

    Pure function. Module-global audit: no mutable state — provider /
    width / height come in via kwargs, output is freshly constructed.
    """
    if not isinstance(spec, CloneSpec):
        raise OutputTransformerError(
            f"apply_image_placeholders expects CloneSpec, got "
            f"{type(spec).__name__}"
        )

    out: list[Mapping[str, str]] = []
    for rec in (spec.images or [])[:MAX_REWRITTEN_LIST_ITEMS]:
        if not isinstance(rec, Mapping):
            continue
        src = rec.get("url", "")
        if _is_bytes_leak_url(src):
            continue
        alt_text = _ascii_safe(_normalize_text(str(rec.get("alt") or ""))) \
            or "Placeholder image"
        placeholder = _placeholder_url(
            provider=provider,
            width=width,
            height=height,
            label=alt_text,
        )
        out.append({
            "url": placeholder,
            "alt": alt_text[:200],
            "kind": "placeholder",
            "source_url": str(src) if isinstance(src, str) else "",
            "width": str(width),
            "height": str(height),
        })
    return tuple(out)


# ── LLM rewrite tier ───────────────────────────────────────────────────


class LangchainTextRewriteLLM:
    """Default :class:`TextRewriteLLM` backed by
    :func:`backend.agents.llm.get_cheapest_model`.

    Routes the rewrite call to the cheapest configured tier (Haiku 4.5 /
    DeepSeek / OpenRouter / Groq, in that preference order) so a
    flagship Opus key is never burned on a single rewrite call. Mirrors
    :class:`backend.web.content_classifier.LangchainClassifierLLM` so an
    operator that has classifier credentials wired automatically has
    rewrite credentials wired too.

    Lazy init: the underlying LangChain client is constructed on first
    ``rewrite_text`` call so importing this module never spins up an
    LLM connection.

    Raises :class:`RewriteUnavailableError` from ``rewrite_text`` when
    the cheapest-model chain returns ``None`` (token freeze / circuit
    breaker open / no keys). :func:`transform_clone_spec` catches and
    falls back to the deterministic heuristic rewrite path.
    """

    name: str = DEFAULT_REWRITE_MODEL

    def __init__(self) -> None:
        self._llm: Any = None  # lazy-initialised

    def _get_llm(self) -> Any:
        if self._llm is None:
            from backend.agents.llm import get_cheapest_model

            llm = get_cheapest_model()
            if llm is None:
                raise RewriteUnavailableError(
                    "no LLM available for output rewrite "
                    "(token freeze / no provider credentials / "
                    "all providers in cooldown)"
                )
            self._llm = llm
            picked = getattr(llm, "model_name", None) or getattr(
                llm, "model", None,
            )
            if picked:
                self.name = str(picked)
        return self._llm

    async def rewrite_text(self, prompt: str, *, system: str) -> str:
        llm = self._get_llm()
        try:
            resp = await llm.ainvoke([
                ("system", system),
                ("user", prompt),
            ])
        except Exception as exc:  # pragma: no cover - provider error
            raise RewriteUnavailableError(
                f"LLM rewrite call failed: {exc!s}"
            ) from exc
        content = getattr(resp, "content", resp)
        if isinstance(content, list):
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


def _parse_rewrite_envelope(raw: str) -> Optional[dict]:
    """Best-effort parse of the LLM's response into the rewrite envelope.

    Strategies tried in order:

        1. ``json.loads`` on the raw string.
        2. Strip ```json``` / ``` fences and retry.
        3. Greedy first-balanced-``{...}`` extraction and retry.

    Returns the parsed dict on success, ``None`` if every strategy fails.
    Never raises — :func:`transform_clone_spec` translates ``None`` into
    the heuristic fallback with warning ``"rewrite_parse_failed"``.
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


# ── Heuristic fallback rewrite ────────────────────────────────────────


_BRAND_TOKEN_PREFIXES_RE = re.compile(
    r"\b("
    r"acme|globex|initech|umbrella|stark|wayne|"
    r"google|facebook|meta|apple|amazon|microsoft|netflix|"
    r"twitter|x\.com|linkedin|instagram|tiktok|youtube"
    r")\b",
    re.IGNORECASE,
)


def _heuristic_rewrite_text(s: str, *, prefix: str = "") -> str:
    """Cheap deterministic rewrite — strip recognisable brand tokens and
    paraphrase via a fixed prefix. Used as the fallback when the LLM
    tier is unavailable so the pipeline still produces a transformed
    artefact (with a warning recorded so operators see the degradation).
    """
    if not s:
        return ""
    redacted = _BRAND_TOKEN_PREFIXES_RE.sub("Our Brand", s)
    redacted = _normalize_text(redacted)
    if prefix and redacted:
        redacted = f"{prefix}: {redacted}"
    return _truncate(_ascii_safe(redacted), MAX_REWRITE_TEXT_CHARS)


def _heuristic_rewrite_envelope(spec: CloneSpec) -> dict:
    """Build a deterministic rewrite envelope without an LLM round-trip.

    Mirrors the shape :func:`_parse_rewrite_envelope` returns so the
    downstream :func:`_envelope_to_transformed` consumer can be reused
    transparently.
    """
    title = _heuristic_rewrite_text(
        _normalize_text(spec.title or ""), prefix="Our take",
    )
    hero = spec.hero or {}
    hero_env = {
        "heading": _heuristic_rewrite_text(
            _normalize_text(str(hero.get("heading") or "")),
            prefix="A page about",
        ),
        "tagline": _heuristic_rewrite_text(
            _normalize_text(str(hero.get("tagline") or "")),
        ),
        "cta_label": _heuristic_rewrite_text(
            _normalize_text(
                str((hero.get("cta") or {}).get("label") or "")
            ) if isinstance(hero.get("cta"), dict) else "",
        ),
    }
    nav_env = [
        {"label": _heuristic_rewrite_text(
            _normalize_text(str(item.get("label", "")))
        )}
        for item in (spec.nav or [])[:MAX_REWRITTEN_LIST_ITEMS]
        if isinstance(item, dict) and item.get("label")
    ]
    sections_env = [
        {
            "heading": _heuristic_rewrite_text(
                _normalize_text(str(s.get("heading") or "")),
            ),
            "summary": _heuristic_rewrite_text(
                _normalize_text(str(s.get("summary") or "")),
            ),
        }
        for s in (spec.sections or [])[:MAX_REWRITTEN_LIST_ITEMS]
        if isinstance(s, dict)
    ]
    footer = spec.footer or {}
    footer_env = {
        "text": _heuristic_rewrite_text(
            _normalize_text(str(footer.get("text", "")))
            if isinstance(footer, dict) else "",
        ),
    }
    return {
        "title": title,
        "hero": hero_env,
        "nav": nav_env,
        "sections": sections_env,
        "footer": footer_env,
    }


# ── Envelope → TransformedSpec ────────────────────────────────────────


def _coerce_str(v: object, *, limit: int = MAX_REWRITE_TEXT_CHARS) -> str:
    if not isinstance(v, str):
        if v is None:
            return ""
        v = str(v)
    return _truncate(_ascii_safe(_normalize_text(v)), limit)


def _envelope_to_transformed(
    envelope: Mapping[str, Any],
    *,
    spec: CloneSpec,
    placeholder_provider: str,
    placeholder_width: int,
    placeholder_height: int,
    model: str,
    signals_used: Tuple[str, ...],
    transformations: Tuple[str, ...],
    warnings: Tuple[str, ...],
) -> TransformedSpec:
    """Map a parsed rewrite envelope onto :class:`TransformedSpec`.

    Lenient: missing fields default to empty strings / empty lists;
    list lengths are capped at :data:`MAX_REWRITTEN_LIST_ITEMS`; every
    string is ASCII-sanitised + truncated. The returned spec is
    immediately ``frozen=True`` because :class:`TransformedSpec`
    declares it so.
    """
    title = _coerce_str(envelope.get("title"))

    hero_env = envelope.get("hero")
    hero: Optional[Mapping[str, str]] = None
    if isinstance(hero_env, Mapping):
        h_heading = _coerce_str(hero_env.get("heading"))
        h_tagline = _coerce_str(hero_env.get("tagline"))
        h_cta = _coerce_str(hero_env.get("cta_label"))
        if h_heading or h_tagline or h_cta:
            hero = {
                "heading": h_heading,
                "tagline": h_tagline,
                "cta_label": h_cta,
            }

    nav_env = envelope.get("nav") or []
    nav_out: list[Mapping[str, str]] = []
    if isinstance(nav_env, list):
        for item in nav_env[:MAX_REWRITTEN_LIST_ITEMS]:
            if not isinstance(item, Mapping):
                continue
            label = _coerce_str(item.get("label"), limit=200)
            if label:
                nav_out.append({"label": label})

    sections_env = envelope.get("sections") or []
    sections_out: list[Mapping[str, str]] = []
    if isinstance(sections_env, list):
        for item in sections_env[:MAX_REWRITTEN_LIST_ITEMS]:
            if not isinstance(item, Mapping):
                continue
            heading = _coerce_str(item.get("heading"), limit=400)
            summary = _coerce_str(item.get("summary"))
            if heading or summary:
                sections_out.append({"heading": heading, "summary": summary})

    footer_env = envelope.get("footer")
    footer: Optional[Mapping[str, str]] = None
    if isinstance(footer_env, Mapping):
        ftext = _coerce_str(footer_env.get("text"))
        if ftext:
            footer = {"text": ftext}

    images = apply_image_placeholders(
        spec,
        provider=placeholder_provider,
        width=placeholder_width,
        height=placeholder_height,
    )

    # Carry through design tokens unchanged — colours / fonts / spacing
    # are functional, not content.
    meta_out: dict[str, str] = {}
    for key in ("description", "og:description", "og:title", "twitter:description"):
        val = (spec.meta or {}).get(key)
        if not val:
            continue
        # Prefer the rewritten title for og:title when we have one, else
        # heuristic-rewrite the source value.
        if key == "og:title" and title:
            meta_out[key] = title
        elif key in {"description", "og:description", "twitter:description"} and title:
            # If the rewrite envelope produced a tagline / hero summary,
            # surface the rewritten title here so the cloned page's meta
            # description doesn't leak the source brand.
            meta_out[key] = _heuristic_rewrite_text(_normalize_text(val))
        else:
            meta_out[key] = _heuristic_rewrite_text(_normalize_text(val))

    return TransformedSpec(
        source_url=spec.source_url,
        fetched_at=spec.fetched_at,
        backend=spec.backend,
        title=title,
        meta=meta_out,
        hero=hero,
        nav=tuple(nav_out),
        sections=tuple(sections_out),
        footer=footer,
        images=images,
        colors=tuple(spec.colors or ()),
        fonts=tuple(spec.fonts or ()),
        spacing=dict(spec.spacing or {}),
        warnings=tuple(warnings),
        signals_used=tuple(signals_used),
        model=model,
        transformations=tuple(transformations),
    )


# ── Public entry points ───────────────────────────────────────────────


async def transform_clone_spec(
    spec: CloneSpec,
    *,
    classification: Optional[RiskClassification] = None,
    llm: Optional[TextRewriteLLM] = None,
    placeholder_provider: str = PLACEHOLDER_PROVIDER,
    placeholder_width: int = DEFAULT_PLACEHOLDER_WIDTH,
    placeholder_height: int = DEFAULT_PLACEHOLDER_HEIGHT,
    fail_open: bool = False,
) -> TransformedSpec:
    """Run the full L3 transformation pipeline over ``spec``.

    Pipeline:

        1. Defensive risk gate: refuse when ``classification.risk_level``
           is above :data:`MAX_TRANSFORM_RISK_LEVEL` — even though L2
           should have raised, this is a belt-and-braces refusal.
        2. Bytes-leak invariant on the input
           (:func:`assert_no_copied_bytes`). Catches a regression in the
           W11.1 / W11.2 capture layers before we burn an LLM call.
        3. LLM text rewrite via ``llm`` (default
           :class:`LangchainTextRewriteLLM`). On
           :class:`RewriteUnavailableError` or parse failure: fall back
           to the deterministic heuristic rewrite path.
        4. Image placeholder substitution
           (:func:`apply_image_placeholders`).
        5. Bytes-leak invariant on the output (same gate, same error).

    Failure modes (all surface as a returned ``TransformedSpec`` —
    failures never raise except the structural gates):

        * LLM unavailable → ``warnings`` records ``"rewrite_llm_unavailable"``,
          ``signals_used`` reads ``("heuristic", "image_placeholder")``,
          ``model`` is ``"heuristic"``.
        * LLM parse failure → ``warnings`` records ``"rewrite_parse_failed"``,
          same fallback.
        * Empty spec (no title / hero / sections / footer) → still
          produces a TransformedSpec with empty fields and a
          ``"empty_spec"`` warning.

    Args:
        spec: Populated :class:`CloneSpec` from W11.3.
        classification: Optional :class:`RiskClassification` from W11.5.
            When provided, the prompt template includes the risk level
            + categories so the LLM can write more cautiously for
            ``medium`` content. The defensive gate refuses above
            :data:`MAX_TRANSFORM_RISK_LEVEL`.
        llm: Optional :class:`TextRewriteLLM` override — tests pass a
            fake; air-gap deployments may pass a self-hosted backend.
            ``None`` (default) constructs a :class:`LangchainTextRewriteLLM`.
        placeholder_provider: Image-placeholder host. Defaults to
            :data:`PLACEHOLDER_PROVIDER` (``placehold.co``). Operators
            with an air-gap stack pass a self-hosted equivalent.
        placeholder_width / placeholder_height: Default placeholder
            dimensions. Real-world pages mix sizes; absent intrinsic
            dimensions in the spec, we use the same size for every
            image (the framework adapter at W11.9 handles responsive
            ``srcset`` against this).
        fail_open: When ``True``, an LLM-down state surfaces as the
            heuristic rewrite. (Even when ``False`` we currently fall
            back to heuristic — the kwarg is reserved for a future
            "refuse rather than degrade" mode that lets operators force
            zero-LLM-output strictness.)

    Raises:
        OutputTransformerError: ``spec`` is not a CloneSpec, or the
            input fails the bytes-leak invariant.
        BytesLeakError: subclass of OutputTransformerError; the produced
            output failed the post-transform bytes-leak invariant
            (almost certainly a bug in this module if it ever fires).
        ContentRiskError: classification was passed and its risk_level
            exceeds :data:`MAX_TRANSFORM_RISK_LEVEL`.

    Module-global audit: see module docstring SOP §1 — pure function
    over in-memory spec + at most one LLM round-trip.
    """
    if not isinstance(spec, CloneSpec):
        raise OutputTransformerError(
            f"spec must be CloneSpec, got {type(spec).__name__}"
        )

    if classification is not None:
        if not isinstance(classification, RiskClassification):
            raise OutputTransformerError(
                f"classification must be RiskClassification, got "
                f"{type(classification).__name__}"
            )
        # Defensive risk gate.
        if classification.risk_level not in _RISK_LEVEL_INDEX:
            raise OutputTransformerError(
                f"classification.risk_level {classification.risk_level!r} "
                f"is not in {RISK_LEVELS}"
            )
        if (
            _RISK_LEVEL_INDEX[classification.risk_level]
            > _RISK_LEVEL_INDEX[MAX_TRANSFORM_RISK_LEVEL]
        ):
            # ``critical`` reaches us → refuse defensively. L2 should
            # have raised; we belt-and-brace.
            raise ContentRiskError(
                classification, threshold=MAX_TRANSFORM_RISK_LEVEL,
            )

    # 1. Bytes-leak invariant on the input.
    assert_no_copied_bytes(spec)

    # 2. Text rewrite — LLM tier with heuristic fallback.
    backend_llm = llm if llm is not None else LangchainTextRewriteLLM()
    user_prompt = _render_rewrite_prompt(spec, classification=classification)

    warnings: list[str] = []
    signals: list[str] = []
    transformations: list[str] = ["bytes_strip"]
    envelope: Optional[dict] = None
    model: str = DEFAULT_REWRITE_MODEL

    try:
        raw = await backend_llm.rewrite_text(
            user_prompt, system=LLM_REWRITE_SYSTEM_PROMPT,
        )
        envelope = _parse_rewrite_envelope(raw)
        if envelope is None:
            warnings.append("rewrite_parse_failed")
        else:
            signals.append("llm")
            transformations.append("text_rewrite")
            model = getattr(backend_llm, "name", DEFAULT_REWRITE_MODEL)
    except RewriteUnavailableError as exc:
        warnings.append(f"rewrite_llm_unavailable: {exc!s}")

    if envelope is None:
        # Deterministic fallback.
        envelope = _heuristic_rewrite_envelope(spec)
        signals.append("heuristic")
        transformations.append("text_rewrite_heuristic")
        model = "heuristic"

    # 3. Image placeholder substitution is run inside the envelope mapper.
    transformations.append("image_placeholder")
    signals.append("image_placeholder")

    # Empty-spec warning — the rewrite is meaningless without semantic
    # surfaces. Operators running the transformer on a 404 / blank page
    # see this in the audit row.
    if not (
        spec.title
        or spec.hero
        or spec.sections
        or spec.nav
        or (spec.footer or {}).get("text")
    ):
        warnings.append("empty_spec")

    transformed = _envelope_to_transformed(
        envelope,
        spec=spec,
        placeholder_provider=placeholder_provider,
        placeholder_width=placeholder_width,
        placeholder_height=placeholder_height,
        model=model,
        signals_used=tuple(signals),
        transformations=tuple(transformations),
        warnings=tuple(warnings),
    )

    # 4. Bytes-leak invariant on the output.
    assert_no_copied_bytes(transformed)

    return transformed


__all__ = [
    "BytesLeakError",
    "DEFAULT_PLACEHOLDER_HEIGHT",
    "DEFAULT_PLACEHOLDER_WIDTH",
    "DEFAULT_REWRITE_MODEL",
    "LLM_REWRITE_SYSTEM_PROMPT",
    "LLM_REWRITE_USER_PROMPT_TEMPLATE",
    "LangchainTextRewriteLLM",
    "MAX_REWRITE_INPUT_CHARS",
    "MAX_REWRITE_TEXT_CHARS",
    "MAX_REWRITTEN_LIST_ITEMS",
    "MAX_TRANSFORM_RISK_LEVEL",
    "OutputTransformerError",
    "PLACEHOLDER_PROVIDER",
    "RewriteUnavailableError",
    "TextRewriteLLM",
    "TransformedSpec",
    "apply_image_placeholders",
    "assert_no_copied_bytes",
    "transform_clone_spec",
]

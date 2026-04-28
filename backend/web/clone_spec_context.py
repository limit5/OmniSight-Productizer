"""W11.10 #XXX — Frontend agent role-prompt clone-spec context block.

This module is the *consumer-side* hook that turns a
:class:`backend.web.output_transformer.TransformedSpec` (W11.6) plus the
optional :class:`backend.web.clone_manifest.CloneManifest` (W11.7) into a
deterministic, agent-readable context block. The block is then injected
into the frontend agent role prompt (React / Vue / Svelte / etc.) so the
specialist node can scaffold a Next / Nuxt / Astro project (W11.9 render
path) using the rewritten outline as design inspiration *without* the
LLM ever seeing source bytes, source brand names, or the original image
URLs.

Where it slots into the W11 pipeline
------------------------------------
The router contract picks up where W11.9 left off::

    transformed = await transform_clone_spec(spec, ...)        # W11.6
    manifest    = build_clone_manifest(transformed=transformed,
                                      ...)                     # W11.7
    project     = render_clone_project(transformed,
                                      framework="next",
                                      manifest=manifest)        # W11.9

    clone_spec_context = build_clone_spec_context(             # ← this row
        transformed, manifest=manifest,
    )
    state = GraphState(
        user_command="Scaffold the cloned site as Next.js",
        routed_to="frontend",
        agent_sub_type="frontend-react",
        clone_spec_context=clone_spec_context,
    )

The context block is plain text and is appended to the frontend agent's
system prompt by :func:`backend.prompt_loader.build_system_prompt` when
its ``clone_spec_context`` parameter is populated. The agent is told,
verbatim, that:

* Source identity (clone_id + manifest_hash + source URL) is fixed —
  the agent must echo these into any artefact it produces so W11.12
  audit replay can cross-reference.
* W11 invariants survive into scaffolding:

  - **Never copy bytes** — the agent must not request a network fetch of
    any image URL it sees in the source spec; it scaffolds with the
    placeholder records the L3 transformer already produced.
  - **Image placeholders only** — every ``<img>`` tag the agent emits
    points at the placeholder provider, never at the original
    ``source_url`` field (which is kept for audit traceability only).
  - **Attribution required** — open-lovable MIT credit travels with
    every scaffolded artefact.
* Rewritten title / meta / hero / nav / sections / footer are
  **design inspiration**, not copy-paste content — the agent may
  paraphrase further; it must not regress to the original wording.
* Colours / fonts / spacing are **functional design tokens** — the
  agent maps them onto the framework's design-token system (Tailwind
  config, CSS variables, etc.) instead of hand-rolling new tokens.

Module-global state audit (SOP §1)
----------------------------------
Module-level state is limited to immutable string constants
(:data:`CLONE_SPEC_CONTEXT_HEADER`, the W11-invariants block,
:data:`MAX_CLONE_SPEC_CONTEXT_CHARS` int, the per-category caps) and a
module ``logger`` (Python's logging system owns the thread-safe
singleton — SOP answer #1). Cross-worker consistency is trivially answer
#1: every worker derives the same block from the same frozen
``TransformedSpec`` + ``CloneManifest`` inputs.

Read-after-write timing audit (SOP §2)
--------------------------------------
N/A — :func:`build_clone_spec_context` is a pure function over
in-memory inputs. No shared writable state, no parallel-vs-serial timing
dependence.

Production Readiness Gate §158
------------------------------
**Stdlib only** — ``json`` / ``logging`` / ``typing`` from stdlib +
:mod:`backend.web.output_transformer` and :mod:`backend.web.clone_manifest`
internal modules. No new pip dep, no image rebuild required.

Inspired by firecrawl/open-lovable (MIT). Attribution string forwards to
the W11.13 ``LICENSES/open-lovable-mit.txt`` row.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

from backend.web.clone_manifest import (
    CloneManifest,
    OPEN_LOVABLE_ATTRIBUTION,
)
from backend.web.output_transformer import (
    TransformedSpec,
    assert_no_copied_bytes,
)
from backend.web.site_cloner import SiteClonerError

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

#: Hard cap on the total context block. The frontend agent role prompt
#: already carries the role skill (~8 KiB) + model rules (~3 KiB) + core
#: rules (~2 KiB); reserving ~4 KiB for the clone-spec context keeps the
#: assembled system prompt comfortably under the 16 KiB / ~4K-token
#: budget operators see in Claude Sonnet / Haiku context. Truncation
#: appends a ``[clone-spec context truncated]`` marker so the agent can
#: see the budget was exhausted instead of silently working from a
#: partial spec.
MAX_CLONE_SPEC_CONTEXT_CHARS: int = 4_000

#: Per-category caps on what the rendered context block enumerates.
#: ``MAX_CONTEXT_NAV_ITEMS`` mirrors the W11.6 transformer's
#: ``MAX_REWRITTEN_LIST_ITEMS=50`` budget downstream of the L3 LLM
#: rewrite — but the role-prompt context block is a *summary*, not a
#: full enumeration, so we cap tighter here.
MAX_CONTEXT_NAV_ITEMS: int = 12
MAX_CONTEXT_SECTION_ITEMS: int = 6
MAX_CONTEXT_IMAGE_ITEMS: int = 6
MAX_CONTEXT_COLOR_ITEMS: int = 12
MAX_CONTEXT_FONT_ITEMS: int = 8
MAX_CONTEXT_SECTION_SUMMARY_CHARS: int = 240

#: Stable section header. Operators reading the assembled prompt grep
#: for this exact string to confirm the block landed.
CLONE_SPEC_CONTEXT_HEADER: str = "# Clone Spec Context (W11)"

#: W11 invariants pinned into every clone-spec context block. The agent
#: is told these rules are non-negotiable and that they survive into the
#: scaffolded artefact. Pinned as a module constant so prompt drift is a
#: code-reviewable diff (mirrors the W11.6 / W11.7 prompt-drift
#: discipline).
W11_INVARIANTS_BLOCK: str = (
    "## W11 invariants (non-negotiable)\n\n"
    "1. **Never copy bytes from the source.** The L3 transformer already\n"
    "   replaced every source image with a placeholder record. You MUST\n"
    "   NOT request a network fetch of any URL listed under\n"
    "   `source_url` of an image record — that field is provenance-only,\n"
    "   pinned in the W11.7 manifest for audit replay.\n"
    "2. **Image placeholders only.** Every `<img>` / `<Image>` tag you\n"
    "   emit must point at the placeholder URL provided in the image\n"
    "   record's `url` field. Never substitute the `source_url`.\n"
    "3. **No source brand names in user-visible copy.** The L3 rewrite\n"
    "   already paraphrased the source — preserve that paraphrase.\n"
    "   Never regress to the original wording even if you can guess\n"
    "   what it was.\n"
    "4. **Attribution travels with the artefact.** The W11.7 traceability\n"
    "   comment + the `LICENSES/open-lovable-mit.txt` reference are\n"
    "   pinned by the framework adapter; your scaffolding must not\n"
    "   delete or rewrite them.\n"
    "5. **Echo the manifest fingerprint.** When you produce a project\n"
    "   README, a header comment, or a metadata file, include the\n"
    "   `clone_id` and `manifest_hash` shown above so the W11.12 audit\n"
    "   replay can cross-reference your output."
)

#: Marker appended after the block was truncated to
#: :data:`MAX_CLONE_SPEC_CONTEXT_CHARS`. Distinct from the per-category
#: ``[…N more …]`` tails so the operator can tell the difference between
#: "this category was capped" and "the whole block was capped".
TRUNCATION_MARKER: str = "\n... [clone-spec context truncated]"


# ── Errors ──────────────────────────────────────────────────────────────


class CloneSpecContextError(SiteClonerError):
    """Raised when :func:`build_clone_spec_context` is called with a
    non-:class:`TransformedSpec` / non-:class:`CloneManifest` input, or
    when the input fails the defensive bytes-leak invariant.

    Subclasses :class:`SiteClonerError` so the existing W11 router-side
    ``except SiteClonerError`` handler covers this row's failures too.
    """


# ── Helpers ─────────────────────────────────────────────────────────────


def _safe_str(value: Any, *, fallback: str = "") -> str:
    """Stringify ``value`` defensively. Returns ``fallback`` for ``None``
    and clamps to a sane upper bound so a hostile / oversized field
    can't dominate the context block.

    Strings that look like they might contain ``\\n`` or other prompt-
    structural characters are not escaped — the rendered block is
    plain markdown, not a JSON envelope, and the W11.6 transformer
    already enforced ASCII-safety on every text surface.
    """
    if value is None:
        return fallback
    text = str(value)
    if len(text) > MAX_CONTEXT_SECTION_SUMMARY_CHARS:
        text = text[: MAX_CONTEXT_SECTION_SUMMARY_CHARS - 1] + "…"
    return text


def _format_identity_block(
    transformed: TransformedSpec,
    manifest: Optional[CloneManifest],
) -> str:
    """Render the per-clone identity header.

    Pulls (clone_id, manifest_hash) from the manifest when present;
    falls back to ``"absent"`` when the caller did not pin a manifest
    yet (development / offline path). The source URL always comes from
    the :class:`TransformedSpec` so a context block is buildable even
    when the manifest write was skipped.
    """
    clone_id = manifest.clone_id if manifest else "absent"
    manifest_hash = manifest.manifest_hash if manifest else "absent"
    source_url = _safe_str(transformed.source_url, fallback="absent")
    fetched_at = _safe_str(transformed.fetched_at, fallback="absent")
    backend = _safe_str(transformed.backend, fallback="absent")
    rewrite_model = _safe_str(transformed.model, fallback="absent")
    transformations = ", ".join(transformed.transformations) or "absent"

    lines = [
        "## Source identity (W11.7 manifest fingerprint)",
        "",
        f"- clone_id: `{clone_id}`",
        f"- manifest_hash: `{manifest_hash}`",
        f"- source_url: `{source_url}`",
        f"- fetched_at: `{fetched_at}`",
        f"- capture_backend: `{backend}`",
        f"- rewrite_model: `{rewrite_model}`",
        f"- transformations: {transformations}",
        f"- attribution: {OPEN_LOVABLE_ATTRIBUTION}",
    ]
    return "\n".join(lines)


def _format_outline_block(transformed: TransformedSpec) -> str:
    """Render the rewritten title / hero / nav / sections / footer
    surfaces as design *inspiration*. Each surface degrades gracefully
    to ``(empty)`` rather than disappearing so the agent can tell
    "the source had no hero" apart from "I forgot to look at the hero".
    """
    title = _safe_str(transformed.title, fallback="(empty)")

    hero_block = "(empty)"
    hero = transformed.hero
    if hero:
        heading = _safe_str(hero.get("heading"), fallback="(empty)")
        tagline = _safe_str(hero.get("tagline"), fallback="(empty)")
        cta = _safe_str(hero.get("cta_label"), fallback="(empty)")
        hero_block = (
            f"heading: {heading}\n"
            f"  tagline: {tagline}\n"
            f"  cta_label: {cta}"
        )

    nav_lines: list[str] = []
    nav_items: Sequence[Mapping[str, str]] = transformed.nav or ()
    for entry in nav_items[:MAX_CONTEXT_NAV_ITEMS]:
        nav_lines.append(f"  - {_safe_str(entry.get('label'), fallback='(empty)')}")
    if len(nav_items) > MAX_CONTEXT_NAV_ITEMS:
        nav_lines.append(f"  - … [{len(nav_items) - MAX_CONTEXT_NAV_ITEMS} more nav items]")
    nav_block = "\n".join(nav_lines) if nav_lines else "  (empty)"

    section_lines: list[str] = []
    sections: Sequence[Mapping[str, str]] = transformed.sections or ()
    for entry in sections[:MAX_CONTEXT_SECTION_ITEMS]:
        heading = _safe_str(entry.get("heading"), fallback="(empty)")
        summary = _safe_str(entry.get("summary"), fallback="(empty)")
        section_lines.append(f"  - **{heading}** — {summary}")
    if len(sections) > MAX_CONTEXT_SECTION_ITEMS:
        section_lines.append(
            f"  - … [{len(sections) - MAX_CONTEXT_SECTION_ITEMS} more sections]"
        )
    section_block = "\n".join(section_lines) if section_lines else "  (empty)"

    footer_block = "(empty)"
    footer = transformed.footer
    if footer:
        footer_text = _safe_str(footer.get("text"), fallback="(empty)")
        footer_block = footer_text

    return (
        "## Rewritten outline (design inspiration only — already paraphrased by L3)\n\n"
        f"- title: {title}\n"
        f"- hero:\n  {hero_block}\n"
        f"- nav:\n{nav_block}\n"
        f"- sections:\n{section_block}\n"
        f"- footer: {footer_block}"
    )


def _format_design_tokens_block(transformed: TransformedSpec) -> str:
    """Render colours / fonts / spacing as design tokens for the agent
    to map onto the framework's tokens (Tailwind, CSS vars, etc.).

    Spacing is a free-form mapping so we render its keys verbatim with
    each value clipped — operators that want to feed an enriched
    spacing schema need only update the W11.6 transformer's
    pass-through rules.
    """
    colours: Sequence[str] = transformed.colors or ()
    colour_view = list(colours[:MAX_CONTEXT_COLOR_ITEMS])
    if len(colours) > MAX_CONTEXT_COLOR_ITEMS:
        colour_view.append(f"… [{len(colours) - MAX_CONTEXT_COLOR_ITEMS} more]")
    colour_block = ", ".join(colour_view) if colour_view else "(empty)"

    fonts: Sequence[str] = transformed.fonts or ()
    font_view = list(fonts[:MAX_CONTEXT_FONT_ITEMS])
    if len(fonts) > MAX_CONTEXT_FONT_ITEMS:
        font_view.append(f"… [{len(fonts) - MAX_CONTEXT_FONT_ITEMS} more]")
    font_block = ", ".join(font_view) if font_view else "(empty)"

    spacing: Mapping[str, Any] = transformed.spacing or {}
    spacing_lines: list[str] = []
    for key in sorted(spacing):
        value = spacing[key]
        spacing_lines.append(f"  - {key}: {_safe_str(value, fallback='(empty)')}")
    spacing_block = "\n".join(spacing_lines) if spacing_lines else "  (empty)"

    return (
        "## Design tokens (functional — map onto your framework's design system)\n\n"
        f"- colors: {colour_block}\n"
        f"- fonts: {font_block}\n"
        f"- spacing:\n{spacing_block}"
    )


def _format_image_block(transformed: TransformedSpec) -> str:
    """Render the placeholder image inventory.

    Each record is shown as ``url → kind=… alt='…'`` with the original
    ``source_url`` deliberately omitted from the agent-visible block —
    the agent never needs the original URL (using it would breach the
    no-copy-bytes invariant); the W11.7 manifest already pins it for
    audit replay.
    """
    images: Sequence[Mapping[str, str]] = transformed.images or ()
    if not images:
        return "## Images (placeholders)\n\n  (empty)"
    lines: list[str] = ["## Images (placeholders — never fetch the source URLs)"]
    for entry in images[:MAX_CONTEXT_IMAGE_ITEMS]:
        url = _safe_str(entry.get("url"), fallback="(empty)")
        kind = _safe_str(entry.get("kind"), fallback="placeholder")
        alt = _safe_str(entry.get("alt"), fallback="(empty)")
        lines.append(f"  - `{url}` (kind={kind}, alt='{alt}')")
    if len(images) > MAX_CONTEXT_IMAGE_ITEMS:
        lines.append(f"  - … [{len(images) - MAX_CONTEXT_IMAGE_ITEMS} more images]")
    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────


def build_clone_spec_context(
    transformed: TransformedSpec,
    *,
    manifest: Optional[CloneManifest] = None,
) -> str:
    """Return the agent-readable clone-spec context block.

    Args:
        transformed: Frozen :class:`TransformedSpec` produced by W11.6
            :func:`backend.web.output_transformer.transform_clone_spec`.
        manifest: Optional :class:`CloneManifest` pinned by W11.7
            :func:`backend.web.clone_manifest.build_clone_manifest`. When
            absent the identity block records ``absent`` for clone_id +
            manifest_hash so the agent can still scaffold but knows the
            artefact is not yet manifest-tracked (development path).

    Returns:
        A plain-text markdown block, ready to inject into a frontend
        agent role prompt. Never empty unless the inputs are pathological
        (in which case :class:`CloneSpecContextError` is raised
        instead).

    Raises:
        CloneSpecContextError: ``transformed`` is not a
            :class:`TransformedSpec`, or ``manifest`` is set but is not
            a :class:`CloneManifest`, or the defensive bytes-leak gate
            (W11.6 invariant) fires on the input.

    The block is bounded above by :data:`MAX_CLONE_SPEC_CONTEXT_CHARS`
    so even a pathological input cannot blow the system-prompt budget.
    """
    if not isinstance(transformed, TransformedSpec):
        raise CloneSpecContextError(
            f"transformed must be a TransformedSpec, got {type(transformed).__name__}"
        )
    if manifest is not None and not isinstance(manifest, CloneManifest):
        raise CloneSpecContextError(
            f"manifest must be a CloneManifest or None, got {type(manifest).__name__}"
        )

    # Defense in depth: even though W11.6 is supposed to have stripped
    # bytes, a future regression could let a `data:` URI through. The
    # context block must never carry bytes into the agent's working
    # memory — re-run the W11.6 invariant gate on the input and raise
    # CloneSpecContextError if it fires.
    try:
        assert_no_copied_bytes(transformed)
    except SiteClonerError as exc:
        raise CloneSpecContextError(
            f"transformed spec failed the no-copied-bytes invariant: {exc}"
        ) from exc

    sections = [
        CLONE_SPEC_CONTEXT_HEADER,
        "",
        _format_identity_block(transformed, manifest),
        "",
        W11_INVARIANTS_BLOCK,
        "",
        _format_outline_block(transformed),
        "",
        _format_design_tokens_block(transformed),
        "",
        _format_image_block(transformed),
    ]
    block = "\n".join(sections).rstrip()

    if len(block) > MAX_CLONE_SPEC_CONTEXT_CHARS:
        # Keep the headline + identity + invariants intact, then trim
        # the tail. Operators reading a truncated block must still be
        # able to see *which* clone the context referred to.
        budget = MAX_CLONE_SPEC_CONTEXT_CHARS - len(TRUNCATION_MARKER)
        block = block[:budget].rstrip() + TRUNCATION_MARKER

    return block


__all__ = [
    "CLONE_SPEC_CONTEXT_HEADER",
    "CloneSpecContextError",
    "MAX_CLONE_SPEC_CONTEXT_CHARS",
    "MAX_CONTEXT_COLOR_ITEMS",
    "MAX_CONTEXT_FONT_ITEMS",
    "MAX_CONTEXT_IMAGE_ITEMS",
    "MAX_CONTEXT_NAV_ITEMS",
    "MAX_CONTEXT_SECTION_ITEMS",
    "MAX_CONTEXT_SECTION_SUMMARY_CHARS",
    "TRUNCATION_MARKER",
    "W11_INVARIANTS_BLOCK",
    "build_clone_spec_context",
]

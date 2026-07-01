"""RPG character registry — the character→(brain, guild, tier) source of truth.

This is the first structural step of the "un-weld" (RPG north-star): today a JIRA
ticket carries a ``class:subscription-<brain>`` label and routing is 2-way welded
(brain × slot). A *character* is a named, reusable persona that OWNS the task and
bears the RPG progression (``agent_character_card`` keyed by the character slug),
while RESOLVING to a brain (which CLI runs + which bot pushes) and a capability
tier ceiling. Physically the work still runs on a free runner SLOT of that brain's
family (a claude character can only run on a claude slot — the CLI + push identity
are brain-bound), but the character is now the first-class owner + stats-bearer +
tier gate, instead of the bare bot account.

Design invariants:

* ``brain`` MUST be a known agent_class (``jira_dispatch._BASE_BOT_BY_CLASS``) so
  the derived ``class:<brain>`` label routes to a real runner + push identity.
* ``guild`` MUST be a known guild slug (``guild_registry.GUILDS``).
* ``max_tier`` is the character's capability ceiling (S<M<L<X) — this is what
  makes "varying-capability characters" real: a cheap brain on a low tier only
  takes small tasks; an elite brain on a high tier takes the hard ones.

The registry is ADDITIVE and back-compatible: a ticket filed with
``character:<slug>`` also carries the derived ``class:<brain>`` so the existing
class-only pickup path is unchanged; the character label only adds ownership +
the tier ceiling on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from backend.agents import jira_dispatch
from backend.agents.guild_registry import GUILDS

# Tier ordering shared with capability_registry.TIER_ORDER (S<M<L<X).
TIER_ORDER: Mapping[str, int] = {"S": 0, "M": 1, "L": 2, "X": 3}


class CharacterRegistryError(RuntimeError):
    """A character definition is malformed or unknown."""


@dataclass(frozen=True)
class CharacterDef:
    """A reusable RPG persona bound to a brain + guild + tier ceiling."""

    slug: str
    display_name: str
    brain: str          # agent_class, e.g. "subscription-claude"
    guild: str          # guild_registry slug
    max_tier: str       # capability ceiling S<M<L<X
    blurb: str = ""

    def __post_init__(self) -> None:
        if self.brain not in jira_dispatch._BASE_BOT_BY_CLASS:
            raise CharacterRegistryError(
                f"character {self.slug!r} brain {self.brain!r} is not a known "
                f"agent_class {sorted(jira_dispatch._BASE_BOT_BY_CLASS)}"
            )
        if self.guild not in GUILDS:
            raise CharacterRegistryError(
                f"character {self.slug!r} guild {self.guild!r} is not a known "
                f"guild slug"
            )
        if self.max_tier not in TIER_ORDER:
            raise CharacterRegistryError(
                f"character {self.slug!r} max_tier {self.max_tier!r} not in "
                f"{sorted(TIER_ORDER)}"
            )


# Starter roster — one persona per live brain, spanning the capability spread
# (cheap grok on tier S → elite claude on tier L). Grows as the operator assembles
# their preferred combos; a brain may host several characters over time.
CHARACTERS: dict[str, CharacterDef] = {
    c.slug: c
    for c in (
        CharacterDef(
            slug="nova", display_name="Nova", brain="subscription-claude",
            guild="backend", max_tier="L",
            blurb="Elite backend architect — deep, high-tier work.",
        ),
        CharacterDef(
            slug="pixel", display_name="Pixel", brain="subscription-codex",
            guild="frontend", max_tier="M",
            blurb="Frontend/UI specialist.",
        ),
        CharacterDef(
            slug="sage", display_name="Sage", brain="subscription-gemini",
            guild="backend", max_tier="M",
            blurb="Versatile backend generalist.",
        ),
        CharacterDef(
            slug="rex", display_name="Rex", brain="subscription-grok",
            guild="sre", max_tier="S",
            blurb="Fast, cheap tooling/ops hand — small self-contained tasks.",
        ),
    )
}


def resolve_character(slug: str) -> CharacterDef:
    """Return the CharacterDef for ``slug`` or raise CharacterRegistryError."""
    try:
        return CHARACTERS[slug]
    except KeyError:
        raise CharacterRegistryError(
            f"unknown character {slug!r}; known: {sorted(CHARACTERS)}"
        ) from None


def character_brain(slug: str) -> str:
    """Return the agent_class (brain) a character resolves to."""
    return resolve_character(slug).brain


def character_from_labels(labels) -> CharacterDef | None:
    """Extract the ``character:<slug>`` label → CharacterDef, or None.

    Unknown slugs return None (fail-open — routing falls back to the raw
    ``class:`` label) rather than raising, so a typo can never wedge pickup.
    """
    for label in labels or ():
        if isinstance(label, str) and label.startswith("character:"):
            slug = label.split(":", 1)[1].strip()
            if slug in CHARACTERS:
                return CHARACTERS[slug]
            return None
    return None


def tier_within_ceiling(tier: str, max_tier: str) -> bool:
    """True if ``tier`` is at or below the character's ``max_tier`` ceiling."""
    t, m = TIER_ORDER.get(tier), TIER_ORDER.get(max_tier)
    if t is None or m is None:
        return False
    return t <= m


__all__ = [
    "CharacterDef",
    "CharacterRegistryError",
    "CHARACTERS",
    "TIER_ORDER",
    "resolve_character",
    "character_brain",
    "character_from_labels",
    "tier_within_ceiling",
]

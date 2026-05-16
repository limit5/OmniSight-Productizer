"""RPG.W9.4 -- Guild Hall roster view-model helpers.

The Guild Hall UI is a downstream consumer of this module. Keep this layer
small and immutable: it turns existing character-card rows plus the RPG Guild
registry into a stable roster payload, including the empty-Guild placeholder
and the ``Recruit`` CTA metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.agents.character_card import (
    CharacterCard,
    CharacterCardRosterEntry,
    CharacterCardSort,
)
from backend.agents.guild_registry import GuildDefinition, list_guild_definitions
from backend.sandbox_tier import Guild


EMPTY_GUILD_PLACEHOLDER = "No agents assigned to this Guild yet."
RECRUIT_CTA_LABEL = "Recruit"
RECRUIT_CTA_ACTION = "recruit"


@dataclass(frozen=True)
class GuildHallRecruitCta:
    """CTA shown when a Guild roster is empty."""

    label: str
    action: str
    guild: str


@dataclass(frozen=True)
class GuildHallMember:
    """Compact roster member for one character card."""

    agent_id: str
    agent_class: str
    instance_suffix: str
    level: int
    xp: int
    specialization_label: str


@dataclass(frozen=True)
class GuildHallGuild:
    """One Guild tile/roster section for the Guild Hall."""

    guild: str
    display_name: str
    summary: str
    members: tuple[GuildHallMember, ...]
    empty_placeholder: str | None
    recruit_cta: GuildHallRecruitCta | None

    @property
    def member_count(self) -> int:
        """Return the number of members assigned to this Guild."""

        return len(self.members)

    @property
    def is_empty(self) -> bool:
        """Whether this Guild should render the empty-state placeholder."""

        return not self.members


@dataclass(frozen=True)
class GuildHallView:
    """Full Guild Hall payload."""

    guilds: tuple[GuildHallGuild, ...]

    @property
    def total_member_count(self) -> int:
        """Return total character cards represented in the Guild Hall."""

        return sum(guild.member_count for guild in self.guilds)


def build_guild_hall_view(
    cards: Iterable[CharacterCard | CharacterCardRosterEntry],
    *,
    definitions: Iterable[GuildDefinition] | None = None,
    sort_by: CharacterCardSort = "level",
) -> GuildHallView:
    """Build a stable Guild Hall payload from character cards.

    Unknown card ``guild`` values are ignored here; the Guild registry is the
    display authority and must not grow implicitly from stale card data.
    """

    ordered_definitions = _definitions(definitions)
    buckets: dict[Guild, list[CharacterCardRosterEntry]] = {
        definition.guild: [] for definition in ordered_definitions
    }

    for listing in cards:
        entry = _roster_entry(listing)
        card = entry.card
        guild = _coerce_guild(card.guild)
        if guild is None or guild not in buckets:
            continue
        buckets[guild].append(entry)

    guild_rows = tuple(
        _build_guild_row(
            definition,
            buckets[definition.guild],
            sort_by=sort_by,
        )
        for definition in ordered_definitions
    )
    return GuildHallView(guilds=guild_rows)


def _definitions(
    definitions: Iterable[GuildDefinition] | None,
) -> tuple[GuildDefinition, ...]:
    if definitions is None:
        return list_guild_definitions()
    return tuple(definitions)


def _build_guild_row(
    definition: GuildDefinition,
    entries: list[CharacterCardRosterEntry],
    *,
    sort_by: CharacterCardSort,
) -> GuildHallGuild:
    members = tuple(
        _member_from_card(entry.card) for entry in _sort_entries(entries, sort_by)
    )
    return GuildHallGuild(
        guild=definition.guild.value,
        display_name=definition.display_name,
        summary=definition.summary,
        members=members,
        empty_placeholder=None if members else EMPTY_GUILD_PLACEHOLDER,
        recruit_cta=None if members else _recruit_cta(definition.guild),
    )


def _member_from_card(card: CharacterCard) -> GuildHallMember:
    return GuildHallMember(
        agent_id=card.agent_id,
        agent_class=card.agent_class,
        instance_suffix=card.instance_suffix,
        level=card.level,
        xp=card.xp,
        specialization_label=card.specialization_label,
    )


def _roster_entry(
    listing: CharacterCard | CharacterCardRosterEntry,
) -> CharacterCardRosterEntry:
    if isinstance(listing, CharacterCardRosterEntry):
        return listing
    return CharacterCardRosterEntry(card=listing, last_activity_at=listing.created_at)


def _sort_entries(
    entries: Iterable[CharacterCardRosterEntry],
    sort_by: CharacterCardSort,
) -> tuple[CharacterCardRosterEntry, ...]:
    if sort_by == "level":
        def key(entry: CharacterCardRosterEntry) -> tuple[int, int, str, str, str]:
            return (
                -entry.card.level,
                -entry.card.xp,
                entry.card.agent_class,
                entry.card.instance_suffix,
                entry.card.agent_id,
            )
    elif sort_by == "xp":
        def key(entry: CharacterCardRosterEntry) -> tuple[int, int, str, str, str]:
            return (
                -entry.card.xp,
                -entry.card.level,
                entry.card.agent_class,
                entry.card.instance_suffix,
                entry.card.agent_id,
            )
    elif sort_by == "activity":
        def key(entry: CharacterCardRosterEntry) -> tuple[float, str, str, str]:
            return (
                -entry.last_activity_at.timestamp(),
                entry.card.agent_class,
                entry.card.instance_suffix,
                entry.card.agent_id,
            )
    else:
        raise ValueError("sort_by must be one of: level, xp, activity")
    return tuple(sorted(entries, key=key))


def _recruit_cta(guild: Guild) -> GuildHallRecruitCta:
    return GuildHallRecruitCta(
        label=RECRUIT_CTA_LABEL,
        action=RECRUIT_CTA_ACTION,
        guild=guild.value,
    )


def _coerce_guild(value: str) -> Guild | None:
    try:
        return Guild(value.strip())
    except ValueError:
        return None


__all__ = [
    "EMPTY_GUILD_PLACEHOLDER",
    "RECRUIT_CTA_ACTION",
    "RECRUIT_CTA_LABEL",
    "GuildHallGuild",
    "GuildHallMember",
    "GuildHallRecruitCta",
    "GuildHallView",
    "build_guild_hall_view",
]

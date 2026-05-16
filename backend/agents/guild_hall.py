"""RPG.W9.4 -- Guild Hall roster view-model helpers.

The Guild Hall UI is a downstream consumer of this module. Keep this layer
small and immutable: it turns existing character-card rows plus the RPG Guild
registry into a stable roster payload, including the empty-Guild placeholder
and the ``Recruit`` CTA metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.agents.character_card import CharacterCard
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
    spec: str
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
    cards: Iterable[CharacterCard],
    *,
    definitions: Iterable[GuildDefinition] | None = None,
) -> GuildHallView:
    """Build a stable Guild Hall payload from character cards.

    Unknown card ``guild`` values are ignored here; the Guild registry is the
    display authority and must not grow implicitly from stale card data.
    """

    ordered_definitions = _definitions(definitions)
    buckets: dict[Guild, list[CharacterCard]] = {
        definition.guild: [] for definition in ordered_definitions
    }

    for card in cards:
        guild = _coerce_guild(card.guild)
        if guild is None or guild not in buckets:
            continue
        buckets[guild].append(card)

    guild_rows = tuple(
        _build_guild_row(definition, buckets[definition.guild])
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
    cards: list[CharacterCard],
) -> GuildHallGuild:
    members = tuple(_member_from_card(card) for card in _sort_cards(cards))
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
        spec=card.specialization_label,
        specialization_label=card.specialization_label,
    )


def _sort_cards(cards: Iterable[CharacterCard]) -> tuple[CharacterCard, ...]:
    return tuple(
        sorted(
            cards,
            key=lambda card: (
                -card.level,
                -card.xp,
                card.agent_class,
                card.instance_suffix,
                card.agent_id,
            ),
        )
    )


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

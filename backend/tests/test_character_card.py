"""RPG.W1.4 -- contract tests for ``backend/agents/character_card.py``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.character_card import (
    CharacterCard,
    CharacterCardAlreadyExistsError,
    CharacterCardCreate,
    CharacterCardGuildDriftError,
    CharacterCardNotFoundError,
    CharacterCardRegistry,
    CharacterCardUpdate,
    FirstTaskCharacterCard,
    InMemoryCharacterCardStore,
    assert_character_card_guilds_within_registry,
    fetch_skill_entries,
    missing_character_card_guilds_from_registry,
)


T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _create(
    agent_id: str = "api-anthropic-alpha",
    *,
    agent_class: str = "api-anthropic",
    guild: str = "backend",
    instance_suffix: str = "alpha",
    level: int = 1,
    xp: int = 0,
    specialization_label: str = "",
    style_fingerprint: str = "",
    created_at: datetime = T0,
) -> CharacterCardCreate:
    return CharacterCardCreate(
        agent_id=agent_id,
        agent_class=agent_class,
        guild=guild,
        instance_suffix=instance_suffix,
        level=level,
        xp=xp,
        specialization_label=specialization_label,
        style_fingerprint=style_fingerprint,
        created_at=created_at,
    )


async def test_create_card_applies_defaults_and_normalizes_text():
    store = InMemoryCharacterCardStore()
    card = await store.create_card(
        CharacterCardCreate(
            agent_id="  api-anthropic-alpha  ",
            agent_class="  api-anthropic  ",
            specialization_label="  schema-first  ",
            style_fingerprint="  fp-1  ",
            created_at=T0.replace(tzinfo=None),
        )
    )

    assert card.agent_id == "api-anthropic-alpha"
    assert card.agent_class == "api-anthropic"
    assert card.instance_suffix == "alpha"
    assert card.guild == "backend"
    assert card.level == 1
    assert card.xp == 0
    assert card.specialization_label == "schema-first"
    assert card.style_fingerprint == "fp-1"
    assert card.created_at == T0


async def test_create_card_rejects_duplicate_agent_id():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create())

    with pytest.raises(CharacterCardAlreadyExistsError):
        await store.create_card(_create())


async def test_list_cards_sorts_by_level_desc_then_agent_id():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create("agent-b", level=3, xp=10, created_at=T1))
    await store.create_card(_create("agent-a", level=3, xp=20, created_at=T2))
    await store.create_card(_create("agent-c", level=5, xp=1, created_at=T0))

    entries = await store.list_cards(sort_by="level")

    assert [entry.card.agent_id for entry in entries] == ["agent-c", "agent-a", "agent-b"]


async def test_list_cards_sorts_by_xp_desc_then_agent_id():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create("agent-b", level=1, xp=50))
    await store.create_card(_create("agent-a", level=9, xp=50))
    await store.create_card(_create("agent-c", level=3, xp=100))

    entries = await store.list_cards(sort_by="xp")

    assert [entry.card.agent_id for entry in entries] == ["agent-c", "agent-a", "agent-b"]


async def test_list_cards_sorts_by_activity_desc_then_agent_id():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create("agent-b", created_at=T2))
    await store.create_card(_create("agent-a", created_at=T2))
    await store.create_card(_create("agent-c", created_at=T1))

    entries = await store.list_cards(sort_by="activity")

    assert [entry.card.agent_id for entry in entries] == ["agent-a", "agent-b", "agent-c"]


async def test_list_cards_rejects_unknown_sort():
    store = InMemoryCharacterCardStore()

    with pytest.raises(ValueError, match="sort_by must be one of"):
        await store.list_cards(sort_by="created")  # type: ignore[arg-type]


async def test_list_cards_filters_by_guild():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create("agent-be", guild="backend"))
    await store.create_card(_create("agent-fe", guild="frontend"))
    await store.create_card(_create("agent-be2", guild="backend"))

    entries = await store.list_cards(guild="backend")

    assert sorted(entry.card.agent_id for entry in entries) == ["agent-be", "agent-be2"]
    assert all(entry.card.guild == "backend" for entry in entries)


async def test_update_card_changes_only_supplied_fields_and_strips_text():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create(level=2, xp=10, specialization_label="old"))

    updated = await store.update_card(
        "api-anthropic-alpha",
        CharacterCardUpdate(
            xp=25,
            specialization_label="  schema-first  ",
            style_fingerprint="  fp-2  ",
        ),
    )

    assert updated.level == 2
    assert updated.xp == 25
    assert updated.specialization_label == "schema-first"
    assert updated.style_fingerprint == "fp-2"


async def test_update_card_rejects_missing_agent():
    store = InMemoryCharacterCardStore()

    with pytest.raises(CharacterCardNotFoundError):
        await store.update_card("missing-agent", CharacterCardUpdate(level=2))


async def test_update_card_rejects_invalid_level_and_xp():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create())

    with pytest.raises(ValueError, match="level must be >= 1"):
        await store.update_card("api-anthropic-alpha", CharacterCardUpdate(level=0))
    with pytest.raises(ValueError, match="xp must be >= 0"):
        await store.update_card("api-anthropic-alpha", CharacterCardUpdate(xp=-1))


async def test_update_card_with_empty_patch_returns_card_unchanged():
    store = InMemoryCharacterCardStore()
    original = await store.create_card(_create(level=4, xp=180, specialization_label="foo"))

    updated = await store.update_card("api-anthropic-alpha", CharacterCardUpdate())

    assert updated == original


async def test_delete_card_reports_whether_card_existed():
    store = InMemoryCharacterCardStore()
    await store.create_card(_create())

    assert await store.delete_card("api-anthropic-alpha") is True
    assert await store.delete_card("api-anthropic-alpha") is False


async def test_ensure_card_for_first_task_creates_default_card():
    store = InMemoryCharacterCardStore()

    card = await store.ensure_card_for_first_task(
        FirstTaskCharacterCard(
            agent_id="api-anthropic-alpha",
            agent_class="api-anthropic",
            task_area="BACKEND",
            specialization_label="schema-first",
        )
    )

    assert card.agent_id == "api-anthropic-alpha"
    assert card.guild == "backend"
    assert card.level == 1
    assert card.xp == 0
    assert card.specialization_label == "schema-first"


async def test_ensure_card_for_first_task_returns_existing_card_without_overwrite():
    store = InMemoryCharacterCardStore()
    existing = await store.create_card(
        _create(level=8, xp=900, specialization_label="performance-first")
    )

    card = await store.ensure_card_for_first_task(
        FirstTaskCharacterCard(
            agent_id="api-anthropic-alpha",
            agent_class="different-class",
            specialization_label="do-not-overwrite",
        )
    )

    assert card is existing
    assert card.agent_class == "api-anthropic"
    assert card.level == 8
    assert card.specialization_label == "performance-first"


async def test_registry_get_card_honors_require_exists_flag():
    registry = CharacterCardRegistry(InMemoryCharacterCardStore())

    assert await registry.get_card("missing-agent", require_exists=False) is None
    with pytest.raises(CharacterCardNotFoundError):
        await registry.get_card("missing-agent")


@dataclass(frozen=True)
class _SkillState:
    skill_id: str
    level: int
    xp: int
    branch_choice: str | None
    last_active_at: datetime


class _SkillStore:
    def __init__(self, rows: tuple[_SkillState, ...]) -> None:
        self.rows = rows
        self.agent_id: str | None = None

    async def list_states(self, agent_id: str) -> tuple[_SkillState, ...]:
        self.agent_id = agent_id
        return self.rows


async def test_fetch_skill_entries_marks_branch_choice_required_at_level_three():
    store = _SkillStore(
        (
            _SkillState(
                skill_id="enterprise_web",
                level=3,
                xp=100,
                branch_choice=None,
                last_active_at=T0,
            ),
        )
    )

    entries = await fetch_skill_entries(store, "api-anthropic-alpha")

    assert store.agent_id == "api-anthropic-alpha"
    assert len(entries) == 1
    assert entries[0].skill_id == "enterprise_web"
    assert entries[0].next_level_xp == 250
    assert entries[0].branch_choice_required is True


async def test_fetch_skill_entries_does_not_flag_branch_choice_below_threshold():
    store = _SkillStore(
        (
            _SkillState(
                skill_id="enterprise_web",
                level=2,
                xp=30,
                branch_choice=None,
                last_active_at=T0,
            ),
        )
    )

    entries = await fetch_skill_entries(store, "api-anthropic-alpha")

    assert len(entries) == 1
    assert entries[0].branch_choice_required is False


def test_character_card_validates_required_fields_and_numeric_bounds():
    with pytest.raises(ValueError, match="agent_id is required"):
        CharacterCard(
            agent_id=" ",
            agent_class="api-anthropic",
            instance_suffix="alpha",
            guild="backend",
            level=1,
            xp=0,
            specialization_label="",
            style_fingerprint="",
            created_at=T0,
        )
    with pytest.raises(ValueError, match="level must be >= 1"):
        _create(level=0).to_card()
    with pytest.raises(ValueError, match="xp must be >= 0"):
        _create(xp=-1).to_card()


def test_character_card_guild_drift_helpers_report_unknown_guilds():
    assert missing_character_card_guilds_from_registry(("backend", "unknown")) == (
        "unknown",
    )

    with pytest.raises(CharacterCardGuildDriftError):
        assert_character_card_guilds_within_registry(("backend", "unknown"))


def test_character_card_rejects_unknown_guild():
    with pytest.raises(ValueError, match="unknown guild"):
        _create(guild="not_a_real_guild").to_card()

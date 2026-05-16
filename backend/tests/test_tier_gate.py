"""RPG.W7.2/W7.3 contract tests for ``backend.agents.tier_gate``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents.character_card import (
    CharacterCardCreate,
    InMemoryCharacterCardStore,
)
from backend.agents.skill_leveling import (
    InMemorySkillStateStore,
    SkillState,
)
from backend.agents.tier_gate import (
    ABSENT_SKILL_LEVEL,
    BP_C_SIZE_MIN_AGENT_LEVEL,
    TIER_X_MIN_AGENT_LEVEL,
    TIER_X_MIN_SKILL_LEVEL,
    TierGateViolation,
    assert_eligible_for_tier,
    evaluate_tier_gate,
    is_eligible_for_tier,
    tier_gate_unmet_reasons,
)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SKILL_ID = "enterprise_web"


def test_bp_c_size_minimum_level_table_is_closed_to_s_m_xl() -> None:
    assert dict(BP_C_SIZE_MIN_AGENT_LEVEL) == {
        "S": 1,
        "M": 10,
        "XL": 30,
    }


def test_bp_c_m_size_requires_first_talent_milestone() -> None:
    assert is_eligible_for_tier(tier="M", agent_level=10, skill_level=0) is True
    assert is_eligible_for_tier(tier="M", agent_level=9, skill_level=5) is False
    assert tier_gate_unmet_reasons(tier="M", agent_level=9, skill_level=5) == (
        "tier_m_agent_level_below_10:9",
    )


def test_bp_c_xl_size_requires_second_talent_milestone_without_skill_floor() -> None:
    assert is_eligible_for_tier(tier="XL", agent_level=30, skill_level=0) is True
    assert tier_gate_unmet_reasons(tier="XL", agent_level=29, skill_level=5) == (
        "tier_xl_agent_level_below_30:29",
    )


def test_tier_x_keeps_agent_and_skill_floor() -> None:
    assert is_eligible_for_tier(
        tier="X",
        agent_level=TIER_X_MIN_AGENT_LEVEL,
        skill_level=TIER_X_MIN_SKILL_LEVEL,
    )
    assert tier_gate_unmet_reasons(tier="X", agent_level=49, skill_level=2) == (
        "tier_x_agent_level_below_50:49",
        "tier_x_skill_level_below_3:2",
    )


def test_assert_eligible_for_tier_raises_structured_reasons() -> None:
    with pytest.raises(TierGateViolation) as exc_info:
        assert_eligible_for_tier(
            tier="XL",
            agent_level=29,
            skill_level=0,
            agent_id="agent-A",
            skill_id=SKILL_ID,
        )

    assert exc_info.value.unmet_reasons == ("tier_xl_agent_level_below_30:29",)


@pytest.mark.asyncio
async def test_evaluate_bp_c_size_gate_reads_character_card_level_only() -> None:
    card_store = InMemoryCharacterCardStore()
    skill_store = InMemorySkillStateStore()
    await card_store.create_card(
        CharacterCardCreate(agent_id="agent-A", agent_class="api-anthropic", level=9)
    )

    decision = await evaluate_tier_gate(
        card_store,
        skill_store,
        agent_id="agent-A",
        tier="M",
        skill_id=None,
    )

    assert decision.eligible is False
    assert decision.agent_level == 9
    assert decision.skill_level == ABSENT_SKILL_LEVEL
    assert decision.unmet_reasons == ("tier_m_agent_level_below_10:9",)


@pytest.mark.asyncio
async def test_evaluate_bp_c_size_gate_accepts_xl_without_skill_state() -> None:
    card_store = InMemoryCharacterCardStore()
    skill_store = InMemorySkillStateStore()
    await card_store.create_card(
        CharacterCardCreate(agent_id="agent-A", agent_class="api-anthropic", level=30)
    )

    decision = await evaluate_tier_gate(
        card_store,
        skill_store,
        agent_id="agent-A",
        tier="XL",
        skill_id=None,
    )

    assert decision.eligible is True
    assert decision.agent_level == 30
    assert decision.skill_level == ABSENT_SKILL_LEVEL
    assert decision.unmet_reasons == ()


@pytest.mark.asyncio
async def test_evaluate_tier_x_still_reads_skill_state() -> None:
    card_store = InMemoryCharacterCardStore()
    skill_store = InMemorySkillStateStore()
    await card_store.create_card(
        CharacterCardCreate(
            agent_id="agent-A",
            agent_class="api-anthropic",
            level=TIER_X_MIN_AGENT_LEVEL,
        )
    )
    await skill_store.upsert_state(
        SkillState(
            agent_id="agent-A",
            skill_id=SKILL_ID,
            level=TIER_X_MIN_SKILL_LEVEL,
            xp=100,
            branch_choice="perf_tuning",
            last_active_at=T0,
        )
    )

    decision = await evaluate_tier_gate(
        card_store,
        skill_store,
        agent_id="agent-A",
        tier="X",
        skill_id=SKILL_ID,
    )

    assert decision.eligible is True
    assert decision.agent_level == TIER_X_MIN_AGENT_LEVEL
    assert decision.skill_level == TIER_X_MIN_SKILL_LEVEL
    assert decision.unmet_reasons == ()


@pytest.mark.asyncio
async def test_unknown_non_x_tier_still_short_circuits_for_legacy_callers() -> None:
    class ExplodingCardStore:
        async def get_card(self, agent_id: str):  # pragma: no cover - guard
            raise AssertionError("unknown tier should not read card store")

    decision = await evaluate_tier_gate(
        ExplodingCardStore(),
        InMemorySkillStateStore(),
        agent_id="agent-A",
        tier="L",
        skill_id=SKILL_ID,
    )

    assert decision.eligible is True
    assert decision.agent_level == 0
    assert decision.skill_level == 0

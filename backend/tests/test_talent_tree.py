"""RPG.W14 -- contract tests for ``backend/agents/talent_tree.py``.

Covers the 9 AC cases listed on OP-219 §"Test plan":

1. Lock happy at each milestone (10/30/50/80).
2. Refuse-when-not-reached (MilestoneNotReached).
3. Idempotent re-lock same value.
4. Refuse re-write different value (TalentAlreadyLocked).
5. Capstone gate (CapstoneRequiresLv80 — both Lv and Lv-80 talent pick).
6. Routing-weight injection (matched label + +20% multiplier).
7. Prompt enrichment present (talent reminder injected into system prompt).
8. Drift guard (TalentIdNotInTree vs YAML).
9. MP routing_policy unreachable degrades silently to multiplier 1.0.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from backend.agents.prompt_builder import (
    TALENT_REMINDER_HEADER,
    enrich_system_prompt_with_talents,
)
from backend.agents.talent_tree import (
    CAPSTONE_LEVEL,
    CapstoneRequiresLv80,
    InMemoryCapstoneStore,
    InMemoryTalentChoiceStore,
    MILESTONE_LEVELS,
    MilestoneNotReached,
    PostgresTalentChoiceStore,
    ROUTING_WEIGHT_TALENT_MATCH,
    TalentAlreadyLocked,
    TalentChoice,
    TalentIdNotInTree,
    TalentTreeError,
    agent_talent_summary,
    available_talents,
    capstone_for_guild,
    load_talent_tree,
    lock_capstone_ability,
    lock_talent,
    prompt_reminders_for_talents,
    routing_weight_multiplier_for_talents,
)
from backend.sandbox_tier import Guild


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeAsyncpgContext:
    def __init__(self, conn: "_FakeTalentChoiceConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeTalentChoiceConn":
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeTalentChoiceConn:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict[str, object]] = {}
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        self.sql.append(sql)
        if "INSERT INTO agent_talent_choice" in sql:
            agent_id, milestone_level, talent_id, chosen_at = args
            key = (str(agent_id), int(milestone_level))
            if key in self.rows:
                return None
            self.rows[key] = {
                "agent_id": agent_id,
                "milestone_level": milestone_level,
                "talent_id": talent_id,
                "chosen_at": chosen_at,
            }
            return self.rows[key]
        if "FROM agent_talent_choice" in sql:
            agent_id, milestone_level = args
            return self.rows.get((str(agent_id), int(milestone_level)))
        raise AssertionError(f"unexpected SQL: {sql}")


# ── YAML loader + shape contract ────────────────────────────────────


def test_load_talent_tree_yaml_has_backend_and_frontend_guilds():
    tree = load_talent_tree()
    assert Guild.backend in tree
    assert Guild.frontend in tree


def test_each_guild_declares_exactly_four_milestones_with_three_options():
    tree = load_talent_tree()
    for guild_tree in tree.values():
        assert set(guild_tree.options_by_milestone.keys()) == set(MILESTONE_LEVELS)
        for milestone, options in guild_tree.options_by_milestone.items():
            assert len(options) == 3, (
                f"{guild_tree.guild.value} Lv {milestone} must have 3 options"
            )
        assert guild_tree.capstone.ability_id


def test_loaded_tree_top_level_mapping_is_immutable():
    tree = load_talent_tree()
    with pytest.raises(TypeError):
        tree[Guild.backend] = tree[Guild.backend]  # type: ignore[index]


def test_loaded_tree_milestone_mapping_is_immutable():
    tree = load_talent_tree()
    with pytest.raises(TypeError):
        tree[Guild.backend].options_by_milestone[10] = ()  # type: ignore[index]


def test_loaded_talent_options_are_frozen_dataclasses():
    option = available_talents("agent-A", Guild.backend, 10)[0]
    with pytest.raises(FrozenInstanceError):
        option.talent_id = "rewritten"  # type: ignore[misc]


def test_available_talents_returns_three_options_backend_lv10():
    options = available_talents("agent-A", Guild.backend, 10)
    talent_ids = {option.talent_id for option in options}
    assert talent_ids == {"schema-first", "performance-first", "security-first"}


def test_available_talents_rejects_milestone_outside_set():
    with pytest.raises(TalentTreeError):
        available_talents("agent-A", Guild.backend, 25)


def test_available_talents_rejects_blank_agent_id():
    with pytest.raises(ValueError):
        available_talents(" ", Guild.backend, 10)


# ── lock_talent: happy path at each milestone ───────────────────────


@pytest.mark.asyncio
async def test_lock_talent_happy_path_lv10_backend():
    store = InMemoryTalentChoiceStore()
    choice = await lock_talent(
        store,
        "agent-A",
        Guild.backend,
        10,
        "schema-first",
        agent_level=12,
        now=T0,
    )
    assert choice.milestone_level == 10
    assert choice.talent_id == "schema-first"
    assert choice.chosen_at == T0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "milestone,talent_id",
    [
        (10, "schema-first"),
        (30, "distributed-systems"),
        (50, "incident-commander"),
        (80, "legacy-archaeologist"),
    ],
)
async def test_lock_talent_happy_path_at_each_milestone(milestone, talent_id):
    store = InMemoryTalentChoiceStore()
    choice = await lock_talent(
        store,
        "agent-A",
        Guild.backend,
        milestone,
        talent_id,
        agent_level=milestone,  # exactly at the gate
        now=T0,
    )
    assert choice.milestone_level == milestone
    assert choice.talent_id == talent_id


# ── lock_talent: refuse-when-not-reached ────────────────────────────


@pytest.mark.asyncio
async def test_lock_talent_refuses_when_agent_below_milestone():
    store = InMemoryTalentChoiceStore()
    with pytest.raises(MilestoneNotReached):
        await lock_talent(
            store,
            "agent-A",
            Guild.backend,
            30,
            "distributed-systems",
            agent_level=29,
            now=T0,
        )


@pytest.mark.asyncio
async def test_lock_talent_rejects_bool_agent_level():
    store = InMemoryTalentChoiceStore()
    with pytest.raises(TypeError):
        await lock_talent(
            store,
            "agent-A",
            Guild.backend,
            10,
            "schema-first",
            agent_level=True,  # type: ignore[arg-type]
            now=T0,
        )


# ── lock_talent: idempotent re-lock same value ──────────────────────


@pytest.mark.asyncio
async def test_lock_talent_idempotent_on_same_value():
    store = InMemoryTalentChoiceStore()
    first = await lock_talent(
        store, "agent-A", Guild.backend, 10, "schema-first", agent_level=10, now=T0,
    )
    second = await lock_talent(
        store, "agent-A", Guild.backend, 10, "schema-first", agent_level=10, now=T0,
    )
    assert first == second
    summary = await agent_talent_summary(store, "agent-A")
    assert len(summary.choices) == 1


@pytest.mark.asyncio
async def test_lock_talent_idempotent_relock_preserves_original_timestamp():
    store = InMemoryTalentChoiceStore()
    first = await lock_talent(
        store,
        "agent-A",
        Guild.backend,
        10,
        "schema-first",
        agent_level=10,
        now=T0,
    )
    second = await lock_talent(
        store,
        "agent-A",
        Guild.backend,
        10,
        "schema-first",
        agent_level=10,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert second == first
    assert second.chosen_at == T0


# ── lock_talent: refuse re-write different value ────────────────────


@pytest.mark.asyncio
async def test_lock_talent_refuses_different_talent_rewrite():
    store = InMemoryTalentChoiceStore()
    await lock_talent(
        store, "agent-A", Guild.backend, 10, "schema-first", agent_level=10, now=T0,
    )
    with pytest.raises(TalentAlreadyLocked):
        await lock_talent(
            store, "agent-A", Guild.backend, 10, "performance-first", agent_level=10, now=T0,
        )


@pytest.mark.asyncio
async def test_in_memory_talent_choice_store_keeps_first_row_immutable():
    store = InMemoryTalentChoiceStore()
    first = TalentChoice("agent-A", 10, "schema-first", T0)
    second = TalentChoice(
        "agent-A",
        10,
        "performance-first",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert await store.upsert_choice(first) == first
    assert await store.upsert_choice(second) == first
    assert await store.get_choice("agent-A", 10) == first


@pytest.mark.asyncio
async def test_postgres_talent_choice_store_insert_only_on_conflict():
    conn = _FakeTalentChoiceConn()
    store = PostgresTalentChoiceStore(lambda: _FakeAsyncpgContext(conn))
    first = TalentChoice("agent-A", 10, "schema-first", T0)
    second = TalentChoice(
        "agent-A",
        10,
        "performance-first",
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert await store.upsert_choice(first) == first
    assert await store.upsert_choice(second) == first

    insert_sql = "\n".join(
        sql for sql in conn.sql if "INSERT INTO agent_talent_choice" in sql
    )
    assert "ON CONFLICT (agent_id, milestone_level) DO NOTHING" in insert_sql
    assert "DO UPDATE" not in insert_sql


# ── Capstone gate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capstone_refused_below_lv80():
    talent_store = InMemoryTalentChoiceStore()
    capstone_store = InMemoryCapstoneStore()
    with pytest.raises(CapstoneRequiresLv80):
        await lock_capstone_ability(
            capstone_store,
            talent_store,
            "agent-A",
            Guild.backend,
            agent_level=79,
            now=T0,
        )


@pytest.mark.asyncio
async def test_capstone_refused_without_lv80_milestone_talent():
    talent_store = InMemoryTalentChoiceStore()
    capstone_store = InMemoryCapstoneStore()
    # Lv 80, but no Lv-80 milestone talent picked yet.
    with pytest.raises(CapstoneRequiresLv80):
        await lock_capstone_ability(
            capstone_store,
            talent_store,
            "agent-A",
            Guild.backend,
            agent_level=80,
            now=T0,
        )


@pytest.mark.asyncio
async def test_capstone_locks_after_lv80_and_final_pick():
    talent_store = InMemoryTalentChoiceStore()
    capstone_store = InMemoryCapstoneStore()
    await lock_talent(
        talent_store,
        "agent-A",
        Guild.backend,
        CAPSTONE_LEVEL,
        "legacy-archaeologist",
        agent_level=80,
        now=T0,
    )
    lock = await lock_capstone_ability(
        capstone_store, talent_store, "agent-A", Guild.backend, agent_level=80, now=T0,
    )
    assert lock.ability_id == capstone_for_guild(Guild.backend).ability_id
    assert lock.ability_id == "code_archaeologist"


@pytest.mark.asyncio
async def test_capstone_relock_preserves_original_lock_timestamp():
    talent_store = InMemoryTalentChoiceStore()
    capstone_store = InMemoryCapstoneStore()
    await lock_talent(
        talent_store,
        "agent-A",
        Guild.backend,
        CAPSTONE_LEVEL,
        "legacy-archaeologist",
        agent_level=80,
        now=T0,
    )
    first = await lock_capstone_ability(
        capstone_store,
        talent_store,
        "agent-A",
        Guild.backend,
        agent_level=80,
        now=T0,
    )
    second = await lock_capstone_ability(
        capstone_store,
        talent_store,
        "agent-A",
        Guild.backend,
        agent_level=80,
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert second == first
    assert second.locked_at == T0


# ── Routing-weight injection ────────────────────────────────────────


def test_routing_weight_multiplier_is_1_when_no_choices():
    assert (
        routing_weight_multiplier_for_talents((), task_labels=("security",))
        == 1.0
    )


def test_routing_weight_multiplier_is_1_when_no_task_labels():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    assert (
        routing_weight_multiplier_for_talents(choices, task_labels=())
        == 1.0
    )


def test_routing_weight_multiplier_applies_match():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("security",), guild=Guild.backend,
    )
    assert multiplier == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH)


def test_routing_weight_multiplier_stacks_multiple_matches():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
        TalentChoice("agent-A", 30, "data-modeling", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices,
        task_labels=("security", "data-model"),
        guild=Guild.backend,
    )
    assert multiplier == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH ** 2)


def test_routing_weight_multiplier_label_case_insensitive():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("SECURITY",), guild=Guild.backend,
    )
    assert multiplier == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH)


def test_routing_weight_multiplier_trims_task_labels():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("  security  ",), guild=Guild.backend,
    )
    assert multiplier == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH)


def test_routing_weight_multiplier_ignores_unknown_choice():
    choices = (
        TalentChoice("agent-A", 10, "fictional-talent", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("security",), guild=Guild.backend,
    )
    assert multiplier == 1.0


def test_routing_weight_multiplier_respects_guild_scope():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("security",), guild=Guild.frontend,
    )
    assert multiplier == 1.0


def test_routing_weight_multiplier_counts_each_locked_talent_once():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    multiplier = routing_weight_multiplier_for_talents(
        choices, task_labels=("security", "SECURITY"), guild=Guild.backend,
    )
    assert multiplier == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH)


# ── Prompt enrichment ───────────────────────────────────────────────


def test_prompt_enrichment_appends_reminder_block():
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    enriched = enrich_system_prompt_with_talents(
        "You are a backend agent.", choices, guild=Guild.backend,
    )
    assert TALENT_REMINDER_HEADER in enriched
    assert "OWASP" in enriched
    assert enriched.startswith("You are a backend agent.")


def test_prompt_enrichment_orders_by_milestone_ascending():
    choices = (
        TalentChoice("agent-A", 50, "incident-commander", T0),
        TalentChoice("agent-A", 10, "schema-first", T0),
    )
    reminders = prompt_reminders_for_talents(choices, guild=Guild.backend)
    assert len(reminders) == 2
    # Lv 10 reminder mentions schema; Lv 50 reminder mentions incident.
    assert "schema" in reminders[0].lower()
    assert "incident" in reminders[1].lower()


def test_prompt_enrichment_no_op_on_empty_choices():
    base = "You are a backend agent."
    assert enrich_system_prompt_with_talents(base, ()) == base


# ── Drift guard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_talent_rejects_talent_id_not_in_tree():
    store = InMemoryTalentChoiceStore()
    with pytest.raises(TalentIdNotInTree):
        await lock_talent(
            store,
            "agent-A",
            Guild.backend,
            10,
            "fictional-talent",
            agent_level=10,
            now=T0,
        )


# ── MP routing_policy unreachable degrades silently ─────────────────


def test_routing_weight_degrades_to_1_when_yaml_missing(tmp_path):
    """If the YAML disappears, the routing-weight helper must NOT crash MP.

    OP-219 §"Error catalog" — RoutingWeightInjectionFailed degrades
    silently. The call site in :mod:`backend.agents.routing_policy`
    catches the exception and returns 1.0; here we verify the exception
    is raised by the talent_tree helper so the call site has something
    to catch.
    """
    from backend.agents.talent_tree import RoutingWeightInjectionFailed

    missing = tmp_path / "does-not-exist.yaml"
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    with pytest.raises((FileNotFoundError, RoutingWeightInjectionFailed, TalentTreeError)):
        routing_weight_multiplier_for_talents(
            choices, task_labels=("security",), path=missing,
        )


def test_routing_policy_call_site_returns_1_when_feature_flag_off(monkeypatch):
    """The routing_policy wrapper short-circuits to 1.0 when the flag is off."""
    from backend.agents import routing_policy

    monkeypatch.setenv(routing_policy.TALENT_ROUTING_ENABLED_ENV, "false")
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    assert (
        routing_policy.talent_routing_weight_multiplier(
            choices, task_labels=("security",), guild=Guild.backend.value,
        )
        == 1.0
    )


def test_routing_policy_call_site_applies_multiplier_when_flag_on(monkeypatch):
    from backend.agents import routing_policy

    monkeypatch.setenv(routing_policy.TALENT_ROUTING_ENABLED_ENV, "true")
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )
    assert (
        routing_policy.talent_routing_weight_multiplier(
            choices, task_labels=("security",), guild=Guild.backend.value,
        )
        == pytest.approx(ROUTING_WEIGHT_TALENT_MATCH)
    )


def test_routing_policy_call_site_returns_1_when_talent_tree_lookup_fails(monkeypatch):
    from backend.agents import routing_policy
    from backend.agents import talent_tree

    def raise_lookup_failure(*args, **kwargs):
        raise TalentTreeError("talent_tree unavailable")

    monkeypatch.setenv(routing_policy.TALENT_ROUTING_ENABLED_ENV, "true")
    monkeypatch.setattr(
        talent_tree,
        "routing_weight_multiplier_for_talents",
        raise_lookup_failure,
    )
    choices = (
        TalentChoice("agent-A", 10, "security-first", T0),
    )

    assert (
        routing_policy.talent_routing_weight_multiplier(
            choices, task_labels=("security",), guild=Guild.backend.value,
        )
        == 1.0
    )

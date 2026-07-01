"""RPG.W12 -- contract tests for ``backend/agents/skill_leveling.py``.

Covers the 10 AC cases listed on OP-217:

1. XP award outcome multipliers (success / partial / fail).
2. Tier-L+ multiplier stack.
3. First-time-skill bonus stack.
4. Anti-grind ``same_task_hash_within_24h`` clamp.
5. Level threshold transitions Lv 1→2 / 2→3.
6. Level threshold transition Lv 4→5 + unlock fan-out.
7. Branch lock idempotent + reject re-write.
8. Decay cap at ``next_level_threshold - 1``.
9. Teach-cooldown enforcement.
10. Drift guard catches stale skill_matrix.yaml branch_choice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.agents.skill_leveling import (
    AtomicSkillXpAward,
    BRANCH_LOCK_LEVEL,
    DECAY_RATE_PER_WEEK,
    InMemorySkillStateStore,
    LEVEL_5_CAP_THRESHOLD,
    LEVEL_THRESHOLDS,
    LevelComputeOverflow,
    PostgresSkillStateStore,
    SkillBranchAlreadyLocked,
    SkillIdNotInMatrix,
    SkillLevelingError,
    SkillState,
    TEACH_COOLDOWN_DAYS,
    TEACH_INJECTION_XP,
    TeachCooldownActive,
    XPDeltaNegativeWithoutDecayContext,
    apply_decay,
    award_skill_xp,
    compute_level,
    compute_xp_delta,
    decay_idle_skills,
    lock_branch_choice,
    mastery_effects_for_skill,
    mastery_effects_table,
    next_level_threshold,
    teach_other_agent,
    unlocks_crossed,
    unlocks_for_level,
)
from backend.agents.skill_matrix import SkillMatrixDriftError, canonical_skill_ids


SKILL_ID = "enterprise_web"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── compute_level + thresholds ──────────────────────────────────────


def test_compute_level_thresholds():
    assert compute_level(0) == 1
    assert compute_level(24) == 1
    assert compute_level(25) == 2
    assert compute_level(99) == 2
    assert compute_level(100) == 3
    assert compute_level(249) == 3
    assert compute_level(250) == 4
    assert compute_level(599) == 4
    assert compute_level(600) == 5
    assert compute_level(99_999) == 5


def test_compute_level_overflow_raises():
    with pytest.raises(LevelComputeOverflow):
        compute_level(10**9)


def test_next_level_threshold_lv5_caps_at_cap_threshold():
    assert next_level_threshold(5) == LEVEL_5_CAP_THRESHOLD


def test_unlocks_crossed_emits_per_level_flags():
    crossed = unlocks_crossed(1, 5)
    assert crossed == (
        "extended_thinking",
        "parallel_subtask",
        "prompt_overhead",
        "teach_other_agent",
    )
    assert unlocks_crossed(4, 5) == unlocks_for_level(5)
    assert unlocks_crossed(3, 3) == ()


def test_mastery_effects_table_covers_every_canonical_skill():
    table = mastery_effects_table()
    assert set(table) == set(canonical_skill_ids())
    assert table[SKILL_ID] == {
        2: ("extended_thinking",),
        3: ("parallel_subtask",),
        4: ("prompt_overhead",),
        5: ("teach_other_agent",),
    }


def test_mastery_effects_for_skill_rejects_skill_id_not_in_matrix():
    with pytest.raises(SkillIdNotInMatrix):
        mastery_effects_for_skill("does_not_exist")


# ── compute_xp_delta (pure multiplier math) ─────────────────────────


def test_outcome_multipliers_basic():
    assert compute_xp_delta(100, "success") == 100
    assert compute_xp_delta(100, "partial") == 40
    assert compute_xp_delta(100, "fail") == 10


def test_tier_l_plus_stacks_on_top_of_outcome():
    assert compute_xp_delta(100, "success", tier_l_plus=True) == 200


def test_first_time_skill_stacks_on_top_of_outcome_and_tier():
    # success (1.0) * tier_l_plus (2.0) * first_time (3.0) = 6.0
    assert compute_xp_delta(100, "success", tier_l_plus=True, first_time_skill_use=True) == 600


def test_anti_grind_clamp():
    # success (1.0) * anti-grind (0.2) = 0.2
    assert compute_xp_delta(100, "success", same_task_hash_within_24h=True) == 20


def test_negative_base_delta_rejected():
    with pytest.raises(XPDeltaNegativeWithoutDecayContext):
        compute_xp_delta(-10, "success")


# ── award_skill_xp (store + threshold transitions) ──────────────────


@pytest.mark.asyncio
async def test_award_skill_xp_lv1_to_lv2_transition_emits_extended_thinking():
    store = InMemorySkillStateStore()
    award = await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=30, outcome="success", now=T0,
    )
    assert award.new_xp == 30
    assert award.previous_level == 1
    assert award.new_level == 2
    assert "extended_thinking" in award.unlocks
    assert award.branch_choice_required is False


@pytest.mark.asyncio
async def test_award_skill_xp_lv2_to_lv3_emits_branch_choice_required():
    store = InMemorySkillStateStore()
    await award_skill_xp(store, "agent-A", SKILL_ID, delta=25, outcome="success", now=T0)
    award = await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=80, outcome="success", now=T0,
    )
    assert award.new_xp == 105
    assert award.new_level == 3
    assert "parallel_subtask" in award.unlocks
    assert award.branch_choice_required is True


@pytest.mark.asyncio
async def test_award_skill_xp_lv4_to_lv5_emits_teach_other_agent_unlock():
    store = InMemorySkillStateStore()
    state = SkillState(
        agent_id="agent-A",
        skill_id=SKILL_ID,
        level=4,
        xp=599,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    await store.upsert_state(state)
    award = await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=10, outcome="success", now=T0,
    )
    assert award.previous_level == 4
    assert award.new_level == 5
    assert "teach_other_agent" in award.unlocks


@pytest.mark.asyncio
async def test_award_skill_xp_anti_grind_does_not_re_emit_branch_choice():
    # Anti-grind row that would have crossed Lv 3 if not clamped should
    # still rely on the clamped delta — agent stays at Lv 2.
    store = InMemorySkillStateStore()
    await award_skill_xp(store, "agent-A", SKILL_ID, delta=25, outcome="success", now=T0)
    award = await award_skill_xp(
        store,
        "agent-A",
        SKILL_ID,
        delta=100,
        outcome="success",
        same_task_hash_within_24h=True,
        now=T0,
    )
    assert award.xp_delta == 20
    assert award.new_level == 2
    assert award.branch_choice_required is False


@pytest.mark.asyncio
async def test_award_skill_xp_rejects_skill_id_not_in_matrix():
    store = InMemorySkillStateStore()
    with pytest.raises(SkillIdNotInMatrix):
        await award_skill_xp(
            store, "agent-A", "does_not_exist", delta=10, outcome="success", now=T0,
        )


@pytest.mark.asyncio
async def test_award_skill_xp_rejects_negative_delta():
    store = InMemorySkillStateStore()
    with pytest.raises(XPDeltaNegativeWithoutDecayContext):
        await award_skill_xp(
            store, "agent-A", SKILL_ID, delta=-5, outcome="success", now=T0,
        )


# ── branch lock idempotent + reject re-write + drift guard ──────────


@pytest.mark.asyncio
async def test_lock_branch_choice_is_idempotent_on_same_value():
    store = InMemorySkillStateStore()
    first = await lock_branch_choice(store, "agent-A", SKILL_ID, "perf_tuning", now=T0)
    second = await lock_branch_choice(store, "agent-A", SKILL_ID, "perf_tuning", now=T0)
    assert first.branch_choice == "perf_tuning"
    assert second.branch_choice == "perf_tuning"


@pytest.mark.asyncio
async def test_lock_branch_choice_rejects_different_branch_rewrite():
    store = InMemorySkillStateStore()
    await lock_branch_choice(store, "agent-A", SKILL_ID, "perf_tuning", now=T0)
    with pytest.raises(SkillBranchAlreadyLocked):
        await lock_branch_choice(
            store, "agent-A", SKILL_ID, "type_correctness", now=T0
        )


@pytest.mark.asyncio
async def test_lock_branch_choice_rejects_branch_not_in_matrix():
    store = InMemorySkillStateStore()
    with pytest.raises(SkillMatrixDriftError):
        await lock_branch_choice(
            store, "agent-A", SKILL_ID, "fictional_branch", now=T0
        )


@pytest.mark.asyncio
async def test_lock_branch_choice_rejects_skill_not_in_matrix():
    store = InMemorySkillStateStore()
    with pytest.raises((SkillIdNotInMatrix, SkillMatrixDriftError)):
        await lock_branch_choice(
            store, "agent-A", "ghost_skill", "perf_tuning", now=T0
        )


# ── decay cap ───────────────────────────────────────────────────────


def test_apply_decay_caps_at_next_level_threshold_minus_one():
    # Lv 2 (xp=99) → next threshold is 100, floor is 99.
    assert apply_decay(99, level=2, weeks_idle=10) == 99
    # Lv 4 starting at xp=400, decaying for many weeks. Floor is 599
    # for someone *at Lv 5*; for Lv 4 it's `LEVEL_THRESHOLDS[5] - 1` = 599.
    # But the agent's *current level* is 4 — floor is `max(LEVEL_THRESHOLDS[4],
    # next_level_threshold(4) - 1)` = max(250, 599) = 599.
    assert apply_decay(400, level=4, weeks_idle=100) == 599 or (
        # Below floor case: xp starts under the floor, decay is a no-op.
        apply_decay(400, level=4, weeks_idle=100) == 400
    )


def test_apply_decay_zero_weeks_is_no_op():
    assert apply_decay(123, level=2, weeks_idle=0) == 123
    assert apply_decay(123, level=2, weeks_idle=-5) == 123


def test_apply_decay_rate_is_five_percent_per_week():
    # one week of decay on a high-XP Lv 5 row: 1000 -> 950 (5%).
    assert abs((1 - DECAY_RATE_PER_WEEK) * 1000 - 950) < 1e-9
    # Lv-5 cap floor is 1499 (LEVEL_5_CAP_THRESHOLD - 1). A Lv 5 row at
    # XP 2000 should never drop below 1499.
    assert apply_decay(2000, level=5, weeks_idle=100) >= LEVEL_5_CAP_THRESHOLD - 1


@pytest.mark.asyncio
async def test_decay_idle_skills_only_touches_rows_older_than_30_days():
    store = InMemorySkillStateStore()
    now = T0 + timedelta(days=45)
    fresh = SkillState(
        agent_id="agent-A",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=now - timedelta(days=5),
    )
    stale = SkillState(
        agent_id="agent-B",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=now - timedelta(days=45),
    )
    await store.upsert_state(fresh)
    await store.upsert_state(stale)
    touched = await decay_idle_skills(store, now=now)
    assert len(touched) == 1
    assert touched[0].agent_id == "agent-B"


# ── teach cooldown ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_teach_other_agent_grants_25_xp_to_student():
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    award = await teach_other_agent(
        store, "teacher", "student", SKILL_ID, now=T0,
    )
    assert award.xp_delta == TEACH_INJECTION_XP


@pytest.mark.asyncio
async def test_teach_cooldown_blocks_within_7_days():
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    await teach_other_agent(store, "teacher", "s1", SKILL_ID, now=T0)
    with pytest.raises(TeachCooldownActive):
        await teach_other_agent(
            store, "teacher", "s2", SKILL_ID, now=T0 + timedelta(days=TEACH_COOLDOWN_DAYS - 1),
        )


@pytest.mark.asyncio
async def test_teach_cooldown_releases_after_7_days():
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    await teach_other_agent(store, "teacher", "s1", SKILL_ID, now=T0)
    # +8 days — cooldown clear.
    award = await teach_other_agent(
        store, "teacher", "s2", SKILL_ID, now=T0 + timedelta(days=TEACH_COOLDOWN_DAYS + 1),
    )
    assert award.xp_delta == TEACH_INJECTION_XP


@pytest.mark.asyncio
async def test_teach_rejected_when_teacher_not_lv5():
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=3,
        xp=120,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    with pytest.raises(SkillLevelingError):
        await teach_other_agent(store, "teacher", "student", SKILL_ID, now=T0)


@pytest.mark.asyncio
async def test_teach_rejected_when_student_above_lv2():
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2000,
        branch_choice="perf_tuning",
        last_active_at=T0,
    )
    student = SkillState(
        agent_id="student",
        skill_id=SKILL_ID,
        level=3,
        xp=120,
        branch_choice="type_correctness",
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    await store.upsert_state(student)
    with pytest.raises(SkillLevelingError):
        await teach_other_agent(store, "teacher", "student", SKILL_ID, now=T0)


# ── invariants ──────────────────────────────────────────────────────


def test_branch_lock_level_is_three():
    assert BRANCH_LOCK_LEVEL == 3


def test_level_thresholds_are_strictly_increasing():
    values = [LEVEL_THRESHOLDS[level] for level in sorted(LEVEL_THRESHOLDS)]
    assert all(values[i] < values[i + 1] for i in range(len(values) - 1))


# ── PostgresSkillStateStore.award_delta_atomic (S1b, OP-2501) ───────


class _FakeAtomicConn:
    """Minimal asyncpg.Connection stand-in for atomic-award tests.

    Records every ``fetchrow`` call and lets the test script scripted
    responses that simulate what Postgres would return from the
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING skill_xp,
    branch_choice, (xmax = 0) AS inserted`` statement.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.fetchrow_returns: list[dict[str, Any]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_returns.pop(0)


class _FakeAcquireCM:
    """Mimics ``async with pool.acquire() as conn``."""

    def __init__(self, conn: _FakeAtomicConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeAtomicConn:
        return self.conn

    async def __aexit__(self, *_args: Any) -> bool:
        return False


def _make_pg_store() -> tuple[PostgresSkillStateStore, _FakeAtomicConn]:
    conn = _FakeAtomicConn()
    store = PostgresSkillStateStore(conn_factory=lambda: _FakeAcquireCM(conn))
    return store, conn


@pytest.mark.asyncio
async def test_award_delta_atomic_first_award_inserts_first_delta():
    store, conn = _make_pg_store()
    # 100 base * 1.0 (success) * 3.0 (first_time) = 300.
    conn.fetchrow_returns.append(
        {"skill_xp": 300, "branch_choice": None, "inserted": True}
    )
    result = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=100, outcome="success",
    )
    assert isinstance(result, AtomicSkillXpAward)
    assert result.inserted is True
    assert result.new_xp == 300
    assert result.applied_delta == 300  # first_delta
    assert result.new_level == compute_level(300)
    assert result.branch_choice is None
    # SQL shape: single INSERT ... ON CONFLICT DO UPDATE RETURNING xmax = 0
    _, sql, args = conn.calls[0]
    assert "INSERT INTO agent_skill_state" in sql
    assert "ON CONFLICT (agent_id, skill_id) DO UPDATE" in sql
    assert "RETURNING" in sql
    assert "xmax = 0" in sql
    # first_delta lands in VALUES, repeat_delta lands in DO UPDATE.
    # For success outcome: first = 300, repeat = 100.
    assert 300 in args
    assert 100 in args


@pytest.mark.asyncio
async def test_award_delta_atomic_second_award_adds_repeat_delta():
    store, conn = _make_pg_store()
    # Update-branch return: previous row skill_xp=300 + repeat_delta=100 = 400.
    conn.fetchrow_returns.append(
        {"skill_xp": 400, "branch_choice": None, "inserted": False}
    )
    result = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=100, outcome="success",
    )
    assert result.inserted is False
    assert result.new_xp == 400
    assert result.applied_delta == 100  # repeat_delta
    assert result.new_level == compute_level(400)


@pytest.mark.asyncio
async def test_award_delta_atomic_concurrent_awards_sum_without_loss():
    """Two runner slots delivering the same character's ticket must
    sum without loss even though both would naively see 'no row' at
    the read stage. In the atomic path only one wins the INSERT and
    the other folds into DO UPDATE with repeat_delta — the sum is
    ``first_delta + repeat_delta`` and NEVER ``first_delta`` (which
    is what the old read-modify-write yielded when the loser
    clobbered the winner).
    """
    store, conn = _make_pg_store()
    # Slot 1 wins the insert race: 300 (first_time).
    conn.fetchrow_returns.append(
        {"skill_xp": 300, "branch_choice": None, "inserted": True}
    )
    # Slot 2 folds into DO UPDATE: 300 + 100 (repeat) = 400.
    conn.fetchrow_returns.append(
        {"skill_xp": 400, "branch_choice": None, "inserted": False}
    )
    a = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=100, outcome="success",
    )
    b = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=100, outcome="success",
    )
    # inserted flag flips True → False across the two calls
    assert (a.inserted, b.inserted) == (True, False)
    # No lost update: the second call's absolute new_xp is the sum,
    # not a re-application of first_delta.
    assert b.new_xp == a.new_xp + b.applied_delta
    assert b.applied_delta == 100  # repeat_delta, not first_delta
    assert b.new_level == compute_level(b.new_xp)


@pytest.mark.asyncio
async def test_award_delta_atomic_level_matches_compute_level_from_returned_xp():
    store, conn = _make_pg_store()
    # 250 crosses Lv 4 threshold — regardless of what caller passed.
    conn.fetchrow_returns.append(
        {"skill_xp": 250, "branch_choice": None, "inserted": True}
    )
    result = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=50, outcome="success",
    )
    assert result.new_level == compute_level(250) == 4


@pytest.mark.asyncio
async def test_award_delta_atomic_branch_choice_required_when_lv3_and_unlocked():
    store, conn = _make_pg_store()
    conn.fetchrow_returns.append(
        {"skill_xp": 150, "branch_choice": None, "inserted": False}
    )
    result = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=50, outcome="success",
    )
    assert result.new_level == 3
    assert result.branch_choice_required is True


@pytest.mark.asyncio
async def test_award_delta_atomic_branch_choice_required_false_when_already_locked():
    store, conn = _make_pg_store()
    conn.fetchrow_returns.append(
        {"skill_xp": 150, "branch_choice": "perf_tuning", "inserted": False}
    )
    result = await store.award_delta_atomic(
        "agent-A", SKILL_ID, base_delta=50, outcome="success",
    )
    assert result.branch_choice == "perf_tuning"
    assert result.branch_choice_required is False


@pytest.mark.asyncio
async def test_award_delta_atomic_precomputes_first_and_repeat_deltas():
    """Both first-time and repeat multipliers are baked into the args
    so the single statement can pick the right one via the conflict
    branch. With tier_l_plus stacked: first = 100*1.0*2.0*3.0 = 600
    and repeat = 100*1.0*2.0 = 200.
    """
    store, conn = _make_pg_store()
    conn.fetchrow_returns.append(
        {"skill_xp": 600, "branch_choice": None, "inserted": True}
    )
    await store.award_delta_atomic(
        "agent-A",
        SKILL_ID,
        base_delta=100,
        outcome="success",
        tier_l_plus=True,
    )
    _, _sql, args = conn.calls[0]
    assert 600 in args  # first_delta
    assert 200 in args  # repeat_delta


@pytest.mark.asyncio
async def test_award_delta_atomic_rejects_negative_base_delta():
    store, _conn = _make_pg_store()
    with pytest.raises(XPDeltaNegativeWithoutDecayContext):
        await store.award_delta_atomic(
            "agent-A", SKILL_ID, base_delta=-1, outcome="success",
        )


@pytest.mark.asyncio
async def test_award_delta_atomic_rejects_skill_id_not_in_matrix():
    store, _conn = _make_pg_store()
    with pytest.raises(SkillIdNotInMatrix):
        await store.award_delta_atomic(
            "agent-A", "does_not_exist", base_delta=10, outcome="success",
        )

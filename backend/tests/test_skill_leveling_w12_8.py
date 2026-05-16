"""RPG.W12.8 — extended contract tests for ``backend/agents/skill_leveling.py``.

OP-177 follow-on to the W12.x bundle landed under OP-217. The original
``test_skill_leveling.py`` covers the 10 high-level AC cases lifted off
ADR-0008 §"Skill leveling (W12)"; this file thickens four axes the
parent ticket called out — XP curve edges, Lv-3 branching exclusivity,
30d/5%-per-week decay, and the 7-day Lv-5 teach cooldown — with ~25
focused cases. The split keeps the OP-217 file pinned to the AC matrix
while this file owns the longer tail of stacking-multiplier,
floor-clamp, and cooldown-boundary tests that operators have asked for
post-pickup.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.skill_leveling import (
    ANTI_GRIND_MULTIPLIER,
    BRANCH_LOCK_LEVEL,
    DECAY_RATE_PER_WEEK,
    FIRST_TIME_SKILL_MULTIPLIER,
    IDLE_BEFORE_DECAY_DAYS,
    InMemorySkillStateStore,
    LEVEL_5_CAP_THRESHOLD,
    LEVEL_OVERFLOW_GUARD_XP,
    LEVEL_THRESHOLDS,
    SkillIdNotInMatrix,
    SkillLevelingError,
    SkillState,
    TEACH_COOLDOWN_DAYS,
    TEACH_INJECTION_XP,
    TIER_L_PLUS_MULTIPLIER,
    TeachCooldownActive,
    XPDeltaNegativeWithoutDecayContext,
    apply_decay,
    award_skill_xp,
    compute_level,
    compute_xp_delta,
    decay_idle_skills,
    lock_branch_choice,
    teach_other_agent,
)
from backend.agents.skill_matrix import BRANCHES_PER_SKILL, load_skill_matrix


SKILL_ID = "enterprise_web"
BRANCH_A = "perf_tuning"
BRANCH_B = "type_correctness"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── XP curve (compute_level + compute_xp_delta edges) ───────────────


def test_compute_level_returns_lv5_just_below_overflow_guard():
    # One token below the defensive overflow ceiling still resolves to
    # the cap level. The +1 case is the one ``test_compute_level_overflow_raises``
    # already covers in the OP-217 file.
    assert compute_level(LEVEL_OVERFLOW_GUARD_XP - 1) == 5


def test_compute_level_rejects_bool_input():
    # bool is an int subclass — the helper must reject it explicitly so a
    # stray ``True``/``False`` from upstream callers does not silently
    # resolve to Lv 1 / Lv 1.
    with pytest.raises(TypeError):
        compute_level(True)  # type: ignore[arg-type]


def test_compute_level_rejects_negative_xp():
    with pytest.raises(ValueError):
        compute_level(-1)


def test_compute_xp_delta_partial_with_tier_l_plus_stacks():
    # partial (0.4) * tier_l_plus (2.0) = 0.8 → 80 XP from 100 base.
    assert compute_xp_delta(100, "partial", tier_l_plus=True) == 80


def test_compute_xp_delta_fail_with_first_time_skill_stacks():
    # fail (0.1) * first_time (3.0) = 0.3 → 30 XP from 100 base.
    assert compute_xp_delta(100, "fail", first_time_skill_use=True) == 30


def test_compute_xp_delta_all_three_modifiers_compound():
    # partial (0.4) * tier_l+ (2.0) * first_time (3.0) * anti-grind (0.2)
    # = 0.48 → 48 XP from 100 base. Asserts the full stacking chain
    # collapses correctly without any flag being silently dropped.
    expected = int(
        100
        * 0.4
        * TIER_L_PLUS_MULTIPLIER
        * FIRST_TIME_SKILL_MULTIPLIER
        * ANTI_GRIND_MULTIPLIER
    )
    assert (
        compute_xp_delta(
            100,
            "partial",
            tier_l_plus=True,
            first_time_skill_use=True,
            same_task_hash_within_24h=True,
        )
        == expected
        == 48
    )


def test_compute_xp_delta_unknown_outcome_raises_value_error():
    with pytest.raises(ValueError):
        compute_xp_delta(100, "great")


def test_compute_xp_delta_rejects_bool_base_delta():
    with pytest.raises(TypeError):
        compute_xp_delta(True, "success")  # type: ignore[arg-type]


def test_compute_xp_delta_failed_alias_equivalent_to_fail():
    # The W12 outcome table aliases "failed" to "fail" so callers that
    # speak in past-tense outcome strings (XP engine, evaluator) do not
    # collapse to a zero multiplier.
    assert compute_xp_delta(100, "failed") == compute_xp_delta(100, "fail") == 10


def test_compute_xp_delta_outcome_normalises_case_and_whitespace():
    # _clean_outcome strips and lowercases — verifies that an outcome
    # token from a noisy upstream source does not silently drop to 0.
    assert compute_xp_delta(100, "  SUCCESS  ") == 100
    assert compute_xp_delta(100, "Partial") == 40


def test_compute_xp_delta_floors_fractional_result_to_int():
    # 23 * 0.4 = 9.2 — int() truncates toward zero, no rounding.
    assert compute_xp_delta(23, "partial") == 9


# ── Branching exclusivity (W12.4 / OP-173) ──────────────────────────


@pytest.mark.asyncio
async def test_lock_branch_choice_creates_row_when_state_absent():
    # Locking before any XP is awarded: the helper must scaffold a
    # Lv 1 / xp=0 row with the branch pre-set so a subsequent
    # ``award_skill_xp`` does not re-emit ``branch_choice_required``.
    store = InMemorySkillStateStore()
    locked = await lock_branch_choice(store, "agent-A", SKILL_ID, BRANCH_A, now=T0)
    assert locked.level == 1
    assert locked.xp == 0
    assert locked.branch_choice == BRANCH_A
    persisted = await store.get_state("agent-A", SKILL_ID)
    assert persisted is not None
    assert persisted.branch_choice == BRANCH_A


@pytest.mark.asyncio
async def test_branch_choice_required_false_after_lock_then_award():
    # Pre-locking BEFORE crossing Lv 3 must suppress the Character Card
    # branch-picker on the same award call that crosses the threshold.
    store = InMemorySkillStateStore()
    await lock_branch_choice(store, "agent-A", SKILL_ID, BRANCH_A, now=T0)
    award = await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=200, outcome="success", now=T0,
    )
    assert award.new_level >= BRANCH_LOCK_LEVEL
    assert award.branch_choice_required is False


def test_canonical_branches_per_skill_yaml_invariant_two():
    # The W12.4 spec constant is exactly two branches per skill; this
    # walks the canonical YAML to assert no skill has drifted to one or
    # three options (which would silently let the picker become
    # uncontested or three-way).
    matrix = load_skill_matrix()
    seen = 0
    for skills in matrix.values():
        for skill in skills:
            assert len(skill.branches) == BRANCHES_PER_SKILL, (
                f"skill {skill.skill_id!r} declared "
                f"{len(skill.branches)} branches, expected {BRANCHES_PER_SKILL}"
            )
            seen += 1
    # Sanity floor: matrix must not collapse to zero skills (drift guard
    # would otherwise pass vacuously).
    assert seen > 0


@pytest.mark.asyncio
async def test_award_skill_xp_preserves_branch_choice_after_lock():
    # Awarding XP after a branch is locked must not nullify the lock —
    # the upsert in award_skill_xp copies the existing branch_choice
    # forward rather than overwriting with None.
    store = InMemorySkillStateStore()
    await lock_branch_choice(store, "agent-A", SKILL_ID, BRANCH_A, now=T0)
    await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=500, outcome="success", now=T0,
    )
    state = await store.get_state("agent-A", SKILL_ID)
    assert state is not None
    assert state.branch_choice == BRANCH_A


@pytest.mark.asyncio
async def test_lock_branch_choice_to_other_branch_rejected_after_xp_award():
    # Branch exclusivity must hold across the "XP-first, lock-later"
    # path: agent grinds past Lv 3, locks BRANCH_A, then a second
    # ``lock_branch_choice`` call with BRANCH_B must still raise.
    from backend.agents.skill_leveling import SkillBranchAlreadyLocked

    store = InMemorySkillStateStore()
    await award_skill_xp(
        store, "agent-A", SKILL_ID, delta=120, outcome="success", now=T0,
    )
    await lock_branch_choice(store, "agent-A", SKILL_ID, BRANCH_A, now=T0)
    with pytest.raises(SkillBranchAlreadyLocked):
        await lock_branch_choice(store, "agent-A", SKILL_ID, BRANCH_B, now=T0)


# ── Decay (W12.5) ───────────────────────────────────────────────────


def test_apply_decay_lv2_floor_is_99():
    # Floor = next_level_threshold(2) - 1 = 100 - 1 = 99. One week of
    # decay at xp=99 would compute to 94 but must clamp to the floor.
    assert apply_decay(99, level=2, weeks_idle=1) == 99
    assert apply_decay(99, level=2, weeks_idle=52) == 99


def test_apply_decay_lv3_floor_is_249():
    # Floor = next_level_threshold(3) - 1 = 250 - 1 = 249.
    assert apply_decay(249, level=3, weeks_idle=1) == 249
    assert apply_decay(249, level=3, weeks_idle=100) == 249


def test_apply_decay_lv4_floor_is_599():
    # Floor = next_level_threshold(4) - 1 = 600 - 1 = 599.
    assert apply_decay(599, level=4, weeks_idle=1) == 599
    assert apply_decay(599, level=4, weeks_idle=100) == 599


def test_apply_decay_lv5_floor_is_cap_threshold_minus_one():
    # Lv-5 floor is the configurable Lv-5 cap minus one, NOT the Lv-5
    # entry threshold — a Lv-5 row that never accumulated past cap
    # cannot decay below ``LEVEL_5_CAP_THRESHOLD - 1`` and demote out
    # of cap.
    floor = LEVEL_5_CAP_THRESHOLD - 1
    assert apply_decay(floor, level=5, weeks_idle=1) == floor
    assert apply_decay(floor, level=5, weeks_idle=200) == floor


def test_apply_decay_high_xp_lv5_decays_proportionally_above_floor():
    # 10000 XP at Lv 5, 4 weeks idle:
    #   decayed = int(10000 * 0.95**4) = int(8145.0625) = 8145
    # which sits well above the Lv-5 floor (1499), so the floor clamp
    # is inactive and the raw multiplier wins.
    decayed = apply_decay(10000, level=5, weeks_idle=4)
    expected = int(10000 * ((1.0 - DECAY_RATE_PER_WEEK) ** 4))
    assert decayed == expected
    assert decayed > LEVEL_5_CAP_THRESHOLD - 1


def test_apply_decay_xp_below_floor_is_clamped_up_to_floor():
    # Documents the contract: when a row's recorded xp is below its
    # level's floor (e.g. row was just promoted via a level field write
    # without an XP backfill), apply_decay clamps UP to the floor
    # rather than decaying below it. This is the OP-217 file's
    # "or apply_decay(...) == 400" branch, locked in explicitly here.
    assert apply_decay(400, level=4, weeks_idle=100) == 599


@pytest.mark.asyncio
async def test_decay_idle_skills_skips_rows_already_at_floor():
    # An idle Lv 5 row at the cap floor must NOT show up in the touched
    # list — the sweep elides no-op writes so the audit log stays tight.
    store = InMemorySkillStateStore()
    now = T0 + timedelta(days=IDLE_BEFORE_DECAY_DAYS + 30)
    floor_row = SkillState(
        agent_id="agent-floor",
        skill_id=SKILL_ID,
        level=5,
        xp=LEVEL_5_CAP_THRESHOLD - 1,
        branch_choice=BRANCH_A,
        last_active_at=now - timedelta(days=IDLE_BEFORE_DECAY_DAYS + 30),
    )
    await store.upsert_state(floor_row)
    touched = await decay_idle_skills(store, now=now)
    assert touched == ()


@pytest.mark.asyncio
async def test_decay_idle_skills_preserves_level_field():
    # The level field must NOT regress when xp decays — only the xp
    # column moves, per ADR-0008 §"Skill leveling (W12)".
    store = InMemorySkillStateStore()
    now = T0 + timedelta(days=IDLE_BEFORE_DECAY_DAYS + 60)
    row = SkillState(
        agent_id="agent-decay",
        skill_id=SKILL_ID,
        level=5,
        xp=10_000,
        branch_choice=BRANCH_A,
        last_active_at=now - timedelta(days=IDLE_BEFORE_DECAY_DAYS + 60),
    )
    await store.upsert_state(row)
    touched = await decay_idle_skills(store, now=now)
    assert len(touched) == 1
    assert touched[0].level == 5
    assert touched[0].xp < 10_000
    persisted = await store.get_state("agent-decay", SKILL_ID)
    assert persisted is not None
    assert persisted.level == 5


@pytest.mark.asyncio
async def test_decay_idle_skills_returns_only_actually_changed_rows():
    # Mixed sweep: one row above floor (touched), one at floor (no-op).
    # Verifies the touched-row tuple is filtered, not the iteration set.
    store = InMemorySkillStateStore()
    now = T0 + timedelta(days=IDLE_BEFORE_DECAY_DAYS + 30)
    last_active = now - timedelta(days=IDLE_BEFORE_DECAY_DAYS + 30)
    above = SkillState(
        agent_id="agent-above",
        skill_id=SKILL_ID,
        level=5,
        xp=5_000,
        branch_choice=BRANCH_A,
        last_active_at=last_active,
    )
    at_floor = SkillState(
        agent_id="agent-floor",
        skill_id=SKILL_ID,
        level=5,
        xp=LEVEL_5_CAP_THRESHOLD - 1,
        branch_choice=BRANCH_A,
        last_active_at=last_active,
    )
    await store.upsert_state(above)
    await store.upsert_state(at_floor)
    touched = await decay_idle_skills(store, now=now)
    assert {row.agent_id for row in touched} == {"agent-above"}


# ── Teaching cooldown (W12.6) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_teach_other_agent_records_last_taught_at_on_teacher():
    # The cooldown clock lives on the teacher row, not on a separate
    # ledger — verify the timestamp lands on ``last_taught_at`` so the
    # next ``teach_other_agent`` call can read it back.
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2_000,
        branch_choice=BRANCH_A,
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    assert teacher.last_taught_at is None
    await teach_other_agent(store, "teacher", "student", SKILL_ID, now=T0)
    updated = await store.get_state("teacher", SKILL_ID)
    assert updated is not None
    assert updated.last_taught_at == T0


@pytest.mark.asyncio
async def test_teach_cooldown_releases_at_exact_seven_day_boundary():
    # Cooldown predicate is ``delta < timedelta(days=7)`` — delta of
    # exactly 7 days is NOT less than 7 days, so the second teach must
    # succeed at the exact boundary. Complements OP-217's day-6 (block)
    # and day-8 (release) cases.
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2_000,
        branch_choice=BRANCH_A,
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    await teach_other_agent(store, "teacher", "s1", SKILL_ID, now=T0)
    award = await teach_other_agent(
        store,
        "teacher",
        "s2",
        SKILL_ID,
        now=T0 + timedelta(days=TEACH_COOLDOWN_DAYS),
    )
    assert award.xp_delta == TEACH_INJECTION_XP


@pytest.mark.asyncio
async def test_teach_cooldown_blocks_one_second_before_boundary():
    # Companion to the boundary test above — one second under the
    # cooldown window must still block, ruling off a "<=" vs "<"
    # regression.
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2_000,
        branch_choice=BRANCH_A,
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    await teach_other_agent(store, "teacher", "s1", SKILL_ID, now=T0)
    with pytest.raises(TeachCooldownActive):
        await teach_other_agent(
            store,
            "teacher",
            "s2",
            SKILL_ID,
            now=T0 + timedelta(days=TEACH_COOLDOWN_DAYS) - timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_teach_other_agent_rejects_self_teach():
    # Teacher and student must differ; otherwise the cooldown UPSERT
    # would race the award UPSERT on the same row.
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="solo",
        skill_id=SKILL_ID,
        level=5,
        xp=2_000,
        branch_choice=BRANCH_A,
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    with pytest.raises(SkillLevelingError):
        await teach_other_agent(store, "solo", "solo", SKILL_ID, now=T0)


@pytest.mark.asyncio
async def test_teach_other_agent_rejects_when_teacher_state_absent():
    # No teacher row at all — distinct from "row exists but level < 5".
    # The error class is the same (SkillLevelingError) but the path is
    # different and the OP-217 file only exercises the level<5 branch.
    store = InMemorySkillStateStore()
    with pytest.raises(SkillLevelingError):
        await teach_other_agent(store, "ghost-teacher", "student", SKILL_ID, now=T0)


@pytest.mark.asyncio
async def test_teach_student_with_no_state_is_eligible_as_lv1():
    # Implicit Lv 1 for an agent the matrix has never seen — must be
    # accepted as a teach target, and the 25-XP injection must scaffold
    # a fresh state row promoted to Lv 2.
    store = InMemorySkillStateStore()
    teacher = SkillState(
        agent_id="teacher",
        skill_id=SKILL_ID,
        level=5,
        xp=2_000,
        branch_choice=BRANCH_A,
        last_active_at=T0,
    )
    await store.upsert_state(teacher)
    award = await teach_other_agent(store, "teacher", "fresh", SKILL_ID, now=T0)
    assert award.xp_delta == TEACH_INJECTION_XP
    student_state = await store.get_state("fresh", SKILL_ID)
    assert student_state is not None
    assert student_state.xp == TEACH_INJECTION_XP
    assert student_state.level == 2  # 25 XP crosses LEVEL_THRESHOLDS[2]


@pytest.mark.asyncio
async def test_teach_other_agent_rejects_skill_not_in_matrix():
    # Drift guard fires before any store read so even a non-existent
    # teacher row does not change the error class for an off-matrix skill.
    store = InMemorySkillStateStore()
    with pytest.raises(SkillIdNotInMatrix):
        await teach_other_agent(store, "teacher", "student", "ghost_skill", now=T0)


# ── Cross-axis sanity (XP curve ↔ branching ↔ decay invariants) ─────


def test_level_thresholds_match_adr_curve_25_100_250_600():
    # Locks in the canonical curve from ADR-0008 §"Skill leveling (W12)"
    # so a stray re-tune (e.g. flattening the Lv 3 gate) trips here
    # rather than silently shifting the Character Card thresholds.
    assert LEVEL_THRESHOLDS[1] == 0
    assert LEVEL_THRESHOLDS[2] == 25
    assert LEVEL_THRESHOLDS[3] == 100
    assert LEVEL_THRESHOLDS[4] == 250
    assert LEVEL_THRESHOLDS[5] == 600
    assert LEVEL_5_CAP_THRESHOLD == 1500


@pytest.mark.asyncio
async def test_award_skill_xp_rejects_bool_delta():
    # bool is an int subclass — guard against ``award_skill_xp(..., delta=True, ...)``
    # silently awarding 1 XP. The XPDeltaNegativeWithoutDecayContext /
    # TypeError split here matches compute_xp_delta's contract.
    store = InMemorySkillStateStore()
    with pytest.raises(TypeError):
        await award_skill_xp(
            store,
            "agent-A",
            SKILL_ID,
            delta=True,  # type: ignore[arg-type]
            outcome="success",
            now=T0,
        )


def test_compute_xp_delta_rejects_negative_with_typed_error():
    # Ensures the negative-delta gate uses the typed exception class
    # rather than a bare ValueError — downstream callers (XP engine
    # reconciliation) catch the typed class.
    with pytest.raises(XPDeltaNegativeWithoutDecayContext):
        compute_xp_delta(-1, "success")

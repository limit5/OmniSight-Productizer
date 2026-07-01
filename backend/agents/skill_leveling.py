"""RPG.W12 -- per-``(agent_id, skill_id)`` skill leveling + branching tree.

ADR-0008 §"Skill leveling (W12)" defines a 1-5 level ladder per skill
with an immutable branch_choice at Lv 3, per-level unlock hooks, and a
30-day-idle XP decay. This module owns the helper surface backend
callers reach for (``award_skill_xp``, ``compute_level``,
``lock_branch_choice``) plus the per-level unlock catalog and the
30d/5%-per-week decay rule. Persistence is delegated to the injected
:class:`SkillStateStore`; the in-memory store is used for dev/tests
and the Postgres store is wired into ``agent_skill_state`` (alembic
0226).

W12 sub-wave coverage in this module
------------------------------------
The W12 module shipped in one bundle under OP-217; this table tracks
the attribution of each sub-wave back to its dedicated TODO row so a
future reader of git blame can resolve a symbol to its W12.x ticket.

- W12.2 (OP-171 / OP-1381): :data:`LEVEL_THRESHOLDS` / :data:`LEVEL_5_CAP_THRESHOLD`
  + :func:`compute_level` / :func:`next_level_threshold` -- the
  ``25 / 100 / 250 / 600 / 1500`` task-success-token curve from
  ADR-0008 §"Skill leveling (W12)". Lv 1 starts at 0 XP; Lv 2-5 are
  the cumulative thresholds to *reach* that level; the Lv-5 cap at
  1500 is the decay floor (not a level gate) so a Lv-5 row cannot
  decay below ``LEVEL_5_CAP_THRESHOLD - 1`` and demote out of cap.

- W12.4 (OP-173): :data:`BRANCH_LOCK_LEVEL` + :func:`lock_branch_choice`
  + :class:`SkillBranchAlreadyLocked` + the
  ``branch_choice_required`` flag on :class:`SkillXpAward` --
  ADR-0008 §"Skill leveling (W12)" 's "每個 base skill 在 Lv 3 分叉
  2 條" rule. At Lv 3 every base skill forks into exactly two
  branches declared in ``skill_matrix.yaml``; the operator picks one
  from the Character Card "Skills" tab and the choice is
  immutable per ``(agent_id, skill_id)``. ``award_skill_xp`` raises
  ``branch_choice_required=True`` when an XP gain pushes the row
  past Lv 3 with no branch locked yet, which is what the Character
  Card uses to render the picker. The drift guard
  (:func:`backend.agents.skill_matrix.assert_branch_choice_in_matrix`)
  rejects a lock whose ``branch`` is not declared in the YAML.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines constants, dataclasses, exception classes, and
``Protocol``/store classes only. Mutable state lives inside the
injected store. No clock reads at import time; the few callers that
need "now" pass it explicitly so unit tests stay deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from backend.agents.skill_matrix import (
    SkillMatrixDriftError,
    assert_branch_choice_in_matrix,
    canonical_branches_for_skill,
    canonical_skill_ids,
)

LOG = logging.getLogger(__name__)


ConnFactory = Callable[[], Any]
"""Callable returning an async context manager yielding a connection."""


# ── Constants from ADR-0008 §"Skill leveling (W12)" ────────────────

MAX_SKILL_LEVEL = 5
# W12.4 (OP-173): the branching-tree fork level per ADR-0008
# §"Skill leveling (W12)" — at Lv 3 every base skill forks into two
# branches declared in ``skill_matrix.yaml`` and ``lock_branch_choice``
# persists the operator's immutable pick.
BRANCH_LOCK_LEVEL = 3
# OP-2503 — RPG.W12 skill-xp-accrual EPIC S2 base token per delivery.
# Deliberately NOT :data:`backend.agents.xp_engine.BASE_TASK_XP` (=100):
# feeding 100 through ``compute_xp_delta`` produces 300 first-time /
# 100 repeat, which instantly max-levels a skill against the
# ``25 / 100 / 250 / 600`` curve. 25 is the ADR-0008 "task-success
# token" so a single delivery lands 75 XP first-time / 25 repeat and
# a real grind is required to progress. Callers of
# :func:`award_skill_xp_from_env` (and its sync wrapper) MUST NOT
# pre-multiply via ``skill_xp_delta_for`` — the store applies the
# outcome / tier / first-time multiplier exactly once.
BASE_SKILL_XP = 25
TEACH_LEVEL = 5
TEACH_COOLDOWN_DAYS = 7
TEACH_INJECTION_XP = 25
IDLE_BEFORE_DECAY_DAYS = 30
DECAY_RATE_PER_WEEK = 0.05
LEVEL_OVERFLOW_GUARD_XP = 10 ** 9

OutcomeStatus = str  # ``success`` | ``partial`` | ``fail``

# W12.2 (OP-171 / OP-1381): cumulative XP thresholds to *reach* a given level
# per ADR-0008 §"Skill leveling (W12)". ``LEVEL_THRESHOLDS[L]`` is the
# task-success-token count at which the agent transitions into Lv ``L``;
# Lv 1 starts at 0. The five curve points are ``25 / 100 / 250 / 600 /
# 1500`` -- the first four are Lv 2-5 entry thresholds and the fifth
# (:data:`LEVEL_5_CAP_THRESHOLD`) is the Lv-5 decay floor, not an entry
# gate (a Lv-5 row's xp can grow past 1500 but :func:`apply_decay`
# refuses to drop it below ``LEVEL_5_CAP_THRESHOLD - 1``).
LEVEL_THRESHOLDS: Mapping[int, int] = MappingProxyType(
    {
        1: 0,
        2: 25,
        3: 100,
        4: 250,
        5: 600,
    }
)
LEVEL_5_CAP_THRESHOLD = 1500


OUTCOME_MULTIPLIERS: Mapping[str, float] = MappingProxyType(
    {
        "success": 1.0,
        "partial": 0.4,
        "fail": 0.1,
        "failed": 0.1,
    }
)

TIER_L_PLUS_MULTIPLIER = 2.0
FIRST_TIME_SKILL_MULTIPLIER = 3.0
ANTI_GRIND_MULTIPLIER = 0.2


# ── Per-level mastery-effect catalog (Lv 2-5) ──────────────────────

MASTERY_EFFECTS_BY_LEVEL: Mapping[int, tuple[str, ...]] = MappingProxyType(
    {
        2: ("extended_thinking",),
        3: ("parallel_subtask",),
        4: ("prompt_overhead",),
        5: ("teach_other_agent",),
    }
)
LEVEL_UNLOCKS = MASTERY_EFFECTS_BY_LEVEL


# ── Errors ──────────────────────────────────────────────────────────


class SkillLevelingError(RuntimeError):
    """Base class for W12 skill-leveling errors."""


class SkillBranchAlreadyLocked(SkillLevelingError):
    """Raised when an operator attempts to re-write an already-locked branch."""


class SkillIdNotInMatrix(SkillLevelingError):
    """Raised when an operation references a skill_id absent from the matrix."""


class XPDeltaNegativeWithoutDecayContext(SkillLevelingError):
    """Raised when a non-decay caller passes a negative XP delta."""


class LevelComputeOverflow(SkillLevelingError):
    """Raised when XP exceeds the defensive overflow guard."""


class TeachCooldownActive(SkillLevelingError):
    """Raised when a Lv-5 teach is attempted within the 7-day cooldown."""


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillState:
    """Durable per-``(agent_id, skill_id)`` row in ``agent_skill_state``."""

    agent_id: str
    skill_id: str
    level: int
    xp: int
    branch_choice: str | None
    last_active_at: datetime
    last_taught_at: datetime | None = None
    mastery_effects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(self, "skill_id", _required("skill_id", self.skill_id))
        if self.level < 1 or self.level > MAX_SKILL_LEVEL:
            raise ValueError(f"level must be 1..{MAX_SKILL_LEVEL}")
        if self.xp < 0:
            raise ValueError("xp must be >= 0")
        if self.branch_choice is not None:
            object.__setattr__(
                self,
                "branch_choice",
                _required("branch_choice", self.branch_choice),
            )
        object.__setattr__(self, "last_active_at", _utc(self.last_active_at))
        if self.last_taught_at is not None:
            object.__setattr__(self, "last_taught_at", _utc(self.last_taught_at))
        object.__setattr__(
            self,
            "mastery_effects",
            tuple(
                str(effect).strip()
                for effect in self.mastery_effects
                if str(effect).strip()
            ),
        )


@dataclass(frozen=True)
class SkillXpAward:
    """Return value of :func:`award_skill_xp`.

    The ``branch_choice_required`` flag is the W12.4 (OP-173) signal
    consumed by the Character Card "Skills" tab: ``True`` means the
    row sits at or above :data:`BRANCH_LOCK_LEVEL` with no branch
    locked yet, so the UI should render the two-option picker against
    ``skill_matrix.yaml``.
    """

    agent_id: str
    skill_id: str
    xp_delta: int
    previous_xp: int
    new_xp: int
    previous_level: int
    new_level: int
    unlocks: tuple[str, ...]
    branch_choice_required: bool


@dataclass(frozen=True)
class AtomicSkillXpAward:
    """Return value of :meth:`PostgresSkillStateStore.award_delta_atomic`.

    Mirrors the caller-visible surface of :class:`SkillXpAward` but adds
    the ``inserted`` flag derived from Postgres's ``(xmax = 0)`` idiom,
    so callers can tell whether this delivery won the insert race or
    folded into a concurrent update. All XP / level fields are
    recomputed from the returned absolute ``new_xp`` — no separate
    read-modify-write against the row is performed.
    """

    agent_id: str
    skill_id: str
    applied_delta: int
    new_xp: int
    new_level: int
    branch_choice: str | None
    branch_choice_required: bool
    inserted: bool


# ── Pure helpers ────────────────────────────────────────────────────


def compute_level(xp: int) -> int:
    """Return the W12 skill level (1-5) for ``xp``.

    Implements the W12.2 (OP-171 / OP-1381) curve: walks :data:`LEVEL_THRESHOLDS`
    in ascending order and returns the highest level whose threshold
    has been met. With the canonical ``25 / 100 / 250 / 600`` entry
    points this maps 0-24 → Lv 1, 25-99 → Lv 2, 100-249 → Lv 3,
    250-599 → Lv 4, ≥600 → Lv 5.

    Defensive: XP above :data:`LEVEL_OVERFLOW_GUARD_XP` raises
    :class:`LevelComputeOverflow`. Below that the curve is capped at
    Lv 5 (per ADR-0008 §"Skill leveling (W12)").
    """
    if isinstance(xp, bool) or not isinstance(xp, int):
        raise TypeError("xp must be an int")
    if xp < 0:
        raise ValueError("xp must be >= 0")
    if xp >= LEVEL_OVERFLOW_GUARD_XP:
        raise LevelComputeOverflow(
            f"xp {xp} exceeds defensive overflow guard {LEVEL_OVERFLOW_GUARD_XP}"
        )
    level = 1
    for candidate, threshold in LEVEL_THRESHOLDS.items():
        if xp >= threshold:
            level = candidate
    return level


def next_level_threshold(level: int) -> int:
    """Return the XP threshold for the next level above ``level``.

    Reads the W12.2 (OP-171 / OP-1381) curve in :data:`LEVEL_THRESHOLDS`. For
    Lv 5 (the cap) returns the configurable
    :data:`LEVEL_5_CAP_THRESHOLD` value; this is what
    :func:`_decay_xp_floor` uses to clamp decay against demotion at
    the cap.
    """
    if level < 1 or level > MAX_SKILL_LEVEL:
        raise ValueError(f"level must be 1..{MAX_SKILL_LEVEL}")
    if level == MAX_SKILL_LEVEL:
        return LEVEL_5_CAP_THRESHOLD
    return LEVEL_THRESHOLDS[level + 1]


def unlocks_for_level(level: int) -> tuple[str, ...]:
    """Return the unlock flags that fire on reaching ``level``."""
    if level < 1 or level > MAX_SKILL_LEVEL:
        raise ValueError(f"level must be 1..{MAX_SKILL_LEVEL}")
    return MASTERY_EFFECTS_BY_LEVEL.get(level, ())


def mastery_effects_for_skill(skill_id: str) -> Mapping[int, tuple[str, ...]]:
    """Return the Lv 2-5 mastery-effect table for one canonical skill."""
    _assert_skill_id_in_matrix(skill_id)
    return MASTERY_EFFECTS_BY_LEVEL


def mastery_effects_table() -> Mapping[str, Mapping[int, tuple[str, ...]]]:
    """Return the mastery-effect table for every canonical RPG skill."""
    return MappingProxyType(
        {
            skill_id: MASTERY_EFFECTS_BY_LEVEL
            for skill_id in sorted(canonical_skill_ids())
        }
    )


def unlocks_crossed(previous_level: int, new_level: int) -> tuple[str, ...]:
    """Return unlock flags that fire when moving from ``previous_level`` → ``new_level``."""
    if new_level <= previous_level:
        return ()
    crossed: list[str] = []
    for lvl in range(previous_level + 1, new_level + 1):
        crossed.extend(unlocks_for_level(lvl))
    return tuple(crossed)


def compute_xp_delta(
    base_delta: int,
    outcome: str,
    *,
    tier_l_plus: bool = False,
    first_time_skill_use: bool = False,
    same_task_hash_within_24h: bool = False,
) -> int:
    """Apply ADR-0008 outcome + anti-grind multipliers to ``base_delta``.

    The ``base_delta`` argument is the *positive* nominal XP award the
    runner has already computed (e.g. from the W4.1 XP engine); this
    helper applies the W12-specific stacking rules and returns the
    integer delta to persist.
    """
    if isinstance(base_delta, bool) or not isinstance(base_delta, int):
        raise TypeError("base_delta must be an int")
    if base_delta < 0:
        raise XPDeltaNegativeWithoutDecayContext(
            "negative base_delta is reserved for the decay path; use apply_decay"
        )
    multiplier = OUTCOME_MULTIPLIERS.get(_clean_outcome(outcome), 0.0)
    if tier_l_plus:
        multiplier *= TIER_L_PLUS_MULTIPLIER
    if first_time_skill_use:
        multiplier *= FIRST_TIME_SKILL_MULTIPLIER
    if same_task_hash_within_24h:
        multiplier *= ANTI_GRIND_MULTIPLIER
    return max(0, int(base_delta * multiplier))


def _decay_xp_floor(level: int) -> int:
    """Return the lowest XP value decay may produce for an agent at ``level``."""
    threshold = next_level_threshold(level)
    return max(LEVEL_THRESHOLDS[level], threshold - 1)


def apply_decay(
    xp: int,
    level: int,
    *,
    weeks_idle: int,
) -> int:
    """Return the post-decay XP for an idle skill row.

    ADR-0008 §"Skill leveling (W12)": 5% per week, capped at
    ``next_level_threshold - 1`` (cannot demote a level, only erode
    toward it). Lv-5 rows are capped at :data:`LEVEL_5_CAP_THRESHOLD`.
    """
    if weeks_idle <= 0:
        return xp
    decayed = int(xp * ((1.0 - DECAY_RATE_PER_WEEK) ** weeks_idle))
    return max(decayed, _decay_xp_floor(level))


# ── Store protocol + in-memory store ───────────────────────────────


class SkillStateStore(Protocol):
    async def get_state(
        self, agent_id: str, skill_id: str
    ) -> SkillState | None: ...
    async def list_states(self, agent_id: str) -> tuple[SkillState, ...]: ...
    async def upsert_state(self, state: SkillState) -> SkillState: ...
    async def iter_idle_states(
        self, *, idle_since: datetime
    ) -> AsyncIterator[SkillState]: ...


class InMemorySkillStateStore:
    """Dev/test store. Production callers should use the Postgres store."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], SkillState] = {}

    async def get_state(
        self, agent_id: str, skill_id: str
    ) -> SkillState | None:
        return self._rows.get((agent_id, skill_id))

    async def list_states(self, agent_id: str) -> tuple[SkillState, ...]:
        return tuple(
            row for key, row in self._rows.items() if key[0] == agent_id
        )

    async def upsert_state(self, state: SkillState) -> SkillState:
        self._rows[(state.agent_id, state.skill_id)] = state
        return state

    async def iter_idle_states(
        self, *, idle_since: datetime
    ) -> AsyncIterator[SkillState]:
        for row in list(self._rows.values()):
            if row.last_active_at <= idle_since:
                yield row


# ── Postgres store ──────────────────────────────────────────────────


class PostgresSkillStateStore:
    """``agent_skill_state``-backed store (alembic 0226).

    The store writes the OP-1378 columns added after the original 0226
    migration: ``skill_xp / last_used_at / mastery_effects``. The earlier
    ``xp / last_active_at`` columns are still populated during rollout so
    older readers observe the same row values. Writes are full-row UPSERTs
    keyed by ``(agent_id, skill_id)`` so the caller does not have to know
    whether a row already exists.
    """

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def get_state(
        self, agent_id: str, skill_id: str
    ) -> SkillState | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_id, skill_id, level, skill_xp AS xp, branch_choice,
                       last_used_at AS last_active_at, last_taught_at,
                       mastery_effects
                FROM agent_skill_state
                WHERE agent_id = $1 AND skill_id = $2
                """,
                _required("agent_id", agent_id),
                _required("skill_id", skill_id),
            )
        return _row_to_state(row) if row else None

    async def list_states(self, agent_id: str) -> tuple[SkillState, ...]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, skill_id, level, skill_xp AS xp, branch_choice,
                       last_used_at AS last_active_at, last_taught_at,
                       mastery_effects
                FROM agent_skill_state
                WHERE agent_id = $1
                ORDER BY skill_id ASC
                """,
                _required("agent_id", agent_id),
            )
        return tuple(_row_to_state(row) for row in rows)

    async def upsert_state(self, state: SkillState) -> SkillState:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_skill_state (
                    agent_id, skill_id, level, xp, skill_xp, branch_choice,
                    last_active_at, last_used_at, last_taught_at, mastery_effects,
                    created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $4, $5, $6, $6, $7, $8, NOW(), NOW())
                ON CONFLICT (agent_id, skill_id) DO UPDATE
                    SET level = EXCLUDED.level,
                        xp = EXCLUDED.xp,
                        skill_xp = EXCLUDED.skill_xp,
                        branch_choice = EXCLUDED.branch_choice,
                        last_active_at = EXCLUDED.last_active_at,
                        last_used_at = EXCLUDED.last_used_at,
                        last_taught_at = EXCLUDED.last_taught_at,
                        mastery_effects = EXCLUDED.mastery_effects,
                        updated_at = NOW()
                RETURNING agent_id, skill_id, level, skill_xp AS xp, branch_choice,
                          last_used_at AS last_active_at, last_taught_at,
                          mastery_effects
                """,
                state.agent_id,
                state.skill_id,
                state.level,
                state.xp,
                state.branch_choice,
                state.last_active_at,
                state.last_taught_at,
                _mastery_effects_for_level(state.level),
            )
        return _row_to_state(row)

    async def iter_idle_states(
        self, *, idle_since: datetime
    ) -> AsyncIterator[SkillState]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, skill_id, level, skill_xp AS xp, branch_choice,
                       last_used_at AS last_active_at, last_taught_at,
                       mastery_effects
                FROM agent_skill_state
                WHERE last_used_at <= $1
                """,
                _utc(idle_since),
            )
        for row in rows:
            yield _row_to_state(row)

    async def award_delta_atomic(
        self,
        agent_id: str,
        skill_id: str,
        *,
        base_delta: int,
        outcome: str,
        tier_l_plus: bool = False,
    ) -> AtomicSkillXpAward:
        """Award a skill-XP delta in ONE atomic Postgres statement.

        The read-modify-write in :func:`award_skill_xp` (``get_state`` then
        ``upsert_state``) loses updates when two runner slots deliver the
        same ``(agent_id, skill_id)`` at once: both slots read no row,
        both compute ``first_time_skill_use=True``, and the last upsert
        clobbers the first. This helper folds the award into a single
        ``INSERT ... ON CONFLICT DO UPDATE`` so the loser of the insert
        race adds to the winner's row instead of overwriting it.

        Because the applied delta depends on first-time-skill-use and
        first-time-skill-use depends on insert-vs-update, both variants
        are precomputed and threaded into the statement: ``first_delta``
        lands in the ``VALUES`` clause (insert branch); ``repeat_delta``
        lands in ``DO UPDATE SET skill_xp = ... + $repeat_delta``. The
        RETURNING clause exposes ``(xmax = 0) AS inserted`` so the
        caller can tell which branch fired.

        ``level`` + :attr:`branch_choice_required` are recomputed in
        Python from the returned absolute ``skill_xp``. On the
        conflict (repeat-award) path the ``ON CONFLICT DO UPDATE``
        clause only bumps ``skill_xp`` / ``xp``, so a second
        ``UPDATE ... SET level = ...`` on the same connection re-syncs
        the denormalised ``level`` column against
        :func:`compute_level` of the new absolute ``skill_xp``.
        Without that follow-up, once a repeat delivery crosses a
        level threshold the row's ``level`` would stay frozen at the
        pre-award value while ``skill_xp`` continued to accrue. xp
        remains the source of truth; level is derived, so a
        two-statement xp-add-then-level-sync within one connection
        is acceptable per the S2b invariant ("stored level ==
        compute_level(stored skill_xp) after any award").
        """
        _assert_skill_id_in_matrix(skill_id)
        agent_id = _required("agent_id", agent_id)
        skill_id = _required("skill_id", skill_id)
        if isinstance(base_delta, bool) or not isinstance(base_delta, int):
            raise TypeError("base_delta must be an int")
        if base_delta < 0:
            raise XPDeltaNegativeWithoutDecayContext(
                "award_delta_atomic does not accept negative deltas; "
                "use decay_idle_skills"
            )

        first_delta = compute_xp_delta(
            base_delta,
            outcome,
            tier_l_plus=tier_l_plus,
            first_time_skill_use=True,
        )
        repeat_delta = compute_xp_delta(
            base_delta,
            outcome,
            tier_l_plus=tier_l_plus,
            first_time_skill_use=False,
        )
        insert_level = compute_level(first_delta)
        insert_mastery = list(_mastery_effects_for_level(insert_level))

        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO agent_skill_state (
                    agent_id, skill_id, level, xp, skill_xp, branch_choice,
                    last_active_at, last_used_at, last_taught_at, mastery_effects,
                    created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $4, NULL, NOW(), NOW(), NULL, $5,
                        NOW(), NOW())
                ON CONFLICT (agent_id, skill_id) DO UPDATE
                    SET skill_xp = agent_skill_state.skill_xp + $6,
                        xp = agent_skill_state.skill_xp + $6,
                        last_active_at = NOW(),
                        last_used_at = NOW(),
                        updated_at = NOW()
                RETURNING skill_xp, branch_choice, (xmax = 0) AS inserted
                """,
                agent_id,
                skill_id,
                insert_level,
                first_delta,
                insert_mastery,
                repeat_delta,
            )

            new_xp = int(row["skill_xp"])
            inserted = bool(row["inserted"])
            branch_choice = row["branch_choice"]
            new_level = compute_level(new_xp)

            if not inserted:
                # OP-2504 (S2b): the ON CONFLICT DO UPDATE clause above only
                # bumps skill_xp / xp — level is left frozen at whatever the
                # prior row stored. Re-sync it against the new absolute
                # skill_xp so a repeat award that crosses a threshold does
                # not leave the denormalised level column stale.
                await conn.execute(
                    """
                    UPDATE agent_skill_state
                    SET level = $1,
                        updated_at = NOW()
                    WHERE agent_id = $2 AND skill_id = $3
                    """,
                    new_level,
                    agent_id,
                    skill_id,
                )

        applied_delta = first_delta if inserted else repeat_delta
        branch_required = (
            new_level >= BRANCH_LOCK_LEVEL and branch_choice is None
        )
        return AtomicSkillXpAward(
            agent_id=agent_id,
            skill_id=skill_id,
            applied_delta=applied_delta,
            new_xp=new_xp,
            new_level=new_level,
            branch_choice=branch_choice,
            branch_choice_required=branch_required,
            inserted=inserted,
        )


# ── Public operations ───────────────────────────────────────────────


async def award_skill_xp(
    store: SkillStateStore,
    agent_id: str,
    skill_id: str,
    *,
    delta: int,
    outcome: str,
    tier_l_plus: bool = False,
    first_time_skill_use: bool = False,
    same_task_hash_within_24h: bool = False,
    now: datetime | None = None,
) -> SkillXpAward:
    """Apply a skill-XP delta with W12 stacking rules.

    Returns a :class:`SkillXpAward` describing the delta that was
    persisted, the new total XP / level, and which Lv-unlock flags
    crossed the level threshold during this award. ``outcome``
    selects the multiplier per :data:`OUTCOME_MULTIPLIERS`.

    Raises
    ------
    SkillIdNotInMatrix
        if ``skill_id`` is not declared in ``skill_matrix.yaml``.
    XPDeltaNegativeWithoutDecayContext
        if ``delta`` is negative (decay goes through :func:`decay_idle_skills`).
    """
    _assert_skill_id_in_matrix(skill_id)
    agent_id = _required("agent_id", agent_id)
    skill_id = _required("skill_id", skill_id)
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise TypeError("delta must be an int")
    if delta < 0:
        raise XPDeltaNegativeWithoutDecayContext(
            "award_skill_xp does not accept negative deltas; use decay_idle_skills"
        )

    applied_delta = compute_xp_delta(
        delta,
        outcome,
        tier_l_plus=tier_l_plus,
        first_time_skill_use=first_time_skill_use,
        same_task_hash_within_24h=same_task_hash_within_24h,
    )
    when = _utc(now or datetime.now(timezone.utc))

    existing = await store.get_state(agent_id, skill_id)
    previous_xp = existing.xp if existing else 0
    previous_level = existing.level if existing else 1
    branch_choice = existing.branch_choice if existing else None
    last_taught_at = existing.last_taught_at if existing else None

    new_xp = previous_xp + applied_delta
    new_level = compute_level(new_xp)
    unlocks = unlocks_crossed(previous_level, new_level)

    state = SkillState(
        agent_id=agent_id,
        skill_id=skill_id,
        level=new_level,
        xp=new_xp,
        branch_choice=branch_choice,
        last_active_at=when,
        last_taught_at=last_taught_at,
        mastery_effects=_mastery_effects_for_level(new_level),
    )
    await store.upsert_state(state)

    branch_required = new_level >= BRANCH_LOCK_LEVEL and branch_choice is None
    return SkillXpAward(
        agent_id=agent_id,
        skill_id=skill_id,
        xp_delta=applied_delta,
        previous_xp=previous_xp,
        new_xp=new_xp,
        previous_level=previous_level,
        new_level=new_level,
        unlocks=unlocks,
        branch_choice_required=branch_required,
    )


async def lock_branch_choice(
    store: SkillStateStore,
    agent_id: str,
    skill_id: str,
    branch: str,
    *,
    now: datetime | None = None,
) -> SkillState:
    """Persist the immutable Lv-3 branch fork for ``(agent_id, skill_id)``.

    W12.4 (OP-173) — implements the "operator picks from Character
    Card" half of the branching-tree contract: the Character Card
    "Skills" tab surfaces ``branch_choice_required`` when the row is
    at or above :data:`BRANCH_LOCK_LEVEL`, the operator chooses one of
    the two declared branches from ``skill_matrix.yaml``, and this
    helper persists the pick on the existing ``agent_skill_state`` row
    (alembic 0226).

    Idempotent on the *same* branch (returns the existing row unchanged)
    and refuses to overwrite a *different* branch with
    :class:`SkillBranchAlreadyLocked` — operators must spawn a new
    instance for a fresh fork. The branch string is validated against
    the canonical ``skill_matrix.yaml`` by
    :func:`backend.agents.skill_matrix.assert_branch_choice_in_matrix`
    before the upsert; drift raises
    :class:`backend.agents.skill_matrix.SkillMatrixDriftError`.
    """
    _assert_skill_id_in_matrix(skill_id)
    assert_branch_choice_in_matrix(skill_id, branch)
    agent_id = _required("agent_id", agent_id)
    skill_id = _required("skill_id", skill_id)
    branch = _required("branch", branch)
    when = _utc(now or datetime.now(timezone.utc))

    existing = await store.get_state(agent_id, skill_id)
    if existing is None:
        new = SkillState(
            agent_id=agent_id,
            skill_id=skill_id,
            level=1,
            xp=0,
            branch_choice=branch,
            last_active_at=when,
        )
        return await store.upsert_state(new)
    if existing.branch_choice is not None and existing.branch_choice != branch:
        raise SkillBranchAlreadyLocked(
            f"branch already locked to {existing.branch_choice!r} for "
            f"({agent_id}, {skill_id}); refusing to re-write to {branch!r}"
        )
    if existing.branch_choice == branch:
        return existing
    updated = replace(existing, branch_choice=branch)
    return await store.upsert_state(updated)


async def teach_other_agent(
    store: SkillStateStore,
    teacher_id: str,
    student_id: str,
    skill_id: str,
    *,
    student_level_at_or_below: int = 2,
    now: datetime | None = None,
) -> SkillXpAward:
    """Inject a one-shot +25 XP teach from a Lv-5 holder.

    Per ADR-0008 §"Skill leveling (W12)" the teach is only valid when
    the student is at or below Lv 2; the cooldown window is enforced
    on the *teacher* row (7-day :data:`TEACH_COOLDOWN_DAYS`).

    Raises
    ------
    SkillLevelingError
        if the teacher is not Lv 5 on this skill or the student is too
        high to receive a teach.
    TeachCooldownActive
        if the teacher already taught this skill within
        :data:`TEACH_COOLDOWN_DAYS`.
    """
    _assert_skill_id_in_matrix(skill_id)
    teacher_id = _required("teacher_id", teacher_id)
    student_id = _required("student_id", student_id)
    if teacher_id == student_id:
        raise SkillLevelingError("teacher and student must be different agents")
    when = _utc(now or datetime.now(timezone.utc))

    teacher = await store.get_state(teacher_id, skill_id)
    if teacher is None or teacher.level < TEACH_LEVEL:
        raise SkillLevelingError(
            f"teacher {teacher_id!r} must be Lv {TEACH_LEVEL} on {skill_id!r}"
        )
    if teacher.last_taught_at is not None:
        delta = when - teacher.last_taught_at
        if delta < timedelta(days=TEACH_COOLDOWN_DAYS):
            raise TeachCooldownActive(
                f"teacher {teacher_id!r} last taught {teacher.last_taught_at}; "
                f"{TEACH_COOLDOWN_DAYS}-day cooldown active"
            )

    student = await store.get_state(student_id, skill_id)
    student_level = student.level if student else 1
    if student_level > student_level_at_or_below:
        raise SkillLevelingError(
            f"student {student_id!r} is Lv {student_level}; teach is only valid "
            f"for Lv <= {student_level_at_or_below}"
        )

    award = await award_skill_xp(
        store,
        student_id,
        skill_id,
        delta=TEACH_INJECTION_XP,
        outcome="success",
        now=when,
    )
    # Mark the teacher's cooldown — separate UPSERT (no XP change).
    await store.upsert_state(replace(teacher, last_taught_at=when))
    return award


async def decay_idle_skills(
    store: SkillStateStore,
    *,
    now: datetime | None = None,
    idle_days: int = IDLE_BEFORE_DECAY_DAYS,
) -> tuple[SkillState, ...]:
    """Apply the 5%/week decay to every skill idle for ``idle_days``.

    Returns the rows touched by this sweep (caller can log or audit).
    The decay is monotonic per :func:`apply_decay` — the level field is
    left untouched, only ``xp`` regresses, never below the floor at
    ``next_level_threshold - 1``.
    """
    when = _utc(now or datetime.now(timezone.utc))
    idle_since = when - timedelta(days=idle_days)
    touched: list[SkillState] = []
    async for row in store.iter_idle_states(idle_since=idle_since):
        weeks_idle = max(0, (when - row.last_active_at).days // 7)
        new_xp = apply_decay(row.xp, row.level, weeks_idle=weeks_idle)
        if new_xp == row.xp:
            continue
        updated = replace(row, xp=new_xp)
        await store.upsert_state(updated)
        touched.append(updated)
    return tuple(touched)


# ── Runner delivery integration (OP-2503) ──────────────────────────


@asynccontextmanager
async def _connect_skill_state_from_env() -> AsyncIterator[Any]:
    """Open a single asyncpg connection from ``OMNISIGHT_DATABASE_URL``.

    Mirrors :func:`backend.agents.character_card._connect_character_card_from_env`
    so the runner's per-delivery skill-XP write reuses the same env-DSN
    contract used by the character-card + telemetry paths:
    ``OMNISIGHT_DATABASE_URL`` wins over the generic ``DATABASE_URL``;
    ``asyncpg`` is imported lazily so non-DB-backed deployments do not
    need the dependency at import time.
    """
    dsn = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("OMNISIGHT_DATABASE_URL or DATABASE_URL is not set")
    from backend.db_url import parse as parse_db_url

    parsed = parse_db_url(dsn)
    if not parsed.is_postgres:
        raise RuntimeError("agent_skill_state requires a Postgres database URL")
    import asyncpg  # type: ignore[import-not-found]

    conn = await asyncpg.connect(**parsed.asyncpg_connect_kwargs())
    try:
        yield conn
    finally:
        await conn.close()


def _skill_xp_conn_passthrough(conn: Any) -> ConnFactory:
    """Wrap an already-open connection as a ``ConnFactory`` for the store."""

    @asynccontextmanager
    async def _factory_cm() -> AsyncIterator[Any]:
        yield conn

    def _factory() -> Any:
        return _factory_cm()

    return _factory


async def award_skill_xp_from_env(
    *,
    agent_id: str,
    skill_id: str,
    tier: str | None = None,
    base_delta: int = BASE_SKILL_XP,
    outcome_status: str = "success",
    conn_factory: ConnFactory | None = None,
) -> AtomicSkillXpAward | None:
    """Award per-skill XP against the env-configured Postgres DSN (OP-2503).

    The runner delivery hook: on a successful push of a character-owned
    ticket that carries a valid in-guild ``skill:<skill_id>`` label, the
    finalizer calls this so the persona's ``agent_skill_state`` row for
    that skill accrues XP through :meth:`PostgresSkillStateStore.award_delta_atomic`
    (S1b, OP-2501) — one atomic ``INSERT ... ON CONFLICT DO UPDATE`` so
    two concurrent slots on the same ``(agent_id, skill_id)`` cannot
    lose an award.

    ``base_delta`` defaults to :data:`BASE_SKILL_XP` (=25), NOT
    :data:`backend.agents.xp_engine.BASE_TASK_XP` (=100) — feeding 100
    would instant-max a skill against the 25/100/250/600 curve. Callers
    MUST NOT pre-multiply the delta via ``skill_xp_delta_for``: the
    store applies the outcome / tier-L+ / first-time multiplier exactly
    once. Fail-open — returns ``None`` on any error (no DSN, asyncpg
    unavailable, schema not deployed, connection error) so a delivery
    is never wedged by the RPG write; ``conn_factory`` is a test seam.
    """
    try:
        if conn_factory is None:
            async with _connect_skill_state_from_env() as conn:
                store = PostgresSkillStateStore(_skill_xp_conn_passthrough(conn))
                return await store.award_delta_atomic(
                    agent_id,
                    skill_id,
                    base_delta=base_delta,
                    outcome=outcome_status,
                    tier_l_plus=str(tier or "").upper() in {"L", "X"},
                )
        store = PostgresSkillStateStore(conn_factory)
        return await store.award_delta_atomic(
            agent_id,
            skill_id,
            base_delta=base_delta,
            outcome=outcome_status,
            tier_l_plus=str(tier or "").upper() in {"L", "X"},
        )
    except Exception as exc:  # noqa: BLE001 — delivery must never wedge on RPG XP
        LOG.warning(
            "SkillXpAwardFailed agent_id=%s skill_id=%s outcome=%s err=%s",
            agent_id,
            skill_id,
            outcome_status,
            exc,
        )
        return None


def award_skill_xp_sync(
    *,
    agent_id: str,
    skill_id: str,
    tier: str | None = None,
    base_delta: int = BASE_SKILL_XP,
    outcome_status: str = "success",
) -> AtomicSkillXpAward | None:
    """Synchronous wrapper for ``auto-runner-jira.py`` (mirrors the *_sync shape).

    Drives a fresh :func:`asyncio.run` loop so the sync dispatch path
    can call into the asyncpg-backed store without restructuring the
    runner. Must not be invoked from inside an already-running event
    loop.
    """
    return asyncio.run(
        award_skill_xp_from_env(
            agent_id=agent_id,
            skill_id=skill_id,
            tier=tier,
            base_delta=base_delta,
            outcome_status=outcome_status,
        )
    )


# ── Internal helpers ────────────────────────────────────────────────


@asynccontextmanager
async def _acquire(factory: ConnFactory) -> AsyncIterator[Any]:
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_state(row: Any) -> SkillState:
    return SkillState(
        agent_id=row["agent_id"],
        skill_id=row["skill_id"],
        level=int(row["level"]),
        xp=int(row["xp"]),
        branch_choice=row["branch_choice"],
        last_active_at=row["last_active_at"],
        last_taught_at=row["last_taught_at"],
        mastery_effects=_coerce_mastery_effects(row["mastery_effects"]),
    )


def _mastery_effects_for_level(level: int) -> tuple[str, ...]:
    effects: list[str] = []
    for current_level in range(2, level + 1):
        effects.extend(unlocks_for_level(current_level))
    return tuple(effects)


def _coerce_mastery_effects(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return (value,) if value else ()
        if isinstance(decoded, list):
            return tuple(str(item) for item in decoded)
        return ()
    return tuple(str(item) for item in value)


def _assert_skill_id_in_matrix(skill_id: str) -> None:
    try:
        canonical = canonical_skill_ids()
    except SkillMatrixDriftError:  # pragma: no cover — drift guard already raised
        raise
    if skill_id not in canonical:
        raise SkillIdNotInMatrix(
            f"skill_id {skill_id!r} is not declared in skill_matrix.yaml"
        )


def _clean_outcome(outcome: Any) -> str:
    if not isinstance(outcome, str):
        raise TypeError("outcome must be a string")
    clean = outcome.strip().lower()
    if clean not in OUTCOME_MULTIPLIERS:
        allowed = ", ".join(sorted(OUTCOME_MULTIPLIERS))
        raise ValueError(f"unsupported outcome {outcome!r}; expected one of {allowed}")
    return clean


def _required(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    return clean


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# Make the canonical branch helper re-exportable from this module so
# UI callers don't need to know it lives in skill_matrix.
__all__ = [
    "ANTI_GRIND_MULTIPLIER",
    "AtomicSkillXpAward",
    "BASE_SKILL_XP",
    "BRANCH_LOCK_LEVEL",
    "DECAY_RATE_PER_WEEK",
    "FIRST_TIME_SKILL_MULTIPLIER",
    "IDLE_BEFORE_DECAY_DAYS",
    "InMemorySkillStateStore",
    "LEVEL_5_CAP_THRESHOLD",
    "LEVEL_OVERFLOW_GUARD_XP",
    "LEVEL_THRESHOLDS",
    "LEVEL_UNLOCKS",
    "LevelComputeOverflow",
    "MASTERY_EFFECTS_BY_LEVEL",
    "MAX_SKILL_LEVEL",
    "OUTCOME_MULTIPLIERS",
    "PostgresSkillStateStore",
    "SkillBranchAlreadyLocked",
    "SkillIdNotInMatrix",
    "SkillLevelingError",
    "SkillState",
    "SkillStateStore",
    "SkillXpAward",
    "TEACH_COOLDOWN_DAYS",
    "TEACH_INJECTION_XP",
    "TEACH_LEVEL",
    "TIER_L_PLUS_MULTIPLIER",
    "TeachCooldownActive",
    "XPDeltaNegativeWithoutDecayContext",
    "apply_decay",
    "award_skill_xp",
    "award_skill_xp_from_env",
    "award_skill_xp_sync",
    "canonical_branches_for_skill",
    "compute_level",
    "compute_xp_delta",
    "decay_idle_skills",
    "lock_branch_choice",
    "mastery_effects_for_skill",
    "mastery_effects_table",
    "next_level_threshold",
    "teach_other_agent",
    "unlocks_crossed",
    "unlocks_for_level",
]

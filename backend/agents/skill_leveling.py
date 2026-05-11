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

Module-global state audit (per project SOP)
-------------------------------------------
This module defines constants, dataclasses, exception classes, and
``Protocol``/store classes only. Mutable state lives inside the
injected store. No clock reads at import time; the few callers that
need "now" pass it explicitly so unit tests stay deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from backend.agents.skill_matrix import (
    SkillMatrixDriftError,
    assert_branch_choice_in_matrix,
    canonical_branches_for_skill,
    canonical_skill_ids,
)


ConnFactory = Callable[[], Any]
"""Callable returning an async context manager yielding a connection."""


# ── Constants from ADR-0008 §"Skill leveling (W12)" ────────────────

MAX_SKILL_LEVEL = 5
BRANCH_LOCK_LEVEL = 3
TEACH_LEVEL = 5
TEACH_COOLDOWN_DAYS = 7
TEACH_INJECTION_XP = 25
IDLE_BEFORE_DECAY_DAYS = 30
DECAY_RATE_PER_WEEK = 0.05
LEVEL_OVERFLOW_GUARD_XP = 10 ** 9

OutcomeStatus = str  # ``success`` | ``partial`` | ``fail``

# Cumulative XP thresholds to *reach* a given level. ``LEVEL_THRESHOLDS[L]``
# is the XP at which the agent transitions into Lv ``L``. Lv 1 starts at 0.
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


# ── Per-level unlock catalog (Lv 2-5) ──────────────────────────────

LEVEL_UNLOCKS: Mapping[int, tuple[str, ...]] = MappingProxyType(
    {
        2: ("extended_thinking_enabled",),
        3: ("parallel_subtask_enabled",),
        4: ("prompt_overhead_reduced",),
        5: ("teach_other_agent",),
    }
)


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


@dataclass(frozen=True)
class SkillXpAward:
    """Return value of :func:`award_skill_xp`."""

    agent_id: str
    skill_id: str
    xp_delta: int
    previous_xp: int
    new_xp: int
    previous_level: int
    new_level: int
    unlocks: tuple[str, ...]
    branch_choice_required: bool


# ── Pure helpers ────────────────────────────────────────────────────


def compute_level(xp: int) -> int:
    """Return the W12 skill level (1-5) for ``xp``.

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

    For Lv 5 (the cap) returns the configurable
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
    return LEVEL_UNLOCKS.get(level, ())


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

    The store expects the columns introduced in the 0226 migration:
    ``agent_id / skill_id / level / xp / branch_choice / last_active_at /
    last_taught_at``. Writes are full-row UPSERTs keyed by
    ``(agent_id, skill_id)`` so the caller does not have to know whether
    a row already exists.
    """

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def get_state(
        self, agent_id: str, skill_id: str
    ) -> SkillState | None:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT agent_id, skill_id, level, xp, branch_choice,
                       last_active_at, last_taught_at
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
                SELECT agent_id, skill_id, level, xp, branch_choice,
                       last_active_at, last_taught_at
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
                    agent_id, skill_id, level, xp, branch_choice,
                    last_active_at, last_taught_at, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
                ON CONFLICT (agent_id, skill_id) DO UPDATE
                    SET level = EXCLUDED.level,
                        xp = EXCLUDED.xp,
                        branch_choice = EXCLUDED.branch_choice,
                        last_active_at = EXCLUDED.last_active_at,
                        last_taught_at = EXCLUDED.last_taught_at,
                        updated_at = NOW()
                RETURNING agent_id, skill_id, level, xp, branch_choice,
                          last_active_at, last_taught_at
                """,
                state.agent_id,
                state.skill_id,
                state.level,
                state.xp,
                state.branch_choice,
                state.last_active_at,
                state.last_taught_at,
            )
        return _row_to_state(row)

    async def iter_idle_states(
        self, *, idle_since: datetime
    ) -> AsyncIterator[SkillState]:
        async with _acquire(self._factory) as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, skill_id, level, xp, branch_choice,
                       last_active_at, last_taught_at
                FROM agent_skill_state
                WHERE last_active_at <= $1
                """,
                _utc(idle_since),
            )
        for row in rows:
            yield _row_to_state(row)


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

    Idempotent on the *same* branch (returns the existing row unchanged)
    and refuses to overwrite a *different* branch with
    :class:`SkillBranchAlreadyLocked` — operators must spawn a new
    instance for a fresh fork.
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
    )


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
    "canonical_branches_for_skill",
    "compute_level",
    "compute_xp_delta",
    "decay_idle_skills",
    "lock_branch_choice",
    "next_level_threshold",
    "teach_other_agent",
    "unlocks_crossed",
    "unlocks_for_level",
]

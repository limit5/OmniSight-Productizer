"""RPG.W4.1 -- deterministic XP award calculation.

This module implements the ADR-0008 XP curve and outcome multipliers without
persistence. Callers pass the observed task outcome and receive the XP delta
that should be applied to the agent's character card by a later storage layer.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only. It performs no database access,
no registry mutation, and no clock reads; anti-grinding is represented by an
explicit task-outcome flag supplied by the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

OutcomeStatus = Literal["success", "partial", "fail", "failed"]

BASE_TASK_XP = 100
MAX_LEVEL = 80
LEVEL_CURVE_EXPONENT = 1.4
TIER_L_PLUS_MULTIPLIER = 2.0
FIRST_TIME_SKILL_MULTIPLIER = 3.0
DUPLICATE_TASK_MULTIPLIER = 0.2

OUTCOME_MULTIPLIERS: Mapping[str, float] = MappingProxyType(
    {
        "success": 1.0,
        "partial": 0.4,
        "fail": 0.1,
        "failed": 0.1,
    }
)


@dataclass(frozen=True)
class TaskOutcome:
    """Normalized task completion signal used by :func:`award_xp`."""

    status: OutcomeStatus
    base_xp: int = BASE_TASK_XP
    tier_l_plus: bool = False
    first_time_skill_use: bool = False
    duplicate_task_within_24h: bool = False


@dataclass(frozen=True)
class XpDelta:
    """Computed XP award for one agent/task outcome."""

    agent_id: str
    xp: int
    status: str
    base_xp: int
    multiplier: float


def award_xp(
    agent_id: str,
    task_outcome: TaskOutcome | Mapping[str, Any] | Any,
) -> XpDelta:
    """Return the XP delta for ``agent_id`` and ``task_outcome``.

    ``task_outcome`` may be a :class:`TaskOutcome`, a mapping, or a dataclass /
    object with matching attributes. The calculation mirrors ADR-0008:
    outcome multiplier, optional Tier-L+ multiplier, optional first-time skill
    multiplier, and optional duplicate-task anti-grinding multiplier.
    """
    clean_agent_id = _clean_agent_id(agent_id)
    outcome = _normalise_task_outcome(task_outcome)
    multiplier = _outcome_multiplier(outcome)
    xp = max(0, math.floor(outcome.base_xp * multiplier))
    return XpDelta(
        agent_id=clean_agent_id,
        xp=xp,
        status=outcome.status,
        base_xp=outcome.base_xp,
        multiplier=round(multiplier, 6),
    )


def level_threshold(level: int) -> int:
    """Return cumulative XP required to reach ``level`` per ADR-0008."""
    if not isinstance(level, int):
        raise TypeError("level must be an int")
    if level < 1:
        raise ValueError("level must be >= 1")
    return math.ceil(BASE_TASK_XP * (level ** LEVEL_CURVE_EXPONENT))


def level_for_xp(total_xp: int) -> int:
    """Return the capped character level for cumulative ``total_xp``."""
    if not isinstance(total_xp, int):
        raise TypeError("total_xp must be an int")
    if total_xp < 0:
        raise ValueError("total_xp must be >= 0")

    level = 1
    while level < MAX_LEVEL and total_xp >= level_threshold(level + 1):
        level += 1
    return level


def _clean_agent_id(agent_id: str) -> str:
    if not isinstance(agent_id, str):
        raise TypeError("agent_id must be a string")
    clean = agent_id.strip()
    if not clean:
        raise ValueError("agent_id must be non-empty")
    return clean


def _normalise_task_outcome(
    task_outcome: TaskOutcome | Mapping[str, Any] | Any,
) -> TaskOutcome:
    if isinstance(task_outcome, TaskOutcome):
        outcome = task_outcome
    else:
        values = _outcome_values(task_outcome)
        status = values.get("status", values.get("outcome"))
        if status is None:
            raise ValueError("task_outcome must include status or outcome")
        outcome = TaskOutcome(
            status=_clean_status(status),
            base_xp=_clean_base_xp(values.get("base_xp", BASE_TASK_XP)),
            tier_l_plus=bool(
                values.get("tier_l_plus", values.get("tier_l_or_higher", False))
            ),
            first_time_skill_use=bool(values.get("first_time_skill_use", False)),
            duplicate_task_within_24h=bool(
                values.get(
                    "duplicate_task_within_24h",
                    values.get("same_task_hash_within_24h", False),
                )
            ),
        )
    _validate_outcome(outcome)
    return outcome


def _outcome_values(task_outcome: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(task_outcome, Mapping):
        return task_outcome
    if is_dataclass(task_outcome):
        return {
            field.name: getattr(task_outcome, field.name)
            for field in fields(task_outcome)
        }
    return {
        name: getattr(task_outcome, name)
        for name in (
            "status",
            "outcome",
            "base_xp",
            "tier_l_plus",
            "tier_l_or_higher",
            "first_time_skill_use",
            "duplicate_task_within_24h",
            "same_task_hash_within_24h",
        )
        if hasattr(task_outcome, name)
    }


def _clean_status(status: Any) -> OutcomeStatus:
    if not isinstance(status, str):
        raise TypeError("task_outcome status must be a string")
    clean = status.strip().lower()
    if clean not in OUTCOME_MULTIPLIERS:
        allowed = ", ".join(sorted(OUTCOME_MULTIPLIERS))
        raise ValueError(
            f"unsupported task_outcome status {status!r}; expected one of {allowed}"
        )
    return clean  # type: ignore[return-value]


def _clean_base_xp(base_xp: Any) -> int:
    if isinstance(base_xp, bool) or not isinstance(base_xp, int):
        raise TypeError("task_outcome base_xp must be an int")
    if base_xp < 0:
        raise ValueError("task_outcome base_xp must be >= 0")
    return base_xp


def _validate_outcome(outcome: TaskOutcome) -> None:
    _clean_status(outcome.status)
    _clean_base_xp(outcome.base_xp)


def _outcome_multiplier(outcome: TaskOutcome) -> float:
    multiplier = OUTCOME_MULTIPLIERS[outcome.status]
    if outcome.tier_l_plus:
        multiplier *= TIER_L_PLUS_MULTIPLIER
    if outcome.first_time_skill_use:
        multiplier *= FIRST_TIME_SKILL_MULTIPLIER
    if outcome.duplicate_task_within_24h:
        multiplier *= DUPLICATE_TASK_MULTIPLIER
    return multiplier


__all__ = [
    "BASE_TASK_XP",
    "DUPLICATE_TASK_MULTIPLIER",
    "FIRST_TIME_SKILL_MULTIPLIER",
    "LEVEL_CURVE_EXPONENT",
    "MAX_LEVEL",
    "OUTCOME_MULTIPLIERS",
    "TIER_L_PLUS_MULTIPLIER",
    "OutcomeStatus",
    "TaskOutcome",
    "XpDelta",
    "award_xp",
    "level_for_xp",
    "level_threshold",
]

"""RPG.W15.1 -- deterministic buff/debuff registry.

ADR-0008 keeps RPG progression mostly stateless at calculation boundaries:
callers pass observed task/quota context and receive deterministic modifiers.
This module mirrors that shape for the W15 buff system. It owns immutable buff
definitions and pure helpers only; durable buff state remains a later storage
concern.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and ``MappingProxyType`` registries
only. It performs no database access, no registry mutation, and no clock reads;
callers supply all timing and streak inputs explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Literal, Mapping

BuffKind = Literal["xp", "routing_priority"]

FRESH_TOKENS_BUFF_ID = "fresh_tokens"
WELL_RESTED_BUFF_ID = "well_rested"
STREAK_BUFF_ID = "streak"
CAP_WARNING_BUFF_ID = "cap_warning"

FRESH_TOKENS_XP_MULTIPLIER = 1.10
FRESH_TOKENS_DURATION_SECONDS = 30 * 60
WELL_RESTED_XP_MULTIPLIER = 1.20
WELL_RESTED_IDLE_SECONDS = 4 * 60 * 60
STREAK_XP_MULTIPLIER = 1.05
STREAK_SUCCESS_COUNT = 5
CAP_WARNING_ROUTING_PRIORITY_MULTIPLIER = 0.80
CAP_WARNING_QUOTA_RATIO = 0.10


@dataclass(frozen=True)
class BuffDefinition:
    """Immutable RPG buff/debuff metadata."""

    buff_id: str
    display_name: str
    kind: BuffKind
    multiplier: float
    summary: str


@dataclass(frozen=True)
class BuffContext:
    """Explicit runtime context used to evaluate active W15 buffs.

    ``consecutive_successes`` is the caller's post-task success count when
    evaluating XP awards. ``last_task_completed_at`` is the task completion
    timestamp before the current task started, so Well-Rested applies only to
    the first task after an idle gap.
    """

    now: datetime
    last_rolling_5h_reset_at: datetime | None = None
    last_task_completed_at: datetime | None = None
    consecutive_successes: int = 0
    remaining_quota_ratio: float | None = None


BUFF_DEFINITIONS: Mapping[str, BuffDefinition] = MappingProxyType(
    {
        FRESH_TOKENS_BUFF_ID: BuffDefinition(
            buff_id=FRESH_TOKENS_BUFF_ID,
            display_name="Fresh Tokens",
            kind="xp",
            multiplier=FRESH_TOKENS_XP_MULTIPLIER,
            summary="Rolling 5h quota reset grants +10% XP for 30 minutes.",
        ),
        WELL_RESTED_BUFF_ID: BuffDefinition(
            buff_id=WELL_RESTED_BUFF_ID,
            display_name="Well-Rested",
            kind="xp",
            multiplier=WELL_RESTED_XP_MULTIPLIER,
            summary="Idle for at least 4 hours grants +20% XP on the next task.",
        ),
        STREAK_BUFF_ID: BuffDefinition(
            buff_id=STREAK_BUFF_ID,
            display_name="Streak",
            kind="xp",
            multiplier=STREAK_XP_MULTIPLIER,
            summary="Five consecutive successful tasks grant +5% XP per task.",
        ),
        CAP_WARNING_BUFF_ID: BuffDefinition(
            buff_id=CAP_WARNING_BUFF_ID,
            display_name="Cap Warning",
            kind="routing_priority",
            multiplier=CAP_WARNING_ROUTING_PRIORITY_MULTIPLIER,
            summary="Quota below 10% applies -20% routing priority.",
        ),
    }
)


class UnknownBuffError(KeyError):
    """Raised when a buff id is absent from the RPG buff registry."""


def get_buff_definition(buff_id: str) -> BuffDefinition:
    """Return the registry definition for ``buff_id``."""

    key = _clean_buff_id(buff_id)
    try:
        return BUFF_DEFINITIONS[key]
    except KeyError as exc:
        raise UnknownBuffError(f"No RPG buff definition for buff_id {key!r}") from exc


def list_buff_definitions() -> tuple[BuffDefinition, ...]:
    """Return all W15 buff definitions in stable registry order."""

    return tuple(BUFF_DEFINITIONS[buff_id] for buff_id in BUFF_DEFINITIONS)


def active_buffs_for_context(context: BuffContext) -> tuple[BuffDefinition, ...]:
    """Return buffs active for the caller-supplied runtime ``context``."""

    _validate_context(context)
    active: list[BuffDefinition] = []
    if _fresh_tokens_active(context):
        active.append(BUFF_DEFINITIONS[FRESH_TOKENS_BUFF_ID])
    if _well_rested_active(context):
        active.append(BUFF_DEFINITIONS[WELL_RESTED_BUFF_ID])
    if _streak_active(context):
        active.append(BUFF_DEFINITIONS[STREAK_BUFF_ID])
    if _cap_warning_active(context):
        active.append(BUFF_DEFINITIONS[CAP_WARNING_BUFF_ID])
    return tuple(active)


def active_buff_ids_for_context(context: BuffContext) -> tuple[str, ...]:
    """Return active buff ids for the caller-supplied runtime ``context``."""

    return tuple(buff.buff_id for buff in active_buffs_for_context(context))


def xp_multiplier_for_context(context: BuffContext) -> float:
    """Return the combined XP multiplier for active XP buffs."""

    return xp_multiplier_for_buff_ids(active_buff_ids_for_context(context))


def xp_multiplier_for_buff_ids(buff_ids: tuple[str, ...]) -> float:
    """Return the combined XP multiplier for explicit active buff ids."""

    multiplier = 1.0
    for buff_id in buff_ids:
        buff = get_buff_definition(buff_id)
        if buff.kind == "xp":
            multiplier *= buff.multiplier
    return multiplier


def routing_priority_multiplier_for_context(context: BuffContext) -> float:
    """Return the combined routing-priority multiplier for active debuffs."""

    multiplier = 1.0
    for buff in active_buffs_for_context(context):
        if buff.kind == "routing_priority":
            multiplier *= buff.multiplier
    return multiplier


def routing_priority_multiplier_for_quota_ratio(
    remaining_quota_ratio: float,
) -> float:
    """Return routing-priority multiplier for one remaining quota ratio."""

    context = BuffContext(
        now=datetime(1970, 1, 1, tzinfo=timezone.utc),
        remaining_quota_ratio=remaining_quota_ratio,
    )
    return routing_priority_multiplier_for_context(context)


def _fresh_tokens_active(context: BuffContext) -> bool:
    if context.last_rolling_5h_reset_at is None:
        return False
    reset_at = _utc(context.last_rolling_5h_reset_at)
    elapsed = (_utc(context.now) - reset_at).total_seconds()
    return 0 <= elapsed <= FRESH_TOKENS_DURATION_SECONDS


def _well_rested_active(context: BuffContext) -> bool:
    if context.last_task_completed_at is None:
        return False
    completed_at = _utc(context.last_task_completed_at)
    idle_seconds = (_utc(context.now) - completed_at).total_seconds()
    return idle_seconds >= WELL_RESTED_IDLE_SECONDS


def _streak_active(context: BuffContext) -> bool:
    return context.consecutive_successes >= STREAK_SUCCESS_COUNT


def _cap_warning_active(context: BuffContext) -> bool:
    if context.remaining_quota_ratio is None:
        return False
    return context.remaining_quota_ratio < CAP_WARNING_QUOTA_RATIO


def _validate_context(context: BuffContext) -> None:
    _utc(context.now)
    if context.last_rolling_5h_reset_at is not None:
        _utc(context.last_rolling_5h_reset_at)
    if context.last_task_completed_at is not None:
        _utc(context.last_task_completed_at)
    if context.consecutive_successes < 0:
        raise ValueError("consecutive_successes must be >= 0")
    if (
        context.remaining_quota_ratio is not None
        and context.remaining_quota_ratio < 0
    ):
        raise ValueError("remaining_quota_ratio must be >= 0")


def _clean_buff_id(buff_id: str) -> str:
    if not isinstance(buff_id, str):
        raise TypeError("buff_id must be a string")
    clean = buff_id.strip()
    if not clean:
        raise ValueError("buff_id must be non-empty")
    return clean


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _assert_registry_complete() -> None:
    expected = {
        FRESH_TOKENS_BUFF_ID,
        WELL_RESTED_BUFF_ID,
        STREAK_BUFF_ID,
        CAP_WARNING_BUFF_ID,
    }
    missing = expected - set(BUFF_DEFINITIONS)
    if missing:
        raise RuntimeError(
            "RPG buff registry is incomplete; missing definitions for "
            f"buff ids: {sorted(missing)}"
        )


_assert_registry_complete()


__all__ = [
    "BUFF_DEFINITIONS",
    "CAP_WARNING_BUFF_ID",
    "CAP_WARNING_QUOTA_RATIO",
    "CAP_WARNING_ROUTING_PRIORITY_MULTIPLIER",
    "FRESH_TOKENS_BUFF_ID",
    "FRESH_TOKENS_DURATION_SECONDS",
    "FRESH_TOKENS_XP_MULTIPLIER",
    "STREAK_BUFF_ID",
    "STREAK_SUCCESS_COUNT",
    "STREAK_XP_MULTIPLIER",
    "WELL_RESTED_BUFF_ID",
    "WELL_RESTED_IDLE_SECONDS",
    "WELL_RESTED_XP_MULTIPLIER",
    "BuffContext",
    "BuffDefinition",
    "BuffKind",
    "UnknownBuffError",
    "active_buff_ids_for_context",
    "active_buffs_for_context",
    "get_buff_definition",
    "list_buff_definitions",
    "routing_priority_multiplier_for_context",
    "routing_priority_multiplier_for_quota_ratio",
    "xp_multiplier_for_buff_ids",
    "xp_multiplier_for_context",
]

"""RPG.W15.2 -- deterministic debuff registry.

ADR-0008 keeps RPG progression mostly stateless at calculation boundaries:
callers pass observed task/retraining context and receive deterministic
modifiers. This module mirrors the W15 buff registry shape for debuffs. It owns
immutable debuff definitions and pure helpers only; durable debuff state remains
a later storage concern.

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

DebuffKind = Literal["xp", "routing_weight"]
DebuffOutcomeStatus = Literal["success", "partial", "fail", "failed"]

BURNOUT_DEBUFF_ID = "burnout"
STALE_MEMORY_DEBUFF_ID = "stale_memory"

BURNOUT_XP_MULTIPLIER = 0.85
BURNOUT_FAILURE_COUNT = 3
STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER = 0.90
STALE_MEMORY_IDLE_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class DebuffDefinition:
    """Immutable RPG debuff metadata."""

    debuff_id: str
    display_name: str
    kind: DebuffKind
    multiplier: float
    summary: str


@dataclass(frozen=True)
class DebuffContext:
    """Explicit runtime context used to evaluate active W15 debuffs.

    ``consecutive_failures`` is the caller's current failure streak. A success
    clears Burnout even if the caller still has a stale pre-success failure
    count. The ``last_retrained_at`` timestamp is supplied by the caller for
    Stale Memory.
    """

    consecutive_failures: int = 0
    outcome_status: DebuffOutcomeStatus | None = None
    now: datetime | None = None
    last_retrained_at: datetime | None = None


DEBUFF_DEFINITIONS: Mapping[str, DebuffDefinition] = MappingProxyType(
    {
        BURNOUT_DEBUFF_ID: DebuffDefinition(
            debuff_id=BURNOUT_DEBUFF_ID,
            display_name="Burnout",
            kind="xp",
            multiplier=BURNOUT_XP_MULTIPLIER,
            summary="Three consecutive failed tasks apply -15% XP until success.",
        ),
        STALE_MEMORY_DEBUFF_ID: DebuffDefinition(
            debuff_id=STALE_MEMORY_DEBUFF_ID,
            display_name="Stale Memory",
            kind="routing_weight",
            multiplier=STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER,
            summary="No retraining for 30 days applies -10% routing weight.",
        ),
    }
)


class UnknownDebuffError(KeyError):
    """Raised when a debuff id is absent from the RPG debuff registry."""


def get_debuff_definition(debuff_id: str) -> DebuffDefinition:
    """Return the registry definition for ``debuff_id``."""

    key = _clean_debuff_id(debuff_id)
    try:
        return DEBUFF_DEFINITIONS[key]
    except KeyError as exc:
        raise UnknownDebuffError(
            f"No RPG debuff definition for debuff_id {key!r}"
        ) from exc


def list_debuff_definitions() -> tuple[DebuffDefinition, ...]:
    """Return all W15 debuff definitions in stable registry order."""

    return tuple(DEBUFF_DEFINITIONS[debuff_id] for debuff_id in DEBUFF_DEFINITIONS)


def active_debuffs_for_context(context: DebuffContext) -> tuple[DebuffDefinition, ...]:
    """Return debuffs active for the caller-supplied runtime ``context``."""

    _validate_context(context)
    active: list[DebuffDefinition] = []
    if _burnout_active(context):
        active.append(DEBUFF_DEFINITIONS[BURNOUT_DEBUFF_ID])
    if _stale_memory_active(context):
        active.append(DEBUFF_DEFINITIONS[STALE_MEMORY_DEBUFF_ID])
    return tuple(active)


def active_debuff_ids_for_context(context: DebuffContext) -> tuple[str, ...]:
    """Return active debuff ids for the caller-supplied runtime ``context``."""

    return tuple(debuff.debuff_id for debuff in active_debuffs_for_context(context))


def xp_multiplier_for_context(context: DebuffContext) -> float:
    """Return the combined XP multiplier for active XP debuffs."""

    return xp_multiplier_for_debuff_ids(active_debuff_ids_for_context(context))


def xp_multiplier_for_debuff_ids(debuff_ids: tuple[str, ...]) -> float:
    """Return the combined XP multiplier for explicit active debuff ids."""

    multiplier = 1.0
    for debuff_id in debuff_ids:
        debuff = get_debuff_definition(debuff_id)
        if debuff.kind == "xp":
            multiplier *= debuff.multiplier
    return multiplier


def routing_weight_multiplier_for_context(context: DebuffContext) -> float:
    """Return the combined routing-weight multiplier for active debuffs."""

    multiplier = 1.0
    for debuff in active_debuffs_for_context(context):
        if debuff.kind == "routing_weight":
            multiplier *= debuff.multiplier
    return multiplier


def routing_weight_multiplier_for_last_retrained_at(
    now: datetime,
    last_retrained_at: datetime | None,
) -> float:
    """Return routing-weight multiplier for one retraining timestamp."""

    context = DebuffContext(now=now, last_retrained_at=last_retrained_at)
    return routing_weight_multiplier_for_context(context)


def _burnout_active(context: DebuffContext) -> bool:
    if (
        context.outcome_status is not None
        and _clean_outcome_status(context.outcome_status) == "success"
    ):
        return False
    return context.consecutive_failures >= BURNOUT_FAILURE_COUNT


def _stale_memory_active(context: DebuffContext) -> bool:
    if context.now is None or context.last_retrained_at is None:
        return False
    elapsed = (_utc(context.now) - _utc(context.last_retrained_at)).total_seconds()
    return elapsed >= STALE_MEMORY_IDLE_SECONDS


def _validate_context(context: DebuffContext) -> None:
    if context.consecutive_failures < 0:
        raise ValueError("consecutive_failures must be >= 0")
    if context.outcome_status is not None:
        _clean_outcome_status(context.outcome_status)
    if context.now is not None:
        _utc(context.now)
    if context.last_retrained_at is not None:
        _utc(context.last_retrained_at)


def _clean_debuff_id(debuff_id: str) -> str:
    if not isinstance(debuff_id, str):
        raise TypeError("debuff_id must be a string")
    clean = debuff_id.strip()
    if not clean:
        raise ValueError("debuff_id must be non-empty")
    return clean


def _clean_outcome_status(status: str) -> DebuffOutcomeStatus:
    if not isinstance(status, str):
        raise TypeError("outcome_status must be a string")
    clean = status.strip().lower()
    if clean not in ("success", "partial", "fail", "failed"):
        raise ValueError(
            "outcome_status must be one of: failed, fail, partial, success"
        )
    return clean  # type: ignore[return-value]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _assert_registry_complete() -> None:
    expected = {
        BURNOUT_DEBUFF_ID,
        STALE_MEMORY_DEBUFF_ID,
    }
    missing = expected - set(DEBUFF_DEFINITIONS)
    if missing:
        raise RuntimeError(
            "RPG debuff registry is incomplete; missing definitions for "
            f"debuff ids: {sorted(missing)}"
        )


_assert_registry_complete()


__all__ = [
    "BURNOUT_DEBUFF_ID",
    "BURNOUT_FAILURE_COUNT",
    "BURNOUT_XP_MULTIPLIER",
    "DEBUFF_DEFINITIONS",
    "STALE_MEMORY_DEBUFF_ID",
    "STALE_MEMORY_IDLE_SECONDS",
    "STALE_MEMORY_ROUTING_WEIGHT_MULTIPLIER",
    "DebuffContext",
    "DebuffDefinition",
    "DebuffKind",
    "DebuffOutcomeStatus",
    "UnknownDebuffError",
    "active_debuff_ids_for_context",
    "active_debuffs_for_context",
    "get_debuff_definition",
    "list_debuff_definitions",
    "routing_weight_multiplier_for_context",
    "routing_weight_multiplier_for_last_retrained_at",
    "xp_multiplier_for_context",
    "xp_multiplier_for_debuff_ids",
]

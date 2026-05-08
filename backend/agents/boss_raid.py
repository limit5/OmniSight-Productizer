"""RPG.W21.1 -- quarterly boss-task eligibility gate.

ADR 0008 defines W21 boss raids as time-gated, high-stakes RPG tasks. This
module keeps the W21.1 gate backend-local and persistence-free: callers pass
the task shape plus party member levels and receive a deterministic eligibility
decision that later routing or storage boundaries can enforce.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Mapping, Sequence


BOSS_RAID_CADENCE = "quarterly"
BOSS_RAID_MIN_PARTY_SIZE = 4
BOSS_RAID_MIN_LEVEL = 50


@dataclass(frozen=True)
class BossRaidPartyMember:
    """One agent's level signal for a W21.1 boss-task party."""

    agent_id: str
    level: int


@dataclass(frozen=True)
class BossRaidEligibility:
    """Deterministic W21.1 gate result for one candidate boss task."""

    eligible: bool
    cadence: str
    required_level: int
    required_party_size: int
    qualified_party_size: int
    qualified_agent_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def boss_raid_eligibility(
    party_members: Sequence[BossRaidPartyMember | Mapping[str, Any] | Any],
    *,
    cadence: str = BOSS_RAID_CADENCE,
    large_refactor: bool = True,
    cross_cutting_upgrade: bool = True,
    required_level: int = BOSS_RAID_MIN_LEVEL,
    required_party_size: int = BOSS_RAID_MIN_PARTY_SIZE,
) -> BossRaidEligibility:
    """Return whether a task satisfies the W21.1 boss-raid gate.

    A W21.1 boss task must be quarterly, be a large refactor or cross-cutting
    upgrade, and have at least ``required_party_size`` unique party members at
    ``required_level`` or above.
    """

    clean_cadence = _required("cadence", cadence).lower()
    clean_required_level = _clean_level(required_level, field="required_level")
    clean_required_party_size = _clean_required_party_size(required_party_size)
    clean_large_refactor = _clean_bool(large_refactor, field="large_refactor")
    clean_cross_cutting = _clean_bool(
        cross_cutting_upgrade,
        field="cross_cutting_upgrade",
    )
    members = tuple(_normalise_party_member(member) for member in party_members)
    qualified = _qualified_unique_members(members, clean_required_level)

    reasons: list[str] = []
    if clean_cadence != BOSS_RAID_CADENCE:
        reasons.append(f"cadence must be {BOSS_RAID_CADENCE!r}")
    if not (clean_large_refactor or clean_cross_cutting):
        reasons.append("task must be a large refactor or cross-cutting upgrade")
    if len(qualified) < clean_required_party_size:
        reasons.append(
            "qualified Lv "
            f"{clean_required_level}+ party size must be >= "
            f"{clean_required_party_size}"
        )

    return BossRaidEligibility(
        eligible=not reasons,
        cadence=clean_cadence,
        required_level=clean_required_level,
        required_party_size=clean_required_party_size,
        qualified_party_size=len(qualified),
        qualified_agent_ids=tuple(member.agent_id for member in qualified),
        reasons=tuple(reasons),
    )


def _qualified_unique_members(
    members: tuple[BossRaidPartyMember, ...],
    required_level: int,
) -> tuple[BossRaidPartyMember, ...]:
    seen: set[str] = set()
    qualified: list[BossRaidPartyMember] = []
    for member in members:
        if member.agent_id in seen:
            continue
        seen.add(member.agent_id)
        if member.level >= required_level:
            qualified.append(member)
    return tuple(qualified)


def _normalise_party_member(
    member: BossRaidPartyMember | Mapping[str, Any] | Any,
) -> BossRaidPartyMember:
    if isinstance(member, BossRaidPartyMember):
        return BossRaidPartyMember(
            agent_id=_required("agent_id", member.agent_id),
            level=_clean_level(member.level, field="level"),
        )

    values = _member_values(member)
    agent_id = values.get("agent_id", values.get("id"))
    level = values.get("level")
    if agent_id is None:
        raise ValueError("party member must include agent_id or id")
    if level is None:
        raise ValueError("party member must include level")
    return BossRaidPartyMember(
        agent_id=_required("agent_id", agent_id),
        level=_clean_level(level, field="level"),
    )


def _member_values(member: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(member, Mapping):
        return member
    if is_dataclass(member):
        return {
            field.name: getattr(member, field.name)
            for field in fields(member)
        }
    return {
        name: getattr(member, name)
        for name in ("agent_id", "id", "level")
        if hasattr(member, name)
    }


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


def _clean_level(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 1:
        raise ValueError(f"{field} must be >= 1")
    return value


def _clean_required_party_size(value: Any) -> int:
    size = _clean_level(value, field="required_party_size")
    if size < 2:
        raise ValueError("required_party_size must be >= 2")
    return size


def _clean_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a bool")
    return value


__all__ = [
    "BOSS_RAID_CADENCE",
    "BOSS_RAID_MIN_LEVEL",
    "BOSS_RAID_MIN_PARTY_SIZE",
    "BossRaidEligibility",
    "BossRaidPartyMember",
    "boss_raid_eligibility",
]

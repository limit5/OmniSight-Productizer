"""RPG.W21.3 -- boss-only loot for talent points and card frames.

ADR 0008 defines W21 boss raids as time-gated, high-stakes RPG tasks. This
module keeps the boss-loot reward backend-local and persistence-free: callers
pass the completed raid signal and receive deterministic talent points plus a
cosmetic character-card frame that should be applied by a later storage
boundary.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


BOSS_RAID_TALENT_POINTS = 1
BOSS_RAID_FRAME_PREFIX = "boss_raid_frame"
BOSS_RAID_LOOT_SOURCE = "boss_raid"

_FRAME_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class BossRaidLootReward:
    """Exclusive W21.3 reward requested after one completed boss raid."""

    agent_id: str
    raid_id: str
    talent_points: int
    cosmetic_frame_id: str
    source: str = BOSS_RAID_LOOT_SOURCE


def boss_raid_loot_reward(
    *,
    agent_id: str,
    raid_id: str,
    boss_raid_completed: bool,
    boss_raid_eligible: bool = True,
    talent_points: int = BOSS_RAID_TALENT_POINTS,
) -> BossRaidLootReward | None:
    """Return boss-only loot after a completed eligible boss raid.

    Non-boss tasks, incomplete raids, or raids that failed the W21.1 eligibility
    gate return ``None`` so callers can evaluate the helper from a generic task
    completion path without accidentally granting exclusive loot.
    """

    if not _clean_bool(boss_raid_completed, field="boss_raid_completed"):
        return None
    if not _clean_bool(boss_raid_eligible, field="boss_raid_eligible"):
        return None

    clean_agent_id = _required("agent_id", agent_id)
    clean_raid_id = _required("raid_id", raid_id)
    clean_talent_points = _clean_talent_points(talent_points)
    return BossRaidLootReward(
        agent_id=clean_agent_id,
        raid_id=clean_raid_id,
        talent_points=clean_talent_points,
        cosmetic_frame_id=boss_raid_cosmetic_frame_id(clean_raid_id),
    )


def boss_raid_cosmetic_frame_id(raid_id: str) -> str:
    """Return the stable exclusive character-card frame id for ``raid_id``."""

    clean = _required("raid_id", raid_id).lower()
    slug = _FRAME_SLUG_RE.sub("_", clean).strip("_")
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]
    if not slug:
        slug = digest
    return f"{BOSS_RAID_FRAME_PREFIX}_{slug}_{digest}"


def _clean_bool(value: bool, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a bool")
    return value


def _clean_talent_points(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("talent_points must be an int")
    if value < 1:
        raise ValueError("talent_points must be >= 1")
    return value


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


__all__ = [
    "BOSS_RAID_FRAME_PREFIX",
    "BOSS_RAID_LOOT_SOURCE",
    "BOSS_RAID_TALENT_POINTS",
    "BossRaidLootReward",
    "boss_raid_cosmetic_frame_id",
    "boss_raid_loot_reward",
]

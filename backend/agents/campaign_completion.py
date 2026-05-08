"""RPG.W20.3 -- campaign-completion bonus XP and badge calculation.

ADR 0008 defines W20 campaigns as narrative wrappers over multi-task work.
This module keeps the completion reward backend-local and persistence-free:
callers pass campaign/task state and receive the XP delta plus deterministic
campaign badge that should be applied by a later storage boundary.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants and frozen dataclasses only. It
performs no database access, registry mutation, network I/O, or clock reads.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from backend.agents.xp_engine import BASE_TASK_XP, XpDelta


CAMPAIGN_COMPLETION_BONUS_MULTIPLIER = 2
CAMPAIGN_COMPLETION_BADGE_PREFIX = "campaign_complete"

_BADGE_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CampaignCompletionReward:
    """Reward requested when one RPG campaign reaches completion."""

    agent_id: str
    campaign_id: str
    campaign_title: str
    bonus_xp: int
    badge_id: str
    badge_name: str
    xp_delta: XpDelta


def campaign_completion_reward(
    *,
    agent_id: str,
    campaign_id: str,
    campaign_title: str,
    completed_tasks: int,
    total_tasks: int,
    base_xp: int = BASE_TASK_XP,
    bonus_multiplier: int = CAMPAIGN_COMPLETION_BONUS_MULTIPLIER,
) -> CampaignCompletionReward | None:
    """Return the completion reward once all campaign tasks are complete.

    Incomplete campaigns return ``None`` so callers can safely evaluate this
    after every task completion without double-applying a reward; durable
    uniqueness still belongs to the caller's badge / XP persistence boundary.
    """

    completed = _clean_task_count("completed_tasks", completed_tasks)
    total = _clean_task_count("total_tasks", total_tasks)
    if total < 1:
        raise ValueError("total_tasks must be >= 1")
    if completed > total:
        raise ValueError("completed_tasks must be <= total_tasks")
    if completed < total:
        return None

    clean_agent_id = _required("agent_id", agent_id)
    clean_campaign_id = _required("campaign_id", campaign_id)
    clean_campaign_title = _required("campaign_title", campaign_title)
    bonus_xp = _completion_bonus_xp(base_xp, bonus_multiplier)
    badge_id = campaign_completion_badge_id(clean_campaign_id)
    return CampaignCompletionReward(
        agent_id=clean_agent_id,
        campaign_id=clean_campaign_id,
        campaign_title=clean_campaign_title,
        bonus_xp=bonus_xp,
        badge_id=badge_id,
        badge_name=f"{clean_campaign_title} Finisher",
        xp_delta=XpDelta(
            agent_id=clean_agent_id,
            xp=bonus_xp,
            status="campaign_complete",
            base_xp=base_xp,
            multiplier=float(bonus_multiplier),
        ),
    )


def campaign_completion_badge_id(campaign_id: str) -> str:
    """Return the stable per-campaign badge id for ``campaign_id``."""

    clean = _required("campaign_id", campaign_id).lower()
    slug = _BADGE_SLUG_RE.sub("_", clean).strip("_")
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]
    if not slug:
        slug = digest
    return f"{CAMPAIGN_COMPLETION_BADGE_PREFIX}_{slug}_{digest}"


def _completion_bonus_xp(base_xp: int, bonus_multiplier: int) -> int:
    clean_base = _clean_task_count("base_xp", base_xp)
    clean_multiplier = _clean_task_count("bonus_multiplier", bonus_multiplier)
    if clean_multiplier < 1:
        raise ValueError("bonus_multiplier must be >= 1")
    return max(0, math.floor(clean_base * clean_multiplier))


def _clean_task_count(field: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} is required")
    return stripped


__all__ = [
    "CAMPAIGN_COMPLETION_BADGE_PREFIX",
    "CAMPAIGN_COMPLETION_BONUS_MULTIPLIER",
    "CampaignCompletionReward",
    "campaign_completion_badge_id",
    "campaign_completion_reward",
]

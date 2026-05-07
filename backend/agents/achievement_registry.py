"""RPG.W16.1 -- achievement milestone registry.

ADR-0008 defines W16 as the progressive achievement / badge layer. This module
keeps the first milestone table deliberately small and immutable: callers can
render or evaluate definitions without taking a dependency on a persistence
layer. Durable unlock state belongs to a later storage ticket.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants, frozen dataclasses, and read-only
mapping views only. It performs no database access, no clock reads, and no
runtime mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

AchievementMetric = Literal[
    "merged_pr_count",
    "zero_regression_streak",
    "agents_taught_count",
    "tier_l_plus_success_count",
    "first_time_skill_success_count",
]

AchievementCategory = Literal[
    "delivery",
    "quality",
    "mentorship",
    "challenge",
    "learning",
]


@dataclass(frozen=True)
class AchievementMilestone:
    """One operator-visible RPG achievement definition."""

    achievement_id: str
    display_name: str
    category: AchievementCategory
    metric: AchievementMetric
    threshold: int
    summary: str

    def __post_init__(self) -> None:
        _required("achievement_id", self.achievement_id)
        _required("display_name", self.display_name)
        _required("category", self.category)
        _required("metric", self.metric)
        _required("summary", self.summary)
        if self.threshold < 1:
            raise ValueError("threshold must be >= 1")


def _required(name: str, value: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} must be non-empty")
    return clean


ACHIEVEMENT_MILESTONES: Mapping[str, AchievementMilestone] = MappingProxyType(
    {
        "merged_pr_100": AchievementMilestone(
            achievement_id="merged_pr_100",
            display_name="100 PR Merged",
            category="delivery",
            metric="merged_pr_count",
            threshold=100,
            summary="Awarded after an agent has 100 accepted and merged PRs.",
        ),
        "zero_regression_streak_30": AchievementMilestone(
            achievement_id="zero_regression_streak_30",
            display_name="0 Regression Streak x30",
            category="quality",
            metric="zero_regression_streak",
            threshold=30,
            summary="Awarded after 30 consecutive completed tasks with no regression.",
        ),
        "taught_agents_5": AchievementMilestone(
            achievement_id="taught_agents_5",
            display_name="Taught 5 Agents",
            category="mentorship",
            metric="agents_taught_count",
            threshold=5,
            summary="Awarded after successful same-Guild skill teaching for five agents.",
        ),
        "tier_l_plus_success_10": AchievementMilestone(
            achievement_id="tier_l_plus_success_10",
            display_name="Tier L+ Veteran",
            category="challenge",
            metric="tier_l_plus_success_count",
            threshold=10,
            summary="Awarded after 10 successful Tier L+ assignments.",
        ),
        "first_time_skill_success_25": AchievementMilestone(
            achievement_id="first_time_skill_success_25",
            display_name="Fast Learner",
            category="learning",
            metric="first_time_skill_success_count",
            threshold=25,
            summary="Awarded after 25 successful first-time skill uses.",
        ),
    }
)


class UnknownAchievementMilestoneError(KeyError):
    """Raised when an achievement_id is absent from the registry."""


def get_achievement_milestone(achievement_id: str) -> AchievementMilestone:
    """Return the achievement definition for ``achievement_id``."""

    key = achievement_id.strip()
    try:
        return ACHIEVEMENT_MILESTONES[key]
    except KeyError as exc:
        raise UnknownAchievementMilestoneError(
            f"No RPG achievement milestone for achievement_id {key!r}"
        ) from exc


def list_achievement_milestones() -> tuple[AchievementMilestone, ...]:
    """Return all achievement milestones in stable registry order."""

    return tuple(ACHIEVEMENT_MILESTONES[key] for key in ACHIEVEMENT_MILESTONES)


def achievement_milestone_reached(
    achievement_id: str,
    metric_value: int,
) -> bool:
    """Whether ``metric_value`` satisfies the achievement threshold."""

    if isinstance(metric_value, bool) or not isinstance(metric_value, int):
        raise TypeError("metric_value must be an int")
    if metric_value < 0:
        raise ValueError("metric_value must be >= 0")
    milestone = get_achievement_milestone(achievement_id)
    return metric_value >= milestone.threshold


def _assert_registry_keys_match_ids() -> None:
    for key, milestone in ACHIEVEMENT_MILESTONES.items():
        if key != milestone.achievement_id:
            raise RuntimeError(
                "RPG achievement registry key does not match achievement_id: "
                f"{key!r} != {milestone.achievement_id!r}"
            )


_assert_registry_keys_match_ids()


__all__ = [
    "ACHIEVEMENT_MILESTONES",
    "AchievementCategory",
    "AchievementMetric",
    "AchievementMilestone",
    "UnknownAchievementMilestoneError",
    "achievement_milestone_reached",
    "get_achievement_milestone",
    "list_achievement_milestones",
]

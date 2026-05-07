"""RPG.W16.2 -- daily achievement auto-unlock scanner.

The W16.1 registry owns immutable achievement definitions. This module owns
the backend-only scan loop that evaluates caller-supplied per-agent metric
snapshots and asks the injected store to persist newly unlocked achievements.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants, frozen dataclasses, Protocols, and
functions only. It opens no database connections at module import time and
keeps durable unlock state inside the injected store. The daemon loop receives
its sleep/clock callables from the caller for deterministic tests and workers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

from backend.agents.achievement_registry import (
    AchievementMetric,
    achievement_milestone_reached,
    list_achievement_milestones,
)


DEFAULT_UNLOCK_SCAN_INTERVAL_SECONDS = 24 * 60 * 60
logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class AchievementMetricSnapshot:
    """Per-agent metric values used by one unlock scan."""

    agent_id: str
    metrics: Mapping[AchievementMetric, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        clean_metrics: dict[AchievementMetric, int] = {}
        for metric, value in self.metrics.items():
            if not isinstance(metric, str) or not metric.strip():
                raise ValueError("metric keys must be non-empty strings")
            clean_metrics[metric] = _non_negative_int(metric, value)
        object.__setattr__(self, "metrics", clean_metrics)


@dataclass(frozen=True)
class AchievementUnlock:
    """Unlock row requested by the scanner."""

    agent_id: str
    achievement_id: str
    metric: AchievementMetric
    metric_value: int
    threshold: int
    unlocked_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(
            self,
            "achievement_id",
            _required("achievement_id", self.achievement_id),
        )
        object.__setattr__(self, "metric", _required("metric", self.metric))
        object.__setattr__(
            self,
            "metric_value",
            _non_negative_int("metric_value", self.metric_value),
        )
        object.__setattr__(
            self,
            "threshold",
            _positive_int("threshold", self.threshold),
        )
        object.__setattr__(self, "unlocked_at", _utc(self.unlocked_at))


@dataclass(frozen=True)
class AchievementUnlockScanResult:
    """Summary of one achievement unlock scan."""

    scanned_agents: int
    checked_milestones: int
    newly_unlocked: tuple[AchievementUnlock, ...]


class AchievementUnlockStore(Protocol):
    """Storage boundary consumed by the auto-unlock scanner."""

    async def list_metric_snapshots(self) -> tuple[AchievementMetricSnapshot, ...]: ...

    async def has_achievement(
        self,
        agent_id: str,
        achievement_id: str,
    ) -> bool: ...

    async def unlock_achievement(self, unlock: AchievementUnlock) -> bool: ...


async def scan_achievement_unlocks(
    store: AchievementUnlockStore,
    *,
    now: Clock | None = None,
) -> AchievementUnlockScanResult:
    """Evaluate all achievement milestones once and persist new unlocks."""

    unlocked_at = _utc((now or _now_utc)())
    snapshots = await store.list_metric_snapshots()
    milestones = list_achievement_milestones()
    newly_unlocked: list[AchievementUnlock] = []

    for snapshot in snapshots:
        for milestone in milestones:
            metric_value = snapshot.metrics.get(milestone.metric, 0)
            if not achievement_milestone_reached(
                milestone.achievement_id,
                metric_value,
            ):
                continue
            already_unlocked = await store.has_achievement(
                snapshot.agent_id,
                milestone.achievement_id,
            )
            if already_unlocked:
                continue
            unlock = AchievementUnlock(
                agent_id=snapshot.agent_id,
                achievement_id=milestone.achievement_id,
                metric=milestone.metric,
                metric_value=metric_value,
                threshold=milestone.threshold,
                unlocked_at=unlocked_at,
            )
            if await store.unlock_achievement(unlock):
                newly_unlocked.append(unlock)

    return AchievementUnlockScanResult(
        scanned_agents=len(snapshots),
        checked_milestones=len(snapshots) * len(milestones),
        newly_unlocked=tuple(newly_unlocked),
    )


async def run_daily_unlock_loop(
    store: AchievementUnlockStore,
    *,
    interval_seconds: float = DEFAULT_UNLOCK_SCAN_INTERVAL_SECONDS,
    sleep: Sleep = asyncio.sleep,
    now: Clock | None = None,
    run_once_on_start: bool = True,
) -> None:
    """Run the achievement auto-unlock scan once per day until cancelled."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")

    if run_once_on_start:
        await _scan_and_log(store, now=now)

    while True:
        await sleep(interval_seconds)
        await _scan_and_log(store, now=now)


async def _scan_and_log(
    store: AchievementUnlockStore,
    *,
    now: Clock | None,
) -> None:
    try:
        result = await scan_achievement_unlocks(store, now=now)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("RPG achievement auto-unlock scan failed")
        return

    if result.newly_unlocked:
        logger.info(
            "RPG achievement auto-unlock scan: scanned=%s checked=%s unlocked=%s",
            result.scanned_agents,
            result.checked_milestones,
            len(result.newly_unlocked),
        )


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} must be non-empty")
    return clean


def _non_negative_int(field: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


def _positive_int(field: str, value: int) -> int:
    clean = _non_negative_int(field, value)
    if clean < 1:
        raise ValueError(f"{field} must be >= 1")
    return clean


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "DEFAULT_UNLOCK_SCAN_INTERVAL_SECONDS",
    "AchievementMetricSnapshot",
    "AchievementUnlock",
    "AchievementUnlockScanResult",
    "AchievementUnlockStore",
    "scan_achievement_unlocks",
    "run_daily_unlock_loop",
]

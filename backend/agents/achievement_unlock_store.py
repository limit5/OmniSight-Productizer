"""RPG.W16.3 -- durable achievement unlock store."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


AchievementRarity = Literal["bronze", "silver", "gold"]
ConnFactory = Callable[[], Any]


@dataclass(frozen=True)
class AchievementUnlockRow:
    """One durable achievement unlock row."""

    agent_id: str
    achievement_id: str
    earned_at: datetime
    rarity: AchievementRarity
    progress_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _required("agent_id", self.agent_id))
        object.__setattr__(
            self,
            "achievement_id",
            _required("achievement_id", self.achievement_id),
        )
        object.__setattr__(self, "earned_at", _utc(self.earned_at))
        object.__setattr__(self, "rarity", _rarity(self.rarity))
        object.__setattr__(
            self,
            "progress_label",
            _optional_required("progress_label", self.progress_label),
        )


class PostgresAchievementUnlockStore:
    """``agent_achievement_unlocks`` backed store."""

    def __init__(self, conn_factory: ConnFactory) -> None:
        self._factory = conn_factory

    async def has_achievement(self, agent_id: str, achievement_id: str) -> bool:
        async with _acquire(self._factory) as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM agent_achievement_unlocks
                WHERE agent_id = $1 AND achievement_id = $2
                """,
                _required("agent_id", agent_id),
                _required("achievement_id", achievement_id),
            )
        return row is not None

    async def unlock_achievement(self, unlock: Any) -> bool:
        await record_unlock(
            unlock.agent_id,
            unlock.achievement_id,
            unlock.unlocked_at,
            "bronze",
            conn_factory=self._factory,
        )
        return True

    async def record_unlock(
        self,
        agent_id: str,
        achievement_id: str,
        earned_at: datetime,
        rarity: AchievementRarity,
        progress_label: str | None = None,
    ) -> AchievementUnlockRow:
        return await record_unlock(
            agent_id,
            achievement_id,
            earned_at,
            rarity,
            progress_label=progress_label,
            conn_factory=self._factory,
        )

    async def list_unlocks(self, agent_id: str) -> tuple[AchievementUnlockRow, ...]:
        return await list_unlocks(agent_id, conn_factory=self._factory)


async def record_unlock(
    agent_id: str,
    achievement_id: str,
    earned_at: datetime,
    rarity: AchievementRarity,
    progress_label: str | None = None,
    *,
    conn_factory: ConnFactory | None = None,
) -> AchievementUnlockRow:
    """Idempotently persist one achievement unlock."""

    row = AchievementUnlockRow(
        agent_id=agent_id,
        achievement_id=achievement_id,
        earned_at=earned_at,
        rarity=rarity,
        progress_label=progress_label,
    )
    async with _acquire(conn_factory) as conn:
        saved = await conn.fetchrow(
            """
            INSERT INTO agent_achievement_unlocks (
                agent_id, achievement_id, earned_at, progress_label, rarity
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (agent_id, achievement_id) DO UPDATE
                SET achievement_id = agent_achievement_unlocks.achievement_id
            RETURNING agent_id, achievement_id, earned_at, progress_label, rarity
            """,
            row.agent_id,
            row.achievement_id,
            row.earned_at,
            row.progress_label,
            row.rarity,
        )
    return _row_to_unlock(saved)


async def list_unlocks(
    agent_id: str,
    *,
    conn_factory: ConnFactory | None = None,
) -> tuple[AchievementUnlockRow, ...]:
    """List durable unlocks for one agent in earned order."""

    async with _acquire(conn_factory) as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id, achievement_id, earned_at, progress_label, rarity
            FROM agent_achievement_unlocks
            WHERE agent_id = $1
            ORDER BY earned_at ASC, achievement_id ASC
            """,
            _required("agent_id", agent_id),
        )
    return tuple(_row_to_unlock(row) for row in rows)


@asynccontextmanager
async def _acquire(factory: ConnFactory | None) -> AsyncIterator[Any]:
    if factory is None:
        from backend.db_pool import get_pool

        async with get_pool().acquire() as conn:
            yield conn
        return
    cm = factory()
    async with cm as conn:
        yield conn


def _row_to_unlock(row: Any) -> AchievementUnlockRow:
    return AchievementUnlockRow(
        agent_id=row["agent_id"],
        achievement_id=row["achievement_id"],
        earned_at=row["earned_at"],
        progress_label=row["progress_label"],
        rarity=row["rarity"],
    )


def _required(field: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} must be non-empty")
    return clean


def _optional_required(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _required(field, value)


def _rarity(value: str) -> AchievementRarity:
    clean = _required("rarity", value)
    if clean not in {"bronze", "silver", "gold"}:
        raise ValueError("rarity must be bronze, silver, or gold")
    return clean  # type: ignore[return-value]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("earned_at must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "AchievementRarity",
    "AchievementUnlockRow",
    "PostgresAchievementUnlockStore",
    "list_unlocks",
    "record_unlock",
]

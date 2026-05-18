"""RPG.W16.3 -- durable achievement unlock store tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.agents.achievement_unlock_store import (
    PostgresAchievementUnlockStore,
    list_unlocks,
    record_unlock,
)


T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _FakeRow(dict[str, Any]):
    pass


class _FakeConn:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _FakeRow] = {}
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRow | None:
        self.fetchrow_calls.append((sql, args))
        if "SELECT 1" in sql:
            return _FakeRow(exists=1) if (args[0], args[1]) in self.rows else None
        if "INSERT INTO agent_achievement_unlocks" not in sql:
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")
        key = (args[0], args[1])
        self.rows.setdefault(
            key,
            _FakeRow(
                agent_id=args[0],
                achievement_id=args[1],
                earned_at=args[2],
                progress_label=args[3],
                rarity=args[4],
            ),
        )
        return self.rows[key]

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRow]:
        self.fetch_calls.append((sql, args))
        if "FROM agent_achievement_unlocks" not in sql:
            raise AssertionError(f"unexpected fetch SQL: {sql}")
        agent_id = args[0]
        return sorted(
            [row for row in self.rows.values() if row["agent_id"] == agent_id],
            key=lambda row: (row["earned_at"], row["achievement_id"]),
        )


@asynccontextmanager
async def _factory(conn: _FakeConn) -> AsyncIterator[_FakeConn]:
    yield conn


@pytest.mark.asyncio
async def test_record_unlock_is_idempotent_on_agent_and_achievement() -> None:
    conn = _FakeConn()
    first = await record_unlock(
        "agent-a",
        "merged_pr_100",
        T0,
        "bronze",
        conn_factory=lambda: _factory(conn),
    )
    second = await record_unlock(
        "agent-a",
        "merged_pr_100",
        datetime(2026, 1, 3, tzinfo=timezone.utc),
        "gold",
        progress_label="100/100",
        conn_factory=lambda: _factory(conn),
    )

    assert first == second
    assert len(conn.rows) == 1
    sql, args = conn.fetchrow_calls[0]
    assert "ON CONFLICT (agent_id, achievement_id)" in sql
    assert args == ("agent-a", "merged_pr_100", T0, None, "bronze")


@pytest.mark.asyncio
async def test_list_unlocks_filters_agent_and_orders_by_earned_at() -> None:
    conn = _FakeConn()
    await record_unlock(
        "agent-a",
        "zero_regression_streak_30",
        datetime(2026, 1, 4, tzinfo=timezone.utc),
        "silver",
        conn_factory=lambda: _factory(conn),
    )
    await record_unlock(
        "agent-a",
        "merged_pr_100",
        T0,
        "bronze",
        conn_factory=lambda: _factory(conn),
    )
    await record_unlock(
        "agent-b",
        "merged_pr_100",
        T0,
        "bronze",
        conn_factory=lambda: _factory(conn),
    )

    rows = await list_unlocks("agent-a", conn_factory=lambda: _factory(conn))

    assert [row.achievement_id for row in rows] == [
        "merged_pr_100",
        "zero_regression_streak_30",
    ]
    sql, args = conn.fetch_calls[0]
    assert "WHERE agent_id = $1" in sql
    assert "ORDER BY earned_at ASC, achievement_id ASC" in sql
    assert args == ("agent-a",)


@pytest.mark.asyncio
async def test_postgres_store_unlock_achievement_calls_record_unlock_contract() -> None:
    conn = _FakeConn()
    store = PostgresAchievementUnlockStore(lambda: _factory(conn))
    unlock = type(
        "Unlock",
        (),
        {
            "agent_id": "agent-a",
            "achievement_id": "taught_agents_5",
            "unlocked_at": T0,
        },
    )()

    assert await store.unlock_achievement(unlock) is True
    assert ("agent-a", "taught_agents_5") in conn.rows
    sql, args = conn.fetchrow_calls[0]
    assert "INSERT INTO agent_achievement_unlocks" in sql
    assert args == ("agent-a", "taught_agents_5", T0, None, "bronze")


@pytest.mark.asyncio
async def test_record_unlock_rejects_unknown_rarity() -> None:
    with pytest.raises(ValueError, match="rarity"):
        await record_unlock("a", "b", T0, "legendary")  # type: ignore[arg-type]

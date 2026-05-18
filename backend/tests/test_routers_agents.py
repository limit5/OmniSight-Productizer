"""Contract tests for ``backend/routers/agents.py`` RPG endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.achievement_unlock_store import AchievementUnlockRow
from backend.routers.agents import get_achievement_unlock_store, router


T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class _AchievementStore:
    def __init__(self, rows: tuple[AchievementUnlockRow, ...]) -> None:
        self.rows = rows

    async def list_unlocks(self, agent_id: str) -> tuple[AchievementUnlockRow, ...]:
        return tuple(row for row in self.rows if row.agent_id == agent_id)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    store = _AchievementStore(
        (
            AchievementUnlockRow(
                agent_id="agent-a",
                achievement_id="merged_pr_100",
                earned_at=T0,
                rarity="bronze",
            ),
        )
    )
    app.dependency_overrides[get_achievement_unlock_store] = lambda: store
    return TestClient(app)


def test_get_agent_achievements_returns_character_badge_shape(
    client: TestClient,
) -> None:
    resp = client.get("/agents/agent-a/achievements")

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "agent-a"
    assert body["unlocked"] == [
        {
            "id": "merged_pr_100",
            "kind": "pr_merged_100",
            "label": "100 PR Merged",
            "description": "Awarded after an agent has 100 accepted and merged PRs.",
            "earnedAt": "2026-01-02T03:04:05Z",
            "progressLabel": None,
            "rarity": "bronze",
            "locked": False,
        }
    ]
    assert {
        "id",
        "kind",
        "label",
        "description",
        "earnedAt",
        "progressLabel",
        "rarity",
        "locked",
    } <= set(body["locked_visible"][0])
    assert all(row["locked"] is True for row in body["locked_visible"])


def test_get_agent_achievements_isolated_by_agent_id(client: TestClient) -> None:
    resp = client.get("/agents/agent-b/achievements")

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "agent-b"
    assert body["unlocked"] == []
    assert len(body["locked_visible"]) == 5

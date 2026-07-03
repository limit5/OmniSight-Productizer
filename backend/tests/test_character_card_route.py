"""RPG.W1.3 -- contract tests for ``GET /agents/{agent_id}/card``.

Covers the stat-sheet JSON route defined in ``backend/routers/agents.py``.
The endpoint is mounted under ``/api/v1`` by ``backend.main``; tests here
hit the bare ``/agents/...`` paths because they include the router on a
local ``FastAPI`` app to keep the suite hermetic (no full app boot).

DI seam: the route depends on ``get_character_card_registry``. Tests
override it with an :class:`InMemoryCharacterCardStore`-backed registry
so no Postgres is required.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agents.character_card import (
    CharacterCardCreate,
    CharacterCardRegistry,
    InMemoryCharacterCardStore,
)
from backend.routers.agents import get_character_card_registry, router


T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def store() -> InMemoryCharacterCardStore:
    return InMemoryCharacterCardStore()


@pytest.fixture
def client(store: InMemoryCharacterCardStore) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    def _override() -> CharacterCardRegistry:
        return CharacterCardRegistry(store)

    app.dependency_overrides[get_character_card_registry] = _override
    return TestClient(app)


async def _seed(
    store: InMemoryCharacterCardStore,
    *,
    agent_id: str = "api-anthropic-alpha",
    agent_class: str = "api-anthropic",
    guild: str = "backend",
    instance_suffix: str = "alpha",
    level: int = 7,
    xp: int = 350,
    specialization_label: str = "schema-first",
    style_fingerprint: str = "abc123",
    created_at: datetime = T0,
) -> None:
    await store.create_card(
        CharacterCardCreate(
            agent_id=agent_id,
            agent_class=agent_class,
            guild=guild,
            instance_suffix=instance_suffix,
            level=level,
            xp=xp,
            specialization_label=specialization_label,
            style_fingerprint=style_fingerprint,
            created_at=created_at,
        )
    )


class TestGetAgentCard:
    async def test_returns_stat_sheet_json_for_existing_card(
        self, client: TestClient, store: InMemoryCharacterCardStore
    ):
        await _seed(store)
        resp = client.get("/agents/api-anthropic-alpha/card")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "agent_id": "api-anthropic-alpha",
            "agent_class": "api-anthropic",
            "instance_suffix": "alpha",
            "guild": "backend",
            "level": 7,
            "xp": 350,
            "specialization_label": "schema-first",
            "style_fingerprint": "abc123",
            "created_at": T0.isoformat(),
            "portrait_url": None,
        }

    async def test_response_shape_matches_adr_0008_l1_columns(
        self, client: TestClient, store: InMemoryCharacterCardStore
    ):
        await _seed(store, agent_id="anthropic-beta", instance_suffix="beta")
        resp = client.get("/agents/anthropic-beta/card")
        assert resp.status_code == 200
        # ADR-0008 §"Memory hierarchy" pins the L1 column set; the route
        # must not silently drop or rename any of them. portrait_url is a
        # derived (non-L1) field added by OP-2517.
        assert set(resp.json().keys()) == {
            "agent_id",
            "agent_class",
            "instance_suffix",
            "guild",
            "level",
            "xp",
            "specialization_label",
            "style_fingerprint",
            "created_at",
            "portrait_url",
        }

    def test_missing_card_returns_404(self, client: TestClient):
        resp = client.get("/agents/does-not-exist/card")
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]

    async def test_each_card_isolated_by_agent_id(
        self, client: TestClient, store: InMemoryCharacterCardStore
    ):
        await _seed(store, agent_id="api-anthropic-alpha", level=5)
        await _seed(
            store,
            agent_id="api-anthropic-beta",
            instance_suffix="beta",
            level=42,
            xp=9001,
            specialization_label="performance-first",
        )

        resp_a = client.get("/agents/api-anthropic-alpha/card").json()
        resp_b = client.get("/agents/api-anthropic-beta/card").json()

        assert resp_a["level"] == 5
        assert resp_b["level"] == 42
        assert resp_b["xp"] == 9001
        assert resp_b["specialization_label"] == "performance-first"
        # Each instance suffix is preserved on its own card (the
        # "duplicate differentiation" requirement from the ADR).
        assert resp_a["instance_suffix"] == "alpha"
        assert resp_b["instance_suffix"] == "beta"

    async def test_does_not_collide_with_sibling_routes(
        self, client: TestClient, store: InMemoryCharacterCardStore
    ):
        # Path ordering check: /{agent_id}/card and /{agent_id}/skills
        # share the same parent template — make sure /card doesn't
        # accidentally match the /skills handler.
        await _seed(store, agent_id="some-agent", level=3)
        card_resp = client.get("/agents/some-agent/card")
        assert card_resp.status_code == 200
        assert card_resp.json()["level"] == 3

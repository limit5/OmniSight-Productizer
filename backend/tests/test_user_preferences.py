"""J4 — Unit tests for user_preferences API."""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.asyncio


async def _app():
    """Build a minimal FastAPI app with auth + preferences routers."""
    import os
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    return app


@pytest.fixture
async def client():
    app = await _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestUserPreferences:
    async def test_put_and_get_preference(self, client: AsyncClient):
        put = await client.put(
            "/api/v1/user-preferences/wizard_seen",
            json={"value": "1"},
        )
        assert put.status_code == 200
        body = put.json()
        assert body["key"] == "wizard_seen"
        assert body["value"] == "1"

        get = await client.get("/api/v1/user-preferences/wizard_seen")
        assert get.status_code == 200
        assert get.json()["value"] == "1"

    async def test_list_preferences(self, client: AsyncClient):
        await client.put("/api/v1/user-preferences/locale", json={"value": "ja"})
        await client.put("/api/v1/user-preferences/tour_seen", json={"value": "1"})

        resp = await client.get("/api/v1/user-preferences")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items["locale"] == "ja"
        assert items["tour_seen"] == "1"

    async def test_get_nonexistent_returns_404(self, client: AsyncClient):
        resp = await client.get("/api/v1/user-preferences/nonexistent_key_xyz")
        assert resp.status_code == 404

    async def test_upsert_overwrites_existing(self, client: AsyncClient):
        await client.put("/api/v1/user-preferences/locale", json={"value": "en"})
        await client.put("/api/v1/user-preferences/locale", json={"value": "ja"})

        resp = await client.get("/api/v1/user-preferences/locale")
        assert resp.json()["value"] == "ja"

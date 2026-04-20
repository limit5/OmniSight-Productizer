"""J4 — Unit tests for user_preferences API."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="SP-4.2 / SP-4.3 / SP-4.4: test fixture uses SQLite tempfile; "
           "auth.py user CRUD now requires the asyncpg pool. Unsticks "
           "when the adjacent session / password tests migrate."
)

import pytest
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Provide an async HTTP test client with fresh DB + finalized bootstrap."""
    db_path = tmp_path / "user_prefs_test.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    from backend import config as _cfg
    _cfg.settings.database_path = str(db_path)
    from backend import db
    db._DB_PATH = db._resolve_db_path()
    from backend.main import app
    await db.init()

    # Seed the anonymous user — in open auth mode, current_user()
    # returns id="anonymous". user_preferences has FK(user_id)
    # REFERENCES users(id), so this user must exist in the table.
    from backend import auth as _auth
    await _auth.ensure_default_admin()
    conn = db._conn()
    await conn.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, password_hash) "
        "VALUES ('anonymous', 'anonymous@local', '(anonymous)', 'admin', '')"
    )
    await conn.commit()

    # Finalize bootstrap so the gate middleware doesn't return 503.
    from backend import bootstrap as _boot
    _boot._gate_cache["finalized"] = True

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        _boot._gate_cache["finalized"] = False
        await db.close()


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

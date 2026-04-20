"""J4 — Unit tests for user_preferences API.

Task #97 migration (2026-04-21): fixture ported from SQLite tempfile
to pg_test_pool. user_preferences has FK to users(id), so we seed the
synthetic ``anonymous`` user (which open-auth-mode's ``current_user``
returns) before driving any route.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.asyncio


# Pool-backed fixture: explicitly point backend.db at the test PG
# (via OMNISIGHT_DATABASE_URL) so routes still on db._conn() talk to
# the same DB as pg_test_pool. Also seed the synthetic ``anonymous``
# user row so user_preferences' FK(user_id) is satisfied.
@pytest.fixture
async def _prefs_client(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) VALUES ($1, $2, $3, $4, $5, 1, $6) "
            "ON CONFLICT (id) DO NOTHING",
            "anonymous", "anonymous@local", "(anonymous)", "admin",
            "", "t-default",
        )

    from backend import db as _db
    from backend.main import app
    from backend import bootstrap as _boot

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    # Force-close any prior (SQLite) connection, then re-open against PG.
    # db.init() opens unconditionally; closing first avoids dangling fds.
    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        _boot._gate_cache_reset()
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, user_preferences RESTART IDENTITY CASCADE"
            )


class TestUserPreferences:
    async def test_put_and_get_preference(self, _prefs_client: AsyncClient):
        put = await _prefs_client.put(
            "/api/v1/user-preferences/wizard_seen",
            json={"value": "1"},
        )
        assert put.status_code == 200
        body = put.json()
        assert body["key"] == "wizard_seen"
        assert body["value"] == "1"

        get = await _prefs_client.get("/api/v1/user-preferences/wizard_seen")
        assert get.status_code == 200
        assert get.json()["value"] == "1"

    async def test_list_preferences(self, _prefs_client: AsyncClient):
        await _prefs_client.put("/api/v1/user-preferences/locale", json={"value": "ja"})
        await _prefs_client.put("/api/v1/user-preferences/tour_seen", json={"value": "1"})

        resp = await _prefs_client.get("/api/v1/user-preferences")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items["locale"] == "ja"
        assert items["tour_seen"] == "1"

    async def test_get_nonexistent_returns_404(self, _prefs_client: AsyncClient):
        resp = await _prefs_client.get("/api/v1/user-preferences/nonexistent_key_xyz")
        assert resp.status_code == 404

    async def test_upsert_overwrites_existing(self, _prefs_client: AsyncClient):
        await _prefs_client.put("/api/v1/user-preferences/locale", json={"value": "en"})
        await _prefs_client.put("/api/v1/user-preferences/locale", json={"value": "ja"})

        resp = await _prefs_client.get("/api/v1/user-preferences/locale")
        assert resp.json()["value"] == "ja"

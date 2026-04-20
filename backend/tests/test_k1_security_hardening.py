"""K1 tests — startup self-check, default password enforcement, 428 gate.

Task #97 migration (2026-04-21): fixtures ported from SQLite tempfile
to pg_test_pool. The HTTP client fixture explicitly points
``OMNISIGHT_DATABASE_URL`` at the test PG so routes still on
``db._conn()`` (K1 428-gate middleware, /agents, /auth/login) read/
write the same PG that pg_test_pool's TRUNCATE targets.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def _auth_db(pg_test_pool):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    from backend import db, auth
    try:
        yield (db, auth)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


# ── Startup self-check ──────────────────────────────────────────


def test_production_env_rejects_non_strict_auth(monkeypatch):
    """ENV=production + AUTH_MODE != strict → ConfigValidationError (exit 78)."""
    from backend.config import validate_startup_config, ConfigValidationError, settings

    monkeypatch.setenv("OMNISIGHT_ENV", "production")
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_startup_config(strict=True)
    assert exc_info.value.code == 78
    assert "strict" in str(exc_info.value.message).lower()


def test_production_env_accepts_strict_auth(monkeypatch):
    """ENV=production + AUTH_MODE=strict → no error on that check."""
    from backend.config import validate_startup_config, settings

    monkeypatch.setenv("OMNISIGHT_ENV", "production")
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "a-very-strong-password-123")
    # H1 audit: strict mode now requires a non-empty DECISION_BEARER too
    # (previously just a warning). Supply a real bearer so this test
    # only checks the ENV=production path, not the bearer gate.
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", "a-strong-random-bearer-123")
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "ci_mode", False)
    monkeypatch.setattr(settings, "llm_provider", "ollama")

    warnings = validate_startup_config(strict=True)
    assert not any("ENV=production" in w for w in warnings)


def test_non_production_env_allows_open_auth(monkeypatch):
    """Without ENV=production, open auth is allowed (just a warning in strict)."""
    from backend.config import validate_startup_config, settings

    monkeypatch.delenv("OMNISIGHT_ENV", raising=False)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setattr(settings, "env", "")
    monkeypatch.setattr(settings, "debug", True)

    warnings = validate_startup_config(strict=False)
    assert any("open" in w.lower() for w in warnings)


# ── Default password → must_change_password ─────────────────────


@pytest.mark.asyncio
async def test_default_admin_with_default_pw_sets_must_change(_auth_db, monkeypatch):
    _, auth = _auth_db
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)

    user = await auth.ensure_default_admin()
    assert user is not None
    assert user.must_change_password is True

    fetched = await auth.get_user(user.id)
    assert fetched.must_change_password is True


@pytest.mark.asyncio
async def test_default_admin_with_custom_pw_no_flag(_auth_db, monkeypatch):
    _, auth = _auth_db
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "my-custom-strong-pw")

    user = await auth.ensure_default_admin()
    assert user is not None
    assert user.must_change_password is False


@pytest.mark.asyncio
async def test_change_password_clears_flag(_auth_db, monkeypatch):
    _, auth = _auth_db
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)

    user = await auth.ensure_default_admin()
    assert user.must_change_password is True

    await auth.change_password(user.id, "new-strong-password-123")
    fetched = await auth.get_user(user.id)
    assert fetched.must_change_password is False

    ok = await auth.authenticate_password("admin@test.local", "new-strong-password-123")
    assert ok is not None


# ── 428 API gate (integration) ──────────────────────────────────


@pytest.fixture()
async def client_with_default_admin(pg_test_pool, pg_test_dsn, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    monkeypatch.delenv("OMNISIGHT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@test.local")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient
    from backend import bootstrap as _boot

    # L1 #2: pin bootstrap gate green so this test isolates the K1 428
    # mechanism. The admin's individual `must_change_password` flag is a
    # per-user gate that persists after bootstrap finalization.
    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )
    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    if db._db is not None:
        await db.close()
    await db.init()
    from backend import auth
    await auth.ensure_default_admin()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        await db.close()
        _boot._gate_cache_reset()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_428_blocks_api_until_password_changed(client_with_default_admin):
    c = client_with_default_admin

    login_resp = await c.post("/api/v1/auth/login", json={
        "email": "admin@test.local",
        "password": "omnisight-admin",
    })
    assert login_resp.status_code == 200
    cookies = dict(login_resp.cookies)

    resp = await c.get("/api/v1/agents", cookies=cookies)
    assert resp.status_code == 428
    assert "change-password" in resp.json()["detail"].lower()

    change_resp = await c.post("/api/v1/auth/change-password", json={
        "current_password": "omnisight-admin",
        "new_password": "new-strong-password-123",
    }, cookies=cookies, headers={"X-CSRF-Token": login_resp.json()["csrf_token"]})
    assert change_resp.status_code == 200

    resp2 = await c.get("/api/v1/agents", cookies=cookies)
    assert resp2.status_code != 428


@pytest.mark.asyncio
async def test_change_password_endpoint_exempt_from_428(client_with_default_admin):
    c = client_with_default_admin

    login_resp = await c.post("/api/v1/auth/login", json={
        "email": "admin@test.local",
        "password": "omnisight-admin",
    })
    assert login_resp.status_code == 200
    cookies = dict(login_resp.cookies)

    change_resp = await c.post("/api/v1/auth/change-password", json={
        "current_password": "omnisight-admin",
        "new_password": "another-strong-pw-123",
    }, cookies=cookies, headers={"X-CSRF-Token": login_resp.json()["csrf_token"]})
    assert change_resp.status_code == 200
    assert change_resp.json()["must_change_password"] is False

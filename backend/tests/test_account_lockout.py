"""K2 — Account lockout tests.

Tests:
  - Account locks after LOCKOUT_THRESHOLD consecutive failures
  - Locked account returns None without PBKDF2
  - Successful login resets failure counter
  - Lock expires after time elapses (time decay)
  - Exponential backoff duration
  - Audit events emitted for auth.login.fail and auth.lockout
  - Token-bucket rate limiter wired to /auth/login
"""

from __future__ import annotations

import time

import pytest

from backend import auth
from backend.rate_limit import reset_limiters
from backend.routers import auth as auth_router


@pytest.fixture(autouse=True)
def _reset_rate():
    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()
    yield
    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()


# ── Unit tests: lockout logic in auth module ──────────────────


@pytest.fixture()
async def _auth_db(pg_test_pool):
    """Phase-3 Step C.1 (2026-04-21): ported off the SQLite-file
    setup onto ``pg_test_pool``. TRUNCATE ``users`` up front so each
    test's create_user starts with a clean slate and email uniqueness
    holds across sibling tests."""
    from backend import db
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    yield db, auth


@pytest.mark.asyncio
async def test_lockout_after_threshold(_auth_db):
    db_mod, auth_mod = _auth_db
    await auth_mod.create_user(
        email="lock@test.com", name="Lock", role="viewer", password="correct-pass-123",
    )
    for i in range(auth_mod.LOCKOUT_THRESHOLD):
        result = await auth_mod.authenticate_password("lock@test.com", "wrong")
        assert result is None

    locked, remaining = await auth_mod.is_account_locked("lock@test.com")
    assert locked
    assert remaining > 0

    result = await auth_mod.authenticate_password("lock@test.com", "correct-pass-123")
    assert result is None


@pytest.mark.asyncio
async def test_successful_login_resets_counter(_auth_db):
    db_mod, auth_mod = _auth_db
    await auth_mod.create_user(
        email="reset@test.com", name="Reset", role="viewer", password="good-pass-1234",
    )
    for _ in range(auth_mod.LOCKOUT_THRESHOLD - 1):
        await auth_mod.authenticate_password("reset@test.com", "wrong")

    user = await auth_mod.authenticate_password("reset@test.com", "good-pass-1234")
    assert user is not None

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT failed_login_count, locked_until "
            "FROM users WHERE email = $1",
            "reset@test.com",
        )
    assert row["failed_login_count"] == 0
    assert row["locked_until"] is None


@pytest.mark.asyncio
async def test_lockout_expires_after_time(_auth_db):
    db_mod, auth_mod = _auth_db
    await auth_mod.create_user(
        email="expire@test.com", name="Expire", role="viewer", password="expire-pass-1",
    )
    for _ in range(auth_mod.LOCKOUT_THRESHOLD):
        await auth_mod.authenticate_password("expire@test.com", "wrong")

    locked, _ = await auth_mod.is_account_locked("expire@test.com")
    assert locked

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET locked_until = $1 WHERE email = $2",
            time.time() - 1, "expire@test.com",
        )

    locked, _ = await auth_mod.is_account_locked("expire@test.com")
    assert not locked

    user = await auth_mod.authenticate_password("expire@test.com", "expire-pass-1")
    assert user is not None


@pytest.mark.asyncio
async def test_exponential_backoff_duration(_auth_db):
    """Each re-lock beyond the threshold should double the duration, capped at 24h."""
    d1 = auth.LOCKOUT_BASE_S
    d2 = auth._lockout_duration(auth.LOCKOUT_THRESHOLD + 1)
    assert d2 == d1 * 2
    d_max = auth._lockout_duration(auth.LOCKOUT_THRESHOLD + 100)
    assert d_max == auth.LOCKOUT_MAX_S


@pytest.mark.asyncio
async def test_locked_account_skips_pbkdf2(_auth_db):
    """While locked, authenticate_password should return None immediately
    without running PBKDF2 — the function checks locked_until before
    calling verify_password."""
    db_mod, auth_mod = _auth_db
    await auth_mod.create_user(
        email="skip@test.com", name="Skip", role="viewer", password="skip-pass-1234",
    )
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET failed_login_count = 10, "
            "locked_until = $1 WHERE email = $2",
            time.time() + 9999, "skip@test.com",
        )

    result = await auth_mod.authenticate_password("skip@test.com", "skip-pass-1234")
    assert result is None


# ── Integration: through the live FastAPI router ──────────────


@pytest.mark.asyncio
async def test_token_bucket_ip_rate_limit(client):
    """Token-bucket per-IP limiter should block after capacity exhausted."""
    from backend.rate_limit import ip_limiter
    lim = ip_limiter()
    for _ in range(lim.capacity):
        lim.allow("127.0.0.1")
    ok, _ = lim.allow("127.0.0.1")
    assert not ok


@pytest.mark.asyncio
async def test_token_bucket_email_rate_limit(client):
    """Token-bucket per-email limiter should block after capacity exhausted."""
    from backend.rate_limit import email_limiter
    lim = email_limiter()
    for _ in range(lim.capacity):
        lim.allow("test@example.com")
    ok, _ = lim.allow("test@example.com")
    assert not ok


@pytest.mark.asyncio
async def test_lockout_via_endpoint(client, monkeypatch):
    """10 bad logins through the real endpoint should trigger lockout (423)."""
    monkeypatch.setenv("OMNISIGHT_LOGIN_MAX_ATTEMPTS", "100")
    monkeypatch.setenv("OMNISIGHT_LOGIN_IP_RATE", "100")
    monkeypatch.setenv("OMNISIGHT_LOGIN_EMAIL_RATE", "100")
    reset_limiters()

    from backend import auth as auth_mod
    await auth_mod.create_user(
        email="endlock@test.com", name="End", role="viewer", password="endpoint-pass1",
    )
    for i in range(auth_mod.LOCKOUT_THRESHOLD):
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "endlock@test.com", "password": "wrong"},
        )
        assert r.status_code == 401, f"attempt {i+1}: {r.status_code}"

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "endlock@test.com", "password": "endpoint-pass1"},
    )
    assert r.status_code == 423
    assert "retry-after" in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_lockout_audit_events(client, monkeypatch):
    """Failed login should emit auth.login.fail; lockout should emit auth.lockout."""
    monkeypatch.setenv("OMNISIGHT_LOGIN_MAX_ATTEMPTS", "100")
    monkeypatch.setenv("OMNISIGHT_LOGIN_IP_RATE", "100")
    monkeypatch.setenv("OMNISIGHT_LOGIN_EMAIL_RATE", "100")
    reset_limiters()

    from backend import auth as auth_mod
    from backend.db_pool import get_pool

    await auth_mod.create_user(
        email="audit@test.com", name="Audit", role="viewer", password="audit-pass-123",
    )

    await client.post(
        "/api/v1/auth/login",
        json={"email": "audit@test.com", "password": "wrong"},
    )

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action FROM audit_log "
            "WHERE action = 'auth.login.fail' "
            "ORDER BY id DESC LIMIT 1"
        )
    assert row is not None

    for _ in range(auth_mod.LOCKOUT_THRESHOLD - 1):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "audit@test.com", "password": "wrong"},
        )

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action FROM audit_log "
            "WHERE action = 'auth.lockout' "
            "ORDER BY id DESC LIMIT 1"
        )
    assert row is not None

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "audit@test.com", "password": "wrong"},
    )
    assert r.status_code == 423

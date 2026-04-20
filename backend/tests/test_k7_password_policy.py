"""K7 tests — password policy, Argon2id upgrade path, password history.

Task #97 migration (2026-04-21): fixture ported from SQLite tempfile
to pg_test_pool. Direct ``db._conn().execute(...)`` accesses are
replaced with inline ``get_pool().acquire()`` + $N placeholders.
HTTP-driven tests set ``OMNISIGHT_DATABASE_URL`` so the db._conn()
compat wrapper talks to the same PG as pg_test_pool.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest


@pytest.fixture()
async def _auth_db(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    from backend import db, auth
    try:
        yield (db, auth)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


# ── Argon2id hashing ──────────────────────────────────────────


def test_new_hash_is_argon2id():
    from backend import auth
    h = auth.hash_password("strongpassword123!")
    assert h.startswith("$argon2id$")


def test_argon2id_roundtrip():
    from backend import auth
    h = auth.hash_password("testpassword1234")
    assert auth.verify_password("testpassword1234", h)
    assert not auth.verify_password("wrong", h)


# ── Legacy PBKDF2 support ────────────────────────────────────


def _make_pbkdf2_hash(plain: str) -> str:
    iters = 320_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${digest.hex()}"


def test_legacy_pbkdf2_still_verifies():
    from backend import auth
    h = _make_pbkdf2_hash("oldpassword123")
    assert auth.verify_password("oldpassword123", h)
    assert not auth.verify_password("wrong", h)


def test_pbkdf2_needs_rehash():
    from backend import auth
    h = _make_pbkdf2_hash("something")
    assert auth.needs_rehash(h)


def test_argon2id_does_not_need_rehash():
    from backend import auth
    h = auth.hash_password("newpassword")
    assert not auth.needs_rehash(h)


def test_garbage_hash_returns_false():
    from backend import auth
    assert not auth.verify_password("anything", "garbage")
    assert not auth.verify_password("anything", "")


# ── Auto-rehash on login ─────────────────────────────────────


@pytest.mark.asyncio
async def test_login_auto_rehash_pbkdf2_to_argon2id(_auth_db):
    _, auth = _auth_db
    pw = "legacy-password-12345!"
    pbkdf2_hash = _make_pbkdf2_hash(pw)
    u = await auth.create_user("legacy@test.com", "Legacy", role="viewer", password="placeholder123!")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash=$1 WHERE id=$2",
            pbkdf2_hash, u.id,
        )

    user = await auth.authenticate_password("legacy@test.com", pw)
    assert user is not None
    assert user.email == "legacy@test.com"

    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id=$1", u.id,
        )
    assert r["password_hash"].startswith("$argon2id$"), "hash should be upgraded"
    assert auth.verify_password(pw, r["password_hash"])


# ── Password strength validation ─────────────────────────────


def test_password_too_short():
    from backend import auth
    err = auth.validate_password_strength("short")
    assert err is not None
    assert "12" in err


def test_weak_password_rejected():
    from backend import auth
    err = auth.validate_password_strength("password1234")
    assert err is not None
    assert "weak" in err.lower() or "too" in err.lower()


def test_strong_password_accepted():
    from backend import auth
    err = auth.validate_password_strength("c0rr3ct-h0rse-b@ttery-st@ple!")
    assert err is None


# ── Password history ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_history_blocks_reuse(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("hist@test.com", "Hist", role="viewer", password="first-password-ok1!")

    await auth.change_password(u.id, "second-password-ok2!")
    await auth.change_password(u.id, "third-password-ok3!")

    reused = await auth.check_password_history(u.id, "second-password-ok2!")
    assert reused is True, "recently used password should be blocked"

    reused_current = await auth.check_password_history(u.id, "third-password-ok3!")
    assert reused_current is True, "current password should be blocked"


@pytest.mark.asyncio
async def test_password_history_allows_old_enough(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("old@test.com", "Old", role="viewer", password="initial-pw-12345!")

    passwords = [f"password-change-{i}-ok!" for i in range(7)]
    for pw in passwords:
        await auth.change_password(u.id, pw)

    allowed = await auth.check_password_history(u.id, "initial-pw-12345!")
    assert allowed is False, "password older than 5 changes should be allowed"


@pytest.mark.asyncio
async def test_password_history_records_old_hash(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("rec@test.com", "Rec", role="viewer", password="original-pass-123!")
    await auth.change_password(u.id, "new-pass-456-ok!")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COUNT(*) as n FROM password_history WHERE user_id=$1", u.id,
        )
    assert r["n"] >= 1, "old hash should be recorded in password_history"


# ── Integration: change-password endpoint ─────────────────────


@pytest.fixture()
async def _k7_http_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """HTTP client for change-password endpoint tests — re-points db._conn()
    compat wrapper at the same PG as pg_test_pool via OMNISIGHT_DATABASE_URL."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")

    from backend import db
    from backend.main import app
    from backend import bootstrap as _boot
    from httpx import ASGITransport, AsyncClient

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

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        _boot._gate_cache_reset()
        await db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


@pytest.mark.asyncio
async def test_change_password_endpoint_rejects_weak(_k7_http_client):
    from backend import auth
    u = await auth.create_user("ep@test.com", "EP", role="admin", password="strong-init-pass-1!")
    sess = await auth.create_session(u.id, ip="127.0.0.1", user_agent="test")

    resp = await _k7_http_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "strong-init-pass-1!", "new_password": "password1234"},
        cookies={"omnisight_session": sess.token},
        headers={"X-CSRF-Token": sess.csrf_token},
    )
    assert resp.status_code == 422
    assert "weak" in resp.json()["detail"].lower() or "too" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_endpoint_rejects_reused(_k7_http_client):
    from backend import auth
    u = await auth.create_user("reuse@test.com", "Reuse", role="admin", password="original-strong-pw1!")
    await auth.change_password(u.id, "second-strong-pw-12!")
    sess = await auth.create_session(u.id, ip="127.0.0.1", user_agent="test")

    resp = await _k7_http_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "second-strong-pw-12!", "new_password": "original-strong-pw1!"},
        cookies={"omnisight_session": sess.token},
        headers={"X-CSRF-Token": sess.csrf_token},
    )
    assert resp.status_code == 422
    assert "reuse" in resp.json()["detail"].lower()

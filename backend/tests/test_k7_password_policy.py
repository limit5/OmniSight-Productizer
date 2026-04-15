"""K7 tests — password policy, Argon2id upgrade path, password history."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile

import pytest


@pytest.fixture()
async def _auth_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "k7.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as _cfg
        _cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        from backend import auth
        try:
            yield (db, auth)
        finally:
            await db.close()


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
    db_mod, auth = _auth_db
    pw = "legacy-password-12345!"
    pbkdf2_hash = _make_pbkdf2_hash(pw)
    u = await auth.create_user("legacy@test.com", "Legacy", role="viewer", password="placeholder123!")
    conn = db_mod._conn()
    await conn.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (pbkdf2_hash, u.id),
    )
    await conn.commit()

    user = await auth.authenticate_password("legacy@test.com", pw)
    assert user is not None
    assert user.email == "legacy@test.com"

    async with conn.execute(
        "SELECT password_hash FROM users WHERE id=?", (u.id,),
    ) as cur:
        r = await cur.fetchone()
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
    db_mod, auth = _auth_db
    u = await auth.create_user("rec@test.com", "Rec", role="viewer", password="original-pass-123!")
    await auth.change_password(u.id, "new-pass-456-ok!")

    conn = db_mod._conn()
    async with conn.execute(
        "SELECT COUNT(*) as n FROM password_history WHERE user_id=?", (u.id,),
    ) as cur:
        r = await cur.fetchone()
    assert r["n"] >= 1, "old hash should be recorded in password_history"


# ── Integration: change-password endpoint ─────────────────────


@pytest.mark.asyncio
async def test_change_password_endpoint_rejects_weak(_auth_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    from backend import auth
    from backend.main import app
    from httpx import AsyncClient, ASGITransport

    u = await auth.create_user("ep@test.com", "EP", role="admin", password="strong-init-pass-1!")
    sess = await auth.create_session(u.id, ip="127.0.0.1", user_agent="test")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "strong-init-pass-1!", "new_password": "password1234"},
            cookies={"omnisight_session": sess.token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert resp.status_code == 422
    assert "weak" in resp.json()["detail"].lower() or "too" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_change_password_endpoint_rejects_reused(_auth_db, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    from backend import auth
    from backend.main import app
    from httpx import AsyncClient, ASGITransport

    u = await auth.create_user("reuse@test.com", "Reuse", role="admin", password="original-strong-pw1!")
    await auth.change_password(u.id, "second-strong-pw-12!")
    sess = await auth.create_session(u.id, ip="127.0.0.1", user_agent="test")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "second-strong-pw-12!", "new_password": "original-strong-pw1!"},
            cookies={"omnisight_session": sess.token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert resp.status_code == 422
    assert "reuse" in resp.json()["detail"].lower()

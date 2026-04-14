"""Phase 54 tests — auth, sessions, RBAC, role gating, GitHub App stub."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def _auth_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.db")
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


# ── core ────────────────────────────────────────────────────────


def test_role_at_least_ladder():
    from backend import auth
    assert auth.role_at_least("admin", "viewer")
    assert auth.role_at_least("operator", "operator")
    assert not auth.role_at_least("viewer", "operator")
    assert not auth.role_at_least("viewer", "admin")
    assert not auth.role_at_least("nonsense", "viewer")


def test_password_hash_roundtrip():
    from backend import auth
    h = auth.hash_password("hunter2")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)
    # tampered hash → False, no crash
    assert not auth.verify_password("hunter2", "garbage")


# ── user CRUD ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_get_user(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("a@b.com", "Alice", role="operator", password="pw")
    assert u.id.startswith("u-")
    assert u.email == "a@b.com"
    fetched = await auth.get_user(u.id)
    assert fetched and fetched.email == "a@b.com" and fetched.role == "operator"


@pytest.mark.asyncio
async def test_create_user_unknown_role_raises(_auth_db):
    _, auth = _auth_db
    with pytest.raises(ValueError):
        await auth.create_user("x@y.com", "X", role="superuser")


@pytest.mark.asyncio
async def test_authenticate_password(_auth_db):
    _, auth = _auth_db
    await auth.create_user("a@b.com", "Alice", role="admin", password="pw")
    ok = await auth.authenticate_password("a@b.com", "pw")
    assert ok and ok.role == "admin"
    bad = await auth.authenticate_password("a@b.com", "WRONG")
    assert bad is None
    nobody = await auth.authenticate_password("nope@x.com", "pw")
    assert nobody is None


# ── sessions ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_create_get_delete(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("a@b.com", "Alice", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="127.0.0.1", user_agent="pytest")
    assert sess.token and sess.csrf_token
    fetched = await auth.get_session(sess.token)
    assert fetched and fetched.user_id == u.id
    await auth.delete_session(sess.token)
    assert (await auth.get_session(sess.token)) is None


@pytest.mark.asyncio
async def test_expired_session_is_purged(_auth_db, monkeypatch):
    _, auth = _auth_db
    u = await auth.create_user("a@b.com", "Alice", role="viewer", password="pw")
    sess = await auth.create_session(u.id)
    # Force it to be expired by rewriting the row
    from backend import db
    await db._conn().execute(
        "UPDATE sessions SET expires_at=? WHERE token=?",
        (0.0, sess.token),
    )
    await db._conn().commit()
    assert (await auth.get_session(sess.token)) is None


@pytest.mark.asyncio
async def test_ensure_default_admin_only_when_empty(_auth_db, monkeypatch):
    _, auth = _auth_db
    monkeypatch.setenv("OMNISIGHT_ADMIN_EMAIL", "admin@local")
    monkeypatch.setenv("OMNISIGHT_ADMIN_PASSWORD", "secret")
    u1 = await auth.ensure_default_admin()
    assert u1 and u1.role == "admin"
    # second call → no new user
    u2 = await auth.ensure_default_admin()
    assert u2 is None


# ── auth_mode ──────────────────────────────────────────────────


def test_auth_mode_default_open(monkeypatch):
    from backend import auth
    monkeypatch.delenv("OMNISIGHT_AUTH_MODE", raising=False)
    assert auth.auth_mode() == "open"


def test_auth_mode_invalid_falls_back_to_open(monkeypatch):
    from backend import auth
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "totally-bogus")
    assert auth.auth_mode() == "open"


def test_auth_mode_strict(monkeypatch):
    from backend import auth
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "strict")
    assert auth.auth_mode() == "strict"


# ── GitHub App scaffold ────────────────────────────────────────


def test_github_app_jwt_requires_env(monkeypatch):
    from backend import github_app
    monkeypatch.delenv("OMNISIGHT_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("OMNISIGHT_GITHUB_APP_PRIVATE_KEY", raising=False)
    with pytest.raises(github_app.GitHubAppNotConfigured):
        github_app.app_jwt()


def test_github_app_jwt_signs_with_test_key(monkeypatch):
    """End-to-end JWT format: header.payload.signature, all base64url."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("OMNISIGHT_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("OMNISIGHT_GITHUB_APP_PRIVATE_KEY", pem)
    from backend import github_app
    jwt = github_app.app_jwt()
    parts = jwt.split(".")
    assert len(parts) == 3
    # header decodes to {"alg":"RS256","typ":"JWT"}
    import base64
    import json as _json
    pad = "=" * (-len(parts[0]) % 4)
    header = _json.loads(base64.urlsafe_b64decode(parts[0] + pad))
    assert header == {"alg": "RS256", "typ": "JWT"}
    payload = _json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    assert payload["iss"] == "12345"


@pytest.mark.asyncio
async def test_github_installation_upsert_and_list(_auth_db):
    _, _ = _auth_db
    from backend import github_app
    await github_app.upsert_installation(
        installation_id=42, account_login="acme",
        account_type="Organization",
        repos=["acme/repo1", "acme/repo2"],
        permissions={"contents": "write"},
    )
    rows = await github_app.list_installations()
    assert len(rows) == 1
    assert rows[0]["installation_id"] == 42
    assert rows[0]["account_login"] == "acme"
    assert rows[0]["repos"] == ["acme/repo1", "acme/repo2"]
    # idempotent upsert
    await github_app.upsert_installation(
        installation_id=42, account_login="acme",
        account_type="Organization",
        repos=["acme/repo1", "acme/repo2", "acme/repo3"],
    )
    rows2 = await github_app.list_installations()
    assert len(rows2) == 1
    assert rows2[0]["repos"] == ["acme/repo1", "acme/repo2", "acme/repo3"]

"""Phase 54 tests — auth, sessions, RBAC, role gating, GitHub App stub.

Phase-3-Runtime-v2 SP-4.2 (2026-04-20): _auth_db fixture migrated from
SQLite tempfile to pg_test_pool. User-CRUD tests run against the
ported pool-backed auth.py functions. Session + password-flow tests
are skipped with SP-4.3 / SP-4.4 markers — they'll un-skip when those
slices port auth.create_session, authenticate_password, etc.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def _auth_db(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users RESTART IDENTITY CASCADE"
        )
    from backend import db, auth
    try:
        yield (db, auth)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users RESTART IDENTITY CASCADE"
            )


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
    assert h.startswith("$argon2id$")
    assert auth.verify_password("hunter2", h)
    assert not auth.verify_password("wrong", h)
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


@pytest.mark.skip(
    reason="SP-4.4: authenticate_password still uses compat _conn(); "
           "unskips when SP-4.4 ports the password-flow functions."
)
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


@pytest.mark.skip(
    reason="SP-4.3: create_session / get_session / delete_session still "
           "use compat _conn(); unskips when SP-4.3 ports session CRUD."
)
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


@pytest.mark.skip(
    reason="SP-4.3: create_session + direct db._conn() UPDATE on "
           "sessions table; unskips when SP-4.3 ports session CRUD."
)
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
@pytest.mark.skip(
    reason="Epic 5: github_app module still uses compat _conn(); "
           "unskips when github_app.py is ported."
)
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


# ── K4: session rotation ──────────────────────────────────────


@pytest.mark.skip(reason="SP-4.3: session rotate CRUD pending port")
@pytest.mark.asyncio
async def test_rotate_session_creates_new_and_graces_old(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("r@b.com", "Bob", role="viewer", password="pw")
    old_sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="TestBrowser/1.0")
    old_token = old_sess.token

    new_sess, returned_old = await auth.rotate_session(
        old_token, ip="1.2.3.4", user_agent="TestBrowser/1.0",
    )
    assert returned_old == old_token
    assert new_sess.token != old_token
    assert new_sess.user_id == u.id

    old_fetched = await auth.get_session(old_token)
    assert old_fetched is not None, "old token should still work during grace"
    assert old_fetched.rotated_from == new_sess.token

    new_fetched = await auth.get_session(new_sess.token)
    assert new_fetched is not None
    assert new_fetched.user_id == u.id


@pytest.mark.skip(reason="SP-4.3: session rotate CRUD pending port")
@pytest.mark.asyncio
async def test_rotate_session_grace_window_expires(_auth_db):
    _, auth = _auth_db
    from backend import db
    u = await auth.create_user("g@b.com", "Grace", role="viewer", password="pw")
    old_sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="UA")
    new_sess, _ = await auth.rotate_session(old_sess.token, ip="1.2.3.4", user_agent="UA")

    await db._conn().execute(
        "UPDATE sessions SET expires_at=? WHERE token=?",
        (0.0, old_sess.token),
    )
    await db._conn().commit()
    assert (await auth.get_session(old_sess.token)) is None, \
        "old token must expire after grace window"
    assert (await auth.get_session(new_sess.token)) is not None, \
        "new token must remain valid"


@pytest.mark.skip(reason="SP-4.3: session rotate CRUD pending port")
@pytest.mark.asyncio
async def test_rotate_session_nonexistent_raises(_auth_db):
    _, auth = _auth_db
    with pytest.raises(ValueError, match="session not found"):
        await auth.rotate_session("nonexistent-token")


@pytest.mark.skip(reason="SP-4.3: session rotate CRUD pending port")
@pytest.mark.asyncio
async def test_rotate_user_sessions_on_role_change(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("rc@b.com", "Carol", role="viewer", password="pw")
    s1 = await auth.create_session(u.id, ip="1.1.1.1", user_agent="UA1")
    await auth.create_session(u.id, ip="2.2.2.2", user_agent="UA2")

    count = await auth.rotate_user_sessions(u.id, exclude_token=None)
    assert count == 2

    fetched1 = await auth.get_session(s1.token)
    assert fetched1 is not None, "should still be in grace window"
    assert fetched1.expires_at < s1.expires_at, "expiry should be shortened to grace"


# ── K4: UA binding ────────────────────────────────────────────


def test_compute_ua_hash_deterministic():
    from backend.auth import compute_ua_hash
    h1 = compute_ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    h2 = compute_ua_hash("Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    assert h1 == h2
    assert len(h1) == 32


def test_compute_ua_hash_different_ua():
    from backend.auth import compute_ua_hash
    h1 = compute_ua_hash("Mozilla/5.0 Chrome")
    h2 = compute_ua_hash("Mozilla/5.0 Firefox")
    assert h1 != h2


def test_compute_ua_hash_empty():
    from backend.auth import compute_ua_hash
    assert compute_ua_hash("") == ""


@pytest.mark.skip(reason="SP-4.3: create_session pending port")
@pytest.mark.asyncio
async def test_ua_binding_match(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua@b.com", "UA", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="MyBrowser/1.0")
    assert await auth.check_ua_binding(sess, "MyBrowser/1.0") is True


@pytest.mark.skip(reason="SP-4.3: create_session pending port")
@pytest.mark.asyncio
async def test_ua_binding_mismatch_returns_false(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua2@b.com", "UA2", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="Chrome/120")
    assert await auth.check_ua_binding(sess, "Firefox/115") is False


@pytest.mark.skip(reason="SP-4.3: create_session pending port")
@pytest.mark.asyncio
async def test_ua_binding_empty_ua_passes(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua3@b.com", "UA3", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="")
    assert await auth.check_ua_binding(sess, "") is True
    assert await auth.check_ua_binding(sess, "SomeUA") is True

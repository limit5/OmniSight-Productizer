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
    # SP-4.3a (2026-04-20): port → use pool acquire for the direct
    # UPDATE that forces expiry; get_session now takes the pool conn
    # path polymorphically.
    _, auth = _auth_db
    u = await auth.create_user("a@b.com", "Alice", role="viewer", password="pw")
    sess = await auth.create_session(u.id)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # FX.11.2: direct UPDATE keys on the lookup hash because
        # ``sessions.token`` now stores KS-envelope JSON.
        await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE token_lookup_index = $2",
            0.0, auth._token_lookup_hash(sess.token),
        )
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


# ── K4: session rotation ──────────────────────────────────────


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


@pytest.mark.asyncio
async def test_rotate_session_grace_window_expires(_auth_db):
    # SP-4.3b (2026-04-20): migrated direct db._conn() UPDATE to pool
    # acquire + $N placeholder.
    _, auth = _auth_db
    u = await auth.create_user("g@b.com", "Grace", role="viewer", password="pw")
    old_sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="UA")
    new_sess, _ = await auth.rotate_session(old_sess.token, ip="1.2.3.4", user_agent="UA")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # FX.11.2: lookup index keyed UPDATE.
        await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE token_lookup_index = $2",
            0.0, auth._token_lookup_hash(old_sess.token),
        )
    assert (await auth.get_session(old_sess.token)) is None, \
        "old token must expire after grace window"
    assert (await auth.get_session(new_sess.token)) is not None, \
        "new token must remain valid"


@pytest.mark.asyncio
async def test_rotate_session_nonexistent_raises(_auth_db):
    _, auth = _auth_db
    with pytest.raises(ValueError, match="session not found"):
        await auth.rotate_session("nonexistent-token")


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


@pytest.mark.asyncio
async def test_ua_binding_match(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua@b.com", "UA", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="MyBrowser/1.0")
    assert await auth.check_ua_binding(sess, "MyBrowser/1.0") is True


@pytest.mark.asyncio
async def test_ua_binding_mismatch_returns_false(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua2@b.com", "UA2", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="Chrome/120")
    assert await auth.check_ua_binding(sess, "Firefox/115") is False


@pytest.mark.asyncio
async def test_ua_binding_empty_ua_passes(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user("ua3@b.com", "UA3", role="viewer", password="pw")
    sess = await auth.create_session(u.id, ip="1.2.3.4", user_agent="")
    assert await auth.check_ua_binding(sess, "") is True
    assert await auth.check_ua_binding(sess, "SomeUA") is True


# ── SP-4.4: password flow (ported 2026-04-21) ─────────────────


@pytest.mark.asyncio
async def test_change_password_records_history_and_trims(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "h@b.com", "Hist", role="viewer", password="original-pass",
    )
    # Walk the history past the retention limit to confirm trimming.
    passwords = [f"rotated-{i}" for i in range(auth.PASSWORD_HISTORY_LIMIT + 3)]
    for pw in passwords:
        await auth.change_password(u.id, pw)
    # The oldest passwords (including "original-pass") should no longer
    # be in history; the most recent PASSWORD_HISTORY_LIMIT are blocked.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM password_history WHERE user_id = $1",
            u.id,
        )
    assert count == auth.PASSWORD_HISTORY_LIMIT, (
        f"password_history must be trimmed to {auth.PASSWORD_HISTORY_LIMIT}, "
        f"got {count}"
    )
    # must_change_password flag cleared after change.
    fetched = await auth.get_user(u.id)
    assert fetched.must_change_password is False


@pytest.mark.asyncio
async def test_change_password_blocks_recent_reuse(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "r@b.com", "Reuse", role="viewer", password="first-password",
    )
    await auth.change_password(u.id, "second-password")
    # The old "first-password" is now in history → check_password_history
    # should flag it as a reuse.
    assert await auth.check_password_history(u.id, "first-password") is True
    # A fresh password is not in history.
    assert await auth.check_password_history(u.id, "never-used-before") is False


@pytest.mark.asyncio
async def test_authenticate_password_wrong_password_increments_counter(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "f@b.com", "Fail", role="viewer", password="correct-password",
    )
    for _ in range(3):
        assert await auth.authenticate_password("f@b.com", "wrong") is None
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT failed_login_count, locked_until FROM users WHERE id = $1",
            u.id,
        )
    assert r["failed_login_count"] == 3
    assert r["locked_until"] is None, (
        "3 failures < LOCKOUT_THRESHOLD, must not be locked yet"
    )


@pytest.mark.asyncio
async def test_authenticate_password_concurrent_failures_atomic(_auth_db):
    """Load-bearing regression guard for SP-4.4 atomic increment.

    Under SQLite's single-writer, the old read-compute-write pattern
    couldn't interleave. Under the asyncpg pool, two concurrent wrong-
    password attempts would both read ``failed_login_count = N``, both
    compute ``N + 1``, and one would clobber the other — leaving the
    counter at ``N + 1`` when it should be ``N + 2``. The atomic
    ``UPDATE ... SET col = col + 1 RETURNING`` makes that impossible.
    """
    import asyncio
    _, auth = _auth_db
    u = await auth.create_user(
        "c@b.com", "Conc", role="viewer", password="correct",
    )
    # Fire 10 wrong-password attempts concurrently. After the race, the
    # counter must be exactly 10 — not less (lost updates).
    N = 10
    results = await asyncio.gather(
        *(auth.authenticate_password("c@b.com", "wrong") for _ in range(N))
    )
    assert all(r is None for r in results)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        count = await conn.fetchval(
            "SELECT failed_login_count FROM users WHERE id = $1", u.id,
        )
    assert count == N, (
        f"atomic increment failed: expected {N} failures recorded, "
        f"got {count} (lost updates under pool concurrency)"
    )


@pytest.mark.asyncio
async def test_authenticate_password_resets_counter_on_success(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "s@b.com", "Succ", role="viewer", password="correct",
    )
    # Poison the counter.
    for _ in range(3):
        await auth.authenticate_password("s@b.com", "wrong")
    # Successful login resets.
    ok = await auth.authenticate_password("s@b.com", "correct")
    assert ok is not None
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT failed_login_count, locked_until, last_login_at "
            "FROM users WHERE id = $1",
            u.id,
        )
    assert r["failed_login_count"] == 0
    assert r["locked_until"] is None
    # last_login_at is a YYYY-MM-DD HH:MM:SS text timestamp.
    assert r["last_login_at"] is not None
    assert len(r["last_login_at"]) == 19


@pytest.mark.asyncio
async def test_authenticate_password_lockout_engages_at_threshold(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "l@b.com", "Lock", role="viewer", password="correct",
    )
    # Drive past the lockout threshold.
    for _ in range(auth.LOCKOUT_THRESHOLD):
        await auth.authenticate_password("l@b.com", "wrong")
    locked, retry = await auth.is_account_locked("l@b.com")
    assert locked is True, "account must be locked after threshold failures"
    assert retry > 0
    # Even the correct password is rejected while locked (defensive
    # read in authenticate_password): it runs argon2 verify but returns
    # None because locked_until > now.
    import time as _t
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        locked_until = await conn.fetchval(
            "SELECT locked_until FROM users WHERE id = $1", u.id,
        )
    assert locked_until is not None and locked_until > _t.time()
    assert await auth.authenticate_password("l@b.com", "correct") is None


@pytest.mark.asyncio
async def test_is_account_locked_not_locked_returns_false(_auth_db):
    _, auth = _auth_db
    await auth.create_user("n@b.com", "Norm", role="viewer", password="pw")
    locked, retry = await auth.is_account_locked("n@b.com")
    assert locked is False
    assert retry == 0.0
    # Unknown email also returns not-locked (no timing signal).
    locked, retry = await auth.is_account_locked("ghost@b.com")
    assert locked is False


@pytest.mark.asyncio
async def test_authenticate_password_disabled_user_rejected(_auth_db):
    _, auth = _auth_db
    u = await auth.create_user(
        "d@b.com", "Dis", role="viewer", password="correct",
    )
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET enabled = 0 WHERE id = $1", u.id,
        )
    # Disabled user rejected even with right password.
    assert await auth.authenticate_password("d@b.com", "correct") is None


# ── SP-4.5: flag_all_admins_must_change_password (ported 2026-04-21) ─


@pytest.mark.asyncio
async def test_flag_all_admins_must_change_password_atomic(_auth_db):
    _, auth = _auth_db
    a1 = await auth.create_user(
        "adm1@b.com", "A1", role="admin", password="pw",
    )
    a2 = await auth.create_user(
        "adm2@b.com", "A2", role="admin", password="pw",
    )
    v1 = await auth.create_user(
        "v@b.com", "V", role="viewer", password="pw",
    )
    disabled_admin = await auth.create_user(
        "dead@b.com", "D", role="admin", password="pw",
    )
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # Clear whatever must_change_password the create_user path set,
        # plus disable one admin to prove the filter skips them.
        await conn.execute(
            "UPDATE users SET must_change_password = 0 "
            "WHERE id = ANY($1::text[])",
            [a1.id, a2.id, v1.id, disabled_admin.id],
        )
        await conn.execute(
            "UPDATE users SET enabled = 0 WHERE id = $1",
            disabled_admin.id,
        )

    flagged = await auth.flag_all_admins_must_change_password()
    flagged_ids = {f["id"] for f in flagged}
    assert flagged_ids == {a1.id, a2.id}, (
        "only enabled admins should be flagged; viewer and disabled "
        f"admin must be skipped (got {flagged_ids})"
    )
    # Verify the DB state matches the returned list.
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, must_change_password FROM users "
            "WHERE id = ANY($1::text[]) ORDER BY id",
            [a1.id, a2.id, v1.id, disabled_admin.id],
        )
    state = {r["id"]: bool(r["must_change_password"]) for r in rows}
    assert state[a1.id] is True
    assert state[a2.id] is True
    assert state[v1.id] is False, "viewer must not be flagged"
    assert state[disabled_admin.id] is False, "disabled admin must not be flagged"


@pytest.mark.asyncio
async def test_flag_all_admins_must_change_password_no_admins(_auth_db):
    _, auth = _auth_db
    await auth.create_user("v@b.com", "V", role="viewer", password="pw")
    flagged = await auth.flag_all_admins_must_change_password()
    assert flagged == []

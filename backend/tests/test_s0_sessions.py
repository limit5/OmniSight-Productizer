"""S0 tests — session CRUD, revocation, audit session_id, bearer fingerprint.

Phase-3-Runtime-v2 SP-4.3a (2026-04-20): migrated from SQLite
tempfile fixture to pg_test_pool. Session CRUD is now pool-backed;
audit.log is too (SP-4.1). Direct ``db._conn().execute(...)``
accesses are replaced with inline ``get_pool().acquire()`` + $N
placeholders.
"""

from __future__ import annotations

import hashlib

import pytest


@pytest.fixture()
async def _s0_db(pg_test_pool, monkeypatch):
    # Clean slate per test — SP-4.3a session tests commit via pool.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, sessions, audit_log "
            "RESTART IDENTITY CASCADE"
        )
    from backend import db, auth, audit
    try:
        yield db, auth, audit
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, sessions, audit_log "
                "RESTART IDENTITY CASCADE"
            )


# ── session listing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_returns_active(_s0_db):
    db, auth, _ = _s0_db
    u = await auth.create_user("s@t.com", "S", role="viewer", password="pw")
    s1 = await auth.create_session(u.id, ip="1.2.3.4", user_agent="Chrome")
    s2 = await auth.create_session(u.id, ip="5.6.7.8", user_agent="Firefox")
    items = await auth.list_sessions(u.id)
    assert len(items) == 2
    tokens = {s["token"] for s in items}
    assert s1.token in tokens
    assert s2.token in tokens
    for s in items:
        assert "token_hint" in s
        assert s["token_hint"].startswith(s["token"][:4])


@pytest.mark.asyncio
async def test_list_sessions_excludes_expired(_s0_db):
    _, auth, _ = _s0_db
    u = await auth.create_user("e@t.com", "E", role="viewer", password="pw")
    s1 = await auth.create_session(u.id)
    # SP-4.3a: force-expire via the pool (direct sessions UPDATE).
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET expires_at = 0 WHERE token = $1",
            s1.token,
        )
    s2 = await auth.create_session(u.id)
    items = await auth.list_sessions(u.id)
    assert len(items) == 1
    assert items[0]["token"] == s2.token


# ── session revocation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_session(_s0_db):
    _, auth, _ = _s0_db
    u = await auth.create_user("r@t.com", "R", role="viewer", password="pw")
    s = await auth.create_session(u.id)
    assert await auth.get_session(s.token) is not None
    ok = await auth.revoke_session(s.token)
    assert ok
    assert await auth.get_session(s.token) is None


@pytest.mark.asyncio
async def test_revoke_nonexistent_session_returns_false(_s0_db):
    _, auth, _ = _s0_db
    ok = await auth.revoke_session("nonexistent-token")
    assert not ok


@pytest.mark.asyncio
async def test_revoke_other_sessions_keeps_current(_s0_db):
    _, auth, _ = _s0_db
    u = await auth.create_user("ro@t.com", "RO", role="viewer", password="pw")
    s1 = await auth.create_session(u.id)
    s2 = await auth.create_session(u.id)
    s3 = await auth.create_session(u.id)
    count = await auth.revoke_other_sessions(u.id, s1.token)
    assert count == 2
    assert await auth.get_session(s1.token) is not None
    assert await auth.get_session(s2.token) is None
    assert await auth.get_session(s3.token) is None


# ── audit session_id tracking ────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_stores_session_id(_s0_db):
    _, _, audit = _s0_db
    rid = await audit.log(
        "test_action", "test", "t1",
        after={"x": 1}, session_id="sess-abc",
    )
    assert rid is not None
    rows = await audit.query(limit=1)
    assert rows[0]["session_id"] == "sess-abc"


@pytest.mark.asyncio
async def test_audit_log_session_id_defaults_none(_s0_db):
    _, _, audit = _s0_db
    await audit.log("act", "kind", "id1")
    rows = await audit.query(limit=1)
    assert rows[0]["session_id"] is None


@pytest.mark.asyncio
async def test_audit_chain_intact_with_session_id(_s0_db):
    _, _, audit = _s0_db
    for i in range(10):
        await audit.log(f"act{i}", "thing", f"id{i}",
                        session_id=f"s-{i}" if i % 2 == 0 else None)
    ok, bad = await audit.verify_chain()
    assert ok and bad is None


# ── write_audit helper ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_audit_extracts_session(_s0_db):
    _, auth, audit = _s0_db

    class FakeState:
        session = auth.Session(
            token="tok-123", user_id="u1", csrf_token="", created_at=0, expires_at=0,
        )
        user = None

    class FakeRequest:
        state = FakeState()

    await audit.write_audit(
        FakeRequest(), action="test", entity_kind="thing",
        entity_id="x", actor="me@test.com",
    )
    rows = await audit.query(limit=1)
    assert rows[0]["session_id"] == "tok-123"
    assert rows[0]["actor"] == "me@test.com"


@pytest.mark.asyncio
async def test_write_audit_no_session(_s0_db):
    _, _, audit = _s0_db

    class FakeState:
        session = None
        user = None

    class FakeRequest:
        state = FakeState()

    await audit.write_audit(FakeRequest(), "act", "kind", actor="sys")
    rows = await audit.query(limit=1)
    assert rows[0]["session_id"] is None


# ── bearer token fingerprint ─────────────────────────────────────


@pytest.mark.skip(
    reason="Epic 5: api_keys.validate_bearer still uses db._conn(); "
           "unstick when api_keys.py ports off the compat wrapper."
)
@pytest.mark.asyncio
async def test_bearer_session_fingerprint(_s0_db, monkeypatch):
    _, auth, _ = _s0_db
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")
    monkeypatch.setenv("OMNISIGHT_DECISION_BEARER", "my-secret-token")

    class FakeClient:
        host = "127.0.0.1"

    class FakeRequest:
        headers = {"authorization": "Bearer my-secret-token"}
        cookies: dict = {}
        method = "GET"
        client = FakeClient()

        class state:
            session = None

    req = FakeRequest()
    user = await auth.current_user(req)
    assert user.id == "anonymous"
    sess = req.state.session
    assert sess is not None
    fp = hashlib.sha256(b"my-secret-token").hexdigest()[:12]
    assert sess.token == f"bearer:{fp}"


# ── Session dataclass has new fields ─────────────────────────────


@pytest.mark.asyncio
async def test_session_has_ip_ua_fields(_s0_db):
    _, auth, _ = _s0_db
    u = await auth.create_user("f@t.com", "F", role="viewer", password="pw")
    s = await auth.create_session(u.id, ip="10.0.0.1", user_agent="TestAgent/1.0")
    assert s.ip == "10.0.0.1"
    assert s.user_agent == "TestAgent/1.0"
    fetched = await auth.get_session(s.token)
    assert fetched.ip == "10.0.0.1"
    assert fetched.user_agent == "TestAgent/1.0"
    assert fetched.mfa_verified is False
    assert fetched.metadata == "{}"
    assert fetched.rotated_from is None


# ── mask_token ───────────────────────────────────────────────────


def test_mask_token():
    from backend.auth import _mask_token
    assert _mask_token("abcdefghijklmnop") == "abcd***mnop"
    assert _mask_token("short") == "***"

"""J5: Per-session Operation Mode tests.

Verifies that operation_mode is stored per-session in
``sessions.metadata``, ``_ModeSlot`` reads per-session cap, and two
sessions with different modes each see their own cap.

Step B.1 / task #102 (2026-04-21): ported from the aiosqlite-plus-
monkeypatch pattern to pool-backed ``pg_test_pool`` + real session
rows. The pre-fix pattern was:

    original_conn = auth._conn
    async def patched_conn():
        return <aiosqlite in-memory db>
    auth._conn = patched_conn

which became a no-op after SP-4.3a/b moved ``auth.update_session_
metadata`` / ``get_session`` to the pool — the monkey-patch
intercepted a target that's no longer on the call path. Tests
silently passed against production PG (empty rows → match 0,
return {}). Rewriting seeds real session rows via the pool and
lets the production pool-native functions do their work.

Unblocks the removal of ``auth._conn`` stub (Step C / Epic 7).
"""

from __future__ import annotations

import json
import time

import pytest

from backend import decision_engine as de


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset():
    de._reset_for_tests()
    yield
    de._reset_for_tests()


@pytest.fixture()
async def _sessions_db(pg_test_pool, monkeypatch):
    """Seed a clean sessions table per test. TRUNCATE covers any
    leakage from cross-file test order."""
    # Seed a synthetic user row (FK users → sessions).
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE sessions, users RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO users (id, email, name, role, enabled, "
            "password_hash, tenant_id, created_at) "
            "VALUES ($1, $2, $3, $4, 1, $5, $6, $7) "
            "ON CONFLICT (id) DO NOTHING",
            "u1", "u1@test.local", "U1", "admin", "hash", "t-default",
            "2024-01-01 00:00:00",
        )
    try:
        yield pg_test_pool
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE sessions, users RESTART IDENTITY CASCADE"
            )


async def _insert_session(pool, token, user_id="u1", metadata=None):
    """Insert a minimal session row via the pool — matches the
    schema auth.create_session uses in production."""
    now = time.time()
    meta = json.dumps(metadata or {})
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (token, user_id, csrf_token, "
            "created_at, expires_at, last_seen_at, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            token, user_id, "csrf", now, now + 86400, now, meta,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session metadata helpers (pure, no DB)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSessionMetadataHelpers:

    def test_get_session_metadata_parses_json(self):
        from backend.auth import Session, get_session_metadata
        s = Session(token="t", user_id="u", csrf_token="c",
                    created_at=0, expires_at=0,
                    metadata='{"operation_mode": "turbo"}')
        meta = get_session_metadata(s)
        assert meta == {"operation_mode": "turbo"}

    def test_get_session_metadata_empty(self):
        from backend.auth import Session, get_session_metadata
        s = Session(token="t", user_id="u", csrf_token="c",
                    created_at=0, expires_at=0, metadata="{}")
        assert get_session_metadata(s) == {}

    def test_get_session_metadata_corrupt(self):
        from backend.auth import Session, get_session_metadata
        s = Session(token="t", user_id="u", csrf_token="c",
                    created_at=0, expires_at=0, metadata="not-json")
        assert get_session_metadata(s) == {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  update_session_metadata (pool-backed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestUpdateSessionMetadata:

    @pytest.mark.asyncio
    async def test_update_merges(self, _sessions_db):
        from backend import auth
        await _insert_session(_sessions_db, "tok1", metadata={"foo": "bar"})
        result = await auth.update_session_metadata(
            "tok1", {"operation_mode": "turbo"},
        )
        assert result["foo"] == "bar"
        assert result["operation_mode"] == "turbo"
        # Verify persistence by re-reading through the pool.
        async with _sessions_db.acquire() as conn:
            stored_json = await conn.fetchval(
                "SELECT metadata FROM sessions WHERE token = $1",
                "tok1",
            )
        stored = json.loads(stored_json)
        assert stored["operation_mode"] == "turbo"
        assert stored["foo"] == "bar"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-session mode in decision_engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGetSessionModeAsync:

    @pytest.mark.asyncio
    async def test_no_session_falls_back_to_global(self):
        de.set_mode("full_auto")
        mode = await de.get_session_mode_async(None)
        assert mode == de.OperationMode.full_auto

    @pytest.mark.asyncio
    async def test_session_with_mode_override(self, _sessions_db):
        await _insert_session(
            _sessions_db, "sess-a", metadata={"operation_mode": "turbo"},
        )
        mode = await de.get_session_mode_async("sess-a")
        assert mode == de.OperationMode.turbo

    @pytest.mark.asyncio
    async def test_session_without_mode_falls_back_to_global(self, _sessions_db):
        de.set_mode("manual")
        await _insert_session(_sessions_db, "sess-b", metadata={})
        mode = await de.get_session_mode_async("sess-b")
        assert mode == de.OperationMode.manual


class TestSetSessionMode:

    @pytest.mark.asyncio
    async def test_set_session_mode_persists(self, _sessions_db):
        await _insert_session(_sessions_db, "sess-c", metadata={})
        result = await de.set_session_mode("sess-c", "turbo")
        assert result == de.OperationMode.turbo
        mode = await de.get_session_mode_async("sess-c")
        assert mode == de.OperationMode.turbo

    @pytest.mark.asyncio
    async def test_set_session_mode_invalid(self):
        with pytest.raises(ValueError, match="unknown mode"):
            await de.set_session_mode("any", "ludicrous")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ModeSlot per-session cap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestModeSlotPerSession:

    def test_slot_default_uses_global_mode(self):
        de.set_mode("supervised")
        slot = de.parallel_slot()
        assert not slot.locked()

    def test_slot_with_session_token_returns_new_instance(self):
        slot = de.parallel_slot(session_token="some-token")
        assert slot is not de._mode_slot_singleton

    def test_slot_without_session_returns_singleton(self):
        slot = de.parallel_slot()
        assert slot is de._mode_slot_singleton


class TestDualSessionModeCap:
    """The core J5 requirement: session A (turbo, cap 8) and session B
    (supervised, cap 2) each see their own mode cap."""

    @pytest.mark.asyncio
    async def test_different_sessions_different_caps(self, _sessions_db):
        await _insert_session(
            _sessions_db, "sess-turbo",
            metadata={"operation_mode": "turbo"},
        )
        await _insert_session(
            _sessions_db, "sess-sup",
            metadata={"operation_mode": "supervised"},
        )
        mode_a = await de.get_session_mode_async("sess-turbo")
        mode_b = await de.get_session_mode_async("sess-sup")
        assert mode_a == de.OperationMode.turbo
        assert mode_b == de.OperationMode.supervised

        cap_a = de._PARALLEL_BUDGET[mode_a]
        cap_b = de._PARALLEL_BUDGET[mode_b]
        assert cap_a == 8
        assert cap_b == 2

        slot_a = de.parallel_slot(session_token="sess-turbo")
        slot_b = de.parallel_slot(session_token="sess-sup")
        cap_a_slot = await slot_a._get_cap_async()
        cap_b_slot = await slot_b._get_cap_async()
        assert cap_a_slot == 8
        assert cap_b_slot == 2

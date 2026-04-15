"""J5: Per-session Operation Mode tests.

Verifies that operation_mode is stored per-session in sessions.metadata,
_ModeSlot reads per-session cap, and two sessions with different modes
each see their own cap.
"""

from __future__ import annotations

import asyncio
import json

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
def db_conn():
    """Yield a live aiosqlite connection with the sessions table."""
    import aiosqlite

    async def _make():
        conn = aiosqlite.connect(":memory:")
        db = await conn.__aenter__()
        db.row_factory = aiosqlite.Row
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token           TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                csrf_token      TEXT NOT NULL,
                created_at      REAL NOT NULL,
                expires_at      REAL NOT NULL,
                last_seen_at    REAL NOT NULL,
                ip              TEXT NOT NULL DEFAULT '',
                user_agent      TEXT NOT NULL DEFAULT '',
                metadata        TEXT NOT NULL DEFAULT '{}',
                mfa_verified    INTEGER NOT NULL DEFAULT 0,
                rotated_from    TEXT
            )
        """)
        await db.commit()
        return db

    loop = asyncio.new_event_loop()
    db = loop.run_until_complete(_make())
    yield db, loop
    loop.run_until_complete(db.close())
    loop.close()


async def _insert_session(db, token, user_id="u1", metadata=None):
    """Insert a minimal session row."""
    import time
    now = time.time()
    meta = json.dumps(metadata or {})
    await db.execute(
        "INSERT INTO sessions (token, user_id, csrf_token, created_at, "
        "expires_at, last_seen_at, metadata) VALUES (?,?,?,?,?,?,?)",
        (token, user_id, "csrf", now, now + 86400, now, meta),
    )
    await db.commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session metadata helpers
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


class TestUpdateSessionMetadata:

    def test_update_merges(self, db_conn):
        db, loop = db_conn
        from backend import auth

        async def run():
            await _insert_session(db, "tok1", metadata={"foo": "bar"})
            original_conn = auth._conn

            async def patched_conn():
                return db
            auth._conn = patched_conn
            try:
                result = await auth.update_session_metadata("tok1", {"operation_mode": "turbo"})
                assert result["foo"] == "bar"
                assert result["operation_mode"] == "turbo"
                async with db.execute("SELECT metadata FROM sessions WHERE token='tok1'") as cur:
                    r = await cur.fetchone()
                stored = json.loads(r["metadata"])
                assert stored["operation_mode"] == "turbo"
            finally:
                auth._conn = original_conn

        loop.run_until_complete(run())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-session mode in decision_engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGetSessionModeAsync:

    def test_no_session_falls_back_to_global(self):
        de.set_mode("full_auto")
        mode = asyncio.get_event_loop().run_until_complete(
            de.get_session_mode_async(None)
        )
        assert mode == de.OperationMode.full_auto

    def test_session_with_mode_override(self, db_conn):
        db, loop = db_conn
        from backend import auth

        async def run():
            await _insert_session(db, "sess-a", metadata={"operation_mode": "turbo"})
            original_conn = auth._conn

            async def patched_conn():
                return db
            auth._conn = patched_conn
            try:
                mode = await de.get_session_mode_async("sess-a")
                assert mode == de.OperationMode.turbo
            finally:
                auth._conn = original_conn

        loop.run_until_complete(run())

    def test_session_without_mode_falls_back_to_global(self, db_conn):
        db, loop = db_conn
        from backend import auth
        de.set_mode("manual")

        async def run():
            await _insert_session(db, "sess-b", metadata={})
            original_conn = auth._conn

            async def patched_conn():
                return db
            auth._conn = patched_conn
            try:
                mode = await de.get_session_mode_async("sess-b")
                assert mode == de.OperationMode.manual
            finally:
                auth._conn = original_conn

        loop.run_until_complete(run())


class TestSetSessionMode:

    def test_set_session_mode_persists(self, db_conn):
        db, loop = db_conn
        from backend import auth

        async def run():
            await _insert_session(db, "sess-c", metadata={})
            original_conn = auth._conn

            async def patched_conn():
                return db
            auth._conn = patched_conn
            try:
                result = await de.set_session_mode("sess-c", "turbo")
                assert result == de.OperationMode.turbo
                mode = await de.get_session_mode_async("sess-c")
                assert mode == de.OperationMode.turbo
            finally:
                auth._conn = original_conn

        loop.run_until_complete(run())

    def test_set_session_mode_invalid(self, db_conn):
        db, loop = db_conn

        async def run():
            with pytest.raises(ValueError, match="unknown mode"):
                await de.set_session_mode("any", "ludicrous")

        loop.run_until_complete(run())


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

    def test_different_sessions_different_caps(self, db_conn):
        db, loop = db_conn
        from backend import auth

        async def run():
            await _insert_session(db, "sess-turbo", metadata={"operation_mode": "turbo"})
            await _insert_session(db, "sess-sup", metadata={"operation_mode": "supervised"})
            original_conn = auth._conn

            async def patched_conn():
                return db
            auth._conn = patched_conn
            try:
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
            finally:
                auth._conn = original_conn

        loop.run_until_complete(run())

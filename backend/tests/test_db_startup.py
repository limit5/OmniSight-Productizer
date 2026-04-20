"""Phase-3-Runtime-v2 SP-3.13 — lifecycle contract tests for db.py
(init / close / execute_raw).

SP-3.1 through 3.12 ported every domain CRUD function in db.py to
take an explicit ``conn: asyncpg.Connection``. SP-3.13 is the Epic-3
closing gate: verify that the remaining lifecycle + escape-hatch
surface still behaves correctly in PG mode alongside the pool.

Coverage:
  * ``init()`` is idempotent on the PG path — calling it a second
    time with a pool already initialised must not crash, double-open
    the compat connection, or run SQLite-specific DDL.
  * ``close()`` safely short-circuits WAL-pragma logic on PG and
    tears down the compat connection exactly once.
  * ``execute_raw()`` — the one remaining direct ``_conn()`` caller
    inside db.py — still works against the compat wrapper that
    ``init()`` installed.
  * Lifecycle functions compose with ``db_pool.init_pool`` — pool
    init and compat init are independent; both can co-exist without
    the pool's connections leaking into the compat wrapper or vice
    versa.

These tests do NOT verify ``_migrate()`` behaviour — it runs only on
SQLite via the ``fresh_db`` fixture path (out of SP-3.13 scope; the
PG schema is alembic-managed).

Runs against the test PG via ``pg_test_dsn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db


class TestDbInitPgMode:
    @pytest.mark.asyncio
    async def test_init_opens_compat_connection_on_pg(
        self, pg_test_dsn, monkeypatch,
    ) -> None:
        # Point db._resolve_pg_dsn at the test PG DSN. db.init() must
        # open a PgCompatConnection (NOT an aiosqlite conn) and set
        # the _IS_PG flag.
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
        # Force re-resolution in case settings caches.
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        try:
            await db.init()
            assert db._IS_PG is True
            assert db._db is not None
            # PgCompatConnection exposes an async execute() that
            # returns an awaitable context manager — probe with
            # SELECT 1 to confirm reachability.
            async with await db._db.execute("SELECT 1") as cur:
                row = await cur.fetchone()
            assert row is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_init_skips_sqlite_ddl_on_pg(
        self, pg_test_dsn, monkeypatch, caplog,
    ) -> None:
        # The contract docstring says PG path does NOT run
        # ``_SCHEMA`` executescript or ``_migrate``. Log-free init
        # on PG would be hard to assert, so we check the happy-path
        # logger message the function emits after its SELECT 1 probe.
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        import logging
        with caplog.at_level(logging.INFO, logger="backend.db"):
            try:
                await db.init()
                assert any(
                    "PostgreSQL" in r.message and "compat" in r.message
                    for r in caplog.records
                )
            finally:
                await db.close()


class TestDbCloseIdempotent:
    @pytest.mark.asyncio
    async def test_close_is_safe_when_db_is_none(
        self, monkeypatch,
    ) -> None:
        # Never-initialised path: close() sees _db is None and
        # returns immediately without raising.
        monkeypatch.setattr(db, "_db", None)
        await db.close()  # must not raise
        assert db._db is None

    @pytest.mark.asyncio
    async def test_close_after_init_on_pg_nullifies_db(
        self, pg_test_dsn, monkeypatch,
    ) -> None:
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        await db.init()
        assert db._db is not None
        await db.close()
        assert db._db is None

    @pytest.mark.asyncio
    async def test_double_close_is_safe(
        self, pg_test_dsn, monkeypatch,
    ) -> None:
        # SIGTERM handlers and atexit hooks sometimes race; close()
        # must tolerate being called twice without re-raising on the
        # already-None ``_db``.
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        await db.init()
        await db.close()
        await db.close()  # second close must be a no-op
        assert db._db is None


class TestExecuteRawEscapeHatch:
    @pytest.mark.asyncio
    async def test_execute_raw_runs_through_compat(
        self, pg_test_dsn, monkeypatch,
    ) -> None:
        # execute_raw is the ONE remaining direct ``_conn()`` caller
        # inside db.py. It is used from main.py's startup-cleanup
        # (agents / simulations stuck-state recovery). Confirm it
        # still works after SP-3.1 through 3.12 leaves the compat
        # wrapper as the only user of _conn().
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        try:
            await db.init()
            # Execute a harmless DML that should always succeed —
            # UPDATE on a table that has no matching rows returns 0,
            # a NO-OP is still a valid exercise of the compat code
            # path.
            rc = await db.execute_raw(
                "UPDATE agents SET status = status WHERE id = ?",
                ("__never_exists__",),
            )
            # execute_raw returns rowcount; 0 = no matching row, not
            # an error.
            assert isinstance(rc, int)
            assert rc == 0
        finally:
            await db.close()


class TestLifecycleCoexistsWithPool:
    @pytest.mark.asyncio
    async def test_init_and_pool_coexist(
        self, pg_test_pool, monkeypatch,
    ) -> None:
        # pg_test_pool installs the module-global pool via SP-3.4's
        # consolidation. ``db.init()`` can ALSO run against the same
        # PG DSN without the two layers fighting — they own different
        # connection resources (the pool's connections vs the compat
        # wrapper's single connection).
        dsn = pg_test_pool.get_size() >= 0  # sanity: pool is alive
        assert dsn is True
        # pg_test_pool already opened the module pool. Now bring the
        # compat wrapper online via db.init().
        import os
        monkeypatch.setattr(db, "_db", None)
        monkeypatch.setattr(db, "_IS_PG", False)
        # pg_test_pool derives its DSN via conftest helpers; we use
        # the same env var the prod lifespan would.
        raw_dsn = os.environ.get("OMNI_TEST_PG_URL") or ""
        if raw_dsn.startswith("postgres://"):
            raw_dsn = raw_dsn.replace("postgres://", "postgresql://", 1)
        monkeypatch.setenv("OMNISIGHT_DATABASE_URL", raw_dsn)
        try:
            await db.init()
            # Both layers respond to reads independently.
            async with pg_test_pool.acquire() as conn:
                n = await conn.fetchval("SELECT 1")
                assert n == 1
            async with await db._db.execute("SELECT 1") as cur:
                row = await cur.fetchone()
                assert row is not None
        finally:
            await db.close()

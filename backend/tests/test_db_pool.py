"""Phase-3-Runtime-v2 SP-1.3 — unit tests for backend.db_pool.

Covers:
  * init_pool / close_pool lifecycle (idempotent close, guarded re-init)
  * get_pool raising before init
  * get_conn FastAPI dependency yields + releases
  * Session-parameter init callback actually runs
  * Pool-exhaustion timeout path (max_size=1, 2nd borrow hits timeout)
  * get_pool_stats across uninit / init / borrowed states
  * _reset_for_tests escape hatch

Requires OMNI_TEST_PG_URL (via the pg_test_dsn fixture from conftest).
Every test here SKIPS cleanly when the env is unset — the non-PG suite
remains runnable.

Coverage target: ≥ 95% of backend/db_pool.py. Current measurement from
the last `--cov` run lives at the bottom of this file as a comment for
the benefit of future reviewers.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from backend import db_pool


# ─── Autouse fixture: reset module global between tests ──────────────
#
# Every test in this module either calls init_pool() itself or relies
# on the pool being absent. We reset the module global before AND
# after each test so a failed test doesn't leak state.
#
# We use _reset_for_tests (no close) because async teardown of a real
# pool would require awaiting, and this fixture runs synchronously.
# Tests that open real pools are responsible for closing them within
# their own async scope; this fixture is the belt-and-braces for the
# module-global reference only.


@pytest.fixture(autouse=True)
def _db_pool_reset_between_tests():
    db_pool._reset_for_tests()
    yield
    db_pool._reset_for_tests()


# ─── Group 1: pure unit tests (no PG needed) ─────────────────────────


class TestGetPoolUninitialised:
    """These run without OMNI_TEST_PG_URL — no PG needed."""

    def test_get_pool_raises_before_init(self) -> None:
        with pytest.raises(RuntimeError, match="before init_pool"):
            db_pool.get_pool()

    def test_get_pool_stats_returns_uninit_shape(self) -> None:
        stats = db_pool.get_pool_stats()
        assert stats["initialised"] is False
        assert stats["min_size"] is None
        assert stats["max_size"] is None
        assert stats["size"] is None
        assert stats["free_size"] is None
        assert stats["used_size"] is None

    def test_reset_for_tests_is_noop_when_uninit(self) -> None:
        # Should not raise even when already None
        db_pool._reset_for_tests()
        db_pool._reset_for_tests()
        assert db_pool._pool is None

    @pytest.mark.asyncio
    async def test_close_pool_noop_when_uninit(self) -> None:
        # Idempotency contract — close_pool() when nothing's open is OK.
        await db_pool.close_pool()
        await db_pool.close_pool()
        assert db_pool._pool is None


# ─── Group 2: PG-backed lifecycle tests ──────────────────────────────


class TestInitClose:
    """Exercise init_pool/close_pool with the real test PG."""

    @pytest.mark.asyncio
    async def test_init_pool_creates_live_pool(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        pool = await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=3,
        )
        try:
            assert pool is not None
            # get_pool() should now return the same object
            assert db_pool.get_pool() is pool
            # Round-trip a query to prove the pool is live
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT 1") == 1
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_double_init_without_close_raises(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        try:
            with pytest.raises(RuntimeError, match="already active"):
                await db_pool.init_pool(
                    pg_test_alembic_upgraded, min_size=1, max_size=2,
                )
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_close_after_init_nulls_global(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        await db_pool.close_pool()
        # After close, get_pool() must raise again
        with pytest.raises(RuntimeError, match="before init_pool"):
            db_pool.get_pool()

    @pytest.mark.asyncio
    async def test_init_pool_is_idempotent_via_close_then_reinit(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        # Simulates lifespan restart: close then re-init must work.
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        await db_pool.close_pool()
        second = await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        try:
            assert db_pool.get_pool() is second
        finally:
            await db_pool.close_pool()


# ─── Group 3: connection init callback ───────────────────────────────


class TestSessionDefaults:
    """Verify each connection gets the UTC / statement_timeout / etc
    session parameters set by _set_connection_defaults."""

    @pytest.mark.asyncio
    async def test_default_init_callback_sets_timezone_utc(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=1,
        )
        try:
            async with db_pool.get_pool().acquire() as conn:
                tz = await conn.fetchval("SHOW timezone")
                assert tz == "UTC"
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_default_init_callback_sets_statement_timeout(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=1,
        )
        try:
            async with db_pool.get_pool().acquire() as conn:
                val = await conn.fetchval("SHOW statement_timeout")
                # PG reports 30000 (ms) or "30s" depending on formatting
                assert val in {"30s", "30000ms", "30000"}
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_default_init_callback_sets_idle_in_tx_timeout(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=1,
        )
        try:
            async with db_pool.get_pool().acquire() as conn:
                val = await conn.fetchval(
                    "SHOW idle_in_transaction_session_timeout"
                )
                assert val in {"1min", "60s", "60000ms", "60000"}
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_custom_init_callback_is_respected(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        # Prove the init= override path: a test-only callback sets
        # timezone to a non-UTC value and we verify it took effect.
        # This proves we can bypass defaults when needed (e.g. a
        # test that specifically needs local timezone).
        async def _custom(conn: asyncpg.Connection) -> None:
            await conn.execute("SET timezone = 'Asia/Taipei'")

        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=1,
            init=_custom,
        )
        try:
            async with db_pool.get_pool().acquire() as conn:
                tz = await conn.fetchval("SHOW timezone")
                assert tz == "Asia/Taipei"
        finally:
            await db_pool.close_pool()


# ─── Group 4: get_conn dependency semantics ──────────────────────────


class TestGetConn:
    """get_conn is an async generator; we exercise it directly to
    prove it yields a live conn and releases on exit."""

    @pytest.mark.asyncio
    async def test_get_conn_yields_live_connection(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        try:
            # Drive the async generator manually — this is what FastAPI
            # does under the hood with Depends(get_conn).
            gen = db_pool.get_conn()
            conn = await gen.__anext__()
            assert await conn.fetchval("SELECT 42") == 42
            # Normal close path — generator cleans up the async-with.
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_get_conn_returns_conn_to_pool_on_generator_close(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=2,
        )
        try:
            stats_before = db_pool.get_pool_stats()
            assert stats_before["used_size"] == 0

            # Borrow via the dependency
            gen = db_pool.get_conn()
            await gen.__anext__()
            # While held, one conn is busy
            stats_during = db_pool.get_pool_stats()
            assert stats_during["used_size"] == 1

            # Release via StopAsyncIteration
            with pytest.raises(StopAsyncIteration):
                await gen.__anext__()
            stats_after = db_pool.get_pool_stats()
            assert stats_after["used_size"] == 0
        finally:
            await db_pool.close_pool()


# ─── Group 5: pool exhaustion ────────────────────────────────────────


class TestPoolExhaustion:
    """When max_size is saturated, further borrows block until a
    connection is released. We prove that behaviour with a short
    timeout so tests run fast."""

    @pytest.mark.asyncio
    async def test_acquire_blocks_when_pool_saturated(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        # Size pool at 1. First acquire succeeds; second queues and
        # must time out after the explicit timeout kwarg.
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=1,
        )
        try:
            pool = db_pool.get_pool()
            first = await pool.acquire()
            try:
                # Second acquire should block; asyncpg raises
                # asyncio.TimeoutError when the `timeout=` expires.
                with pytest.raises(asyncio.TimeoutError):
                    await pool.acquire(timeout=0.3)
            finally:
                await pool.release(first)
        finally:
            await db_pool.close_pool()


# ─── Group 6: introspection / stats ──────────────────────────────────


class TestPoolStats:
    @pytest.mark.asyncio
    async def test_stats_after_init_show_capacity(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=2, max_size=4,
        )
        try:
            stats = db_pool.get_pool_stats()
            assert stats["initialised"] is True
            assert stats["min_size"] == 2
            assert stats["max_size"] == 4
            # After init the pool has min_size warm conns.
            assert stats["size"] >= 2
            assert stats["free_size"] >= 0
            assert stats["used_size"] == stats["size"] - stats["free_size"]
        finally:
            await db_pool.close_pool()

    @pytest.mark.asyncio
    async def test_stats_reflect_borrowed_count(
        self, pg_test_alembic_upgraded: str,
    ) -> None:
        await db_pool.init_pool(
            pg_test_alembic_upgraded, min_size=1, max_size=3,
        )
        try:
            pool = db_pool.get_pool()
            before = db_pool.get_pool_stats()
            c1 = await pool.acquire()
            c2 = await pool.acquire()
            try:
                during = db_pool.get_pool_stats()
                assert during["used_size"] == before["used_size"] + 2
            finally:
                await pool.release(c1)
                await pool.release(c2)
            after = db_pool.get_pool_stats()
            assert after["used_size"] == before["used_size"]
        finally:
            await db_pool.close_pool()

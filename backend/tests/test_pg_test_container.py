"""Phase-3-Runtime-v2 SP-1.2 — smoke test for the test PG fixtures.

These tests are meta-tests: they verify the `pg_test_dsn /
pg_test_alembic_upgraded / pg_test_pool / pg_test_conn` fixtures in
``conftest.py`` actually deliver a usable PG with HEAD schema and
isolate state between tests.

Every test here requires ``OMNI_TEST_PG_URL`` to be set — if unset, the
fixtures skip the test cleanly (see conftest.py for the skip logic).

Why these tests exist: SP-1.2's deliverable is "test-PG container works".
Without proving it end-to-end, later SPs that depend on the fixtures
might fail for ambiguous reasons (fixture bug vs. code under test bug).
These tests pin down the fixture contract itself.
"""

from __future__ import annotations

import os

import pytest


# ─── Sync smoke ────────────────────────────────────────────────────


class TestPgTestDsnFixture:
    def test_dsn_is_libpq_form(self, pg_test_dsn: str) -> None:
        # Fixture normalises SQLAlchemy-style URLs to plain libpq.
        assert pg_test_dsn.startswith("postgresql://"), (
            f"pg_test_dsn must return a libpq-shape DSN usable by asyncpg; "
            f"got {pg_test_dsn!r}"
        )
        assert "+psycopg2" not in pg_test_dsn
        assert "+asyncpg" not in pg_test_dsn

    def test_dsn_points_at_test_db(self, pg_test_dsn: str) -> None:
        # Guardrail: if someone runs the test suite against prod by
        # accident, this test surfaces the misconfiguration early. The
        # test PG container uses database name `omni_test` by convention
        # (docker-compose.test.yml). We assert the name is in the DSN
        # — not ultra-strict, but catches the obvious mistake.
        assert "omni_test" in pg_test_dsn, (
            f"pg_test_dsn should reference the `omni_test` database to "
            f"avoid accidentally pointing at prod; got {pg_test_dsn!r}"
        )


class TestAlembicUpgradedFixture:
    def test_upgrade_returns_same_dsn(
        self, pg_test_dsn: str, pg_test_alembic_upgraded: str,
    ) -> None:
        # Contract: the alembic fixture returns the same DSN it received
        # (so downstream fixtures can chain on it).
        assert pg_test_alembic_upgraded == pg_test_dsn


# ─── Async smoke via asyncpg ──────────────────────────────────────


class TestPgTestPoolFixture:
    @pytest.mark.asyncio
    async def test_pool_connects(self, pg_test_pool) -> None:
        # Pool is live; can borrow a conn and round-trip a query.
        async with pg_test_pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1

    @pytest.mark.asyncio
    async def test_pool_alembic_head_schema_present(self, pg_test_pool) -> None:
        # After alembic upgrade head, core tables from the 0001 baseline
        # should exist. We pick `audit_log` because every migration phase
        # since the beginning has it, and it's the most load-bearing
        # table for data integrity (hash chain).
        async with pg_test_pool.acquire() as conn:
            exists = await conn.fetchval(
                """SELECT EXISTS (
                      SELECT FROM information_schema.tables
                      WHERE table_schema = 'public'
                        AND table_name = 'audit_log'
                   )"""
            )
            assert exists is True, (
                "alembic upgrade head should have created audit_log — "
                "either the migration failed or the fixture isn't chaining "
                "through pg_test_alembic_upgraded"
            )


class TestPgTestConnIsolation:
    """Two tests in sequence that prove savepoint rollback actually
    isolates state — if the fixture leaked, test #2 would see test #1's
    INSERT. Requires tests to run in the declared order (pytest default).
    """

    @pytest.mark.asyncio
    async def test_a_insert_row_visible_within_test(
        self, pg_test_conn,
    ) -> None:
        # Use a throwaway scratch table name so we don't collide with any
        # real schema. Create + insert + read within the savepoint.
        await pg_test_conn.execute(
            "CREATE TEMP TABLE _sp12_scratch (v INT)"
        )
        await pg_test_conn.execute("INSERT INTO _sp12_scratch VALUES (42)")
        rows = await pg_test_conn.fetch("SELECT v FROM _sp12_scratch")
        assert [r["v"] for r in rows] == [42]
        # Fixture teardown will rollback — test B should NOT see this row.

    @pytest.mark.asyncio
    async def test_b_previous_test_data_rolled_back(
        self, pg_test_conn,
    ) -> None:
        # The TEMP TABLE from test A should be gone (TEMP is session-
        # scoped + we rolled back). A new TEMP table here should be
        # creatable (no "already exists" collision).
        await pg_test_conn.execute(
            "CREATE TEMP TABLE _sp12_scratch (v INT)"
        )
        rows = await pg_test_conn.fetch("SELECT v FROM _sp12_scratch")
        assert rows == [], (
            "pg_test_conn failed to roll back between tests — test A's "
            "INSERT is still visible. Savepoint/rollback logic in conftest "
            "needs inspection."
        )


class TestPgTestConnSavepointNested:
    @pytest.mark.asyncio
    async def test_nested_transaction_uses_savepoint(
        self, pg_test_conn,
    ) -> None:
        # Inside the outer fixture-level transaction, asyncpg should
        # transparently use a SAVEPOINT for nested `conn.transaction()`.
        # This is the pattern production code uses for FTS5 rollback in
        # insert_episodic_memory etc.
        await pg_test_conn.execute(
            "CREATE TEMP TABLE _sp12_nested (v INT)"
        )

        try:
            async with pg_test_conn.transaction():
                await pg_test_conn.execute(
                    "INSERT INTO _sp12_nested VALUES (1)"
                )
                raise RuntimeError("boom")  # force nested rollback
        except RuntimeError:
            pass

        # Outer tx is still alive, row #1 rolled back by savepoint.
        await pg_test_conn.execute("INSERT INTO _sp12_nested VALUES (2)")
        rows = await pg_test_conn.fetch("SELECT v FROM _sp12_nested ORDER BY v")
        assert [r["v"] for r in rows] == [2], (
            "asyncpg should have rolled back the nested `boom` block via "
            "a savepoint, leaving the outer tx intact. Found: "
            f"{[dict(r) for r in rows]!r}"
        )


# ─── Skip-path smoke (always runs, regardless of OMNI_TEST_PG_URL) ──


class TestSkipPathWhenUnset:
    """Runs unconditionally — verifies the skip behaviour doesn't
    silently turn into a hang or a weird ImportError when env unset.
    """

    def test_dsn_skip_message_is_informative_when_unset(
        self, monkeypatch,
    ) -> None:
        # Snapshot current env + blank it for this test
        monkeypatch.delenv("OMNI_TEST_PG_URL", raising=False)
        # Re-call the helper inside the fixture — we're not asking the
        # fixture to run (that would skip us), just the normalisation
        # function, to prove "unset" maps to empty string.
        from backend.tests.conftest import _omni_test_pg_dsn_normalised
        result = _omni_test_pg_dsn_normalised()
        assert result == "", (
            "Unset OMNI_TEST_PG_URL should normalise to empty string so "
            "the fixture's skip branch triggers; got " + repr(result)
        )

    def test_dsn_normalisation_strips_sqlalchemy_prefix(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv(
            "OMNI_TEST_PG_URL",
            "postgresql+psycopg2://u:p@h:5/d",
        )
        from backend.tests.conftest import _omni_test_pg_dsn_normalised
        assert _omni_test_pg_dsn_normalised() == "postgresql://u:p@h:5/d"

        monkeypatch.setenv(
            "OMNI_TEST_PG_URL",
            "postgresql+asyncpg://u:p@h:5/d",
        )
        assert _omni_test_pg_dsn_normalised() == "postgresql://u:p@h:5/d"

    def test_dsn_normalisation_passthrough_when_already_libpq(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("OMNI_TEST_PG_URL", "postgresql://u:p@h:5/d")
        from backend.tests.conftest import _omni_test_pg_dsn_normalised
        assert _omni_test_pg_dsn_normalised() == "postgresql://u:p@h:5/d"

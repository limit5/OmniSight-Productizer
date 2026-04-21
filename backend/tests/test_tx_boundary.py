"""Phase-3-Runtime-v2 SP-9.3 / task #82 — transaction boundary guards.

Pins down the transaction semantics that the Phase-3-v2 polymorphic-
conn pattern relies on. Every domain helper in the backend follows
the shape::

    async def do_work(conn=None, ...):
        if conn is None:
            async with get_pool().acquire() as owned:
                async with owned.transaction():
                    return await _impl(owned, ...)
        else:
            async with conn.transaction():
                return await _impl(conn, ...)

The caller-owned branch becomes a PG SAVEPOINT on a running
transaction. This file tests the four scenarios the Epic 9.3 spec
calls out — explicit rollback, exception rollback, nested savepoint,
concurrent tx — plus a few adjacent invariants that fall out of
asyncpg's transaction implementation and that prod code relies on.

Why this matters: the v1 compat wrapper had a single connection +
``asyncio.Lock`` + lazy ``BEGIN`` that didn't behave like a real
transaction at all (no real rollback, no savepoints). v2's move to
asyncpg-native makes these semantics available — but only if the
code on top actually uses them correctly, and only if the invariants
hold under pool contention.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture: per-test scratch table (TEMP-free — must survive
#  across pool acquires, which a session-TEMP table wouldn't)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _tx_table(pg_test_pool):
    """Plain table (not TEMP) — TEMP tables are per-connection and
    would evaporate when the pool returns the connection to the
    idle queue. Regular table + TRUNCATE teardown gives a clean
    slate per test while staying visible across borrows."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _test_tx_boundary ("
            "    id INTEGER PRIMARY KEY, "
            "    payload TEXT NOT NULL"
            ")"
        )
        await conn.execute("TRUNCATE _test_tx_boundary RESTART IDENTITY")
    try:
        yield pg_test_pool
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS _test_tx_boundary")


async def _count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM _test_tx_boundary")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 1: explicit rollback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_explicit_rollback_discards_all_writes(_tx_table):
    """Manually ``await tx.rollback()`` mid-block. Every write
    inside the tx must vanish; post-tx reads see no rows.

    Uses the explicit ``tx = conn.transaction(); await tx.start()``
    form because ``async with conn.transaction()`` auto-commits on
    clean exit and only auto-rollbacks on exception. Explicit
    control is the pattern used by rotation / upsert helpers that
    want to commit-or-rollback based on business logic."""
    pool = _tx_table
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(
            "INSERT INTO _test_tx_boundary (id, payload) VALUES "
            "(1, 'will-roll-back'), (2, 'also-rolls-back')"
        )
        # Row is visible within the tx.
        n_in_tx = await conn.fetchval(
            "SELECT COUNT(*) FROM _test_tx_boundary"
        )
        assert n_in_tx == 2
        await tx.rollback()
    assert await _count(pool) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 2: exception inside async with → auto-rollback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _TestRollbackTrigger(Exception):
    """Sentinel so we can distinguish an intentional rollback-
    triggering error from any surprise real failure."""


@pytest.mark.asyncio
async def test_exception_inside_tx_auto_rolls_back(_tx_table):
    """``async with conn.transaction():`` must commit on clean
    exit and roll back on exception. Raise mid-block; confirm
    the INSERT didn't persist."""
    pool = _tx_table
    with pytest.raises(_TestRollbackTrigger):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO _test_tx_boundary (id, payload) "
                    "VALUES (3, 'dies')"
                )
                raise _TestRollbackTrigger("intentional mid-tx raise")
    assert await _count(pool) == 0


@pytest.mark.asyncio
async def test_clean_async_with_commits(_tx_table):
    """The positive case of scenario 2: clean exit from ``async
    with conn.transaction():`` commits. Included so the rollback
    assertion above isn't trivially satisfied by a broken commit
    path."""
    pool = _tx_table
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO _test_tx_boundary (id, payload) "
                "VALUES (4, 'stays')"
            )
    assert await _count(pool) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 3: nested savepoint semantics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_nested_savepoint_rollback_keeps_outer_writes(_tx_table):
    """Nested ``conn.transaction()`` becomes a SAVEPOINT. If the
    inner rolls back but the outer commits, outer writes persist
    and inner writes are gone. This is the shape that makes the
    polymorphic-conn pattern safe — a caller-owned outer tx can
    share its conn with a helper that opens its own (inner)
    transaction."""
    pool = _tx_table
    async with pool.acquire() as conn:
        async with conn.transaction():  # outer
            await conn.execute(
                "INSERT INTO _test_tx_boundary (id, payload) "
                "VALUES (10, 'outer')"
            )
            with pytest.raises(_TestRollbackTrigger):
                async with conn.transaction():  # inner SAVEPOINT
                    await conn.execute(
                        "INSERT INTO _test_tx_boundary (id, payload) "
                        "VALUES (11, 'inner-dies')"
                    )
                    raise _TestRollbackTrigger("inner-only rollback")
            # Still inside outer; outer row visible, inner row gone.
            in_tx_ids = [
                r["id"] for r in await conn.fetch(
                    "SELECT id FROM _test_tx_boundary ORDER BY id"
                )
            ]
            assert in_tx_ids == [10]
    # After outer commit, the outer row is durable.
    async with pool.acquire() as conn:
        ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM _test_tx_boundary ORDER BY id"
            )
        ]
    assert ids == [10]


@pytest.mark.asyncio
async def test_outer_rollback_discards_nested_success(_tx_table):
    """Symmetric check — if the outer tx rolls back, a
    successfully-committed inner savepoint still goes away with
    it. Savepoints don't promote inner success past an outer
    rollback; they only give you fine-grained rollback within
    the outer."""
    pool = _tx_table
    async with pool.acquire() as conn:
        tx_outer = conn.transaction()
        await tx_outer.start()
        async with conn.transaction():  # inner savepoint, clean exit
            await conn.execute(
                "INSERT INTO _test_tx_boundary (id, payload) "
                "VALUES (20, 'inner-ok-but-outer-dies')"
            )
        await tx_outer.rollback()
    # Inner row is gone — outer rollback swept it away.
    assert await _count(_tx_table) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scenario 4: two concurrent tx on different conns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_concurrent_tx_on_different_conns_both_commit(_tx_table):
    """Two tasks each borrow a distinct pool connection and open
    their own transaction. Both can INSERT and commit without
    interfering with each other. This is the base invariant
    behind the pool concurrency work — per-connection tx
    isolation."""
    pool = _tx_table

    async def _writer(n: int, payload: str) -> None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO _test_tx_boundary (id, payload) "
                    "VALUES ($1, $2)",
                    n, payload,
                )

    await asyncio.gather(
        _writer(100, "from-task-a"),
        _writer(101, "from-task-b"),
    )
    assert await _count(pool) == 2


@pytest.mark.asyncio
async def test_concurrent_tx_one_rollback_doesnt_leak_to_other(_tx_table):
    """Two concurrent tx: one commits, the other rolls back. The
    committed row persists; the rolled-back row doesn't. Proves
    rollback is scoped to the borrower's own tx, not the whole
    pool state."""
    pool = _tx_table

    async def _commit_writer() -> None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO _test_tx_boundary (id, payload) "
                    "VALUES (200, 'commits')"
                )

    async def _rollback_writer() -> None:
        async with pool.acquire() as conn:
            with pytest.raises(_TestRollbackTrigger):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO _test_tx_boundary (id, payload) "
                        "VALUES (201, 'rolls-back')"
                    )
                    raise _TestRollbackTrigger("intentional")

    await asyncio.gather(_commit_writer(), _rollback_writer())

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, payload FROM _test_tx_boundary ORDER BY id"
        )
    assert len(rows) == 1
    assert rows[0]["id"] == 200
    assert rows[0]["payload"] == "commits"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adjacent: constraint violation rolls back the enclosing tx
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_unique_violation_raises_and_rolls_back(_tx_table):
    """PG's default behaviour: a UniqueViolation inside a tx
    aborts the tx — any subsequent statement on that conn raises
    ``InFailedSQLTransactionError`` until rollback. asyncpg maps
    this cleanly: the first violating INSERT raises
    ``UniqueViolationError``, the ``async with`` block catches
    it and rolls back."""
    pool = _tx_table
    # Seed one row.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO _test_tx_boundary (id, payload) "
            "VALUES (300, 'seed')"
        )

    # Try to insert a duplicate inside a tx.
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with pool.acquire() as conn:
            async with conn.transaction():
                # This second insert should commit fine on its
                # own, BUT because the third insert fails, the
                # whole tx rolls back — so even this row is gone.
                await conn.execute(
                    "INSERT INTO _test_tx_boundary (id, payload) "
                    "VALUES (301, 'would-have-stayed')"
                )
                await conn.execute(
                    "INSERT INTO _test_tx_boundary (id, payload) "
                    "VALUES (300, 'dup')"
                )

    # Only the seeded row survives; the "would-have-stayed" row
    # was rolled back because its tx also contained the dup.
    async with pool.acquire() as conn:
        ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM _test_tx_boundary ORDER BY id"
            )
        ]
    assert ids == [300]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adjacent: savepoint rollback lets outer tx keep going
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_savepoint_rollback_then_outer_continues(_tx_table):
    """Savepoint catches a constraint-violation so the outer tx
    can keep running + eventually commit. This is the useful
    side of savepoints — a helper that might conflict can be
    wrapped in its own tx block, fail, roll back just the
    savepoint, and the outer tx survives to do other work."""
    pool = _tx_table
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO _test_tx_boundary (id, payload) "
            "VALUES (400, 'seed')"
        )
        async with conn.transaction():  # outer
            await conn.execute(
                "INSERT INTO _test_tx_boundary (id, payload) "
                "VALUES (401, 'outer-pre')"
            )
            # Inner savepoint that will fail + roll back just
            # itself.
            try:
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO _test_tx_boundary (id, payload) "
                        "VALUES (400, 'dup-inside-sp')"
                    )
            except asyncpg.exceptions.UniqueViolationError:
                pass  # savepoint rolled back; outer still healthy
            # Outer tx still usable; run a final insert that
            # should land.
            await conn.execute(
                "INSERT INTO _test_tx_boundary (id, payload) "
                "VALUES (402, 'outer-post')"
            )
    async with pool.acquire() as conn:
        ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM _test_tx_boundary ORDER BY id"
            )
        ]
    # 400 seed + 401 outer-pre + 402 outer-post; the 400 dup
    # inside the savepoint got rolled back.
    assert ids == [400, 401, 402]

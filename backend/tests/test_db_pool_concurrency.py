"""Phase-3-Runtime-v2 SP-9.1 / task #82 — asyncpg pool concurrency guard.

50-task asyncio.gather stress test that proves the pool correctly
serialises connection acquisition under contention. The negative
assertion is the load-bearing one: **zero** ``another operation
is in progress`` errors — asyncpg raises that when two coroutines
touch the same ``Connection`` concurrently, which is exactly the
bug pattern that broke the Phase-3-Runtime v1 compat wrapper
(single shared connection + asyncio.Lock was prone to race under
heavy multi-router traffic).

Why 50 tasks? The test pool is ``max_size=5`` (see
``backend/tests/conftest.py::pg_test_pool``), so 50 tasks means
**10× oversubscription** — every slot is contested, the queue is
deep, and any bug in the pool's queue-fairness or
connection-borrow semantics surfaces within a second. The real
production pool is ``max_size=20`` per worker × 2 workers × 2
replicas = 80 connections; 50 tasks is well inside the per-worker
envelope.

Scope caveats:
  * These tests use ``pg_test_pool`` only — they don't touch the
    full lifespan machinery. For per-worker lifecycle coverage
    see ``test_db_pool_lifespan.py``.
  * ``SSLContext`` / TLS is not exercised — test PG runs plain
    TCP. Prod connects over TLS; any asyncpg TLS race would
    surface only in staging.
  * No actual user tables are populated. The pool is exercised
    via ``SELECT $1::int`` round-trips because the goal is to
    stress connection acquisition, not query planning.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg
import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core stress: 50 tasks × short SELECT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_50_concurrent_tasks_all_acquire_release_cleanly(pg_test_pool):
    """50 tasks each borrow a conn, run a trivial SELECT, release.
    Every task must succeed; no ``InterfaceError`` / ``another
    operation in progress`` / ``connection was closed`` surfaces.

    Load-bearing negative: if two coroutines ever share the same
    ``Connection`` object, asyncpg raises immediately — this test
    would catch a regression where the pool or a caller reuses a
    borrowed conn concurrently (a shape of bug the v1 compat
    wrapper actively had)."""
    async def _task(i: int) -> int:
        async with pg_test_pool.acquire() as conn:
            val = await conn.fetchval("SELECT $1::int", i)
        return val

    results = await asyncio.gather(
        *[_task(i) for i in range(50)],
        return_exceptions=True,
    )
    # All succeed; results are the round-tripped ints.
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"concurrent acquire raised: {errors[:3]}"
    assert sorted(results) == list(range(50))


@pytest.mark.asyncio
async def test_50_concurrent_short_writes_commit_all_rows(pg_test_pool):
    """50 tasks each insert one row with a distinct id, each inside
    its own pool-acquired conn. All rows must land — no drop, no
    double-insert, no deadlock. Exercises the short-lived-conn
    write path which is how most API handlers use the pool."""
    async with pg_test_pool.acquire() as conn:
        # Use a dedicated table the other suites don't touch so the
        # post-test row count is unambiguous.
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _test_pool_concurrency ("
            "    id INTEGER PRIMARY KEY, "
            "    payload TEXT NOT NULL"
            ")"
        )
        await conn.execute(
            "TRUNCATE _test_pool_concurrency RESTART IDENTITY"
        )

    async def _task(i: int) -> None:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO _test_pool_concurrency (id, payload) "
                "VALUES ($1, $2)",
                i, f"payload-{i}",
            )

    await asyncio.gather(*[_task(i) for i in range(50)])

    async with pg_test_pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM _test_pool_concurrency")
        distinct = await conn.fetchval(
            "SELECT COUNT(DISTINCT payload) FROM _test_pool_concurrency"
        )
        await conn.execute("DROP TABLE _test_pool_concurrency")

    assert n == 50
    assert distinct == 50


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Queue fairness under contention
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_contention_no_deadlock_under_held_connections(pg_test_pool):
    """With ``max_size=5`` and 10 tasks each holding a conn for
    ~100ms then releasing, the pool must serve every task within a
    reasonable wall-clock window. The test fails if the pool
    deadlocks (never wakes waiters) or if any task starves past
    the loose upper bound — either symptom points at a queue-
    fairness regression in the release path."""
    hold_s = 0.1
    n_tasks = 10
    max_pool = pg_test_pool.get_max_size()
    # Sanity: the test is only meaningful when tasks exceed slots.
    assert n_tasks > max_pool, (
        f"test precondition: n_tasks ({n_tasks}) must exceed "
        f"pool max ({max_pool}) to exercise queue fairness"
    )

    completions: list[float] = []

    async def _task() -> None:
        async with pg_test_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await asyncio.sleep(hold_s)
        completions.append(time.monotonic())

    t0 = time.monotonic()
    await asyncio.gather(*[_task() for _ in range(n_tasks)])
    elapsed = time.monotonic() - t0

    # n_tasks=10, max_pool=5, hold=100ms → best case 2 waves × 100ms
    # = 200ms. Allow 5× slack for CI jitter / test-PG latency.
    assert len(completions) == n_tasks
    assert elapsed < 5.0, (
        f"contention test took {elapsed:.2f}s — pool queue may be "
        f"deadlocking or starving waiters (expected ~0.2s × slack)"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Connection-level isolation between borrowers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_borrowers_never_share_a_connection(pg_test_pool):
    """Each ``async with pool.acquire() as conn`` hands out a
    Connection that MUST be exclusive to the caller for the
    duration of the with-block. This test runs 20 overlapping
    borrows, records each borrower's ``conn.get_server_pid()``
    while the borrow is live, and asserts no two ACTIVE borrowers
    ever see the same backend pid at the same instant.

    Two borrowers can legally see the same pid over time (the pool
    returns a connection to the idle queue after release, next
    caller reuses it) — the invariant is only about CONCURRENT
    overlap. We encode "active at the same instant" by capturing
    (acquire_time, release_time) windows and checking no two
    windows overlap for the same pid."""
    n = 20
    hold_s = 0.05

    # Each entry: (pid, acquired_at, released_at)
    observations: list[tuple[int, float, float]] = []
    lock = asyncio.Lock()

    async def _task() -> None:
        async with pg_test_pool.acquire() as conn:
            acquired = time.monotonic()
            pid = await conn.fetchval("SELECT pg_backend_pid()")
            await asyncio.sleep(hold_s)
            released = time.monotonic()
        async with lock:
            observations.append((pid, acquired, released))

    await asyncio.gather(*[_task() for _ in range(n)])

    # For each pair sharing the same pid, their hold windows must
    # NOT overlap. Overlap ⇒ two borrowers holding the same conn
    # concurrently ⇒ bug.
    by_pid: dict[int, list[tuple[float, float]]] = {}
    for pid, a, r in observations:
        by_pid.setdefault(pid, []).append((a, r))

    for pid, windows in by_pid.items():
        windows.sort()
        for (a1, r1), (a2, r2) in zip(windows, windows[1:]):
            # windows[1:] starts from the second; paired with
            # predecessor. Sorted by acquire time, so a2 >= a1.
            assert a2 >= r1 - 0.001, (  # 1ms slack for clock jitter
                f"pid {pid} acquired at {a2:.4f} before its prior "
                f"borrower released at {r1:.4f} — pool served the "
                f"same connection to two concurrent borrowers"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error-path: asyncpg raises inside borrowed conn
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_error_inside_acquire_still_releases_connection(pg_test_pool):
    """A task that raises inside the ``async with pool.acquire()``
    block must still release its conn to the pool — not leak it.
    The pool would eventually exhaust if borrowers leaked on
    error. We run 10 error-raising tasks then 10 success tasks and
    confirm all 20 finish without hitting ``command_timeout``."""
    errors_raised = 0

    async def _raising_task() -> None:
        async with pg_test_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            raise RuntimeError("intentional test failure")

    async def _success_task() -> int:
        async with pg_test_pool.acquire() as conn:
            return await conn.fetchval("SELECT 42")

    # First wave: 10 tasks all raise.
    results = await asyncio.gather(
        *[_raising_task() for _ in range(10)],
        return_exceptions=True,
    )
    errors_raised = sum(
        1 for r in results if isinstance(r, RuntimeError)
    )
    assert errors_raised == 10

    # Second wave: pool must still be functional. If the first
    # wave leaked connections, this gather hangs past
    # command_timeout (10s in the pg_test_pool fixture) and
    # asyncpg raises ``TimeoutError``. 10× successful acquire ⇒
    # no leak.
    second = await asyncio.gather(
        *[_success_task() for _ in range(10)],
        return_exceptions=True,
    )
    leaks = [r for r in second if isinstance(r, BaseException)]
    assert not leaks, f"pool leaked on error path: {leaks[:2]}"
    assert second == [42] * 10


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cancellation during acquire
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_cancelled_acquire_does_not_strand_slot(pg_test_pool):
    """A task that's cancelled while waiting for a pool slot must
    not strand the slot. Fill the pool, queue up extra waiters,
    cancel half of them, then confirm the remaining + fresh
    acquires all complete. Asserts the pool correctly notifies
    waiters on cancel rather than treating their slot as held."""
    max_pool = pg_test_pool.get_max_size()
    # Hold every slot with a sentinel task so all subsequent
    # acquires queue up.
    hold_event = asyncio.Event()

    async def _holder() -> None:
        async with pg_test_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await hold_event.wait()

    holders = [asyncio.create_task(_holder()) for _ in range(max_pool)]
    # Give the holders a tick to actually acquire their slots.
    await asyncio.sleep(0.05)

    # Queue up more tasks that will block on acquire.
    async def _waiter() -> int:
        async with pg_test_pool.acquire() as conn:
            return await conn.fetchval("SELECT 1")

    waiters = [asyncio.create_task(_waiter()) for _ in range(6)]
    # Give the waiters a tick to start waiting.
    await asyncio.sleep(0.05)

    # Cancel half of them.
    for w in waiters[:3]:
        w.cancel()

    # Release the holders; the 3 remaining waiters should complete
    # using their slots.
    hold_event.set()
    await asyncio.gather(*holders)

    # The 3 remaining waiters proceed; the 3 cancelled ones raise
    # CancelledError.
    results = await asyncio.gather(*waiters, return_exceptions=True)
    cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
    succeeded = sum(1 for r in results if r == 1)
    assert cancelled == 3
    assert succeeded == 3

    # Final sanity: a fresh acquire still works — the pool
    # accounting is consistent after the cancellations.
    async with pg_test_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 99") == 99


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session params actually apply on every borrow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_concurrent_tasks_each_see_clean_session_state(pg_test_pool):
    """Verify that two concurrent tasks don't see each other's
    session state leak through the connection. Task A sets a
    temporary session variable; task B on a *different* borrow
    must not see it. This is the asyncpg invariant that the
    pool's ``reset`` on release erases ``SET LOCAL`` state + any
    cursors / prepared statements.

    This test is probabilistic — it relies on both tasks
    overlapping, but with ``max_size=5`` and 2 tasks the overlap
    is essentially guaranteed."""
    async def _task_a() -> str | None:
        async with pg_test_pool.acquire() as conn:
            # ``SET LOCAL`` is tx-scoped, so wrap the SET + read
            # in an explicit transaction. The release-side reset
            # that asyncpg performs on the conn still has to
            # clean up even non-LOCAL session state on top.
            async with conn.transaction():
                await conn.execute(
                    "SET LOCAL application_name = 'task-a'"
                )
                name = await conn.fetchval(
                    "SELECT current_setting('application_name')"
                )
            await asyncio.sleep(0.02)
            return name

    async def _task_b() -> str | None:
        # Borrow a conn without setting application_name; on a
        # fresh or properly-reset conn the default (from the test
        # pool's ``init=None`` setup) should not be 'task-a'.
        await asyncio.sleep(0.01)  # start just after A's SET
        async with pg_test_pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT current_setting('application_name')"
            )

    a_name, b_name = await asyncio.gather(_task_a(), _task_b())
    # A sees its own SET LOCAL while inside the tx.
    assert a_name == "task-a"
    # B is on a different borrow (or the same conn after reset);
    # must not see A's setting. SET LOCAL is tx-scoped so it
    # vanishes on commit either way, and the pool's release-side
    # reset cleans up any non-LOCAL state. The negative assertion
    # catches a regression where either invariant breaks.
    assert b_name != "task-a"

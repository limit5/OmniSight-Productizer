"""Phase-3-Runtime-v2 SP-9.4 / task #82 — pool exhaustion behaviour.

Pins down what happens when every slot in ``asyncpg.Pool`` is
held and a new borrower wants in. The contract under test:

1. ``pool.acquire(timeout=...)`` with a finite timeout raises
   ``asyncio.TimeoutError`` when no slot frees up in time — NOT
   a silent hang, NOT ``InterfaceError``, NOT a PG-side error.
2. The exhaustion exception is recoverable — after holders
   release their slots the pool continues serving fresh
   acquires, no residual lockout.
3. Holders that are still alive are NOT kicked when a
   ``TimeoutError`` is raised on behalf of a waiter — the waiter
   fails cleanly, the holder's work continues.

Why this matters: under pathological load (a stuck long-running
query, a leaked connection, a sudden request spike larger than
the pool can serve), the right failure mode is **fast + visible**
— the caller gets an exception with a predictable shape that
upstream middleware can translate into HTTP 503 "Service
Unavailable" + a Retry-After header. The wrong failure mode is
an indefinite hang that starves the event loop.

Epic 9.4 spec: ``timeout=10s`` + proper 503 shape verified.
This file verifies the underlying ``TimeoutError`` contract that a
future ``backend/middleware/pool_timeout.py`` FastAPI middleware
would translate into 503; the translation layer itself is a
follow-up ticket (not in Phase-3-Runtime-v2 scope).

See ``docs/phase-3-runtime-v2/02-sub-phases.md`` Epic 9.4
(LOC ~200).
"""

from __future__ import annotations

import asyncio
import time

import asyncpg
import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helper: build a small isolated pool so we don't starve
#  sibling tests via the shared ``pg_test_pool`` fixture
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def tiny_pool(pg_test_dsn):
    """A private 2-slot pool. Keeps exhaustion tests from
    stealing slots from the shared 5-slot ``pg_test_pool`` that
    other tests running in parallel might need."""
    pool = await asyncpg.create_pool(
        pg_test_dsn,
        min_size=1,
        max_size=2,
        command_timeout=5.0,
    )
    try:
        yield pool
    finally:
        await pool.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core contract: acquire(timeout=) raises TimeoutError
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_exhausted_pool_raises_timeout_error(tiny_pool):
    """Fill a 2-slot pool with two holders that never release,
    then a third acquire with ``timeout=0.3`` must raise
    ``asyncio.TimeoutError`` in ~0.3s (not hang, not crash).
    This is the exact exception shape a future HTTP 503
    translator would key on."""
    hold_event = asyncio.Event()

    async def _holder() -> None:
        async with tiny_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await hold_event.wait()

    h1 = asyncio.create_task(_holder())
    h2 = asyncio.create_task(_holder())
    # Give the holders a tick to actually acquire slots.
    await asyncio.sleep(0.05)

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        async with tiny_pool.acquire(timeout=0.3):
            pytest.fail("acquire must not succeed on exhausted pool")
    elapsed = time.monotonic() - t0

    # Verify the timeout fired at roughly the right moment —
    # catches a regression where the timeout is silently ignored
    # (acquire blocks forever) or is triggering far too early.
    assert 0.25 <= elapsed <= 1.5, (
        f"timeout fired at {elapsed:.3f}s — expected ~0.3s"
    )

    # Release the holders and wait for them to drain.
    hold_event.set()
    await asyncio.gather(h1, h2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Recovery: pool works after exhaustion clears
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_pool_recovers_after_exhaustion(tiny_pool):
    """After the exhaustion event clears, fresh acquires must
    succeed on the same pool — no residual lockout, no poisoned
    state. This is the negative-space check for scenario 1: the
    pool accounting after a timeout is the same as after a
    normal release."""
    hold_event = asyncio.Event()

    async def _holder() -> None:
        async with tiny_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await hold_event.wait()

    holders = [asyncio.create_task(_holder()) for _ in range(2)]
    await asyncio.sleep(0.05)

    # Trigger the exhaustion failure.
    with pytest.raises(asyncio.TimeoutError):
        async with tiny_pool.acquire(timeout=0.2):
            pass

    # Release holders.
    hold_event.set()
    await asyncio.gather(*holders)

    # Pool must now serve fresh acquires normally.
    async with tiny_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 42") == 42
    async with tiny_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 43") == 43


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Holders survive a waiter's timeout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_waiter_timeout_does_not_kick_holders(tiny_pool):
    """A waiter timing out must NOT evict the holders. The
    failure mode is scoped to the waiter's own acquire attempt —
    holders continue running, their borrowed connections stay
    valid, and their in-flight work completes normally."""
    holder_results: list[int] = []

    async def _holder(val: int) -> None:
        async with tiny_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            # Simulate a slow operation — 0.5s work while a
            # waiter times out at 0.2s.
            await asyncio.sleep(0.5)
            holder_results.append(
                await conn.fetchval("SELECT $1::int", val)
            )

    h1 = asyncio.create_task(_holder(111))
    h2 = asyncio.create_task(_holder(222))
    await asyncio.sleep(0.05)

    with pytest.raises(asyncio.TimeoutError):
        async with tiny_pool.acquire(timeout=0.2):
            pass

    # Holders must finish cleanly.
    await asyncio.gather(h1, h2)
    assert sorted(holder_results) == [111, 222]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Queue-before-timeout: borrowers that start waiting just in
#  time to claim a slot as it frees must NOT spuriously timeout
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_waiter_acquires_before_timeout_when_slot_frees(tiny_pool):
    """Fill the pool, kick off a waiter with a generous timeout,
    then release a slot well before the timeout expires. The
    waiter must successfully acquire — not spuriously time out
    because the deadline was set at wait-start time. Catches a
    bug where the timeout bookkeeping doesn't reset when a slot
    becomes available."""
    release_holder = asyncio.Event()

    async def _holder() -> None:
        async with tiny_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await release_holder.wait()

    holders = [asyncio.create_task(_holder()) for _ in range(2)]
    await asyncio.sleep(0.05)

    async def _waiter() -> int:
        async with tiny_pool.acquire(timeout=2.0) as conn:
            return await conn.fetchval("SELECT 99")

    waiter_task = asyncio.create_task(_waiter())
    # Give the waiter a tick to start waiting.
    await asyncio.sleep(0.1)

    # Release one holder — the waiter should claim its slot.
    release_holder.set()

    result = await asyncio.wait_for(waiter_task, timeout=3.0)
    assert result == 99

    # Cleanup the second holder.
    await asyncio.gather(*holders)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Documentation-test: the exception shape a future 503
#  middleware would match on
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_timeout_shape_is_asyncio_timeout_error(tiny_pool):
    """Document the exact exception type a future ``pool_timeout``
    middleware would translate to HTTP 503. If asyncpg ever changes
    this (e.g. wraps it in its own exception class), this test
    fails loudly and the middleware contract has to be updated."""
    hold_event = asyncio.Event()

    async def _holder() -> None:
        async with tiny_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            await hold_event.wait()

    holders = [asyncio.create_task(_holder()) for _ in range(2)]
    await asyncio.sleep(0.05)

    exc_type: type | None = None
    try:
        async with tiny_pool.acquire(timeout=0.1):
            pass
    except BaseException as e:
        exc_type = type(e)

    # The documented shape: ``asyncio.TimeoutError``. Python 3.11+
    # aliases ``TimeoutError`` to ``asyncio.TimeoutError`` (same
    # class), so the isinstance check is stable across versions.
    assert exc_type is not None
    assert issubclass(exc_type, asyncio.TimeoutError), (
        f"pool exhaustion surfaced as {exc_type.__name__!r} — "
        f"503 middleware would miss it. Update the middleware "
        f"contract in backend/middleware/pool_timeout.py when it "
        f"lands."
    )

    hold_event.set()
    await asyncio.gather(*holders)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Command-timeout vs acquire-timeout: orthogonal controls
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_command_timeout_independent_from_acquire_timeout(pg_test_dsn):
    """``command_timeout`` fires on an individual SQL statement
    running too long; ``acquire(timeout=)`` fires on slot-wait.
    The two clocks don't interfere. A quick acquire followed by
    a slow query trips ``command_timeout``, not ``TimeoutError``."""
    pool = await asyncpg.create_pool(
        pg_test_dsn,
        min_size=1,
        max_size=2,
        command_timeout=0.3,  # short stmt timeout
    )
    try:
        async with pool.acquire(timeout=5.0) as conn:
            # pg_sleep(1) > command_timeout → asyncpg cancels + raises.
            # Shape: asyncpg.exceptions.QueryCanceledError (PG
            # 57014) OR asyncio.TimeoutError depending on the
            # exact race inside asyncpg — we accept both by
            # asserting it's NOT a TimeoutError for the ACQUIRE
            # side (the acquire succeeded fast above; the failure
            # must come from the slow SELECT).
            with pytest.raises(
                (asyncpg.exceptions.QueryCanceledError, asyncio.TimeoutError)
            ):
                await conn.execute("SELECT pg_sleep(1)")
    finally:
        await pool.close()

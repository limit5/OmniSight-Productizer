"""Phase-3-Runtime-v2 SP-9.2 / task #82 — multi-tenant isolation
under concurrent pool load.

Proves that two tenants (A, B) running in-flight operations side
by side through the same process-global asyncpg pool never see
each other's data — no row leak, no hash-chain contamination, no
session-state cross-talk. The substrate is ``audit_log`` because
it's the most load-bearing tenant-scoped surface (hash chain
verified per-tenant, ``tenant_where_pg`` filter on every read,
``pg_advisory_xact_lock(hashtext(tenant_id))`` serialising writes
within a tenant but NOT across tenants).

Attack vectors under test:

  * ``db_context.current_tenant_id`` lives in a ``ContextVar``
    scoped to the asyncio task. Two concurrent tasks each calling
    ``set_tenant_id(A)`` / ``set_tenant_id(B)`` must not leak
    between tasks — the ContextVar-per-task guarantee is what the
    whole tenant-context machinery rests on.
  * Pool connections are physically shared across tenants; the
    release-side reset must clear any per-tenant session state
    before the next borrower picks up the conn.
  * Writes inside the per-tenant advisory-lock scope must not
    serialise cross-tenant writes — if they did, concurrent
    tenants would block each other and the test would time out.
  * Queries using ``tenant_where_pg`` must never return rows from
    the non-current tenant, even when both tenants' rows live on
    the same physical PG pages.

Spec: 20 parallel workers, A/B parallel reads + writes, zero
cross-tenant leak. See ``docs/phase-3-runtime-v2/02-sub-phases.md``
Epic 9.2 (LOC ~300).
"""

from __future__ import annotations

import asyncio
import random

import pytest


TENANT_A = "t-iso-alpha"
TENANT_B = "t-iso-beta"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture: truncate + seed parent tenants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _two_tenant_db(pg_test_pool):
    """Clean ``audit_log`` slate + parent tenant rows for A/B.

    ``tenant_id`` on ``audit_log`` has an FK to ``tenants`` in
    some schemas (via CASCADE paths on users); seeding both
    parents up front removes FK noise from the isolation
    assertions."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE audit_log RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO tenants (id, name, plan) VALUES "
            "($1, $2, 'starter'), ($3, $4, 'starter') "
            "ON CONFLICT (id) DO NOTHING",
            TENANT_A, "Alpha Isolation", TENANT_B, "Beta Isolation",
        )
    from backend.db_context import set_tenant_id
    try:
        yield pg_test_pool
    finally:
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE audit_log RESTART IDENTITY CASCADE"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Core isolation: parallel writes + per-tenant reads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_parallel_writes_each_tenant_chain_intact(_two_tenant_db):
    """20 workers, 10 per tenant, each appending an audit row.
    After the storm: A's chain verifies + B's chain verifies,
    independently. A bug that leaked a row into the wrong tenant
    (or corrupted the chain by sharing a prev_hash across tenants)
    would break one of the two ``verify_chain`` calls."""
    from backend import audit
    from backend.db_context import set_tenant_id

    async def _writer(tenant: str, i: int) -> None:
        # Each worker sets its own tenant context in its own task
        # — ContextVar-per-task guarantees no leak.
        set_tenant_id(tenant)
        try:
            await audit.log(
                f"iso_write_{i}", "thing", f"{tenant}_id_{i}",
                before={"v": i}, after={"v": i + 1},
            )
        finally:
            set_tenant_id(None)

    jobs = []
    for i in range(10):
        jobs.append(_writer(TENANT_A, i))
        jobs.append(_writer(TENANT_B, i))
    # Shuffle so the asyncio scheduler doesn't run all-A-then-all-B.
    random.shuffle(jobs)
    await asyncio.gather(*jobs)

    ok_a, bad_a = await audit.verify_chain(tenant_id=TENANT_A)
    ok_b, bad_b = await audit.verify_chain(tenant_id=TENANT_B)
    assert ok_a and bad_a is None, f"A chain broken at {bad_a}"
    assert ok_b and bad_b is None, f"B chain broken at {bad_b}"


@pytest.mark.asyncio
async def test_query_never_returns_other_tenants_rows(_two_tenant_db):
    """After parallel writes from both tenants, each tenant's
    ``audit.query()`` must only return its own rows. Cross-tenant
    leak would fail the ``all rows match tenant_id`` assertion."""
    from backend import audit
    from backend.db_context import set_tenant_id

    async def _seed(tenant: str, i: int) -> None:
        set_tenant_id(tenant)
        try:
            await audit.log(
                f"iso_query_{tenant}_{i}", "thing", f"id_{i}",
            )
        finally:
            set_tenant_id(None)

    jobs = []
    for i in range(10):
        jobs.append(_seed(TENANT_A, i))
        jobs.append(_seed(TENANT_B, i))
    random.shuffle(jobs)
    await asyncio.gather(*jobs)

    # Query as tenant A.
    set_tenant_id(TENANT_A)
    rows_a = await audit.query(limit=100)
    set_tenant_id(TENANT_B)
    rows_b = await audit.query(limit=100)
    set_tenant_id(None)

    assert len(rows_a) == 10
    assert len(rows_b) == 10
    # Every row A sees must bear an action prefix that matches A.
    for r in rows_a:
        assert r["action"].startswith(f"iso_query_{TENANT_A}_"), (
            f"tenant A saw a row from elsewhere: {r['action']}"
        )
    for r in rows_b:
        assert r["action"].startswith(f"iso_query_{TENANT_B}_"), (
            f"tenant B saw a row from elsewhere: {r['action']}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Interleaved read-after-write
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_interleaved_read_after_write_isolated(_two_tenant_db):
    """Each worker does: set tenant → write → read-back → verify.
    Under 20-way concurrency the scheduler will interleave A and
    B tasks arbitrarily. The read-back must only return the
    worker's own just-written rows (plus any earlier rows from
    the same tenant), never the opposite tenant's data."""
    from backend import audit
    from backend.db_context import set_tenant_id

    async def _worker(tenant: str, i: int) -> tuple[str, list[str]]:
        set_tenant_id(tenant)
        try:
            await audit.log(
                f"rw_{tenant}_{i}", "thing", f"id_{i}",
                before={"w": i}, after={"w": i + 1},
            )
            # Yield so another worker can interleave.
            await asyncio.sleep(0.001)
            rows = await audit.query(limit=200)
            actions = [r["action"] for r in rows]
        finally:
            set_tenant_id(None)
        return tenant, actions

    jobs = []
    for i in range(10):
        jobs.append(_worker(TENANT_A, i))
        jobs.append(_worker(TENANT_B, i))
    random.shuffle(jobs)
    results = await asyncio.gather(*jobs)

    for tenant, actions in results:
        # Every action this worker read must start with ``rw_<its
        # own tenant>_`` — never ``rw_<other tenant>_``.
        other = TENANT_B if tenant == TENANT_A else TENANT_A
        leaks = [a for a in actions if a.startswith(f"rw_{other}_")]
        assert not leaks, (
            f"tenant {tenant} read rows from {other}: {leaks[:3]}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ContextVar per-task guarantee
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_context_var_per_task_no_leak_on_overlap():
    """Directly exercise the ContextVar contract that the tenant
    isolation invariant rests on: two tasks spawned from the same
    parent must each see their own ``set_tenant_id`` without
    leaking into the other. This test doesn't touch the DB — it's
    a pure Python asyncio-primitives guard against a regression
    where tenant context becomes process-global instead of
    task-scoped."""
    from backend.db_context import current_tenant_id, set_tenant_id
    set_tenant_id(None)

    barrier = asyncio.Event()
    saw: dict[str, str | None] = {}

    async def _task(tenant: str) -> None:
        set_tenant_id(tenant)
        # Wait at a barrier so both tasks overlap inside the
        # set-tenant-id scope.
        barrier.set()
        await asyncio.sleep(0.02)
        saw[tenant] = current_tenant_id()

    await asyncio.gather(
        _task("alpha-ctx"),
        _task("beta-ctx"),
    )
    assert saw["alpha-ctx"] == "alpha-ctx"
    assert saw["beta-ctx"] == "beta-ctx"
    # Parent task's context is unchanged — the ContextVar set
    # inside a child task should not propagate out.
    assert current_tenant_id() is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-tenant writes don't block each other
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_cross_tenant_writes_do_not_serialise(_two_tenant_db):
    """Per-tenant ``pg_advisory_xact_lock(hashtext(tenant_id))``
    must only serialise appends WITHIN a tenant. A long-running
    write on tenant A must NOT block a concurrent write on tenant
    B. This test spawns two slow writers (simulated with an
    explicit transaction holding the advisory lock) and asserts
    the second one doesn't wait for the first."""
    import time as _t
    from backend import audit
    from backend.db_context import set_tenant_id

    # Two concurrent writes on DIFFERENT tenants must complete in
    # ~parallel, not in series. Each writer takes ~hold_s seconds
    # because it grabs the lock and sleeps inside the tx.
    hold_s = 0.3

    async def _slow_writer(tenant: str) -> float:
        set_tenant_id(tenant)
        try:
            t0 = _t.monotonic()
            # audit.log internally wraps in a transaction + takes
            # the per-tenant advisory lock; inject the sleep via
            # a secondary tx on a fresh conn that ALSO takes the
            # same lock key, proving the second tenant's lock is
            # on a different key and proceeds.
            import asyncpg  # noqa: F401 (imports kept explicit)
            from backend.db_pool import get_pool
            async with get_pool().acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext($1))",
                        tenant,
                    )
                    await asyncio.sleep(hold_s)
                    # Write one audit row while still holding the
                    # lock — proves the lock is actually scoped
                    # and released on commit.
                    await audit.log(
                        f"cross_{tenant}", "thing", "x", conn=conn,
                    )
            return _t.monotonic() - t0
        finally:
            set_tenant_id(None)

    d_a, d_b = await asyncio.gather(
        _slow_writer(TENANT_A),
        _slow_writer(TENANT_B),
    )
    # If they serialised, total would be ~2 × hold_s ≈ 0.6s each.
    # Parallel execution should have each complete in ~hold_s.
    # Generous 2× slack for CI / PG latency.
    assert d_a < hold_s * 2, (
        f"tenant A write took {d_a:.2f}s — cross-tenant serialised?"
    )
    assert d_b < hold_s * 2, (
        f"tenant B write took {d_b:.2f}s — cross-tenant serialised?"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tampering one tenant doesn't affect the other
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_tamper_one_tenant_leaves_other_intact(_two_tenant_db, pg_test_pool):
    """Corrupt a row in tenant A's chain; tenant B's chain must
    still verify clean. Proves the chains are truly per-tenant
    and tamper-detection is scoped to the tampered-with tenant."""
    from backend import audit
    from backend.db_context import set_tenant_id

    # Seed 5 rows per tenant sequentially (no concurrency needed
    # — we're testing tamper isolation, not race).
    set_tenant_id(TENANT_A)
    for i in range(5):
        await audit.log(f"tamper_A_{i}", "thing", f"id_{i}",
                        after={"v": i})
    set_tenant_id(TENANT_B)
    for i in range(5):
        await audit.log(f"tamper_B_{i}", "thing", f"id_{i}",
                        after={"v": i})
    set_tenant_id(None)

    # Corrupt the middle row of tenant A.
    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY id ASC OFFSET 2 LIMIT 1",
            TENANT_A,
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"v\":999}' "
            "WHERE id = $1",
            tampered_id,
        )

    ok_a, bad_a = await audit.verify_chain(tenant_id=TENANT_A)
    ok_b, bad_b = await audit.verify_chain(tenant_id=TENANT_B)
    assert not ok_a
    assert bad_a == tampered_id
    # Tenant B is untouched.
    assert ok_b and bad_b is None

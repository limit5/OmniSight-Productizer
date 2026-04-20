"""Phase 53 / I8 tests — audit chain integrity + per-tenant chain isolation.

Phase-3-Runtime-v2 SP-4.1 (2026-04-20): migrated from SQLite tempfile
fixture to ``pg_test_pool``. audit.py is now asyncpg-native with
``pg_advisory_xact_lock`` per-tenant for concurrent-append safety;
tests exercise that via the pool.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def _audit_db(pg_test_pool, monkeypatch):
    # Clean slate per test — audit_log is NOT savepoint-isolated
    # because audit.log commits via its own pool-scoped transaction.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE audit_log RESTART IDENTITY CASCADE"
        )
    from backend import audit
    try:
        yield audit
    finally:
        from backend.db_context import set_tenant_id
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE audit_log RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
async def test_log_appends_row(_audit_db):
    audit = _audit_db
    rid = await audit.log("mode_change", "operation_mode", "global",
                          before={"mode": "supervised"},
                          after={"mode": "full_auto"})
    assert isinstance(rid, int) and rid > 0
    rows = await audit.query(limit=10)
    assert len(rows) == 1
    assert rows[0]["action"] == "mode_change"
    assert rows[0]["after"] == {"mode": "full_auto"}


@pytest.mark.asyncio
async def test_chain_intact_after_many_writes(_audit_db):
    audit = _audit_db
    for i in range(20):
        await audit.log(f"act_{i % 3}", "thing", f"id_{i}",
                        before={"v": i}, after={"v": i + 1})
    ok, bad = await audit.verify_chain()
    assert ok and bad is None


@pytest.mark.asyncio
async def test_chain_detects_tampering(_audit_db):
    audit = _audit_db
    from backend.db_pool import get_pool
    for i in range(5):
        await audit.log("set_strategy", "budget_strategy", "global",
                        before={"s": "balanced"}, after={"s": "sprint"})
    # Find the 3rd row's actual id (autoincrement may not start at 1
    # on a shared PG; use offset 2 for the third insert).
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log ORDER BY id ASC OFFSET 2 LIMIT 1"
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"s\":\"FORGED\"}' "
            "WHERE id = $1",
            tampered_id,
        )
    ok, bad = await audit.verify_chain()
    assert not ok
    assert bad == tampered_id, f"first bad should be the tampered row, got {bad}"


@pytest.mark.asyncio
async def test_query_filters(_audit_db):
    audit = _audit_db
    await audit.log("a", "decision", "d1", actor="user")
    await audit.log("b", "operation_mode", "global", actor="system")
    await audit.log("a", "decision", "d2", actor="user")

    by_actor = await audit.query(actor="user")
    assert len(by_actor) == 2
    by_kind = await audit.query(entity_kind="operation_mode")
    assert len(by_kind) == 1
    assert by_kind[0]["entity_id"] == "global"


@pytest.mark.asyncio
async def test_query_session_id_filter(_audit_db):
    audit = _audit_db
    await audit.log("a", "decision", "d1", actor="user", session_id="sess-aaa")
    await audit.log("b", "operation_mode", "global", actor="user", session_id="sess-bbb")
    await audit.log("c", "decision", "d2", actor="user", session_id="sess-aaa")

    by_sess = await audit.query(session_id="sess-aaa")
    assert len(by_sess) == 2
    assert all(r["session_id"] == "sess-aaa" for r in by_sess)

    by_sess_b = await audit.query(session_id="sess-bbb")
    assert len(by_sess_b) == 1
    assert by_sess_b[0]["action"] == "b"

    no_match = await audit.query(session_id="sess-zzz")
    assert len(no_match) == 0


@pytest.mark.asyncio
async def test_log_failure_does_not_raise(_audit_db, monkeypatch):
    # SP-4.1: confirm the outer try/except in audit.log still swallows
    # errors + returns None rather than bubbling them to the caller.
    # Simulate failure by monkeypatching get_pool to raise — avoids
    # the "close the shared module pool and break the fixture"
    # anti-pattern the original SQLite test used.
    audit = _audit_db

    def _broken_pool(*a, **kw):
        raise RuntimeError("simulated pool-unavailable")

    monkeypatch.setattr("backend.db_pool.get_pool", _broken_pool)
    rid = await audit.log("a", "x", None)
    assert rid is None


# ─── I8: Per-tenant hash chain tests ───


async def _create_test_tenants(*tids):
    """Insert test tenant rows so FK constraints pass."""
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        for tid in tids:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free') "
                "ON CONFLICT (id) DO NOTHING",
                tid, f"Test {tid}",
            )


@pytest.mark.asyncio
async def test_per_tenant_independent_chains(_audit_db):
    """Each tenant has its own genesis (empty prev_hash) and independent chain."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(5):
        await audit.log(f"alpha_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(3):
        await audit.log(f"beta_{i}", "thing", f"b{i}")

    ok_a, bad_a = await audit.verify_chain(tenant_id="t-alpha")
    assert ok_a and bad_a is None

    ok_b, bad_b = await audit.verify_chain(tenant_id="t-beta")
    assert ok_b and bad_b is None


@pytest.mark.asyncio
async def test_per_tenant_genesis_starts_empty(_audit_db):
    """First row of each tenant's chain should have empty prev_hash."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-one", "t-two")

    set_tenant_id("t-one")
    await audit.log("first", "thing", "x1")

    set_tenant_id("t-two")
    await audit.log("first", "thing", "x2")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT tenant_id, prev_hash FROM audit_log ORDER BY id ASC"
        )
    assert len(rows) == 2
    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == ""


@pytest.mark.asyncio
async def test_tampering_one_tenant_does_not_affect_other(_audit_db):
    """Tampering in tenant A should not break tenant B's chain."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(3):
        await audit.log(f"a_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(3):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY id ASC LIMIT 1 OFFSET 1",
            "t-alpha",
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            tampered_id,
        )

    ok_a, bad_a = await audit.verify_chain(tenant_id="t-alpha")
    assert not ok_a
    assert bad_a == tampered_id

    ok_b, bad_b = await audit.verify_chain(tenant_id="t-beta")
    assert ok_b and bad_b is None


@pytest.mark.asyncio
async def test_verify_all_chains(_audit_db):
    """verify_all_chains returns per-tenant results."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    for i in range(3):
        await audit.log(f"a_{i}", "thing", f"a{i}")

    set_tenant_id("t-beta")
    for i in range(2):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    results = await audit.verify_all_chains()
    assert "t-alpha" in results
    assert "t-beta" in results
    assert results["t-alpha"] == (True, None)
    assert results["t-beta"] == (True, None)


@pytest.mark.asyncio
async def test_verify_all_chains_detects_partial_tampering(_audit_db):
    """verify_all_chains should detect tampering in one tenant while others pass."""
    audit = _audit_db
    from backend import db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-good", "t-bad")

    set_tenant_id("t-good")
    for i in range(3):
        await audit.log(f"g_{i}", "thing", f"g{i}")

    set_tenant_id("t-bad")
    for i in range(3):
        await audit.log(f"b_{i}", "thing", f"b{i}")

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE tenant_id = $1 "
            "ORDER BY id ASC LIMIT 1 OFFSET 1",
            "t-bad",
        )
        tampered_id = row["id"]
        await conn.execute(
            "UPDATE audit_log SET after_json = '{\"forged\":true}' "
            "WHERE id = $1",
            tampered_id,
        )

    results = await audit.verify_all_chains()
    assert results["t-good"] == (True, None)
    ok_bad, bad_id = results["t-bad"]
    assert not ok_bad
    assert bad_id == tampered_id


@pytest.mark.asyncio
async def test_cross_tenant_query_isolation(_audit_db):
    """Queries with tenant context only return that tenant's rows."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-alpha", "t-beta")

    set_tenant_id("t-alpha")
    await audit.log("a1", "thing", "x1")
    await audit.log("a2", "thing", "x2")

    set_tenant_id("t-beta")
    await audit.log("b1", "thing", "y1")

    set_tenant_id("t-alpha")
    rows_alpha = await audit.query()
    assert len(rows_alpha) == 2
    assert all(r["action"].startswith("a") for r in rows_alpha)

    set_tenant_id("t-beta")
    rows_beta = await audit.query()
    assert len(rows_beta) == 1
    assert rows_beta[0]["action"] == "b1"


@pytest.mark.asyncio
async def test_interleaved_writes_maintain_separate_chains(_audit_db):
    """Alternating writes between tenants should maintain correct chains."""
    audit = _audit_db
    from backend.db_context import set_tenant_id
    await _create_test_tenants("t-even", "t-odd")

    for i in range(6):
        tid = "t-even" if i % 2 == 0 else "t-odd"
        set_tenant_id(tid)
        await audit.log(f"act_{i}", "thing", f"id_{i}")

    ok_even, _ = await audit.verify_chain(tenant_id="t-even")
    ok_odd, _ = await audit.verify_chain(tenant_id="t-odd")
    assert ok_even
    assert ok_odd


# ─── SP-4.1 concurrent-append contract (load-bearing) ──────────────


@pytest.mark.asyncio
async def test_concurrent_appends_preserve_chain(_audit_db):
    """LOAD-BEARING: multiple simultaneous audit.log() calls on the
    same tenant must NOT create chain forks.

    Without pg_advisory_xact_lock, two tasks running on different pool
    connections can both read the same prev_hash (the SELECT for the
    previous row's curr_hash isn't under SELECT FOR UPDATE on the
    tenant's tail), compute the same curr_hash, and INSERT two rows
    with identical prev_hash + curr_hash → chain forks and verify_chain
    fails from the second row onward.

    SP-4.1 adds ``SELECT pg_advisory_xact_lock(hashtext('audit-chain-'||tenant))``
    as the first statement inside each append's transaction. PG
    serializes writers on the lock key; different tenants hold
    different keys and still append in parallel. Regression guard:
    if that advisory lock is dropped or the key becomes wrong, this
    test fails within a few runs.
    """
    import asyncio
    audit = _audit_db
    await _create_test_tenants("t-concurrent")
    from backend.db_context import set_tenant_id
    set_tenant_id("t-concurrent")

    async def _one(i: int) -> None:
        await audit.log(f"concurrent_{i}", "thing", f"id_{i}",
                        before={"v": i}, after={"v": i + 1})

    # Fan out 20 concurrent appends — with the advisory lock they
    # serialise at the DB level; without it, the race window is
    # large enough that several will collide.
    await asyncio.gather(*(_one(i) for i in range(20)))

    ok, bad = await audit.verify_chain(tenant_id="t-concurrent")
    assert ok, (
        f"Chain broke at row {bad} under concurrent appends — advisory "
        f"lock missing or keyed incorrectly?"
    )

    # 20 rows total, chain intact, no forks.
    rows = await audit.query(limit=100)
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_concurrent_appends_different_tenants_dont_block(
    _audit_db,
):
    """Different tenants' advisory locks use different keys → their
    appends can proceed in parallel (regression guard against using
    a single global lock key).
    """
    import asyncio
    audit = _audit_db
    await _create_test_tenants("t-par-A", "t-par-B")
    from backend.db_context import set_tenant_id

    async def _append(tid: str, n: int) -> None:
        set_tenant_id(tid)
        for i in range(n):
            await audit.log(f"{tid}_{i}", "thing", f"id_{i}")

    await asyncio.gather(
        _append("t-par-A", 10),
        _append("t-par-B", 10),
    )

    ok_a, _ = await audit.verify_chain(tenant_id="t-par-A")
    ok_b, _ = await audit.verify_chain(tenant_id="t-par-B")
    assert ok_a
    assert ok_b

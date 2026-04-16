"""Phase 53 / I8 tests — audit chain integrity + per-tenant chain isolation."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def _audit_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "audit.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as _cfg
        _cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        await db._conn().execute("DELETE FROM audit_log")
        await db._conn().commit()
        from backend import audit
        try:
            yield audit
        finally:
            from backend.db_context import set_tenant_id
            set_tenant_id(None)
            await db.close()


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
    from backend import db
    for i in range(5):
        await audit.log("set_strategy", "budget_strategy", "global",
                        before={"s": "balanced"}, after={"s": "sprint"})
    await db._conn().execute(
        "UPDATE audit_log SET after_json='{\"s\":\"FORGED\"}' WHERE id=3"
    )
    await db._conn().commit()
    ok, bad = await audit.verify_chain()
    assert not ok
    assert bad == 3, f"first bad should be the tampered row, got {bad}"


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
    audit = _audit_db
    from backend import db
    await db.close()
    rid = await audit.log("a", "x", None)
    assert rid is None
    db._DB_PATH = db._resolve_db_path()
    await db.init()


# ─── I8: Per-tenant hash chain tests ───


async def _create_test_tenants(*tids):
    """Insert test tenant rows so FK constraints pass."""
    from backend import db
    conn = db._conn()
    for tid in tids:
        await conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name, plan) VALUES (?, ?, 'free')",
            (tid, f"Test {tid}"),
        )
    await conn.commit()


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

    conn = db._conn()
    async with conn.execute(
        "SELECT tenant_id, prev_hash FROM audit_log ORDER BY id ASC"
    ) as cur:
        rows = await cur.fetchall()
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

    conn = db._conn()
    async with conn.execute(
        "SELECT id FROM audit_log WHERE tenant_id = 't-alpha' ORDER BY id ASC LIMIT 1 OFFSET 1"
    ) as cur:
        row = await cur.fetchone()
    tampered_id = row["id"]
    await conn.execute(
        f"UPDATE audit_log SET after_json='{{\"forged\":true}}' WHERE id={tampered_id}"
    )
    await conn.commit()

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

    conn = db._conn()
    async with conn.execute(
        "SELECT id FROM audit_log WHERE tenant_id = 't-bad' ORDER BY id ASC LIMIT 1 OFFSET 1"
    ) as cur:
        row = await cur.fetchone()
    await conn.execute(
        f"UPDATE audit_log SET after_json='{{\"forged\":true}}' WHERE id={row['id']}"
    )
    await conn.commit()

    results = await audit.verify_all_chains()
    assert results["t-good"] == (True, None)
    ok_bad, bad_id = results["t-bad"]
    assert not ok_bad
    assert bad_id == row["id"]


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

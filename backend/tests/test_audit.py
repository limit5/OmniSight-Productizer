"""Phase 53 tests — audit chain integrity + query."""

from __future__ import annotations

import asyncio
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
        # Clear any rows from a fresh schema
        await db._conn().execute("DELETE FROM audit_log")
        await db._conn().commit()
        from backend import audit
        try:
            yield audit
        finally:
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
    # Tamper with row 3's after_json without recomputing curr_hash
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
async def test_log_failure_does_not_raise(_audit_db, monkeypatch):
    audit = _audit_db
    # Force the write path to blow up by closing the connection
    from backend import db
    await db.close()
    rid = await audit.log("a", "x", None)
    # log() must absorb the failure — returns None instead of raising
    assert rid is None
    # restore for fixture teardown
    db._DB_PATH = db._resolve_db_path()
    await db.init()

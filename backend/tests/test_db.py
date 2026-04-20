"""Fix-D D1 — per-table CRUD smoke coverage for backend.db.

Goal is not line coverage — it's *contract* coverage: every table has at
least one round-trip (write + read) and one mutation (update/delete).
If a migration silently drops a column, or a JSON field stops getting
encoded, these tests fail fast.

Fixture strategy: one fresh on-disk SQLite DB per test. aiosqlite does
not do true in-memory shared connections cleanly, and the `init()` path
runs migrations we actually want to exercise. Cost is ~50ms per test;
the whole file finishes in <5s.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest


@pytest.fixture()
async def fresh_db(monkeypatch):
    """Fresh sqlite file, initialised schema + migrations applied."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as cfg
        cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        try:
            yield db
        finally:
            await db.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Agents  —  MOVED TO test_db_agents.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.1 (2026-04-20): the 5 agent functions
# (list_agents / get_agent / upsert_agent / delete_agent / agent_count)
# were ported from compat-wrapper SQLite-compatible signatures to
# native asyncpg with an explicit ``conn: asyncpg.Connection`` first
# argument. The SQLite-backed ``fresh_db`` fixture in this file can no
# longer exercise them — they require a pool-borrowed connection.
#
# The per-function contract tests live in ``test_db_agents.py``, which
# uses the ``pg_test_conn`` fixture from conftest.py (skips cleanly
# when OMNI_TEST_PG_URL is unset).


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tasks + comments  —  MOVED TO test_db_tasks.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.2 (2026-04-20): the 7 tasks functions
# (list_tasks / get_task / upsert_task / delete_task / task_count /
# insert_task_comment / list_task_comments) were ported from
# compat-wrapper SQLite-compatible signatures to native asyncpg with an
# explicit ``conn: asyncpg.Connection`` first argument. The SQLite
# ``fresh_db`` fixture in this file can no longer exercise them — they
# require a pool-borrowed connection.
#
# The per-function contract tests live in ``test_db_tasks.py``, which
# uses the ``pg_test_conn`` fixture from conftest.py (skips cleanly
# when OMNI_TEST_PG_URL is unset).


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token usage
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_token_usage_upsert_list(fresh_db):
    db = fresh_db
    await db.upsert_token_usage({
        "model": "claude-opus-4-6", "input_tokens": 100, "output_tokens": 50,
        "total_tokens": 150, "cost": 0.012, "request_count": 1,
        "avg_latency": 1.2, "last_used": "2026-04-14T00:00:00",
    })
    rows = await db.list_token_usage()
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 150
    # Upsert same model
    await db.upsert_token_usage({
        "model": "claude-opus-4-6", "input_tokens": 200, "output_tokens": 100,
        "total_tokens": 300, "cost": 0.024, "request_count": 2,
        "avg_latency": 1.1, "last_used": "2026-04-14T00:00:01",
    })
    rows = await db.list_token_usage()
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 300


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handoffs  —  MOVED TO test_db_handoffs.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.3 (2026-04-20): the 3 handoff functions
# (upsert_handoff / get_handoff / list_handoffs) were ported from
# compat-wrapper SQLite-compatible signatures to native asyncpg with
# an explicit ``conn: asyncpg.Connection`` first argument. The SQLite
# ``fresh_db`` fixture in this file can no longer exercise them —
# they require a pool-borrowed connection.
#
# The per-function contract tests live in ``test_db_handoffs.py``,
# which uses the ``pg_test_conn`` fixture from conftest.py (skips
# cleanly when OMNI_TEST_PG_URL is unset).


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Notifications  —  MOVED TO test_db_notifications.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.4 (2026-04-20): the 6 notification functions
# (insert_notification / list_notifications / mark_notification_read /
# count_unread_notifications / update_notification_dispatch /
# list_failed_notifications) were ported from compat-wrapper
# SQLite-compatible signatures to native asyncpg with an explicit
# ``conn: asyncpg.Connection`` first argument. The SQLite
# ``fresh_db`` fixture in this file can no longer exercise them.
#
# The per-function contract tests live in
# ``test_db_notifications.py`` (pg_test_conn-backed).


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Artifacts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_artifacts_insert_filter_delete(fresh_db):
    db = fresh_db
    for i in range(3):
        await db.insert_artifact({
            "id": f"art{i}", "task_id": "t1" if i < 2 else "t2",
            "agent_id": "a1", "name": f"file{i}.bin", "type": "firmware",
            "file_path": f"/tmp/file{i}.bin", "size": 100 * i,
            "created_at": f"2026-04-14T00:00:0{i}",
        })
    assert len(await db.list_artifacts()) == 3
    assert len(await db.list_artifacts(task_id="t1")) == 2
    assert len(await db.list_artifacts(agent_id="a1")) == 3
    assert (await db.get_artifact("art0"))["name"] == "file0.bin"
    assert await db.delete_artifact("art0") is True
    assert await db.get_artifact("art0") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPI state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_npi_state_roundtrip(fresh_db):
    db = fresh_db
    assert await db.get_npi_state() == {}
    await db.save_npi_state({"phase": "MVP", "progress": 0.42})
    assert (await db.get_npi_state())["phase"] == "MVP"
    await db.save_npi_state({"phase": "GA"})  # overwrite
    assert (await db.get_npi_state())["phase"] == "GA"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulations
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_simulation_insert_update_filter(fresh_db):
    db = fresh_db
    await db.insert_simulation({
        "id": "sim1", "task_id": "t1", "agent_id": "a1",
        "track": "algo", "module": "isp", "status": "running",
        "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
        "coverage_pct": 0.0, "valgrind_errors": 0, "duration_ms": 0,
        "report_json": "{}", "artifact_id": None, "created_at": "2026-04-14T00:00:00",
    })
    sim = await db.get_simulation("sim1")
    assert sim and sim["status"] == "running"
    # update — only whitelisted columns are written
    await db.update_simulation("sim1", {
        "status": "passed", "tests_passed": 10, "tests_failed": 0,
        "bogus_column": "ignored",
    })
    sim = await db.get_simulation("sim1")
    assert sim["status"] == "passed"
    assert sim["tests_passed"] == 10
    # filter
    assert len(await db.list_simulations(task_id="t1")) == 1
    assert len(await db.list_simulations(status="failed")) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Debug findings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_debug_finding_insert_update(fresh_db):
    db = fresh_db
    await db.insert_debug_finding({
        "id": "f1", "task_id": "t1", "agent_id": "a1",
        "finding_type": "error", "severity": "high",
        "content": "null deref", "context": "{}",
        "status": "open", "created_at": "2026-04-14T00:00:00",
    })
    rows = await db.list_debug_findings(status="open")
    assert len(rows) == 1
    assert await db.update_debug_finding("f1", "resolved") is True
    assert len(await db.list_debug_findings(status="open")) == 0
    assert len(await db.list_debug_findings(status="resolved")) == 1
    # INSERT OR IGNORE — duplicate id is no-op
    await db.insert_debug_finding({
        "id": "f1", "task_id": "t1", "agent_id": "a1",
        "finding_type": "error", "severity": "low",
        "content": "dup", "context": "{}", "status": "open",
        "created_at": "2026-04-14T00:00:01",
    })
    assert len(await db.list_debug_findings()) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event log
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_event_log_insert_list_cleanup(fresh_db):
    db = fresh_db
    await db.insert_event("agent_update", json.dumps({"id": "a1"}))
    await db.insert_event("task_update", json.dumps({"id": "t1"}))
    all_ev = await db.list_events()
    assert len(all_ev) == 2
    only_agent = await db.list_events(event_types=["agent_update"])
    assert len(only_agent) == 1
    # cleanup with 0 days → deletes nothing that was just inserted
    # (datetime('now', '-0 days') equals now; strict < comparison)
    deleted = await db.cleanup_old_events(days=0)
    assert deleted == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Episodic memory (L3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_episodic_memory_insert_get_delete(fresh_db):
    db = fresh_db
    await db.insert_episodic_memory({
        "id": "mem1", "error_signature": "segfault in isp_init",
        "solution": "init NPU before ISP", "soc_vendor": "rockchip",
        "sdk_version": "1.2.3", "hardware_rev": "A1",
        "source_task_id": "t1", "source_agent_id": "a1",
        "gerrit_change_id": "I0001", "tags": "npu,isp",
        "quality_score": 0.9,
    })
    got = await db.get_episodic_memory("mem1")
    assert got is not None
    assert got["error_signature"] == "segfault in isp_init"
    rows = await db.list_episodic_memories()
    assert len(rows) == 1
    assert await db.delete_episodic_memory("mem1") is True
    assert await db.get_episodic_memory("mem1") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision rules (Phase 50B)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_decision_rules_replace_load(fresh_db):
    db = fresh_db
    assert await db.load_decision_rules() == []
    await db.replace_decision_rules([
        {
            "id": "r1", "kind_pattern": "git_push/*", "severity": "destructive",
            "auto_in_modes": ["full_auto"], "default_option_id": "abort",
            "priority": 10, "enabled": True, "note": "prod safety",
        },
        {
            "id": "r2", "kind_pattern": "stuck/*", "severity": "risky",
            "auto_in_modes": ["supervised", "full_auto"],
            "default_option_id": "switch_model",
            "priority": 100, "enabled": False, "note": "",
        },
    ])
    rules = await db.load_decision_rules()
    assert len(rules) == 2
    ids = {r["id"] for r in rules}
    assert ids == {"r1", "r2"}
    r1 = next(r for r in rules if r["id"] == "r1")
    assert r1["auto_in_modes"] == ["full_auto"]  # JSON round-trip
    assert r1["enabled"] is True
    # Replace atomically — old rules gone
    await db.replace_decision_rules([
        {
            "id": "r3", "kind_pattern": "deploy/*", "severity": "destructive",
            "auto_in_modes": [], "default_option_id": "abort",
            "priority": 5, "enabled": True, "note": "",
        },
    ])
    rules = await db.load_decision_rules()
    assert len(rules) == 1 and rules[0]["id"] == "r3"

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
#  Token usage  —  MOVED TO test_db_token_usage.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.5 (2026-04-20): the 3 token_usage functions
# (list_token_usage / upsert_token_usage / clear_token_usage) were
# ported to native asyncpg with an explicit ``conn: asyncpg.Connection``
# first argument. SQLite ``fresh_db`` can no longer exercise them.
#
# Per-function contract tests live in ``test_db_token_usage.py`` using
# the ``pg_test_conn`` fixture (savepoint + TRUNCATE isolation).


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
#  Artifacts  —  MOVED TO test_db_artifacts.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.6a (2026-04-20): the 4 artifact functions
# (insert_artifact / list_artifacts / get_artifact / delete_artifact)
# were ported to native asyncpg with an explicit
# ``conn: asyncpg.Connection`` first argument. The SQLite ``fresh_db``
# fixture can no longer exercise them.
#
# Per-function contract tests (including tenant-isolation guards that
# preserve the RLS coverage previously provided by tests/test_rls.py)
# live in ``test_db_artifacts.py`` using pg_test_conn. Five ancillary
# test files (test_artifacts.py, test_artifact_pipeline.py,
# test_release.py, test_npi.py, tests/test_rls.py) remain on the old
# signature and are SKIPPED with SP-3.6b markers; SP-3.6b migrates
# them to pg_test_conn in a follow-up commit.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NPI state  —  MOVED TO test_db_npi_state.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.7 (2026-04-20): get_npi_state /
# save_npi_state now require an asyncpg.Connection; SQLite fresh_db
# can't exercise them. Per-function tests live in
# ``test_db_npi_state.py`` with pg_test_conn.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Simulations  —  MOVED TO test_db_simulations.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.8 (2026-04-20): the 4 simulation functions
# (insert_simulation / get_simulation / list_simulations /
# update_simulation) were ported to native asyncpg with an explicit
# ``conn: asyncpg.Connection`` first argument. update_simulation's
# column whitelist (_SIMULATION_COLUMNS) is preserved; dynamic SET
# clause now uses positional ``$N`` placeholders derived from the
# whitelisted dict keys.
#
# Per-function contract tests live in ``test_db_simulations.py``.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Debug findings  —  MOVED TO test_db_debug_findings.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.9 (2026-04-20): the 3 debug-finding
# functions now require an explicit asyncpg.Connection. PG's
# ``ON CONFLICT (id) DO NOTHING`` replaces SQLite's ``INSERT OR IGNORE``;
# tenant filtering is via the promoted ``tenant_where_pg`` helper in
# db_context.py. Per-function contract tests (including tenant
# isolation for the update path) live in
# ``test_db_debug_findings.py``.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event log  —  MOVED TO test_db_events.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.10 (2026-04-20): insert_event / list_events
# / cleanup_old_events ported to native asyncpg. The
# ``datetime('now', '-N days')`` cutoff is replaced with PG's
# ``NOW() - INTERVAL '1 day' * $N`` — fixing the second-boundary
# flakiness the old ``days=0`` test exposed in large batches (flagged
# as pre-existing fragility in SP-3.5 commit 9f25a702).
#
# The replacement contract test (backend/tests/test_db_events.py)
# intentionally avoids the racy ``days=0`` assertion — the deterministic
# boundaries are: ``days=365`` (nothing old enough; 0 deletes) and
# ``days=-1`` (future cutoff; everything deleted).
#
# cleanup_old_events also gained a tenant_id WHERE clause in SP-3.10
# — the pre-port version deleted GLOBALLY across all tenants, which
# was a multi-tenant data-integrity bug. Covered explicitly by
# TestEventsCleanup::test_cleanup_scoped_to_current_tenant.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Episodic memory (L3)  —  MOVED TO test_db_episodic_memory.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.12 (2026-04-20): all 7 episodic_memory
# functions (insert / search / get / list / delete / count /
# rebuild_fts) ported to native asyncpg. The SQLite FTS5 virtual
# table sync logic is gone — alembic 0017 (SP-2.1) added a
# ``tsv tsvector GENERATED ALWAYS AS (...) STORED`` column which
# PG maintains automatically. Search uses
# ``tsv @@ plainto_tsquery('english', ...)`` with ``ts_rank``
# ordering (operator-approved drift from SQLite FTS5 BM25).
#
# Per-function contract tests + search equivalence tests live in
# ``test_db_episodic_memory.py``.


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision rules (Phase 50B)  —  MOVED TO test_db_decision_rules.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Phase-3-Runtime-v2 SP-3.11 (2026-04-20): load_decision_rules /
# replace_decision_rules now take explicit ``asyncpg.Connection``.
# The old manual ``BEGIN IMMEDIATE`` / commit / rollback transaction
# is replaced with ``async with conn.transaction()``; atomicity
# (DELETE + bulk INSERT all-or-nothing) is preserved.
#
# Per-function contract tests — including the atomic-replace invariant
# and tenant-scoped DELETE guard (cross-tenant rules must survive a
# replace call) — live in ``test_db_decision_rules.py``.

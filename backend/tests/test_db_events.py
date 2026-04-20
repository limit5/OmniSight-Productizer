"""Phase-3-Runtime-v2 SP-3.10 — contract tests for ported event_log
db.py functions.

Replaces the SQLite-backed ``test_event_log_insert_list_cleanup`` in
``test_db.py`` AND preserves the RLS coverage previously in
``tests/test_rls.py::TestEventLogRLS`` (skipped with SP-3.10
rationale pointing here).

Coverage:
  * Three functions: insert_event / list_events / cleanup_old_events.
  * Filters: by event_type IN (...) list + since (timestamp lower bound).
  * **Tenant isolation (load-bearing)**:
    - insert auto-fills tenant_id from context.
    - list is scoped to current tenant.
    - **cleanup is scoped to current tenant** — pre-port cleanup_old_events
      deleted globally across tenants; this test locks the fix.
  * Cleanup cutoff semantics with **deterministic** boundaries
    (days=365: no deletes; days=-1: all deleted). The racy ``days=0``
    case from SP-3.5 is intentionally NOT tested — that boundary sits
    on the second-resolution of the created_at column and is
    inherently flaky.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import json

import pytest

from backend import db
from backend.db_context import set_tenant_id


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


TENANT_A = "t-alpha"
TENANT_B = "t-beta"


# ─── Happy path ──────────────────────────────────────────────────


class TestEventsCrud:
    @pytest.mark.asyncio
    async def test_insert_then_list(self, pg_test_conn) -> None:
        await db.insert_event(
            pg_test_conn, "agent_update", json.dumps({"id": "a1"}),
        )
        await db.insert_event(
            pg_test_conn, "task_update", json.dumps({"id": "t1"}),
        )
        rows = await db.list_events(pg_test_conn)
        assert len(rows) == 2
        types = {r["event_type"] for r in rows}
        assert types == {"agent_update", "task_update"}

    @pytest.mark.asyncio
    async def test_filter_by_types(self, pg_test_conn) -> None:
        for t in ("agent_update", "task_update", "simulation"):
            await db.insert_event(pg_test_conn, t, "{}")
        rows = await db.list_events(
            pg_test_conn, event_types=["agent_update", "simulation"],
        )
        assert {r["event_type"] for r in rows} == {
            "agent_update", "simulation",
        }

    @pytest.mark.asyncio
    async def test_filter_by_since(self, pg_test_conn) -> None:
        # created_at is server-side; inserts here happen in tx-now.
        # To exercise the since filter, insert a row then probe with
        # a since = "9999-01-01" (future) — should return 0.
        await db.insert_event(pg_test_conn, "test_event", "{}")
        rows = await db.list_events(pg_test_conn, since="9999-01-01")
        assert rows == []

    @pytest.mark.asyncio
    async def test_list_order_newest_first(self, pg_test_conn) -> None:
        # ORDER BY id DESC — autoincrement PK monotonic per
        # insertion order.
        for i in range(3):
            await db.insert_event(
                pg_test_conn, f"type-{i}", json.dumps({"n": i}),
            )
        rows = await db.list_events(pg_test_conn)
        assert [r["event_type"] for r in rows] == [
            "type-2", "type-1", "type-0",
        ]

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, pg_test_conn) -> None:
        for i in range(5):
            await db.insert_event(pg_test_conn, f"t{i}", "{}")
        rows = await db.list_events(pg_test_conn, limit=2)
        assert len(rows) == 2


# ─── Tenant isolation ────────────────────────────────────────────


class TestEventsTenantIsolation:
    @pytest.mark.asyncio
    async def test_insert_auto_fills_tenant(self, pg_test_conn) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_event(pg_test_conn, "test", "{}")
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM event_log WHERE event_type = $1",
            "test",
        )
        assert row["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_list_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_event(pg_test_conn, "alpha_event", "{}")
        set_tenant_id(TENANT_B)
        await db.insert_event(pg_test_conn, "beta_event", "{}")

        set_tenant_id(TENANT_A)
        rows = await db.list_events(pg_test_conn)
        types = {r["event_type"] for r in rows}
        assert types == {"alpha_event"}
        assert "beta_event" not in types


# ─── Cleanup semantics + tenant-scoped-cleanup bug fix ───────────


class TestEventsCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_far_future_deletes_all(
        self, pg_test_conn,
    ) -> None:
        # days=-1 → cutoff is 1 day IN THE FUTURE → every row has
        # created_at < future → all deleted. Deterministic boundary.
        await db.insert_event(pg_test_conn, "e1", "{}")
        await db.insert_event(pg_test_conn, "e2", "{}")
        deleted = await db.cleanup_old_events(pg_test_conn, days=-1)
        assert deleted == 2
        assert await db.list_events(pg_test_conn) == []

    @pytest.mark.asyncio
    async def test_cleanup_far_past_deletes_nothing(
        self, pg_test_conn,
    ) -> None:
        # days=365 → cutoff is 365 days ago → no freshly-inserted row
        # qualifies. Deterministic boundary.
        await db.insert_event(pg_test_conn, "e1", "{}")
        await db.insert_event(pg_test_conn, "e2", "{}")
        deleted = await db.cleanup_old_events(pg_test_conn, days=365)
        assert deleted == 0
        assert len(await db.list_events(pg_test_conn)) == 2

    @pytest.mark.asyncio
    async def test_cleanup_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        # LOAD-BEARING SAFETY TEST: pre-port cleanup_old_events had
        # NO tenant filter. SP-3.10 adds one. Regression guard against
        # a refactor that drops the filter — if cleanup runs against
        # Tenant A's schedule and deletes Tenant B's events, that's
        # cross-tenant data destruction.
        set_tenant_id(TENANT_A)
        await db.insert_event(pg_test_conn, "alpha_old", "{}")
        set_tenant_id(TENANT_B)
        await db.insert_event(pg_test_conn, "beta_old", "{}")

        # Cleanup on Tenant A's schedule with days=-1 (aggressive).
        # Should delete Tenant A's row ONLY.
        set_tenant_id(TENANT_A)
        deleted = await db.cleanup_old_events(pg_test_conn, days=-1)
        assert deleted == 1

        # Tenant B's row must still exist — confirm without filter.
        set_tenant_id(None)
        all_rows = await pg_test_conn.fetch(
            "SELECT event_type FROM event_log",
        )
        types = {r["event_type"] for r in all_rows}
        assert types == {"beta_old"}


# ─── Error paths ─────────────────────────────────────────────────


class TestEventsErrorPaths:
    @pytest.mark.asyncio
    async def test_insert_rejects_null_event_type(
        self, pg_test_conn,
    ) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_event(pg_test_conn, None, "{}")

    @pytest.mark.asyncio
    async def test_insert_rejects_null_data_json(
        self, pg_test_conn,
    ) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_event(pg_test_conn, "test_type", None)

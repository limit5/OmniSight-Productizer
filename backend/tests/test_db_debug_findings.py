"""Phase-3-Runtime-v2 SP-3.9 — contract tests for ported debug_finding
db.py functions.

Replaces the SQLite-backed ``test_debug_finding_insert_update`` in
``test_db.py`` AND preserves the RLS coverage previously in
``tests/test_rls.py::TestDebugFindingRLS`` (which is skipped with a
SP-3.9 rationale pointing here — same pattern as SP-3.6b for
TestArtifactRLS).

Coverage:
  * Three functions: insert_debug_finding / list_debug_findings /
    update_debug_finding.
  * **ON CONFLICT (id) DO NOTHING** — duplicate id is no-op; the
    shared blackboard's append-only guarantee.
  * **Tenant isolation (load-bearing)**:
    - insert auto-fills tenant_id from context (anti-forge).
    - list is scoped to current tenant.
    - update cross-tenant is no-op; victim tenant's row intact.
  * update sets both ``status`` and ``resolved_at`` (to_char(clock_timestamp)).

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL).
"""

from __future__ import annotations

import pytest

from backend import db
from backend.db_context import set_tenant_id


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _finding_fixture(**overrides) -> dict:
    base = {
        "id": "dbg-test",
        "task_id": "t-test",
        "agent_id": "a-test",
        "finding_type": "error",
        "severity": "warn",
        "content": "something went wrong",
        "context": "{}",
        "status": "open",
        "created_at": "2026-04-20T00:00:00",
    }
    base.update(overrides)
    return base


TENANT_A = "t-alpha"
TENANT_B = "t-beta"


# ─── Happy path ──────────────────────────────────────────────────


class TestDebugFindingsCrud:
    @pytest.mark.asyncio
    async def test_insert_then_list(self, pg_test_conn) -> None:
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(id="f1"))
        rows = await db.list_debug_findings(pg_test_conn, status="open")
        assert len(rows) == 1
        assert rows[0]["id"] == "f1"

    @pytest.mark.asyncio
    async def test_duplicate_id_is_noop(self, pg_test_conn) -> None:
        # ON CONFLICT (id) DO NOTHING — second insert with same id is
        # silently ignored; the original row is preserved unchanged.
        # Regression guard: if the migration replaces this with
        # ``ON CONFLICT DO UPDATE``, the blackboard's "first finding
        # wins" semantics break.
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-dup", content="first entry", severity="error",
        ))
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-dup", content="SECOND entry", severity="info",
        ))
        rows = await db.list_debug_findings(pg_test_conn)
        assert len(rows) == 1
        assert rows[0]["content"] == "first entry"
        assert rows[0]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_filter_by_task(self, pg_test_conn) -> None:
        for i, tid in enumerate(("t-A", "t-A", "t-B")):
            await db.insert_debug_finding(pg_test_conn, _finding_fixture(
                id=f"f-task-{i}", task_id=tid,
            ))
        rows = await db.list_debug_findings(pg_test_conn, task_id="t-A")
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_filter_by_agent(self, pg_test_conn) -> None:
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-aX", agent_id="a-X",
        ))
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-aY", agent_id="a-Y",
        ))
        rows = await db.list_debug_findings(pg_test_conn, agent_id="a-Y")
        assert [r["id"] for r in rows] == ["f-aY"]

    @pytest.mark.asyncio
    async def test_filter_by_status(self, pg_test_conn) -> None:
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-open", status="open",
        ))
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-resolved", status="resolved",
        ))
        rows = await db.list_debug_findings(pg_test_conn, status="open")
        assert [r["id"] for r in rows] == ["f-open"]


# ─── Update semantics + ordering ─────────────────────────────────


class TestDebugFindingsUpdate:
    @pytest.mark.asyncio
    async def test_update_sets_status_and_resolved_at(
        self, pg_test_conn,
    ) -> None:
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(id="f-u"))
        # Pre-update: resolved_at is NULL (default on insert).
        row = await pg_test_conn.fetchrow(
            "SELECT status, resolved_at FROM debug_findings WHERE id = $1",
            "f-u",
        )
        assert row["status"] == "open"
        assert row["resolved_at"] is None

        ok = await db.update_debug_finding(pg_test_conn, "f-u", "resolved")
        assert ok is True

        row = await pg_test_conn.fetchrow(
            "SELECT status, resolved_at FROM debug_findings WHERE id = $1",
            "f-u",
        )
        assert row["status"] == "resolved"
        # resolved_at is populated (non-empty string matching the
        # YYYY-MM-DD HH:MM:SS format from to_char(clock_timestamp)).
        assert row["resolved_at"] is not None
        assert len(row["resolved_at"]) == 19  # "YYYY-MM-DD HH:MM:SS"

    @pytest.mark.asyncio
    async def test_update_missing_returns_false(self, pg_test_conn) -> None:
        assert await db.update_debug_finding(
            pg_test_conn, "never-existed", "resolved",
        ) is False


# ─── Tenant isolation — preserves tests/test_rls coverage ────────


class TestDebugFindingsTenantIsolation:
    @pytest.mark.asyncio
    async def test_insert_auto_fills_tenant(self, pg_test_conn) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-ctx",
        ))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM debug_findings WHERE id = $1", "f-ctx",
        )
        assert row["tenant_id"] == TENANT_A

    @pytest.mark.asyncio
    async def test_insert_with_no_context_uses_default(
        self, pg_test_conn,
    ) -> None:
        # tenant_insert_value() returns "t-default" when no context set.
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-def",
        ))
        row = await pg_test_conn.fetchrow(
            "SELECT tenant_id FROM debug_findings WHERE id = $1", "f-def",
        )
        assert row["tenant_id"] == "t-default"

    @pytest.mark.asyncio
    async def test_list_scoped_to_current_tenant(
        self, pg_test_conn,
    ) -> None:
        set_tenant_id(TENANT_A)
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-a", content="alpha finding",
        ))
        set_tenant_id(TENANT_B)
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-b", content="beta finding",
        ))
        set_tenant_id(TENANT_A)
        rows = await db.list_debug_findings(pg_test_conn)
        ids = {r["id"] for r in rows}
        assert ids == {"f-a"}
        assert "f-b" not in ids

    @pytest.mark.asyncio
    async def test_update_cross_tenant_is_noop(self, pg_test_conn) -> None:
        # Tenant A owns the finding. Tenant B attempts to update it.
        # The tenant_where_pg filter must prevent the write — attest
        # both the return value (False; rowcount=0) AND the row's
        # actual state (still "open", resolved_at still NULL).
        set_tenant_id(TENANT_A)
        await db.insert_debug_finding(pg_test_conn, _finding_fixture(
            id="f-cross",
        ))
        set_tenant_id(TENANT_B)
        ok = await db.update_debug_finding(
            pg_test_conn, "f-cross", "resolved",
        )
        assert ok is False

        # Confirm the row's original state is intact — no partial writes.
        # Need to read without tenant filter to see it.
        set_tenant_id(None)
        row = await pg_test_conn.fetchrow(
            "SELECT status, resolved_at FROM debug_findings WHERE id = $1",
            "f-cross",
        )
        assert row["status"] == "open"
        assert row["resolved_at"] is None


# ─── Error paths ─────────────────────────────────────────────────


class TestDebugFindingsErrorPaths:
    @pytest.mark.asyncio
    async def test_insert_missing_required_key_raises(
        self, pg_test_conn,
    ) -> None:
        with pytest.raises(KeyError):
            await db.insert_debug_finding(pg_test_conn, {
                # missing "id", "task_id", "agent_id", "finding_type",
                # "content"
                "severity": "info",
            })

    @pytest.mark.asyncio
    async def test_insert_rejects_null_content(self, pg_test_conn) -> None:
        import asyncpg
        with pytest.raises(asyncpg.exceptions.NotNullViolationError):
            await db.insert_debug_finding(pg_test_conn, _finding_fixture(
                id="f-null", content=None,
            ))

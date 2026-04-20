"""Tests for Handoff Visualization + NPI Gantt (Phase 27)."""

import pytest


class TestHandoffChainEndpoint:

    @pytest.mark.asyncio
    async def test_task_handoffs_empty(self, client):
        resp = await client.get("/api/v1/tasks/nonexistent/handoffs")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_recent_handoffs(self, client):
        resp = await client.get("/api/v1/tasks/handoffs/recent?limit=5")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_handoff_after_upsert(self, client):
        # SP-3.3 (2026-04-20): upsert_handoff now requires an explicit
        # asyncpg.Connection. The client fixture owns the module-global
        # pool (init_pool was called there), so acquire inline for the
        # seed write. The write auto-commits (outside a tx block), so
        # the subsequent GET — served by a handler with its own
        # Depends(get_conn) — sees the row at READ COMMITTED.
        # Cleanup is explicit (delete row) to keep sibling tests clean.
        from backend import db
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            await db.upsert_handoff(conn, "task-viz-1", "agent-fw-1", "# Handoff\nTest content")
        try:
            resp = await client.get("/api/v1/tasks/task-viz-1/handoffs")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) >= 1
            assert data[0]["agent_id"] == "agent-fw-1"
        finally:
            async with get_pool().acquire() as conn:
                await conn.execute(
                    "DELETE FROM handoffs WHERE task_id = $1", "task-viz-1",
                )


class TestHandoffTimelineComponent:

    def test_component_file_exists(self):
        from pathlib import Path
        comp = Path(__file__).resolve().parent.parent.parent / "components" / "omnisight" / "handoff-timeline.tsx"
        assert comp.exists()


class TestNPIGanttComponent:

    def test_component_file_exists(self):
        from pathlib import Path
        comp = Path(__file__).resolve().parent.parent.parent / "components" / "omnisight" / "npi-gantt.tsx"
        assert comp.exists()
        content = comp.read_text()
        assert "NPIGantt" in content
        assert "BarChart" in content or "gantt" in content.lower()


class TestAPIFunctions:

    def test_handoff_api_types_exist(self):
        """Verify HandoffItem type and API functions are defined in api.ts."""
        from pathlib import Path
        api_file = Path(__file__).resolve().parent.parent.parent / "lib" / "api.ts"
        content = api_file.read_text()
        assert "HandoffItem" in content
        assert "getTaskHandoffs" in content
        assert "getRecentHandoffs" in content

"""Tests for debug blackboard and loop detection (Phase 20)."""

import json
import uuid

import pytest


class TestDebugFindingsDB:

    @pytest.mark.asyncio
    async def test_insert_and_list(self):
        from backend import db
        await db.init()
        try:
            fid = f"dbg-{uuid.uuid4().hex[:6]}"
            await db.insert_debug_finding({
                "id": fid, "task_id": "t-1", "agent_id": "a-1",
                "finding_type": "stuck_loop", "severity": "error",
                "content": "Tool failed 3 times", "context": "{}",
                "status": "open", "created_at": "2026-01-01T00:00:00",
            })
            findings = await db.list_debug_findings(task_id="t-1")
            assert any(f["id"] == fid for f in findings)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_update_status(self):
        from backend import db
        await db.init()
        try:
            fid = f"dbg-upd-{uuid.uuid4().hex[:6]}"
            await db.insert_debug_finding({
                "id": fid, "task_id": "t-2", "agent_id": "a-2",
                "finding_type": "error_repeated", "severity": "warn",
                "content": "Same error twice", "context": "{}",
                "status": "open", "created_at": "2026-01-01T00:00:00",
            })
            result = await db.update_debug_finding(fid, "resolved")
            assert result is True
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_filter_by_status(self):
        from backend import db
        await db.init()
        try:
            for status in ("open", "resolved"):
                await db.insert_debug_finding({
                    "id": f"dbg-filt-{status}-{uuid.uuid4().hex[:4]}",
                    "task_id": "t-3", "agent_id": "a-3",
                    "finding_type": "stuck_loop", "severity": "error",
                    "content": f"Test {status}", "context": "{}",
                    "status": status, "created_at": "2026-01-01T00:00:00",
                })
            open_findings = await db.list_debug_findings(status="open")
            assert all(f["status"] == "open" for f in open_findings)
        finally:
            await db.close()


class TestDebugEndpoint:

    @pytest.mark.asyncio
    async def test_debug_state_endpoint(self, client):
        resp = await client.get("/api/v1/system/debug")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_errors" in data
        assert "blocked_tasks" in data
        assert "total_findings" in data
        assert "findings_by_type" in data
        assert "recent_findings" in data


class TestLoopDetection:

    def test_extract_error_key(self):
        from backend.agents.nodes import _extract_error_key
        assert _extract_error_key("run_bash: command failed") == "run_bash"
        assert _extract_error_key("no colon here") == "no colon here"

    def test_graphstate_error_history_default(self):
        from backend.agents.state import GraphState
        state = GraphState()
        assert state.error_history == []
        assert state.same_error_count == 0
        assert state.loop_breaker_triggered is False

    def test_graphstate_task_id_default(self):
        from backend.agents.state import GraphState
        state = GraphState()
        assert state.task_id is None

    def test_should_retry_loop_breaker(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        state = GraphState(loop_breaker_triggered=True, last_error="err", retry_count=1)
        assert _should_retry(state) == "summarizer"

    def test_should_retry_normal(self):
        from backend.agents.nodes import _should_retry
        from backend.agents.state import GraphState
        state = GraphState(last_error="err", retry_count=1, max_retries=3)
        assert _should_retry(state) == state.routed_to


class TestDebugFindingModel:

    def test_model_creation(self):
        from backend.models import DebugFinding
        f = DebugFinding(
            id="dbg-1", task_id="t-1", agent_id="a-1",
            finding_type="stuck_loop", content="test",
        )
        assert f.severity == "info"
        assert f.status == "open"
        assert f.resolved_at is None

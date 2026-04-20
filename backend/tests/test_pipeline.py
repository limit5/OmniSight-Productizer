"""Tests for E2E Orchestration Pipeline (Phase 46).

Covers:
- Pipeline steps definition
- Pipeline start/status/advance
- Task npi_phase_id linkage
- Phase completion detection
- Slash command
- API endpoints
"""

from __future__ import annotations

import pytest

from backend.pipeline import (
    PIPELINE_STEPS,
    get_pipeline_status,
    run_pipeline,
    advance_pipeline,
    force_advance,
    _check_phase_complete,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pipeline Steps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPipelineSteps:

    def test_step_count(self):
        assert len(PIPELINE_STEPS) == 7

    def test_step_order(self):
        ids = [s["id"] for s in PIPELINE_STEPS]
        assert ids == ["spec", "develop", "review", "test", "deploy", "package", "docs"]

    def test_all_steps_have_required_fields(self):
        for step in PIPELINE_STEPS:
            assert "id" in step
            assert "name" in step
            assert "npi_phase" in step
            assert "tasks" in step
            assert "auto_advance" in step
            assert len(step["tasks"]) >= 1

    def test_human_checkpoints(self):
        review = next(s for s in PIPELINE_STEPS if s["id"] == "review")
        deploy = next(s for s in PIPELINE_STEPS if s["id"] == "deploy")
        assert review["auto_advance"] is False
        assert deploy["auto_advance"] is False
        assert "human_checkpoint" in review
        assert "human_checkpoint" in deploy

    def test_auto_advance_steps(self):
        auto = [s for s in PIPELINE_STEPS if s["auto_advance"]]
        assert len(auto) == 5  # spec, develop, test, package, docs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pipeline Status
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPipelineStatus:

    def test_idle_status(self):
        import backend.pipeline as p
        p._active_pipeline = None
        status = get_pipeline_status()
        assert status["status"] == "idle"
        assert "steps" in status

    @pytest.mark.asyncio
    async def test_start_pipeline(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        result = await run_pipeline("Build AI UVC camera with IMX335")
        assert result["status"] == "running"
        assert result["current_step"] == "spec"
        assert result["tasks_created"] >= 1
        # Cleanup
        p._active_pipeline = None

    @pytest.mark.asyncio
    async def test_double_start_rejected(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        await run_pipeline("test")
        result = await run_pipeline("test again")
        assert result["status"] == "error"
        p._active_pipeline = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase Completion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPhaseCompletion:

    @pytest.mark.asyncio
    async def test_empty_phase_is_complete(self, client):
        result = await _check_phase_complete("nonexistent-phase")
        assert result is True

    @pytest.mark.asyncio
    async def test_advance_idle_pipeline(self):
        import backend.pipeline as p
        p._active_pipeline = None
        result = await advance_pipeline()
        assert result["status"] == "idle"

    @pytest.mark.asyncio
    async def test_force_advance(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        await run_pipeline("test")
        # Force advance from spec to develop
        result = await force_advance()
        assert result["status"] in ("advanced", "completed")
        p._active_pipeline = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task npi_phase_id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTaskPhaseLinkage:

    def test_task_model_has_npi_phase_id(self):
        from backend.models import Task
        t = Task(id="t1", title="test", npi_phase_id="phase-2")
        assert t.npi_phase_id == "phase-2"

    def test_task_npi_phase_id_optional(self):
        from backend.models import Task
        t = Task(id="t2", title="test")
        assert t.npi_phase_id is None

    @pytest.mark.asyncio
    async def test_db_round_trip(self, pg_test_conn):
        # SP-3.2 (2026-04-20): upsert_task / get_task now require an
        # asyncpg.Connection as first argument. pg_test_conn is a
        # pool-borrowed savepoint conn that rolls back on teardown —
        # no schema pollution across tests.
        from backend import db
        await db.upsert_task(pg_test_conn, {
            "id": "pipe-test-1",
            "title": "Pipeline test task",
            "npi_phase_id": "phase-3",
        })
        row = await db.get_task(pg_test_conn, "pipe-test-1")
        assert row is not None
        assert row["npi_phase_id"] == "phase-3"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slash Command
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPipelineSlashCommand:

    @pytest.mark.asyncio
    async def test_pipeline_no_args(self, client):
        # SP-3.1 (2026-04-20): handle_slash_command signature is now
        # (conn, command, args); the /pipeline handler reads state from
        # memory only, so conn=None is safe.
        import backend.pipeline as p
        p._active_pipeline = None
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "pipeline", "")
        assert "Pipeline" in result
        assert "idle" in result.lower() or "Status" in result

    @pytest.mark.asyncio
    async def test_pipeline_start(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        from backend.slash_commands import handle_slash_command
        result = await handle_slash_command(None, "pipeline", "start Build AI camera")
        assert "Started" in result or "Pipeline" in result
        p._active_pipeline = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  API Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPipelineEndpoints:

    @pytest.mark.asyncio
    async def test_get_status(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        resp = await client.get("/api/v1/runtime/pipeline/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
        assert "steps" in data

    @pytest.mark.asyncio
    async def test_start_pipeline(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        resp = await client.post("/api/v1/runtime/pipeline/start", json={
            "spec_context": "AI UVC camera with face recognition",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["current_step"] == "spec"
        p._active_pipeline = None

    @pytest.mark.asyncio
    async def test_force_advance(self, client):
        import backend.pipeline as p
        p._active_pipeline = None
        await client.post("/api/v1/runtime/pipeline/start", json={"spec_context": "test"})
        resp = await client.post("/api/v1/runtime/pipeline/advance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("advanced", "completed", "error")
        p._active_pipeline = None

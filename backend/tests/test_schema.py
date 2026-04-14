"""Tests for Schema Formalization (Phase 31).

Covers:
- Response model validation (endpoints return data matching Pydantic models)
- DB upsert round-trip (fields survive insert → read cycle)
- SSE event schema completeness
- SimulationStatus enum alignment
"""

from __future__ import annotations


import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Response Model Validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestHealthResponse:

    @pytest.mark.asyncio
    async def test_health_matches_model(self, client):
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "engine" in data
        assert "version" in data
        assert "phase" in data


class TestSystemInfoResponse:

    @pytest.mark.asyncio
    async def test_info_matches_model(self, client):
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 200
        data = resp.json()
        # All fields from SystemInfoResponse should be present
        for field in ("hostname", "os", "kernel", "arch", "cpu_cores",
                      "memory_total", "memory_used", "disk_total_mb"):
            assert field in data, f"Missing field: {field}"


class TestSystemStatusResponse:

    @pytest.mark.asyncio
    async def test_status_has_cpu_summary(self, client):
        resp = await client.get("/api/v1/system/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "cpu_summary" in data
        assert "memory_summary" in data
        assert "tasks_completed" in data
        assert "agents_running" in data


class TestTokenBudgetResponse:

    @pytest.mark.asyncio
    async def test_budget_matches_model(self, client):
        resp = await client.get("/api/v1/system/token-budget")
        assert resp.status_code == 200
        data = resp.json()
        for field in ("budget", "usage", "ratio", "frozen", "level"):
            assert field in data, f"Missing field: {field}"


class TestProvidersResponse:

    @pytest.mark.asyncio
    async def test_providers_matches_model(self, client):
        resp = await client.get("/api/v1/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_provider" in data
        assert "active_model" in data
        assert "providers" in data
        assert isinstance(data["providers"], list)


class TestProviderHealthResponse:

    @pytest.mark.asyncio
    async def test_health_matches_model(self, client):
        resp = await client.get("/api/v1/providers/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "chain" in data
        assert "health" in data


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DB Upsert Round-Trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTaskUpsertRoundTrip:

    @pytest.mark.asyncio
    async def test_depends_on_persisted(self, client):
        """depends_on should survive upsert round-trip."""
        from backend import db
        task_data = {
            "id": "test-schema-dep",
            "title": "Schema test task",
            "depends_on": ["task-a", "task-b"],
        }
        await db.upsert_task(task_data)
        row = await db.get_task("test-schema-dep")
        assert row is not None
        assert row["depends_on"] == ["task-a", "task-b"]

    @pytest.mark.asyncio
    async def test_external_issue_platform_persisted(self, client):
        from backend import db
        task_data = {
            "id": "test-schema-ext",
            "title": "External issue test",
            "external_issue_platform": "github",
            "last_external_sync_at": "2026-04-13T00:00:00",
        }
        await db.upsert_task(task_data)
        row = await db.get_task("test-schema-ext")
        assert row is not None
        assert row["external_issue_platform"] == "github"
        assert row["last_external_sync_at"] == "2026-04-13T00:00:00"

    @pytest.mark.asyncio
    async def test_labels_round_trip(self, client):
        from backend import db
        task_data = {
            "id": "test-schema-labels",
            "title": "Labels test",
            "labels": ["bug", "firmware", "urgent"],
        }
        await db.upsert_task(task_data)
        row = await db.get_task("test-schema-labels")
        assert row["labels"] == ["bug", "firmware", "urgent"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE Event Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSSESchemaExport:

    def test_all_events_have_schemas(self):
        from backend.sse_schemas import SSE_EVENT_SCHEMAS
        expected_events = {
            # Core runtime
            "agent_update", "task_update", "tool_progress", "pipeline",
            "workspace", "container", "invoke", "token_warning",
            "simulation", "debug_finding", "notification",
            "artifact_created", "heartbeat",
            # Phase 47 Autonomous Decision Engine
            "mode_changed", "decision_pending", "decision_auto_executed",
            "decision_resolved", "decision_undone", "budget_strategy_changed",
        }
        assert set(SSE_EVENT_SCHEMAS.keys()) == expected_events

    def test_schema_export_is_valid_json_schema(self):
        from backend.sse_schemas import get_sse_schema_export, SSE_EVENT_SCHEMAS
        export = get_sse_schema_export()
        # Size equals the (dynamic) event map — don't hard-code a magic
        # number that drifts every time a new SSE event type is added.
        assert len(export) == len(SSE_EVENT_SCHEMAS)
        for event_type, info in export.items():
            assert "schema" in info
            schema = info["schema"]
            assert "properties" in schema
            assert "title" in schema

    def test_agent_update_has_required_fields(self):
        from backend.sse_schemas import SSEAgentUpdate
        schema = SSEAgentUpdate.model_json_schema()
        assert "agent_id" in schema["properties"]
        assert "status" in schema["properties"]

    def test_tool_progress_has_phase_field(self):
        from backend.sse_schemas import SSEToolProgress
        schema = SSEToolProgress.model_json_schema()
        assert "phase" in schema["properties"]
        assert "tool_name" in schema["properties"]

    @pytest.mark.asyncio
    async def test_sse_schema_endpoint(self, client):
        resp = await client.get("/api/v1/system/sse-schema")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_update" in data
        assert "schema" in data["agent_update"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum Alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulationStatusEnum:

    def test_values_match_frontend(self):
        from backend.models import SimulationStatus
        assert SimulationStatus.passed.value == "pass"
        assert SimulationStatus.failed.value == "fail"
        assert SimulationStatus.running.value == "running"
        assert SimulationStatus.error.value == "error"

    def test_all_task_statuses_valid(self):
        from backend.models import TaskStatus
        expected = {"backlog", "analyzing", "assigned", "in_progress",
                    "in_review", "completed", "blocked"}
        actual = {s.value for s in TaskStatus}
        assert actual == expected


class TestResponseModelConsistency:

    def test_system_info_model_fields(self):
        from backend.models import SystemInfoResponse
        fields = set(SystemInfoResponse.model_fields.keys())
        assert "hostname" in fields
        assert "cpu_model" in fields
        assert "docker" in fields

    def test_token_budget_model_fields(self):
        from backend.models import TokenBudgetResponse
        fields = set(TokenBudgetResponse.model_fields.keys())
        assert "budget" in fields
        assert "frozen" in fields
        assert "fallback_provider" in fields

    def test_providers_list_response_structure(self):
        from backend.models import ProvidersListResponse, ProviderInfo
        resp = ProvidersListResponse(
            active_provider="anthropic",
            active_model="claude-sonnet",
            providers=[ProviderInfo(id="anthropic", name="Anthropic")],
        )
        assert resp.active_provider == "anthropic"
        assert len(resp.providers) == 1

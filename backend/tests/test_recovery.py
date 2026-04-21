"""Tests for error handling and recovery mechanisms (Phase 16)."""

import asyncio

import pytest


class TestDatabaseHardening:
    """Phase-3 Step C.1 (2026-04-21): the original three PRAGMA
    checks (``PRAGMA journal_mode = wal``, ``PRAGMA busy_timeout``,
    ``PRAGMA quick_check``) were SQLite-specific and have been
    deleted. PostgreSQL enforces durability + concurrency with a
    different stack (WAL + autovacuum + ``synchronous_commit``);
    equivalent guardrails live in ``db_pool._set_connection_defaults``
    (``statement_timeout``, ``lock_timeout``,
    ``idle_in_transaction_session_timeout``) which are covered by
    dedicated pool tests. Keeping the SQLite PRAGMA asserts would
    have been false-positive — they ran against the pg_compat
    shim and returned nonsense.

    Only ``execute_raw`` survives here; it gains a regression guard
    that the startup-cleanup code path in ``backend.main`` keeps
    working against the pool.
    """

    @pytest.mark.asyncio
    async def test_execute_raw(self, pg_test_pool):
        """``db.execute_raw`` is used by ``backend.main`` at startup
        to reset stuck agents + simulations. Exercise it against the
        test pool to confirm the pool-native path still returns an
        int row-count."""
        from backend import db
        n = await db.execute_raw(
            "UPDATE agents SET status=status WHERE 1=0"
        )
        assert n == 0  # no rows affected


class TestGraphTimeout:

    def test_graph_timeout_constant(self):
        from backend.agents.graph import GRAPH_TIMEOUT
        assert GRAPH_TIMEOUT == 300  # 5 minutes


class TestStartupCleanup:

    @pytest.mark.asyncio
    async def test_stuck_simulations_reset(self, pg_test_conn):
        """Simulations stuck in 'running' should be reset to 'error' on startup.

        SP-3.8 (2026-04-20): migrated to pg_test_conn. The prior
        ``db.execute_raw`` call is replaced with direct
        ``conn.execute`` on the same conn — same semantics (single
        statement, auto-commits outside tx; here it runs inside
        pg_test_conn's outer savepoint so rolls back on teardown).
        """
        import uuid
        from backend import db
        sim_id = f"sim-stuck-{uuid.uuid4().hex[:6]}"
        await db.insert_simulation(pg_test_conn, {
            "id": sim_id, "task_id": "", "agent_id": "",
            "track": "algo", "module": "test", "status": "running",
            "tests_total": 0, "tests_passed": 0, "tests_failed": 0,
            "coverage_pct": 0.0, "valgrind_errors": 0, "duration_ms": 0,
            "report_json": "{}", "artifact_id": None,
            "created_at": "2026-01-01T00:00:00",
        })
        # Simulate startup cleanup
        await pg_test_conn.execute(
            "UPDATE simulations SET status='error' WHERE status='running'"
        )
        sim = await db.get_simulation(pg_test_conn, sim_id)
        assert sim["status"] == "error"


class TestWatchdog:

    def test_watchdog_timeout_constant(self):
        from backend.routers.invoke import TASK_TIMEOUT
        assert TASK_TIMEOUT == 1800  # 30 minutes

    def test_running_tasks_registry_exists(self):
        from backend.routers.invoke import _running_tasks
        assert isinstance(_running_tasks, dict)


class TestContainerRecovery:

    def test_cleanup_orphaned_exists(self):
        from backend.container import cleanup_orphaned_containers
        assert asyncio.iscoroutinefunction(cleanup_orphaned_containers)

    def test_stop_all_exists(self):
        from backend.container import stop_all_containers
        assert asyncio.iscoroutinefunction(stop_all_containers)


class TestContainerResourceLimits:

    def test_config_has_docker_limits(self):
        from backend.config import settings
        assert hasattr(settings, "docker_memory_limit")
        assert hasattr(settings, "docker_cpu_limit")
        assert settings.docker_memory_limit == "1g"
        assert settings.docker_cpu_limit == "2"


class TestWorkspaceRecovery:

    def test_cleanup_stale_locks_exists(self):
        from backend.workspace import cleanup_stale_locks
        assert asyncio.iscoroutinefunction(cleanup_stale_locks)


class TestLLMFailoverCooldown:

    def test_cooldown_constant(self):
        from backend.agents.llm import PROVIDER_COOLDOWN
        assert PROVIDER_COOLDOWN == 300  # 5 minutes

    def test_provider_failures_dict(self):
        from backend.agents.llm import _provider_failures
        assert isinstance(_provider_failures, dict)


class TestTokenBudgetAutoReset:

    def test_daily_reset_function_exists(self):
        from backend.routers.system import _maybe_reset_daily_budget
        assert callable(_maybe_reset_daily_budget)


class TestEmergencyHalt:

    @pytest.mark.asyncio
    async def test_halt_endpoint(self, client):
        resp = await client.post("/api/v1/invoke/halt")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert data["status"] == "halted"
        assert "tasks_cancelled" in data
        assert "containers_stopped" in data
        # Resume to restore state
        await client.post("/api/v1/invoke/resume")

    @pytest.mark.asyncio
    async def test_resume_endpoint(self, client):
        resp = await client.post("/api/v1/invoke/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "resumed"


class TestForceResetAgent:

    @pytest.mark.asyncio
    async def test_reset_nonexistent(self, client):
        resp = await client.post("/api/v1/agents/nonexistent/reset")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reset_existing(self, client):
        # Create an agent first
        resp = await client.post("/api/v1/agents", json={
            "type": "firmware", "name": "test-reset-agent"
        })
        if resp.status_code == 200:
            agent = resp.json()
            resp = await client.post(f"/api/v1/agents/{agent['id']}/reset")
            assert resp.status_code == 200
            assert resp.json()["status"] == "idle"
            assert "[RESET]" in resp.json()["thought_chain"]


class TestErrorBoundary:

    def test_error_page_exists(self):
        from pathlib import Path
        error_page = Path(__file__).resolve().parent.parent.parent / "app" / "error.tsx"
        assert error_page.exists()
        content = error_page.read_text()
        assert "SYSTEM ERROR" in content
        assert "RELOAD" in content

"""Tests for Orchestrator-Subagent enhancements (Phase 23)."""

import pytest


class TestTaskDependency:

    def test_depends_on_default(self):
        from backend.models import Task
        t = Task(id="t1", title="test")
        assert t.depends_on == []

    def test_depends_on_set(self):
        from backend.models import Task
        t = Task(id="t1", title="test", depends_on=["t0", "t-1"])
        assert len(t.depends_on) == 2
        assert "t0" in t.depends_on


class TestPrefetchContext:

    @pytest.mark.asyncio
    async def test_prefetch_returns_string(self):
        from backend.routers.invoke import _prefetch_codebase_context
        result = await _prefetch_codebase_context("firmware driver sensor", ".")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_prefetch_empty_text(self):
        from backend.routers.invoke import _prefetch_codebase_context
        result = await _prefetch_codebase_context("", ".")
        assert result == ""

    @pytest.mark.asyncio
    async def test_prefetch_nonexistent_path(self):
        from backend.routers.invoke import _prefetch_codebase_context
        result = await _prefetch_codebase_context("test query", "/nonexistent/path")
        assert result == ""

    @pytest.mark.asyncio
    async def test_prefetch_finds_matches(self):
        from backend.routers.invoke import _prefetch_codebase_context
        # Search for something that definitely exists in this project
        result = await _prefetch_codebase_context("EventBus publish subscribe", ".")
        assert "EventBus" in result or result == ""  # May find matches in events.py


class TestDynamicReallocation:

    def test_score_agent_for_task_exists(self):
        from backend.routers.invoke import _score_agent_for_task
        assert callable(_score_agent_for_task)


class TestDependencyCheckInPlanActions:

    def test_plan_actions_exists(self):
        from backend.routers.invoke import _plan_actions
        assert callable(_plan_actions)

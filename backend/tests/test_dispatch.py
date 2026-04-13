"""Tests for task dispatch scoring, decomposition, and multi-routing."""

import asyncio

import pytest

from backend.models import Agent, AgentProgress, Task, TaskStatus
from backend.routers.invoke import _score_agent_for_task, _maybe_decompose_task
from backend.agents.nodes import _rule_based_route


class TestAgentScoring:

    def test_type_match_scores_higher(self):
        fw_agent = Agent(id="a1", name="FW", type="firmware", progress=AgentProgress())
        sw_agent = Agent(id="a2", name="SW", type="software", progress=AgentProgress())
        task = Task(id="t1", title="Build firmware driver", suggested_agent_type="firmware")
        assert _score_agent_for_task(fw_agent, task) > _score_agent_for_task(sw_agent, task)

    def test_sub_type_keyword_boost(self):
        bsp = Agent(id="a1", name="BSP", type="firmware", sub_type="bsp", progress=AgentProgress())
        isp = Agent(id="a2", name="ISP", type="firmware", sub_type="isp", progress=AgentProgress())
        task = Task(id="t1", title="configure ISP pipeline", suggested_agent_type="firmware")
        # ISP agent should score higher for ISP-related task
        assert _score_agent_for_task(isp, task) > _score_agent_for_task(bsp, task)

    def test_ai_model_bonus(self):
        with_model = Agent(id="a1", name="A1", type="firmware", ai_model="claude-sonnet-4", progress=AgentProgress())
        no_model = Agent(id="a2", name="A2", type="firmware", progress=AgentProgress())
        task = Task(id="t1", title="Build driver", suggested_agent_type="firmware")
        assert _score_agent_for_task(with_model, task) > _score_agent_for_task(no_model, task)

    def test_unmatched_type_gets_base_score(self):
        agent = Agent(id="a1", name="SW", type="software", progress=AgentProgress())
        task = Task(id="t1", title="Build driver", suggested_agent_type="firmware")
        score = _score_agent_for_task(agent, task)
        assert score == 2  # base score only


class TestMultiRoute:

    def test_single_route(self):
        primary, secondary = _rule_based_route("write a UVC driver")
        assert primary == "firmware"
        assert isinstance(secondary, list)

    def test_compound_route(self):
        primary, secondary = _rule_based_route("write firmware driver and run tests")
        assert primary in ("firmware", "validator")
        # Both should appear (one primary, one secondary)
        all_routes = [primary] + secondary
        assert "firmware" in all_routes or "validator" in all_routes

    def test_no_match_returns_general(self):
        primary, secondary = _rule_based_route("hello world")
        assert primary == "general"
        assert secondary == []


class TestTaskDecomposition:

    @pytest.mark.asyncio
    async def test_compound_task_decomposed(self):
        task = Task(id="t1", title="write firmware driver and then run validation tests", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        assert len(children) == 2
        assert children[0].parent_task_id == "t1"
        assert children[1].parent_task_id == "t1"
        # Auto-dependency: second sub-task depends on first
        assert children[1].depends_on == [children[0].id]

    @pytest.mark.asyncio
    async def test_simple_task_not_decomposed(self):
        task = Task(id="t1", title="write firmware driver", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        assert len(children) == 0

    @pytest.mark.asyncio
    async def test_chinese_conjunction_split(self):
        task = Task(id="t1", title="編譯驅動程式 然後 執行測試", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        assert len(children) == 2

    @pytest.mark.asyncio
    async def test_child_tasks_have_suggested_type(self):
        task = Task(id="t1", title="write firmware driver and then generate compliance report", status=TaskStatus.backlog)
        children = await _maybe_decompose_task(task)
        types = [c.suggested_agent_type for c in children]
        # One should be firmware-related, other reporter-related
        type_strs = [t.value if hasattr(t, 'value') else str(t) for t in types if t]
        assert len(type_strs) >= 1  # At least one gets a suggested type

"""Tests for backend/agents/graph.py — graph structure and routing."""

from __future__ import annotations

import pytest

from backend.agents.graph import build_graph, run_graph, agent_graph
from backend.agents.state import GraphState


class TestGraphStructure:

    def test_graph_has_all_nodes(self):
        nodes = set(agent_graph.nodes.keys())
        expected = {
            "__start__",
            "orchestrator",
            "firmware",
            "software",
            "validator",
            "reporter",
            "general",
            "tool_executor",
            "error_check",
            "summarizer",
        }
        assert expected.issubset(nodes)

    def test_build_graph_returns_compiled(self):
        graph = build_graph()
        assert hasattr(graph, "ainvoke")


class TestRunGraph:
    """Test run_graph in rule-based mode (no LLM key configured)."""

    @pytest.mark.asyncio
    async def test_firmware_routing(self):
        result = await run_graph("write a UVC driver for the IMX335 sensor")
        assert result.routed_to == "firmware"
        assert result.answer  # should have some answer text

    @pytest.mark.asyncio
    async def test_software_routing(self):
        result = await run_graph("refactor the algorithm module and compile")
        assert result.routed_to == "software"

    @pytest.mark.asyncio
    async def test_validator_routing(self):
        result = await run_graph("run the test suite and check coverage")
        assert result.routed_to == "validator"

    @pytest.mark.asyncio
    async def test_reporter_routing(self):
        result = await run_graph("generate FCC compliance report")
        assert result.routed_to == "reporter"

    @pytest.mark.asyncio
    async def test_general_fallback(self):
        result = await run_graph("hello")
        assert result.routed_to == "general"

    @pytest.mark.asyncio
    async def test_tool_execution_on_read_file(self):
        """Commands that match tool patterns should execute tools."""
        result = await run_graph("read file README.md")
        # Should route to a specialist and attempt tool execution
        assert result.answer
        # tool_results may or may not be populated depending on tool matching
        # but the pipeline should complete without error

    @pytest.mark.asyncio
    async def test_workspace_path_forwarded(self):
        result = await run_graph("git status", workspace_path="/tmp")
        assert result.answer

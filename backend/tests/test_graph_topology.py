"""BP.C.2 contract tests for ``backend.graph_topology``.

These tests pin the builder surface only. Runtime graph selection and
the ``OMNISIGHT_TOPOLOGY_MODE`` feature flag are BP.C.3/BP.C.4 scope.
"""

from __future__ import annotations

import pytest

from backend.agents.state import GraphState, ToolCall
from backend import graph_topology as gt


def _nodes(graph: object) -> set[str]:
    return set(getattr(graph, "nodes").keys())


class TestTopologyConstants:
    def test_valid_sizes_are_closed(self) -> None:
        assert gt.VALID_TOPOLOGY_SIZES == ("S", "M", "XL")

    def test_standard_specialists_match_legacy_graph(self) -> None:
        assert gt.STANDARD_SPECIALISTS == (
            "firmware",
            "software",
            "validator",
            "reporter",
            "reviewer",
            "general",
        )


class TestRoutingHelpers:
    def test_m_routes_conversation_before_specialist(self) -> None:
        state = GraphState(is_conversational=True, routed_to="firmware")
        assert gt._route_after_orchestrator(state) == "conversation"

    def test_m_routes_known_specialist(self) -> None:
        state = GraphState(routed_to="firmware")
        assert gt._route_after_orchestrator(state) == "firmware"

    def test_m_unknown_specialist_falls_back_to_general(self) -> None:
        state = GraphState(routed_to="unknown")
        assert gt._route_after_orchestrator(state) == "general"

    def test_s_routes_non_conversation_to_single_track(self) -> None:
        state = GraphState(routed_to="firmware")
        assert gt._route_to_single_track(state) == "single_track"

    def test_check_tool_calls_routes_to_executor_when_present(self) -> None:
        state = GraphState(tool_calls=[ToolCall(tool_name="read_file")])
        assert gt._check_tool_calls(state) == "tool_executor"

    def test_check_tool_calls_routes_to_context_gate_when_empty(self) -> None:
        assert gt._check_tool_calls(GraphState()) == "context_gate"

    def test_xl_retry_maps_legacy_specialist_names_to_domain_nodes(self) -> None:
        state = GraphState(
            routed_to="firmware",
            retry_count=0,
            max_retries=1,
            last_error="tool failed",
        )
        assert gt._retry_xl_matrix(state) == "firmware_domain"


class TestTopologyBuilders:
    def test_s_builder_returns_compiled_single_track_graph(self) -> None:
        graph = gt.build_s_topology()
        nodes = _nodes(graph)
        assert hasattr(graph, "ainvoke")
        assert {
            "__start__",
            "orchestrator",
            "conversation",
            "single_track",
            "tool_executor",
            "error_check",
            "context_gate",
            "summarizer",
        }.issubset(nodes)
        assert "firmware" not in nodes

    def test_m_builder_matches_current_standard_dag_node_surface(self) -> None:
        graph = gt.build_m_topology()
        nodes = _nodes(graph)
        assert hasattr(graph, "ainvoke")
        assert {
            "__start__",
            "orchestrator",
            "conversation",
            "firmware",
            "software",
            "validator",
            "reporter",
            "reviewer",
            "general",
            "tool_executor",
            "error_check",
            "context_gate",
            "summarizer",
        }.issubset(nodes)
        assert "single_track" not in nodes

    def test_xl_builder_returns_fractal_matrix_node_surface(self) -> None:
        graph = gt.build_xl_topology()
        nodes = _nodes(graph)
        assert hasattr(graph, "ainvoke")
        assert {
            "__start__",
            "orchestrator",
            "conversation",
            "portfolio_architect",
            "firmware_domain",
            "software_domain",
            "validation_domain",
            "reporting_domain",
            "integration_gate",
            "tool_executor",
            "error_check",
            "context_gate",
            "summarizer",
        }.issubset(nodes)
        assert "firmware" not in nodes

    @pytest.mark.parametrize(
        ("size", "expected_node"),
        [
            ("S", "single_track"),
            ("M", "firmware"),
            ("XL", "portfolio_architect"),
        ],
    )
    def test_build_topology_dispatches_by_size(self, size: str, expected_node: str) -> None:
        graph = gt.build_topology(size)  # type: ignore[arg-type]
        assert expected_node in _nodes(graph)

    def test_build_topology_rejects_unknown_size(self) -> None:
        with pytest.raises(ValueError, match="unknown topology size"):
            gt.build_topology("L")  # type: ignore[arg-type]

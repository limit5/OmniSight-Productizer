"""BP.C.7 contract suite for S/M/XL topology wiring.

This file intentionally overlaps the high-level BP.C.2/BP.C.3 smoke
tests with a denser topology contract. The goal is to pin the exact
compiled graph surfaces and feature-flag selection rules without
changing production graph construction.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from backend import graph_topology as gt
from backend.agents import graph as agent_graph_module
from backend.agents.state import GraphState, ToolCall


GraphBuilder = Callable[[], object]


TOPOLOGY_BUILDERS: dict[str, GraphBuilder] = {
    "S": gt.build_s_topology,
    "M": gt.build_m_topology,
    "XL": gt.build_xl_topology,
}

S_NODES = {
    "__start__",
    "orchestrator",
    "conversation",
    "single_track",
    "tool_executor",
    "error_check",
    "context_gate",
    "summarizer",
    "__end__",
}

M_NODES = {
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
    "__end__",
}

XL_NODES = {
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
    "__end__",
}

EXPECTED_NODE_SETS = {
    "S": S_NODES,
    "M": M_NODES,
    "XL": XL_NODES,
}

S_EDGES = {
    ("__start__", "orchestrator", False),
    ("orchestrator", "conversation", True),
    ("orchestrator", "single_track", True),
    ("conversation", "context_gate", False),
    ("single_track", "tool_executor", True),
    ("single_track", "context_gate", True),
    ("tool_executor", "error_check", False),
    ("error_check", "single_track", True),
    ("error_check", "context_gate", True),
    ("context_gate", "summarizer", False),
    ("summarizer", "__end__", False),
}

M_EDGES = {
    ("__start__", "orchestrator", False),
    ("orchestrator", "conversation", True),
    ("orchestrator", "firmware", True),
    ("orchestrator", "software", True),
    ("orchestrator", "validator", True),
    ("orchestrator", "reporter", True),
    ("orchestrator", "reviewer", True),
    ("orchestrator", "general", True),
    ("conversation", "context_gate", False),
    ("firmware", "tool_executor", True),
    ("firmware", "context_gate", True),
    ("software", "tool_executor", True),
    ("software", "context_gate", True),
    ("validator", "tool_executor", True),
    ("validator", "context_gate", True),
    ("reporter", "tool_executor", True),
    ("reporter", "context_gate", True),
    ("reviewer", "tool_executor", True),
    ("reviewer", "context_gate", True),
    ("general", "tool_executor", True),
    ("general", "context_gate", True),
    ("tool_executor", "error_check", False),
    ("error_check", "firmware", True),
    ("error_check", "software", True),
    ("error_check", "validator", True),
    ("error_check", "reporter", True),
    ("error_check", "reviewer", True),
    ("error_check", "general", True),
    ("error_check", "context_gate", True),
    ("context_gate", "summarizer", False),
    ("summarizer", "__end__", False),
}

XL_EDGES = {
    ("__start__", "orchestrator", False),
    ("orchestrator", "conversation", True),
    ("orchestrator", "portfolio_architect", True),
    ("conversation", "context_gate", False),
    ("portfolio_architect", "tool_executor", True),
    ("portfolio_architect", "firmware_domain", True),
    ("firmware_domain", "tool_executor", True),
    ("firmware_domain", "software_domain", True),
    ("software_domain", "tool_executor", True),
    ("software_domain", "validation_domain", True),
    ("validation_domain", "tool_executor", True),
    ("validation_domain", "reporting_domain", True),
    ("reporting_domain", "tool_executor", True),
    ("reporting_domain", "integration_gate", True),
    ("integration_gate", "tool_executor", True),
    ("integration_gate", "context_gate", True),
    ("tool_executor", "error_check", False),
    ("error_check", "portfolio_architect", True),
    ("error_check", "firmware_domain", True),
    ("error_check", "software_domain", True),
    ("error_check", "validation_domain", True),
    ("error_check", "reporting_domain", True),
    ("error_check", "integration_gate", True),
    ("error_check", "context_gate", True),
    ("context_gate", "summarizer", False),
    ("summarizer", "__end__", False),
}

EXPECTED_EDGE_SETS = {
    "S": S_EDGES,
    "M": M_EDGES,
    "XL": XL_EDGES,
}

TOPOLOGY_SPECIFIC_NODES = (
    ("S", "single_track"),
    ("M", "firmware"),
    ("M", "software"),
    ("M", "validator"),
    ("M", "reporter"),
    ("M", "reviewer"),
    ("M", "general"),
    ("XL", "portfolio_architect"),
    ("XL", "firmware_domain"),
    ("XL", "software_domain"),
    ("XL", "validation_domain"),
    ("XL", "reporting_domain"),
    ("XL", "integration_gate"),
)

def _compiled_graph(size: str) -> object:
    return TOPOLOGY_BUILDERS[size]()


def _drawable(graph: object) -> object:
    return graph.get_graph()  # type: ignore[attr-defined]


def _node_names(graph: object) -> set[str]:
    return set(_drawable(graph).nodes)


def _edge_triples(graph: object) -> set[tuple[str, str, bool]]:
    return {
        (edge.source, edge.target, edge.conditional)
        for edge in _drawable(graph).edges
    }


class TestSMXLTopologyConstants:
    def test_valid_sizes_are_closed_and_ordered(self) -> None:
        assert gt.VALID_TOPOLOGY_SIZES == ("S", "M", "XL")

    def test_standard_specialists_stay_on_m_topology_only(self) -> None:
        assert gt.STANDARD_SPECIALISTS == (
            "firmware",
            "software",
            "validator",
            "reporter",
            "reviewer",
            "general",
        )


class TestSMXLCompiledNodeSurface:
    @pytest.mark.parametrize("size", gt.VALID_TOPOLOGY_SIZES)
    def test_each_builder_returns_ainvokable_compiled_graph(self, size: str) -> None:
        assert hasattr(_compiled_graph(size), "ainvoke")

    @pytest.mark.parametrize("size", gt.VALID_TOPOLOGY_SIZES)
    def test_each_topology_has_exact_node_set(self, size: str) -> None:
        assert _node_names(_compiled_graph(size)) == EXPECTED_NODE_SETS[size]

    @pytest.mark.parametrize(("size", "node"), TOPOLOGY_SPECIFIC_NODES)
    def test_topology_specific_nodes_are_present(self, size: str, node: str) -> None:
        assert node in _node_names(_compiled_graph(size))


class TestSMXLCompiledEdgeSurface:
    @pytest.mark.parametrize("size", gt.VALID_TOPOLOGY_SIZES)
    def test_each_topology_has_exact_edge_set(self, size: str) -> None:
        assert _edge_triples(_compiled_graph(size)) == EXPECTED_EDGE_SETS[size]

    def test_s_routes_orchestrator_only_to_conversation_or_single_track(self) -> None:
        edges = {
            target
            for source, target, conditional in _edge_triples(gt.build_s_topology())
            if source == "orchestrator" and conditional
        }
        assert edges == {"conversation", "single_track"}

    def test_m_routes_orchestrator_to_conversation_and_specialists(self) -> None:
        edges = {
            target
            for source, target, conditional in _edge_triples(gt.build_m_topology())
            if source == "orchestrator" and conditional
        }
        assert edges == {"conversation", *gt.STANDARD_SPECIALISTS}

    def test_xl_routes_orchestrator_to_conversation_or_matrix_entry(self) -> None:
        edges = {
            target
            for source, target, conditional in _edge_triples(gt.build_xl_topology())
            if source == "orchestrator" and conditional
        }
        assert edges == {"conversation", "portfolio_architect"}

    @pytest.mark.parametrize("specialist", gt.STANDARD_SPECIALISTS)
    def test_m_specialists_can_execute_tools_or_continue(
        self,
        specialist: str,
    ) -> None:
        edges = _edge_triples(gt.build_m_topology())
        assert (specialist, "tool_executor", True) in edges
        assert (specialist, "context_gate", True) in edges

    @pytest.mark.parametrize(
        ("source", "next_node"),
        [
            ("portfolio_architect", "firmware_domain"),
            ("firmware_domain", "software_domain"),
            ("software_domain", "validation_domain"),
            ("validation_domain", "reporting_domain"),
            ("reporting_domain", "integration_gate"),
            ("integration_gate", "context_gate"),
        ],
    )
    def test_xl_matrix_lanes_can_execute_tools_or_continue(
        self,
        source: str,
        next_node: str,
    ) -> None:
        edges = _edge_triples(gt.build_xl_topology())
        assert (source, "tool_executor", True) in edges
        assert (source, next_node, True) in edges


class TestSMXLRoutingHelpers:
    def test_m_router_prefers_conversation_mode(self) -> None:
        state = GraphState(is_conversational=True, routed_to="firmware")
        assert gt._route_after_orchestrator(state) == "conversation"

    @pytest.mark.parametrize("routed_to", gt.STANDARD_SPECIALISTS)
    def test_m_router_accepts_known_specialist(self, routed_to: str) -> None:
        assert gt._route_after_orchestrator(GraphState(routed_to=routed_to)) == routed_to

    @pytest.mark.parametrize("routed_to", ["", "frontend", "unknown", "firmware_domain"])
    def test_m_router_unknown_specialist_falls_back_to_general(
        self,
        routed_to: str,
    ) -> None:
        assert gt._route_after_orchestrator(GraphState(routed_to=routed_to)) == "general"

    def test_s_router_prefers_conversation_mode(self) -> None:
        state = GraphState(is_conversational=True, routed_to="firmware")
        assert gt._route_to_single_track(state) == "conversation"

    @pytest.mark.parametrize(
        "routed_to",
        [*gt.STANDARD_SPECIALISTS, "unknown"],
    )
    def test_s_router_collapses_tasks_to_single_track(self, routed_to: str) -> None:
        assert gt._route_to_single_track(GraphState(routed_to=routed_to)) == "single_track"

    @pytest.mark.parametrize("tool_calls", [[], [ToolCall(tool_name="read_file")]])
    def test_tool_call_router_splits_executor_and_context_paths(
        self,
        tool_calls: list[ToolCall],
    ) -> None:
        expected = "tool_executor" if tool_calls else "context_gate"
        assert gt._check_tool_calls(GraphState(tool_calls=tool_calls)) == expected

    @pytest.mark.parametrize(
        "next_node",
        [
            "firmware_domain",
            "software_domain",
            "validation_domain",
            "reporting_domain",
            "integration_gate",
        ],
    )
    def test_xl_continue_helper_advances_when_no_tool_call(self, next_node: str) -> None:
        edge = gt._check_tool_calls_or_continue(next_node)
        assert edge(GraphState()) == next_node

    @pytest.mark.parametrize(
        "next_node",
        [
            "firmware_domain",
            "software_domain",
            "validation_domain",
            "reporting_domain",
            "integration_gate",
        ],
    )
    def test_xl_continue_helper_executes_tools_first(self, next_node: str) -> None:
        edge = gt._check_tool_calls_or_continue(next_node)
        state = GraphState(tool_calls=[ToolCall(tool_name="read_file")])
        assert edge(state) == "tool_executor"

    @pytest.mark.parametrize("is_conversational", [True, False])
    def test_xl_entry_router_splits_conversation_and_matrix(
        self,
        is_conversational: bool,
    ) -> None:
        state = GraphState(is_conversational=is_conversational)
        expected = "conversation" if is_conversational else "portfolio_architect"
        assert gt._route_to_xl_matrix(state) == expected

    @pytest.mark.parametrize(
        ("routed_to", "expected"),
        [
            ("firmware", "firmware_domain"),
            ("software", "software_domain"),
            ("validator", "validation_domain"),
            ("reporter", "reporting_domain"),
            ("reviewer", "portfolio_architect"),
            ("general", "integration_gate"),
            ("unknown", "context_gate"),
        ],
    )
    def test_xl_retry_maps_legacy_routes_to_matrix_nodes(
        self,
        routed_to: str,
        expected: str,
    ) -> None:
        state = GraphState(routed_to=routed_to, retry_count=0, max_retries=1, last_error="x")
        assert gt._retry_xl_matrix(state) == expected

    def test_xl_retry_loop_breaker_goes_to_context_gate(self) -> None:
        state = GraphState(
            routed_to="firmware",
            last_error="x",
            loop_breaker_triggered=True,
        )
        assert gt._retry_xl_matrix(state) == "context_gate"

    def test_xl_retry_exhausted_goes_to_context_gate(self) -> None:
        state = GraphState(
            routed_to="firmware",
            retry_count=1,
            max_retries=1,
            last_error="x",
        )
        assert gt._retry_xl_matrix(state) == "context_gate"


class TestSMXLDispatchAndFeatureFlag:
    @pytest.mark.parametrize(
        ("size", "expected_node"),
        [
            ("S", "single_track"),
            ("M", "firmware"),
            ("XL", "portfolio_architect"),
        ],
    )
    def test_build_topology_dispatches_to_requested_size(
        self,
        size: str,
        expected_node: str,
    ) -> None:
        assert expected_node in _node_names(gt.build_topology(size))  # type: ignore[arg-type]

    @pytest.mark.parametrize("size", ["", "L", "small", "xl"])
    def test_build_topology_rejects_unknown_size(self, size: str) -> None:
        with pytest.raises(ValueError, match="unknown topology size"):
            gt.build_topology(size)  # type: ignore[arg-type]

    @pytest.mark.parametrize("size", gt.VALID_TOPOLOGY_SIZES)
    def test_legacy_mode_uses_m_graph_for_every_state_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
        size: str,
    ) -> None:
        monkeypatch.setenv(agent_graph_module.TOPOLOGY_MODE_ENV, "legacy")
        graph = agent_graph_module._select_graph_for_state(
            GraphState(size=size),  # type: ignore[arg-type]
        )
        assert graph is agent_graph_module.agent_graph
        assert "firmware" in graph.nodes
        assert "portfolio_architect" not in graph.nodes

    @pytest.mark.parametrize(
        ("size", "expected_node", "excluded_node"),
        [
            ("S", "single_track", "firmware"),
            ("M", "firmware", "single_track"),
            ("XL", "portfolio_architect", "firmware"),
        ],
    )
    def test_smxl_mode_uses_graphstate_size(
        self,
        monkeypatch: pytest.MonkeyPatch,
        size: str,
        expected_node: str,
        excluded_node: str,
    ) -> None:
        monkeypatch.setenv(agent_graph_module.TOPOLOGY_MODE_ENV, "smxl")
        graph = agent_graph_module._select_graph_for_state(
            GraphState(size=size),  # type: ignore[arg-type]
        )
        assert expected_node in graph.nodes
        assert excluded_node not in graph.nodes

    @pytest.mark.parametrize("mode", ["", "smx l", " experimental ", "legacy"])
    def test_non_smxl_modes_fail_closed_to_legacy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        monkeypatch.setenv(agent_graph_module.TOPOLOGY_MODE_ENV, mode)
        graph = agent_graph_module._select_graph_for_state(GraphState(size="XL"))
        assert graph is agent_graph_module.agent_graph

    @pytest.mark.parametrize("mode", ["smxl", "SMXL", " smxl "])
    def test_smxl_mode_is_case_and_whitespace_tolerant(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        monkeypatch.setenv(agent_graph_module.TOPOLOGY_MODE_ENV, mode)
        graph = agent_graph_module._select_graph_for_state(GraphState(size="XL"))
        assert "portfolio_architect" in graph.nodes

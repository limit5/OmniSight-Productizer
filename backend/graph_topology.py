"""BP.C.2 - S/M/XL LangGraph topology builders.

This module owns only topology construction. It does not select a
topology for live traffic, read ``OMNISIGHT_TOPOLOGY_MODE``, mutate
``GraphState``, or change ``backend.agents.graph``; BP.C.3/BP.C.4 own
that wiring.

Module-global audit (SOP Step 1): module constants are immutable tuples
and frozensets. The builders allocate fresh ``StateGraph`` instances per
call and keep no singleton/cache/shared mutable state, so each worker
derives the same topology from code without cross-worker coordination.
"""

from __future__ import annotations

from typing import Callable, Literal

from backend.llm_adapter import END, StateGraph
from backend.agents.state import GraphState
from backend.agents.nodes import (
    _should_retry,
    context_compression_gate,
    conversation_node,
    error_check_node,
    firmware_node,
    general_node,
    orchestrator_node,
    reporter_node,
    reviewer_node,
    software_node,
    summarizer_node,
    tool_executor_node,
    validator_node,
)

TopologySize = Literal["S", "M", "XL"]

VALID_TOPOLOGY_SIZES: tuple[str, ...] = ("S", "M", "XL")
STANDARD_SPECIALISTS: tuple[str, ...] = (
    "firmware",
    "software",
    "validator",
    "reporter",
    "reviewer",
    "general",
)
_VALID_SPECIALISTS = frozenset(STANDARD_SPECIALISTS)


def _route_after_orchestrator(state: GraphState) -> str:
    """Conditional edge: conversation mode or specialist routing."""
    if state.is_conversational:
        return "conversation"
    return state.routed_to if state.routed_to in _VALID_SPECIALISTS else "general"


def _route_to_single_track(state: GraphState) -> str:
    """S topology edge: conversation mode or the lightweight lane."""
    if state.is_conversational:
        return "conversation"
    return "single_track"


def _check_tool_calls(state: GraphState) -> str:
    """After an agent node runs, check if it requested tool calls."""
    if state.tool_calls:
        return "tool_executor"
    return "context_gate"


def _check_tool_calls_or_continue(next_node: str) -> Callable[[GraphState], str]:
    """Return a conditional edge that either executes tools or advances."""

    def _edge(state: GraphState) -> str:
        if state.tool_calls:
            return "tool_executor"
        return next_node

    return _edge


def build_s_topology() -> StateGraph:
    """Build the S-size single-track topology.

    S keeps the orchestration gate and audit path, then collapses task
    execution into one lightweight lane before context compression and
    summarization.
    """
    builder = StateGraph(GraphState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("conversation", conversation_node)
    builder.add_node("single_track", general_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("error_check", error_check_node)
    builder.add_node("context_gate", context_compression_gate)
    builder.add_node("summarizer", summarizer_node)

    builder.set_entry_point("orchestrator")
    builder.add_conditional_edges(
        "orchestrator",
        _route_to_single_track,
        {
            "conversation": "conversation",
            "single_track": "single_track",
        },
    )
    builder.add_edge("conversation", "context_gate")
    builder.add_conditional_edges(
        "single_track",
        _check_tool_calls,
        {
            "tool_executor": "tool_executor",
            "context_gate": "context_gate",
        },
    )
    builder.add_edge("tool_executor", "error_check")
    builder.add_conditional_edges(
        "error_check",
        _should_retry,
        {
            "firmware": "single_track",
            "software": "single_track",
            "validator": "single_track",
            "reporter": "single_track",
            "reviewer": "single_track",
            "general": "single_track",
            "summarizer": "context_gate",
        },
    )
    builder.add_edge("context_gate", "summarizer")
    builder.add_edge("summarizer", END)

    return builder.compile()


def build_m_topology() -> StateGraph:
    """Build the M-size standard DAG topology.

    This mirrors the current legacy graph shape in
    ``backend.agents.graph.build_graph`` so default ``GraphState.size ==
    "M"`` remains the compatibility path when BP.C.3 wires selection in.
    """
    builder = StateGraph(GraphState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("firmware", firmware_node)
    builder.add_node("software", software_node)
    builder.add_node("validator", validator_node)
    builder.add_node("reporter", reporter_node)
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("general", general_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("error_check", error_check_node)
    builder.add_node("conversation", conversation_node)
    builder.add_node("context_gate", context_compression_gate)
    builder.add_node("summarizer", summarizer_node)

    builder.set_entry_point("orchestrator")
    builder.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {
            "conversation": "conversation",
            "firmware": "firmware",
            "software": "software",
            "validator": "validator",
            "reporter": "reporter",
            "reviewer": "reviewer",
            "general": "general",
        },
    )
    builder.add_edge("conversation", "context_gate")

    for specialist in STANDARD_SPECIALISTS:
        builder.add_conditional_edges(
            specialist,
            _check_tool_calls,
            {
                "tool_executor": "tool_executor",
                "context_gate": "context_gate",
            },
        )

    builder.add_edge("tool_executor", "error_check")
    builder.add_conditional_edges(
        "error_check",
        _should_retry,
        {
            "firmware": "firmware",
            "software": "software",
            "validator": "validator",
            "reporter": "reporter",
            "reviewer": "reviewer",
            "general": "general",
            "summarizer": "context_gate",
        },
    )
    builder.add_edge("context_gate", "summarizer")
    builder.add_edge("summarizer", END)

    return builder.compile()


def build_xl_topology() -> StateGraph:
    """Build the XL-size fractal-matrix topology.

    XL keeps the standard tool/error loop but expands the main lane into
    a portfolio review plus firmware/software/validation/reporting domain
    passes before summarization. BP.C.3 can later decide when this graph
    is selected; BP.C.2 only makes the graph shape available.
    """
    builder = StateGraph(GraphState)

    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("conversation", conversation_node)
    builder.add_node("portfolio_architect", reviewer_node)
    builder.add_node("firmware_domain", firmware_node)
    builder.add_node("software_domain", software_node)
    builder.add_node("validation_domain", validator_node)
    builder.add_node("reporting_domain", reporter_node)
    builder.add_node("integration_gate", general_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("error_check", error_check_node)
    builder.add_node("context_gate", context_compression_gate)
    builder.add_node("summarizer", summarizer_node)

    builder.set_entry_point("orchestrator")
    builder.add_conditional_edges(
        "orchestrator",
        _route_to_xl_matrix,
        {
            "conversation": "conversation",
            "portfolio_architect": "portfolio_architect",
        },
    )
    builder.add_edge("conversation", "context_gate")
    builder.add_conditional_edges(
        "portfolio_architect",
        _check_tool_calls_or_continue("firmware_domain"),
        {
            "tool_executor": "tool_executor",
            "firmware_domain": "firmware_domain",
        },
    )
    builder.add_conditional_edges(
        "firmware_domain",
        _check_tool_calls_or_continue("software_domain"),
        {
            "tool_executor": "tool_executor",
            "software_domain": "software_domain",
        },
    )
    builder.add_conditional_edges(
        "software_domain",
        _check_tool_calls_or_continue("validation_domain"),
        {
            "tool_executor": "tool_executor",
            "validation_domain": "validation_domain",
        },
    )
    builder.add_conditional_edges(
        "validation_domain",
        _check_tool_calls_or_continue("reporting_domain"),
        {
            "tool_executor": "tool_executor",
            "reporting_domain": "reporting_domain",
        },
    )
    builder.add_conditional_edges(
        "reporting_domain",
        _check_tool_calls_or_continue("integration_gate"),
        {
            "tool_executor": "tool_executor",
            "integration_gate": "integration_gate",
        },
    )
    builder.add_conditional_edges(
        "integration_gate",
        _check_tool_calls,
        {
            "tool_executor": "tool_executor",
            "context_gate": "context_gate",
        },
    )
    builder.add_edge("tool_executor", "error_check")
    builder.add_conditional_edges(
        "error_check",
        _retry_xl_matrix,
        {
            "portfolio_architect": "portfolio_architect",
            "firmware_domain": "firmware_domain",
            "software_domain": "software_domain",
            "validation_domain": "validation_domain",
            "reporting_domain": "reporting_domain",
            "integration_gate": "integration_gate",
            "context_gate": "context_gate",
        },
    )
    builder.add_edge("context_gate", "summarizer")
    builder.add_edge("summarizer", END)

    return builder.compile()


def build_topology(size: TopologySize) -> StateGraph:
    """Dispatch to the S/M/XL topology builder for ``size``."""
    if size == "S":
        return build_s_topology()
    if size == "M":
        return build_m_topology()
    if size == "XL":
        return build_xl_topology()
    raise ValueError(f"unknown topology size: {size!r}")


def _route_to_xl_matrix(state: GraphState) -> str:
    if state.is_conversational:
        return "conversation"
    return "portfolio_architect"


def _retry_xl_matrix(state: GraphState) -> str:
    route = _should_retry(state)
    return {
        "firmware": "firmware_domain",
        "software": "software_domain",
        "validator": "validation_domain",
        "reporter": "reporting_domain",
        "reviewer": "portfolio_architect",
        "general": "integration_gate",
        "summarizer": "context_gate",
    }.get(route, "context_gate")

"""LangGraph topology for the OmniSight multi-agent system.

Graph structure (with tool calling):

    ┌──────────┐
    │  START   │
    └────┬─────┘
         ▼
    ┌──────────┐
    │Orchestrat│  ← routes based on intent
    └────┬─────┘
         ▼
    ┌──────────┐
    │  Router  │  ← conditional edge
    └─┬──┬──┬──┘
      │  │  │
      ▼  ▼  ▼
     FW SW VA RE GEN  ← specialist nodes (may request tools)
      │  │  │  │  │
      └──┴──┴──┴──┘
         ▼
    ┌──────────┐     ┌──────────┐
    │  Check   │────►│  Tool    │
    │ToolCalls │     │ Executor │
    └────┬─────┘     └────┬─────┘
         │                │
         │◄───────────────┘
         ▼
    ┌──────────┐
    │Summarizer│  ← synthesize tool results into answer
    └────┬─────┘
         ▼
    ┌──────────┐
    │   END    │
    └──────────┘
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage

from backend.agents.state import GraphState
from backend.agents.nodes import (
    orchestrator_node,
    firmware_node,
    software_node,
    validator_node,
    reporter_node,
    general_node,
    tool_executor_node,
    summarizer_node,
)


def _route_to_specialist(state: GraphState) -> str:
    """Conditional edge: pick the specialist based on orchestrator's routing."""
    return state.routed_to


def _check_tool_calls(state: GraphState) -> str:
    """After a specialist runs, check if it requested tool calls."""
    if state.tool_calls:
        return "tool_executor"
    return "summarizer"


def build_graph() -> StateGraph:
    """Construct and compile the agent topology graph."""
    builder = StateGraph(GraphState)

    # ── Nodes ──
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("firmware", firmware_node)
    builder.add_node("software", software_node)
    builder.add_node("validator", validator_node)
    builder.add_node("reporter", reporter_node)
    builder.add_node("general", general_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("summarizer", summarizer_node)

    # ── Edges ──

    # Entry
    builder.set_entry_point("orchestrator")

    # Orchestrator → specialist (conditional)
    builder.add_conditional_edges(
        "orchestrator",
        _route_to_specialist,
        {
            "firmware": "firmware",
            "software": "software",
            "validator": "validator",
            "reporter": "reporter",
            "general": "general",
        },
    )

    # All specialists → check if tools are needed (conditional)
    for specialist in ("firmware", "software", "validator", "reporter", "general"):
        builder.add_conditional_edges(
            specialist,
            _check_tool_calls,
            {
                "tool_executor": "tool_executor",
                "summarizer": "summarizer",
            },
        )

    # Tool executor → summarizer (tools done, produce final answer)
    builder.add_edge("tool_executor", "summarizer")

    # Summarizer → END
    builder.add_edge("summarizer", END)

    return builder.compile()


# Singleton compiled graph
agent_graph = build_graph()


async def run_graph(user_command: str, workspace_path: str | None = None) -> GraphState:
    """Execute the full agent pipeline for a user command.

    Args:
        user_command: The user's instruction.
        workspace_path: If set, tools will operate in this isolated workspace.
    """
    initial_state = GraphState(
        user_command=user_command,
        messages=[HumanMessage(content=user_command)],
        workspace_path=workspace_path,
    )
    result = await agent_graph.ainvoke(initial_state)
    if isinstance(result, dict):
        return GraphState(**result)
    return result

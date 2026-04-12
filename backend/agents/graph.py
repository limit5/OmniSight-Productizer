"""LangGraph topology for the OmniSight multi-agent system.

Graph structure (with tool calling and self-healing loop):

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
         │           ┌────▼─────┐
         │           │  Error   │  ← self-healing gate
         │           │  Check   │
         │           └─┬──────┬─┘
         │     retry ◄─┘      └─► no error / retries exhausted
         │       │
         │  (back to specialist)
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
    conversation_node,
    firmware_node,
    software_node,
    validator_node,
    reporter_node,
    reviewer_node,
    general_node,
    tool_executor_node,
    error_check_node,
    _should_retry,
    summarizer_node,
)


_VALID_SPECIALISTS = {"firmware", "software", "validator", "reporter", "reviewer", "general"}


def _route_after_orchestrator(state: GraphState) -> str:
    """Conditional edge: conversation mode or specialist routing."""
    if state.is_conversational:
        return "conversation"
    return state.routed_to if state.routed_to in _VALID_SPECIALISTS else "general"


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
    builder.add_node("reviewer", reviewer_node)
    builder.add_node("general", general_node)
    builder.add_node("tool_executor", tool_executor_node)
    builder.add_node("error_check", error_check_node)
    builder.add_node("conversation", conversation_node)
    builder.add_node("summarizer", summarizer_node)

    # ── Edges ──

    # Entry
    builder.set_entry_point("orchestrator")

    # Orchestrator → conversation or specialist (conditional)
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

    # Conversation → summarizer (direct, no tools)
    builder.add_edge("conversation", "summarizer")

    # All specialists → check if tools are needed (conditional)
    for specialist in ("firmware", "software", "validator", "reporter", "reviewer", "general"):
        builder.add_conditional_edges(
            specialist,
            _check_tool_calls,
            {
                "tool_executor": "tool_executor",
                "summarizer": "summarizer",
            },
        )

    # Tool executor → error_check (self-healing gate)
    builder.add_edge("tool_executor", "error_check")

    # Error check → retry specialist or proceed to summarizer
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
            "summarizer": "summarizer",
        },
    )

    # Summarizer → END
    builder.add_edge("summarizer", END)

    return builder.compile()


# Singleton compiled graph
agent_graph = build_graph()


GRAPH_TIMEOUT = 300  # 5 minutes max per graph execution


async def run_graph(
    user_command: str,
    workspace_path: str | None = None,
    model_name: str = "",
    agent_sub_type: str = "",
    handoff_context: str = "",
    task_skill_context: str = "",
    task_id: str | None = None,
) -> GraphState:
    """Execute the full agent pipeline for a user command.

    Args:
        user_command: The user's instruction.
        workspace_path: If set, tools will operate in this isolated workspace.
        model_name: LLM model name (for model-specific prompt rules).
        agent_sub_type: Role sub-type (for role-specific skill loading).
        handoff_context: Previous task handoff content (injected into prompt).
        task_skill_context: Anthropic SKILL.md content for task-specific guidance.
        task_id: Associated task ID for debug finding tracking.
    """
    import asyncio

    initial_state = GraphState(
        user_command=user_command,
        messages=[HumanMessage(content=user_command)],
        workspace_path=workspace_path,
        model_name=model_name,
        task_id=task_id,
        agent_sub_type=agent_sub_type,
        handoff_context=handoff_context,
        task_skill_context=task_skill_context,
    )
    try:
        result = await asyncio.wait_for(
            agent_graph.ainvoke(initial_state),
            timeout=GRAPH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return GraphState(
            user_command=user_command,
            messages=initial_state.messages,
            workspace_path=workspace_path,
            model_name=model_name,
            task_id=task_id,
            agent_sub_type=agent_sub_type,
            handoff_context=handoff_context,
            task_skill_context=task_skill_context,
            answer=f"[TIMEOUT] Graph execution exceeded {GRAPH_TIMEOUT}s",
            last_error="Graph execution timeout",
        )
    if isinstance(result, dict):
        return GraphState(**result)
    return result

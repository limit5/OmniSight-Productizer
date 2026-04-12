"""Shared state schema for the LangGraph agent topology."""

from __future__ import annotations

from typing import Annotated, Literal

from langgraph.graph import add_messages
from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage


class AgentAction(BaseModel):
    """An action an agent wants the system to perform."""
    type: Literal["spawn_agent", "assign_task", "update_status", "execute_command", "report"]
    agent_type: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    detail: str = ""


class ToolCall(BaseModel):
    """A tool invocation requested by an agent node."""
    tool_name: str
    arguments: dict = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The result of a tool execution."""
    tool_name: str
    output: str
    success: bool = True


class GraphState(BaseModel):
    """Top-level state flowing through the LangGraph pipeline.

    `messages` uses the LangGraph `add_messages` reducer so nodes can
    simply append without overwriting the full list.
    """
    # Conversation history — automatically merged by LangGraph
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)

    # The original user command (kept for easy reference)
    user_command: str = ""
    task_id: str | None = None  # Associated task ID (for debug findings + artifact tracking)

    # Which specialist should handle the request (set by the router)
    routed_to: str = "general"
    secondary_routes: list[str] = Field(default_factory=list)

    # Tool calling
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    # Actions the pipeline wants the API layer to execute
    actions: list[AgentAction] = Field(default_factory=list)

    # Isolated workspace path (set when agent has a provisioned workspace)
    workspace_path: str | None = None

    # Per-agent model and role context
    model_name: str = ""
    agent_sub_type: str = ""
    handoff_context: str = ""
    task_skill_context: str = ""  # Anthropic SKILL.md content for task-specific guidance
    is_conversational: bool = False  # True = conversation mode (no tools), False = task execution

    # Gerrit Code Review context
    gerrit_change_id: str = ""
    gerrit_commit: str = ""

    # Self-healing loop: retry tracking + loop detection
    # Note: error_history uses LangGraph default (REPLACE, not append).
    # This is intentional — each error_check_node returns the full accumulated list.
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    error_history: list[str] = Field(default_factory=list)
    same_error_count: int = 0
    loop_breaker_triggered: bool = False
    rtk_bypass: bool = False  # When True, skip output compression (fallback for debug)

    # Final answer text to return to the frontend
    answer: str = ""

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

    # Which specialist should handle the request (set by the router)
    routed_to: Literal["firmware", "software", "validator", "reporter", "general"] = "general"

    # Tool calling
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    # Actions the pipeline wants the API layer to execute
    actions: list[AgentAction] = Field(default_factory=list)

    # Isolated workspace path (set when agent has a provisioned workspace)
    workspace_path: str | None = None

    # Self-healing loop: retry tracking
    retry_count: int = 0
    max_retries: int = 2
    last_error: str = ""

    # Final answer text to return to the frontend
    answer: str = ""

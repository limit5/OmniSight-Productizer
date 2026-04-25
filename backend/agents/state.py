"""Shared state schema for the LangGraph agent topology."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from backend.llm_adapter import BaseMessage, add_messages


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

    # R0 (#306): sandbox tier controlling PEP whitelist (t1 / t2 / t3).
    # Defaults to t1 — the most restrictive — when the workflow layer
    # hasn't set it explicitly. Unknown values collapse to t1.
    sandbox_tier: str = "t1"

    # Per-agent model and role context
    model_name: str = ""
    agent_sub_type: str = ""
    handoff_context: str = ""
    task_skill_context: str = ""  # Anthropic SKILL.md content for task-specific guidance
    is_conversational: bool = False  # True = conversation mode (no tools), False = task execution

    # R20 Phase 0 (2026-04-25): role of the user driving this graph run.
    # Threaded into ``conversation_node``'s RAG retrieval so the
    # classification gate filters by this role. Defaults to ``operator``
    # — the most common case — because anonymous LangGraph runs only
    # happen in admin-driven tooling. Values: anonymous / operator / admin.
    user_role: str = "operator"

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
    # Auto-fix loop guard (Batch 4 H8): track which permission categories
    # have been auto-fixed already this graph run so we don't loop forever.
    auto_fix_history: list[str] = Field(default_factory=list)
    same_error_count: int = 0
    loop_breaker_triggered: bool = False
    rtk_bypass: bool = False  # When True, skip output compression (fallback for debug)

    # Verification loop: separate from retry_count (which tracks tool execution errors)
    # This tracks "generate → verify → [FAIL] → fix → verify" iterations
    verification_loop_iteration: int = 0
    max_verification_iterations: int = 2
    last_verification_failure: str = ""

    # Final answer text to return to the frontend
    answer: str = ""

    # Phase 67-E follow-up — platform tags used by the sandbox RAG
    # pre-fetch to enforce the SDK-version hard-lock. Empty strings
    # map to "unknown, be permissive" in prefetch_for_sandbox_error.
    # Populated by the workflow layer from the active platform config
    # (get_platform_config) when a task is routed to a platform-aware
    # specialist.
    soc_vendor: str = ""
    sdk_version: str = ""

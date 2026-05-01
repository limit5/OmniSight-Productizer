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

    # BP.C.5 (Blueprint v2 Phase C — T-shirt Gateway + S/M/XL Topology):
    # T-shirt size assigned by the upstream sizer (`backend/t_shirt_sizer.py`,
    # BP.C.1) and consumed by the topology builder (`backend/graph_topology.py`,
    # BP.C.2) to pick S (single-track) / M (standard DAG) / XL (fractal matrix).
    #
    # Default = "M" because:
    #   1. M maps to the *current* legacy LangGraph topology (standard DAG),
    #      so any code path that constructs ``GraphState()`` without going
    #      through the new sizer pre-stage (BP.C.4) keeps the legacy
    #      behaviour byte-for-byte. This is the safe default while the
    #      ``OMNISIGHT_TOPOLOGY_MODE=legacy|smxl`` feature flag is still
    #      gating the rollout.
    #   2. Treating "M" as the canonical mid-point matches the design doc
    #      (`docs/design/blueprint-v2-implementation-plan.md` §"Phase C",
    #      §"Appendix B TaskTemplate.size").
    #
    # Module-global audit (per SOP §"Module-global state"): this is a
    # per-instance Pydantic field, no module-level mutable state. Each
    # GraphState flows through one graph run; multi-worker processes
    # never share GraphState instances (LangGraph state is run-scoped).
    # No cross-worker coordination required.
    size: Literal["S", "M", "XL"] = "M"

    # Per-agent model and role context
    model_name: str = ""
    agent_sub_type: str = ""
    handoff_context: str = ""
    task_skill_context: str = ""  # Anthropic SKILL.md content for task-specific guidance
    # W11.10 (#XXX): pre-rendered clone-spec context block produced by
    # ``backend.web.clone_spec_context.build_clone_spec_context``. Empty
    # string for non-W11 graph runs. Threaded through
    # ``_specialist_node_factory`` into ``build_system_prompt`` so the
    # frontend agent role prompt sees the rewritten outline + W11
    # invariants without the LLM ever touching source bytes.
    clone_spec_context: str = ""
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

    # BP.C.5 (2026-04-25) — T-shirt sizing dimension for the S/M/XL
    # topology gateway (Blueprint v2 Phase C). Written by
    # ``backend.t_shirt_sizer`` (BP.C.1) before graph execution and
    # read by ``backend.graph_topology`` (BP.C.2) / ``backend.agents.graph``
    # (BP.C.3) to choose the correct topology builder:
    #
    #   - "S"  → single-track (lightweight sequential pipeline)
    #   - "M"  → standard DAG (current default LangGraph topology)
    #   - "XL" → fractal matrix (heavy multi-agent recursion)
    #
    # Default is ``"M"`` so existing call sites and tests that don't
    # populate this field keep their pre-BP.C behaviour (standard DAG).
    # The dimension is independent of ``ProjectClass`` (BP.C.6 — three-
    # way orthogonal axis: ProjectClass × Target_Triple × T-shirt size)
    # and of ``OMNISIGHT_TOPOLOGY_MODE`` (the legacy/smxl feature flag
    # is read at gateway level, not here).
    #
    # Module-global audit (SOP Step 1): this field lives on the per-
    # request ``GraphState`` instance — no shared singleton, no cross-
    # worker visibility concern; each uvicorn worker constructs its own
    # state object per invocation (answer type 1: not shared because
    # each worker derives identical defaults independently).
    size: Literal["S", "M", "XL"] = "M"

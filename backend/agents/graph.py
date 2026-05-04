"""LangGraph entry point for the OmniSight multi-agent system.

Runtime topology is gated by ``OMNISIGHT_TOPOLOGY_MODE``:

* ``legacy`` (default): always use the M standard DAG for backward
  compatibility.
* ``smxl``: select S / M / XL from the run-scoped ``GraphState.size``.

M graph structure (with tool calling and self-healing loop):

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

import os
from types import MappingProxyType

from backend.llm_adapter import HumanMessage
from backend.agents.state import GraphState
from backend.graph_topology import (
    TopologySize,
    VALID_TOPOLOGY_SIZES,
    build_topology,
    _check_tool_calls,
    _route_after_orchestrator,
)
from backend.security.llm_firewall import (
    BLOCKED_REFUSAL_MESSAGE,
    FirewallResult,
    enforce_input,
)


TOPOLOGY_MODE_ENV = "OMNISIGHT_TOPOLOGY_MODE"
VALID_TOPOLOGY_MODES: tuple[str, ...] = ("legacy", "smxl")


def get_topology_mode() -> str:
    """Return the graph topology feature-flag mode.

    Unknown values fail closed to ``legacy`` so a typo does not activate
    the BP.C S/M/XL runtime path before operators intend it.
    """
    mode = (os.getenv(TOPOLOGY_MODE_ENV) or "legacy").strip().lower()
    if mode in VALID_TOPOLOGY_MODES:
        return mode
    return "legacy"


def build_graph(size: TopologySize = "M"):
    """Construct and compile the requested S/M/XL topology graph."""
    return build_topology(size)


# Singleton compiled graphs.
#
# Module-global / cross-worker audit: the mapping is read-only after import and
# each uvicorn worker compiles the same three graphs from code. No mutable
# cache, singleton mutation, or cross-worker coordination is required.
_TOPOLOGY_GRAPHS = MappingProxyType({
    size: build_graph(size) for size in VALID_TOPOLOGY_SIZES
})

# Backward-compatible alias used by existing imports/tests: legacy == M.
agent_graph = _TOPOLOGY_GRAPHS["M"]


def _select_graph_for_state(state: GraphState):
    """Pick the compiled graph for a run-scoped ``GraphState``."""
    if get_topology_mode() != "smxl":
        return agent_graph
    return _TOPOLOGY_GRAPHS[state.size]


GRAPH_TIMEOUT = 300  # 5 minutes max per graph execution


async def _enforce_user_facing_firewall(
    user_command: str,
    *,
    firewall_result: FirewallResult | None = None,
    task_id: str | None = None,
):
    """Run KS.4.12's user-facing firewall before specialist routing.

    Module-global / cross-worker audit: this helper stores no mutable state.
    Each worker derives the same guard decision from request text plus the
    configured classifier credentials at call time; internal graph node retries
    do not re-enter this function.
    """
    from backend.config import settings

    try:
        return await enforce_input(
            user_command,
            result=firewall_result,
            api_key=getattr(settings, "anthropic_api_key", "") or None,
            actor="orchestrator_entry",
            entity_id=task_id,
            session_id=task_id,
        )
    except RuntimeError as exc:
        # Fresh dev/test installs often have no firewall classifier key.
        # Keep the entry point callable while production deployments with
        # configured credentials still enforce through Haiku.
        if "ANTHROPIC_API_KEY" not in str(exc):
            raise
        return None


async def run_graph(
    user_command: str,
    workspace_path: str | None = None,
    model_name: str = "",
    agent_sub_type: str = "",
    handoff_context: str = "",
    task_skill_context: str = "",
    task_id: str | None = None,
    soc_vendor: str = "",
    sdk_version: str = "",
    size: TopologySize = "M",
    firewall_result: FirewallResult | None = None,
    firewall_trust: str = "external",
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
        soc_vendor / sdk_version: Phase 67-E follow-up — pass through
            so prefetch_for_sandbox_error can enforce the SDK
            hard-lock. Empty strings keep the gate permissive (the
            non-platform-aware default).
        size: BP.C S/M/XL topology size. Defaults to ``"M"`` so existing
            callers keep the legacy standard DAG until BP.C.4 wires the
            upstream sizer.
        firewall_trust: ``external`` runs the KS.4.12 firewall before
            routing. ``internal`` is reserved for specialist-to-specialist
            traffic that has already passed the user-facing entry guard.
    """
    import asyncio

    firewall = None
    if firewall_trust != "internal":
        firewall = await _enforce_user_facing_firewall(
            user_command,
            firewall_result=firewall_result,
            task_id=task_id,
        )
        if firewall and not firewall.allow_invocation:
            return GraphState(
                user_command=user_command,
                messages=[HumanMessage(content=user_command)],
                workspace_path=workspace_path,
                model_name=model_name,
                task_id=task_id,
                agent_sub_type=agent_sub_type,
                handoff_context=handoff_context,
                task_skill_context=task_skill_context,
                soc_vendor=soc_vendor,
                sdk_version=sdk_version,
                size=size,
                answer=firewall.refusal_message or BLOCKED_REFUSAL_MESSAGE,
                last_error="llm_firewall_blocked",
            )
        if firewall and firewall.system_prompt_warning:
            handoff_context = firewall.apply_system_prompt_warning(
                handoff_context,
            )

    messages = [HumanMessage(content=user_command)]
    initial_state = GraphState(
        user_command=user_command,
        messages=messages,
        workspace_path=workspace_path,
        model_name=model_name,
        task_id=task_id,
        agent_sub_type=agent_sub_type,
        handoff_context=handoff_context,
        task_skill_context=task_skill_context,
        soc_vendor=soc_vendor,
        sdk_version=sdk_version,
        size=size,
    )
    try:
        result = await asyncio.wait_for(
            _select_graph_for_state(initial_state).ainvoke(initial_state),
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
            soc_vendor=soc_vendor,
            sdk_version=sdk_version,
            size=size,
            answer=f"[TIMEOUT] Graph execution exceeded {GRAPH_TIMEOUT}s",
            last_error="Graph execution timeout",
        )
    if isinstance(result, dict):
        return GraphState(**result)
    return result

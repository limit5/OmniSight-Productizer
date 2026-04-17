"""SSE Event payload schemas — typed definitions for all real-time events.

These models document the exact shape of each SSE event's data field.
They are used for:
  1. Schema export (GET /system/sse-schema)
  2. Frontend TypeScript type generation reference
  3. Event validation in tests

The actual emit_* functions in events.py still use dicts for backwards
compatibility, but these schemas are the authoritative contract.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SSEAgentUpdate(BaseModel):
    """agent_update — Agent status change."""
    agent_id: str
    status: str
    thought_chain: str = ""
    timestamp: str = ""


class SSETaskUpdate(BaseModel):
    """task_update — Task status change."""
    task_id: str
    status: str
    assigned_agent_id: Optional[str] = None
    timestamp: str = ""


class SSEToolProgress(BaseModel):
    """tool_progress — Tool execution lifecycle (start/done/error)."""
    tool_name: str
    phase: str  # "start" | "done" | "error"
    output: str = ""
    index: int = 0
    success: Optional[bool] = None
    timestamp: str = ""


class SSEPipeline(BaseModel):
    """pipeline — Graph pipeline phase events."""
    phase: str
    detail: str = ""
    timestamp: str = ""


class SSEWorkspace(BaseModel):
    """workspace — Git worktree lifecycle events."""
    agent_id: str
    action: str  # "provision" | "finalize" | "cleanup"
    detail: str = ""
    timestamp: str = ""


class SSEContainer(BaseModel):
    """container — Docker container lifecycle events."""
    agent_id: str
    action: str  # "start" | "stop" | "exec" | "build"
    detail: str = ""
    timestamp: str = ""


class SSEInvoke(BaseModel):
    """invoke — INVOKE orchestration action events."""
    action_type: str
    detail: str = ""
    timestamp: str = ""


class SSETokenWarning(BaseModel):
    """token_warning — Token budget threshold events."""
    level: str  # "warn" | "downgrade" | "frozen" | "reset" | "all_providers_failed"
    message: str
    usage: float = 0.0
    budget: float = 0.0
    timestamp: str = ""


class SSESimulation(BaseModel):
    """simulation — Dual-track simulation lifecycle events."""
    sim_id: str
    action: str  # "start" | "progress" | "result"
    detail: str = ""
    status: str = ""
    track: str = ""
    module: str = ""
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    timestamp: str = ""


class SSEDebugFinding(BaseModel):
    """debug_finding — Debug blackboard discovery events."""
    id: str
    task_id: str
    agent_id: str
    finding_type: str
    severity: str = "info"
    message: str = ""
    timestamp: str = ""


class SSENotification(BaseModel):
    """notification — Notification dispatch events."""
    id: str
    level: str
    title: str
    message: str = ""
    source: str = ""
    timestamp: str = ""


class SSEArtifactCreated(BaseModel):
    """artifact_created — New artifact generated."""
    id: str
    name: str
    type: str = ""
    task_id: str = ""
    agent_id: str = ""
    size: int = 0


class SSEHeartbeat(BaseModel):
    """heartbeat — Connection keepalive."""
    subscribers: int = 0


# ─── Phase 47: Decision Engine events ───

class SSEModeChanged(BaseModel):
    """mode_changed — OperationMode switched."""
    mode: str
    previous: str
    parallel_cap: int
    timestamp: str = ""


class SSEBudgetTuning(BaseModel):
    """Knobs surfaced by budget_strategy.get_tuning(). Strongly typed so
    the TS side can rely on the shape (R2-#17)."""
    strategy: str = ""
    model_tier: str = ""
    max_retries: int = 0
    downgrade_at: float = 0.0
    freeze_at: float = 0.0
    prefer_parallel: bool = False


class SSEBudgetStrategyChanged(BaseModel):
    """budget_strategy_changed — Budget strategy switched (Phase 47C)."""
    strategy: str
    previous: str
    tuning: SSEBudgetTuning = Field(default_factory=SSEBudgetTuning)
    timestamp: str = ""


# ─── R1 (#307): ChatOps Mirror event ───


class SSEChatOpsMessage(BaseModel):
    """chatops.message — Outbound/inbound ChatOps traffic mirror.

    Emitted by :mod:`backend.chatops_bridge` whenever a message is sent
    via any adapter or a webhook callback is dispatched. Enables the
    ChatOps Mirror Panel (``chatops-mirror.tsx``) to show real-time
    bi-directional chat traffic inside the dashboard.
    """
    id: str
    direction: str                  # "outbound" | "inbound"
    channel: str                    # "discord" | "teams" | "line" | "dashboard"
    ts: float = 0.0
    title: str = ""
    body: str = ""
    author: str = ""
    user_id: str = ""
    kind: str = ""                  # for inbound: "button" | "command" | "message"
    button_id: str = ""
    command: str = ""
    command_args: str = ""
    buttons: list[dict] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    timestamp: str = ""


# ─── R2 (#308): Semantic Entropy Monitor event ───


class SSEAgentEntropy(BaseModel):
    """agent.entropy — Semantic entropy measurement for a running agent.

    Emitted by :mod:`backend.semantic_entropy` every N rounds once the
    rolling window has ≥2 outputs. ``verdict`` is classified via the
    warn / deadlock thresholds; on ``deadlock`` a companion
    ``debug_finding`` of type ``cognitive_deadlock`` is also emitted.
    """
    agent_id: str
    task_id: Optional[str] = None
    entropy_score: float
    threshold_warn: float = 0.5
    threshold_deadlock: float = 0.7
    verdict: str  # "ok" | "warning" | "deadlock"
    window_size: int = 0
    round: int = 0
    timestamp: str = ""


# ─── R0 (#306): PEP Gateway event ───

class SSEPepDecision(BaseModel):
    """pep.decision — Policy Enforcement Point tool-call classification."""
    id: str
    ts: float
    agent_id: str = ""
    tool: str
    command: str = ""
    tier: str = "t1"
    action: str  # "auto_allow" | "hold" | "deny"
    rule: str = ""
    reason: str = ""
    impact_scope: str = ""  # "local" | "prod" | "destructive"
    decision_id: Optional[str] = None
    degraded: bool = False
    timestamp: str = ""


class SSEDecisionOption(BaseModel):
    """One decision option. Strongly typed to catch drift (R2-#17)."""
    id: str
    label: str = ""
    description: str = ""
    is_safe_default: bool = False


class SSEDecision(BaseModel):
    """decision_pending / decision_auto_executed / decision_resolved / decision_undone."""
    id: str
    kind: str
    severity: str
    title: str
    detail: str = ""
    status: str
    options: list[SSEDecisionOption] = Field(default_factory=list)
    default_option_id: Optional[str] = None
    chosen_option_id: Optional[str] = None
    resolver: Optional[str] = None
    created_at: float = 0.0
    deadline_at: Optional[float] = None
    resolved_at: Optional[float] = None
    source: dict = Field(default_factory=dict)  # N12: matches Decision.to_dict()
    timestamp: str = ""


# Registry for schema export
SSE_EVENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "agent_update": SSEAgentUpdate,
    "task_update": SSETaskUpdate,
    "tool_progress": SSEToolProgress,
    "pipeline": SSEPipeline,
    "workspace": SSEWorkspace,
    "container": SSEContainer,
    "invoke": SSEInvoke,
    "token_warning": SSETokenWarning,
    "simulation": SSESimulation,
    "debug_finding": SSEDebugFinding,
    "notification": SSENotification,
    "artifact_created": SSEArtifactCreated,
    "heartbeat": SSEHeartbeat,
    # Phase 47
    "mode_changed": SSEModeChanged,
    "decision_pending": SSEDecision,
    "decision_auto_executed": SSEDecision,
    "decision_resolved": SSEDecision,
    "decision_undone": SSEDecision,
    "budget_strategy_changed": SSEBudgetStrategyChanged,
    # R0 (#306): PEP Gateway
    "pep.decision": SSEPepDecision,
    # R1 (#307): ChatOps Mirror
    "chatops.message": SSEChatOpsMessage,
    # R2 (#308): Semantic Entropy Monitor
    "agent.entropy": SSEAgentEntropy,
}


def get_sse_schema_export() -> dict:
    """Export all SSE event schemas as JSON Schema (for frontend codegen)."""
    return {
        event_type: {
            "description": model.__doc__ or "",
            "schema": model.model_json_schema(),
        }
        for event_type, model in SSE_EVENT_SCHEMAS.items()
    }

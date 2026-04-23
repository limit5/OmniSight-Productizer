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


# ─── R3 (#309): Scratchpad Memory Offload + Auto-Continuation ───


class SSEAgentScratchpadSaved(BaseModel):
    """agent.scratchpad.saved — per-agent persistent-state flush.

    Emitted by :mod:`backend.scratchpad` every 10 ReAct turns, on
    tool-call completion, and on sub-task switch. ``size_bytes`` is the
    encrypted on-disk length, not the plaintext memory cost.
    """
    agent_id: str
    task_id: Optional[str] = None
    turn: int
    size_bytes: int
    sections_count: int
    trigger: str  # "turn_interval" | "tool_done" | "subtask_switch" | "manual"
    subtask: Optional[str] = None
    timestamp: str = ""


class SSEAgentTokenContinuation(BaseModel):
    """agent.token_continuation — one auto-continuation round.

    Emitted when the LLM adapter returns ``stop_reason=max_tokens`` and
    the AutoContinuation helper re-prompts the model. The UI uses this
    to attach an "↩ auto-continued" tag to the most recent agent
    message in the stream.
    """
    agent_id: str
    task_id: Optional[str] = None
    provider: str = "unknown"
    continuation_round: int
    total_rounds: int
    appended_chars: int = 0
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


class SSEHostBaseline(BaseModel):
    """Static host baseline (HOST_BASELINE) — pinned per H1 spec."""
    cpu_cores: int
    mem_total_gb: int
    disk_total_gb: int
    cpu_model: str


class SSEHostSamplePayload(BaseModel):
    """Per-tick whole-host psutil snapshot."""
    cpu_percent: float
    mem_percent: float
    mem_used_gb: float
    mem_total_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    loadavg_1m: float
    loadavg_5m: float
    loadavg_15m: float
    sampled_at: float


class SSEDockerSamplePayload(BaseModel):
    """Per-tick docker-daemon view (count + reservation, with source tag)."""
    container_count: int
    total_mem_reservation_bytes: int
    source: str  # "sdk" | "cli" | "unavailable"
    sampled_at: float


class SSENotificationRead(BaseModel):
    """notification.read — Q.3-SUB-3 (#297) cross-device read-state flip.

    Emitted by :mod:`backend.routers.system` after every successful
    ``mark_notification_read``. Carries the notification ``id`` + the
    owning ``user_id`` so the frontend can (a) decrement its unread
    counter (b) drop the row from its local list. ``broadcast_scope=
    'user'`` — see Q.4 (#298) for the scope-enforcement roadmap;
    until then consumers must self-filter on user identity.
    """
    id: str
    user_id: str
    timestamp: str = ""


class SSEWorkflowUpdated(BaseModel):
    """workflow_updated — Q.3-SUB-1 (#297) cross-device workflow_run push.

    Emitted by :mod:`backend.workflow` after every successful
    ``workflow_runs`` INSERT / UPDATE. ``version`` is the post-bump
    value and matches the ``ETag`` returned from the REST handlers,
    so the frontend can reconcile optimistic-lock state without a
    follow-up ``GET``.  ``broadcast_scope='user'`` — see Q.4 (#298)
    for the scope-enforcement roadmap; until then consumers must
    self-filter on user identity.
    """
    run_id: str
    status: str
    version: int
    kind: Optional[str] = None
    timestamp: str = ""


class SSEPreferencesUpdated(BaseModel):
    """preferences.updated — Q.3-SUB-4 (#297) cross-device user-prefs push.

    Emitted by :mod:`backend.routers.preferences` after a successful
    ``PUT /user-preferences/{key}`` upsert. Carries ``pref_key`` +
    ``value`` + owning ``user_id`` so the frontend can patch the
    matching entry in its cached prefs map without waiting for a
    follow-up poll. ``broadcast_scope='user'`` — see Q.4 (#298) for
    the scope-enforcement roadmap; until then consumers must
    self-filter on user identity.
    """
    pref_key: str
    value: str
    user_id: str
    timestamp: str = ""


class SSEHostMetricsTick(BaseModel):
    """host.metrics.tick — H1 5s whole-host sampling push.

    Mirrors the per-snapshot shape of ``GET /api/v1/host/metrics``'s
    ``current`` field plus a static ``baseline`` and a pre-computed
    ``high_pressure`` flag (loadavg_1m / cpu_cores > 0.9). Carried at the
    ``SAMPLE_INTERVAL_S`` cadence (5 s) so subscribers can render live
    sparklines without polling the REST endpoint.
    """
    host: SSEHostSamplePayload
    docker: SSEDockerSamplePayload
    baseline: SSEHostBaseline
    high_pressure: bool
    sampled_at: float
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
    # R3 (#309): Scratchpad Memory Offload + Auto-Continuation
    "agent.scratchpad.saved": SSEAgentScratchpadSaved,
    "agent.token_continuation": SSEAgentTokenContinuation,
    # H1: whole-host metrics sampling tick (5 s cadence)
    "host.metrics.tick": SSEHostMetricsTick,
    # Q.3-SUB-1 (#297): cross-device workflow_run state push
    "workflow_updated": SSEWorkflowUpdated,
    # Q.3-SUB-3 (#297): cross-device notification read-state push
    "notification.read": SSENotificationRead,
    # Q.3-SUB-4 (#297): cross-device user-preferences push
    "preferences.updated": SSEPreferencesUpdated,
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

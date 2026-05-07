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


class SSEProviderQuotaUpdated(BaseModel):
    """provider.quota.updated — subscription-provider quota snapshot.

    Emitted by :mod:`backend.agents.provider_quota_tracker` after provider
    usage or reset mutations commit. The dashboard's ``useEngine()`` SSE
    stream can replace cached quota rows with this payload instead of
    polling after each subscription dispatch.
    """
    provider: str
    rolling_5h_tokens: int = 0
    weekly_tokens: int = 0
    cap_5h_tokens: int = 0
    cap_weekly_tokens: int = 0
    remaining_5h_tokens: int = 0
    remaining_weekly_tokens: int = 0
    remaining_5h_quota_ratio: float = 0.0
    remaining_weekly_quota_ratio: float = 0.0
    circuit_state: str = "closed"
    last_reset_at: Optional[str] = None
    last_cap_hit_at: Optional[str] = None
    reason: str = ""
    scopes: list[str] = Field(default_factory=list)
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
    guild_id: str = ""
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


class SSEChatMessageSuggestion(BaseModel):
    """Nested AISuggestion payload for chat.message events.

    Shape mirrors :class:`backend.models.AISuggestion` but stays
    optional on the SSE event (most messages have no suggestion).
    """
    id: str
    type: str
    title: str
    description: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    priority: str = "medium"
    status: str = "pending"


class SSEChatMessage(BaseModel):
    """chat.message — Q.3-SUB-6 (#297) cross-device chat-history push.

    Emitted by :mod:`backend.routers.chat` after each successful
    ``chat_messages`` INSERT. Carries the persisted id / role /
    content / user_id so a second device owned by the same user
    appends the line immediately; the on-mount ``GET /chat/history``
    fetch continues to seed the initial snapshot.

    ``broadcast_scope='user'`` — see Q.4 (#298) for the scope-
    enforcement roadmap; until then consumers must self-filter on
    ``user_id``. Streaming token-by-token stays bound to the
    originator session and does NOT use this event type (it rides
    the ``/chat/stream`` HTTP-body SSE directly; this helper only
    publishes the finalised message).
    """
    id: str
    user_id: str
    role: str  # "user" | "orchestrator" | "system"
    content: str
    ts: str = ""
    suggestion: Optional[SSEChatMessageSuggestion] = None
    timestamp: str = ""


class SSEIntegrationSettingsUpdated(BaseModel):
    """integration.settings.updated — Q.3-SUB-5 (#297) cross-device
    non-LLM integration-settings push.

    Emitted by :mod:`backend.routers.integration` after a successful
    ``PUT /runtime/settings`` that touched any field outside the LLM
    family (Gerrit / JIRA / GitHub / GitLab / Slack / PagerDuty /
    webhooks / CI / Docker). The LLM subset still piggy-backs on the
    existing ``invoke('provider_switch')`` event — they were already
    wired and working. ``fields_changed`` is the raw applied key list
    from the PUT handler; the frontend matches it against its own
    per-tab key map so the backend doesn't have to second-guess which
    modal tab to repaint. ``broadcast_scope='user'`` — see Q.4 (#298)
    for the scope-enforcement roadmap; until then consumers must
    self-filter on user identity.
    """
    fields_changed: list[str] = Field(default_factory=list)
    timestamp: str = ""


class SSETurnToolStats(BaseModel):
    """turn_tool_stats — ZZ.A3 #303-3 per-turn tool-execution summary.

    Emitted once from the summarizer node at the very end of every
    :func:`backend.agents.graph.run_graph` execution. Aggregates the
    ``GraphState.tool_results`` list into a turn-level snapshot the UI
    can render as "tools 5 / failed 1" in the TokenUsageStats card.

    ``tool_failure_count`` counts entries where the tool executor marked
    ``result.success == False`` — i.e. the LangGraph shape's equivalent
    of the spec's ``result.error is not None``. ``failed_tools`` is the
    ordered list of tool names that failed (with duplicates preserved so
    a repeated failure of the same tool shows up as a higher count in
    the badge).
    """
    agent_type: str = ""
    task_id: Optional[str] = None
    tool_call_count: int = 0
    tool_failure_count: int = 0
    failed_tools: list[str] = Field(default_factory=list)
    timestamp: str = ""


class SSETurnMetrics(BaseModel):
    """turn_metrics — ZZ.A2 #303-2 per-turn LLM context-usage snapshot.

    Emitted by :class:`backend.agents.llm.TokenTrackingCallback` at the end
    of every LLM turn. The payload is a *per-turn* snapshot (this turn's
    tokens only, not lifetime totals) so the frontend can render a live
    progress bar + warning-icon strip against the provider's advertised
    context window. ``context_limit`` comes from
    :func:`backend.context_limits.get_context_limit` — ``None`` means the
    YAML has no entry for the provider/model pair (Ollama local models,
    OpenRouter pass-through routes, unknown providers). When
    ``context_limit`` is ``None`` the ``context_usage_pct`` degrades to
    ``None`` too; the UI must render ``—`` rather than a fake 0%
    (preserves the NULL-vs-genuine-zero contract ZZ.A1 established for
    prompt-cache fields).
    """
    provider: Optional[str] = None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_used: int = 0
    context_limit: Optional[int] = None
    context_usage_pct: Optional[float] = None
    latency_ms: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    timestamp: str = ""


class SSETurnMessagePart(BaseModel):
    """One LLM message inside a ``turn.complete`` payload.

    ZZ.B1 #304-1 checkbox 3: the prompt (system / user / tool) + the
    assistant response captured at the end of an LLM turn. ``tokens``
    is the per-message prompt-token contribution when the provider
    exposes it; ``None`` when unattributable so the UI can distinguish
    "zero tokens" from "don't know".
    """
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tokens: Optional[int] = None
    tool_name: Optional[str] = None


class SSETurnToolCall(BaseModel):
    """One tool invocation inside a ``turn.complete`` payload.

    ZZ.B1 #304-1 checkbox 3: args / result / duration_ms are optional
    because the summarizer path only knows ``{tool_name, success}``
    today. Populated when the tool executor carries the extra metadata.
    """
    name: str
    success: bool = True
    args: Optional[dict] = None
    result: Optional[str] = None
    duration_ms: Optional[int] = None


class SSETurnComplete(BaseModel):
    """turn.complete — ZZ.B1 #304-1 checkbox 3 per-turn terminal event.

    Emitted once per LLM turn from
    :class:`backend.agents.llm.TokenTrackingCallback` after
    :func:`emit_turn_metrics`. Carries the richer payload the
    ``<TurnDetailDrawer>`` needs (messages, per-tool-call detail,
    backend-authoritative cost) so the frontend can replace its
    interim frontend-estimated cost + "Waiting for turn.complete"
    placeholders.

    Persisted to ``event_log`` so ``GET /runtime/turns`` can hand
    the last N turns to a newly-connected client instead of waiting
    for the next live emit to populate the ring buffer.
    """
    turn_id: str
    provider: Optional[str] = None
    model: str
    agent_type: Optional[str] = None
    task_id: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_used: int = 0
    context_limit: Optional[int] = None
    context_usage_pct: Optional[float] = None
    latency_ms: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: Optional[float] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    summary: Optional[str] = None
    messages: list[SSETurnMessagePart] = Field(default_factory=list)
    tool_calls: list[SSETurnToolCall] = Field(default_factory=list)
    tool_call_count: int = 0
    tool_failure_count: int = 0
    timestamp: str = ""


class SSESessionTitled(BaseModel):
    """session.titled — ZZ.B2 #304-2 checkbox 1 LLM-generated chat title.

    Fires once per session after the background task composes
    ``metadata.auto_title`` from the first 3 condensed user turns.
    The sidebar subscribes and relabels the matching row in-place.

    ``user_id`` is carried so the frontend can self-filter even while
    ``broadcast_scope='user'`` is advisory. ``source`` is ``"auto"``
    today; reserved values ``"user"`` (operator rename) are for later.
    """
    session_id: str
    user_id: str
    title: str
    source: str = "auto"
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
    "provider.quota.updated": SSEProviderQuotaUpdated,
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
    # ZZ.A2 #303-2: per-turn context-usage snapshot for TokenUsageStats card
    "turn_metrics": SSETurnMetrics,
    # ZZ.A3 #303-3: per-turn tool-execution summary for TokenUsageStats card
    "turn_tool_stats": SSETurnToolStats,
    # ZZ.B1 #304-1 checkbox 3: per-turn terminal event (messages + tools + cost)
    "turn.complete": SSETurnComplete,
    # Q.3-SUB-1 (#297): cross-device workflow_run state push
    "workflow_updated": SSEWorkflowUpdated,
    # Q.3-SUB-3 (#297): cross-device notification read-state push
    "notification.read": SSENotificationRead,
    # Q.3-SUB-4 (#297): cross-device user-preferences push
    "preferences.updated": SSEPreferencesUpdated,
    # Q.3-SUB-5 (#297): cross-device non-LLM integration-settings push
    "integration.settings.updated": SSEIntegrationSettingsUpdated,
    # Q.3-SUB-6 (#297): cross-device chat-history push
    "chat.message": SSEChatMessage,
    # ZZ.B2 #304-2 checkbox 1: LLM-generated auto-title push
    "session.titled": SSESessionTitled,
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

"""Pydantic models aligned with frontend TypeScript interfaces."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- Agent ----------

class ProjectClass(str, Enum):
    embedded_product = "embedded_product"
    algo_sim = "algo_sim"
    optical_sim = "optical_sim"
    iso_standard = "iso_standard"
    test_tool = "test_tool"
    factory_tool = "factory_tool"
    enterprise_web = "enterprise_web"


class AgentType(str, Enum):
    firmware = "firmware"
    software = "software"
    reporter = "reporter"
    validator = "validator"
    reviewer = "reviewer"
    custom = "custom"


class AgentStatus(str, Enum):
    idle = "idle"
    running = "running"
    success = "success"
    error = "error"
    warning = "warning"
    booting = "booting"
    awaiting_confirmation = "awaiting_confirmation"
    materializing = "materializing"


class AgentProgress(BaseModel):
    current: int = 0
    total: int = 0


class SubTask(BaseModel):
    id: str
    label: str
    status: str = "pending"


# L#48: pin the valid set so typos (`status="activ"`) fail at validate
# time instead of leaking to the UI. The four values were previously
# documented in a trailing comment only.
AgentWorkspaceStatus = Literal["none", "active", "finalized", "cleaned"]


class AgentWorkspace(BaseModel):
    branch: Optional[str] = None
    path: Optional[str] = None
    status: AgentWorkspaceStatus = "none"
    commit_count: int = 0
    task_id: Optional[str] = None
    remote_name: str = "origin"
    repo_url: Optional[str] = None
    # R8 #314: anchor commit SHA — the retry target for
    # WorkspaceManager.discard_and_recreate (see
    # docs/design/r8-idempotent-retry-worktree.md). Captured at provision
    # time and never mutated. Optional so legacy rows in the agents table
    # (workspace JSON without this key) still deserialise.
    anchor_sha: Optional[str] = None


class Agent(BaseModel):
    id: str
    name: str
    type: AgentType
    sub_type: str = ""  # Role specialization (bsp, isp, sdet, etc.)
    status: AgentStatus = AgentStatus.idle
    progress: AgentProgress = Field(default_factory=AgentProgress)
    thought_chain: str = ""
    ai_model: Optional[str] = None
    sub_tasks: list[SubTask] = Field(default_factory=list)
    workspace: AgentWorkspace = Field(default_factory=AgentWorkspace)
    file_scope: list[str] = Field(default_factory=list)  # Glob patterns from CODEOWNERS

    class Config:
        populate_by_name = True


class AgentCreate(BaseModel):
    name: str
    type: AgentType
    sub_type: str = ""
    ai_model: Optional[str] = None


# ---------- Task ----------

class TaskPriority(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class TaskStatus(str, Enum):
    backlog = "backlog"
    analyzing = "analyzing"
    assigned = "assigned"
    in_progress = "in_progress"
    in_review = "in_review"
    completed = "completed"
    blocked = "blocked"


# Valid state transitions (from → set of allowed destinations)
TASK_TRANSITIONS: dict[str, set[str]] = {
    "backlog":     {"analyzing", "assigned", "in_progress", "blocked"},
    "analyzing":   {"assigned", "backlog", "blocked"},
    "assigned":    {"in_progress", "backlog", "blocked"},
    "in_progress": {"in_review", "completed", "blocked"},
    "in_review":   {"in_progress", "completed", "blocked"},  # in_progress = revision needed
    "completed":   {"backlog"},  # reopen
    "blocked":     {"backlog", "assigned", "in_progress"},
}


class TaskComment(BaseModel):
    id: str
    task_id: str
    author: str  # agent_id or "human"
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class Task(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.medium
    status: TaskStatus = TaskStatus.backlog
    assigned_agent_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    ai_analysis: Optional[str] = None
    suggested_agent_type: Optional[AgentType] = None
    suggested_sub_type: Optional[str] = None
    parent_task_id: Optional[str] = None
    child_task_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)  # Task IDs that must complete before this task
    # Issue tracking integration
    external_issue_id: Optional[str] = None  # e.g. "OMNI-123", "42"
    issue_url: Optional[str] = None  # e.g. "https://jira.company.com/browse/OMNI-123"
    external_issue_platform: Optional[str] = None  # "github" | "gitlab" | "jira"
    last_external_sync_at: Optional[str] = None  # ISO timestamp — debounce sync loops
    acceptance_criteria: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    # Pipeline linkage (Phase 46)
    npi_phase_id: Optional[str] = None  # Links task to an NPI phase for pipeline tracking
    # Q.7 #301 — optimistic-lock version. Incremented on every successful
    # PATCH; clients echo via ``If-Match`` so two devices racing on the
    # same task produce exactly one winner + one 409 loser.
    version: int = 0


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.medium
    suggested_agent_type: Optional[AgentType] = None
    suggested_sub_type: Optional[str] = None
    parent_task_id: Optional[str] = None
    external_issue_id: Optional[str] = None
    issue_url: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    labels: list[str] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[TaskPriority] = None
    status: Optional[TaskStatus] = None
    assigned_agent_id: Optional[str] = None
    suggested_sub_type: Optional[str] = None
    parent_task_id: Optional[str] = None
    child_task_ids: Optional[list[str]] = None
    external_issue_id: Optional[str] = None
    issue_url: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    labels: Optional[list[str]] = None


# ---------- Chat / Orchestrator ----------

class MessageRole(str, Enum):
    user = "user"
    orchestrator = "orchestrator"
    system = "system"


class AISuggestion(BaseModel):
    id: str
    type: str  # assign | spawn | alert | complete | reassign
    title: str
    description: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: Optional[AgentType] = None
    priority: TaskPriority = TaskPriority.medium
    status: str = "pending"  # pending | accepted | rejected


class OrchestratorMessage(BaseModel):
    id: str
    role: MessageRole
    content: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    suggestion: Optional[AISuggestion] = None


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    message: OrchestratorMessage
    agents: list[Agent] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)


# ---------- Notifications ----------

# ---------- Artifacts ----------

# ---------- NPI Lifecycle ----------

class BusinessModel(str, Enum):
    odm = "odm"
    oem = "oem"
    jdm = "jdm"
    obm = "obm"


class NPITrackType(str, Enum):
    engineering = "engineering"
    design = "design"
    market = "market"


class NPIMilestone(BaseModel):
    id: str
    title: str
    track: NPITrackType = NPITrackType.engineering
    status: str = "pending"  # pending | in_progress | completed | blocked
    due_date: Optional[str] = None
    completed_date: Optional[str] = None
    assigned_agent_type: Optional[str] = None
    jira_tag: Optional[str] = None  # e.g. "[HW]", "[MKT]", "[ID]"


class NPIPhase(BaseModel):
    id: str
    name: str
    short_name: str  # e.g. "PRD", "EIV", "POC"
    order: int = 0
    status: str = "pending"  # pending | active | completed | blocked
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    completed_date: Optional[str] = None
    milestones: list[NPIMilestone] = Field(default_factory=list)


class NPIProject(BaseModel):
    """Top-level NPI lifecycle state."""
    business_model: BusinessModel = BusinessModel.odm
    phases: list[NPIPhase] = Field(default_factory=list)
    current_phase_id: Optional[str] = None


# ---------- Artifacts ----------

class ArtifactType(str, Enum):
    pdf = "pdf"
    markdown = "markdown"
    json_doc = "json"
    log = "log"
    html = "html"
    # Binary artifact types (Phase 39)
    binary = "binary"             # Generic compiled binary
    firmware = "firmware"         # .bin / .hex / .elf firmware image
    kernel_module = "kernel_module"  # .ko Linux kernel module
    sdk = "sdk"                   # SDK package (.tar.gz, .deb)
    model = "model"              # NPU model (.rknn, .tflite, .engine)
    archive = "archive"          # .tar.gz / .zip bundle


class Artifact(BaseModel):
    id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    name: str
    type: ArtifactType = ArtifactType.markdown
    file_path: str = ""
    size: int = 0  # bytes
    version: str = ""            # Semantic version (e.g. "1.0.0-rc1")
    checksum: str = ""           # SHA-256 hex digest
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# ---------- Notifications ----------

class NotificationLevel(str, Enum):
    info = "info"          # L1: silent log
    warning = "warning"    # L2: badge + IM
    action = "action"      # L3: banner + ticket
    critical = "critical"  # L4: fullscreen + pager


# R9 row 2935 (#315): operational-priority tag attached to existing
# L1-L4 notifications. *Not* a parallel routing tier — see
# :mod:`backend.severity` for the design rationale and the spec
# constant :data:`backend.severity.SEVERITY_TIER_MAPPING`. Re-exported
# from this module so Pydantic models (the canonical wire-format
# definition spot) can reference it without callers needing two
# imports; the source of truth is :class:`backend.severity.Severity`.
from backend.severity import Severity  # noqa: E402  (re-export at point of use)


class Notification(BaseModel):
    id: str
    level: NotificationLevel
    title: str
    message: str
    source: str = ""         # e.g. "agent:firmware-alpha", "gerrit", "token_budget"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    read: bool = False
    action_url: Optional[str] = None   # e.g. Gerrit change URL
    action_label: Optional[str] = None  # e.g. "Review in Gerrit"
    auto_resolved: bool = False
    dispatch_status: str = "pending"  # pending, sent, failed, skipped
    send_attempts: int = 0
    last_error: Optional[str] = None
    # R9 row 2935: optional operational-priority tag. None = legacy
    # caller without severity awareness; the dispatcher falls back to
    # plain level routing in that case.
    severity: Optional[Severity] = None


# ---------- Simulations ----------

class SimulationTrack(str, Enum):
    algo = "algo"
    hw = "hw"
    npu = "npu"


class SimulationStatus(str, Enum):
    running = "running"
    passed = "pass"     # DB and frontend use "pass"
    failed = "fail"     # DB and frontend use "fail"
    error = "error"


class Simulation(BaseModel):
    id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    track: SimulationTrack
    module: str
    status: SimulationStatus = SimulationStatus.running
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    coverage_pct: float = 0.0
    valgrind_errors: int = 0
    duration_ms: int = 0
    report_json: dict = Field(default_factory=dict)
    artifact_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    # NPU-specific fields (only populated for npu track)
    npu_latency_ms: float = 0.0        # Average inference latency (ms/frame)
    npu_throughput_fps: float = 0.0     # Inference throughput (frames/sec)
    accuracy_delta: float = 0.0         # Accuracy drop from quantization (mAP diff)
    model_size_kb: int = 0              # Model file size
    npu_framework: str = ""             # rknn | tflite | tensorrt


class SimulationRequest(BaseModel):
    track: SimulationTrack
    module: str
    input_data: Optional[str] = None
    mock: bool = True
    platform: str = "aarch64"
    task_id: Optional[str] = None
    # NPU-specific request fields
    model_path: str = ""                # Path to model file (.rknn, .tflite, .engine)
    framework: str = ""                 # rknn | tflite | tensorrt
    test_images: str = ""               # Path to test image dataset directory


# ---------- Debug Blackboard ----------

class DebugFinding(BaseModel):
    id: str
    task_id: str
    agent_id: str
    finding_type: str  # see backend.finding_types.FindingType enum
    severity: str = "info"  # info, warn, error, critical
    content: str
    context: dict = Field(default_factory=dict)
    status: str = "open"  # open, acknowledged, resolved
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    resolved_at: Optional[str] = None


# ---------- API Response Models ----------

class HealthResponse(BaseModel):
    status: str
    engine: str
    version: str
    phase: str = ""


class StatusResponse(BaseModel):
    status: str
    detail: str = ""


class InvokeHaltResponse(BaseModel):
    status: str
    tasks_cancelled: int = 0
    containers_stopped: int = 0


class SystemInfoResponse(BaseModel):
    hostname: str = ""
    os: str = ""
    kernel: str = ""
    arch: str = ""
    cpu_model: str = ""
    cpu_cores: int = 0
    cpu_usage: float = 0.0
    memory_total: int = 0
    memory_used: int = 0
    disk_total_mb: int = 0
    disk_used_mb: int = 0
    disk_use_pct: str | float = 0.0
    uptime: str = ""
    wsl: bool = False
    docker: bool = False

    model_config = {"extra": "allow"}


class SystemStatusResponse(BaseModel):
    tasks_completed: int = 0
    tasks_total: int = 0
    agents_running: int = 0
    wsl_status: str = "OFFLINE"
    usb_status: str = "Detecting..."
    cpu_summary: str = ""
    memory_summary: str = ""
    workspaces_active: int = 0
    containers_active: int = 0


class TokenBudgetResponse(BaseModel):
    budget: float = 0.0
    usage: float = 0.0
    ratio: float = 0.0
    frozen: bool = False
    level: str = "normal"
    warn_threshold: float = 0.8
    downgrade_threshold: float = 0.9
    freeze_threshold: float = 1.0
    fallback_provider: str = ""
    fallback_model: str = ""


class TokenUsageEntry(BaseModel):
    """Per-model lifetime token counters returned by ``GET /runtime/tokens``.

    ZZ.A1 (#303-1): ``cache_read_tokens`` / ``cache_create_tokens`` /
    ``cache_hit_ratio`` are ``None`` on pre-ZZ rows (legacy payloads
    loaded from Redis or SQLite predating the prompt-cache columns) so
    the UI can distinguish "no data" from "genuine zero hits"; on ZZ-era
    rows they carry lifetime totals (``cache_hit_ratio = cache_read /
    (input + cache_read)``).
    """

    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    request_count: int = 0
    avg_latency: int = 0
    last_used: str = ""
    cache_read_tokens: Optional[int] = None
    cache_create_tokens: Optional[int] = None
    cache_hit_ratio: Optional[float] = None


class TokenBurnRatePoint(BaseModel):
    """One 60-second bucket in the burn-rate time series.

    ZZ.B3 #304-3 checkbox 1 (2026-04-24): the spec phrases the source as
    "aggregate ``token_usage`` 表的 ``created_at``", but ``token_usage`` is
    a per-model UPSERTed state table — the only time-series source of
    per-turn spend is ``event_log`` rows with ``event_type='turn.complete'``
    (persisted via ``_PERSIST_EVENT_TYPES``). Each row's ``data_json``
    carries ``tokens_used`` + ``cost_usd`` and ``created_at`` is the
    authoritative bucket key.

    Rates are normalised to per-hour so the sparkline can render the
    same y-axis regardless of which window the operator picked:
    ``tokens_per_hour = sum(bucket_tokens) / 60 * 3600``. Since buckets
    are exactly 60 s wide, the derivation collapses to
    ``sum(bucket_tokens) * 60``.
    """

    timestamp: str = ""
    tokens_per_hour: int = 0
    cost_per_hour: float = 0.0


class TokenBurnRateResponse(BaseModel):
    """ZZ.B3 #304-3 checkbox 1: burn-rate time-series envelope.

    ``window`` echoes the query parameter the client sent (``15m`` /
    ``1h`` / ``24h``) so the frontend sparkline can title the panel
    without parsing the URL again. ``bucket_seconds`` is the fixed
    60-second bucket width documented in the row spec — surfaced on
    the response so a future widening (e.g. ``24h`` → 5-min buckets)
    is discoverable client-side.
    """

    window: str = ""
    bucket_seconds: int = 60
    points: list[TokenBurnRatePoint] = Field(default_factory=list)


class TokenHeatmapCell(BaseModel):
    """ZZ.C2 #305-2 checkbox 1: one cell in the token-usage heatmap.

    The TODO spec phrases the schema as
    ``[{day, hour, token_total, cost_total}]`` (7 × 24 or 30 × 24
    matrix). Source of truth is the same ``event_log`` rows with
    ``event_type='turn.complete'`` that feed ``/runtime/tokens/
    burn-rate`` — each row carries ``tokens_used`` + ``cost_usd`` in
    ``data_json`` and a ``created_at`` in ``YYYY-MM-DD HH24:MI:SS``
    TEXT format.

      * ``day``: ``YYYY-MM-DD`` date string in UTC. A date string
        (rather than a relative ``0..N-1`` index) makes it trivial
        for the frontend to label each heatmap row with an operator-
        recognisable calendar date; the client still converts to
        its local timezone in the render pass (checkbox 5 of ZZ.C2
        locks that contract).
      * ``hour``: 0–23 integer, hour-of-day in UTC. The frontend
        shifts by the local offset when painting the grid.
      * ``token_total``: ``SUM(tokens_used)`` across all
        ``turn.complete`` rows whose UTC (day, hour) bucket matches.
      * ``cost_total``: ``SUM(COALESCE(cost_usd, 0))`` across the
        same bucket — the NULL-vs-genuine-zero contract from
        ``_estimate_turn_cost_usd`` maps unknown-model turns to 0
        cost without dropping the bucket's tokens (same
        COALESCE-to-zero policy as burn-rate).

    The endpoint emits only non-empty cells (sparse list). The
    heatmap UI fills in zeros for the ``(day, hour)`` slots the
    response omits; this keeps the payload proportional to real
    activity instead of always paying 168 / 720 cells on every GET.
    """

    day: str = ""
    hour: int = 0
    token_total: int = 0
    cost_total: float = 0.0


class TokenHeatmapResponse(BaseModel):
    """ZZ.C2 #305-2 checkbox 1: session-heatmap envelope.

    ``window`` echoes ``7d`` or ``30d`` so the frontend Calendar-
    style heatmap (checkbox 2) can title its panel and decide the
    grid height (7 vs 30 rows) without re-parsing the URL.

    Cells are sparse: only ``(day, hour)`` buckets with at least one
    ``turn.complete`` row appear. The frontend treats missing cells
    as genuine zero activity — this matches operator intuition
    (empty slot = no work happened) and keeps the payload bounded
    by real traffic rather than by window size.

    ZZ.C2 #305-2 checkbox 4 (2026-04-24): ``available_models`` carries
    the distinct ``model`` slugs observed across the unfiltered
    window so the frontend can render a per-model dropdown without
    a second round-trip. The list is intentionally derived *before*
    applying the ``model`` filter so operators can still pick a
    different model after selecting one — otherwise filtering by
    ``claude-opus-4-7`` would hide every other option. ``model``
    echoes the applied filter (or ``None`` for "All models") so the
    frontend can reconcile the select state after a remount.
    """

    window: str = ""
    cells: list[TokenHeatmapCell] = Field(default_factory=list)
    available_models: list[str] = Field(default_factory=list)
    model: str | None = None


class PromptVersionEntry(BaseModel):
    """ZZ.C1 #305-1 checkbox 1: one row in the prompt-version timeline.

    The TODO row phrases the schema as ``(id, agent_type, content_hash,
    content, created_at, supersedes_id)`` but the shipped table is
    ``prompt_versions(id, path, version, role, body, body_sha256,
    created_at, …)`` (see SP-5.2 + ``backend/prompt_registry.py``). We
    project the real columns onto the spec's field names:

      * ``agent_type``    ← basename of ``path`` without ``.md`` (e.g.
                            ``backend/agents/prompts/orchestrator.md``
                            → ``orchestrator``)
      * ``content``       ← ``body``
      * ``content_hash``  ← ``body_sha256`` (truncated-friendly SHA-256)
      * ``supersedes_id`` ← id of the next-older distinct-hash row for
                            the same path, derived at query time from
                            the dedupe cursor. Null for the bottom of
                            the timeline.

    ``version`` + ``role`` are also surfaced so the drawer UI can show
    "v7 (active)" / "v6 (archive)" without an extra round-trip.
    """

    id: int
    agent_type: str
    content_hash: str
    content: str
    content_preview: str = ""
    created_at: str = ""
    supersedes_id: Optional[int] = None
    version: int = 0
    role: str = ""


class PromptVersionsListResponse(BaseModel):
    """ZZ.C1 envelope for ``GET /runtime/prompts``.

    Echoes the request params (``agent_type`` + resolved ``path``) so
    the frontend drawer can cache per-agent lists without reparsing the
    URL, and exposes the raw ``limit`` that was applied after clamping.
    """

    agent_type: str = ""
    path: str = ""
    limit: int = 20
    versions: list[PromptVersionEntry] = Field(default_factory=list)


class PromptDiffResponse(BaseModel):
    """ZZ.C1 envelope for ``GET /runtime/prompts/diff``.

    The TODO spec phrases the response as "unified diff text" — we keep
    that literal shape under ``diff`` while also surfacing the two row
    endpoints' metadata so the drawer can label both sides without a
    second fetch (``from`` / ``to`` carry agent_type, version, hash,
    created_at; ``content`` bodies are omitted to keep the payload slim).
    """

    from_id: int = 0
    to_id: int = 0
    agent_type: str = ""
    from_hash: str = ""
    to_hash: str = ""
    from_version: int = 0
    to_version: int = 0
    from_created_at: str = ""
    to_created_at: str = ""
    diff: str = ""


class ProviderToolCallingCompat(BaseModel):
    """Per-model tool-calling support entry from config/ollama_tool_calling.yaml."""
    support: str  # "full" | "partial" | "none"
    min_ollama_version: str
    notes: str = ""


class ProviderInfo(BaseModel):
    id: str
    name: str
    default_model: str = ""
    models: list[str] = Field(default_factory=list)
    requires_key: bool = True
    env_var: Optional[str] = ""
    configured: bool = False
    base_url: Optional[str] = None
    # Z.6.4: ollama-only field; None for all other providers.
    tool_calling_compat: Optional[dict[str, ProviderToolCallingCompat]] = None


class ProvidersListResponse(BaseModel):
    active_provider: str = ""
    active_model: str = ""
    providers: list[ProviderInfo] = Field(default_factory=list)


class OllamaToolFailuresResponse(BaseModel):
    """Z.6.5 — Ollama tool-call failure counters from SharedKV."""
    total: int = 0
    daemon_error: int = 0
    parse_error: int = 0
    unsupported: int = 0
    has_warning: bool = False


class ProviderHealthItem(BaseModel):
    id: str
    name: str
    configured: bool = False
    is_active: bool = False
    last_failure: Optional[str] = None
    cooldown_remaining: int = 0
    status: str = "unknown"


class ProviderHealthResponse(BaseModel):
    chain: list[str] = Field(default_factory=list)
    health: list[ProviderHealthItem] = Field(default_factory=list)


class EpisodicMemory(BaseModel):
    id: str
    error_signature: str
    solution: str
    soc_vendor: str = ""
    sdk_version: str = ""
    hardware_rev: str = ""
    source_task_id: Optional[str] = None
    source_agent_id: Optional[str] = None
    gerrit_change_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    quality_score: float = 0.0
    access_count: int = 0
    created_at: str = ""
    updated_at: str = ""


# ---------- Hardware Deploy (Phase 30) ----------

class DeployMethod(str, Enum):
    ssh = "ssh"
    serial = "serial"
    fastboot = "fastboot"
    adb = "adb"


class EVKDevice(BaseModel):
    """An EVK (Evaluation Kit) board target for deployment."""
    platform: str                      # Platform profile name (e.g. "vendor-example")
    board_name: str = ""               # e.g. "Rockchip RK3588 EVK v2"
    deploy_method: str = "ssh"         # ssh | serial | fastboot | adb
    deploy_target_ip: str = ""         # Static IP of EVK board
    deploy_user: str = "root"
    deploy_path: str = "/opt/app"
    reachable: bool = False
    last_check: str = ""


class DeployRequest(BaseModel):
    """Request to deploy compiled artifacts to an EVK board."""
    platform: str                      # Target platform profile
    module: str                        # Module to deploy (e.g. "sensor_driver")
    workspace_path: str = ""           # Source workspace (auto-detect if empty)
    binary_path: str = ""              # Specific binary to deploy (optional)
    run_after_deploy: bool = True      # SSH exec after copy


class DeployResult(BaseModel):
    """Result of a deploy operation."""
    status: str = "pending"            # pending | deploying | success | error
    platform: str = ""
    target_ip: str = ""
    deploy_method: str = ""
    artifacts_copied: list[str] = Field(default_factory=list)
    remote_output: str = ""
    duration_ms: int = 0
    error: str = ""


class UVCDevice(BaseModel):
    """A UVC (USB Video Class) camera device."""
    device_path: str                   # /dev/video0
    name: str = ""                     # e.g. "USB Camera: HD Webcam"
    vendor_id: str = ""
    product_id: str = ""
    formats: list[str] = Field(default_factory=list)  # ["MJPG", "YUYV", "H264"]
    resolutions: list[str] = Field(default_factory=list)  # ["1920x1080", "1280x720"]
    capabilities: list[str] = Field(default_factory=list)  # ["video_capture", "streaming"]

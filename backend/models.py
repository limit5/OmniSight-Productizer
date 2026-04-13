"""Pydantic models aligned with frontend TypeScript interfaces."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Agent ----------

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


class AgentWorkspace(BaseModel):
    branch: Optional[str] = None
    path: Optional[str] = None
    status: str = "none"  # none | active | finalized | cleaned
    commit_count: int = 0
    task_id: Optional[str] = None
    remote_name: str = "origin"
    repo_url: Optional[str] = None


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
    finding_type: str  # error_repeated, stuck_loop, timeout, loop_breaker_trigger
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


class ProviderInfo(BaseModel):
    id: str
    name: str
    default_model: str = ""
    models: list[str] = Field(default_factory=list)
    requires_key: bool = True
    env_var: Optional[str] = ""
    configured: bool = False
    base_url: Optional[str] = None


class ProvidersListResponse(BaseModel):
    active_provider: str = ""
    active_model: str = ""
    providers: list[ProviderInfo] = Field(default_factory=list)


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

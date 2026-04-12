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
    # Issue tracking integration
    external_issue_id: Optional[str] = None  # e.g. "OMNI-123", "42"
    issue_url: Optional[str] = None  # e.g. "https://jira.company.com/browse/OMNI-123"
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


class Artifact(BaseModel):
    id: str
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    name: str
    type: ArtifactType = ArtifactType.markdown
    file_path: str = ""
    size: int = 0  # bytes
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


# ---------- Simulations ----------

class SimulationTrack(str, Enum):
    algo = "algo"
    hw = "hw"


class SimulationStatus(str, Enum):
    running = "running"
    passed = "passed"
    failed = "failed"
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


class SimulationRequest(BaseModel):
    track: SimulationTrack
    module: str
    input_data: Optional[str] = None
    mock: bool = True
    platform: str = "aarch64"
    task_id: Optional[str] = None

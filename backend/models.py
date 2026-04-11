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
    completed = "completed"
    blocked = "blocked"


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


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: TaskPriority = TaskPriority.medium
    suggested_agent_type: Optional[AgentType] = None
    suggested_sub_type: Optional[str] = None
    parent_task_id: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[TaskPriority] = None
    status: Optional[TaskStatus] = None
    assigned_agent_id: Optional[str] = None


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

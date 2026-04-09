"""Task management endpoints."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.models import Task, TaskCreate, TaskStatus, TaskUpdate
from backend.events import emit_task_update

router = APIRouter(prefix="/tasks", tags=["tasks"])

# In-memory store
_tasks: dict[str, Task] = {}


def _seed_defaults() -> None:
    defaults = [
        ("task-1", "Build IMX335 camera driver", "Compile and test firmware for Sony IMX335 sensor", "high", "firmware"),
        ("task-2", "Run validation suite", "Execute full test coverage for ISP pipeline", "medium", "validator"),
        ("task-3", "Generate compliance report", "Create FCC/CE certification documentation", "low", "reporter"),
    ]
    for tid, title, desc, priority, agent_type in defaults:
        _tasks[tid] = Task(
            id=tid,
            title=title,
            description=desc,
            priority=priority,
            status=TaskStatus.backlog,
            suggested_agent_type=agent_type,
        )


_seed_defaults()


@router.get("", response_model=list[Task])
async def list_tasks():
    return list(_tasks.values())


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return _tasks[task_id]


@router.post("", response_model=Task, status_code=201)
async def create_task(body: TaskCreate):
    task_id = f"task-{uuid.uuid4().hex[:6]}"
    task = Task(
        id=task_id,
        title=body.title,
        description=body.description,
        priority=body.priority,
        suggested_agent_type=body.suggested_agent_type,
    )
    _tasks[task_id] = task
    return task


@router.patch("/{task_id}", response_model=Task)
async def update_task(task_id: str, body: TaskUpdate):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = _tasks[task_id]
    update_data = body.model_dump(exclude_unset=True)
    if "status" in update_data and update_data["status"] == TaskStatus.completed:
        update_data["completed_at"] = datetime.now().isoformat()
    for field, value in update_data.items():
        setattr(task, field, value)
    emit_task_update(task_id, task.status, task.assigned_agent_id)
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    del _tasks[task_id]

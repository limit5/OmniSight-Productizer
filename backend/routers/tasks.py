"""Task management endpoints — persisted to SQLite."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException

from backend.models import Task, TaskCreate, TaskStatus, TaskUpdate
from backend.events import emit_task_update
from backend import db
from backend.routers import _pagination as _pg

router = APIRouter(prefix="/tasks", tags=["tasks"])

# ── In-memory mirror (kept in sync with DB for fast access by invoke) ──
_tasks: dict[str, Task] = {}


async def seed_defaults_if_empty() -> None:
    """Seed default tasks if the database is empty (called at startup)."""
    if await db.task_count() > 0:
        for row in await db.list_tasks():
            _tasks[row["id"]] = Task(**row)
        return

    defaults = [
        ("task-1", "Build IMX335 camera driver", "Compile and test firmware for Sony IMX335 sensor", "high", "firmware"),
        ("task-2", "Run validation suite", "Execute full test coverage for ISP pipeline", "medium", "validator"),
        ("task-3", "Generate compliance report", "Create FCC/CE certification documentation", "low", "reporter"),
    ]
    for tid, title, desc, priority, agent_type in defaults:
        task = Task(
            id=tid,
            title=title,
            description=desc,
            priority=priority,
            status=TaskStatus.backlog,
            suggested_agent_type=agent_type,
        )
        _tasks[tid] = task
        await db.upsert_task(task.model_dump())


async def _persist(task: Task) -> None:
    """Write task state to both memory and DB."""
    _tasks[task.id] = task
    await db.upsert_task(task.model_dump())


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
        suggested_sub_type=body.suggested_sub_type,
        parent_task_id=body.parent_task_id,
        external_issue_id=body.external_issue_id,
        issue_url=body.issue_url,
        acceptance_criteria=body.acceptance_criteria,
        labels=body.labels,
    )
    await _persist(task)
    return task


@router.get("/{task_id}/transitions")
async def get_transitions(task_id: str):
    """Return the list of valid next statuses for a task (state machine)."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    current = _tasks[task_id].status
    current_str = current.value if hasattr(current, "value") else current
    from backend.models import TASK_TRANSITIONS
    allowed = sorted(TASK_TRANSITIONS.get(current_str, set()))
    return {"task_id": task_id, "current_status": current_str, "allowed_transitions": allowed}


@router.patch("/{task_id}", response_model=Task)
async def update_task(task_id: str, body: TaskUpdate, force: bool = False):
    """Update a task. Status changes are validated against the state machine.

    Pass ``force=true`` to bypass transition validation (for system/human use).
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    task = _tasks[task_id]
    update_data = body.model_dump(exclude_unset=True)

    # State machine validation
    if "status" in update_data and not force:
        new_status = update_data["status"]
        new_str = new_status.value if hasattr(new_status, "value") else new_status
        current_str = task.status.value if hasattr(task.status, "value") else task.status
        from backend.models import TASK_TRANSITIONS
        allowed = TASK_TRANSITIONS.get(current_str, set())
        if new_str not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid transition: {current_str} → {new_str}. Allowed: {sorted(allowed)}",
            )

        # Fact-based gating: verify workspace has commits before in_review/completed
        if new_str in ("in_review", "completed") and task.assigned_agent_id:
            from backend.workspace import get_workspace
            ws = get_workspace(task.assigned_agent_id)
            if ws and ws.commit_count == 0 and new_str == "in_review":
                raise HTTPException(
                    status_code=400,
                    detail="Cannot move to in_review: no commits in workspace. Push code first.",
                )

    if "status" in update_data:
        s = update_data["status"]
        s_str = s.value if hasattr(s, "value") else s
        if s_str == "completed":
            update_data["completed_at"] = datetime.now().isoformat()

    for field, value in update_data.items():
        setattr(task, field, value)
    await _persist(task)
    emit_task_update(task_id, task.status, task.assigned_agent_id)

    # Sync to external issue tracker (non-blocking, capture URL before async dispatch)
    if "status" in update_data and task.issue_url:
        import asyncio
        _sync_url = task.issue_url  # Capture now to avoid race if URL changes later
        _sync_status = task.status.value if hasattr(task.status, "value") else task.status
        _sync_id = task.id
        asyncio.create_task(_sync_external_issue(_sync_url, _sync_status, _sync_id))

    return task


async def _sync_external_issue(issue_url: str, status: str, task_id: str) -> None:
    """Background: sync task status to external issue tracker."""
    try:
        from backend.issue_tracker import sync_issue_status
        result = await sync_issue_status(issue_url, status, comment=f"OmniSight task {task_id} status → {status}")
        if result.get("status") == "error":
            import logging
            logging.getLogger(__name__).warning("External sync failed for %s: %s", task_id, result.get("message"))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("External sync error for %s: %s", task_id, exc)


# ── Task Comments ──

@router.get("/{task_id}/comments")
async def get_task_comments(task_id: str, limit: int = _pg.Limit(default=20, max_cap=200)):
    """Get comment thread for a task."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.list_task_comments(task_id, limit=limit)


@router.post("/{task_id}/comments")
async def add_task_comment(task_id: str, author: str = "human", content: str = ""):
    """Add a comment to a task."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Comment content cannot be empty")
    import uuid as _uuid
    comment = {
        "id": f"comment-{_uuid.uuid4().hex[:8]}",
        "task_id": task_id,
        "author": author,
        "content": content,
        "timestamp": datetime.now().isoformat(),
    }
    await db.insert_task_comment(comment)
    return comment


@router.get("/{task_id}/handoffs")
async def get_task_handoffs(task_id: str):
    """Get handoff chain for a task — shows agent-to-agent transitions."""
    all_handoffs = await db.list_handoffs()
    chain = [h for h in all_handoffs if h.get("task_id") == task_id]
    return chain


@router.get("/handoffs/recent")
async def get_recent_handoffs(limit: int = _pg.Limit(default=20, max_cap=200)):
    """Get recent handoffs across all tasks."""
    handoffs = await db.list_handoffs()
    return handoffs[:limit]


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    del _tasks[task_id]
    await db.delete_task(task_id)

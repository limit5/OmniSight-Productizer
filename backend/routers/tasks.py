"""Task management endpoints.

Phase-3-Runtime-v2 SP-3.2 (2026-04-20): ported to native asyncpg +
``Depends(get_conn)`` pool-scoped connections. Request handlers carry
a request-scoped ``asyncpg.Connection`` parameter that propagates to
``_persist()`` and downstream ``db.*`` calls.

``_persist()`` is deliberately polymorphic on ``conn`` — request
handlers pass the Depends-injected conn; background workers
(invoke.py watchdog, pipeline.py, asyncio.create_task side-tasks,
agents/tools.py) that lack a request scope call it without a conn
and ``_persist`` acquires its own from the pool. This is the proper
use of the pool API for worker contexts where FastAPI's Depends isn't
available; it is NOT a workaround for the A1 router-propagation rule,
which still holds for every request handler.
"""

import uuid
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from backend.models import Task, TaskCreate, TaskStatus, TaskUpdate
from backend.events import emit_task_update
from backend import db
from backend.db_pool import get_conn, get_pool
from backend.routers import _pagination as _pg

router = APIRouter(prefix="/tasks", tags=["tasks"])

# ── In-memory mirror (kept in sync with DB for fast access by invoke) ──
_tasks: dict[str, Task] = {}


async def seed_defaults_if_empty(conn: asyncpg.Connection) -> None:
    """Seed default tasks if the database is empty (called at startup).

    Runs outside a request context — the lifespan handler acquires a
    connection from ``db_pool`` explicitly via ``async with
    get_pool().acquire() as conn:`` and passes it here. Skipping this
    call in SQLite dev mode is the lifespan's responsibility (the
    pool is only initialised when a Postgres DSN is configured).
    """
    if await db.task_count(conn) > 0:
        for row in await db.list_tasks(conn):
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
        await db.upsert_task(conn, task.model_dump())


async def _persist(task: Task, conn: asyncpg.Connection | None = None) -> None:
    """Write task state to both memory and DB.

    Memory-first: the in-memory mirror is updated before the DB write so
    a subsequent read (which hits memory, not DB) reflects the new state
    immediately. If the DB write fails, memory is stale — acceptable
    because ``seed_defaults_if_empty`` on next cold start re-syncs from
    DB, and handlers should NOT catch+swallow DB exceptions.

    Two call modes:
      * Request scope: handler passes its ``Depends(get_conn)`` conn →
        write rides the request's pool-scoped connection.
      * Worker scope (no request): conn is None → acquire a fresh one
        from the pool for the duration of this write, then release.
        invoke.py watchdog + pipeline.py + agents/tools.py use this
        path; they intentionally do NOT hold a long-lived conn.
    """
    _tasks[task.id] = task
    if conn is None:
        async with get_pool().acquire() as owned_conn:
            await db.upsert_task(owned_conn, task.model_dump())
    else:
        await db.upsert_task(conn, task.model_dump())


@router.get("", response_model=list[Task])
async def list_tasks():
    # Reads the in-memory mirror — no DB conn needed.
    return list(_tasks.values())


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str):
    # Reads the in-memory mirror — no DB conn needed.
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return _tasks[task_id]


@router.post("", response_model=Task, status_code=201)
async def create_task(
    body: TaskCreate,
    conn: asyncpg.Connection = Depends(get_conn),
):
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
    await _persist(task, conn)
    # Q.3-SUB-2 (#297): broadcast create so other devices append without
    # polling. Pre-Q.3-SUB-2 only PATCH emitted — create+delete were
    # invisible cross-device. ``action`` discriminates the mutation kind
    # so the frontend dispatcher can append vs patch vs remove; the
    # existing ``task_update`` channel is reused to keep the event-type
    # surface narrow (Q.4 #298 scope policy sweep can re-scope later).
    emit_task_update(
        task.id, task.status, task.assigned_agent_id,
        action="created",
    )
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
async def update_task(
    task_id: str,
    body: TaskUpdate,
    force: bool = False,
    conn: asyncpg.Connection = Depends(get_conn),
):
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
    await _persist(task, conn)
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
async def get_task_comments(
    task_id: str,
    limit: int = _pg.Limit(default=20, max_cap=200),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Get comment thread for a task."""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return await db.list_task_comments(conn, task_id, limit=limit)


@router.post("/{task_id}/comments")
async def add_task_comment(
    task_id: str,
    author: str = "human",
    content: str = "",
    conn: asyncpg.Connection = Depends(get_conn),
):
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
    await db.insert_task_comment(conn, comment)
    return comment


@router.get("/{task_id}/handoffs")
async def get_task_handoffs(
    task_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Get handoff chain for a task — shows agent-to-agent transitions."""
    all_handoffs = await db.list_handoffs(conn)
    chain = [h for h in all_handoffs if h.get("task_id") == task_id]
    return chain


@router.get("/handoffs/recent")
async def get_recent_handoffs(
    limit: int = _pg.Limit(default=20, max_cap=200),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Get recent handoffs across all tasks."""
    handoffs = await db.list_handoffs(conn)
    return handoffs[:limit]


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    del _tasks[task_id]
    await db.delete_task(conn, task_id)
    # Q.3-SUB-2 (#297): broadcast delete so other devices drop the row
    # without polling. Fires AFTER the DB delete so a subscriber who
    # re-fetches on receipt (belt + braces) already sees the row gone.
    # ``status='deleted'`` is an out-of-band sentinel for the action
    # channel — the frontend dispatcher switches on ``action``, not
    # on status, but we pass status for the log line and to keep the
    # payload shape consistent with other task_update events.
    emit_task_update(task_id, "deleted", action="deleted")

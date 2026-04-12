"""Webhook endpoints for external system integrations.

Currently supports Gerrit Code Review events:
- ``patchset-created`` → triggers AI Reviewer agent
- ``comment-added`` with -1 → notifies coder agent to fix
- ``change-merged`` → triggers replication to GitHub/GitLab
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.events import emit_invoke, emit_agent_update, emit_task_update
from backend.models import (
    Agent, AgentCreate, AgentProgress, AgentStatus,
    Task, TaskStatus, TaskPriority,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/gerrit")
async def gerrit_webhook(request: Request):
    """Receive Gerrit events and trigger appropriate actions.

    Gerrit sends JSON events via its webhook plugin or stream-events.
    """
    if not settings.gerrit_enabled:
        return JSONResponse(status_code=503, content={"detail": "Gerrit integration disabled"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    event_type = body.get("type", "")
    logger.info("Gerrit webhook: type=%s", event_type)

    if event_type == "patchset-created":
        await _on_patchset_created(body)
    elif event_type == "comment-added":
        await _on_comment_added(body)
    elif event_type == "change-merged":
        await _on_change_merged(body)
    else:
        logger.debug("Ignoring Gerrit event: %s", event_type)

    return {"status": "ok", "event": event_type}


async def _on_patchset_created(event: dict) -> None:
    """A new patchset was pushed — spawn a reviewer agent to review it."""
    change = event.get("change", {})
    patchset = event.get("patchSet", {})

    change_id = change.get("id", "")
    change_subject = change.get("subject", "")
    commit = patchset.get("revision", "")
    uploader = patchset.get("uploader", {}).get("name", "unknown")

    logger.info(
        "Patchset created: change=%s subject=%s commit=%s uploader=%s",
        change_id, change_subject, commit[:8], uploader,
    )

    # L2 notification: new patchset for review
    from backend.notifications import notify
    await notify(
        "warning", f"New patchset: {change_subject}",
        message=f"Change {change_id} by {uploader} — commit {commit[:8]}",
        source="gerrit",
        action_url=f"{settings.gerrit_url}/c/{change_id}" if settings.gerrit_url else None,
        action_label="Review in Gerrit",
    )

    # Create a review task
    from backend.routers.tasks import _tasks, _persist as _persist_task
    task_id = f"review-{uuid.uuid4().hex[:6]}"
    task = Task(
        id=task_id,
        title=f"Review: {change_subject}",
        description=f"Review Gerrit change {change_id} (commit {commit[:8]}) by {uploader}",
        priority=TaskPriority.high,
        status=TaskStatus.backlog,
        suggested_agent_type="reviewer",
    )
    _tasks[task_id] = task
    await _persist_task(task)

    # Find or create a reviewer agent
    from backend.routers.agents import _agents, _persist as _persist_agent
    reviewer = None
    for a in _agents.values():
        if a.type == "reviewer" and a.status in (AgentStatus.idle, AgentStatus.booting):
            reviewer = a
            break

    if not reviewer:
        reviewer_id = f"reviewer-{uuid.uuid4().hex[:6]}"
        reviewer = Agent(
            id=reviewer_id,
            name="Auto Reviewer",
            type="reviewer",
            sub_type="code-review",
            status=AgentStatus.idle,
            progress=AgentProgress(current=0, total=0),
            thought_chain="Spawned by Gerrit webhook.",
        )
        _agents[reviewer_id] = reviewer
        await _persist_agent(reviewer)

    # Assign and trigger
    task.status = TaskStatus.assigned
    task.assigned_agent_id = reviewer.id
    reviewer.status = AgentStatus.running
    reviewer.thought_chain = f"Reviewing change {change_id}: {change_subject}"
    await _persist_task(task)
    await _persist_agent(reviewer)

    emit_task_update(task_id, task.status, reviewer.id)
    emit_agent_update(reviewer.id, reviewer.status, reviewer.thought_chain)

    # Execute review in background (webhook must return fast)
    import asyncio
    asyncio.create_task(_run_review(reviewer, change_id, commit, change_subject))


async def _run_review(reviewer: Agent, change_id: str, commit: str, subject: str) -> None:
    """Background task: run LangGraph review pipeline and update agent status."""
    from backend.routers.agents import _persist as _persist_agent
    try:
        from backend.agents.graph import run_graph
        review_command = (
            f"Review Gerrit patchset for change {change_id}. "
            f"Commit: {commit}. Subject: {subject}. "
            f"Use gerrit_get_diff to read the diff, then analyze for issues. "
            f"Post inline comments with gerrit_post_comment for any findings. "
            f"Finally use gerrit_submit_review to give +1 or -1."
        )
        result = await run_graph(
            review_command,
            model_name=reviewer.ai_model or "",
            agent_sub_type=reviewer.sub_type,
        )
        reviewer.thought_chain = result.answer[:200] if result.answer else "Review complete."
        reviewer.status = AgentStatus.success
    except Exception as exc:
        reviewer.thought_chain = f"Review failed: {exc}"
        reviewer.status = AgentStatus.error
        logger.error("Review failed: %s", exc)

    await _persist_agent(reviewer)
    emit_agent_update(reviewer.id, reviewer.status, reviewer.thought_chain)


async def _on_comment_added(event: dict) -> None:
    """Code-Review -1 received → auto-create fix task for agent to iterate."""
    approvals = event.get("approvals", [])
    change = event.get("change", {})
    change_id = change.get("id", "")
    subject = change.get("subject", change_id)

    for approval in approvals:
        if approval.get("type") == "Code-Review" and approval.get("value") == "-1":
            logger.info("Code-Review -1 on change %s — creating fix task", change_id)

            # Extract reviewer feedback
            review_feedback = approval.get("message", "")
            if not review_feedback:
                review_feedback = event.get("comment", "No specific feedback provided.")

            # Create a fix task for INVOKE to pick up
            import uuid
            from backend.models import Task, TaskPriority, TaskStatus
            from backend.routers.tasks import _tasks
            from backend import db

            fix_task_id = f"fix-{uuid.uuid4().hex[:6]}"
            fix_task = Task(
                id=fix_task_id,
                title=f"Fix Gerrit review: {subject[:60]}",
                description=(
                    f"Code-Review -1 on change {change_id}.\n\n"
                    f"Reviewer feedback:\n{review_feedback}\n\n"
                    f"Analyze the feedback, fix the code, and push a new patchset."
                ),
                priority=TaskPriority.high,
                status=TaskStatus.backlog,
                suggested_agent_type="software",
                labels=["gerrit-review-fix"],
                external_issue_id=change_id,
            )
            _tasks[fix_task_id] = fix_task
            try:
                await db.upsert_task(fix_task.model_dump())
            except Exception:
                pass

            emit_invoke("review_rejected", f"Change {change_id} received -1 — fix task {fix_task_id} created")

            # L2 notification
            from backend.notifications import notify
            asyncio.create_task(notify(
                "warning", f"Code-Review -1: {subject[:60]}",
                message=f"Fix task {fix_task_id} created. Agent will iterate.",
                source="gerrit",
                action_label="View Change",
            ))
            break


async def _on_change_merged(event: dict) -> None:
    """A change was merged — trigger replication to external repos."""
    change = event.get("change", {})
    change_id = change.get("id", "")
    subject = change.get("subject", "")

    logger.info("Change merged: %s — %s", change_id, subject)
    emit_invoke("merged", f"Change {change_id} merged: {subject}")

    # L1 notification: merge + replication
    from backend.notifications import notify
    await notify("info", f"Merged: {subject}", source="gerrit")

    # Trigger replication
    targets = [t.strip() for t in settings.gerrit_replication_targets.split(",") if t.strip()]
    if not targets:
        return

    from backend.git_auth import get_auth_env
    from backend.workspace import _run, _MAIN_REPO

    for target in targets:
        try:
            # Get remote URL for auth
            rc, url, _ = await _run(f'git remote get-url "{target}"', cwd=_MAIN_REPO)
            auth_env = get_auth_env(url.strip()) if rc == 0 else {}
            rc, out, err = await _run(
                f'git push "{target}" main --force-with-lease',
                cwd=_MAIN_REPO,
                extra_env=auth_env,
            )
            if rc == 0:
                logger.info("Replicated to %s", target)
                emit_invoke("replicated", f"Pushed to {target}")
            else:
                logger.warning("Replication to %s failed: %s", target, err)
        except Exception as exc:
            logger.error("Replication to %s error: %s", target, exc)

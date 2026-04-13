"""Webhook endpoints for external system integrations.

Currently supports Gerrit Code Review events:
- ``patchset-created`` → triggers AI Reviewer agent
- ``comment-added`` with -1 → notifies coder agent to fix
- ``change-merged`` → triggers replication to GitHub/GitLab
"""

from __future__ import annotations

import asyncio
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
    Supports optional HMAC-SHA256 signature via X-Gerrit-Signature header.
    """
    if not settings.gerrit_enabled:
        return JSONResponse(status_code=503, content={"detail": "Gerrit integration disabled"})

    # Authenticate if secret is configured
    raw_body = await request.body()
    if settings.gerrit_webhook_secret:
        import hashlib
        import hmac as _hmac
        signature = request.headers.get("X-Gerrit-Signature", "")
        expected = _hmac.new(
            settings.gerrit_webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not _hmac.compare_digest(signature, expected):
            return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

    try:
        body = json.loads(raw_body)
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
        if approval.get("type") == "Code-Review" and str(approval.get("value")) == "-1":
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

    # Package build artifacts from merged change
    asyncio.create_task(_package_merged_artifacts(change_id, subject))

    # L3 Episodic Memory: auto-save solution from merged change
    asyncio.create_task(_save_merged_solution_to_l3(change_id, subject))

    # Trigger CI/CD pipelines after merge
    asyncio.create_task(_trigger_ci_pipelines())


async def _package_merged_artifacts(change_id: str, subject: str) -> None:
    """Create a release artifact bundle from a merged change.

    Scans the main repo for recent build outputs and packages them
    as a tar.gz archive registered in the artifact system.
    """
    import hashlib
    import tarfile
    import uuid as _uuid
    from datetime import datetime
    from pathlib import Path

    try:
        from backend.routers.artifacts import get_artifacts_root
        from backend.workspace import _MAIN_REPO, _BUILD_OUTPUT_DIRS
        from backend import db

        # Scan main repo for build outputs
        build_files: list[Path] = []
        for build_dir_name in _BUILD_OUTPUT_DIRS:
            build_dir = _MAIN_REPO / build_dir_name
            if not build_dir.is_dir():
                continue
            for fpath in build_dir.rglob("*"):
                if fpath.is_file() and fpath.stat().st_size >= 10:
                    build_files.append(fpath)
            if build_files:
                break

        if not build_files:
            logger.debug("No build outputs found for merged change %s", change_id)
            return

        # Create tar.gz bundle
        artifacts_root = get_artifacts_root()
        bundle_dir = artifacts_root / "releases"
        bundle_dir.mkdir(parents=True, exist_ok=True)

        safe_subject = "".join(c if c.isalnum() or c in "-_" else "_" for c in subject[:40])
        safe_change_id = "".join(c if c.isalnum() or c in "-_" else "" for c in change_id[:12])
        bundle_name = f"release_{safe_subject}_{safe_change_id}.tar.gz"
        bundle_path = bundle_dir / bundle_name

        with tarfile.open(bundle_path, "w:gz") as tar:
            for fpath in build_files:
                tar.add(fpath, arcname=fpath.name)

        # Compute checksum
        sha = hashlib.sha256()
        with open(bundle_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)

        artifact_id = f"art-{_uuid.uuid4().hex[:8]}"
        await db.insert_artifact({
            "id": artifact_id,
            "task_id": "",
            "agent_id": "gerrit-merge",
            "name": bundle_name,
            "type": "archive",
            "file_path": str(bundle_path),
            "size": bundle_path.stat().st_size,
            "created_at": datetime.now().isoformat(),
            "version": change_id[:12],
            "checksum": sha.hexdigest(),
        })

        from backend.events import bus
        bus.publish("artifact_created", {
            "id": artifact_id, "name": bundle_name, "type": "archive",
            "task_id": "", "agent_id": "gerrit-merge",
            "size": bundle_path.stat().st_size,
        })

        logger.info("Merge artifact: %s (%d files, %d bytes)", bundle_name, len(build_files), bundle_path.stat().st_size)

    except Exception as exc:
        logger.warning("Merge artifact packaging failed (non-critical): %s", exc)


async def _save_merged_solution_to_l3(change_id: str, subject: str) -> None:
    """Save a merged change's solution to L3 episodic memory if it fixed a bug.

    Only saves if the change has associated debug findings (indicating it was a bug fix).
    This ensures L3 only contains verified, human-approved solutions (Gerrit +2).
    """
    try:
        from backend import db
        # Find debug findings linked to this change's task
        findings = await db.list_debug_findings(status="open", limit=20)
        # Match findings by looking for the change subject in task context
        related = [f for f in findings if subject and (
            subject.lower() in f.get("content", "").lower()
            or change_id in f.get("context", "")
        )]

        if not related:
            return

        for finding in related[:3]:  # Max 3 memories per merge
            memory_id = f"mem-{uuid.uuid4().hex[:12]}"
            await db.insert_episodic_memory({
                "id": memory_id,
                "error_signature": finding.get("content", "")[:500],
                "solution": f"Fix: {subject}",
                "soc_vendor": "",  # Can be enriched from platform config
                "sdk_version": "",
                "gerrit_change_id": change_id,
                "source_task_id": finding.get("task_id", ""),
                "source_agent_id": finding.get("agent_id", ""),
                "tags": [finding.get("finding_type", "fix")],
                "quality_score": 1.0,  # Merged = verified
            })
            # Mark the finding as resolved
            await db.update_debug_finding(finding["id"], "resolved")
            logger.info("L3: Saved merged solution %s for finding %s", memory_id, finding["id"])

    except Exception as exc:
        logger.warning("L3 auto-save on merge failed (non-critical): %s", exc)


async def _trigger_ci_pipelines() -> None:
    """Trigger configured CI/CD pipelines after a Gerrit merge."""
    if settings.ci_github_actions_enabled and settings.github_token:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "workflow", "run", "ci.yml", "-r", "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "GitHub Actions workflow triggered")
            else:
                logger.warning("GitHub Actions trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("GitHub Actions trigger error: %s", exc)

    if settings.ci_jenkins_enabled and settings.ci_jenkins_url:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST",
                f"{settings.ci_jenkins_url}/build",
                "-u", f"{settings.ci_jenkins_user}:{settings.ci_jenkins_api_token}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "Jenkins build triggered")
            else:
                logger.warning("Jenkins trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("Jenkins trigger error: %s", exc)

    if settings.ci_gitlab_enabled and settings.gitlab_token:
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST",
                f"{settings.gitlab_url}/api/v4/projects/{settings.gerrit_project.replace('/', '%2F')}/pipeline",
                "-H", f"PRIVATE-TOKEN: {settings.gitlab_token}",
                "-d", "ref=main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "GitLab CI pipeline triggered")
            else:
                logger.warning("GitLab CI trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("GitLab CI trigger error: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  External → Internal Webhook Sync
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _find_task_by_issue_url(url: str):
    """Find internal task matching an external issue URL or ID."""
    from backend.routers.tasks import _tasks
    for t in _tasks.values():
        if t.issue_url and t.issue_url == url:
            return t
        if t.external_issue_id and t.external_issue_id in url:
            return t
    return None


async def _sync_external_to_task(task, new_status: str, platform: str) -> dict:
    """Apply external status change to internal task with debounce."""
    from datetime import datetime as _dt
    from backend.routers.tasks import _persist

    # Debounce: skip if synced < 5s ago (prevent sync loops)
    if task.last_external_sync_at:
        try:
            last = _dt.fromisoformat(task.last_external_sync_at)
            if (_dt.now() - last).total_seconds() < 5:
                return {"status": "debounced"}
        except Exception:
            pass

    old_status = task.status.value if hasattr(task.status, "value") else str(task.status)
    if new_status in TaskStatus.__members__:
        task.status = TaskStatus[new_status]
    task.last_external_sync_at = _dt.now().isoformat()
    task.external_issue_platform = platform
    await _persist(task)
    emit_task_update(task.id, task.status.value, task.assigned_agent_id)
    logger.info("[EXT→INT] Task %s: %s → %s (via %s)", task.id, old_status, new_status, platform)
    return {"status": "synced", "task_id": task.id, "new_status": new_status}


@router.post("/github")
async def github_webhook(request: Request):
    """Receive GitHub issue/PR webhooks — sync status to internal tasks."""
    import hashlib, hmac as _hmac
    if not settings.github_webhook_secret:
        return JSONResponse(status_code=503, content={"detail": "GitHub webhooks not configured"})

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + _hmac.new(settings.github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected, sig):
        return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

    event = json.loads(body)
    event_type = request.headers.get("X-GitHub-Event", "")
    issue = event.get("issue", {})
    issue_url = issue.get("html_url", "")

    task = _find_task_by_issue_url(issue_url)
    if not task:
        return {"status": "ok", "message": "No matching task"}

    if event_type == "issues":
        state = issue.get("state", "")
        new_status = "completed" if state == "closed" else "in_progress"
        return await _sync_external_to_task(task, new_status, "github")

    return {"status": "ok", "event": event_type}


@router.post("/gitlab")
async def gitlab_webhook(request: Request):
    """Receive GitLab issue webhooks — sync status to internal tasks."""
    import hmac as _hmac
    if not settings.gitlab_webhook_secret:
        return JSONResponse(status_code=503, content={"detail": "GitLab webhooks not configured"})

    token = request.headers.get("X-Gitlab-Token", "")
    if not _hmac.compare_digest(token, settings.gitlab_webhook_secret):
        return JSONResponse(status_code=401, content={"detail": "Invalid token"})

    event = await request.json()
    attrs = event.get("object_attributes", {})
    issue_url = attrs.get("url", "")

    task = _find_task_by_issue_url(issue_url)
    if not task:
        return {"status": "ok", "message": "No matching task"}

    state = attrs.get("state", "")
    new_status = "completed" if state == "closed" else "in_progress"
    return await _sync_external_to_task(task, new_status, "gitlab")


@router.post("/jira")
async def jira_webhook(request: Request):
    """Receive Jira issue webhooks — sync status to internal tasks."""
    import hmac as _hmac
    if not settings.jira_webhook_secret:
        return JSONResponse(status_code=503, content={"detail": "Jira webhooks not configured"})

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not _hmac.compare_digest(auth[7:], settings.jira_webhook_secret):
        return JSONResponse(status_code=401, content={"detail": "Invalid token"})

    event = await request.json()
    issue_key = event.get("issue", {}).get("key", "")

    task = _find_task_by_issue_url(issue_key)
    if not task:
        return {"status": "ok", "message": "No matching task"}

    # Extract status change from changelog
    for item in event.get("changelog", {}).get("items", []):
        if item.get("field") == "status":
            jira_status = item.get("toString", "")
            status_map = {"Done": "completed", "In Progress": "in_progress",
                          "In Review": "in_review", "Blocked": "blocked", "To Do": "backlog"}
            new_status = status_map.get(jira_status, "in_progress")
            return await _sync_external_to_task(task, new_status, "jira")

    return {"status": "ok", "event": "no_status_change"}

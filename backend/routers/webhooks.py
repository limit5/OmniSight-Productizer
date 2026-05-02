"""Webhook endpoints for external system integrations.

Currently supports Gerrit Code Review events:
- ``patchset-created`` → triggers AI Reviewer agent
- ``comment-added`` with -1 → notifies coder agent to fix
- ``change-merged`` → triggers replication to GitHub/GitLab
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.db_pool import get_conn, get_pool
from backend.email_delivery.webhooks import (
    EmailFeedbackEvent,
    normalize_email_webhook_provider,
    parse_email_feedback_events,
)
from backend.events import emit_invoke, emit_agent_update, emit_task_update
from backend.models import (
    Agent, AgentProgress, AgentStatus,
    Task, TaskStatus, TaskPriority,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/email/{provider}")
async def email_feedback_webhook(provider: str, request: Request):
    """Receive provider bounce / complaint webhooks for FS.4 email.

    The endpoint accepts a shared bearer token or HMAC-SHA256 signature.
    ``settings.email_webhook_secret`` is env/runtime configuration, so
    every worker independently verifies against the same source value
    without writing module-global state.
    """
    try:
        canonical_provider = normalize_email_webhook_provider(provider)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    secret = settings.email_webhook_secret
    if not secret:
        return JSONResponse(
            status_code=503,
            content={"detail": "Email feedback webhooks not configured"},
        )

    raw_body = await request.body()
    if len(raw_body) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})
    if not _verify_email_feedback_webhook(request, raw_body, secret):
        return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    try:
        events = parse_email_feedback_events(canonical_provider, body)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    for event in events:
        await _on_email_feedback_event(event)

    return {
        "status": "ok",
        "provider": canonical_provider,
        "count": len(events),
        "events": [event.to_dict() for event in events],
    }


def _verify_email_feedback_webhook(
    request: Request,
    raw_body: bytes,
    secret: str,
) -> bool:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ").strip()
        if hmac.compare_digest(token, secret):
            return True

    token = request.headers.get("X-OmniSight-Email-Webhook-Token", "")
    if token and hmac.compare_digest(token, secret):
        return True

    signature = request.headers.get("X-OmniSight-Email-Signature", "")
    expected = "sha256=" + hmac.new(
        secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


async def _on_email_feedback_event(event: EmailFeedbackEvent) -> None:
    """Route a normalized email feedback event to operator notification."""
    logger.warning(
        "email_feedback provider=%s type=%s recipient=%s message_id=%s reason=%s",
        event.provider,
        event.event_type,
        event.recipient,
        event.message_id,
        event.reason,
    )
    from backend.notifications import notify

    title = (
        "Email complaint received"
        if event.event_type == "complaint"
        else "Email bounce received"
    )
    await notify(
        "warning",
        title,
        message=(
            f"{event.provider} reported {event.event_type} for "
            f"{event.recipient}"
            + (f" ({event.reason})" if event.reason else "")
        ),
        source="email_delivery",
    )


@router.post("/gerrit")
async def gerrit_webhook(
    request: Request,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Receive Gerrit events and trigger appropriate actions.

    Gerrit sends JSON events via its webhook plugin or stream-events.
    Supports optional HMAC-SHA256 signature via X-Gerrit-Signature header.

    Phase-3-Runtime-v2 SP-3.1: handler takes a pool-backed
    ``asyncpg.Connection`` so the agent-spawn code path inside
    ``_on_patchset_created`` can persist the spawned reviewer via
    the ported ``db.upsert_agent`` API. Background tasks started from
    here (``_run_review``) acquire their OWN conn since the request
    scope ends before they run.
    """
    if not settings.gerrit_enabled:
        return JSONResponse(status_code=503, content={"detail": "Gerrit integration disabled"})

    # Authenticate — verify signature BEFORE parsing payload to avoid DoS on
    # untrusted JSON and to keep payload-derived host lookup post-verification.
    raw_body = await request.body()
    # Reject obviously oversized payloads (DoS guard) — Gerrit events are <64KB.
    if len(raw_body) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})

    import hashlib
    import hmac as _hmac
    signature = request.headers.get("X-Gerrit-Signature", "")
    scalar_secret = settings.gerrit_webhook_secret
    scalar_ok = False
    if scalar_secret:
        expected = _hmac.new(
            scalar_secret.encode(), raw_body, hashlib.sha256,
        ).hexdigest()
        scalar_ok = _hmac.compare_digest(signature, expected)

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    # Phase 5-7 (#multi-account-forge): per-instance secret check is now
    # routed through ``get_webhook_secret_for_host_async`` so operator-
    # added ``git_accounts(platform='gerrit')`` rows are honoured. The
    # async path reads the canonical PG table; the legacy shim continues
    # to synthesise a virtual ``default-gerrit`` row from
    # ``settings.gerrit_*`` scalars so single-instance deployments stay
    # working with no operator action required.
    gerrit_host = ""
    try:
        change_url = (body.get("change") or {}).get("url", "") or ""
        if change_url:
            from urllib.parse import urlparse as _urlparse
            gerrit_host = (_urlparse(change_url).hostname or "")
    except Exception:
        gerrit_host = ""

    host_ok = False
    if gerrit_host:
        try:
            from backend.git_credentials import (
                get_webhook_secret_for_host_async,
            )
            host_secret = await get_webhook_secret_for_host_async(
                gerrit_host, "gerrit",
            )
        except Exception:
            host_secret = ""
        if host_secret:
            expected_h = _hmac.new(
                host_secret.encode(), raw_body, hashlib.sha256,
            ).hexdigest()
            host_ok = _hmac.compare_digest(signature, expected_h)

    if (scalar_secret or gerrit_host) and not (scalar_ok or host_ok):
        return JSONResponse(status_code=401, content={"detail": "Invalid signature"})

    event_type = body.get("type", "")
    logger.info("Gerrit webhook: type=%s", event_type)

    if event_type == "patchset-created":
        await _on_patchset_created(conn, body)
    elif event_type == "comment-added":
        await _on_comment_added(body)
    elif event_type == "change-merged":
        await _on_change_merged(body)
    else:
        logger.debug("Ignoring Gerrit event: %s", event_type)

    return {"status": "ok", "event": event_type}


async def _on_patchset_created(
    conn: asyncpg.Connection, event: dict,
) -> None:
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
    # SP-3.2: pass the request-scoped pool conn through to _persist so
    # the webhook's atomic acquire is reused (no per-call pool churn).
    await _persist_task(task, conn)

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
        await _persist_agent(reviewer, conn)

    # Assign and trigger
    task.status = TaskStatus.assigned
    task.assigned_agent_id = reviewer.id
    reviewer.status = AgentStatus.running
    reviewer.thought_chain = f"Reviewing change {change_id}: {change_subject}"
    await _persist_task(task, conn)
    await _persist_agent(reviewer, conn)

    emit_task_update(task_id, task.status, reviewer.id)
    emit_agent_update(reviewer.id, reviewer.status, reviewer.thought_chain)

    # Execute review in background (webhook must return fast). _run_review
    # acquires its OWN conn from the pool because the request-scoped conn
    # is released when the webhook handler returns (asyncio.create_task
    # runs after return).
    asyncio.create_task(_run_review(reviewer, change_id, commit, change_subject))


async def _run_review(reviewer: Agent, change_id: str, commit: str, subject: str) -> None:
    """Background task: run LangGraph review pipeline and update agent status.

    Acquires its own pool-backed conn via ``async with pool.acquire()``
    — the webhook's request-scoped conn is gone by the time this task
    runs.
    """
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

    # SP-3.2 (2026-04-20): _persist_agent is now polymorphic on conn —
    # background context → call with None and it acquires from pool.
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
            from backend.routers.tasks import _persist as _persist_task

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
            # SP-3.2: _persist is polymorphic — worker context (no conn)
            # acquires its own pool-scoped connection for the write.
            try:
                await _persist_task(fix_task)
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

    # O5 (#268) — drive IntentSource bridge to flip the sub-task (and
    # parent, when all sub-tasks are merged) to Done.  Best-effort.
    commit_msg = (event.get("change", {}) or {}).get("commitMessage", "") or \
        subject
    try:
        from backend import intent_bridge
        await intent_bridge.on_gerrit_change_merged(
            change_id=change_id, commit_msg=commit_msg, vendor=None,
        )
    except Exception as exc:
        logger.warning("intent_bridge.on_gerrit_change_merged failed: %s", exc)

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

    # Pipeline auto-advance: Gerrit merge = review checkpoint passed
    try:
        from backend.pipeline import force_advance, get_pipeline_status
        status = get_pipeline_status()
        if status.get("status") == "running" and status.get("current_step") == "review":
            asyncio.create_task(force_advance())
            logger.info("Pipeline: Gerrit merge triggered force-advance past review checkpoint")
    except Exception:
        pass


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
        # SP-3.6a: _package_merged_artifacts is a background worker
        # (spawned via asyncio.create_task from _on_change_merged) —
        # no request conn. Acquire from pool for the single insert.
        async with get_pool().acquire() as _conn:
            await db.insert_artifact(_conn, {
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
    # SP-3.9: _save_merged_solution_to_l3 is a background task (spawned
    # via asyncio.create_task from _on_change_merged) — no request
    # conn. Acquire ONCE for the list + update loop since the read
    # result drives the subsequent writes on the same logical unit of
    # work. insert_episodic_memory is still pre-port (SP-3.12) so it
    # still works via the compat wrapper.
    try:
        from backend import db
        from backend.db_pool import get_pool
        async with get_pool().acquire() as _conn:
            findings = await db.list_debug_findings(
                _conn, status="open", limit=20,
            )
            # Match findings by looking for the change subject in task context
            related = [f for f in findings if subject and (
                subject.lower() in f.get("content", "").lower()
                or change_id in f.get("context", "")
            )]

            if not related:
                return

            for finding in related[:3]:  # Max 3 memories per merge
                memory_id = f"mem-{uuid.uuid4().hex[:12]}"
                # SP-3.12: conn already acquired at the top of this
                # try/except block (see SP-3.9 change) — reuse it.
                await db.insert_episodic_memory(_conn, {
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
                await db.update_debug_finding(_conn, finding["id"], "resolved")
                logger.info("L3: Saved merged solution %s for finding %s", memory_id, finding["id"])

    except Exception as exc:
        logger.warning("L3 auto-save on merge failed (non-critical): %s", exc)


async def _trigger_ci_pipelines() -> None:
    """Trigger configured CI/CD pipelines after a Gerrit merge.

    Phase 5-6 (#multi-account-forge): GitHub + GitLab token + URL reads
    run through :func:`backend.git_credentials.pick_default` so operator-
    added ``git_accounts`` rows are honoured. Resolver falls back to the
    legacy shim (``settings.github_token`` / ``settings.gitlab_token`` /
    ``settings.gitlab_url``) when the table is empty.
    """
    from backend.git_credentials import pick_default
    gh_account = await pick_default("github") if settings.ci_github_actions_enabled else None
    gh_token = (gh_account or {}).get("token") or ""
    if settings.ci_github_actions_enabled and gh_token:
        try:
            import os as _os
            gh_env = {**_os.environ, "GH_TOKEN": gh_token}
            proc = await asyncio.create_subprocess_exec(
                "gh", "workflow", "run", "ci.yml", "-r", "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=gh_env,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "GitHub Actions workflow triggered")
            else:
                logger.warning("GitHub Actions trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("GitHub Actions trigger error: %s", exc)

    if settings.ci_jenkins_enabled and settings.ci_jenkins_url:
        # Auth via stdin -K config to keep token out of argv (visible in `ps`).
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST",
                f"{settings.ci_jenkins_url}/build",
                "-K", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            user = (settings.ci_jenkins_user or "").replace('"', '\\"')
            tok = (settings.ci_jenkins_api_token or "").replace('"', '\\"')
            cfg = f'user = "{user}:{tok}"\n'.encode()
            try:
                await asyncio.wait_for(proc.communicate(input=cfg), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "Jenkins build triggered")
            else:
                logger.warning("Jenkins trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("Jenkins trigger error: %s", exc)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception as kill_exc:
                    # Fix-A S6: visibility on orphaned CI subprocesses.
                    logger.warning(
                        "orphaned CI subprocess pid=%s kill failed: %s",
                        proc.pid, kill_exc,
                    )
                    from backend import metrics as _m
                    _m.subprocess_orphan_total.labels(target="jenkins").inc()

    gl_account = await pick_default("gitlab") if settings.ci_gitlab_enabled else None
    gl_token = (gl_account or {}).get("token") or ""
    gl_base = (gl_account or {}).get("instance_url") or settings.gitlab_url or "https://gitlab.com"
    if settings.ci_gitlab_enabled and gl_token:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST",
                f"{gl_base}/api/v4/projects/{settings.gerrit_project.replace('/', '%2F')}/pipeline",
                "-K", "-",
                "-d", "ref=main",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            tok = gl_token.replace('"', '\\"')
            cfg = f'header = "PRIVATE-TOKEN: {tok}"\n'.encode()
            try:
                await asyncio.wait_for(proc.communicate(input=cfg), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode == 0:
                emit_invoke("ci_triggered", "GitLab CI pipeline triggered")
            else:
                logger.warning("GitLab CI trigger failed (rc=%d)", proc.returncode)
        except Exception as exc:
            logger.warning("GitLab CI trigger error: %s", exc)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception as kill_exc:
                    logger.warning(
                        "orphaned CI subprocess pid=%s kill failed: %s",
                        proc.pid, kill_exc,
                    )
                    from backend import metrics as _m
                    _m.subprocess_orphan_total.labels(target="gitlab").inc()


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
    """Receive GitHub issue/PR webhooks — sync status to internal tasks.

    Phase 5-7 (#multi-account-forge): per-instance secret read goes
    through the async resolver so operator-added ``git_accounts`` rows
    are honoured. Falls back to the legacy ``settings.github_webhook_secret``
    via :func:`get_webhook_secret_for_host_async`'s scalar tail.
    """
    import hashlib
    import hmac as _hmac
    try:
        from backend.git_credentials import get_webhook_secret_for_host_async
        secret = await get_webhook_secret_for_host_async("github.com", "github")
    except Exception:
        secret = settings.github_webhook_secret
    if not secret:
        return JSONResponse(status_code=503, content={"detail": "GitHub webhooks not configured"})

    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
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
    """Receive GitLab issue webhooks — sync status to internal tasks.

    Phase 5-7 (#multi-account-forge): per-instance secret read goes
    through the async resolver so operator-added ``git_accounts`` rows
    are honoured (e.g. multiple self-hosted GitLab instances each with
    its own webhook secret).
    """
    import hmac as _hmac
    try:
        from backend.git_credentials import get_webhook_secret_for_host_async
        # Try to identify GitLab instance from header (GitLab 15.x+) or fallback
        gl_instance = request.headers.get("X-Gitlab-Instance", "gitlab.com")
        from urllib.parse import urlparse
        gl_host = urlparse(gl_instance).hostname or gl_instance
        secret = await get_webhook_secret_for_host_async(gl_host, "gitlab")
    except Exception:
        secret = settings.gitlab_webhook_secret
    if not secret:
        return JSONResponse(status_code=503, content={"detail": "GitLab webhooks not configured"})

    token = request.headers.get("X-Gitlab-Token", "")
    if not _hmac.compare_digest(token, secret):
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
    """Receive Jira issue webhooks — sync status to internal tasks.

    Cross-worker coherence: the rotate endpoint in ``integration.py`` mirrors
    ``jira_webhook_secret`` into the Redis-backed SharedKV, but the local
    ``settings`` singleton is per-worker. Overlay SharedKV on each inbound
    webhook so a rotate on worker-A is immediately visible to the verifier
    on worker-B (the ``_SHARED_KV_STR_FIELDS`` registration alone only
    provides the write side; the read side needs this overlay call to close
    the loop). Cheap: a single Redis HGETALL round-trip per webhook, which
    is already guarded by try/except inside the overlay helper.

    Phase 5-8 (#multi-account-forge): per-instance secret read goes through
    :func:`backend.git_credentials.get_webhook_secret_for_host_async` so
    operator-added ``git_accounts(platform='jira')`` rows are honoured
    (including the auto-migrated ``ga-legacy-jira-*`` row from row 5-5).
    The helper's platform-scoped scalar tail falls back to
    ``settings.jira_webhook_secret`` so single-instance deployments with
    only the legacy scalar configured keep authenticating.

    Tenant isolation: the resolver scopes by ``current_tenant_id()``,
    defaulting to ``t-default`` at webhook time (no user session yet). A
    tenant-A JIRA account's ``webhook_secret`` sits in a ``tenant_id='t-A'``
    row and is not visible to this default-tenant lookup — so tenant A's
    credential can't leak out via the shared webhook endpoint.
    """
    import hmac as _hmac
    from backend.routers.integration import _overlay_runtime_settings
    _overlay_runtime_settings()

    try:
        from backend.git_credentials import get_webhook_secret_for_host_async
        # JIRA has no pre-verify host signal (bearer token auth, no
        # HMAC-over-body). Use an empty host so the resolver skips the
        # per-host loop and goes straight to ``pick_default('jira')``
        # then scalar fallback.
        secret = await get_webhook_secret_for_host_async("", "jira")
    except Exception:
        secret = settings.jira_webhook_secret
    if not secret:
        return JSONResponse(status_code=503, content={"detail": "Jira webhooks not configured"})

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not _hmac.compare_digest(auth[7:], secret):
        return JSONResponse(status_code=401, content={"detail": "Invalid token"})

    event = await request.json()

    # Y-prep.3 (#289) — automation dispatcher fires for ALL events regardless
    # of whether an internal Task matches the issue key. The existing
    # status-sync path below only fires when there IS a match; the two paths
    # are independent. Dispatcher failures are logged but must not block the
    # status-sync path (best-effort).
    try:
        await _on_jira_event(event)
    except Exception as exc:
        logger.warning("_on_jira_event dispatch failed (non-critical): %s", exc)

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JIRA inbound event dispatcher (Y-prep.3 / #289)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _on_jira_event(event: dict) -> None:
    """Parse JIRA ``webhookEvent`` and route to the matching handler.

    JIRA Cloud sends a top-level ``webhookEvent`` string identifying the
    event kind (`jira:issue_created` / `jira:issue_updated` /
    `comment_created` / `comment_updated` / …). This dispatcher normalises
    that string, scopes the request to a tenant context (``t-default``
    today, real tenant after Y4), and routes to the matching
    ``_on_jira_*`` handler.

    Tenant context (Y-prep.3 / Y4 seam): the inbound JIRA webhook is
    authenticated via a shared secret — there is no user session, hence
    no ``require_tenant`` dependency. We explicitly set
    ``set_tenant_id("t-default")`` here so the downstream ``audit.log``
    (and any other tenant-scoped DB write) inherits a well-defined
    tenant id instead of silently falling through to the library
    default. Y4 will swap this one line for
    ``set_tenant_id(derive_tenant_from_event(event))`` once per-tenant
    JIRA instances land. We capture+restore the prior tenant in a
    ``finally`` so this dispatcher stays reentrancy-safe if it ever runs
    inside a request that DID set a tenant first (e.g. an admin replay
    endpoint).

    Module-global state audit (SOP Step 1, qualified answer #3): the
    tenant is a ``contextvars.ContextVar`` — task-local by design.
    Each request/task gets its own copy, so cross-worker AND
    cross-request interference is impossible. No module-level cache
    introduced.

    Read-after-write timing audit: no new parallelism. The dispatcher
    awaits each handler in-order, the handlers' side effects
    (``bus.publish``, ``audit.log``, ``_package_merged_artifacts`` spawn,
    ``intent_bridge.on_intake_queued``) are each self-serialised and
    independent of the subsequent status-sync path in ``jira_webhook``.
    """
    webhook_event = (event.get("webhookEvent") or "").strip()
    if not webhook_event:
        logger.debug("JIRA webhook: missing webhookEvent field; ignoring")
        return

    # Comment events carry an additional `comment.*` sub-event indicator in
    # some JIRA versions; prefer the top-level string when present.
    logger.info("JIRA webhook event: %s", webhook_event)

    from backend.db_context import set_tenant_id, current_tenant_id
    prior_tenant = current_tenant_id()
    try:
        set_tenant_id(prior_tenant or "t-default")
        if webhook_event == "comment_created":
            await _on_jira_comment_created(event)
        elif webhook_event == "comment_updated":
            # Same shape as `comment_created`; route to the same handler so
            # that edited commands are re-evaluated. Negative-path filters
            # (e.g. "only `/command` lines trigger") live inside the handler.
            await _on_jira_comment_created(event)
        elif webhook_event == "jira:issue_updated":
            await _on_jira_issue_updated(event)
        elif webhook_event == "jira:issue_created":
            await _on_jira_issue_created(event)
        else:
            logger.debug("JIRA webhook: unhandled event type %r", webhook_event)
    finally:
        set_tenant_id(prior_tenant)


async def _on_jira_comment_created(event: dict) -> None:
    """Route a JIRA ``comment_created`` event via ``jira_event_router``.

    The real handler lives in ``backend.jira_event_router`` so dispatcher
    and action logic stay in separate files (eases test isolation and
    keeps ``webhooks.py`` focused on transport concerns).
    """
    from backend import jira_event_router
    await jira_event_router.handle_comment_created(event)


async def _on_jira_issue_updated(event: dict) -> None:
    """Route a JIRA ``jira:issue_updated`` event via ``jira_event_router``."""
    from backend import jira_event_router
    await jira_event_router.handle_issue_updated(event)


async def _on_jira_issue_created(event: dict) -> None:
    """Route a JIRA ``jira:issue_created`` event via ``jira_event_router``."""
    from backend import jira_event_router
    await jira_event_router.handle_issue_created(event)

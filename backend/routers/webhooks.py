"""Webhook endpoints for external system integrations.

Currently supports Gerrit Code Review events:
- ``patchset-created`` → triggers AI Reviewer agent + OP-714 proactive
  merger check (mergeable=false fires merger before any Submit attempt).
- ``comment-added`` with -1 → notifies coder agent to fix
- ``change-merged`` → triggers replication to GitHub/GitLab
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import uuid

import asyncpg
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.config import settings
from backend.db_pool import get_conn, get_pool
from backend.email_delivery.webhooks import (
    EmailFeedbackEvent,
    normalize_email_webhook_provider,
    parse_email_feedback_events,
)
from backend.events import emit_invoke, emit_task_update
from backend.integrations.jira_to_graphiti_webhook import (
    GraphitiIngestionWebhookUnreachable,
    GraphitiWebhookConfig,
    GraphitiWebhookConfigError,
    handle_jira_webhook,
)
from backend.models import (
    TaskStatus,
)
from backend.stripe_webhooks import (
    StripeWebhookEvent,
    parse_stripe_webhook_event,
    sync_stripe_subscription_state,
    verify_stripe_webhook_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/jira/graphiti")
async def jira_graphiti_webhook(request: Request):
    """Receive JIRA changelog webhooks and forward them to Graphiti."""
    try:
        config = GraphitiWebhookConfig.from_env()
    except GraphitiWebhookConfigError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    raw_body = await request.body()
    if len(raw_body) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    try:
        result = handle_jira_webhook(
            body,
            authorization=request.headers.get("Authorization", ""),
            config=config,
        )
    except GraphitiIngestionWebhookUnreachable as exc:
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    if result.get("status") == "rejected":
        return JSONResponse(status_code=401, content={"detail": result["reason"]})
    return result


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


@router.post("/stripe")
async def stripe_webhook(request: Request):
    """Receive Stripe billing webhooks for FS.8.

    ``settings.stripe_webhook_secret`` is env/runtime configuration, so
    every worker independently verifies against the same source value
    without writing module-global state. FS.8.4 subscription lifecycle
    events are then persisted through PG ``provisioned_billing``.
    """
    secret = settings.stripe_webhook_secret
    if not secret:
        return JSONResponse(
            status_code=503,
            content={"detail": "Stripe webhooks not configured"},
        )

    raw_body = await request.body()
    if len(raw_body) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})

    try:
        verify_stripe_webhook_signature(
            raw_body,
            request.headers.get("Stripe-Signature", ""),
            secret,
        )
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    try:
        event = parse_stripe_webhook_event(body)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    await _on_stripe_webhook_event(event)

    return {
        "status": "ok",
        "event": event.to_dict(),
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


async def _on_stripe_webhook_event(event: StripeWebhookEvent) -> None:
    """Route a verified Stripe event into the FS.8 billing state sync."""
    synced = await sync_stripe_subscription_state(event)
    logger.info(
        "stripe_webhook event_id=%s type=%s object=%s synced=%s",
        event.event_id,
        event.event_type,
        event.object_type,
        synced,
    )


@router.post("/gerrit")
async def gerrit_webhook(
    request: Request,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Receive Gerrit events and trigger appropriate actions.

    Auth model (OP-715 cleanup of OP-714):
    Gerrit's webhooks plugin v3.13.5 has NO support for any form of
    auth metadata — its config docs at ``/plugins/webhooks/Documentation/
    config.html`` contain zero references to ``header``, ``auth``,
    ``bearer``, ``signature``, ``secret`` or ``algorithm``. The plugin
    just POSTs the event body. So OP-714's attempt to require
    ``Authorization: Bearer`` + ``X-Jira-Webhook-Secret`` headers via
    ``require_operator`` + ``_verify_jira_signature`` was structurally
    impossible to satisfy and 401'd every real event.

    OP-715 moves the proactive merger trigger duty to the existing
    stream-events SSH daemon at :mod:`backend.agents.gerrit_jira_bridge`,
    which is auth'd at the SSH protocol layer. This webhook handler
    stays in place to catch any events the daemon might miss, but
    does NOT enforce header-level auth — the security boundary for
    this endpoint is the Caddy / Cloudflare zero-trust tunnel
    upstream of the backend.

    Event handling:
      * ``patchset-created`` — fires the existing AI reviewer task
        creation in :func:`_on_patchset_created` (OP-713 will rewire
        this to a real LLM review). The proactive merger trigger that
        OP-714 wired here is now invoked by the daemon; we keep
        :func:`_proactive_merger_check` exported so the daemon and a
        future webhook-auth fix can share it.
      * ``comment-added`` with -1 — notifies coder agent.
      * ``change-merged`` — replication trigger.

    Phase-3-Runtime-v2 SP-3.1: handler takes a pool-backed
    ``asyncpg.Connection`` so the agent-spawn code path inside
    ``_on_patchset_created`` can persist the spawned reviewer via
    the ported ``db.upsert_agent`` API. Background tasks started from
    here acquire their OWN conn since the request scope ends before
    they run.
    """
    if not settings.gerrit_enabled:
        return JSONResponse(status_code=503, content={"detail": "Gerrit integration disabled"})

    raw_body = await request.body()
    # Reject obviously oversized payloads (DoS guard) — Gerrit events are <64KB.
    if len(raw_body) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Payload too large"})

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Invalid JSON"})

    event_type = body.get("type", "")
    logger.info("Gerrit webhook: type=%s", event_type)

    if event_type == "patchset-created":
        # OP-714 Phase 2: proactive merger check runs in the background
        # so the webhook returns fast (Gerrit retries on slow responses
        # and rate-limits otherwise). Errors inside the task are caught
        # there and never propagate.
        asyncio.create_task(_proactive_merger_check(body))
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
    """A new patchset was pushed — kick off the OP-713 AI Reviewer.

    The synchronous portion (loop-prevention + throttle + L2 notify)
    runs inline so the webhook returns in <100 ms; the LLM review
    itself is fire-and-forget via :func:`_run_ai_review` because Gerrit
    rate-limits webhook senders that take >5 s to respond.
    """
    change = event.get("change", {})
    patchset = event.get("patchSet", {})

    change_id = change.get("id", "")
    change_number = str(change.get("number") or "")
    change_subject = change.get("subject", "")
    project = change.get("project", "")
    commit = patchset.get("revision", "")
    uploader = patchset.get("uploader", {}) or {}
    uploader_name = uploader.get("name", "unknown")
    insertions = int(patchset.get("sizeInsertions") or 0)
    deletions = int(patchset.get("sizeDeletions") or 0)

    logger.info(
        "Patchset created: change=%s subject=%s commit=%s uploader=%s loc=%d",
        change_id, change_subject, commit[:8] if commit else "",
        uploader_name, insertions + deletions,
    )

    # Loop prevention — never review patchsets the merger bot itself
    # uploaded (the conflict-resolution patchsets land via the existing
    # merger pipeline and carry their own audit trail).
    if _is_merger_uploader(uploader):
        logger.info(
            "ai_reviewer skip change=%s reason=uploader_is_merger",
            change_id,
        )
        return

    # Idempotency throttle — same (change_id, revision) within 24 h
    # produces only one review. Multi-worker dedup is best-effort; see
    # ``ai_reviewer._THROTTLE`` for the rationale.
    from backend.agents import ai_reviewer as _ai_reviewer
    if _ai_reviewer.should_skip_recent(change_id, commit):
        logger.info(
            "ai_reviewer skip change=%s rev=%s reason=throttle_24h",
            change_id, commit[:8] if commit else "",
        )
        return
    _ai_reviewer.mark_reviewed(change_id, commit)

    # L2 notification: humans want to see the patchset land before the
    # review comment arrives, since the LLM call can take a few seconds.
    from backend.notifications import notify
    await notify(
        "warning", f"New patchset: {change_subject}",
        message=f"Change {change_id} by {uploader_name} — commit {commit[:8] if commit else ''}",
        source="gerrit",
        action_url=f"{settings.gerrit_url}/c/{change_id}" if settings.gerrit_url else None,
        action_label="Review in Gerrit",
    )

    # Background — runs after the webhook returns so Gerrit gets a
    # fast 200 and isn't rate-limited.
    asyncio.create_task(_run_ai_review(
        change_id=change_id,
        change_number=change_number,
        revision=commit,
        project=project,
        subject=change_subject,
        insertions=insertions,
        deletions=deletions,
    ))


async def _run_ai_review(
    *,
    change_id: str,
    change_number: str,
    revision: str,
    project: str,
    subject: str,
    insertions: int,
    deletions: int,
) -> None:
    """Background task: route → LLM-review → post Code-Review → record cost.

    Errors swallow at every step. The webhook already returned 200 so a
    raised exception here would only spam the backend log. Each
    failure mode logs at WARNING with enough context for triage.
    """
    if not revision:
        logger.warning(
            "ai_reviewer: change=%s missing revision SHA, skipping",
            change_id,
        )
        return

    try:
        from backend.agents import ai_reviewer
        from backend.gerrit import gerrit_client

        files_meta, fetched_subject = await _fetch_change_files(
            change_number or change_id, project,
        )
        files = [f for f in files_meta if f and f != "/COMMIT_MSG"]
        # Prefer the gerrit-provided LOC counts when available, falling
        # back to the patchset-event totals (some Gerrit setups omit
        # sizeInsertions/sizeDeletions on the event payload).
        loc_total = insertions + deletions

        # Size-cap fast path: skip the LLM entirely.
        if ai_reviewer.is_too_large(
            insertions=insertions,
            deletions=deletions,
            limit=ai_reviewer.DEFAULT_REVIEW_DIFF_LIMIT_LOC,
        ):
            chosen = ai_reviewer.route_model(files=files)
            logger.info(
                "ai_reviewer_invoked change=%s model=%s loc=%d skip=too_large",
                change_id, chosen, loc_total,
            )
            msg = ai_reviewer.too_large_message(
                insertions=insertions,
                deletions=deletions,
                model_id=chosen,
            )
            result = await gerrit_client.post_review(
                commit=revision,
                message=msg,
                labels={"Code-Review": 0},
                project=project,
            )
            if "error" in result:
                logger.warning(
                    "ai_reviewer post_review (too-large) failed: %s",
                    result["error"],
                )
            else:
                logger.info(
                    "ai_reviewer too_large change=%s loc=%d model=%s",
                    change_id, loc_total, chosen,
                )
            return

        diff = await _fetch_patchset_diff(revision, project)

        chosen = ai_reviewer.route_model(diff=diff, files=files)
        # OP-801 — emit a single greppable invocation line carrying the
        # picked tier so operators (and the OP-801 AC verification step)
        # can confirm risk-tier routing fired before the LLM round-trip.
        logger.info(
            "ai_reviewer_invoked change=%s model=%s loc=%d",
            change_id, chosen, loc_total,
        )
        # ``invoke_chat`` is sync — push it off the event loop so the
        # gerrit pool isn't blocked while the LLM is thinking.
        review = await asyncio.to_thread(
            ai_reviewer.review_patchset,
            diff,
            chosen,
            files=tuple(files),
            subject=subject or fetched_subject or "",
            insertions=insertions,
            deletions=deletions,
        )

        labels = {"Code-Review": review.score}
        post = await gerrit_client.post_review(
            commit=revision,
            message=review.message,
            labels=labels,
            project=project,
        )
        if "error" in post:
            logger.warning(
                "ai_reviewer post_review failed change=%s err=%s",
                change_id, post["error"],
            )
        else:
            logger.info(
                "ai_reviewer posted change=%s rev=%s model=%s score=%+d "
                "tokens=%d/%d cost=$%.4f",
                change_id, revision[:8], review.model_id,
                review.score, review.input_tokens, review.output_tokens,
                review.cost_usd,
            )

        # Best-effort billing-event recording. The runner already wires
        # token_usage on its own LLM path; this row is the AI-Reviewer-
        # specific one keyed by ``model_id`` for the cost dashboard.
        if review.model_id and (review.input_tokens or review.output_tokens):
            try:
                from backend.billing_usage import record_llm_call
                await record_llm_call(
                    model=review.model_id,
                    input_tokens=review.input_tokens,
                    output_tokens=review.output_tokens,
                    cost_usd=review.cost_usd,
                    provider="anthropic",
                    product_line="ai_reviewer",
                    metadata={
                        "change_id": change_id,
                        "revision": revision,
                        "score": review.score,
                        "ticket": "OP-713",
                    },
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "ai_reviewer billing record failed (non-critical): %s",
                    exc,
                )
    except Exception as exc:
        logger.exception(
            "ai_reviewer unhandled error change=%s err=%s", change_id, exc,
        )


async def _fetch_change_files(
    change_id: str, project: str,
) -> tuple[list[str], str]:
    """Best-effort fetch of file paths + subject for a Gerrit change.

    Returns ``([], "")`` if Gerrit is misconfigured or the query fails
    — the caller treats an empty file list as "default routing"
    (haiku) which is the cheapest fallback.
    """
    try:
        from backend.gerrit import gerrit_client
        data = await gerrit_client.query_change(change_id, project)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "ai_reviewer.query_change err=%s change=%s",
            exc, change_id,
        )
        return ([], "")
    if not data:
        return ([], "")
    cps = data.get("currentPatchSet") or {}
    files = []
    for f in cps.get("files") or []:
        path = f.get("file") or ""
        if path:
            files.append(path)
    return (files, str(data.get("subject") or ""))


async def _fetch_patchset_diff(revision: str, project: str) -> str:
    """Fetch a unified diff for a patchset SHA from a local mirror.

    Tries ``git show <revision>`` against the workspace's main repo;
    returns ``""`` if the SHA isn't available locally (in which case
    the LLM gets a file-list-only prompt). Truncated to 64 KB so a
    pathological diff doesn't blow up the prompt budget.
    """
    try:
        from backend.workspace import _MAIN_REPO, _run
        rc, out, _err = await _run(
            f'git show --format= --no-color {revision}',
            cwd=_MAIN_REPO,
        )
        if rc != 0:
            return ""
        diff = out or ""
        if len(diff) > 65_536:
            diff = diff[:65_536] + "\n... [diff truncated at 64 KB]"
        return diff
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("ai_reviewer.fetch_diff err=%s rev=%s",
                       exc, revision[:8] if revision else "")
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OP-801 — AI Reviewer invocation from the bridge daemon
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _ai_reviewer_check(event: dict) -> None:
    """OP-801 — bridge-daemon-callable AI Reviewer pipeline.

    Self-contained coroutine equivalent of :func:`_on_patchset_created`
    but with the actual review *awaited* (no ``asyncio.create_task``)
    so a caller running ``asyncio.run`` inside a daemon thread sees the
    whole pipeline complete before the thread exits. The HTTP path
    keeps using :func:`_on_patchset_created` so its existing fast-200
    contract is preserved.

    OP-801 background — Gerrit's webhooks plugin v3.13.5 has no
    documented auth surface (Lesson L-OP-713 confirmed neither
    ``secret`` nor any signature header are honoured), so the
    ``/webhooks/gerrit`` endpoint is unreachable from the plugin's
    auth-gated POST. OP-715 already moved the proactive merger trigger
    to the SSH stream-events bridge; this coroutine is the matching
    AI Reviewer migration. We keep ONE implementation so a future
    plugin-auth fix and the daemon stay in sync.

    Skip behaviour mirrors :func:`_on_patchset_created` exactly:

      1. Loop prevention — uploader=merger-agent-bot returns early
         (the merger's own conflict resolution patchsets must not
         retrigger an AI review).
      2. Idempotency — the (change_id, revision) throttle in
         :mod:`backend.agents.ai_reviewer` skips repeats inside the
         24 h TTL so a daemon restart that replays recent events
         does NOT double-review.
      3. L2 notification — operator dashboards still see the
         "New patchset" warning so the heads-up arrives before the
         LLM finishes.

    Errors are caught at the outermost level — daemon survival is
    higher priority than reporting one missed review (next patchset
    re-runs the pipeline).
    """
    try:
        change = event.get("change") or {}
        patchset = event.get("patchSet") or {}

        change_id = str(change.get("id") or "")
        change_number = str(change.get("number") or "")
        change_subject = str(change.get("subject") or "")
        project = str(change.get("project") or "")
        commit = str(patchset.get("revision") or "")
        uploader = patchset.get("uploader") or {}
        uploader_name = uploader.get("name", "unknown")
        insertions = int(patchset.get("sizeInsertions") or 0)
        deletions = int(patchset.get("sizeDeletions") or 0)

        # 1. Loop prevention — never review the merger's own resolution
        #    patchsets (would feedback-loop with OP-714 conflict fixes).
        if _is_merger_uploader(uploader):
            logger.info(
                "ai_reviewer_skip change=%s reason=uploader_is_merger",
                change_id,
            )
            return

        # 2. Idempotency throttle — same (change_id, revision) within
        #    24 h gets ONE review. Multi-worker dedup is best-effort;
        #    see ``ai_reviewer._THROTTLE`` for the rationale.
        from backend.agents import ai_reviewer as _ai_reviewer
        if _ai_reviewer.should_skip_recent(change_id, commit):
            logger.info(
                "ai_reviewer_skip change=%s rev=%s reason=throttle_24h",
                change_id, commit[:8] if commit else "",
            )
            return
        _ai_reviewer.mark_reviewed(change_id, commit)

        # 3. L2 notification — operator heads-up before the LLM call.
        #    Errors here must NOT block the actual review.
        try:
            from backend.notifications import notify
            await notify(
                "warning",
                f"New patchset: {change_subject}",
                message=(
                    f"Change {change_id} by {uploader_name} — "
                    f"commit {commit[:8] if commit else ''}"
                ),
                source="gerrit",
                action_url=(
                    f"{settings.gerrit_url}/c/{change_id}"
                    if settings.gerrit_url else None
                ),
                action_label="Review in Gerrit",
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "ai_reviewer notify failed change=%s err=%s",
                change_id, exc,
            )

        # 4. Run the review (awaited, not create_task — see docstring).
        await _run_ai_review(
            change_id=change_id,
            change_number=change_number,
            revision=commit,
            project=project,
            subject=change_subject,
            insertions=insertions,
            deletions=deletions,
        )
    except Exception as exc:  # pragma: no cover — top-level safety net
        logger.exception(
            "_ai_reviewer_check unhandled error: %s", exc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  OP-714 — proactive merger trigger on patchset-created with mergeable=false
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MERGER_BOT_NAMES = ("merger-agent-bot",)
_PROACTIVE_HASHTAG_PREFIX = "Merger-Proactive-PS"
_RESOLVED_HASHTAG = "Merge-Conflict-Resolved"

# OP-718 — daemon → backend HTTP delegate. Default targets the local
# backend container; operators override via OMNISIGHT_BACKEND_URL when
# the daemon ships on a different host than the FastAPI app.
_MERGER_HTTP_DEFAULT_BACKEND = "http://localhost:8000"
_MERGER_HTTP_PATH = "/api/v1/orchestrator/merge-conflict"
_MERGER_HTTP_TIMEOUT_SECONDS = 120.0


async def _post_merge_conflict_to_backend(
    task: "MergeConflictTask",  # noqa: F821 — forward ref to local import
) -> dict:
    """OP-718 — delegate merger invocation to the running backend over HTTP.

    Replaces the OP-714/OP-717 in-process ``on_merge_conflict_webhook``
    call. The daemon process (gerrit_jira_bridge) does not initialise
    the asyncpg pool, LLM provider, or JIRA client, so the in-process
    path short-circuits every backend-init-dependent step (audit
    logging, abstain-ticket creation, hashtag write). Routing the
    invocation back to the already-initialised backend over HTTP keeps
    a single source of truth for those deps.

    Auth model mirrors the merger endpoint at
    ``backend.routers.orchestrator.merge_conflict_endpoint``:

      * ``Authorization: Bearer <OMNISIGHT_GERRIT_WEBHOOK_API_KEY>`` —
        satisfies ``require_operator`` (an API-key bearer).
      * ``X-Jira-Webhook-Secret: <OMNISIGHT_JIRA_WEBHOOK_SECRET>`` —
        satisfies ``_verify_jira_signature``.

    Returns a dict that always carries a ``reason`` key so callers can
    log a uniform ``merger_outcome reason=...`` line. On infrastructure
    failure (missing creds / network / non-2xx), the synthetic
    ``reason=merger_http_*`` value flags the problem without raising.
    """
    api_key = (os.environ.get("OMNISIGHT_GERRIT_WEBHOOK_API_KEY") or "").strip()
    jira_secret = (
        os.environ.get("OMNISIGHT_JIRA_WEBHOOK_SECRET") or ""
    ).strip()
    if not api_key or not jira_secret:
        return {
            "ok": False,
            "reason": "merger_http_missing_credentials",
            "detail": (
                "OMNISIGHT_GERRIT_WEBHOOK_API_KEY and "
                "OMNISIGHT_JIRA_WEBHOOK_SECRET must be set in the "
                "daemon environment to delegate to backend"
            ),
        }

    backend_url = (
        os.environ.get("OMNISIGHT_BACKEND_URL")
        or _MERGER_HTTP_DEFAULT_BACKEND
    ).rstrip("/")
    url = f"{backend_url}{_MERGER_HTTP_PATH}"

    payload = dataclasses.asdict(task)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Jira-Webhook-Secret": jira_secret,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=_MERGER_HTTP_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "reason": "merger_http_request_error",
            "detail": f"{type(exc).__name__}: {exc}"[:300],
        }

    if response.status_code != 200:
        return {
            "ok": False,
            "reason": f"merger_http_status_{response.status_code}",
            "detail": response.text[:300],
        }

    try:
        body = response.json()
    except ValueError as exc:
        return {
            "ok": False,
            "reason": "merger_http_invalid_json",
            "detail": str(exc)[:200],
        }
    if not isinstance(body, dict):
        return {
            "ok": False,
            "reason": "merger_http_invalid_body",
            "detail": "response body is not a JSON object",
        }
    body.setdefault("reason", "unknown")
    return body


def _is_merger_uploader(uploader: dict) -> bool:
    """Loop prevention — recognise merger-agent-bot's own patchset uploads."""
    name = (uploader.get("name") or "").lower()
    email = (uploader.get("email") or "").lower()
    username = (uploader.get("username") or "").lower()
    for marker in _MERGER_BOT_NAMES:
        if marker in name or marker in email or marker in username:
            return True
    return False


async def _proactive_merger_check(event: dict) -> None:
    """OP-714 Phase 2 — invoke merger preemptively when a fresh patchset
    arrives already mergeable=false.

    Background coroutine (fire-and-forget). MUST NOT raise — the caller
    already returned the webhook 200 to Gerrit, and a thrown exception
    here would just spam the backend log without operator visibility.

    Decision flow (each step logs ``merger_proactive_decision`` for
    observability; caller can grep ``skip_reason=`` for triage):

      1. Skip if uploader is ``merger-agent-bot`` (loop prevention —
         the merger's own resolution patchset must not retrigger).
      2. Query the change for hashtags. Skip if any
         ``Merger-Proactive-PS*`` is set (already attempted) or if
         ``Merge-Conflict-Resolved`` is set (merger succeeded; awaiting
         human +2).
      3. Skip if change is ``work_in_progress`` or private.
      4. Query mergeability. Skip if ``mergeable=true`` or commit is
         already merged.
      5. Mark the change with ``Merger-Proactive-PS<n>`` BEFORE
         invoking the merger so concurrent events don't double-fire.
      6. Invoke ``merge_arbiter.on_merge_conflict_webhook`` with a
         synthesised :class:`MergeConflictTask`. ``conflict_text`` is
         left empty for the MVP — the merger short-circuits to
         ``refused_no_conflict`` and the arbiter routes the change to
         the abstain JIRA ticket pipeline, which still gives the
         operator a visible notification ("merger detected mergeable=
         false on change X — please rebase locally"). Real conflict_
         text enrichment via worktree merge is OP-715 follow-up scope.
    """
    try:
        change = event.get("change") or {}
        patchset = event.get("patchSet") or {}

        change_id_str = str(change.get("id") or "")
        change_number = change.get("number") or ""
        project = str(change.get("project") or "")
        rev = str(patchset.get("revision") or "")
        ps_number = patchset.get("number") or 0
        uploader = patchset.get("uploader") or {}
        log_prefix = (
            f"merger_proactive_decision change={change_number} ps={ps_number}"
        )

        # ── 1. Loop prevention ────────────────────────────────────
        if _is_merger_uploader(uploader):
            logger.info("%s skip_reason=uploader_is_merger", log_prefix)
            return

        # ── 2/3. Hashtag + WIP check via gerrit query ─────────────
        from backend.gerrit import gerrit_client
        try:
            change_data = await gerrit_client.query_change(
                str(change_number) if change_number else change_id_str,
                project,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("%s skip_reason=gerrit_query_error err=%s",
                           log_prefix, exc)
            return
        if change_data is None:
            logger.warning("%s skip_reason=gerrit_query_no_result", log_prefix)
            return

        hashtags = change_data.get("hashtags") or []
        if any(h.startswith(_PROACTIVE_HASHTAG_PREFIX) for h in hashtags):
            logger.info("%s skip_reason=hashtag_already_attempted hashtags=%s",
                        log_prefix, hashtags)
            return
        if _RESOLVED_HASHTAG in hashtags:
            logger.info("%s skip_reason=hashtag_resolved hashtags=%s",
                        log_prefix, hashtags)
            return

        if change_data.get("wip") or change_data.get("workInProgress"):
            logger.info("%s skip_reason=work_in_progress", log_prefix)
            return
        if change_data.get("private"):
            logger.info("%s skip_reason=private_change", log_prefix)
            return

        # ── 4. Mergeability check via local 3-way merge (OP-717) ──
        # Replaces the OP-714 _fetch_mergeable REST call with a local
        # `git merge --no-commit --no-ff` against origin/develop. The
        # local-merge approach gives us BOTH the mergeable boolean AND
        # the conflict markers in one shot, sidestepping the OP-716
        # LDAP auth gap on the REST mergeable endpoint.
        from backend.agents.conflict_enrichment import enrich_via_local_merge
        result = await enrich_via_local_merge(
            change_number=change_number,
            patchset_revision=rev,
            project=project,
            patchset_number=ps_number,
        )
        if result.error:
            logger.warning("%s skip_reason=enrichment_error err=%s",
                           log_prefix, result.error[:200])
            return
        if result.mergeable:
            logger.info("%s skip_reason=mergeable", log_prefix)
            return
        if result.too_many:
            logger.info(
                "%s skip_reason=too_many_conflicts files=%d",
                log_prefix, len(result.conflict_files),
            )
            # Still mark the hashtag so we don't re-attempt on the same PS.
            try:
                await gerrit_client.add_hashtag(
                    change_id=str(change_number),
                    project=project,
                    hashtag=f"{_PROACTIVE_HASHTAG_PREFIX}{ps_number}",
                )
            except Exception:
                pass
            return
        if not result.conflict_files:
            logger.warning(
                "%s skip_reason=no_conflict_files_returned", log_prefix,
            )
            return

        # ── 5. Mark before invoke (throttle) ──────────────────────
        marker_hashtag = f"{_PROACTIVE_HASHTAG_PREFIX}{ps_number}"
        try:
            await gerrit_client.add_hashtag(
                change_id=str(change_number),
                project=project,
                hashtag=marker_hashtag,
            )
        except Exception as exc:
            # Non-fatal — at worst we double-fire merger on the same PS.
            logger.warning("%s set_hashtag_failed hashtag=%s err=%s",
                           log_prefix, marker_hashtag, exc)

        # ── 6. Invoke merger via backend HTTP delegate (OP-718) ───
        # OP-717 built the MergeConflictTask from the FIRST conflict
        # file (alphabetical); additional_files lists the rest so the
        # merger has scope context. OP-718 replaces the prior
        # in-process ``on_merge_conflict_webhook`` call with an httpx
        # POST to the running backend so the merger pipeline runs in a
        # context that already has the asyncpg pool, LLM provider, and
        # JIRA client initialised — closing the runtime-init gap that
        # the in-process daemon path hit (every backend-init-dependent
        # step short-circuited: audit log, LLM call, JIRA write,
        # Gerrit hashtag).
        from backend.merge_arbiter import MergeConflictTask

        subject = change_data.get("subject", "")
        ticket_match = re.search(r"\bOP-\d+\b", subject)
        jira_ticket = ticket_match.group(0) if ticket_match else ""

        primary = result.conflict_files[0]
        additional = [cf.path for cf in result.conflict_files[1:]]

        task = MergeConflictTask(
            change_id=str(change_number),
            project=project,
            file_path=primary.path,
            conflict_text=primary.conflict_text,
            file_context=primary.file_context,
            head_commit_message=result.head_subject or subject,
            incoming_commit_message=result.incoming_subject or subject,
            patchset_revision=rev,
            jira_ticket=jira_ticket,
            additional_files=additional,
        )

        logger.info(
            "%s decision=invoke_merger jira_ticket=%s primary_file=%s "
            "additional_count=%d",
            log_prefix, jira_ticket or "<none>",
            primary.path, len(additional),
        )
        try:
            outcome = await _post_merge_conflict_to_backend(task)
            outcome_reason = outcome.get("reason", "unknown")
            logger.info(
                "%s merger_outcome reason=%s",
                log_prefix, outcome_reason,
            )
        except Exception as exc:
            logger.exception(
                "%s merger_invocation_failed err=%s", log_prefix, exc,
            )
    except Exception as exc:  # pragma: no cover — final safety net
        logger.exception("merger_proactive_check unhandled error: %s", exc)


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

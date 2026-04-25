"""Tiered Notification Routing System.

Routes events to appropriate channels based on severity level:
  L1 (info)     → system log only
  L2 (warning)  → SSE notification + IM webhook
  L3 (action)   → SSE notification + IM @mention + issue tracker
  L4 (critical) → SSE notification + IM @channel + PagerDuty/SMS
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from backend.config import settings
from backend.events import bus
from backend.models import Notification, NotificationLevel, Severity

logger = logging.getLogger(__name__)


async def notify(
    level: NotificationLevel | str,
    title: str,
    message: str = "",
    source: str = "",
    action_url: str | None = None,
    action_label: str | None = None,
    interactive: bool = False,
    interactive_buttons: list[dict] | None = None,
    interactive_channel: str = "*",
    severity: Severity | str | None = None,
    conn=None,
) -> Notification:
    """Create and route a notification through the tiered system.

    This is the single entry point — all notification-worthy events
    should call this function.

    Phase-3-Runtime-v2 SP-3.4 (2026-04-20): ``conn`` is polymorphic —
    request handlers that hold a ``Depends(get_conn)`` conn can pass
    it through to share the request's pool-scoped connection with the
    notification insert; workers (watchdog, webhooks, agent
    orchestration) call without conn and the function borrows one
    from the pool just for the insert. Matches the
    routers/tasks.py::_persist pattern.

    R9 row 2935 (#315): ``severity`` is the new orthogonal P1/P2/P3
    tag (see :mod:`backend.severity`). When ``None`` (the legacy
    case) the tag column persists as NULL and the dispatcher behaves
    exactly as before — falling back to plain level routing. When set
    it is persisted + re-broadcast on the SSE bus so the frontend
    notification-center can render the per-card severity badge (UI
    delivery is a separate row in R9). The actual tier-fan-out logic
    that consumes the tag (PagerDuty / Jira / ChatOps interactive
    diff per severity) lives in row 2939's ``send_notification(tier,
    severity, payload, interactive=False)`` extension; this row
    establishes the data path so row 2939 has the field to read.
    """
    level_str = level.value if hasattr(level, "value") else level
    severity_str: str | None
    if severity is None:
        severity_str = None
    else:
        severity_str = severity.value if hasattr(severity, "value") else str(severity)
    notif = Notification(
        id=f"notif-{uuid.uuid4().hex[:8]}",
        level=level_str,
        title=title,
        message=message,
        source=source,
        timestamp=datetime.now().isoformat(),
        action_url=action_url,
        action_label=action_label,
        severity=severity_str,
    )

    # 1. Always persist to DB (best-effort — a failed DB write still
    #    publishes to SSE and routes to external channels so the
    #    operator sees the notification; durability recovery is Epic 7's
    #    problem).
    from backend import db
    try:
        if conn is None:
            from backend.db_pool import get_pool
            async with get_pool().acquire() as owned_conn:
                await db.insert_notification(owned_conn, notif.model_dump())
        else:
            await db.insert_notification(conn, notif.model_dump())
    except Exception as exc:
        logger.warning("Failed to persist notification: %s", exc)

    # 2. Always push via SSE (frontend receives all levels)
    bus.publish("notification", {
        "id": notif.id,
        "level": level_str,
        "title": title,
        "message": message,
        "source": source,
        "timestamp": notif.timestamp,
        "action_url": action_url,
        "action_label": action_label,
        # R9 row 2935: severity tag rides alongside level so the
        # frontend can render per-card P1/P2/P3 badges without an
        # extra round-trip. None for legacy callers.
        "severity": severity_str,
    })

    # 3. Route to external channels based on level
    if level_str in ("warning", "action", "critical"):
        asyncio.create_task(_dispatch_external(notif))

    # R1 (#307): interactive mirror to ChatOps bridge. Non-fatal if
    # bridge is unavailable — notification already persisted + SSE'd.
    if interactive:
        asyncio.create_task(_dispatch_chatops(
            notif, interactive_channel, interactive_buttons or [],
        ))

    # 4. Log
    from backend.routers.system import add_system_log
    log_level = {"info": "info", "warning": "warn", "action": "error", "critical": "error"}.get(level_str, "info")
    add_system_log(f"[NOTIFY:{level_str.upper()}] {title}", log_level)

    return notif


async def _dispatch_external(notif: Notification) -> None:
    """Send notification to external channels with retry on failure.

    Tracks dispatch status in DB. Failed dispatches are retried up to
    notification_max_retries with exponential backoff.

    SP-3.4: always runs in a worker context (spawned via
    ``asyncio.create_task`` from notify() after the request conn has
    already been released), so we unconditionally borrow a pool-scoped
    conn for the dispatch-status update. The network calls (slack /
    jira / pagerduty / sms) run OUTSIDE the acquire block so a slow
    webhook doesn't pin a pool connection for the duration of the HTTP
    call.

    R9 row 2936 (#315) — when ``notif.severity`` is set, fan-out is
    driven by :data:`backend.severity.SEVERITY_TIER_MAPPING` instead of
    plain level routing. P1 thus reaches PagerDuty + SMS + Jira + Slack
    @everyone regardless of the underlying ``level`` (which defaults to
    ``critical`` for P1 callers anyway via the level-floor convention).
    Severity-aware payload variants are picked up inside the per-channel
    senders (Slack adds ``@everyone`` mention for Discord; Jira adds the
    ``severity-P1`` label + severity to description).
    """
    from backend import db
    from backend.db_pool import get_pool
    from backend.severity import (
        L2_IM_WEBHOOK,
        L3_JIRA,
        L4_PAGERDUTY,
        L4_SMS,
        tiers_for,
    )
    level = notif.level
    severity = notif.severity.value if notif.severity is not None else None
    severity_tiers = tiers_for(severity) if severity else frozenset()
    errors: list[str] = []
    any_required = False

    # R9 row 2936: tier activation merges severity-driven fan-out with
    # legacy level-based routing. The two ladders are additive — a P1
    # severity activates SMS + PagerDuty + Jira + Slack even if the
    # caller passed level="info"; a level=critical without severity
    # still fires Slack + Jira + PagerDuty as before.
    fire_slack = (
        L2_IM_WEBHOOK in severity_tiers
        or level in ("warning", "action", "critical")
    )
    fire_jira = (
        L3_JIRA in severity_tiers
        or level in ("action", "critical")
    )
    fire_pagerduty = (
        L4_PAGERDUTY in severity_tiers
        or level == "critical"
    )
    fire_sms = L4_SMS in severity_tiers

    # L2+: IM (Slack/Teams/Discord — the webhook URL is opaque to us;
    # P1 mention text uses both Slack ``<!channel>`` and Discord
    # ``@everyone`` so either backend recognises the broadcast).
    if fire_slack and settings.notification_slack_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_slack, "slack")
        if not ok:
            errors.append("slack")

    # L3+: Issue tracker (severity attaches as label + description tag).
    if fire_jira and settings.notification_jira_url:
        any_required = True
        ok = await _send_with_retry(notif, _send_jira, "jira")
        if not ok:
            errors.append("jira")

    # L4: PagerDuty (severity tag rides into the payload).
    if fire_pagerduty and settings.notification_pagerduty_key:
        any_required = True
        ok = await _send_with_retry(notif, _send_pagerduty, "pagerduty")
        if not ok:
            errors.append("pagerduty")

    # L4: SMS — only ever activated by severity tag; no level-only
    # caller reaches this leg (intentional — SMS is reserved for the P1
    # broadcast tier and would otherwise spam on-call for routine
    # critical-level events that PagerDuty already covers).
    if fire_sms and settings.notification_sms_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_sms, "sms")
        if not ok:
            errors.append("sms")

    if not any_required:
        # No external channels configured for this level
        try:
            async with get_pool().acquire() as _conn:
                await db.update_notification_dispatch(
                    _conn, notif.id, "skipped",
                )
        except Exception as exc:
            # Fix-B B2/B6: persistence failure is non-fatal but observable.
            logger.warning("notifications: persist skipped status for %s failed: %s", notif.id, exc)
            from backend import metrics as _m
            _m.persist_failure_total.labels(module="notifications").inc()
        return

    # Update dispatch status in DB (pool conn acquired AFTER network I/O
    # completed, so we don't hold the conn during slack/jira latency).
    try:
        async with get_pool().acquire() as _conn:
            if errors:
                await db.update_notification_dispatch(
                    _conn, notif.id, "failed",
                    attempts=settings.notification_max_retries,
                    error=f"Failed channels: {', '.join(errors)}",
                )
            else:
                await db.update_notification_dispatch(
                    _conn, notif.id, "sent", attempts=1,
                )
    except Exception as exc:
        logger.warning("Failed to update dispatch status for %s: %s", notif.id, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  R1 (#307): ChatOps interactive mirror
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _dispatch_chatops(
    notif: Notification,
    channel: str,
    buttons: list[dict],
) -> None:
    """Forward a notification to the ChatOps bridge as an interactive card."""
    try:
        from backend import chatops_bridge as bridge
        btns = [
            bridge.Button(
                id=str(b.get("id") or ""),
                label=str(b.get("label") or b.get("id") or ""),
                style=str(b.get("style") or "primary"),
                value=str(b.get("value") or ""),
            )
            for b in (buttons or [])
            if b.get("id")
        ]
        await bridge.send_interactive(
            channel, notif.message or notif.title,
            title=f"[{notif.level.upper()}] {notif.title}",
            buttons=btns,
            meta={"notification_id": notif.id, "source": notif.source},
        )
    except Exception as exc:
        logger.warning("ChatOps mirror dispatch failed for %s: %s", notif.id, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase 52: Webhook DLQ retry worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DLQ_RUNNING = False


async def retry_failed_notifications(
    limit: int = 50, conn=None,
) -> dict[str, int]:
    """Scan `dispatch_status='failed'` notifications and re-attempt dispatch.

    Exhausted rows (send_attempts >= max_retries) are marked `'dead'` so the
    next sweep skips them. Returns {retried, recovered, dead}.

    SP-3.4: ``conn`` is polymorphic — called from ``run_dlq_loop``
    (background worker) without conn (auto-acquire), and from tests
    that want to pass ``pg_test_conn`` for savepoint isolation. The
    acquired conn is only used for the scan + dead-mark; the
    per-notification ``_dispatch_external`` call manages its own
    conn so the outer DLQ sweep doesn't hold the pool while external
    HTTP calls run.
    """
    from backend import db
    owned = False
    if conn is None:
        from backend.db_pool import get_pool
        _owner_cm = get_pool().acquire()
        conn = await _owner_cm.__aenter__()
        owned = True
    try:
        rows = await db.list_failed_notifications(conn, limit=limit)
        retried = recovered = dead = 0
        max_retries = settings.notification_max_retries
        for row in rows:
            attempts = int(row.get("send_attempts") or 0)
            if attempts >= max_retries:
                try:
                    await db.update_notification_dispatch(
                        conn, row["id"], "dead", attempts=attempts,
                        error=(row.get("last_error") or "exhausted"),
                    )
                except Exception as exc:
                    logger.warning("DLQ: mark dead failed for %s: %s", row.get("id"), exc)
                    from backend import metrics as _m
                    _m.persist_failure_total.labels(module="notifications").inc()
                dead += 1
                continue
            retried += 1
            try:
                notif = Notification(**{k: row.get(k) for k in (
                    "id", "level", "title", "message", "source", "timestamp",
                    "action_url", "action_label",
                ) if row.get(k) is not None})
            except Exception as exc:
                logger.warning("DLQ: cannot rehydrate %s: %s", row.get("id"), exc)
                continue
            await _dispatch_external(notif)
            # Post-dispatch status check: a dedicated ``get_notification``
            # helper doesn't exist yet (Epic-7 follow-up); skip the
            # recovery counter gracefully so the sweep still reports
            # retried / dead accurately.
            if hasattr(db, "get_notification"):
                try:
                    fresh = await db.get_notification(conn, notif.id)
                    if fresh and fresh.get("dispatch_status") == "sent":
                        recovered += 1
                except Exception as exc:
                    logger.debug("DLQ: post-dispatch status check failed for %s: %s", notif.id, exc)
        return {"retried": retried, "recovered": recovered, "dead": dead}
    finally:
        if owned:
            await _owner_cm.__aexit__(None, None, None)


async def run_dlq_loop() -> None:
    """Background coroutine: periodically retries failed webhook dispatches.

    Interval = notification_retry_backoff (default 30s), capped at 5min.
    Exits cleanly on CancelledError.
    """
    global _DLQ_RUNNING
    if _DLQ_RUNNING:
        return
    _DLQ_RUNNING = True
    interval = max(5, min(300, int(settings.notification_retry_backoff)))
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await retry_failed_notifications()
            except Exception as exc:
                logger.warning("DLQ sweep failed: %s", exc)
    except asyncio.CancelledError:
        pass
    finally:
        _DLQ_RUNNING = False


async def _send_with_retry(notif: Notification, sender, channel: str) -> bool:
    """Retry a sender function with exponential backoff. Returns True on success."""
    max_retries = settings.notification_max_retries
    backoff = settings.notification_retry_backoff

    for attempt in range(1, max_retries + 1):
        try:
            await sender(notif)
            return True
        except Exception as exc:
            logger.warning(
                "Dispatch to %s failed (attempt %d/%d): %s",
                channel, attempt, max_retries, exc,
            )
            if attempt < max_retries:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))  # exponential: 30, 60, 120
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  External dispatchers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _send_slack(notif: Notification) -> None:
    """Send to Slack/Discord via Incoming Webhook.

    R9 row 2936 (#315) — for ``severity=P1`` the broadcast mention
    string carries BOTH Slack ``<!channel>`` and Discord ``@everyone``.
    The opposite-platform syntax renders as plain text on the other
    side (Slack does not interpret ``@everyone``, Discord does not
    interpret ``<!channel>``), so a single payload covers both backends
    without us having to detect the webhook flavour.
    """
    url = settings.notification_slack_webhook
    sev = notif.severity.value if notif.severity is not None else None
    emoji = {"warning": ":warning:", "action": ":rotating_light:", "critical": ":fire:"}.get(notif.level, ":information_source:")
    if sev == "P1":
        emoji = ":rotating_light:"
    mention = ""
    if sev == "P1":
        # Broadcast: Slack `<!channel>` + Discord `@everyone` rendered
        # together so the message broadcasts on whichever IM the
        # webhook actually points at.
        mention = " <!channel> @everyone"
    elif notif.level == "action":
        mention = f" <@{settings.notification_slack_mention}>" if settings.notification_slack_mention else ""
    elif notif.level == "critical":
        mention = " <!channel>"

    sev_tag = f" [severity:{sev}]" if sev else ""
    payload = {
        "text": f"{emoji} *[{notif.level.upper()}]*{sev_tag} {notif.title}{mention}\n{notif.message}",
    }
    if notif.action_url:
        payload["text"] += f"\n<{notif.action_url}|{notif.action_label or 'View'}>"

    import json
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"Slack webhook failed (rc={proc.returncode}) for {notif.id}")


async def _send_jira(notif: Notification) -> None:
    """Create Jira issue via REST API.

    R9 row 2936 (#315) — when ``notif.severity`` is set the issue
    carries (a) a ``severity-<P1|P2|P3>`` label, (b) the severity tag
    in the description prefix, and (c) the priority is forced to
    ``Highest`` for P1 regardless of ``notif.level``. The
    ``severity:P2`` / ``label:blocked`` combo for P2 is row 2937 scope
    and not handled here.
    """
    url = settings.notification_jira_url
    token = settings.notification_jira_token
    project = settings.notification_jira_project
    if not all([url, token, project]):
        return

    import json
    sev = notif.severity.value if notif.severity is not None else None
    fields: dict = {
        "project": {"key": project},
        "summary": f"[{notif.level.upper()}] {notif.title}",
        "description": (f"[severity:{sev}] " if sev else "") + (notif.message or ""),
        "issuetype": {
            "name": "Bug" if (notif.level == "critical" or sev == "P1") else "Task",
        },
        "priority": {
            "name": "Highest" if (notif.level == "critical" or sev == "P1") else "High",
        },
    }
    if sev:
        fields["labels"] = [f"severity-{sev}"]
    payload = json.dumps({"fields": fields})
    api_url = f"{url}/rest/api/2/issue"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", api_url,
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"Jira issue creation failed (rc={proc.returncode}) for {notif.id}")


async def _send_pagerduty(notif: Notification) -> None:
    """Trigger PagerDuty incident via Events API v2.

    R9 row 2936 (#315) — when ``notif.severity`` is set, the operational
    severity tag is forwarded as a ``custom_detail`` field on the
    PagerDuty incident so on-call responders see the P-tag inline; the
    Events API v2 ``severity`` field is left at ``critical`` because
    P1 is the only severity that activates this leg and it always maps
    to PagerDuty's ``critical`` severity.
    """
    key = settings.notification_pagerduty_key
    if not key:
        return

    import json
    sev = notif.severity.value if notif.severity is not None else None
    summary_prefix = f"[{sev}]" if sev else "[CRITICAL]"
    pd_payload: dict = {
        "summary": f"{summary_prefix} {notif.title}: {notif.message}",
        "severity": "critical",
        "source": f"omnisight:{notif.source}",
    }
    if sev:
        pd_payload["custom_details"] = {"omnisight_severity": sev}
    payload = json.dumps({
        "routing_key": key,
        "event_action": "trigger",
        "payload": pd_payload,
    })
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST",
        "https://events.pagerduty.com/v2/enqueue",
        "-H", "Content-Type: application/json",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"PagerDuty trigger failed (rc={proc.returncode}) for {notif.id}")


async def _send_sms(notif: Notification) -> None:
    """Send a P1 SMS broadcast via the configured SMS gateway webhook.

    R9 row 2936 (#315) — L4_SMS leg, only ever activated by
    ``severity=P1`` (see :data:`backend.severity.SEVERITY_TIER_MAPPING`).
    The webhook URL is operator-provided (``OMNISIGHT_NOTIFICATION_SMS_
    WEBHOOK``); we POST a generic JSON envelope that downstream gateways
    (Twilio Programmable SMS forwarder, AWS SNS HTTP endpoint, or
    in-house SMS bridge) can adapt to the carrier's API. The
    destination phone number(s) come from
    ``notification_sms_to`` — empty means the gateway's default
    routing (e.g. an SNS topic with subscribed phone numbers).

    Body is intentionally short (PagerDuty + Slack already deliver the
    full payload — SMS is the "wake on-call up" channel). 160-char
    truncation matches GSM-7 single-segment limit.
    """
    url = settings.notification_sms_webhook
    to = settings.notification_sms_to
    sev = notif.severity.value if notif.severity is not None else None

    body = f"[{sev or notif.level.upper()}] {notif.title}".strip()
    if notif.message:
        body += f": {notif.message}"
    if len(body) > 160:
        body = body[:157] + "..."

    import json
    payload = json.dumps({
        "to": to,
        "message": body,
        "severity": sev,
        "source": notif.source,
        "notification_id": notif.id,
    })
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", payload,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"SMS gateway failed (rc={proc.returncode}) for {notif.id}")

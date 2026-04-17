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
from backend.models import Notification, NotificationLevel

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
) -> Notification:
    """Create and route a notification through the tiered system.

    This is the single entry point — all notification-worthy events
    should call this function.
    """
    level_str = level.value if hasattr(level, "value") else level
    notif = Notification(
        id=f"notif-{uuid.uuid4().hex[:8]}",
        level=level_str,
        title=title,
        message=message,
        source=source,
        timestamp=datetime.now().isoformat(),
        action_url=action_url,
        action_label=action_label,
    )

    # 1. Always persist to DB
    from backend import db
    try:
        await db.insert_notification(notif.model_dump())
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
    """
    from backend import db
    level = notif.level
    errors: list[str] = []
    any_required = False

    # L2+: IM (Slack/Teams)
    if level in ("warning", "action", "critical") and settings.notification_slack_webhook:
        any_required = True
        ok = await _send_with_retry(notif, _send_slack, "slack")
        if not ok:
            errors.append("slack")

    # L3+: Issue tracker
    if level in ("action", "critical") and settings.notification_jira_url:
        any_required = True
        ok = await _send_with_retry(notif, _send_jira, "jira")
        if not ok:
            errors.append("jira")

    # L4: PagerDuty
    if level == "critical" and settings.notification_pagerduty_key:
        any_required = True
        ok = await _send_with_retry(notif, _send_pagerduty, "pagerduty")
        if not ok:
            errors.append("pagerduty")

    if not any_required:
        # No external channels configured for this level
        try:
            await db.update_notification_dispatch(notif.id, "skipped")
        except Exception as exc:
            # Fix-B B2/B6: persistence failure is non-fatal but observable.
            logger.warning("notifications: persist skipped status for %s failed: %s", notif.id, exc)
            from backend import metrics as _m
            _m.persist_failure_total.labels(module="notifications").inc()
        return

    # Update dispatch status in DB
    try:
        if errors:
            await db.update_notification_dispatch(
                notif.id, "failed",
                attempts=settings.notification_max_retries,
                error=f"Failed channels: {', '.join(errors)}",
            )
        else:
            await db.update_notification_dispatch(notif.id, "sent", attempts=1)
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


async def retry_failed_notifications(limit: int = 50) -> dict[str, int]:
    """Scan `dispatch_status='failed'` notifications and re-attempt dispatch.

    Exhausted rows (send_attempts >= max_retries) are marked `'dead'` so the
    next sweep skips them. Returns {retried, recovered, dead}.
    """
    from backend import db
    rows = await db.list_failed_notifications(limit=limit)
    retried = recovered = dead = 0
    max_retries = settings.notification_max_retries
    for row in rows:
        attempts = int(row.get("send_attempts") or 0)
        if attempts >= max_retries:
            try:
                await db.update_notification_dispatch(
                    row["id"], "dead", attempts=attempts,
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
        # Re-check status; dispatched() may have set 'sent'
        try:
            fresh = await db.get_notification(notif.id) if hasattr(db, "get_notification") else None
            if fresh and fresh.get("dispatch_status") == "sent":
                recovered += 1
        except Exception as exc:
            logger.debug("DLQ: post-dispatch status check failed for %s: %s", notif.id, exc)
    return {"retried": retried, "recovered": recovered, "dead": dead}


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
    """Send to Slack via Incoming Webhook."""
    url = settings.notification_slack_webhook
    emoji = {"warning": ":warning:", "action": ":rotating_light:", "critical": ":fire:"}.get(notif.level, ":information_source:")
    mention = ""
    if notif.level == "action":
        mention = f" <@{settings.notification_slack_mention}>" if settings.notification_slack_mention else ""
    elif notif.level == "critical":
        mention = " <!channel>"

    payload = {
        "text": f"{emoji} *[{notif.level.upper()}]* {notif.title}{mention}\n{notif.message}",
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
    """Create Jira issue via REST API."""
    url = settings.notification_jira_url
    token = settings.notification_jira_token
    project = settings.notification_jira_project
    if not all([url, token, project]):
        return

    import json
    payload = json.dumps({
        "fields": {
            "project": {"key": project},
            "summary": f"[{notif.level.upper()}] {notif.title}",
            "description": notif.message,
            "issuetype": {"name": "Bug" if notif.level == "critical" else "Task"},
            "priority": {"name": "Highest" if notif.level == "critical" else "High"},
        }
    })
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
    """Trigger PagerDuty incident via Events API v2."""
    key = settings.notification_pagerduty_key
    if not key:
        return

    import json
    payload = json.dumps({
        "routing_key": key,
        "event_action": "trigger",
        "payload": {
            "summary": f"[CRITICAL] {notif.title}: {notif.message}",
            "severity": "critical",
            "source": f"omnisight:{notif.source}",
        },
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

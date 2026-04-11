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

    # 4. Log
    from backend.routers.system import add_system_log
    log_level = {"info": "info", "warning": "warn", "action": "error", "critical": "error"}.get(level_str, "info")
    add_system_log(f"[NOTIFY:{level_str.upper()}] {title}", log_level)

    return notif


async def _dispatch_external(notif: Notification) -> None:
    """Send notification to external channels. Errors are logged, never raised."""
    level = notif.level

    # L2+: IM (Slack/Teams)
    if level in ("warning", "action", "critical") and settings.notification_slack_webhook:
        await _send_slack(notif)

    # L3+: Issue tracker
    if level in ("action", "critical") and settings.notification_jira_url:
        await _send_jira(notif)

    # L4: PagerDuty
    if level == "critical" and settings.notification_pagerduty_key:
        await _send_pagerduty(notif)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  External dispatchers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _send_slack(notif: Notification) -> None:
    """Send to Slack via Incoming Webhook."""
    import os
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
        logger.warning("Slack webhook failed for notification %s", notif.id)


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
        logger.warning("Jira issue creation failed for notification %s", notif.id)


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
        logger.warning("PagerDuty trigger failed for notification %s", notif.id)

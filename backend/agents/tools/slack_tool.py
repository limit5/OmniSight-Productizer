"""OP-113 — Slack agent tool."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SCOPE_SURRENDERED = "🛑 SCOPE_SURRENDERED"
SLACK_TOKEN_ENV = "OMNISIGHT_SLACK_TOKEN"

logger = logging.getLogger(__name__)


def slack_post_message(
    channel: str,
    text: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Post ``text`` to ``channel`` via Slack Web API."""
    resolved_token = token if token is not None else os.getenv(SLACK_TOKEN_ENV)
    if not resolved_token:
        note = "Slack token not provisioned"
        logger.warning("%s %s", SCOPE_SURRENDERED, note)
        return {
            "ok": False,
            "skipped": True,
            "reason": f"{SCOPE_SURRENDERED} {note}",
        }

    response = httpx.post(
        SLACK_POST_MESSAGE_URL,
        headers={
            "Authorization": f"Bearer {resolved_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "channel": channel,
            "text": text,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        error = payload.get("error", "unknown_error")
        raise RuntimeError(f"Slack chat.postMessage failed: {error}")
    return payload

"""R1 (#307) — Microsoft Teams adapter.

Outbound: Incoming Webhook posts an Adaptive Card with an
``Action.Submit`` per button. Inbound callbacks come either from a Teams
Bot Framework endpoint or an outgoing-webhook; we accept either and
normalise via :func:`parse_inbound`. Authentic callbacks are HMAC-SHA256
signed with ``chatops_teams_secret``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.chatops_teams_webhook)


def status_reason() -> str:
    if not settings.chatops_teams_webhook:
        return "chatops_teams_webhook not set"
    if not settings.chatops_teams_secret:
        return "connected (outbound only — set chatops_teams_secret for buttons)"
    return "connected"


def _build_adaptive_card(title: str, body: str, buttons) -> dict[str, Any]:
    actions = []
    for b in (buttons or [])[:6]:  # Teams caps Action.Submit count; be conservative
        payload = {"buttonId": b.id}
        if b.value:
            payload["buttonValue"] = b.value
        actions.append({
            "type": "Action.Submit",
            "title": b.label[:40],
            "style": "destructive" if b.style == "danger" else "positive" if b.style == "success" else "default",
            "data": payload,
        })
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title[:120], "wrap": True},
            {"type": "TextBlock", "text": body[:3800], "wrap": True, "spacing": "Small"},
        ],
    }
    if actions:
        card["actions"] = actions
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": card,
        }],
    }


async def send_interactive(*, title: str, body: str, buttons, meta=None) -> dict[str, Any]:
    url = settings.chatops_teams_webhook
    if not url:
        return {"skipped": "teams webhook not configured"}
    payload = _build_adaptive_card(title, body, buttons)
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", url,
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Teams webhook failed rc={proc.returncode}: {err.decode(errors='replace')[:200]}"
        )
    return {"ok": True}


def verify(headers: dict[str, str], raw_body: bytes) -> None:
    """HMAC-SHA256 over the raw request body vs. ``chatops_teams_secret``."""
    secret = (settings.chatops_teams_secret or "").encode()
    if not secret:
        raise PermissionError("chatops_teams_secret not configured; inbound refused")
    provided = headers.get("authorization") or headers.get("Authorization") or ""
    if provided.lower().startswith("hmac "):
        provided = provided[5:]
    digest = hmac.new(secret, raw_body, hashlib.sha256).digest()
    expected = digest.hex()
    # Teams official signature uses base64; accept either encoding.
    import base64
    expected_b64 = base64.b64encode(digest).decode()
    if not (hmac.compare_digest(provided, expected) or hmac.compare_digest(provided, expected_b64)):
        raise PermissionError("bad Teams HMAC signature")


def parse_inbound(payload: dict[str, Any]):
    from backend.chatops_bridge import Inbound
    user = payload.get("from") or payload.get("user") or {}
    author = user.get("name") or user.get("id") or "unknown"
    user_id = str(user.get("id") or "")
    message_id = str(payload.get("id") or "")
    # Action.Submit bubbles as "invoke"/"value" with our embedded buttonId.
    value = payload.get("value") or {}
    if value.get("buttonId"):
        return Inbound(
            kind="button", channel="teams", author=author, user_id=user_id,
            button_id=str(value.get("buttonId")),
            button_value=str(value.get("buttonValue") or ""),
            message_id=message_id, raw=payload,
        )
    text = str(payload.get("text") or "").strip()
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        return Inbound(
            kind="command", channel="teams", author=author, user_id=user_id,
            command=head, command_args=rest, text=text, message_id=message_id, raw=payload,
        )
    return Inbound(
        kind="message", channel="teams", author=author, user_id=user_id,
        text=text, message_id=message_id, raw=payload,
    )

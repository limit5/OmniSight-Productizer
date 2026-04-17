"""R1 (#307) — LINE Messaging API adapter.

Outbound: Flex Message with Postback Action buttons. The Messaging API
uses ``/v2/bot/message/push``; authentication is via
``chatops_line_channel_token`` (OAuth-ish bearer). Inbound webhooks are
verified with X-Line-Signature (base64 HMAC-SHA256 over the raw body,
keyed by ``chatops_line_channel_secret``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def is_configured() -> bool:
    return bool(settings.chatops_line_channel_token and settings.chatops_line_to)


def status_reason() -> str:
    if not settings.chatops_line_channel_token:
        return "chatops_line_channel_token not set"
    if not settings.chatops_line_to:
        return "chatops_line_to (user/group id) not set"
    if not settings.chatops_line_channel_secret:
        return "connected (outbound only — set chatops_line_channel_secret for buttons)"
    return "connected"


def _button_bubble(b) -> dict[str, Any]:
    style = "primary" if b.style in ("primary", "success") else "secondary"
    color = None
    if b.style == "danger":
        color = "#dc2626"
    btn = {
        "type": "button",
        "style": style,
        "action": {
            "type": "postback",
            "label": b.label[:20],
            "data": f"buttonId={b.id}" + (f"&value={b.value}" if b.value else ""),
            "displayText": b.label[:60],
        },
        "height": "sm",
    }
    if color:
        btn["color"] = color
    return btn


def _build_flex(title: str, body: str, buttons) -> dict[str, Any]:
    body_contents: list[dict[str, Any]] = [
        {"type": "text", "text": title[:120], "weight": "bold", "size": "md", "wrap": True},
        {"type": "text", "text": body[:1800], "wrap": True, "size": "sm", "margin": "md"},
    ]
    footer_contents = [_button_bubble(b) for b in (buttons or [])[:4]]
    bubble: dict[str, Any] = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": body_contents},
    }
    if footer_contents:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm",
                            "contents": footer_contents}
    return {
        "type": "flex",
        "altText": title[:400],
        "contents": bubble,
    }


async def send_interactive(*, title: str, body: str, buttons, meta=None) -> dict[str, Any]:
    token = settings.chatops_line_channel_token
    to = settings.chatops_line_to
    if not token or not to:
        return {"skipped": "line not configured"}
    payload = {
        "to": to,
        "messages": [_build_flex(title, body, buttons)],
    }
    proc = await asyncio.create_subprocess_exec(
        "curl", "-s", "-X", "POST", _LINE_PUSH_URL,
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "-d", json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Line push failed rc={proc.returncode}: {err.decode(errors='replace')[:200]}"
        )
    return {"ok": True}


def verify(headers: dict[str, str], raw_body: bytes) -> None:
    secret = (settings.chatops_line_channel_secret or "").encode()
    if not secret:
        raise PermissionError("chatops_line_channel_secret not configured; inbound refused")
    provided = (
        headers.get("x-line-signature")
        or headers.get("X-Line-Signature")
        or ""
    )
    digest = hmac.new(secret, raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(provided, expected):
        raise PermissionError("bad Line signature")


def _parse_postback_data(data: str) -> tuple[str, str]:
    parts = {}
    for chunk in (data or "").split("&"):
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            parts[k] = v
    return parts.get("buttonId", ""), parts.get("value", "")


def parse_inbound(payload: dict[str, Any]):
    """Line wraps events in ``{"events": [...]}``. We pick the first event."""
    from backend.chatops_bridge import Inbound
    events = payload.get("events") or []
    if not events:
        return Inbound(kind="message", channel="line", author="unknown", raw=payload)
    ev = events[0]
    source = ev.get("source") or {}
    user_id = str(source.get("userId") or source.get("groupId") or "")
    author = user_id or "unknown"
    message_id = str(ev.get("replyToken") or "")
    etype = ev.get("type")
    if etype == "postback":
        bid, bval = _parse_postback_data((ev.get("postback") or {}).get("data", ""))
        return Inbound(
            kind="button", channel="line", author=author, user_id=user_id,
            button_id=bid, button_value=bval, message_id=message_id, raw=payload,
        )
    if etype == "message":
        msg = ev.get("message") or {}
        text = str(msg.get("text") or "")
        if text.startswith("/"):
            head, _, rest = text[1:].partition(" ")
            return Inbound(
                kind="command", channel="line", author=author, user_id=user_id,
                command=head, command_args=rest, text=text,
                message_id=message_id, raw=payload,
            )
        return Inbound(
            kind="message", channel="line", author=author, user_id=user_id,
            text=text, message_id=message_id, raw=payload,
        )
    return Inbound(kind="message", channel="line", author=author, user_id=user_id,
                   raw=payload, message_id=message_id)

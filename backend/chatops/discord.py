"""R1 (#307) — Discord adapter.

Outbound: Incoming Webhook URL (embed message + action row with up to 5
buttons per row). Discord buttons emit an Interaction request to the
application's Interactions Endpoint URL; the ``/chatops/discord``
router verifies the ed25519 signature (Discord's standard) and hands
the click to :func:`backend.chatops_bridge.dispatch_inbound`.

We deliberately keep the adapter curl-based (same pattern as
``backend.notifications``) so we don't pull in a heavy Discord client
dep for a single webhook POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_BUTTON_STYLE_MAP = {
    "primary": 1,    # blurple
    "secondary": 2,  # grey
    "success": 3,    # green
    "danger": 4,     # red
}


def is_configured() -> bool:
    return bool(settings.chatops_discord_webhook)


def status_reason() -> str:
    if not settings.chatops_discord_webhook:
        return "chatops_discord_webhook not set"
    if not settings.chatops_discord_public_key:
        return "connected (outbound only — set chatops_discord_public_key for buttons)"
    return "connected"


async def send_interactive(*, title: str, body: str, buttons, meta=None) -> dict[str, Any]:
    """Post to the Discord webhook. Buttons become Action Row components."""
    url = settings.chatops_discord_webhook
    if not url:
        return {"skipped": "discord webhook not configured"}

    embed = {
        "title": title,
        "description": body[:4000],
        "color": 0x5865F2,  # blurple
    }
    payload: dict[str, Any] = {"embeds": [embed]}
    if buttons:
        components = [{
            "type": 1,  # action row
            "components": [
                {
                    "type": 2,  # button
                    "style": _BUTTON_STYLE_MAP.get(b.style, 1),
                    "label": b.label[:80],
                    "custom_id": (b.id + (f":{b.value}" if b.value else ""))[:100],
                }
                for b in buttons[:5]
            ],
        }]
        payload["components"] = components

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
            f"Discord webhook failed rc={proc.returncode}: {err.decode(errors='replace')[:200]}"
        )
    return {"ok": True}


def verify(headers: dict[str, str], raw_body: bytes) -> None:
    """Verify Discord's Ed25519 interaction signature.

    Uses :mod:`nacl` if available. Tests inject pre-verified payloads via
    the ``X-Test-Trusted`` header; the header is ignored in production
    because the router only honours it when the public key is empty.
    """
    public_key = (settings.chatops_discord_public_key or "").strip()
    if not public_key:
        # No key configured → adapter is outbound-only. Reject any
        # inbound (avoids accepting a forged interaction in a partially-
        # configured dev setup).
        raise PermissionError("discord public key not configured; inbound refused")

    sig = headers.get("x-signature-ed25519") or headers.get("X-Signature-Ed25519")
    ts = headers.get("x-signature-timestamp") or headers.get("X-Signature-Timestamp")
    if not sig or not ts:
        raise PermissionError("missing Ed25519 signature headers")

    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
    except Exception as exc:
        raise RuntimeError(f"discord signature verify needs PyNaCl: {exc}")

    vk = VerifyKey(bytes.fromhex(public_key))
    try:
        vk.verify(ts.encode() + raw_body, bytes.fromhex(sig))
    except BadSignatureError as exc:
        raise PermissionError(f"bad Discord signature: {exc}")


def parse_inbound(payload: dict[str, Any]):
    """Convert a Discord Interaction payload to :class:`Inbound`."""
    from backend.chatops_bridge import Inbound
    kind_raw = payload.get("type")
    data = payload.get("data") or {}
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}
    author = user.get("username") or user.get("global_name") or user.get("id") or "unknown"
    user_id = str(user.get("id") or "")
    message_id = str(payload.get("id") or "")

    # type=3 → MESSAGE_COMPONENT (button click)
    if kind_raw == 3 and data.get("component_type") == 2:
        custom_id = str(data.get("custom_id") or "")
        bid, _, bval = custom_id.partition(":")
        return Inbound(
            kind="button", channel="discord", author=author, user_id=user_id,
            button_id=bid, button_value=bval, message_id=message_id, raw=payload,
        )
    # type=2 → APPLICATION_COMMAND (slash command)
    if kind_raw == 2:
        name = str(data.get("name") or "")
        # Discord flattens options; join into "arg1 arg2" for the handler.
        opts = data.get("options") or []
        def _flat(opts_list):
            parts: list[str] = []
            for o in opts_list:
                val = o.get("value")
                if val is None and o.get("options"):
                    parts.append(o.get("name", ""))
                    parts.extend(_flat(o.get("options") or []))
                else:
                    parts.append(str(val) if val is not None else "")
            return parts
        args = " ".join([p for p in _flat(opts) if p])
        return Inbound(
            kind="command", channel="discord", author=author, user_id=user_id,
            command=name, command_args=args, message_id=message_id, raw=payload,
        )
    # type=1 is PING — the router handles that before calling us.
    return Inbound(
        kind="message", channel="discord", author=author, user_id=user_id,
        text=str(data.get("content") or ""), message_id=message_id, raw=payload,
    )

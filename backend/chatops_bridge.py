"""R1 (#307) — Unified ChatOps bridge.

Single surface over Discord / Teams / Line so the rest of OmniSight
(PEP gateway, notification router, slash-command handler) can fire one
``send_interactive(channel, message, buttons)`` and get the same
behaviour on every transport.

Design notes
------------

* **Adapters are pluggable** — each transport lives in
  :mod:`backend.chatops.<name>` with a module-level ``send_interactive``,
  ``parse_inbound``, ``verify`` trio. A missing / unconfigured adapter
  is silently skipped (no-op) so dev environments don't need every
  webhook wired up.
* **Callbacks are typed, not strings** — :func:`on_button_click` and
  :func:`on_command` register Python callables keyed by
  ``button_id`` / ``command``.  The inbound router resolves the handler,
  invokes it, and returns whatever the handler returns so the transport
  layer can echo the reply back.
* **Outbound mirror** — every outbound message publishes a
  ``chatops.message`` SSE event (``direction="outbound"``) so the
  dashboard ChatOps Mirror Panel can render the same content the operator
  sees in Discord / Teams / Line.
* **Audit** — every inbound button click / command goes to the audit
  hash-chain; outbound messages are not audited (they're already
  auditable via the decision-engine / PEP gateway logs upstream).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.config import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Canonical shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class Button:
    id: str                 # stable identifier — routed via on_button_click
    label: str              # user-facing label
    style: str = "primary"  # primary | secondary | danger | success
    value: str = ""         # optional opaque payload forwarded to the handler


@dataclass
class Inbound:
    """Normalised inbound ChatOps event (button click / command / message)."""
    kind: str                # "button" | "command" | "message"
    channel: str             # "discord" | "teams" | "line"
    author: str              # username / email / user-id
    user_id: str = ""        # raw platform user id (for authz)
    button_id: str = ""      # populated when kind=="button"
    button_value: str = ""
    command: str = ""        # populated when kind=="command" (no leading slash)
    command_args: str = ""
    text: str = ""           # raw message body
    message_id: str = ""
    raw: dict = field(default_factory=dict)  # transport-specific original payload


@dataclass
class OutboundMessage:
    """Mirror-record of a message the bridge emitted to a transport."""
    id: str
    ts: float
    channel: str
    title: str
    body: str
    buttons: list[Button] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # e.g. {"pep_id": "..."}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "channel": self.channel,
            "title": self.title,
            "body": self.body,
            "buttons": [b.__dict__ for b in self.buttons],
            "meta": dict(self.meta),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Callback registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


ButtonHandler = Callable[[Inbound], Awaitable[str]]
CommandHandler = Callable[[Inbound], Awaitable[str]]

_button_handlers: dict[str, ButtonHandler] = {}
_command_handlers: dict[str, CommandHandler] = {}


def on_button_click(button_id: str, handler: ButtonHandler) -> None:
    """Register a handler for a specific button id. Overwrites existing."""
    _button_handlers[button_id] = handler


def on_command(command: str, handler: CommandHandler) -> None:
    """Register a handler for a slash-style command (no leading slash).

    Commands use the ``/omnisight <verb>`` convention from the R1 spec, so
    most callers register ``command="omnisight"`` and branch on
    :attr:`Inbound.command_args`.
    """
    _command_handlers[command] = handler


def list_commands() -> list[str]:
    return sorted(_command_handlers.keys())


def list_buttons() -> list[str]:
    return sorted(_button_handlers.keys())


def _is_authorized_user(user_id: str, author: str) -> bool:
    """Is this ChatOps user on the allow-list for inject-class verbs?

    We match both the raw platform user id and the author display name
    against the ``chatops_authorized_users`` comma-separated env setting.
    Empty allow-list ⇒ everyone is allowed (dev mode). Production should
    set this to the Gerrit non-ai-reviewer group's ChatOps handles.
    """
    allow_raw = settings.chatops_authorized_users or ""
    allowed = {x.strip() for x in allow_raw.split(",") if x.strip()}
    if not allowed:
        return True
    return user_id in allowed or author in allowed


def authorize_inject(inbound: Inbound) -> None:
    """Raise :class:`PermissionError` if the author can't inject hints."""
    if not _is_authorized_user(inbound.user_id, inbound.author):
        raise PermissionError(
            f"ChatOps user {inbound.author!r} ({inbound.user_id!r}) "
            "is not in chatops_authorized_users allow-list"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adapter discovery
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_ADAPTER_NAMES = ("discord", "teams", "line")


def get_adapter(name: str):
    """Import and return the adapter module for ``name``.

    Raises ``ValueError`` on an unknown name (not ``ImportError`` — unknown
    channel is a caller error, not a missing dependency).
    """
    name = (name or "").lower()
    if name not in _ADAPTER_NAMES:
        raise ValueError(f"unknown ChatOps channel: {name!r}")
    return importlib.import_module(f"backend.chatops.{name}")


def adapter_status() -> dict[str, dict[str, Any]]:
    """Per-adapter connection status for the UI channel selector."""
    out: dict[str, dict[str, Any]] = {}
    for name in _ADAPTER_NAMES:
        try:
            mod = get_adapter(name)
            configured = bool(mod.is_configured())
            reason = mod.status_reason()
        except Exception as exc:
            configured = False
            reason = f"adapter error: {exc}"
        out[name] = {"configured": configured, "reason": reason}
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mirror ring (recent messages surfaced to dashboard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_mirror_ring: list[dict] = []
_MIRROR_MAX = 200


def _mirror_record(direction: str, payload: dict) -> None:
    entry = {"direction": direction, **payload}
    _mirror_ring.append(entry)
    if len(_mirror_ring) > _MIRROR_MAX:
        _mirror_ring.pop(0)


def mirror_snapshot(limit: int = 100) -> list[dict]:
    items = list(_mirror_ring[-limit:])
    items.reverse()
    return items


def _emit_sse(direction: str, payload: dict) -> None:
    try:
        from backend.events import bus
        bus.publish("chatops.message", {"direction": direction, **payload})
    except Exception as exc:
        logger.debug("chatops.message SSE publish skipped: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outbound
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def send_interactive(
    channel: str,
    message: str,
    *,
    title: str = "OmniSight",
    buttons: list[Button] | None = None,
    meta: dict | None = None,
) -> OutboundMessage:
    """Send an interactive message to ``channel``.

    ``channel`` is one of ``"discord"``, ``"teams"``, ``"line"``, or the
    sentinel ``"*"`` which fans out to every configured adapter. The
    return value is the mirror-record that was also pushed to the SSE
    bus (the frontend renders this in the ChatOps Mirror Panel).
    """
    buttons = list(buttons or [])
    meta = dict(meta or {})
    out = OutboundMessage(
        id=f"cm-{uuid.uuid4().hex[:10]}",
        ts=time.time(),
        channel=channel,
        title=title,
        body=message,
        buttons=buttons,
        meta=meta,
    )
    if channel == "*":
        targets: tuple[str, ...] = _ADAPTER_NAMES
    else:
        # Validate channel upfront so an unknown name is a loud caller
        # error rather than a silent no-op warning.
        get_adapter(channel)
        targets = (channel,)
    errors: list[str] = []
    for name in targets:
        try:
            mod = get_adapter(name)
            if not mod.is_configured():
                continue
            await mod.send_interactive(title=title, body=message, buttons=buttons, meta=meta)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            logger.warning("ChatOps send to %s failed: %s", name, exc)

    payload = out.to_dict()
    if errors:
        payload["errors"] = errors
    _mirror_record("outbound", payload)
    _emit_sse("outbound", payload)
    return out


async def send_text(channel: str, message: str, *, title: str = "OmniSight") -> OutboundMessage:
    """Convenience wrapper — no buttons."""
    return await send_interactive(channel, message, title=title, buttons=[])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Inbound dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def dispatch_inbound(inbound: Inbound) -> dict[str, Any]:
    """Route an inbound event to the registered handler + mirror it.

    Returns a dict with ``ok``, ``handled``, and ``reply`` keys. The
    caller (the webhook router) serialises this back to whichever HTTP
    surface the transport expects.

    All inbound events (button / command / message) are mirrored via
    SSE so the dashboard panel always sees what's happening on Discord /
    Teams / Line.
    """
    mirror_payload = {
        "id": inbound.message_id or f"cm-in-{uuid.uuid4().hex[:10]}",
        "ts": time.time(),
        "channel": inbound.channel,
        "author": inbound.author,
        "user_id": inbound.user_id,
        "kind": inbound.kind,
        "body": inbound.text,
        "button_id": inbound.button_id,
        "command": inbound.command,
        "command_args": inbound.command_args,
    }
    _mirror_record("inbound", mirror_payload)
    _emit_sse("inbound", mirror_payload)

    # Audit — inbound button / command is operator-intent so it must land
    # on the hash chain regardless of handler outcome.
    _schedule_inbound_audit(inbound)

    reply: str = ""
    handled = False
    try:
        if inbound.kind == "button":
            handler = _button_handlers.get(inbound.button_id)
            if handler:
                reply = await handler(inbound)
                handled = True
        elif inbound.kind == "command":
            handler = _command_handlers.get(inbound.command)
            if handler:
                reply = await handler(inbound)
                handled = True
        elif inbound.kind == "message":
            # Messages without an explicit command fall through — the LLM
            # layer decides whether to respond. Bridge stays mute.
            handled = False
    except PermissionError as exc:
        reply = f"⚠️ Forbidden: {exc}"
        handled = True
    except Exception as exc:
        logger.warning("ChatOps dispatch failed (%s/%s): %s",
                       inbound.kind, inbound.button_id or inbound.command, exc)
        reply = f"⚠️ Error: {exc}"
        handled = True

    return {"ok": True, "handled": handled, "reply": reply}


def _schedule_inbound_audit(inbound: Inbound) -> None:
    try:
        from backend import audit as _audit
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_audit.log(
            action=f"chatops.{inbound.kind}",
            entity_kind="chatops_event",
            entity_id=inbound.message_id or "",
            after={
                "channel": inbound.channel,
                "author": inbound.author,
                "user_id": inbound.user_id,
                "kind": inbound.kind,
                "button_id": inbound.button_id,
                "button_value": inbound.button_value[:200],
                "command": inbound.command,
                "command_args": inbound.command_args[:500],
                "text": (inbound.text or "")[:500],
            },
            actor=f"chatops:{inbound.author or inbound.user_id or 'unknown'}",
        ))
    except Exception as exc:
        logger.debug("chatops inbound audit skipped: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reset_for_tests() -> None:
    _button_handlers.clear()
    _command_handlers.clear()
    _mirror_ring.clear()

"""R1 (#307) — chatops_bridge unit tests.

Exercises the transport-agnostic surface (send / dispatch / mirror ring
/ SSE mirror) using fake adapters so no Discord / Teams / Line webhook
is ever hit. A dedicated integration test file covers the adapter-level
payload shape.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from backend import chatops_bridge as bridge
from backend import events


@pytest.fixture(autouse=True)
def _reset():
    bridge._reset_for_tests()
    yield
    bridge._reset_for_tests()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fake adapter injected in place of discord/teams/line
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeAdapterModule:
    def __init__(self):
        self.sent: list[dict] = []
        self._configured = True

    def is_configured(self):
        return self._configured

    def status_reason(self):
        return "fake" if self._configured else "fake-unconfigured"

    async def send_interactive(self, *, title, body, buttons, meta=None):
        self.sent.append({"title": title, "body": body, "buttons": buttons, "meta": meta})
        return {"ok": True}

    def verify(self, headers, raw_body):
        return None

    def parse_inbound(self, payload):
        from backend.chatops_bridge import Inbound
        return Inbound(
            kind=payload.get("kind", "message"),
            channel=payload.get("channel", "discord"),
            author=payload.get("author", "alice"),
            user_id=payload.get("user_id", "u1"),
            button_id=payload.get("button_id", ""),
            command=payload.get("command", ""),
            command_args=payload.get("command_args", ""),
            text=payload.get("text", ""),
            raw=payload,
        )


@pytest.fixture
def fake_discord(monkeypatch):
    fake = _FakeAdapterModule()
    monkeypatch.setitem(sys.modules, "backend.chatops.discord", fake)
    yield fake


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outbound
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_send_interactive_fans_out_to_configured_adapter(fake_discord):
    async def _go():
        out = await bridge.send_interactive(
            "discord", "hello world", title="t",
            buttons=[bridge.Button(id="ok", label="OK")],
        )
        assert out.id.startswith("cm-")
        assert fake_discord.sent[0]["title"] == "t"
        assert fake_discord.sent[0]["body"] == "hello world"
        assert fake_discord.sent[0]["buttons"][0].id == "ok"
    asyncio.run(_go())


def test_send_unknown_channel_raises():
    async def _go():
        with pytest.raises(ValueError):
            await bridge.send_interactive("nosuch", "x")
    asyncio.run(_go())


def test_send_unconfigured_adapter_is_skipped(fake_discord):
    fake_discord._configured = False
    async def _go():
        out = await bridge.send_interactive("discord", "hi")
        assert out.body == "hi"
        assert fake_discord.sent == []
    asyncio.run(_go())


def test_mirror_records_outbound(fake_discord):
    async def _go():
        await bridge.send_interactive("discord", "first")
        await bridge.send_interactive("discord", "second")
        snap = bridge.mirror_snapshot()
        assert len(snap) == 2
        assert snap[0]["direction"] == "outbound"
        assert snap[0]["body"] == "second"  # newest first
    asyncio.run(_go())


def test_outbound_emits_sse_event(fake_discord):
    received: list[tuple[str, dict]] = []

    class _Q:
        def put_nowait(self, msg):
            import json
            received.append((msg["event"], json.loads(msg["data"])))

    events.bus._subscribers[_Q()] = (None, None)  # type: ignore[assignment]
    try:
        async def _go():
            await bridge.send_interactive("discord", "hi")
        asyncio.run(_go())
    finally:
        events.bus._subscribers.clear()
    kinds = [e for e, _ in received]
    assert "chatops.message" in kinds


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Inbound dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_button_click_routes_to_registered_handler():
    calls: list[str] = []

    async def _handler(inbound):
        calls.append(inbound.button_id)
        return "ok"

    bridge.on_button_click("approve", _handler)
    async def _go():
        res = await bridge.dispatch_inbound(bridge.Inbound(
            kind="button", channel="discord", author="a",
            user_id="u1", button_id="approve", button_value="pep-1",
        ))
        assert res["handled"] is True
        assert res["reply"] == "ok"
    asyncio.run(_go())
    assert calls == ["approve"]


def test_command_routes_to_registered_handler():
    async def _h(inbound):
        return f"got args={inbound.command_args}"
    bridge.on_command("omnisight", _h)
    async def _go():
        res = await bridge.dispatch_inbound(bridge.Inbound(
            kind="command", channel="discord", author="a", user_id="u1",
            command="omnisight", command_args="status",
        ))
        assert res["reply"] == "got args=status"
    asyncio.run(_go())


def test_unknown_command_is_not_handled():
    async def _go():
        res = await bridge.dispatch_inbound(bridge.Inbound(
            kind="command", channel="discord", author="a", user_id="u1",
            command="nosuch",
        ))
        assert res["handled"] is False
    asyncio.run(_go())


def test_handler_exception_becomes_reply():
    async def _boom(inbound):
        raise RuntimeError("nope")

    bridge.on_button_click("bad", _boom)
    async def _go():
        res = await bridge.dispatch_inbound(bridge.Inbound(
            kind="button", channel="discord", author="a", user_id="u1",
            button_id="bad",
        ))
        assert res["handled"] is True
        assert "Error" in res["reply"]
    asyncio.run(_go())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_authorize_inject_allows_empty_allowlist(monkeypatch):
    monkeypatch.setattr(bridge.settings, "chatops_authorized_users", "")
    bridge.authorize_inject(bridge.Inbound(
        kind="command", channel="discord", author="anyone", user_id="x"))


def test_authorize_inject_rejects_when_not_on_list(monkeypatch):
    monkeypatch.setattr(bridge.settings, "chatops_authorized_users", "alice,bob")
    with pytest.raises(PermissionError):
        bridge.authorize_inject(bridge.Inbound(
            kind="command", channel="discord", author="mallory", user_id="m"))


def test_authorize_inject_allows_matching_author(monkeypatch):
    monkeypatch.setattr(bridge.settings, "chatops_authorized_users", "alice,bob")
    bridge.authorize_inject(bridge.Inbound(
        kind="command", channel="discord", author="alice", user_id="x"))


def test_authorize_inject_allows_matching_user_id(monkeypatch):
    monkeypatch.setattr(bridge.settings, "chatops_authorized_users", "u_123,u_456")
    bridge.authorize_inject(bridge.Inbound(
        kind="command", channel="discord", author="anything", user_id="u_123"))

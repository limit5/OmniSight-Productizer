"""BP.H.3 — ``Notification.is_red_card`` dispatch contract tests.

Locks the Blueprint Phase H row:

  Notification 加 ``is_red_card`` bool；映射到 L3 Jira + L4 PagerDuty

The shape mirrors the existing R9 severity-dispatch tests: offline curl
capture, settings monkeypatching, and DB persistence stubs. Red-card is
an additive marker on ``Notification`` rather than a new severity value.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from backend.models import Notification


@dataclass
class _CapturedCurl:
    cmd: tuple[str, ...]
    body: dict | None


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.fixture()
def fake_subprocess(monkeypatch):
    captured: list[_CapturedCurl] = []

    async def _fake_exec(*args, **kwargs):
        body: dict | None = None
        try:
            d_idx = args.index("-d")
            body = json.loads(args[d_idx + 1])
        except (ValueError, IndexError, json.JSONDecodeError):
            body = None
        captured.append(_CapturedCurl(cmd=args, body=body))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


@pytest.fixture()
def configured_settings(monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "notification_slack_webhook", "https://hooks.slack.test/T0/B0/X")
    monkeypatch.setattr(settings, "notification_jira_url", "https://jira.test")
    monkeypatch.setattr(settings, "notification_jira_token", "tk")
    monkeypatch.setattr(settings, "notification_jira_project", "OMNI")
    monkeypatch.setattr(settings, "notification_pagerduty_key", "pd-routing-key")
    monkeypatch.setattr(settings, "notification_sms_webhook", "https://sms.gateway.test/send")
    monkeypatch.setattr(settings, "notification_max_retries", 1)
    monkeypatch.setattr(settings, "notification_retry_backoff", 0)
    return settings


@pytest.fixture()
def stub_persistence(monkeypatch):
    from backend import db
    from backend import db_pool

    class _NullConn:
        async def execute(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def fetchrow(self, *a, **kw):
            return None

    class _NullCM:
        async def __aenter__(self):
            return _NullConn()

        async def __aexit__(self, *a):
            return False

    class _NullPool:
        def acquire(self):
            return _NullCM()

    monkeypatch.setattr(db_pool, "get_pool", lambda: _NullPool())

    async def _noop_insert(*a, **kw):
        return None

    async def _noop_update(*a, **kw):
        return None

    monkeypatch.setattr(db, "insert_notification", _noop_insert)
    monkeypatch.setattr(db, "update_notification_dispatch", _noop_update)


def _red_card_notif(level: str = "info") -> Notification:
    return Notification(
        id="notif-red-card-test",
        level=level,
        title="Agent red-carded",
        message="agent-alpha hit Verified -1 threshold",
        source="red_card",
        timestamp="2026-05-04T00:00:00",
        is_red_card=True,
    )


def test_notification_red_card_defaults_false_and_round_trips() -> None:
    legacy = Notification(id="notif-legacy", level="info", title="legacy", message="")
    assert legacy.is_red_card is False

    red = Notification(
        id="notif-red",
        level="critical",
        title="red card",
        message="",
        is_red_card=True,
    )
    dumped = red.model_dump(mode="json")
    assert dumped["is_red_card"] is True
    assert Notification(**dumped).is_red_card is True


@pytest.mark.asyncio
async def test_notify_publishes_red_card_on_bus(monkeypatch) -> None:
    from backend import notifications as n
    from backend.events import bus

    captured: list[dict] = []

    async def _no_db(conn, data):
        return None

    def _capture(channel: str, payload: dict, **kw) -> None:
        if channel == "notification":
            captured.append(payload)

    def _close_task(coro):
        coro.close()
        return None

    monkeypatch.setattr("backend.db.insert_notification", _no_db)
    monkeypatch.setattr(bus, "publish", _capture)
    monkeypatch.setattr(n.asyncio, "create_task", _close_task)

    await n.notify(
        level="info",
        title="red card",
        message="agent-alpha blocked",
        source="red_card",
        is_red_card=True,
    )

    assert len(captured) == 1
    assert captured[0]["is_red_card"] is True
    assert captured[0]["level"] == "info"


@pytest.mark.asyncio
async def test_red_card_dispatch_maps_info_to_jira_and_pagerduty_only(
    fake_subprocess, configured_settings, stub_persistence,
) -> None:
    from backend.notifications import _dispatch_external

    await _dispatch_external(_red_card_notif(level="info"))

    urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
    assert urls == [
        "https://jira.test/rest/api/2/issue",
        "https://events.pagerduty.com/v2/enqueue",
    ]


@pytest.mark.asyncio
async def test_red_card_jira_and_pagerduty_payloads_carry_marker(
    fake_subprocess, configured_settings, stub_persistence,
) -> None:
    from backend.notifications import _send_jira, _send_pagerduty

    notif = _red_card_notif(level="info")
    await _send_jira(notif)
    await _send_pagerduty(notif)

    jira_body = fake_subprocess[0].body
    assert jira_body["fields"]["labels"] == ["red-card"]
    assert jira_body["fields"]["description"].startswith("[red-card] ")

    pagerduty_body = fake_subprocess[1].body
    assert pagerduty_body["payload"]["summary"].startswith("[RED CARD]")
    assert pagerduty_body["payload"]["custom_details"]["is_red_card"] is True


@pytest.mark.asyncio
async def test_send_notification_payload_red_card_maps_to_jira_and_pagerduty(
    fake_subprocess, configured_settings, stub_persistence,
) -> None:
    from backend.notifications import send_notification

    notif = await send_notification(
        tier=None,
        severity=None,
        payload={
            "title": "Agent red-carded",
            "message": "agent-alpha hit Verified -1 threshold",
            "source": "red_card",
            "level": "info",
            "is_red_card": True,
        },
    )

    assert notif.is_red_card is True
    urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
    assert urls == [
        "https://jira.test/rest/api/2/issue",
        "https://events.pagerduty.com/v2/enqueue",
    ]


@pytest.mark.asyncio
async def test_insert_and_list_round_trips_red_card_marker(pg_test_conn) -> None:
    from backend import db

    await db.insert_notification(pg_test_conn, {
        "id": "n-red-card-roundtrip",
        "level": "info",
        "title": "red card",
        "message": "agent-alpha blocked",
        "source": "red_card",
        "timestamp": "2026-05-04T00:00:00",
        "read": False,
        "auto_resolved": False,
        "is_red_card": True,
    })

    rows = await db.list_notifications(pg_test_conn)
    assert len(rows) == 1
    assert rows[0]["is_red_card"] is True

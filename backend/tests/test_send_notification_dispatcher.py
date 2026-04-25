"""R9 row 2941 (#315) — ``send_notification`` tier-explicit dispatcher
contract tests.

Locks the row 2941 sub-bullet verbatim:

  ``backend/notifications.py`` 擴充：``send_notification(tier,
  severity, payload, interactive=False)`` — interactive=True 時走
  R1 ChatOps bridge

Row 2935 shipped the severity spec; rows 2936 / 2939 / 2940 shipped
severity-driven implicit fan-out via :func:`_dispatch_external`. THIS
row owns the *tier-explicit* surface: callers (R9 watchdog event
taxonomy in row 2942) hand the dispatcher a specific tier (or set of
tiers), an optional severity tag, a payload dict, and an
``interactive`` flag — the dispatcher fires exactly those tiers.

What we lock here:

  1. Tier normalisation — single string, list, set, frozenset, tuple,
     or ``None`` all collapse to the same internal frozenset.
  2. Unknown tier identifiers raise ``ValueError`` (drift guard
     against typos / future-tier-added-but-not-supported).
  3. ``tier=None`` + ``severity="P1"`` falls back to
     :data:`backend.severity.SEVERITY_TIER_MAPPING` so a caller using
     only the severity gets the mapped fan-out (P1 → 4 legs).
  4. ``tier="L4_PAGERDUTY"`` + ``severity="P1"`` fires PagerDuty only
     — the explicit tier WINS over the severity-driven mapping. This
     is the "tier-explicit" contract and is the whole reason this API
     exists separate from :func:`notify`.
  5. ``interactive=True`` adds ``L2_CHATOPS_INTERACTIVE`` to the tier
     set automatically and routes through R1's :func:`_dispatch_chatops`
     surface with caller-supplied buttons / channel from the payload.
  6. ``interactive=True`` without explicit buttons falls back to
     :func:`_dispatch_chatops_severity` (default ack / inject-hint /
     view-logs button set, channel ``"*"`` broadcast).
  7. ``payload`` accepts either a dict (constructs a ``Notification``)
     or a pre-built ``Notification`` instance (severity overrides any
     existing value on the model).
  8. ``payload`` dict missing ``title`` raises ``ValueError`` — title
     is the one universally required field.
  9. SSE bus payload carries the severity tag (frontend per-card
     badge contract from row 2935).
 10. Slack / Jira / PagerDuty / SMS legs each check their config knob
     individually — unconfigured leg silently skips (mirrors
     :func:`_dispatch_external` semantics).
 11. ``L1_LOG_EMAIL`` leg appends to ``_DIGEST_BUFFER`` and is NOT
     counted toward dispatch_status (best-effort, parallels P3 path).
 12. ChatOps leg is NOT counted toward dispatch_status either (live
     surface, durable record is the persisted notification row).
 13. dispatch_status update — ``sent`` when all required legs
     succeed, ``failed`` when any required leg fails, ``skipped``
     when no required leg was configured.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from backend.models import Notification, Severity


# ─────────────────────────────────────────────────────────────────
#  Fixtures (shared shape with row 2939 / 2940 contract tests)
# ─────────────────────────────────────────────────────────────────


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
    """Replace ``asyncio.create_subprocess_exec`` with a capture so the
    HTTP-based dispatchers (Slack / Jira / PagerDuty / SMS) run their
    full code path without firing real curl.
    """
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
def captured_chatops(monkeypatch):
    """Spy on ``chatops_bridge.send_interactive`` so both
    ``_dispatch_chatops`` (R1 explicit) and
    ``_dispatch_chatops_severity`` (default) routes can be observed.
    """
    calls: list[dict] = []

    from backend import chatops_bridge as bridge

    class _StubOutboundMessage:
        def __init__(self, channel, title, body, buttons, meta):
            self.id = "cm-stub"
            self.ts = 0.0
            self.channel = channel
            self.title = title
            self.body = body
            self.buttons = buttons
            self.meta = dict(meta or {})

        def to_dict(self):
            return {
                "id": self.id, "ts": self.ts, "channel": self.channel,
                "title": self.title, "body": self.body,
                "buttons": [b.__dict__ for b in self.buttons],
                "meta": dict(self.meta),
            }

    async def _fake_send_interactive(channel, message, *, title="OmniSight",
                                     buttons=None, meta=None):
        calls.append({
            "channel": channel,
            "title": title,
            "body": message,
            "buttons": list(buttons or []),
            "meta": dict(meta or {}),
        })
        return _StubOutboundMessage(channel, title, message,
                                    list(buttons or []), meta or {})

    monkeypatch.setattr(bridge, "send_interactive", _fake_send_interactive)
    return calls


@pytest.fixture()
def configured_settings(monkeypatch):
    from backend.config import settings

    monkeypatch.setattr(settings, "notification_slack_webhook", "https://hooks.slack.test/T0/B0/X")
    monkeypatch.setattr(settings, "notification_slack_mention", "U_ONCALL")
    monkeypatch.setattr(settings, "notification_jira_url", "https://jira.test")
    monkeypatch.setattr(settings, "notification_jira_token", "tk")
    monkeypatch.setattr(settings, "notification_jira_project", "OMNI")
    monkeypatch.setattr(settings, "notification_pagerduty_key", "pd-routing-key")
    monkeypatch.setattr(settings, "notification_sms_webhook", "https://sms.gateway.test/send")
    monkeypatch.setattr(settings, "notification_sms_to", "+15551234567")
    monkeypatch.setattr(settings, "notification_max_retries", 1)
    monkeypatch.setattr(settings, "notification_retry_backoff", 0)
    return settings


@pytest.fixture()
def stub_persistence(monkeypatch):
    from backend import db, db_pool

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


@pytest.fixture(autouse=True)
def reset_digest_state(monkeypatch):
    """Reset the digest buffer + flags between tests so the L1_LOG_EMAIL
    leg is observable cleanly."""
    from backend import notifications as n
    n._DIGEST_BUFFER.clear()
    monkeypatch.setattr(n, "_DIGEST_OVERFLOW_WARNED", False)
    yield
    n._DIGEST_BUFFER.clear()


@pytest.fixture()
def captured_bus(monkeypatch):
    """Capture SSE bus.publish calls so we can assert the severity tag
    rides on the broadcast payload (row 2935 frontend badge contract).
    """
    captured: list[tuple[str, dict]] = []
    from backend.events import bus

    real_publish = bus.publish

    def _capture(event, payload):
        captured.append((event, payload))
        return real_publish(event, payload)

    monkeypatch.setattr(bus, "publish", _capture)
    return captured


# ─────────────────────────────────────────────────────────────────
#  #1 — Tier normalisation
# ─────────────────────────────────────────────────────────────────


class TestTierNormalisation:
    @pytest.mark.asyncio
    async def test_tier_string_single(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """A single tier passed as a string fires only that one leg."""
        from backend.notifications import send_notification
        from backend.severity import L4_PAGERDUTY

        await send_notification(
            tier=L4_PAGERDUTY,
            severity="P1",
            payload={"title": "system down", "level": "critical"},
        )

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("events.pagerduty.com" in u for u in urls), urls
        assert not any("hooks.slack.test" in u for u in urls)
        assert not any("jira.test" in u for u in urls)
        assert not any("sms.gateway.test" in u for u in urls)

    @pytest.mark.asyncio
    async def test_tier_iterable_set(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """A set of tiers fires every named leg."""
        from backend.notifications import send_notification
        from backend.severity import L4_PAGERDUTY, L3_JIRA

        await send_notification(
            tier={L4_PAGERDUTY, L3_JIRA},
            severity="P1",
            payload={"title": "system down", "level": "critical"},
        )

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("events.pagerduty.com" in u for u in urls), urls
        assert any("jira.test" in u for u in urls), urls
        # Slack / SMS not requested.
        assert not any("hooks.slack.test" in u for u in urls)
        assert not any("sms.gateway.test" in u for u in urls)

    @pytest.mark.asyncio
    async def test_tier_iterable_list(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """A list of tiers (different iterable shape) collapses identically."""
        from backend.notifications import send_notification
        from backend.severity import L4_SMS

        await send_notification(
            tier=[L4_SMS],
            severity="P1",
            payload={"title": "wake on-call", "level": "critical"},
        )

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("sms.gateway.test" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_tier_iterable_tuple(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        await send_notification(
            tier=(L3_JIRA,),
            severity="P2",
            payload={"title": "task blocked", "level": "action"},
        )
        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("jira.test" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_unknown_tier_raises_value_error(
        self, configured_settings, stub_persistence,
    ) -> None:
        """An unknown tier identifier is rejected — drift guard against
        typo'd tier strings sneaking past the call site."""
        from backend.notifications import send_notification

        with pytest.raises(ValueError, match="unknown tier"):
            await send_notification(
                tier="L9_FAKE_TIER",
                severity="P1",
                payload={"title": "x", "level": "critical"},
            )

    @pytest.mark.asyncio
    async def test_tier_none_with_severity_falls_back_to_mapping(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``tier=None`` + ``severity="P1"`` falls back to the severity
        mapping (P1 → 4 legs)."""
        from backend.notifications import send_notification

        await send_notification(
            tier=None,
            severity="P1",
            payload={"title": "system down", "level": "critical"},
        )
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        # P1 mapping is {L4_PAGERDUTY, L4_SMS, L3_JIRA, L2_IM_WEBHOOK}.
        assert any("events.pagerduty.com" in u for u in urls), urls
        assert any("sms.gateway.test" in u for u in urls), urls
        assert any("jira.test" in u for u in urls), urls
        assert any("hooks.slack.test" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_tier_none_no_severity_persists_only(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """No tier + no severity → notification persists + SSEs but no
        external fan-out fires."""
        from backend.notifications import send_notification

        await send_notification(
            tier=None,
            severity=None,
            payload={"title": "informational", "level": "info"},
        )

        # No curl invocations of any kind.
        assert fake_subprocess == [], fake_subprocess


# ─────────────────────────────────────────────────────────────────
#  #2 — Tier WINS over severity mapping
# ─────────────────────────────────────────────────────────────────


class TestTierExplicitWinsOverSeverity:
    @pytest.mark.asyncio
    async def test_explicit_tier_overrides_severity_implicit_mapping(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``tier=L4_PAGERDUTY`` + ``severity="P1"`` — only PagerDuty
        fires. The explicit tier wins over P1's implicit 4-leg mapping.

        This is the core "tier-explicit" contract that distinguishes
        ``send_notification`` from ``notify`` (the latter would fire
        all four P1 legs via :func:`_dispatch_external`).
        """
        from backend.notifications import send_notification
        from backend.severity import L4_PAGERDUTY

        await send_notification(
            tier=L4_PAGERDUTY,
            severity="P1",
            payload={"title": "page only", "level": "critical"},
        )
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("events.pagerduty.com" in u for u in urls), urls
        # Slack / SMS / Jira NOT fired despite P1 mapping including them.
        assert not any("hooks.slack.test" in u for u in urls), urls
        assert not any("sms.gateway.test" in u for u in urls), urls
        assert not any("jira.test" in u for u in urls), urls
        # ChatOps NOT spawned either.
        assert captured_chatops == []


# ─────────────────────────────────────────────────────────────────
#  #3 — interactive=True behaviour
# ─────────────────────────────────────────────────────────────────


class TestInteractiveFlag:
    @pytest.mark.asyncio
    async def test_interactive_true_adds_chatops_tier(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``interactive=True`` implicitly adds ``L2_CHATOPS_INTERACTIVE``
        to the tier set even when the caller didn't list it."""
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        await send_notification(
            tier=L3_JIRA,
            severity="P2",
            payload={"title": "blocked", "level": "action"},
            interactive=True,
        )
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("jira.test" in u for u in urls), urls
        # ChatOps leg fired exactly once.
        assert len(captured_chatops) == 1

    @pytest.mark.asyncio
    async def test_interactive_true_with_explicit_buttons_uses_r1_path(
        self, captured_chatops, configured_settings, stub_persistence,
    ) -> None:
        """When ``interactive=True`` AND the payload includes
        ``interactive_buttons``, the R1 explicit ``_dispatch_chatops``
        path is used so caller-supplied buttons survive verbatim.
        """
        from backend.notifications import send_notification

        await send_notification(
            tier=None,
            severity=None,
            payload={
                "title": "approve deploy",
                "level": "action",
                "interactive_channel": "discord",
                "interactive_buttons": [
                    {"id": "approve", "label": "Approve", "style": "success"},
                    {"id": "deny", "label": "Deny", "style": "danger"},
                ],
            },
            interactive=True,
        )
        await asyncio.sleep(0)

        assert len(captured_chatops) == 1
        call = captured_chatops[0]
        # R1 explicit path uses the caller's channel verbatim.
        assert call["channel"] == "discord", call
        # Buttons round-trip with caller-supplied ids + styles.
        ids = [b.id for b in call["buttons"]]
        assert ids == ["approve", "deny"], ids
        styles = [b.style for b in call["buttons"]]
        assert styles == ["success", "danger"], styles

    @pytest.mark.asyncio
    async def test_interactive_true_without_buttons_uses_default_path(
        self, captured_chatops, configured_settings, stub_persistence,
    ) -> None:
        """``interactive=True`` without explicit buttons falls back to
        the row 2939 default ack/inject-hint/view-logs broadcast button
        set."""
        from backend.notifications import send_notification

        await send_notification(
            tier=None,
            severity="P2",
            payload={"title": "deadlock", "level": "action"},
            interactive=True,
        )
        await asyncio.sleep(0)

        assert len(captured_chatops) == 1
        call = captured_chatops[0]
        # Default path → broadcast channel
        assert call["channel"] == "*", call
        # Default 3-button set
        ids = [b.id for b in call["buttons"]]
        assert ids == ["ack", "inject_hint", "view_logs"], ids

    @pytest.mark.asyncio
    async def test_interactive_false_does_not_spawn_chatops(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``interactive=False`` (default) with a tier that doesn't
        include ChatOps does NOT spawn a chat card."""
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        await send_notification(
            tier=L3_JIRA,
            severity="P2",
            payload={"title": "blocked", "level": "action"},
            interactive=False,
        )
        await asyncio.sleep(0)

        # Jira fired but ChatOps did NOT.
        assert captured_chatops == []


# ─────────────────────────────────────────────────────────────────
#  #4 — payload polymorphism
# ─────────────────────────────────────────────────────────────────


class TestPayloadPolymorphism:
    @pytest.mark.asyncio
    async def test_payload_dict_constructs_notification(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        notif = await send_notification(
            tier=L3_JIRA,
            severity="P2",
            payload={
                "title": "task blocked",
                "message": "agent looping for 12m",
                "source": "watchdog",
                "level": "action",
            },
        )
        assert isinstance(notif, Notification)
        assert notif.title == "task blocked"
        assert notif.source == "watchdog"
        assert notif.severity == Severity.P2

    @pytest.mark.asyncio
    async def test_payload_prebuilt_notification_used_as_is(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Pre-built ``Notification`` objects pass through; the
        ``severity`` kwarg overrides any value already on the model.
        """
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        prebuilt = Notification(
            id="notif-prebuilt",
            level="action",
            title="rebuilt elsewhere",
            message="some upstream code already built this",
            source="agent:firmware-alpha",
            timestamp="2026-04-25T00:00:00",
            severity=None,
        )

        result = await send_notification(
            tier=L3_JIRA,
            severity="P2",
            payload=prebuilt,
        )
        # Severity override flowed through.
        assert result.severity == Severity.P2
        # Other fields preserved.
        assert result.id == "notif-prebuilt"
        assert result.title == "rebuilt elsewhere"

    @pytest.mark.asyncio
    async def test_payload_missing_title_raises(
        self, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        with pytest.raises(ValueError, match="title is required"):
            await send_notification(
                tier=L3_JIRA,
                severity="P2",
                payload={"message": "no title"},
            )


# ─────────────────────────────────────────────────────────────────
#  #5 — SSE bus carries the severity tag (row 2935 contract)
# ─────────────────────────────────────────────────────────────────


class TestSseBusContract:
    @pytest.mark.asyncio
    async def test_sse_payload_includes_severity_tag(
        self, captured_bus, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import send_notification

        await send_notification(
            tier=None,
            severity="P1",
            payload={"title": "system down", "level": "critical"},
        )

        # Find the notification publish call.
        notif_events = [p for (e, p) in captured_bus if e == "notification"]
        assert len(notif_events) == 1
        payload = notif_events[0]
        assert payload["severity"] == "P1"
        assert payload["level"] == "critical"
        assert payload["title"] == "system down"

    @pytest.mark.asyncio
    async def test_sse_payload_severity_none_for_legacy(
        self, captured_bus, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        await send_notification(
            tier=L3_JIRA,
            severity=None,
            payload={"title": "no severity", "level": "action"},
        )

        notif_events = [p for (e, p) in captured_bus if e == "notification"]
        assert len(notif_events) == 1
        assert notif_events[0]["severity"] is None


# ─────────────────────────────────────────────────────────────────
#  #6 — L1_LOG_EMAIL leg appends to digest buffer
# ─────────────────────────────────────────────────────────────────


class TestLogEmailLeg:
    @pytest.mark.asyncio
    async def test_l1_log_email_appends_to_buffer(
        self, configured_settings, stub_persistence,
    ) -> None:
        """``tier=L1_LOG_EMAIL`` appends the notification to the per-
        process digest buffer (parallel to row 2940 P3 path)."""
        from backend import notifications as n
        from backend.notifications import send_notification
        from backend.severity import L1_LOG_EMAIL

        await send_notification(
            tier=L1_LOG_EMAIL,
            severity="P3",
            payload={
                "title": "auto-recovery",
                "message": "agent restarted cleanly",
                "level": "info",
            },
        )

        assert len(n._DIGEST_BUFFER) == 1
        ev = list(n._DIGEST_BUFFER)[0]
        assert ev.title == "auto-recovery"
        assert ev.severity == Severity.P3


# ─────────────────────────────────────────────────────────────────
#  #7 — dispatch_status semantics (sent / failed / skipped)
# ─────────────────────────────────────────────────────────────────


class TestDispatchStatusUpdates:
    @pytest.mark.asyncio
    async def test_dispatch_status_sent_when_all_legs_succeed(
        self, fake_subprocess, configured_settings, monkeypatch,
    ) -> None:
        """When every required leg succeeds, dispatch_status =
        ``sent`` with attempts=1."""
        from backend import db, db_pool, notifications as n
        from backend.severity import L4_PAGERDUTY

        captured_status: list[tuple] = []

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

        async def _capture_update(_conn, notif_id, status, **kw):
            captured_status.append((notif_id, status, kw))

        monkeypatch.setattr(db, "insert_notification", _noop_insert)
        monkeypatch.setattr(db, "update_notification_dispatch", _capture_update)

        await n.send_notification(
            tier=L4_PAGERDUTY,
            severity="P1",
            payload={"title": "ok", "level": "critical"},
        )

        assert len(captured_status) == 1
        _, status, kw = captured_status[0]
        assert status == "sent"
        assert kw.get("attempts") == 1

    @pytest.mark.asyncio
    async def test_dispatch_status_skipped_when_no_required_leg(
        self, configured_settings, monkeypatch,
    ) -> None:
        """When only L1_LOG_EMAIL or ChatOps fires (neither is counted
        toward dispatch_status), the status update is ``skipped``."""
        from backend import db, db_pool, notifications as n
        from backend.severity import L1_LOG_EMAIL

        captured_status: list[tuple] = []

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

        async def _capture_update(_conn, notif_id, status, **kw):
            captured_status.append((notif_id, status, kw))

        monkeypatch.setattr(db, "insert_notification", _noop_insert)
        monkeypatch.setattr(db, "update_notification_dispatch", _capture_update)

        await n.send_notification(
            tier=L1_LOG_EMAIL,
            severity="P3",
            payload={"title": "auto-recovery", "level": "info"},
        )

        assert len(captured_status) == 1
        _, status, _ = captured_status[0]
        assert status == "skipped"

    @pytest.mark.asyncio
    async def test_dispatch_status_failed_on_curl_error(
        self, configured_settings, monkeypatch,
    ) -> None:
        """When a required leg fails (curl returncode != 0), dispatch
        status becomes ``failed`` and the channel is named in the
        error string."""
        from backend import db, db_pool, notifications as n
        from backend.severity import L4_PAGERDUTY

        # Force curl to fail.
        class _BadProc:
            returncode = 7

            async def communicate(self):
                return b"", b"timeout"

        async def _bad_exec(*args, **kwargs):
            return _BadProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _bad_exec)

        captured_status: list[tuple] = []

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

        async def _capture_update(_conn, notif_id, status, **kw):
            captured_status.append((notif_id, status, kw))

        monkeypatch.setattr(db, "insert_notification", _noop_insert)
        monkeypatch.setattr(db, "update_notification_dispatch", _capture_update)

        await n.send_notification(
            tier=L4_PAGERDUTY,
            severity="P1",
            payload={"title": "page", "level": "critical"},
        )

        assert len(captured_status) == 1
        _, status, kw = captured_status[0]
        assert status == "failed"
        assert "pagerduty" in (kw.get("error") or "")


# ─────────────────────────────────────────────────────────────────
#  #8 — Jira / Slack severity-aware payload still applies
# ─────────────────────────────────────────────────────────────────


class TestSeverityAwarePayloadPropagates:
    @pytest.mark.asyncio
    async def test_jira_p2_carries_blocked_label(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """When ``severity="P2"`` flows through the explicit dispatcher,
        the Jira leg still attaches the ``blocked`` label (row 2939
        contract)."""
        from backend.notifications import send_notification
        from backend.severity import L3_JIRA

        await send_notification(
            tier=L3_JIRA,
            severity="P2",
            payload={"title": "deadlock", "level": "action"},
        )

        jira_calls = [c for c in fake_subprocess if len(c.cmd) > 4 and "jira" in c.cmd[4]]
        assert len(jira_calls) == 1
        body = jira_calls[0].body
        labels = body["fields"].get("labels") or []
        assert "severity-P2" in labels
        assert "blocked" in labels

    @pytest.mark.asyncio
    async def test_slack_p1_carries_broadcast_mentions(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """P1 → Slack leg still carries dual @everyone / <!channel>
        broadcast tokens (row 2936 contract)."""
        from backend.notifications import send_notification
        from backend.severity import L2_IM_WEBHOOK

        await send_notification(
            tier=L2_IM_WEBHOOK,
            severity="P1",
            payload={"title": "down", "level": "critical"},
        )

        slack_calls = [c for c in fake_subprocess if len(c.cmd) > 4 and "slack" in c.cmd[4]]
        assert len(slack_calls) == 1
        text = slack_calls[0].body["text"]
        assert "@everyone" in text
        assert "<!channel>" in text
        assert "[severity:P1]" in text

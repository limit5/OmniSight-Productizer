"""R9 row 2942 (#315) — watchdog event taxonomy contract tests.

Locks the row 2942 sub-bullet verbatim:

  統一 event taxonomy：``watchdog.p1_system_down`` /
  ``watchdog.p2_cognitive_deadlock`` / ``watchdog.p3_auto_recovery``
  → 各自映射 L1-L4 + severity tag

What we lock here:

  1. Three (and only three) canonical event-name strings live in the
     :class:`WatchdogEvent` enum. Adding a 4th means an explicit code
     change and an explicit test update — not a silent string typo.
  2. Each event maps to the exact severity tag from row 2935 spec
     (P1 → P1, P2 → P2, P3 → P3 — sounds tautological but locks the
     "no severity inflation/deflation per event" contract).
  3. Each event's tier set is identical to ``SEVERITY_TIER_MAPPING``
     for the corresponding severity — i.e. the watchdog taxonomy does
     NOT diverge from the generic spec. This is a drift guard: any
     future change to one side without the other CI-reds.
  4. ``EVENT_SPEC`` is an immutable :class:`MappingProxyType` — spec
     drift via test monkeypatching is rejected.
  5. ``spec_for`` accepts both the enum and its string value
     ergonomically; unknown events raise ``ValueError``.
  6. ``emit`` plumbs through to :func:`send_notification` with the
     event's tier set + severity + payload.
  7. Each event fires the correct fan-out legs end-to-end:
       - P1_SYSTEM_DOWN       → PagerDuty + SMS + Jira + Slack
       - P2_COGNITIVE_DEADLOCK → Jira + ChatOps interactive
       - P3_AUTO_RECOVERY     → log + email digest buffer (no curl)
  8. ``emit`` defaults missing ``level`` and ``source`` from the
     event spec so callers can fire with just ``{title}``.
  9. ``emit`` rejects payload missing ``title`` (required field).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest


# ─────────────────────────────────────────────────────────────────
#  Fixtures (shape mirrors test_send_notification_dispatcher.py)
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
    """Reset the row 2940 digest buffer + flags between tests so the
    P3 path's L1_LOG_EMAIL leg is observable cleanly."""
    from backend import notifications as n
    n._DIGEST_BUFFER.clear()
    monkeypatch.setattr(n, "_DIGEST_OVERFLOW_WARNED", False)
    yield
    n._DIGEST_BUFFER.clear()


# ─────────────────────────────────────────────────────────────────
#  #1 — Enum cardinality + canonical names
# ─────────────────────────────────────────────────────────────────


class TestWatchdogEventEnum:
    def test_three_canonical_events(self) -> None:
        """Exactly three events — adding a 4th requires an explicit
        spec entry + test update (no silent expansion)."""
        from backend.watchdog_events import WatchdogEvent

        members = list(WatchdogEvent)
        assert len(members) == 3, members

    def test_event_string_values_match_spec(self) -> None:
        """Event names are the exact ``watchdog.p*`` strings from row
        2942 — no abbreviations, no separators changes."""
        from backend.watchdog_events import WatchdogEvent

        assert WatchdogEvent.P1_SYSTEM_DOWN.value == "watchdog.p1_system_down"
        assert WatchdogEvent.P2_COGNITIVE_DEADLOCK.value == "watchdog.p2_cognitive_deadlock"
        assert WatchdogEvent.P3_AUTO_RECOVERY.value == "watchdog.p3_auto_recovery"

    def test_event_is_str_enum(self) -> None:
        """Members behave as strings so legacy emitters that already
        log a string event name interoperate without conversion."""
        from backend.watchdog_events import WatchdogEvent

        assert isinstance(WatchdogEvent.P1_SYSTEM_DOWN, str)
        assert WatchdogEvent.P1_SYSTEM_DOWN == "watchdog.p1_system_down"


# ─────────────────────────────────────────────────────────────────
#  #2 — Per-event severity tag
# ─────────────────────────────────────────────────────────────────


class TestEventSeverityMapping:
    def test_p1_event_has_p1_severity(self) -> None:
        from backend.severity import Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P1_SYSTEM_DOWN].severity is Severity.P1

    def test_p2_event_has_p2_severity(self) -> None:
        from backend.severity import Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P2_COGNITIVE_DEADLOCK].severity is Severity.P2

    def test_p3_event_has_p3_severity(self) -> None:
        from backend.severity import Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P3_AUTO_RECOVERY].severity is Severity.P3


# ─────────────────────────────────────────────────────────────────
#  #3 — Per-event tier set (drift guard against severity spec)
# ─────────────────────────────────────────────────────────────────


class TestEventTierMapping:
    def test_p1_tiers_match_p1_severity_mapping(self) -> None:
        """Drift guard: ``EVENT_SPEC[P1].tiers`` must equal
        ``SEVERITY_TIER_MAPPING[Severity.P1]``. If row 2935's spec
        ever moves a tier into/out-of P1, this test fires."""
        from backend.severity import SEVERITY_TIER_MAPPING, Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert (
            EVENT_SPEC[WatchdogEvent.P1_SYSTEM_DOWN].tiers
            == SEVERITY_TIER_MAPPING[Severity.P1]
        )

    def test_p2_tiers_match_p2_severity_mapping(self) -> None:
        from backend.severity import SEVERITY_TIER_MAPPING, Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert (
            EVENT_SPEC[WatchdogEvent.P2_COGNITIVE_DEADLOCK].tiers
            == SEVERITY_TIER_MAPPING[Severity.P2]
        )

    def test_p3_tiers_match_p3_severity_mapping(self) -> None:
        from backend.severity import SEVERITY_TIER_MAPPING, Severity
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert (
            EVENT_SPEC[WatchdogEvent.P3_AUTO_RECOVERY].tiers
            == SEVERITY_TIER_MAPPING[Severity.P3]
        )

    def test_p1_tier_set_literal(self) -> None:
        """Belt-and-suspenders: also lock the literal tier set so the
        row 2942 spec is auditable from this test alone (without
        chasing through severity.py)."""
        from backend.severity import L2_IM_WEBHOOK, L3_JIRA, L4_PAGERDUTY, L4_SMS
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P1_SYSTEM_DOWN].tiers == frozenset({
            L4_PAGERDUTY, L4_SMS, L3_JIRA, L2_IM_WEBHOOK,
        })

    def test_p2_tier_set_literal(self) -> None:
        from backend.severity import L2_CHATOPS_INTERACTIVE, L3_JIRA
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P2_COGNITIVE_DEADLOCK].tiers == frozenset({
            L3_JIRA, L2_CHATOPS_INTERACTIVE,
        })

    def test_p3_tier_set_literal(self) -> None:
        from backend.severity import L1_LOG_EMAIL
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        assert EVENT_SPEC[WatchdogEvent.P3_AUTO_RECOVERY].tiers == frozenset({
            L1_LOG_EMAIL,
        })

    def test_p2_tiers_disjoint_from_p1_broadcast(self) -> None:
        """Negative regression guard: P2 must NOT include PagerDuty /
        SMS / IM webhook (those are P1 broadcast tiers; P2 is ticket
        + interactive only). Mirrors row 2939's
        ``test_p2_jira_chatops_only`` invariant at the spec layer."""
        from backend.severity import L2_IM_WEBHOOK, L4_PAGERDUTY, L4_SMS
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        p2_tiers = EVENT_SPEC[WatchdogEvent.P2_COGNITIVE_DEADLOCK].tiers
        assert L4_PAGERDUTY not in p2_tiers
        assert L4_SMS not in p2_tiers
        assert L2_IM_WEBHOOK not in p2_tiers

    def test_p3_tiers_disjoint_from_higher_severities(self) -> None:
        """Negative regression guard: P3 must NOT include any tier
        from P1 or P2 (P3 is informational digest only)."""
        from backend.severity import (
            L2_CHATOPS_INTERACTIVE,
            L2_IM_WEBHOOK,
            L3_JIRA,
            L4_PAGERDUTY,
            L4_SMS,
        )
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        p3_tiers = EVENT_SPEC[WatchdogEvent.P3_AUTO_RECOVERY].tiers
        for higher_tier in (L4_PAGERDUTY, L4_SMS, L3_JIRA,
                            L2_IM_WEBHOOK, L2_CHATOPS_INTERACTIVE):
            assert higher_tier not in p3_tiers


# ─────────────────────────────────────────────────────────────────
#  #4 — EVENT_SPEC immutability
# ─────────────────────────────────────────────────────────────────


class TestEventSpecImmutability:
    def test_event_spec_is_mapping_proxy(self) -> None:
        """Spec drift via test monkeypatch / dispatcher mistake must
        be rejected at runtime — mirrors row 2935's discipline."""
        from types import MappingProxyType

        from backend.watchdog_events import EVENT_SPEC

        assert isinstance(EVENT_SPEC, MappingProxyType)

    def test_event_spec_assignment_raises(self) -> None:
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent

        with pytest.raises(TypeError):
            EVENT_SPEC[WatchdogEvent.P1_SYSTEM_DOWN] = None  # type: ignore[index]


# ─────────────────────────────────────────────────────────────────
#  #5 — spec_for ergonomics
# ─────────────────────────────────────────────────────────────────


class TestSpecForHelper:
    def test_spec_for_accepts_enum(self) -> None:
        from backend.watchdog_events import EVENT_SPEC, WatchdogEvent, spec_for

        assert spec_for(WatchdogEvent.P1_SYSTEM_DOWN) is EVENT_SPEC[
            WatchdogEvent.P1_SYSTEM_DOWN
        ]

    def test_spec_for_accepts_string(self) -> None:
        from backend.watchdog_events import WatchdogEvent, spec_for

        assert spec_for("watchdog.p2_cognitive_deadlock") is spec_for(
            WatchdogEvent.P2_COGNITIVE_DEADLOCK,
        )

    def test_spec_for_unknown_string_raises(self) -> None:
        from backend.watchdog_events import spec_for

        with pytest.raises(ValueError, match="unknown event"):
            spec_for("watchdog.bogus_event")


# ─────────────────────────────────────────────────────────────────
#  #6 — emit() end-to-end fan-out per event
# ─────────────────────────────────────────────────────────────────


class TestEmitFanOut:
    @pytest.mark.asyncio
    async def test_p1_system_down_fires_four_durable_legs(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """``watchdog.p1_system_down`` → PagerDuty + SMS + Jira + Slack
        (the four P1 broadcast tiers) all fire. ChatOps not in P1
        spec, so no chat card."""
        from backend.watchdog_events import WatchdogEvent, emit

        await emit(WatchdogEvent.P1_SYSTEM_DOWN, {
            "title": "kernel oom-killer fired on backend-a",
            "message": "RSS spike to 7.8 GiB triggered cgroup OOM.",
        })
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("events.pagerduty.com" in u for u in urls), urls
        assert any("sms.gateway.test" in u for u in urls), urls
        assert any("jira.test" in u for u in urls), urls
        assert any("hooks.slack.test" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_p2_cognitive_deadlock_fires_jira_and_chatops(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``watchdog.p2_cognitive_deadlock`` → Jira (with ``blocked``
        label per row 2939 contract) + ChatOps interactive (default
        ack/inject_hint/view_logs button set per row 2939 helper).
        NOT PagerDuty, NOT SMS, NOT Slack IM webhook."""
        from backend.watchdog_events import WatchdogEvent, emit

        await emit(WatchdogEvent.P2_COGNITIVE_DEADLOCK, {
            "title": "agent firmware-alpha looping for 14m",
            "message": "semantic entropy below 0.05 across last 8 turns.",
        })
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("jira.test" in u for u in urls), urls
        # ChatOps fired exactly once with default broadcast channel.
        assert len(captured_chatops) == 1, captured_chatops
        assert captured_chatops[0]["channel"] == "*"

        # Negative: P1 broadcast tiers MUST NOT fire.
        assert not any("events.pagerduty.com" in u for u in urls), urls
        assert not any("sms.gateway.test" in u for u in urls), urls
        assert not any("hooks.slack.test" in u for u in urls), urls

    @pytest.mark.asyncio
    async def test_p2_jira_payload_carries_blocked_label(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """End-to-end check that row 2939's ``blocked`` Jira label is
        still applied when P2 fires through the watchdog taxonomy."""
        from backend.watchdog_events import WatchdogEvent, emit

        await emit(WatchdogEvent.P2_COGNITIVE_DEADLOCK, {
            "title": "agent stuck",
            "message": "semantic entropy below threshold",
        })
        await asyncio.sleep(0)

        jira_bodies = [
            c.body for c in fake_subprocess
            if c.body and len(c.cmd) > 4 and "jira.test" in c.cmd[4]
        ]
        assert jira_bodies, fake_subprocess
        labels = jira_bodies[0]["fields"].get("labels", [])
        assert "blocked" in labels, labels
        assert "severity-P2" in labels, labels

    @pytest.mark.asyncio
    async def test_p3_auto_recovery_logs_and_buffers_only(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """``watchdog.p3_auto_recovery`` → log line + email digest
        buffer. NO curl invocations of any kind, NO ChatOps."""
        from backend import notifications as n
        from backend.watchdog_events import WatchdogEvent, emit

        before = len(n._DIGEST_BUFFER)
        await emit(WatchdogEvent.P3_AUTO_RECOVERY, {
            "title": "scratchpad reload succeeded for task-42",
            "message": "checkpoint resumed after 18s downtime.",
        })
        await asyncio.sleep(0)

        # Zero external dispatches.
        assert fake_subprocess == [], fake_subprocess
        assert captured_chatops == [], captured_chatops
        # Digest buffer received the event.
        assert len(n._DIGEST_BUFFER) == before + 1


# ─────────────────────────────────────────────────────────────────
#  #7 — emit() default-fill behaviour
# ─────────────────────────────────────────────────────────────────


class TestEmitDefaults:
    @pytest.mark.asyncio
    async def test_emit_defaults_level_from_event_spec(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """If caller omits ``level``, ``emit`` fills from
        ``EVENT_SPEC[event].default_level``. Verified by the fact that
        the persisted Notification carries the level."""
        from backend.models import Notification, Severity
        from backend.watchdog_events import WatchdogEvent, emit

        notif = await emit(
            WatchdogEvent.P1_SYSTEM_DOWN, {"title": "system down"},
        )
        assert isinstance(notif, Notification)
        # Default level for P1_SYSTEM_DOWN is "critical".
        notif_level = (
            notif.level.value if hasattr(notif.level, "value")
            else str(notif.level)
        )
        assert notif_level == "critical", notif_level
        assert notif.severity == Severity.P1

    @pytest.mark.asyncio
    async def test_emit_defaults_source_from_event_spec(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Missing ``source`` defaults to the event spec's
        ``default_source`` (e.g. ``watchdog.agent`` for P2)."""
        from backend.watchdog_events import WatchdogEvent, emit

        notif = await emit(
            WatchdogEvent.P2_COGNITIVE_DEADLOCK,
            {"title": "agent stuck"},
        )
        assert notif.source == "watchdog.agent"

    @pytest.mark.asyncio
    async def test_emit_caller_level_overrides_default(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Caller-supplied ``level`` wins over ``default_level``."""
        from backend.watchdog_events import WatchdogEvent, emit

        notif = await emit(
            WatchdogEvent.P1_SYSTEM_DOWN,
            {"title": "system down", "level": "warning"},
        )
        notif_level = (
            notif.level.value if hasattr(notif.level, "value")
            else str(notif.level)
        )
        assert notif_level == "warning", notif_level

    @pytest.mark.asyncio
    async def test_emit_caller_source_overrides_default(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.watchdog_events import WatchdogEvent, emit

        notif = await emit(
            WatchdogEvent.P3_AUTO_RECOVERY,
            {"title": "ok", "source": "agent:firmware-alpha"},
        )
        assert notif.source == "agent:firmware-alpha"


# ─────────────────────────────────────────────────────────────────
#  #8 — emit() validation
# ─────────────────────────────────────────────────────────────────


class TestEmitValidation:
    @pytest.mark.asyncio
    async def test_emit_missing_title_raises(
        self, configured_settings, stub_persistence,
    ) -> None:
        """``payload['title']`` is required — drift guard against
        watchdog emitters that forget to summarise the event."""
        from backend.watchdog_events import WatchdogEvent, emit

        with pytest.raises(ValueError, match="title.*required"):
            await emit(WatchdogEvent.P1_SYSTEM_DOWN, {})

    @pytest.mark.asyncio
    async def test_emit_empty_title_raises(
        self, configured_settings, stub_persistence,
    ) -> None:
        from backend.watchdog_events import WatchdogEvent, emit

        with pytest.raises(ValueError, match="title.*required"):
            await emit(WatchdogEvent.P2_COGNITIVE_DEADLOCK, {"title": "   "})

    @pytest.mark.asyncio
    async def test_emit_unknown_event_string_raises(
        self, configured_settings, stub_persistence,
    ) -> None:
        from backend.watchdog_events import emit

        with pytest.raises(ValueError, match="unknown event"):
            await emit("watchdog.p4_made_up", {"title": "x"})

    @pytest.mark.asyncio
    async def test_emit_accepts_string_event_name(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Event name passed as the canonical string (``"watchdog.
        p3_auto_recovery"``) works identically to the enum — for
        legacy emitters that already log a string event name and
        don't want to depend on the enum."""
        from backend import notifications as n
        from backend.watchdog_events import emit

        before = len(n._DIGEST_BUFFER)
        await emit("watchdog.p3_auto_recovery", {"title": "string-form"})
        await asyncio.sleep(0)

        # Identical effect to the enum form.
        assert len(n._DIGEST_BUFFER) == before + 1
        assert fake_subprocess == []

"""R9 row 2939 (#315) — P2 fan-out dispatcher contract tests.

Locks the row 2939 sub-bullet verbatim:

  P2 (任務卡死) → L3 Jira (severity: P2, label: blocked) +
                  L2 ChatOps interactive (R1)

Row 2935 shipped the spec module + ``Notification.severity`` field +
``notify(severity=...)`` plumb-through; row 2936 shipped the P1 four-leg
fan-out (PagerDuty + SMS + Jira + Slack/Discord @everyone). THIS row
owns the P2 fan-out path: when ``severity="P2"`` the dispatcher fires
(a) ``_send_jira`` with ``labels=["severity-P2", "blocked"]`` and (b)
``_dispatch_chatops_severity`` against the R1 (#307) ChatOps bridge with
default channel ``"*"`` and a default ack / inject-hint / view-logs
button set so on-call can triage from inside chat.

What we lock here:

  1. ``_dispatch_external(notif)`` with ``notif.severity = P2`` fires
     Jira + ChatOps but does NOT fire PagerDuty / SMS / Slack-with-
     ``@everyone`` (those are P1-only — severity-additive ladder).
  2. The P2 dispatch fires even when ``notif.level == "info"`` (severity
     is additive — caller miss-labelling level must not silently drop
     P2 fan-out).
  3. ``_send_jira`` for P2 attaches **both** ``severity-P2`` and
     ``blocked`` labels, prefixes the description with ``[severity:P2]``,
     and keeps issuetype=Task / priority=High (P1's ``Bug`` / ``Highest``
     forcing is NOT extended to P2 — Jira boards already separate "Bug"
     vs "Task" lanes).
  4. ``_dispatch_chatops_severity`` pushes the message to channel ``"*"``
     (broadcast across all configured adapters) with the standard 3-button
     set (Acknowledge / Inject Hint / View Logs) and a meta dict carrying
     ``severity=P2`` for downstream button-handler routing.
  5. ChatOps severity-driven leg is *severity-only* — a legacy
     ``notify(level="action")`` without severity does NOT spawn the
     severity-driven ChatOps card (the bridge is reserved for severity-
     tagged events; legacy callers use the explicit ``interactive=True``
     path with their own buttons).
  6. Tier mapping coverage: ``SEVERITY_TIER_MAPPING[Severity.P2]`` is
     exactly ``{L3_JIRA, L2_CHATOPS_INTERACTIVE}`` (drift guard against
     row 2935 spec erosion).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from backend.models import Notification, Severity


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
    full code path without firing real curl. ChatOps does NOT go through
    curl — it goes through the bridge, captured separately via
    ``captured_chatops``.
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
    """Spy on ``chatops_bridge.send_interactive`` so the severity-driven
    ChatOps dispatcher can be asserted on without wiring real adapters.
    The spy returns a no-op ``OutboundMessage``-shaped object so callers
    that ``await out.to_dict()`` don't crash.
    """
    calls: list[dict] = []

    from backend import chatops_bridge as bridge

    class _StubOutboundMessage:
        def __init__(self, channel: str, title: str, body: str,
                     buttons: list, meta: dict) -> None:
            self.id = "cm-stub"
            self.ts = 0.0
            self.channel = channel
            self.title = title
            self.body = body
            self.buttons = buttons
            self.meta = dict(meta or {})

        def to_dict(self) -> dict:
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

    async def _noop_update(*a, **kw):
        return None

    monkeypatch.setattr(db, "update_notification_dispatch", _noop_update)


def _p2_notif(level: str = "action") -> Notification:
    return Notification(
        id="notif-p2-test",
        level=level,
        title="Agent task deadlocked",
        message="watchdog.p2_cognitive_deadlock — firmware-alpha looping for 12m",
        source="watchdog",
        timestamp="2026-04-25T00:00:00",
        severity=Severity.P2,
    )


def _legacy_action_notif() -> Notification:
    return Notification(
        id="notif-legacy-action",
        level="action",
        title="Tool failed",
        message="lint --strict returned 12 errors",
        source="ci",
        timestamp="2026-04-25T00:00:00",
        # severity intentionally omitted (None)
    )


# ─────────────────────────────────────────────────────────────────
#  #1 — _dispatch_external: P2 fires Jira + ChatOps only
# ─────────────────────────────────────────────────────────────────


class TestP2FanOutLegs:
    @pytest.mark.asyncio
    async def test_p2_fires_jira_and_chatops_only(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """P2 → Jira + ChatOps interactive. NOT PagerDuty, NOT SMS, NOT
        Slack-with-``@everyone``. The ladder is severity-additive: P2
        doesn't piggy-back on P1's broadcast tier.
        """
        from backend.notifications import _dispatch_external

        await _dispatch_external(_p2_notif(level="action"))
        # Give the fire-and-forget _dispatch_chatops_severity task a
        # chance to run — _dispatch_external schedules it via
        # create_task and returns before the bridge call completes.
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        # Slack fires (level=action triggers fire_slack via the legacy
        # OR-merge gate) but it does NOT carry severity-driven broadcast
        # tokens (those are P1-only — see test_p2_slack_no_everyone).
        assert any("hooks.slack.test" in u for u in urls)
        assert any("jira.test" in u for u in urls), urls
        assert not any("events.pagerduty.com" in u for u in urls), urls
        assert not any("sms.gateway.test" in u for u in urls), urls
        # ChatOps severity-driven leg fires exactly once.
        assert len(captured_chatops) == 1

    @pytest.mark.asyncio
    async def test_p2_fires_jira_and_chatops_even_when_level_info(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """A caller passing ``level="info"`` but ``severity="P2"`` must
        still reach the P2 fan-out legs — the severity ladder is
        additive (mirrors row 2936's P1-with-level-info contract).
        """
        from backend.notifications import _dispatch_external

        await _dispatch_external(_p2_notif(level="info"))
        await asyncio.sleep(0)

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        # Jira fires (severity tier override).
        assert any("jira.test" in u for u in urls), urls
        # PagerDuty / SMS / Slack do not — severity-additive, P2 doesn't
        # activate those legs (and level=info doesn't activate Slack
        # either via the legacy OR-merge gate).
        assert not any("events.pagerduty.com" in u for u in urls)
        assert not any("sms.gateway.test" in u for u in urls)
        assert not any("hooks.slack.test" in u for u in urls)
        # ChatOps still fires (severity-only gate, independent of level).
        assert len(captured_chatops) == 1

    @pytest.mark.asyncio
    async def test_p2_slack_no_everyone(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """When the Slack leg fires for a P2 (because level=action
        triggers the legacy gate), the message must NOT carry the
        ``@everyone`` Discord broadcast token — that is reserved for P1
        only. Negative regression guard against a refactor that pushes
        every severity into the broadcast text.
        """
        from backend.notifications import _send_slack

        await _send_slack(_p2_notif(level="action"))

        body = fake_subprocess[0].body
        text = body["text"]
        assert "@everyone" not in text
        assert "<!channel>" not in text


# ─────────────────────────────────────────────────────────────────
#  #2 — Jira: severity-P2 + blocked label, description, type/priority
# ─────────────────────────────────────────────────────────────────


class TestP2JiraPayload:
    @pytest.mark.asyncio
    async def test_p2_jira_carries_both_severity_and_blocked_labels(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_jira

        await _send_jira(_p2_notif())

        body = fake_subprocess[0].body
        labels = body["fields"]["labels"]
        assert "severity-P2" in labels
        # Row 2939 sub-bullet verbatim: "L3 Jira (severity: P2, label:
        # blocked)" — both must be present.
        assert "blocked" in labels

    @pytest.mark.asyncio
    async def test_p2_jira_description_prefixed_with_severity_tag(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_jira

        await _send_jira(_p2_notif())

        body = fake_subprocess[0].body
        assert body["fields"]["description"].startswith("[severity:P2] ")

    @pytest.mark.asyncio
    async def test_p2_jira_issuetype_task_priority_high(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """P1's ``Bug`` / ``Highest`` forcing is intentionally NOT
        extended to P2 — Jira boards already split "Bug" vs "Task"
        lanes; a stuck task should land in the Task lane (so engineering
        triage works it as a workflow blocker rather than a defect).
        """
        from backend.notifications import _send_jira

        await _send_jira(_p2_notif(level="action"))

        body = fake_subprocess[0].body
        assert body["fields"]["issuetype"]["name"] == "Task"
        assert body["fields"]["priority"]["name"] == "High"

    @pytest.mark.asyncio
    async def test_p2_jira_blocked_label_only_for_p2_not_p1(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Negative assertion — ``blocked`` label is P2-specific. P1
        carries ``severity-P1`` only (P1 events are full-system-down,
        not "blocked work"; the label semantics differ).
        """
        from backend.notifications import _send_jira

        p1 = Notification(
            id="notif-p1",
            level="critical",
            title="System down",
            message="oom-killer fired",
            source="watchdog",
            timestamp="2026-04-25T00:00:00",
            severity=Severity.P1,
        )
        await _send_jira(p1)

        body = fake_subprocess[0].body
        assert "severity-P1" in body["fields"]["labels"]
        assert "blocked" not in body["fields"]["labels"]

    @pytest.mark.asyncio
    async def test_legacy_jira_no_blocked_label(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        """Legacy notifications (no severity) get no ``labels`` key at
        all — locks against a refactor that always emits
        ``["blocked"]`` when notifications happen to be in a Jira-firing
        level branch.
        """
        from backend.notifications import _send_jira

        await _send_jira(_legacy_action_notif())

        body = fake_subprocess[0].body
        assert "labels" not in body["fields"]


# ─────────────────────────────────────────────────────────────────
#  #3 — ChatOps severity-driven dispatcher
# ─────────────────────────────────────────────────────────────────


class TestP2ChatOpsDispatch:
    @pytest.mark.asyncio
    async def test_p2_chatops_broadcasts_to_star_channel(
        self, captured_chatops,
    ) -> None:
        """Channel is ``"*"`` — broadcast to every configured adapter
        (Discord / Teams / Line). The bridge skips unconfigured ones
        gracefully so dev environments without every adapter wired up
        still receive the dispatch on whichever adapter IS set.
        """
        from backend.notifications import _dispatch_chatops_severity

        await _dispatch_chatops_severity(_p2_notif())

        assert len(captured_chatops) == 1
        assert captured_chatops[0]["channel"] == "*"

    @pytest.mark.asyncio
    async def test_p2_chatops_carries_severity_tag_in_title(
        self, captured_chatops,
    ) -> None:
        from backend.notifications import _dispatch_chatops_severity

        await _dispatch_chatops_severity(_p2_notif())

        assert "[severity:P2]" in captured_chatops[0]["title"]

    @pytest.mark.asyncio
    async def test_p2_chatops_default_buttons(
        self, captured_chatops,
    ) -> None:
        """Default 3-button set — Acknowledge / Inject Hint / View Logs.
        Locks the contract so a UI/UX change to the button labels
        doesn't silently break the on-call ergonomics.
        """
        from backend.notifications import _dispatch_chatops_severity

        await _dispatch_chatops_severity(_p2_notif())

        buttons = captured_chatops[0]["buttons"]
        button_ids = [b.id for b in buttons]
        assert button_ids == ["ack", "inject_hint", "view_logs"]

    @pytest.mark.asyncio
    async def test_p2_chatops_meta_carries_severity_and_notification_id(
        self, captured_chatops,
    ) -> None:
        """``meta`` dict is what the R1 button-click handler reads to
        route the action — must carry severity (so handler knows it's
        the severity-driven path, not a caller-specific R1 PEP wire) +
        notification_id (for audit-chain linking) + source.
        """
        from backend.notifications import _dispatch_chatops_severity

        await _dispatch_chatops_severity(_p2_notif())

        meta = captured_chatops[0]["meta"]
        assert meta["severity"] == "P2"
        assert meta["notification_id"] == "notif-p2-test"
        assert meta["source"] == "watchdog"

    @pytest.mark.asyncio
    async def test_p2_chatops_swallows_bridge_exceptions(
        self, monkeypatch,
    ) -> None:
        """Fire-and-forget contract — if the bridge raises, the
        severity dispatcher logs and returns; it MUST NOT propagate so
        a bridge hiccup can't take down the watchdog event loop that
        spawned it.
        """
        from backend import notifications as n
        from backend import chatops_bridge as bridge

        async def _boom(*a, **kw):
            raise RuntimeError("bridge down")

        monkeypatch.setattr(bridge, "send_interactive", _boom)

        # Must not raise.
        await n._dispatch_chatops_severity(_p2_notif())


# ─────────────────────────────────────────────────────────────────
#  #4 — ChatOps severity-driven leg is severity-only
# ─────────────────────────────────────────────────────────────────


class TestChatOpsSeverityOnlyActivation:
    @pytest.mark.asyncio
    async def test_legacy_action_does_not_fire_severity_chatops(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """Legacy ``notify(level="action")`` without severity must NOT
        spawn the severity-driven ChatOps card — that ladder is
        reserved for severity-tagged events. Legacy callers wanting
        ChatOps interactivity use the explicit ``interactive=True``
        path with their own buttons (R1 #307 PEP gateway, etc.).
        """
        from backend.notifications import _dispatch_external

        await _dispatch_external(_legacy_action_notif())
        await asyncio.sleep(0)

        # Legacy action still fires Slack + Jira (level-based).
        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("hooks.slack.test" in u for u in urls)
        assert any("jira.test" in u for u in urls)
        # But NOT the severity-driven ChatOps leg.
        assert len(captured_chatops) == 0

    @pytest.mark.asyncio
    async def test_legacy_critical_does_not_fire_severity_chatops(
        self, fake_subprocess, captured_chatops, configured_settings,
        stub_persistence,
    ) -> None:
        """Same negative assertion at the critical level — a level-only
        critical notification is PagerDuty's domain; ChatOps interactive
        only activates for severity-tagged events.
        """
        from backend.notifications import _dispatch_external

        legacy_critical = Notification(
            id="notif-legacy-crit",
            level="critical",
            title="Disk full",
            message="/var/log at 100%",
            source="metrics",
            timestamp="2026-04-25T00:00:00",
        )
        await _dispatch_external(legacy_critical)
        await asyncio.sleep(0)

        assert len(captured_chatops) == 0


# ─────────────────────────────────────────────────────────────────
#  #5 — Tier mapping coverage (drift guard)
# ─────────────────────────────────────────────────────────────────


class TestTierMappingP2Coverage:
    def test_p2_mapping_is_exactly_jira_and_chatops(self) -> None:
        """Drift guard against row 2935 spec erosion — locks row 2939's
        contract that P2 activates exactly Jira + ChatOps interactive.
        """
        from backend.severity import (
            L2_CHATOPS_INTERACTIVE,
            L3_JIRA,
            SEVERITY_TIER_MAPPING,
        )

        assert SEVERITY_TIER_MAPPING[Severity.P2] == frozenset({
            L3_JIRA, L2_CHATOPS_INTERACTIVE,
        })

    def test_p2_does_not_reach_pagerduty_sms_or_imwebhook(self) -> None:
        """Negative assertion — P2 spec excludes the broadcast tier
        (PagerDuty / SMS / IM-webhook-with-@everyone) so a "simplify
        the spec" refactor that merges P1 ⊃ P2 broadcast wouldn't slip
        past CI.
        """
        from backend.severity import (
            L2_IM_WEBHOOK,
            L4_PAGERDUTY,
            L4_SMS,
            SEVERITY_TIER_MAPPING,
        )

        p2 = SEVERITY_TIER_MAPPING[Severity.P2]
        assert L4_PAGERDUTY not in p2
        assert L4_SMS not in p2
        assert L2_IM_WEBHOOK not in p2

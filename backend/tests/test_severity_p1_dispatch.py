"""R9 row 2936 (#315) — P1 fan-out dispatcher contract tests.

Locks the row 2936 sub-bullet verbatim:

  P1 (系統崩潰) → L4 PagerDuty + L3 Jira (severity: P1) +
                   L2 Slack/Discord @everyone + SMS

Row 2935 already shipped the spec module + ``Notification.severity``
field + ``notify(severity=...)`` plumb-through; row 2939 will ship the
``send_notification(tier, severity, payload, interactive=False)``
single-tier-explicit dispatcher. THIS row owns the actual fan-out path
that *consumes* :data:`backend.severity.SEVERITY_TIER_MAPPING` so a
``severity="P1"`` notification fires PagerDuty + SMS + Jira (with
severity tag) + Slack/Discord (with @everyone) regardless of the
``level`` the caller passed in.

What we lock here:

  1. ``_dispatch_external(notif)`` with ``notif.severity = P1`` fires
     all four legs even when ``notif.level == "info"`` (the severity
     ladder is additive — a P1 caller shouldn't be silently dropped
     because someone passed level="info").
  2. ``_send_slack`` payload for P1 contains both Slack ``<!channel>``
     and Discord ``@everyone`` so a single message broadcasts on
     either webhook flavour.
  3. ``_send_jira`` for P1 attaches ``severity-P1`` label, prefixes
     description with ``[severity:P1]``, and forces priority=Highest
     and issuetype=Bug.
  4. ``_send_pagerduty`` for P1 attaches ``custom_details.omnisight_
     severity = "P1"`` to the Events API v2 payload.
  5. ``_send_sms`` (NEW) POSTs the truncated body + severity to the
     ``OMNISIGHT_NOTIFICATION_SMS_WEBHOOK`` URL with the destination
     phone number(s) from ``OMNISIGHT_NOTIFICATION_SMS_TO``.
  6. SMS is *only* activated by the severity tag — a level-only caller
     (e.g. ``level="critical"`` without ``severity``) does NOT fire
     SMS (intentional: SMS is reserved for P1 broadcast, not for
     routine criticals which PagerDuty already pages on).
  7. Legacy callers without severity see the OLD level-based routing
     unchanged — no regressions for the 40+ existing
     ``notify(level=...)`` call-sites.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from backend.models import Notification, Severity


@dataclass
class _CapturedCurl:
    """Snapshot of one ``asyncio.create_subprocess_exec`` call so a
    test can assert on the URL, headers, and JSON body that would have
    been POSTed to the external channel.
    """
    cmd: tuple[str, ...]
    body: dict | None  # parsed JSON from the ``-d`` arg, if any


class _FakeProc:
    """Minimal asyncio.subprocess.Process stand-in — communicate() is a
    no-op coroutine and returncode is configurable per channel.
    """
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.fixture()
def fake_subprocess(monkeypatch):
    """Replace ``asyncio.create_subprocess_exec`` with a capture so the
    dispatchers run their full code path without firing real curl.
    """
    captured: list[_CapturedCurl] = []

    async def _fake_exec(*args, **kwargs):
        # ``args`` looks like ("curl", "-s", "-X", "POST", url, ...,
        # "-d", json_body, ...).  Pull out the JSON body if present so
        # tests can assert structure without re-parsing.
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
    """All four channels configured so the dispatcher takes the live
    code path; tests still run offline because ``fake_subprocess``
    intercepts the curl call.
    """
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
    """Skip the DB writes so unit tests don't need PG; ``_dispatch_
    external`` still runs the dispatch-status update branch but writes
    a no-op.
    """
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


def _p1_notif(level: str = "critical") -> Notification:
    return Notification(
        id="notif-p1-test",
        level=level,
        title="System down",
        message="oom-killer fired on backend-a",
        source="watchdog",
        timestamp="2026-04-25T00:00:00",
        severity=Severity.P1,
    )


def _legacy_notif(level: str = "critical") -> Notification:
    return Notification(
        id="notif-legacy",
        level=level,
        title="High latency",
        message="p99 > 2s",
        source="metrics",
        timestamp="2026-04-25T00:00:00",
        # severity intentionally omitted (None)
    )


# ─────────────────────────────────────────────────────────────────
#  #1 — _dispatch_external fires all four legs for P1
# ─────────────────────────────────────────────────────────────────


class TestP1FanOutAllFourLegs:
    @pytest.mark.asyncio
    async def test_p1_fires_pagerduty_sms_jira_slack(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _dispatch_external

        await _dispatch_external(_p1_notif(level="critical"))

        # All four legs must have been hit. We identify each by its
        # webhook URL fragment.
        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("hooks.slack.test" in u for u in urls), urls
        assert any("jira.test" in u for u in urls), urls
        assert any("events.pagerduty.com" in u for u in urls), urls
        assert any("sms.gateway.test" in u for u in urls), urls
        assert len(fake_subprocess) == 4

    @pytest.mark.asyncio
    async def test_p1_fires_all_four_even_when_level_is_info(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        # The severity ladder is additive — even a caller who somehow
        # passed level="info" but severity=P1 must reach all four
        # broadcast legs.  Locks against a regression where a refactor
        # short-circuits on level alone and silently drops P1.
        from backend.notifications import _dispatch_external

        await _dispatch_external(_p1_notif(level="info"))

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("hooks.slack.test" in u for u in urls)
        assert any("jira.test" in u for u in urls)
        assert any("events.pagerduty.com" in u for u in urls)
        assert any("sms.gateway.test" in u for u in urls)


# ─────────────────────────────────────────────────────────────────
#  #2 — Slack/Discord broadcast: <!channel> AND @everyone
# ─────────────────────────────────────────────────────────────────


class TestP1SlackBroadcast:
    @pytest.mark.asyncio
    async def test_slack_payload_contains_both_broadcast_syntaxes(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_slack

        await _send_slack(_p1_notif())

        assert len(fake_subprocess) == 1
        body = fake_subprocess[0].body
        assert body is not None
        text = body["text"]
        # Slack broadcast token — Slack renders, Discord shows as text.
        assert "<!channel>" in text
        # Discord broadcast token — Discord renders, Slack shows as text.
        assert "@everyone" in text

    @pytest.mark.asyncio
    async def test_slack_payload_contains_severity_tag(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_slack

        await _send_slack(_p1_notif())

        body = fake_subprocess[0].body
        assert "[severity:P1]" in body["text"]

    @pytest.mark.asyncio
    async def test_legacy_critical_only_uses_slack_channel_no_everyone(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        # Negative assertion: a legacy critical notification (no
        # severity tag) keeps the OLD `<!channel>` mention — adding
        # `@everyone` to every critical event would over-broadcast on
        # Discord webhooks for routine alarms.
        from backend.notifications import _send_slack

        await _send_slack(_legacy_notif(level="critical"))

        body = fake_subprocess[0].body
        assert "<!channel>" in body["text"]
        assert "@everyone" not in body["text"]
        assert "[severity:" not in body["text"]


# ─────────────────────────────────────────────────────────────────
#  #3 — Jira: severity label + description + Bug + Highest
# ─────────────────────────────────────────────────────────────────


class TestP1JiraPayload:
    @pytest.mark.asyncio
    async def test_jira_payload_carries_severity_label(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_jira

        await _send_jira(_p1_notif())

        body = fake_subprocess[0].body
        assert "severity-P1" in body["fields"]["labels"]

    @pytest.mark.asyncio
    async def test_jira_description_prefixed_with_severity_tag(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_jira

        await _send_jira(_p1_notif())

        body = fake_subprocess[0].body
        assert body["fields"]["description"].startswith("[severity:P1] ")

    @pytest.mark.asyncio
    async def test_jira_priority_and_issuetype_for_p1(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_jira

        await _send_jira(_p1_notif(level="info"))

        body = fake_subprocess[0].body
        # P1 forces Bug + Highest regardless of level.
        assert body["fields"]["issuetype"]["name"] == "Bug"
        assert body["fields"]["priority"]["name"] == "Highest"

    @pytest.mark.asyncio
    async def test_jira_legacy_payload_no_severity_label(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        # Negative assertion: legacy notification → no `labels` key, no
        # `[severity:...]` description prefix.  Locks against a refactor
        # that always emits `severity-None`.
        from backend.notifications import _send_jira

        await _send_jira(_legacy_notif(level="action"))

        body = fake_subprocess[0].body
        assert "labels" not in body["fields"]
        assert not body["fields"]["description"].startswith("[severity:")


# ─────────────────────────────────────────────────────────────────
#  #4 — PagerDuty: custom_details carries severity tag
# ─────────────────────────────────────────────────────────────────


class TestP1PagerDutyPayload:
    @pytest.mark.asyncio
    async def test_pagerduty_custom_details_has_severity(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_pagerduty

        await _send_pagerduty(_p1_notif())

        body = fake_subprocess[0].body
        assert body["payload"]["custom_details"]["omnisight_severity"] == "P1"
        # Events API v2 severity is left at "critical" — that's the only
        # PagerDuty severity P1 maps to.
        assert body["payload"]["severity"] == "critical"

    @pytest.mark.asyncio
    async def test_pagerduty_summary_prefixed_with_p1(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_pagerduty

        await _send_pagerduty(_p1_notif())

        body = fake_subprocess[0].body
        assert body["payload"]["summary"].startswith("[P1]")

    @pytest.mark.asyncio
    async def test_pagerduty_legacy_payload_unchanged(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_pagerduty

        await _send_pagerduty(_legacy_notif(level="critical"))

        body = fake_subprocess[0].body
        # Legacy summary keeps the [CRITICAL] prefix; no custom_details.
        assert body["payload"]["summary"].startswith("[CRITICAL]")
        assert "custom_details" not in body["payload"]


# ─────────────────────────────────────────────────────────────────
#  #5 — SMS dispatcher (new)
# ─────────────────────────────────────────────────────────────────


class TestP1SmsDispatcher:
    @pytest.mark.asyncio
    async def test_sms_posts_to_configured_webhook(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_sms

        await _send_sms(_p1_notif())

        assert len(fake_subprocess) == 1
        # URL is the 5th element of the curl arg vector.
        assert fake_subprocess[0].cmd[4] == "https://sms.gateway.test/send"

    @pytest.mark.asyncio
    async def test_sms_payload_carries_severity_to_and_message(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_sms

        await _send_sms(_p1_notif())

        body = fake_subprocess[0].body
        assert body["severity"] == "P1"
        assert body["to"] == "+15551234567"
        assert body["message"].startswith("[P1]")
        assert "System down" in body["message"]
        # notification_id round-trips so gateway dedup can correlate.
        assert body["notification_id"] == "notif-p1-test"

    @pytest.mark.asyncio
    async def test_sms_truncates_long_message_to_160_chars(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        from backend.notifications import _send_sms

        long_notif = Notification(
            id="notif-long",
            level="critical",
            title="A" * 80,
            message="B" * 200,
            source="watchdog",
            timestamp="2026-04-25T00:00:00",
            severity=Severity.P1,
        )
        await _send_sms(long_notif)

        body = fake_subprocess[0].body
        assert len(body["message"]) <= 160
        # Truncation marker present when over limit.
        assert body["message"].endswith("...")

    @pytest.mark.asyncio
    async def test_sms_failure_raises_for_retry_loop(
        self, configured_settings, stub_persistence, monkeypatch,
    ) -> None:
        # When curl exits non-zero the dispatcher must raise so the
        # outer `_send_with_retry` loop counts the failure.
        from backend import notifications as n

        async def _fake_fail(*a, **kw):
            return _FakeProc(returncode=7)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_fail)

        with pytest.raises(RuntimeError, match="SMS gateway failed"):
            await n._send_sms(_p1_notif())


# ─────────────────────────────────────────────────────────────────
#  #6 — SMS leg is severity-only (no level-only activation)
# ─────────────────────────────────────────────────────────────────


class TestSmsSeverityOnlyActivation:
    @pytest.mark.asyncio
    async def test_legacy_critical_does_not_fire_sms(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        # A level=critical notification WITHOUT severity tag must NOT
        # reach SMS — that ladder is reserved for the P1 broadcast
        # tier; routine criticals are PagerDuty-only by design.
        from backend.notifications import _dispatch_external

        await _dispatch_external(_legacy_notif(level="critical"))

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert any("hooks.slack.test" in u for u in urls)
        assert any("jira.test" in u for u in urls)
        assert any("events.pagerduty.com" in u for u in urls)
        assert not any("sms.gateway.test" in u for u in urls)

    @pytest.mark.asyncio
    async def test_legacy_warning_only_fires_slack(
        self, fake_subprocess, configured_settings, stub_persistence,
    ) -> None:
        # Sanity: legacy level=warning still hits Slack only — the
        # severity-aware dispatcher must be additive, not replacing.
        from backend.notifications import _dispatch_external

        await _dispatch_external(_legacy_notif(level="warning"))

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert urls == ["https://hooks.slack.test/T0/B0/X"]


# ─────────────────────────────────────────────────────────────────
#  #7 — Tier mapping & SMS leg disabled when webhook empty
# ─────────────────────────────────────────────────────────────────


class TestTierMappingP1Coverage:
    def test_p1_mapping_includes_all_four_tier_identifiers(self) -> None:
        # Locks the row 2935 spec in row 2936's contract: the four tiers
        # the dispatcher reads are exactly what row 2936's prose
        # demands.
        from backend.severity import (
            L2_IM_WEBHOOK,
            L3_JIRA,
            L4_PAGERDUTY,
            L4_SMS,
            SEVERITY_TIER_MAPPING,
        )

        assert SEVERITY_TIER_MAPPING[Severity.P1] == frozenset({
            L4_PAGERDUTY, L4_SMS, L3_JIRA, L2_IM_WEBHOOK,
        })

    @pytest.mark.asyncio
    async def test_sms_skipped_when_webhook_empty(
        self, fake_subprocess, configured_settings, stub_persistence,
        monkeypatch,
    ) -> None:
        # Operator with no SMS gateway configured still gets the rest
        # of the P1 broadcast (PagerDuty + Slack + Jira) — the SMS leg
        # gracefully skips.
        from backend.notifications import _dispatch_external

        monkeypatch.setattr(
            configured_settings, "notification_sms_webhook", "",
        )
        await _dispatch_external(_p1_notif())

        urls = [c.cmd[4] for c in fake_subprocess if len(c.cmd) > 4]
        assert not any("sms.gateway.test" in u for u in urls)
        assert any("events.pagerduty.com" in u for u in urls)
        assert any("hooks.slack.test" in u for u in urls)
        assert any("jira.test" in u for u in urls)

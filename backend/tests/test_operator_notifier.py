"""Integration tests for backend.agents.operator_notifier (OP-722).

Each AC from the ticket has at least one named test below — see the
:func:`_ac` markers — so the AC ↔ test mapping for the JIRA verification
comment is mechanical to read off.

The tests use a fake clock and fake channel implementations so we
exercise the dispatcher's real code paths (dedup window, ack loop,
fanout, error tolerance) without external SMTP / Slack / LINE / JIRA
side-effects."""
from __future__ import annotations

from email.message import EmailMessage

import pytest

from backend.agents.operator_notifier import (
    EmailChannel,
    JiraChannel,
    LineNotifyChannel,
    Notification,
    Notifier,
    NotifierConfig,
    Severity,
    SlackChannel,
    SmtpConfig,
    build_channels_from_env,
    canary_self_test,
    channels_for,
    format_text,
)


# ── Test doubles ──────────────────────────────────────────────────


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class RecordingChannel:
    """Captures payloads + can be configured to raise on send."""

    def __init__(self, name: str, *, raises: Exception | None = None) -> None:
        self.name = name
        self.calls: list[Notification] = []
        self._raises = raises

    def send(self, payload: Notification) -> None:
        self.calls.append(payload)
        if self._raises is not None:
            raise self._raises


def _make_notifier(
    *,
    dedup: float = 60.0,
    p0_repage: float = 300.0,
    critical_repage: float = 600.0,
    ack_base: str | None = "https://ops.example.com/ack",
    default_ticket: str | None = "OP-722",
    channels: dict | None = None,
    clock: FakeClock | None = None,
) -> tuple[Notifier, FakeClock, dict[str, RecordingChannel]]:
    clk = clock or FakeClock(1_000_000.0)
    chans = channels or {
        "jira": RecordingChannel("jira"),
        "email": RecordingChannel("email"),
        "slack": RecordingChannel("slack"),
        "line": RecordingChannel("line"),
    }
    cfg = NotifierConfig(
        dedup_window_seconds=dedup,
        p0_repage_seconds=p0_repage,
        critical_repage_seconds=critical_repage,
        ack_base_url=ack_base,
        default_ticket=default_ticket,
    )
    return Notifier(chans, cfg, clock=clk.now), clk, chans  # type: ignore[arg-type]


# ── AC1: WARN → JIRA only ─────────────────────────────────────────


def test_ac1_warn_routes_to_jira_only():
    """AC: notify(WARN, ...) → JIRA comment created on the relevant ticket."""
    n, _, ch = _make_notifier()
    n.notify(Severity.WARN, "warn-x", "low-grade event", ticket="OP-722")
    n.flush_all()

    assert len(ch["jira"].calls) == 1
    assert ch["jira"].calls[0].ticket == "OP-722"
    assert ch["jira"].calls[0].severity == Severity.WARN
    assert ch["email"].calls == []
    assert ch["slack"].calls == []
    assert ch["line"].calls == []


# ── AC2: DEGRADED → JIRA + email ──────────────────────────────────


def test_ac2_degraded_routes_to_jira_and_email():
    """AC: notify(DEGRADED, ...) → JIRA comment + email sent."""
    n, _, ch = _make_notifier()
    n.notify(Severity.DEGRADED, "degraded-x", "service slow", ticket="OP-722")
    n.flush_all()

    assert len(ch["jira"].calls) == 1
    assert len(ch["email"].calls) == 1
    assert ch["slack"].calls == []
    assert ch["line"].calls == []


# ── AC3: CRITICAL → JIRA + email + Slack/LINE ─────────────────────


def test_ac3_critical_routes_to_all_chat_channels():
    """AC: notify(CRITICAL, ...) → JIRA + email + Slack/LINE message."""
    n, _, ch = _make_notifier()
    n.notify(Severity.CRITICAL, "crit-x", "everything is on fire", ticket="OP-722")
    n.flush_all()

    assert len(ch["jira"].calls) == 1
    assert len(ch["email"].calls) == 1
    assert len(ch["slack"].calls) == 1
    assert len(ch["line"].calls) == 1
    assert ch["slack"].calls[0].ack_url is not None  # CRITICAL carries ack URL
    assert ch["slack"].calls[0].ack_url.startswith("https://ops.example.com/ack/")


# ── AC4: dedup — 5 calls within 1min collapse to 1 outbound count=5 ─


def test_ac4_five_calls_within_window_collapse_to_one_outbound_count_five():
    """AC: 5 identical notify() calls within 1 minute → 1 outbound notification with count=5."""
    n, clk, ch = _make_notifier(dedup=60.0)
    for i in range(5):
        n.notify(Severity.DEGRADED, "burst-y", f"call #{i}", ticket="OP-722")
        clk.advance(5)  # 5 calls × 5s = 25s elapsed (still inside the 60s window)

    # Window not yet expired — flush_expired must be a no-op.
    assert n.flush_expired() == []
    assert ch["jira"].calls == []

    # Advance past window edge; flush_expired must dispatch ONE outbound,
    # carrying count=5 across every fanout channel (jira + email).
    clk.advance(61)
    dispatched = n.flush_expired()
    assert len(dispatched) == 1
    assert dispatched[0].count == 5
    assert dispatched[0].code == "burst-y"
    assert len(ch["jira"].calls) == 1
    assert ch["jira"].calls[0].count == 5
    assert len(ch["email"].calls) == 1
    assert ch["email"].calls[0].count == 5


def test_dedup_distinct_codes_do_not_collapse():
    """Same severity, different code → two distinct outbound."""
    n, _, ch = _make_notifier()
    n.notify(Severity.DEGRADED, "code-A", "msg")
    n.notify(Severity.DEGRADED, "code-B", "msg")
    n.flush_all()
    codes = sorted(c.code for c in ch["jira"].calls)
    assert codes == ["code-A", "code-B"]


def test_dedup_window_rolls_over_emits_separate_bursts():
    """A new burst after the window must produce a SECOND outbound,
    not blend into the prior one."""
    n, clk, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.DEGRADED, "rollover", "first")
    clk.advance(120)  # past window
    n.notify(Severity.DEGRADED, "rollover", "second")
    # The second call observes the stale entry, flushes it inline,
    # then opens a fresh burst.
    assert len(ch["jira"].calls) == 1
    assert ch["jira"].calls[0].count == 1
    n.flush_all()
    assert len(ch["jira"].calls) == 2
    assert ch["jira"].calls[1].count == 1


# ── AC5: CRITICAL not acked in 10min → re-paged ───────────────────


def test_ac5_critical_repaged_when_unacked_after_interval():
    """AC: CRITICAL not acked in 10 min → re-paged."""
    # critical_repage=600s in the AC; use a short dedup so the first
    # dispatch happens deterministically without waiting.
    n, clk, ch = _make_notifier(dedup=1.0, critical_repage=600.0)
    n.notify(Severity.CRITICAL, "page-me", "wake up")
    clk.advance(2)
    n.flush_expired()  # first dispatch
    assert len(ch["slack"].calls) == 1
    first_id = ch["slack"].calls[0].notification_id
    assert n.pending_acks(), "CRITICAL should be tracked for re-page until acked"

    # Less than the repage interval — tick must not fire.
    clk.advance(599)
    assert n.tick_ack_loop() == []
    assert len(ch["slack"].calls) == 1

    # Cross the 10-minute threshold — tick re-pages on every channel.
    clk.advance(2)
    repaged = n.tick_ack_loop()
    assert len(repaged) == 1
    assert repaged[0].notification_id == first_id
    assert len(ch["slack"].calls) == 2
    assert len(ch["email"].calls) == 2
    assert len(ch["jira"].calls) == 2
    assert len(ch["line"].calls) == 2


def test_ack_clears_pending_repage():
    n, clk, _ = _make_notifier(dedup=1.0, critical_repage=60.0)
    n.notify(Severity.CRITICAL, "ack-me", "msg")
    clk.advance(2)
    n.flush_expired()
    nid = n.pending_acks()[0].notification_id

    assert n.acknowledge(nid) is True
    assert n.pending_acks() == []

    clk.advance(120)  # past would-be repage window
    assert n.tick_ack_loop() == []
    # Acknowledging an unknown id is False (idempotent for already-acked).
    assert n.acknowledge("deadbeefdead") is False
    assert n.acknowledge(nid) is False


def test_p0_uses_p0_repage_interval_not_critical():
    """P0 has its own (shorter by default) re-page interval."""
    n, clk, ch = _make_notifier(dedup=1.0, p0_repage=120.0, critical_repage=600.0)
    n.notify(Severity.P0, "p0-x", "wake up faster")
    clk.advance(2)
    n.flush_expired()
    assert len(ch["slack"].calls) == 1
    clk.advance(121)
    repaged = n.tick_ack_loop()
    assert len(repaged) == 1


# ── AC6: integration — all 4 channels covered with mocks ──────────


def test_ac6_jira_channel_calls_add_comment_with_text():
    """AC6 evidence for JIRA channel: integration test that wires the
    real ``JiraChannel`` against an injected client/comment_fn double
    (no network)."""
    captured: list[tuple[str, str]] = []

    def fake_client_factory(agent_class: str):
        return {"agent_class": agent_class}

    def fake_comment(client, key, body):
        captured.append((key, body))

    chan = JiraChannel(
        agent_class="subscription-claude",
        default_ticket="OP-722",
        client_factory=fake_client_factory,
        comment_fn=fake_comment,
    )
    chan.send(Notification(
        notification_id="abc123",
        severity=Severity.WARN,
        code="jira-test",
        message="hello",
        ticket="OP-722",
    ))
    assert len(captured) == 1
    assert captured[0][0] == "OP-722"
    assert "jira-test" in captured[0][1]
    assert "WARN" in captured[0][1]


def test_ac6_email_channel_invokes_smtp_send_fn():
    """AC6 evidence for email channel: integration test that wires
    the real ``EmailChannel`` and intercepts the SMTP transport."""
    captured: list[EmailMessage] = []

    def fake_smtp_send(_smtp, msg):
        captured.append(msg)

    chan = EmailChannel(
        smtp=SmtpConfig(host="smtp.example.com", port=587, username="u", password="p", sender="ops@example.com"),
        recipients=["a@example.com", "b@example.com"],
        send_fn=fake_smtp_send,
    )
    chan.send(Notification(
        notification_id="abc123",
        severity=Severity.DEGRADED,
        code="email-test",
        message="check inbox",
    ))
    assert len(captured) == 1
    assert captured[0]["From"] == "ops@example.com"
    assert captured[0]["To"] == "a@example.com, b@example.com"
    assert captured[0]["Subject"] == "[DEGRADED] email-test"
    assert "check inbox" in captured[0].get_content()


def test_ac6_slack_channel_posts_json_payload():
    """AC6 evidence for Slack channel: integration test that wires the
    real ``SlackChannel`` against an injected post_fn (mocking the
    incoming-webhook HTTP call)."""

    captured: list[tuple[str, bytes, str, dict | None, int]] = []

    def fake_post(url, body, content_type, headers, timeout):
        captured.append((url, body, content_type, headers, timeout))
        return 200, "ok"

    chan = SlackChannel("https://hooks.slack.com/services/T/B/X", post_fn=fake_post)
    chan.send(Notification(
        notification_id="abc123",
        severity=Severity.CRITICAL,
        code="slack-test",
        message="!!! red alert",
    ))
    assert len(captured) == 1
    url, body, content_type, headers, _ = captured[0]
    assert url == "https://hooks.slack.com/services/T/B/X"
    assert content_type == "application/json"
    import json as _json
    payload = _json.loads(body)
    assert "red alert" in payload["text"]
    assert "CRITICAL" in payload["text"]


def test_ac6_slack_channel_raises_on_http_error():
    def fake_post(*_a, **_kw):
        return 500, "internal error"

    chan = SlackChannel("https://hooks.slack.com/services/T/B/X", post_fn=fake_post)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        chan.send(Notification(
            notification_id="x",
            severity=Severity.CRITICAL,
            code="c",
            message="m",
        ))


def test_ac6_line_channel_posts_form_encoded_with_bearer():
    """AC6 evidence for LINE channel: integration test that wires the
    real ``LineNotifyChannel`` against an injected post_fn (mocking the
    LINE Notify HTTP call)."""

    captured: list[tuple[str, bytes, str, dict | None, int]] = []

    def fake_post(url, body, content_type, headers, timeout):
        captured.append((url, body, content_type, headers, timeout))
        return 200, "ok"

    chan = LineNotifyChannel(token="token-abc", post_fn=fake_post)
    chan.send(Notification(
        notification_id="abc123",
        severity=Severity.CRITICAL,
        code="line-test",
        message="page operator",
    ))
    assert len(captured) == 1
    url, body, content_type, headers, _ = captured[0]
    assert url == LineNotifyChannel.DEFAULT_ENDPOINT
    assert content_type == "application/x-www-form-urlencoded"
    assert headers == {"Authorization": "Bearer token-abc"}
    # form-encoded message= field with text body
    assert b"message=" in body
    assert b"page+operator" in body or b"page%20operator" in body


# ── Cross-cutting tests ───────────────────────────────────────────


def test_partial_channel_failure_does_not_silence_other_channels():
    """Slack down must not block JIRA + email + LINE delivery."""
    chans = {
        "jira": RecordingChannel("jira"),
        "email": RecordingChannel("email"),
        "slack": RecordingChannel("slack", raises=RuntimeError("slack 503")),
        "line": RecordingChannel("line"),
    }
    n, _, _ = _make_notifier(channels=chans, dedup=1.0)
    n.notify(Severity.CRITICAL, "partial-fail", "msg")
    # Force flush so we see the dispatch.
    n.flush_all()
    assert len(chans["jira"].calls) == 1
    assert len(chans["email"].calls) == 1
    assert len(chans["slack"].calls) == 1  # send was attempted
    assert len(chans["line"].calls) == 1
    # The error is recorded on the payload context for observability.
    assert any("slack" in v for v in chans["jira"].calls[0].context.values() if isinstance(v, str))


def test_unconfigured_channel_is_skipped_not_errored():
    """If only JIRA is wired, a CRITICAL still fans out to JIRA."""
    chans = {"jira": RecordingChannel("jira")}
    n, _, _ = _make_notifier(channels=chans, dedup=1.0)
    n.notify(Severity.CRITICAL, "skip-test", "msg")
    n.flush_all()
    assert len(chans["jira"].calls) == 1


def test_format_text_includes_count_and_ack_url():
    payload = Notification(
        notification_id="nid",
        severity=Severity.CRITICAL,
        code="c",
        message="m",
        context={"k": "v"},
        count=5,
        ack_url="https://ops/ack/nid",
    )
    body = format_text(payload)
    assert "[CRITICAL] c: m" in body
    assert "count=5" in body
    assert "Ack: https://ops/ack/nid" in body
    assert "k: v" in body
    assert "id=nid" in body


def test_channels_for_routing_table_is_complete():
    assert channels_for(Severity.WARN) == frozenset({"jira"})
    assert channels_for(Severity.DEGRADED) == frozenset({"jira", "email"})
    assert channels_for(Severity.CRITICAL) == frozenset({"jira", "email", "slack", "line"})
    assert channels_for(Severity.P0) == frozenset({"jira", "email", "slack", "line"})


def test_severity_string_aliases_are_accepted():
    """notify() must accept either Severity enum or its string value."""
    n, _, ch = _make_notifier()
    n.notify("WARN", "string-sev", "ok", ticket="OP-722")
    n.flush_all()
    assert len(ch["jira"].calls) == 1


def test_canary_self_test_pings_every_wired_channel_directly():
    """canary must bypass dedup and call send() on every channel exactly once."""
    chans = {
        "jira": RecordingChannel("jira"),
        "email": RecordingChannel("email"),
        "slack": RecordingChannel("slack"),
        "line": RecordingChannel("line"),
    }
    n, _, _ = _make_notifier(channels=chans)
    results = canary_self_test(n)
    assert results == {"jira": "ok", "email": "ok", "slack": "ok", "line": "ok"}
    for ch in chans.values():
        assert len(ch.calls) == 1


def test_canary_self_test_records_per_channel_failure():
    """A failing channel must not abort the canary for the others."""
    chans = {
        "jira": RecordingChannel("jira"),
        "slack": RecordingChannel("slack", raises=RuntimeError("503 boom")),
    }
    n, _, _ = _make_notifier(channels=chans)
    results = canary_self_test(n)
    assert results["jira"] == "ok"
    assert "RuntimeError" in results["slack"]
    assert "503 boom" in results["slack"]


def test_build_channels_from_env_omits_missing_config():
    env = {
        # No SMTP, no Slack, no LINE, no recipient list — only JIRA.
        "OMNISIGHT_NOTIFIER_AGENT_CLASS": "subscription-claude",
        "OMNISIGHT_NOTIFIER_JIRA_TICKET": "OP-722",
    }
    channels = build_channels_from_env(env)
    assert "jira" in channels
    assert "email" not in channels
    assert "slack" not in channels
    assert "line" not in channels


def test_build_channels_from_env_wires_email_with_host_port_form():
    env = {
        "OMNISIGHT_NOTIFIER_AGENT_CLASS": "subscription-claude",
        "OMNISIGHT_NOTIFIER_SMTP_HOST": "smtp.example.com:2525",
        "OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS": "a@b.com, c@d.com",
        "OMNISIGHT_NOTIFIER_SMTP_FROM": "ops@example.com",
        "OMNISIGHT_NOTIFIER_SMTP_USER": "u",
        "OMNISIGHT_NOTIFIER_SMTP_PASSWORD": "p",
    }
    channels = build_channels_from_env(env)
    assert "email" in channels
    email_chan = channels["email"]
    assert isinstance(email_chan, EmailChannel)
    assert email_chan.smtp.host == "smtp.example.com"
    assert email_chan.smtp.port == 2525
    assert email_chan.recipients == ["a@b.com", "c@d.com"]


def test_immediate_repeat_does_not_dispatch_until_window_expires():
    """Sanity check on the coalescing contract: nothing is sent until
    flush_expired runs after the dedup window. This prevents accidental
    'fire on first' regressions in future refactors."""
    n, clk, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.DEGRADED, "no-fire", "msg")
    assert ch["jira"].calls == []  # no immediate dispatch
    clk.advance(30)
    n.flush_expired()  # mid-window → no-op
    assert ch["jira"].calls == []
    clk.advance(31)
    n.flush_expired()  # past window → dispatch
    assert len(ch["jira"].calls) == 1

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
    RateLimitConfig,
    RateLimiter,
    Severity,
    SlackChannel,
    SmtpConfig,
    build_channels_from_env,
    build_rate_limit_config_from_env,
    canary_self_test,
    channels_for,
    default_rate_limit_config,
    format_text,
    is_high_severity,
    severity_tier,
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
    rate_limit_config: RateLimitConfig | None = None,
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
    # Default to "no rate-limit" for OP-722 tests so the existing AC
    # tests (which dispatch many DEGRADED bursts) remain insensitive
    # to OP-755 enforcement. OP-755 tests construct their own config.
    rl_cfg = rate_limit_config if rate_limit_config is not None else RateLimitConfig()
    return (
        Notifier(chans, cfg, clock=clk.now, rate_limit_config=rl_cfg),  # type: ignore[arg-type]
        clk,
        chans,
    )


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


# ── OP-755: grouping by (severity, scope, root_cause_key) ─────────


def test_op755_ac1_grouping_default_window_is_300s():
    """AC: grouping window default 5 min (300s) — matches OP-722
    dedup_window_seconds default."""
    cfg = NotifierConfig()
    assert cfg.dedup_window_seconds == 300.0


def test_op755_ac2_distinct_codes_collapse_under_same_root_cause_key():
    """AC: alerts within window for same (severity, scope, root_cause_key)
    collapse into 1 aggregated alert.

    Distinct ``code`` values that all share the same
    ``root_cause_key`` should fold into one outbound — this is the
    feature that OP-755 adds on top of OP-722's strict (code, severity)
    grouping."""
    n, clk, ch = _make_notifier(dedup=300.0)
    for i in range(5):
        n.notify(
            Severity.DEGRADED,
            f"ingest-error-{i}",  # different code each call
            f"event #{i}",
            scope="runner-pipeline",
            root_cause_key="ingest-cluster-down",
        )
        clk.advance(10)

    # Within window → no dispatch yet.
    assert ch["jira"].calls == []
    # Past window → exactly one outbound, count=5.
    clk.advance(301)
    n.flush_expired()
    assert len(ch["jira"].calls) == 1
    payload = ch["jira"].calls[0]
    assert payload.count == 5
    assert payload.scope == "runner-pipeline"
    assert payload.root_cause_key == "ingest-cluster-down"


def test_op755_distinct_scope_does_not_collapse():
    """Same severity and root_cause_key but different scope → two
    distinct outbound. Scope is part of the grouping key."""
    n, _, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.DEGRADED, "x", "m", scope="runner-pipeline", root_cause_key="rk")
    n.notify(Severity.DEGRADED, "x", "m", scope="ingest-pipeline", root_cause_key="rk")
    n.flush_all()
    scopes = sorted(c.scope for c in ch["jira"].calls)
    assert scopes == ["ingest-pipeline", "runner-pipeline"]


def test_op755_distinct_root_cause_key_does_not_collapse():
    """Same severity and scope but different root_cause_key → two
    distinct outbound. Root cause key is part of the grouping key."""
    n, _, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.DEGRADED, "x", "m", scope="s", root_cause_key="rk-A")
    n.notify(Severity.DEGRADED, "x", "m", scope="s", root_cause_key="rk-B")
    n.flush_all()
    rcks = sorted(c.root_cause_key for c in ch["jira"].calls)
    assert rcks == ["rk-A", "rk-B"]


def test_op755_distinct_severity_does_not_collapse():
    """Severity remains part of the grouping key — a WARN and a
    DEGRADED with identical scope+root_cause_key still split."""
    n, _, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.WARN, "x", "m", scope="s", root_cause_key="rk")
    n.notify(Severity.DEGRADED, "x", "m", scope="s", root_cause_key="rk")
    n.flush_all()
    severities = sorted(c.severity.value for c in ch["jira"].calls)
    assert severities == ["DEGRADED", "WARN"]


def test_op755_legacy_callers_without_scope_keep_op722_behavior():
    """OP-722 callers that pass no scope/root_cause_key get the same
    (code, severity) grouping as before — proves backward compat."""
    n, _, ch = _make_notifier(dedup=60.0)
    n.notify(Severity.DEGRADED, "code-A", "m1")
    n.notify(Severity.DEGRADED, "code-A", "m2")
    n.notify(Severity.DEGRADED, "code-B", "m3")
    n.flush_all()
    codes = sorted(c.code for c in ch["jira"].calls)
    assert codes == ["code-A", "code-B"]
    a = next(c for c in ch["jira"].calls if c.code == "code-A")
    assert a.count == 2  # two code-A calls collapsed


# ── OP-755 AC3: aggregated alert format ───────────────────────────


def test_op755_ac3_aggregated_format_header_on_count_gt_one():
    """AC: aggregated body = `[N events in M min] severity=<tier>
    scope=<scope> sample-event=<code>`."""
    n, clk, ch = _make_notifier(dedup=300.0)
    for _ in range(3):
        n.notify(
            Severity.CRITICAL,
            "rk-event",
            "the system is on fire",
            scope="runner-pipeline",
            root_cause_key="rk",
        )
        clk.advance(5)
    clk.advance(301)
    n.flush_expired()

    body = format_text(ch["slack"].calls[0])
    # Header line is the first line.
    first_line = body.splitlines()[0]
    assert "[3 events in 5 min]" in first_line
    assert "severity=high" in first_line
    assert "scope=runner-pipeline" in first_line
    assert "sample-event=rk-event" in first_line


def test_op755_no_aggregated_header_on_single_event():
    """count=1 dispatches must NOT carry the aggregated header — only
    ``[<SEV>] <code>: <message>`` exactly as OP-722 emitted."""
    n, _, ch = _make_notifier(dedup=1.0)
    n.notify(Severity.WARN, "single", "one event")
    n.flush_all()
    body = format_text(ch["jira"].calls[0])
    assert "events in" not in body
    assert body.startswith("[WARN] single: one event")


def test_op755_aggregated_format_uses_severity_tier_label():
    """Header ``severity=`` field renders the tier (low/medium/high),
    not the enum value."""
    n, clk, ch = _make_notifier(dedup=60.0)
    for _ in range(2):
        n.notify(Severity.DEGRADED, "x", "m", scope="s", root_cause_key="rk")
        clk.advance(5)
    clk.advance(61)
    n.flush_expired()
    body = format_text(ch["jira"].calls[0])
    assert "severity=medium" in body.splitlines()[0]


# ── OP-755 AC4: per-channel rate limit ────────────────────────────


def test_op755_ac4_medium_severity_rate_limited_to_5_per_15min():
    """AC: max 5 alerts / 15min for medium severity (per channel).

    Default rate-limit config is medium = 5/900s. The 6th medium burst
    in a 15min window must be dropped on every channel."""
    n, clk, ch = _make_notifier(
        dedup=1.0,
        rate_limit_config=default_rate_limit_config(),
    )
    # Six DEGRADED bursts on six distinct root_cause_keys → six
    # distinct dispatches.
    for i in range(6):
        n.notify(
            Severity.DEGRADED, f"code-{i}", "m",
            scope="s", root_cause_key=f"rk-{i}",
        )
        clk.advance(2)
        n.flush_expired()
    # 5 dispatched, 6th rate-limited on every wired channel.
    assert len(ch["jira"].calls) == 5
    assert len(ch["email"].calls) == 5

    # After 15-minute window expires, a fresh dispatch is allowed again.
    clk.advance(901)
    n.notify(Severity.DEGRADED, "recover", "m", scope="s", root_cause_key="rk-recover")
    clk.advance(2)
    n.flush_expired()
    assert len(ch["jira"].calls) == 6


def test_op755_ac4_rate_limit_per_channel_independent():
    """A per-channel override on Slack must not leak to Email.

    Configure slack to medium=2/900s but leave the wildcard at 10/900s.
    On the 3rd burst, Slack is dropped but Email still sends."""
    rl = RateLimitConfig(rules={
        ("*", "medium"): (10, 900.0),
        ("slack", "medium"): (2, 900.0),
    })
    n, clk, ch = _make_notifier(dedup=1.0, rate_limit_config=rl)
    # CRITICAL bypasses rate limit entirely — use DEGRADED channels
    # (jira+email) for this test instead.
    # But we need a slack-routed severity. CRITICAL/P0 bypass per spec.
    # Solution: build a custom RateLimiter and exercise allow() directly.
    rl_inst = RateLimiter(rl)
    assert rl_inst.allow("slack", Severity.DEGRADED) is True   # 1
    assert rl_inst.allow("slack", Severity.DEGRADED) is True   # 2
    assert rl_inst.allow("slack", Severity.DEGRADED) is False  # 3 dropped
    # Email under the same severity still has 10-bucket headroom.
    assert rl_inst.allow("email", Severity.DEGRADED) is True
    # Touch ch so flake8/pyflakes don't drop the assignment in --strict.
    _ = (n, clk, ch)


def test_op755_ac5_high_severity_bypasses_rate_limit():
    """AC: High severity (CRITICAL, P0) bypasses rate-limit but still
    dedupes.

    Even with a draconian medium=1/900s + low=1/900s config, CRITICAL
    must dispatch every time (each on a different root_cause_key)."""
    rl = RateLimitConfig(rules={
        ("*", "low"): (1, 900.0),
        ("*", "medium"): (1, 900.0),
    })
    n, clk, ch = _make_notifier(dedup=1.0, rate_limit_config=rl)
    for i in range(10):
        n.notify(
            Severity.CRITICAL, f"page-{i}", "m",
            scope="s", root_cause_key=f"rk-{i}",
        )
        clk.advance(2)
        n.flush_expired()
    assert len(ch["slack"].calls) == 10
    assert len(ch["line"].calls) == 10


def test_op755_high_severity_still_dedupes():
    """AC: high-severity bypass applies to dispatch, not to grouping.
    Five CRITICAL calls on the same root_cause_key in the window
    collapse to one outbound carrying count=5."""
    rl = RateLimitConfig()  # no rate limits at all
    n, clk, ch = _make_notifier(dedup=60.0, rate_limit_config=rl)
    for _ in range(5):
        n.notify(
            Severity.CRITICAL, "page-x", "m",
            scope="s", root_cause_key="rk-shared",
        )
        clk.advance(5)
    clk.advance(61)
    n.flush_expired()
    assert len(ch["slack"].calls) == 1
    assert ch["slack"].calls[0].count == 5


def test_op755_p0_bypasses_rate_limit():
    """P0 is also "high" tier — must bypass alongside CRITICAL."""
    rl = RateLimitConfig(rules={("*", "medium"): (1, 900.0)})
    rl_inst = RateLimiter(rl)
    assert rl_inst.allow("slack", Severity.P0) is True
    assert rl_inst.allow("slack", Severity.P0) is True
    assert rl_inst.allow("slack", Severity.P0) is True


def test_op755_low_severity_unlimited_by_default():
    """Default config rate-limits medium only; WARN must always pass."""
    rl_inst = RateLimiter(default_rate_limit_config())
    for _ in range(50):
        assert rl_inst.allow("jira", Severity.WARN) is True


def test_op755_rate_limited_drop_recorded_on_payload_context():
    """When a dispatch is rate-limit-dropped on some channels, the
    payload context records the dropped channel names for observability
    — operators can grep ``_rate_limited_channels`` in JIRA threads."""
    # Saturate JIRA to 1/900s, leave email open.
    rl = RateLimitConfig(rules={
        ("jira", "medium"): (1, 900.0),
    })
    n, clk, ch = _make_notifier(dedup=1.0, rate_limit_config=rl)
    n.notify(Severity.DEGRADED, "a", "m1", scope="s", root_cause_key="rk-a")
    clk.advance(2)
    n.flush_expired()
    # First burst: jira accepts, email accepts.
    assert len(ch["jira"].calls) == 1
    assert len(ch["email"].calls) == 1

    n.notify(Severity.DEGRADED, "b", "m2", scope="s", root_cause_key="rk-b")
    clk.advance(2)
    n.flush_expired()
    # Second burst: jira dropped, email still sent.
    assert len(ch["jira"].calls) == 1  # unchanged
    assert len(ch["email"].calls) == 2
    second = ch["email"].calls[1]
    assert second.context.get("_rate_limited_channels") == "jira"


# ── OP-755 AC6: configurability ───────────────────────────────────


def test_op755_ac6_rate_limits_configurable_via_env_default():
    """AC: thresholds configurable per channel.

    Default env (no overrides) yields medium=5/900 wildcard."""
    cfg = build_rate_limit_config_from_env(env={})
    assert cfg.limit_for("slack", "medium") == (5, 900.0)
    assert cfg.limit_for("jira", "medium") == (5, 900.0)
    # Low is unconfigured by default.
    assert cfg.limit_for("slack", "low") is None


def test_op755_rate_limits_configurable_via_env_overrides():
    env = {
        "OMNISIGHT_NOTIFIER_RATE_LIMIT_MEDIUM": "10/600",
        "OMNISIGHT_NOTIFIER_RATE_LIMIT_LOW": "30/900",
        "OMNISIGHT_NOTIFIER_RATE_LIMIT_SLACK_MEDIUM": "2/900",
    }
    cfg = build_rate_limit_config_from_env(env=env)
    # Wildcard defaults applied to channels with no override.
    assert cfg.limit_for("jira", "medium") == (10, 600.0)
    assert cfg.limit_for("jira", "low") == (30, 900.0)
    # Per-channel override beats wildcard.
    assert cfg.limit_for("slack", "medium") == (2, 900.0)
    # Channels not overridden still see the wildcard.
    assert cfg.limit_for("email", "medium") == (10, 600.0)


def test_op755_rate_limit_invalid_spec_falls_back_silently():
    """An unparseable spec is logged and ignored — the env var must not
    block notifier startup. Empty string disables the medium default."""
    env = {
        "OMNISIGHT_NOTIFIER_RATE_LIMIT_MEDIUM": "not-a-spec",
    }
    cfg = build_rate_limit_config_from_env(env=env)
    # No medium rule in the resolved config (the default was overridden
    # by an invalid spec, which parses to None).
    assert cfg.limit_for("slack", "medium") is None


def test_op755_rate_limit_zero_or_negative_disables_rule():
    """A spec with ``count <= 0`` or ``window <= 0`` is treated as
    "no rule" (unlimited), not "always block"."""
    env = {"OMNISIGHT_NOTIFIER_RATE_LIMIT_MEDIUM": "0/900"}
    cfg = build_rate_limit_config_from_env(env=env)
    assert cfg.limit_for("slack", "medium") is None


def test_op755_severity_tier_mapping():
    """Stable mapping from Severity to tier label used by both the
    aggregated header and rate-limit config."""
    assert severity_tier(Severity.WARN) == "low"
    assert severity_tier(Severity.DEGRADED) == "medium"
    assert severity_tier(Severity.CRITICAL) == "high"
    assert severity_tier(Severity.P0) == "high"
    assert is_high_severity(Severity.CRITICAL) is True
    assert is_high_severity(Severity.P0) is True
    assert is_high_severity(Severity.DEGRADED) is False
    assert is_high_severity(Severity.WARN) is False


def test_op755_rate_limit_window_pruning_after_expiry():
    """Sliding window — after entries expire, the bucket recovers
    capacity. Without pruning, the bucket would block forever."""
    clk = FakeClock(0.0)
    rl = RateLimiter(
        RateLimitConfig(rules={("*", "medium"): (3, 100.0)}),
        clock=clk.now,
    )
    # Saturate.
    assert rl.allow("slack", Severity.DEGRADED) is True   # t=0
    clk.advance(10)
    assert rl.allow("slack", Severity.DEGRADED) is True   # t=10
    clk.advance(10)
    assert rl.allow("slack", Severity.DEGRADED) is True   # t=20
    assert rl.allow("slack", Severity.DEGRADED) is False  # t=20 — saturated
    # Advance past the t=0 entry's expiry; bucket size drops to 2.
    clk.advance(91)  # now t=111, cutoff=11 → t=0 dropped, t=10 dropped
    assert rl.allow("slack", Severity.DEGRADED) is True   # accepted

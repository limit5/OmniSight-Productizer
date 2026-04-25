"""R9 row 2940 (#315) — P3 fan-out dispatcher contract tests.

Locks the row 2940 sub-bullet verbatim:

  P3 (自動修復中) → L1 log + email 匯總報告 (digest)

Row 2935 shipped the spec module + ``Notification.severity`` field +
``notify(severity=...)`` plumb-through; row 2936 / 2939 shipped the
P1 / P2 fan-out paths. THIS row owns the **L1_LOG_EMAIL** leg —
``severity="P3"`` notifications fire (a) a structured ``[severity:P3]``
log line via ``logger.info`` + ``add_system_log`` and (b) get appended
to a per-process digest buffer that the ``run_email_digest_loop``
background task drains every ``notification_email_digest_interval_s``
into either an SMTP-sent digest email or — when SMTP is not
configured — a single structured fallback log line.

What we lock here:

  1. ``_dispatch_log_email(notif)`` for P3 appends to the digest buffer
     AND emits a structured log line with the severity tag.
  2. ``_dispatch_external(notif)`` with ``notif.severity = P3`` fires
     ONLY the L1_LOG_EMAIL leg — NOT Slack / Jira / PagerDuty / SMS /
     ChatOps. The severity-additive ladder restricts P3 to its mapped
     tier set.
  3. ``notify()`` spawns ``_dispatch_external`` for P3 even when the
     caller passes ``level="info"`` (the default). Without this, the
     legacy level-only gate ``level in {warning, action, critical}``
     would silently drop P3 events.
  4. ``_flush_email_digest()`` with no SMTP configured drains the
     buffer into a single fallback log line and reports
     ``fallback_logged=1, sent=0``.
  5. ``_flush_email_digest()`` with SMTP configured calls ``smtplib``
     once with the right (host, port, sender, recipients, body)
     tuple, returns ``sent=1, fallback_logged=0``, and drains the
     buffer.
  6. SMTP failure (any exception inside ``_smtp_send_digest``) falls
     back to log-only without losing events; reports
     ``fallback_logged=1``.
  7. Tier-mapping coverage: ``SEVERITY_TIER_MAPPING[Severity.P3]`` is
     exactly ``frozenset({L1_LOG_EMAIL})`` (drift guard).
  8. Buffer overflow drops oldest events (deque maxlen) and emits a
     one-line warning the first time per interval (not per-event).
  9. ``run_email_digest_loop`` cancels cleanly on ``CancelledError``
     and drains the buffer one final time before exiting (graceful
     shutdown contract).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from backend.models import Notification, Severity


# ─────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_digest_state(monkeypatch):
    """Clear the per-process digest buffer + reset overflow flag +
    reset singleton guard between tests so cross-test pollution can't
    leak state. Restores the original deque (with whatever maxlen the
    module ships) at teardown.
    """
    from backend import notifications as n
    n._DIGEST_BUFFER.clear()
    monkeypatch.setattr(n, "_DIGEST_OVERFLOW_WARNED", False)
    monkeypatch.setattr(n, "_DIGEST_RUNNING", False)
    yield
    n._DIGEST_BUFFER.clear()


@pytest.fixture()
def smtp_unconfigured(monkeypatch):
    """All email-related settings empty so ``_flush_email_digest``
    takes the log-only fallback path.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "notification_email_smtp_host", "")
    monkeypatch.setattr(settings, "notification_email_smtp_port", 587)
    monkeypatch.setattr(settings, "notification_email_smtp_user", "")
    monkeypatch.setattr(settings, "notification_email_smtp_password", "")
    monkeypatch.setattr(settings, "notification_email_smtp_use_tls", True)
    monkeypatch.setattr(settings, "notification_email_from", "")
    monkeypatch.setattr(settings, "notification_email_to", "")
    monkeypatch.setattr(settings, "notification_email_digest_interval_s", 60)
    monkeypatch.setattr(settings, "notification_email_digest_max_buffer", 500)
    return settings


@pytest.fixture()
def smtp_configured(monkeypatch):
    """SMTP fully wired so ``_flush_email_digest`` takes the live send
    path; the actual ``smtplib.SMTP`` is intercepted by ``fake_smtp``.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "notification_email_smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "notification_email_smtp_port", 587)
    monkeypatch.setattr(settings, "notification_email_smtp_user", "alerts@omnisight.test")
    monkeypatch.setattr(settings, "notification_email_smtp_password", "supersecret")
    monkeypatch.setattr(settings, "notification_email_smtp_use_tls", True)
    monkeypatch.setattr(settings, "notification_email_from", "alerts@omnisight.test")
    monkeypatch.setattr(settings, "notification_email_to", "oncall@omnisight.test, sre@omnisight.test")
    monkeypatch.setattr(settings, "notification_email_digest_interval_s", 60)
    monkeypatch.setattr(settings, "notification_email_digest_max_buffer", 500)
    return settings


@pytest.fixture()
def fake_smtp(monkeypatch):
    """Replace ``smtplib.SMTP`` with a capture so we can assert host /
    port / login / sendmail args without a real server.
    """
    captured: dict = {
        "host": None,
        "port": None,
        "starttls": 0,
        "login": None,
        "sendmail": None,
        "ehlo": 0,
    }

    class _FakeSMTP:
        def __init__(self, host, port, timeout=30):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            captured["ehlo"] += 1

        def starttls(self):
            captured["starttls"] += 1

        def login(self, user, password):
            captured["login"] = (user, password)

        def sendmail(self, sender, recipients, payload):
            captured["sendmail"] = (sender, list(recipients), payload)

    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return captured


@pytest.fixture()
def stub_persistence(monkeypatch):
    """Skip DB writes — _dispatch_external still runs the dispatch-status
    update branch but writes a no-op.
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


def _p3_notif(level: str = "info", uid: str = "notif-p3-test") -> Notification:
    return Notification(
        id=uid,
        level=level,
        title="Auto-recovered task task-42",
        message="watchdog.p3_auto_recovery — checkpoint reload succeeded",
        source="watchdog",
        timestamp="2026-04-25T00:00:00",
        severity=Severity.P3,
    )


def _legacy_info_notif() -> Notification:
    return Notification(
        id="notif-legacy-info",
        level="info",
        title="Cache warmed",
        message="prewarm pool replenished",
        source="cache",
        timestamp="2026-04-25T00:00:00",
        # severity intentionally None
    )


# ─────────────────────────────────────────────────────────────────
#  #1 — _dispatch_log_email: buffer append + log line
# ─────────────────────────────────────────────────────────────────


class TestP3DispatchLogEmail:
    def test_appends_to_digest_buffer(self, smtp_unconfigured):
        from backend import notifications as n
        n._dispatch_log_email(_p3_notif())
        assert len(n._DIGEST_BUFFER) == 1
        assert n._DIGEST_BUFFER[0].id == "notif-p3-test"

    def test_emits_severity_tagged_log_line(self, smtp_unconfigured, caplog):
        from backend import notifications as n
        with caplog.at_level(logging.INFO, logger="backend.notifications"):
            n._dispatch_log_email(_p3_notif())
        msgs = " | ".join(r.getMessage() for r in caplog.records)
        assert "P3 auto-recovery" in msgs
        assert "task-42" in msgs

    def test_buffer_overflow_logs_warning_once(
        self, smtp_unconfigured, monkeypatch, caplog,
    ):
        from backend import notifications as n
        monkeypatch.setattr(
            "backend.config.settings.notification_email_digest_max_buffer", 2,
        )
        # Re-trigger the in-function resize check
        n._resize_digest_buffer(2)

        with caplog.at_level(logging.WARNING, logger="backend.notifications"):
            n._dispatch_log_email(_p3_notif(uid="a"))
            n._dispatch_log_email(_p3_notif(uid="b"))
            n._dispatch_log_email(_p3_notif(uid="c"))  # overflow #1
            n._dispatch_log_email(_p3_notif(uid="d"))  # overflow #2 — must NOT re-log

        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "P3 digest buffer at cap" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"Expected exactly one overflow warning per interval, got {len(warnings)}"
        )

    def test_buffer_overflow_drops_oldest(
        self, smtp_unconfigured, monkeypatch,
    ):
        """deque(maxlen=N) silently drops oldest on append-past-cap; the
        digest then carries the most-recent N events. This is intentional
        — P3 is informational and prioritising the freshest events
        beats failing the dispatch on overflow.
        """
        from backend import notifications as n
        monkeypatch.setattr(
            "backend.config.settings.notification_email_digest_max_buffer", 2,
        )
        n._resize_digest_buffer(2)
        n._dispatch_log_email(_p3_notif(uid="a"))
        n._dispatch_log_email(_p3_notif(uid="b"))
        n._dispatch_log_email(_p3_notif(uid="c"))
        ids = [ev.id for ev in n._DIGEST_BUFFER]
        assert ids == ["b", "c"], ids


# ─────────────────────────────────────────────────────────────────
#  #2 — _dispatch_external: P3 fires ONLY L1_LOG_EMAIL leg
# ─────────────────────────────────────────────────────────────────


class TestP3FanOutLegs:
    @pytest.mark.asyncio
    async def test_p3_fires_only_log_email_leg(
        self, smtp_unconfigured, stub_persistence, monkeypatch,
    ):
        """P3 → L1 log + email digest only. No Slack / Jira / PagerDuty /
        SMS / ChatOps. Severity-additive ladder restricts P3 to its
        mapped tier set; the dispatcher must NOT silently piggy-back P3
        onto the broadcast channels.
        """
        from backend import notifications as n
        # Configure the broadcast channels so the legacy gate WOULD fire
        # them if reached — proves P3 doesn't accidentally activate them.
        from backend.config import settings
        monkeypatch.setattr(settings, "notification_slack_webhook", "https://hooks.slack.test/X")
        monkeypatch.setattr(settings, "notification_jira_url", "https://jira.test")
        monkeypatch.setattr(settings, "notification_jira_token", "tk")
        monkeypatch.setattr(settings, "notification_jira_project", "OMNI")
        monkeypatch.setattr(settings, "notification_pagerduty_key", "pd-key")
        monkeypatch.setattr(settings, "notification_sms_webhook", "https://sms.test")
        monkeypatch.setattr(settings, "notification_sms_to", "+15551234567")

        captured_curls: list = []

        async def _fake_exec(*args, **kwargs):
            captured_curls.append(args)

            class _P:
                returncode = 0

                async def communicate(self):
                    return b"", b""
            return _P()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        chatops_calls: list = []
        from backend import chatops_bridge as bridge

        async def _fake_send_interactive(*a, **kw):
            chatops_calls.append((a, kw))

            class _M:
                pass
            return _M()
        monkeypatch.setattr(bridge, "send_interactive", _fake_send_interactive)

        await n._dispatch_external(_p3_notif(level="info"))
        await asyncio.sleep(0)  # let any fire-and-forget tasks drain

        assert captured_curls == [], (
            f"P3 must NOT fire any external HTTP leg, got: {captured_curls}"
        )
        assert chatops_calls == [], (
            "P3 must NOT fire ChatOps interactive (that's L2 P2-only)"
        )
        # But the digest buffer MUST have one entry.
        assert len(n._DIGEST_BUFFER) == 1
        assert n._DIGEST_BUFFER[0].id == "notif-p3-test"

    @pytest.mark.asyncio
    async def test_p3_with_level_info_default_still_dispatches(
        self, smtp_unconfigured, stub_persistence, monkeypatch,
    ):
        """The notify() gate must spawn _dispatch_external for severity=P3
        even when level="info" — without this, P3 events are silently
        dropped because the legacy level-only gate excludes "info".
        """
        from backend import notifications as n

        # Use the public notify() entrypoint to exercise the gate.
        # Stub persistence + SSE bus so we don't need real DB / pubsub.
        from backend import db
        called: list = []

        async def _fake_insert(conn, data):
            called.append(("insert", data.get("id"), data.get("severity")))

        monkeypatch.setattr(db, "insert_notification", _fake_insert)
        # bus.publish is sync; just let it run.

        notif = await n.notify(
            level="info",
            title="Auto-recovery confirmed",
            message="task-99 reloaded from checkpoint",
            source="watchdog",
            severity="P3",
        )
        assert notif.severity == Severity.P3
        # Give the asyncio.create_task(_dispatch_external) spawned by
        # notify() one yield to actually run.
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(n._DIGEST_BUFFER) == 1, (
            f"P3 with level=info must reach the digest buffer; "
            f"got {len(n._DIGEST_BUFFER)} events"
        )

    @pytest.mark.asyncio
    async def test_legacy_info_does_not_dispatch(
        self, smtp_unconfigured, stub_persistence, monkeypatch,
    ):
        """Negative regression guard: a legacy ``notify(level="info")``
        without severity must NOT spawn _dispatch_external (this is the
        original level-only behaviour) and must not reach the digest
        buffer.
        """
        from backend import notifications as n
        from backend import db

        async def _fake_insert(conn, data):
            return None
        monkeypatch.setattr(db, "insert_notification", _fake_insert)

        await n.notify(
            level="info",
            title="Cache warmed",
            message="prewarm pool replenished",
            source="cache",
        )
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(n._DIGEST_BUFFER) == 0, (
            "Legacy info notification must not enter the P3 digest buffer"
        )


# ─────────────────────────────────────────────────────────────────
#  #3 — _flush_email_digest: log-only fallback
# ─────────────────────────────────────────────────────────────────


class TestDigestFlushLogFallback:
    @pytest.mark.asyncio
    async def test_no_smtp_configured_logs_fallback(
        self, smtp_unconfigured, caplog,
    ):
        from backend import notifications as n
        n._dispatch_log_email(_p3_notif(uid="x"))
        n._dispatch_log_email(_p3_notif(uid="y"))

        with caplog.at_level(logging.INFO, logger="backend.notifications"):
            result = await n._flush_email_digest()

        assert result == {"events": 2, "sent": 0, "fallback_logged": 1}
        assert len(n._DIGEST_BUFFER) == 0  # buffer drained
        msgs = " | ".join(r.getMessage() for r in caplog.records)
        assert "P3 digest" in msgs
        assert "log-only fallback" in msgs

    @pytest.mark.asyncio
    async def test_empty_buffer_is_noop(self, smtp_unconfigured):
        from backend import notifications as n
        result = await n._flush_email_digest()
        assert result == {"events": 0, "sent": 0, "fallback_logged": 0}


# ─────────────────────────────────────────────────────────────────
#  #4 — _flush_email_digest: SMTP send path
# ─────────────────────────────────────────────────────────────────


class TestDigestFlushSmtp:
    @pytest.mark.asyncio
    async def test_smtp_send_fires_with_correct_args(
        self, smtp_configured, fake_smtp,
    ):
        from backend import notifications as n
        n._dispatch_log_email(_p3_notif(uid="x"))
        n._dispatch_log_email(_p3_notif(uid="y"))

        result = await n._flush_email_digest()

        assert result == {"events": 2, "sent": 1, "fallback_logged": 0}
        assert fake_smtp["host"] == "smtp.test"
        assert fake_smtp["port"] == 587
        assert fake_smtp["starttls"] == 1  # use_tls=True
        assert fake_smtp["login"] == ("alerts@omnisight.test", "supersecret")
        sender, recipients, payload = fake_smtp["sendmail"]
        assert sender == "alerts@omnisight.test"
        # CSV-split into two recipients
        assert recipients == ["oncall@omnisight.test", "sre@omnisight.test"]
        # Parse the RFC822 payload so we can inspect the decoded body
        # without caring whether MIMEText chose 7bit / qp / base64
        # (utf-8 content typically gets base64-encoded by stdlib).
        import email
        msg = email.message_from_string(payload)
        decoded_body = msg.get_payload(decode=True).decode("utf-8")
        assert "Auto-recovered task task-42" in decoded_body
        assert "[severity:P3]" in decoded_body
        assert "auto-recovery" in decoded_body
        # Subject carries the worker PID so multi-worker digests can
        # be mentally deduped by the operator.
        subject = str(msg["Subject"])
        assert "P3" in subject
        assert "auto-recovery" in subject

    @pytest.mark.asyncio
    async def test_smtp_failure_falls_back_to_log_without_losing_events(
        self, smtp_configured, fake_smtp, monkeypatch, caplog,
    ):
        from backend import notifications as n
        n._dispatch_log_email(_p3_notif(uid="x"))

        def _explode(*a, **kw):
            raise RuntimeError("smtp gateway down")

        monkeypatch.setattr(n, "_smtp_send_digest", _explode)

        with caplog.at_level(logging.WARNING, logger="backend.notifications"):
            result = await n._flush_email_digest()

        assert result["events"] == 1
        assert result["sent"] == 0
        assert result["fallback_logged"] == 1
        # SMTP failure warning must show
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("SMTP send failed" in r.getMessage() for r in warnings)


# ─────────────────────────────────────────────────────────────────
#  #5 — Tier-mapping coverage drift guard
# ─────────────────────────────────────────────────────────────────


class TestTierMappingP3Coverage:
    def test_p3_mapping_is_exactly_l1_log_email(self):
        from backend.severity import (
            L1_LOG_EMAIL,
            SEVERITY_TIER_MAPPING,
            Severity,
        )
        expected = frozenset({L1_LOG_EMAIL})
        assert SEVERITY_TIER_MAPPING[Severity.P3] == expected

    def test_p3_does_not_include_p1_or_p2_tiers(self):
        """Negative regression guard against a refactor that simplifies
        ``P1 ⊃ P2 ⊃ P3`` (a tempting but wrong intuition — P3 is a
        DIFFERENT channel, not a watered-down P1).
        """
        from backend.severity import (
            L2_CHATOPS_INTERACTIVE,
            L2_IM_WEBHOOK,
            L3_JIRA,
            L4_PAGERDUTY,
            L4_SMS,
            SEVERITY_TIER_MAPPING,
            Severity,
        )
        p3 = SEVERITY_TIER_MAPPING[Severity.P3]
        assert L4_PAGERDUTY not in p3
        assert L4_SMS not in p3
        assert L3_JIRA not in p3
        assert L2_IM_WEBHOOK not in p3
        assert L2_CHATOPS_INTERACTIVE not in p3


# ─────────────────────────────────────────────────────────────────
#  #6 — run_email_digest_loop: cancellation drain + singleton guard
# ─────────────────────────────────────────────────────────────────


class TestEmailDigestLoop:
    @pytest.mark.asyncio
    async def test_loop_drains_buffer_on_cancel(
        self, smtp_unconfigured, monkeypatch,
    ):
        """Graceful shutdown contract: when the loop is cancelled
        (lifespan teardown), it must still flush the buffer once before
        exiting so the events queued in the last interval window aren't
        lost on restart.
        """
        from backend import notifications as n
        # Make the interval longer than the test so the loop doesn't
        # naturally tick during the test window.
        monkeypatch.setattr(
            "backend.config.settings.notification_email_digest_interval_s", 3600,
        )

        flushes: list = []
        original_flush = n._flush_email_digest

        async def _spy_flush():
            r = await original_flush()
            flushes.append(r)
            return r

        monkeypatch.setattr(n, "_flush_email_digest", _spy_flush)

        n._dispatch_log_email(_p3_notif(uid="pre-cancel"))
        task = asyncio.create_task(n.run_email_digest_loop())
        # Yield once so the loop enters the await asyncio.sleep(interval).
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The shutdown drain must have flushed the one queued event.
        assert any(f.get("events") == 1 for f in flushes), (
            f"Expected at least one flush of 1 event on shutdown drain, got {flushes}"
        )

    @pytest.mark.asyncio
    async def test_loop_singleton_guard(self, smtp_unconfigured, monkeypatch):
        from backend import notifications as n
        monkeypatch.setattr(
            "backend.config.settings.notification_email_digest_interval_s", 3600,
        )

        t1 = asyncio.create_task(n.run_email_digest_loop())
        await asyncio.sleep(0)  # let t1 enter the loop
        # Second start while t1 still running must be no-op (returns
        # immediately).
        result = await asyncio.wait_for(n.run_email_digest_loop(), timeout=0.5)
        assert result is None
        t1.cancel()
        try:
            await t1
        except asyncio.CancelledError:
            pass


# ─────────────────────────────────────────────────────────────────
#  #7 — _smtp_send_digest input validation
# ─────────────────────────────────────────────────────────────────


class TestSmtpInputValidation:
    def test_rejects_when_sender_unresolvable(
        self, smtp_unconfigured, monkeypatch,
    ):
        """``_smtp_send_digest`` derives the From: header from
        ``notification_email_from`` falling back to
        ``notification_email_smtp_user``; both empty must raise so the
        caller falls back to log-only path with a clear error.
        """
        from backend import notifications as n
        from backend.config import settings
        # Both empty:
        monkeypatch.setattr(settings, "notification_email_from", "")
        monkeypatch.setattr(settings, "notification_email_smtp_user", "")
        monkeypatch.setattr(settings, "notification_email_to", "x@y.test")
        with pytest.raises(RuntimeError, match="From: header"):
            n._smtp_send_digest("subj", "body", "x@y.test")

    def test_rejects_when_no_recipients(
        self, smtp_unconfigured, monkeypatch,
    ):
        from backend import notifications as n
        from backend.config import settings
        monkeypatch.setattr(settings, "notification_email_from", "alerts@omnisight.test")
        with pytest.raises(RuntimeError, match="no addresses"):
            n._smtp_send_digest("subj", "body", "   ,  ,")

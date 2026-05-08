"""Operator notification bridge — multi-channel alert fanout (OP-722).

Foundation ticket of META OP-721. Every operator-facing alert in the
program — JIRA daemon events, gerrit-jira-bridge errors, scanner
findings — flows through ``notify()`` so we can rate-limit, deduplicate
and route by severity centrally.

Severity → channel matrix:

    WARN      → JIRA comment only
    DEGRADED  → JIRA + email
    CRITICAL  → JIRA + email + Slack + LINE
    P0        → JIRA + email + Slack + LINE, re-paged every
                ``OMNISIGHT_NOTIFIER_P0_REPAGE_SECONDS`` until acked

Anti-flood (dedup): identical ``(code, severity)`` pairs within
``OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS`` (default 300s) collapse
into ONE outbound notification carrying ``count=N`` — the literal AC
requirement. Mechanically:

* ``notify()`` is non-dispatching: it inserts/updates an in-memory
  burst entry and returns the running snapshot. No channel I/O.
* :meth:`Notifier.flush_expired` dispatches every entry whose
  ``first_seen + dedup_window`` has elapsed and clears the entry.
* :meth:`Notifier.flush_all` force-flushes every entry regardless of
  age (used by tests and clean-shutdown paths).
* :meth:`Notifier.start_dispatch_loop` runs a background thread that
  periodically calls ``flush_expired`` and ``tick_ack_loop``.

This shape means there is a worst-case dispatch delay of
``dedup_window`` seconds on any notification. Operators tune the
window per their tolerance; for low-latency CRITICAL paths set
``OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS=5`` (or similar). The
trade-off is documented in ``docs/sop/notifier-config.md``.

Acknowledgement: every CRITICAL/P0 notification carries an ack URL
(``OMNISIGHT_NOTIFIER_ACK_BASE_URL/<notification_id>``).
:func:`acknowledge` clears the re-page schedule. Without ack within
``critical_repage_seconds`` / ``p0_repage_seconds``, the notification
is re-dispatched.

Config (env vars; ``Notifier`` accepts overrides for tests):

    OMNISIGHT_NOTIFIER_JIRA_TICKET             default ticket key for
                                               JIRA fanout when notify()
                                               caller does not pass one
    OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS        comma-separated to-addresses
    OMNISIGHT_NOTIFIER_SMTP_HOST               relay host (host:port form
                                               accepted; port defaults 587)
    OMNISIGHT_NOTIFIER_SMTP_USER               SMTP auth username
    OMNISIGHT_NOTIFIER_SMTP_PASSWORD           SMTP auth password
    OMNISIGHT_NOTIFIER_SMTP_FROM               From: address
    OMNISIGHT_NOTIFIER_SLACK_WEBHOOK           Slack incoming-webhook URL
    OMNISIGHT_NOTIFIER_LINE_TOKEN              LINE Notify bearer token
    OMNISIGHT_NOTIFIER_ACK_BASE_URL            prefix for ack URL (HTTP
                                               endpoint impl: separate ticket)
    OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS    default 300
    OMNISIGHT_NOTIFIER_P0_REPAGE_SECONDS       default 300 (P0)
    OMNISIGHT_NOTIFIER_CRITICAL_REPAGE_SECONDS default 600 (CRITICAL)
    OMNISIGHT_NOTIFIER_AGENT_CLASS             agent_class for the JIRA
                                               dispatch client (default
                                               "subscription-claude")

Canary self-test: :func:`canary_self_test` calls ``send`` directly
on every wired channel — bypassing the dedup layer — so a misconfigured
webhook surfaces at startup instead of waiting for a real incident.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import Enum
from typing import Any, Callable, Protocol

logger = logging.getLogger("omnisight.operator_notifier")


# ── Severity ──────────────────────────────────────────────────────


class Severity(str, Enum):
    """Severity tiers. String values are stable identifiers used in
    dedup keys, log lines and channel payloads — do not rename without
    a migration plan."""

    WARN = "WARN"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    P0 = "P0"


_FANOUT: dict[Severity, frozenset[str]] = {
    Severity.WARN: frozenset({"jira"}),
    Severity.DEGRADED: frozenset({"jira", "email"}),
    Severity.CRITICAL: frozenset({"jira", "email", "slack", "line"}),
    Severity.P0: frozenset({"jira", "email", "slack", "line"}),
}


def channels_for(severity: Severity) -> frozenset[str]:
    return _FANOUT[severity]


# ── Channel protocol + implementations ────────────────────────────


class Channel(Protocol):
    """Minimal channel contract. Implementations MUST raise on failure
    so the dispatcher can record per-channel error context."""

    name: str

    def send(self, payload: "Notification") -> None: ...


@dataclass
class Notification:
    """One outbound notification record. ``count`` reflects how many
    upstream ``notify()`` calls collapsed into this single fanout."""

    notification_id: str
    severity: Severity
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    ticket: str | None = None
    count: int = 1
    ack_url: str | None = None
    created_at: float = field(default_factory=time.time)


def format_text(payload: Notification) -> str:
    """Plain-text body shared across channels. Centralised so the
    dedup ``count=N`` and ack URL render consistently everywhere."""

    lines = [f"[{payload.severity.value}] {payload.code}: {payload.message}"]
    if payload.count > 1:
        lines.append(f"(suppressed {payload.count - 1} duplicates within dedup window; count={payload.count})")
    if payload.context:
        lines.append("Context:")
        for k, v in sorted(payload.context.items()):
            lines.append(f"  {k}: {v}")
    if payload.ack_url:
        lines.append(f"Ack: {payload.ack_url}")
    lines.append(f"id={payload.notification_id}")
    return "\n".join(lines)


# JIRA channel ─────────────────────────────────────────────────────

class JiraChannel:
    """JIRA channel. Uses :mod:`backend.agents.jira_dispatch` to post
    an ADF comment on the supplied ticket."""

    name = "jira"

    def __init__(
        self,
        agent_class: str,
        default_ticket: str | None,
        client_factory: Callable[[str], Any] | None = None,
        comment_fn: Callable[[Any, str, str], None] | None = None,
    ) -> None:
        self.agent_class = agent_class
        self.default_ticket = default_ticket
        self._client_factory = client_factory
        self._comment_fn = comment_fn
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(self.agent_class)
        else:
            from backend.agents.jira_dispatch import make_client
            self._client = make_client(self.agent_class)
        return self._client

    def send(self, payload: Notification) -> None:
        ticket = payload.ticket or self.default_ticket
        if not ticket:
            raise RuntimeError(
                "JIRA channel: no ticket on payload and no OMNISIGHT_NOTIFIER_JIRA_TICKET configured"
            )
        client = self._ensure_client()
        if self._comment_fn is not None:
            self._comment_fn(client, ticket, format_text(payload))
            return
        from backend.agents.jira_dispatch import add_comment
        add_comment(client, ticket, format_text(payload))


# Email channel ────────────────────────────────────────────────────

@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    use_starttls: bool = True


class EmailChannel:
    """SMTP email channel. ``send_fn`` is overridable for tests; the
    production path uses :class:`smtplib.SMTP`."""

    name = "email"

    def __init__(
        self,
        smtp: SmtpConfig,
        recipients: list[str],
        send_fn: Callable[[SmtpConfig, EmailMessage], None] | None = None,
    ) -> None:
        self.smtp = smtp
        self.recipients = list(recipients)
        self._send_fn = send_fn or _smtp_send

    def send(self, payload: Notification) -> None:
        if not self.recipients:
            raise RuntimeError("Email channel: no recipients configured")
        msg = EmailMessage()
        msg["Subject"] = f"[{payload.severity.value}] {payload.code}"
        msg["From"] = self.smtp.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(format_text(payload))
        self._send_fn(self.smtp, msg)


def _smtp_send(smtp: SmtpConfig, msg: EmailMessage) -> None:
    """Production SMTP transport. STARTTLS on submission ports. Kept
    module-level so tests inject a fake without subclassing."""

    with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as s:
        s.ehlo()
        if smtp.use_starttls and smtp.port != 25:
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        if smtp.username and smtp.password:
            s.login(smtp.username, smtp.password)
        s.send_message(msg)


# HTTP-webhook channels (Slack, LINE) ──────────────────────────────


def _http_post(
    url: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> tuple[int, str]:
    headers = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


class SlackChannel:
    """Slack incoming-webhook channel."""

    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        post_fn: Callable[[str, bytes, str, dict[str, str] | None, int], tuple[int, str]] | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self._post_fn = post_fn or _http_post

    def send(self, payload: Notification) -> None:
        if not self.webhook_url:
            raise RuntimeError("Slack channel: webhook URL not configured")
        body = json.dumps({"text": format_text(payload)}).encode("utf-8")
        status, response = self._post_fn(self.webhook_url, body, "application/json", None, 10)
        if status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {status}: {response}")


class LineNotifyChannel:
    """LINE Notify channel. POSTs form-encoded ``message=...`` to
    ``https://notify-api.line.me/api/notify`` with a bearer-token
    Authorization header."""

    name = "line"
    DEFAULT_ENDPOINT = "https://notify-api.line.me/api/notify"

    def __init__(
        self,
        token: str,
        endpoint: str | None = None,
        post_fn: Callable[[str, bytes, str, dict[str, str] | None, int], tuple[int, str]] | None = None,
    ) -> None:
        self.token = token
        self.endpoint = endpoint or self.DEFAULT_ENDPOINT
        self._post_fn = post_fn or _http_post

    def send(self, payload: Notification) -> None:
        if not self.token:
            raise RuntimeError("LINE channel: token not configured")
        body = urllib.parse.urlencode({"message": format_text(payload)}).encode("utf-8")
        status, response = self._post_fn(
            self.endpoint,
            body,
            "application/x-www-form-urlencoded",
            {"Authorization": f"Bearer {self.token}"},
            10,
        )
        if status >= 300:
            raise RuntimeError(f"LINE Notify returned HTTP {status}: {response}")


# ── Coalesce + ack-loop state ─────────────────────────────────────


@dataclass
class _BurstEntry:
    """In-memory burst state for a (code, severity) key. Held until
    flush_expired runs or the entry is force-flushed."""

    notification_id: str
    severity: Severity
    code: str
    message: str
    context: dict[str, Any]
    ticket: str | None
    first_seen: float
    count: int


@dataclass
class _PendingAck:
    """Tracks a CRITICAL/P0 notification awaiting acknowledgement.
    The ack-loop tick re-dispatches when ``now - last_paged_at >= repage_interval``."""

    notification: Notification
    last_paged_at: float
    repage_interval: float


# ── Notifier ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class NotifierConfig:
    dedup_window_seconds: float = 300.0
    p0_repage_seconds: float = 300.0
    critical_repage_seconds: float = 600.0
    ack_base_url: str | None = None
    default_ticket: str | None = None


class Notifier:
    """Multi-channel notification bridge with coalescing + re-page.

    Thread-safety: ``notify``, ``flush_*`` and ``acknowledge`` may be
    called from any thread. Internal state is guarded by a single
    coarse lock — the burst and pending-ack tables are bounded by
    active alert cardinality, so contention is not a concern.

    Background dispatch loop is OFF by default; call
    :meth:`start_dispatch_loop` in long-lived processes (the systemd
    unit does this on startup). Tests drive flushes deterministically
    via :meth:`flush_all` / :meth:`tick_ack_loop` instead of waiting
    on a real timer."""

    def __init__(
        self,
        channels: dict[str, Channel],
        config: NotifierConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.channels = dict(channels)
        self.config = config or NotifierConfig()
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._bursts: dict[tuple[str, Severity], _BurstEntry] = {}
        self._pending: dict[str, _PendingAck] = {}
        self._dispatch_thread: threading.Thread | None = None
        self._dispatch_stop = threading.Event()

    # Public API ---------------------------------------------------

    def notify(
        self,
        severity: Severity | str,
        code: str,
        message: str,
        context: dict[str, Any] | None = None,
        ticket: str | None = None,
    ) -> _BurstEntry:
        """Record a notify() call. Returns the in-memory burst entry —
        NOT an outbound :class:`Notification`. Outbound dispatch happens
        on flush.

        Behaviour:

        * No existing burst for ``(code, severity)`` → open a fresh one
          with ``count=1``.
        * Existing burst within window → increment ``count``; the latest
          message + context overwrite earlier ones (operator-friendly:
          the last sample is usually the most informative).
        * Existing burst past window → flush the stale burst now, then
          open a fresh one. This guarantees we never lose a previous
          burst's count just because a new one started.
        """

        sev = Severity(severity) if not isinstance(severity, Severity) else severity
        ctx = dict(context or {})
        now = self._clock()

        stale_to_flush: _BurstEntry | None = None
        with self._lock:
            key = (code, sev)
            entry = self._bursts.get(key)
            if entry is not None and (now - entry.first_seen) > self.config.dedup_window_seconds:
                stale_to_flush = entry
                entry = None
                self._bursts.pop(key, None)

            if entry is None:
                entry = _BurstEntry(
                    notification_id=uuid.uuid4().hex[:12],
                    severity=sev,
                    code=code,
                    message=message,
                    context=ctx,
                    ticket=ticket or self.config.default_ticket,
                    first_seen=now,
                    count=1,
                )
                self._bursts[key] = entry
            else:
                entry.count += 1
                entry.message = message
                entry.context = ctx
                if ticket:
                    entry.ticket = ticket
            snapshot = _BurstEntry(**vars(entry))

        if stale_to_flush is not None:
            # Stale burst inherited from an expired window — dispatch
            # it now so the operator does not lose the residual count.
            self._dispatch_burst(stale_to_flush, now)

        return snapshot

    def flush_expired(self) -> list[Notification]:
        """Dispatch every burst whose window has elapsed since
        ``first_seen``. Returns the list of dispatched notifications."""

        now = self._clock()
        with self._lock:
            due = [
                e for e in list(self._bursts.values())
                if (now - e.first_seen) >= self.config.dedup_window_seconds
            ]
            for e in due:
                self._bursts.pop((e.code, e.severity), None)
        return [self._dispatch_burst(e, now) for e in due]

    def flush_all(self) -> list[Notification]:
        """Force-flush every buffered burst regardless of age. Used by
        tests and clean-shutdown."""

        now = self._clock()
        with self._lock:
            due = list(self._bursts.values())
            self._bursts.clear()
        return [self._dispatch_burst(e, now) for e in due]

    def acknowledge(self, notification_id: str) -> bool:
        """Clear a pending re-page. Returns True if there was a record
        to clear, False otherwise (already acked / unknown id)."""

        with self._lock:
            return self._pending.pop(notification_id, None) is not None

    def pending_acks(self) -> list[Notification]:
        with self._lock:
            return [p.notification for p in self._pending.values()]

    # Ack loop -----------------------------------------------------

    def tick_ack_loop(self) -> list[Notification]:
        """Process one re-page tick. Returns the list of notifications
        that were re-paged on this tick."""

        now = self._clock()
        with self._lock:
            due: list[_PendingAck] = []
            for entry in list(self._pending.values()):
                if (now - entry.last_paged_at) >= entry.repage_interval:
                    entry.last_paged_at = now
                    due.append(entry)

        repaged: list[Notification] = []
        for entry in due:
            n = entry.notification
            n.count += 1
            self._dispatch(n)
            repaged.append(n)
        return repaged

    def start_dispatch_loop(self, poll_interval: float = 5.0) -> None:
        """Start a background thread that calls ``flush_expired`` and
        ``tick_ack_loop`` every ``poll_interval`` seconds. No-op if
        already running."""

        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return
        self._dispatch_stop.clear()

        def _run() -> None:
            while not self._dispatch_stop.is_set():
                try:
                    self.flush_expired()
                    self.tick_ack_loop()
                except Exception:
                    logger.exception("dispatch loop tick raised")
                self._dispatch_stop.wait(poll_interval)

        t = threading.Thread(target=_run, name="operator-notifier-dispatch", daemon=True)
        self._dispatch_thread = t
        t.start()

    def stop_dispatch_loop(self, join_timeout: float = 5.0) -> None:
        self._dispatch_stop.set()
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=join_timeout)
            self._dispatch_thread = None

    # Internal -----------------------------------------------------

    def _dispatch_burst(self, entry: _BurstEntry, now: float) -> Notification:
        """Convert a burst entry to a Notification and dispatch it."""

        ack_url = None
        if entry.severity in (Severity.CRITICAL, Severity.P0) and self.config.ack_base_url:
            ack_url = f"{self.config.ack_base_url.rstrip('/')}/{entry.notification_id}"

        payload = Notification(
            notification_id=entry.notification_id,
            severity=entry.severity,
            code=entry.code,
            message=entry.message,
            context=dict(entry.context),
            ticket=entry.ticket,
            count=entry.count,
            ack_url=ack_url,
            created_at=entry.first_seen,
        )
        self._dispatch(payload)

        if entry.severity in (Severity.CRITICAL, Severity.P0):
            interval = (
                self.config.p0_repage_seconds if entry.severity == Severity.P0
                else self.config.critical_repage_seconds
            )
            with self._lock:
                self._pending[entry.notification_id] = _PendingAck(
                    notification=payload,
                    last_paged_at=now,
                    repage_interval=interval,
                )
        return payload

    def _dispatch(self, payload: Notification) -> None:
        wanted = channels_for(payload.severity)
        errors: list[str] = []
        for name in sorted(wanted):
            ch = self.channels.get(name)
            if ch is None:
                logger.info("notifier skipping unconfigured channel=%s id=%s", name, payload.notification_id)
                continue
            try:
                ch.send(payload)
                logger.info(
                    "notifier sent channel=%s code=%s severity=%s id=%s count=%d",
                    name, payload.code, payload.severity.value,
                    payload.notification_id, payload.count,
                )
            except Exception as exc:  # noqa: BLE001 — record per-channel and keep going
                logger.exception("notifier channel=%s send failed", name)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        # Partial fanout (e.g. Slack down but JIRA succeeds) must not
        # silence surviving channels: errors are stored on the payload
        # for observability instead of raised.
        if errors:
            payload.context.setdefault("_channel_errors", "; ".join(errors))


# ── Env-driven factory + canary self-test ─────────────────────────


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_smtp_host(raw: str) -> tuple[str, int]:
    """Accept ``host`` or ``host:port``. Defaults to 587 (submission +
    STARTTLS) which is correct for Mailgun/SES/Gmail relays."""

    if ":" in raw:
        h, _, p = raw.partition(":")
        return h, int(p)
    return raw, 587


def build_channels_from_env(env: dict[str, str] | None = None) -> dict[str, Channel]:
    """Wire up the four channels from env vars. Channels with missing
    required config are omitted; :meth:`Notifier._dispatch` logs them
    as skipped, never crashes."""

    e = env if env is not None else os.environ
    channels: dict[str, Channel] = {}

    channels["jira"] = JiraChannel(
        agent_class=e.get("OMNISIGHT_NOTIFIER_AGENT_CLASS", "subscription-claude"),
        default_ticket=e.get("OMNISIGHT_NOTIFIER_JIRA_TICKET"),
    )

    smtp_host = e.get("OMNISIGHT_NOTIFIER_SMTP_HOST")
    recipients = _split_csv(e.get("OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS"))
    sender = e.get("OMNISIGHT_NOTIFIER_SMTP_FROM")
    if smtp_host and recipients and sender:
        host, port = _parse_smtp_host(smtp_host)
        channels["email"] = EmailChannel(
            smtp=SmtpConfig(
                host=host,
                port=port,
                username=e.get("OMNISIGHT_NOTIFIER_SMTP_USER"),
                password=e.get("OMNISIGHT_NOTIFIER_SMTP_PASSWORD"),
                sender=sender,
            ),
            recipients=recipients,
        )

    slack_url = e.get("OMNISIGHT_NOTIFIER_SLACK_WEBHOOK")
    if slack_url:
        channels["slack"] = SlackChannel(slack_url)

    line_token = e.get("OMNISIGHT_NOTIFIER_LINE_TOKEN")
    if line_token:
        channels["line"] = LineNotifyChannel(line_token)

    return channels


def build_notifier_from_env(env: dict[str, str] | None = None) -> Notifier:
    e = env if env is not None else os.environ

    def _f(name: str, default: float) -> float:
        raw = e.get(name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning("notifier env %s=%r not a number; using default %s", name, raw, default)
            return default

    cfg = NotifierConfig(
        dedup_window_seconds=_f("OMNISIGHT_NOTIFIER_DEDUP_WINDOW_SECONDS", 300.0),
        p0_repage_seconds=_f("OMNISIGHT_NOTIFIER_P0_REPAGE_SECONDS", 300.0),
        critical_repage_seconds=_f("OMNISIGHT_NOTIFIER_CRITICAL_REPAGE_SECONDS", 600.0),
        ack_base_url=e.get("OMNISIGHT_NOTIFIER_ACK_BASE_URL"),
        default_ticket=e.get("OMNISIGHT_NOTIFIER_JIRA_TICKET"),
    )
    return Notifier(build_channels_from_env(e), cfg)


def canary_self_test(notifier: Notifier | None = None) -> dict[str, str]:
    """Issue a self-test ping on every wired channel.

    Returns ``{channel_name: "ok" | error_str}`` for the systemd unit
    to log. Bypasses the dedup layer (calls ``Channel.send`` directly)
    so a misconfigured webhook surfaces at startup, before any real
    incident."""

    n = notifier or build_notifier_from_env()
    code = f"notifier:canary:{uuid.uuid4().hex[:8]}"
    results: dict[str, str] = {}
    for name, ch in n.channels.items():
        try:
            ch.send(Notification(
                notification_id=uuid.uuid4().hex[:12],
                severity=Severity.WARN,
                code=code,
                message="canary self-test — operator-notifier bridge online",
                context={"hostname": os.uname().nodename},
                ticket=n.config.default_ticket,
            ))
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {exc}"
            logger.exception("canary self-test channel=%s failed", name)
    return results


# ── Module-level singleton (lazy) ─────────────────────────────────


_default_notifier: Notifier | None = None
_default_lock = threading.Lock()


def get_default_notifier() -> Notifier:
    global _default_notifier
    with _default_lock:
        if _default_notifier is None:
            _default_notifier = build_notifier_from_env()
        return _default_notifier


def notify(
    severity: Severity | str,
    code: str,
    message: str,
    context: dict[str, Any] | None = None,
    ticket: str | None = None,
) -> _BurstEntry:
    """Module-level convenience wrapper — most callers should use this.
    Lazily builds the process-wide :class:`Notifier` from env."""
    return get_default_notifier().notify(severity, code, message, context, ticket)


def acknowledge(notification_id: str) -> bool:
    return get_default_notifier().acknowledge(notification_id)


__all__ = [
    "Severity",
    "Notification",
    "Channel",
    "JiraChannel",
    "EmailChannel",
    "SmtpConfig",
    "SlackChannel",
    "LineNotifyChannel",
    "Notifier",
    "NotifierConfig",
    "build_channels_from_env",
    "build_notifier_from_env",
    "canary_self_test",
    "get_default_notifier",
    "notify",
    "acknowledge",
    "channels_for",
    "format_text",
]

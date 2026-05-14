"""v2 AlertBridge channel adapter implementation.

Cross-channel envelope schema
-----------------------------
Every adapter receives the same :class:`AlertEnvelope` and must preserve the
canonical fields emitted by :func:`canonical_envelope`: ``alertname``,
``severity``, ``area``, ``family``, ``defense_dimension``, ``labels``,
``annotations``, ``dedupe_key``, ``fired_at``, ``resolved_at``, and
``critical_labels``. Stdout writes that schema as one JSON line. Email uses the
same JSON payload as the message body, with the subject ``[{severity}]
{summary}``. The future Alertmanager webhook adapter is expected to normalise
back to this schema before migration tests compare envelopes.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
import hashlib
import json
import logging
import os
import smtplib
import ssl
from typing import IO, Literal, Protocol

LOGGER = logging.getLogger(__name__)

Severity = Literal["page", "warn", "info"]
DefenseDimension = Literal["D1", "D2", "D3", "D4", "D5"]

SEVERITY_CHANNEL_MAP: dict[str, tuple[str, ...]] = {
    "page": ("email", "stdout"),
    "warn": ("email", "stdout"),
    "info": ("stdout",),
}

REQUIRED_ANNOTATIONS = ("summary", "description", "runbook_url", "remediation_hint")
CARDINALITY_META_ALERT = "OmniSightAlertCardinalityCap"


@dataclass(frozen=True)
class AlertEnvelope:
    alertname: str
    severity: Severity
    area: str
    family: str
    defense_dimension: DefenseDimension
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    dedupe_key: str
    fired_at: datetime
    resolved_at: datetime | None
    critical_labels: tuple[str, ...]


@dataclass(frozen=True)
class AdapterHealth:
    ok: bool
    detail: str


class ChannelAdapter(Protocol):
    name: str

    def deliver(self, envelope: AlertEnvelope) -> None:
        """Deliver one canonical alert envelope."""

    def healthcheck(self) -> AdapterHealth:
        """Return static adapter wiring health."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    username: str = ""
    password: str = ""
    use_starttls: bool = True


class DedupeKeyGen:
    @staticmethod
    def generate(alertname: str, family: str, labels: Mapping[str, str], critical_labels: Sequence[str]) -> str:
        if not critical_labels:
            raise ValueError("critical_labels must not be empty")
        if "alertname" in critical_labels or "family" in critical_labels:
            raise ValueError("critical_labels must not include alertname or family")

        missing = [key for key in critical_labels if key not in labels]
        if missing:
            raise ValueError(f"critical_labels missing from labels: {', '.join(sorted(missing))}")

        critical = "|".join(sorted(f"{key}={labels[key]}" for key in critical_labels))
        payload = f"{alertname}\x1f{family}\x1f{critical}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class CardinalityLimitExceeded(RuntimeError):
    def __init__(self, rule: str, label: str, cap: int, meta_alert: AlertEnvelope) -> None:
        super().__init__(f"{rule} crossed cardinality cap for {label}: max {cap} distinct values")
        self.rule = rule
        self.label = label
        self.cap = cap
        self.meta_alert = meta_alert


class CardinalityValidator:
    def __init__(
        self,
        *,
        default_cap: int = 10,
        window: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if default_cap > 10:
            raise ValueError("default cardinality cap must be <= 10")
        self.default_cap = default_cap
        self.window = window
        self._clock = clock or (lambda: datetime.now(UTC))
        self._seen: dict[tuple[str, str], dict[str, datetime]] = {}

    def validate(
        self,
        envelope: AlertEnvelope,
        *,
        caps: Mapping[str, int] | None = None,
    ) -> None:
        now = self._clock()
        caps = caps or {}
        for label, value in envelope.labels.items():
            cap = caps.get(label, self.default_cap)
            if cap > 10:
                raise ValueError(f"cardinality cap for {label} must be <= 10")
            key = (envelope.alertname, label)
            values = self._seen.setdefault(key, {})
            self._prune(values, now)
            if value in values:
                values[value] = now
                continue
            if len(values) >= cap:
                meta = self._meta_alert(envelope, label, cap, now)
                LOGGER.warning(
                    "alert cardinality cap exceeded rule=%s label=%s cap=%s",
                    envelope.alertname,
                    label,
                    cap,
                )
                raise CardinalityLimitExceeded(envelope.alertname, label, cap, meta)
            values[value] = now

    def _prune(self, values: dict[str, datetime], now: datetime) -> None:
        cutoff = now - self.window
        for value, seen_at in list(values.items()):
            if seen_at < cutoff:
                del values[value]

    def _meta_alert(self, envelope: AlertEnvelope, label: str, cap: int, now: datetime) -> AlertEnvelope:
        labels = {
            "severity": "warn",
            "area": "ops",
            "family": "shared",
            "defense_dimension": "D1",
            "offending_rule": envelope.alertname,
            "offending_label": label,
        }
        annotations = {
            "summary": f"Alert cardinality cap crossed for {envelope.alertname}",
            "description": f"Rule {envelope.alertname} emitted more than {cap} values for label {label}.",
            "runbook_url": envelope.annotations.get("runbook_url", ""),
            "remediation_hint": f"Inspect rule {envelope.alertname} label {label} before re-enabling delivery.",
        }
        critical_labels = ("offending_rule", "offending_label")
        return AlertEnvelope(
            alertname=CARDINALITY_META_ALERT,
            severity="warn",
            area="ops",
            family="shared",
            defense_dimension="D1",
            labels=labels,
            annotations=annotations,
            dedupe_key=DedupeKeyGen.generate(CARDINALITY_META_ALERT, "shared", labels, critical_labels),
            fired_at=now,
            resolved_at=None,
            critical_labels=critical_labels,
        )


class StdoutEmailAdapter:
    name = "stdout_email"

    def __init__(
        self,
        *,
        smtp: SmtpConfig | None = None,
        stream: IO[str] | None = None,
        send_fn: Callable[[SmtpConfig, EmailMessage], None] | None = None,
    ) -> None:
        self.smtp = smtp
        self.stream = stream
        self._send_fn = send_fn or _smtp_send

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        stream: IO[str] | None = None,
        send_fn: Callable[[SmtpConfig, EmailMessage], None] | None = None,
    ) -> StdoutEmailAdapter:
        e = env if env is not None else os.environ
        smtp_host = (e.get("OMNISIGHT_NOTIFIER_SMTP_HOST") or "").strip()
        recipients = _split_csv(e.get("OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS"))
        sender = (e.get("OMNISIGHT_NOTIFIER_SMTP_FROM") or "").strip()
        smtp = None
        if smtp_host and recipients and sender:
            host, port = _parse_smtp_host(smtp_host)
            smtp = SmtpConfig(
                host=host,
                port=port,
                sender=sender,
                recipients=tuple(recipients),
                username=e.get("OMNISIGHT_NOTIFIER_SMTP_USER", ""),
                password=e.get("OMNISIGHT_NOTIFIER_SMTP_PASSWORD", ""),
            )
        return cls(smtp=smtp, stream=stream, send_fn=send_fn)

    def deliver(self, envelope: AlertEnvelope) -> None:
        if envelope.resolved_at is not None and envelope.severity in {"warn", "info"}:
            return

        channels = SEVERITY_CHANNEL_MAP[envelope.severity]
        if "stdout" in channels:
            self._deliver_stdout(envelope)
        if "email" in channels:
            self._deliver_email(envelope)

    def healthcheck(self) -> AdapterHealth:
        if self.smtp is None:
            return AdapterHealth(ok=False, detail="email disabled: SMTP env vars incomplete")
        return AdapterHealth(ok=True, detail=f"stdout enabled; email enabled via {self.smtp.host}:{self.smtp.port}")

    def _deliver_stdout(self, envelope: AlertEnvelope) -> None:
        target = self.stream
        line = json.dumps(canonical_envelope(envelope), sort_keys=True, separators=(",", ":"))
        if target is None:
            print(line, flush=True)
        else:
            print(line, file=target, flush=True)

    def _deliver_email(self, envelope: AlertEnvelope) -> None:
        if self.smtp is None:
            raise RuntimeError("email delivery requested but SMTP env vars are incomplete")
        summary = envelope.annotations.get("summary", envelope.alertname)
        severity = "info" if envelope.resolved_at is not None else envelope.severity
        prefix = "[RESOLVED] " if envelope.resolved_at is not None else ""
        msg = EmailMessage()
        msg["Subject"] = f"[{severity}] {prefix}{summary}"
        msg["From"] = self.smtp.sender
        msg["To"] = ", ".join(self.smtp.recipients)
        msg.set_content(json.dumps(canonical_envelope(envelope), sort_keys=True, indent=2))
        self._send_fn(self.smtp, msg)


class AlertBridge:
    def __init__(
        self,
        adapter: ChannelAdapter,
        *,
        validator: CardinalityValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.validator = validator or CardinalityValidator()
        self._clock = clock or (lambda: datetime.now(UTC))

    def fire(
        self,
        *,
        alertname: str,
        severity: Severity,
        area: str,
        family: str,
        defense_dimension: DefenseDimension,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        critical_labels: Sequence[str],
        resolved_at: datetime | None = None,
        caps: Mapping[str, int] | None = None,
    ) -> AlertEnvelope:
        _validate_contract(severity, defense_dimension, annotations)
        dedupe_key = DedupeKeyGen.generate(alertname, family, labels, critical_labels)
        envelope = AlertEnvelope(
            alertname=alertname,
            severity=severity,
            area=area,
            family=family,
            defense_dimension=defense_dimension,
            labels=dict(labels),
            annotations=dict(annotations),
            dedupe_key=dedupe_key,
            fired_at=self._clock(),
            resolved_at=resolved_at,
            critical_labels=tuple(critical_labels),
        )
        self.validator.validate(envelope, caps=caps)
        self.adapter.deliver(envelope)
        return envelope


def canonical_envelope(envelope: AlertEnvelope | Mapping[str, object]) -> dict[str, object]:
    if isinstance(envelope, AlertEnvelope):
        return {
            "alertname": envelope.alertname,
            "severity": envelope.severity,
            "area": envelope.area,
            "family": envelope.family,
            "defense_dimension": envelope.defense_dimension,
            "labels": dict(sorted(envelope.labels.items())),
            "annotations": dict(sorted(envelope.annotations.items())),
            "dedupe_key": envelope.dedupe_key,
            "fired_at": _format_dt(envelope.fired_at),
            "resolved_at": _format_dt(envelope.resolved_at) if envelope.resolved_at else None,
            "critical_labels": list(envelope.critical_labels),
        }

    labels = dict(envelope.get("labels", {}))  # type: ignore[arg-type]
    annotations = dict(envelope.get("annotations", {}))  # type: ignore[arg-type]
    for key in ("fingerprint", "startsAt", "endsAt", "generatorURL", "receiver"):
        labels.pop(key, None)
        annotations.pop(key, None)
    return {
        "alertname": envelope["alertname"],
        "severity": envelope["severity"],
        "area": envelope["area"],
        "family": envelope["family"],
        "defense_dimension": envelope["defense_dimension"],
        "labels": dict(sorted(labels.items())),
        "annotations": dict(sorted(annotations.items())),
        "dedupe_key": envelope["dedupe_key"],
        "fired_at": envelope["fired_at"],
        "resolved_at": envelope.get("resolved_at"),
        "critical_labels": list(envelope["critical_labels"]),  # type: ignore[arg-type]
    }


def _validate_contract(severity: str, defense_dimension: str, annotations: Mapping[str, str]) -> None:
    if severity not in SEVERITY_CHANNEL_MAP:
        raise ValueError(f"unknown severity: {severity}")
    if defense_dimension not in {"D1", "D2", "D3", "D4", "D5"}:
        raise ValueError(f"unknown defense_dimension: {defense_dimension}")
    missing = [key for key in REQUIRED_ANNOTATIONS if not annotations.get(key)]
    if missing:
        raise ValueError(f"missing required annotations: {', '.join(missing)}")


def _format_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_smtp_host(raw: str) -> tuple[str, int]:
    if ":" in raw:
        host, _, port = raw.partition(":")
        return host, int(port)
    return raw, 587


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _smtp_send(smtp: SmtpConfig, msg: EmailMessage) -> None:
    with smtplib.SMTP(smtp.host, smtp.port, timeout=15) as client:
        client.ehlo()
        if smtp.use_starttls and smtp.port != 25:
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if smtp.username and smtp.password:
            client.login(smtp.username, smtp.password)
        client.send_message(msg)


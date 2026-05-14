from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
import hashlib
import io
import json

import pytest

from backend.alerting.bridge import (
    AlertBridge,
    AlertEnvelope,
    CardinalityLimitExceeded,
    CardinalityValidator,
    DedupeKeyGen,
    SmtpConfig,
    StdoutEmailAdapter,
    canonical_envelope,
)


FIXED_NOW = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)


def _annotations() -> dict[str, str]:
    return {
        "summary": "Runner claim stale",
        "description": "Runner claim has not advanced inside the expected window.",
        "runbook_url": "https://ops.example.test/runbooks/runner-claim-stale",
        "remediation_hint": "Inspect runner release-1 before requeueing the claim.",
    }


def _labels(instance: str = "runner-a") -> dict[str, str]:
    return {
        "severity": "warn",
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
        "instance": instance,
    }


def _envelope(instance: str = "runner-a", *, severity: str = "warn") -> AlertEnvelope:
    labels = _labels(instance)
    labels["severity"] = severity
    critical_labels = ("instance",)
    return AlertEnvelope(
        alertname="runner_claim_stale",
        severity=severity,  # type: ignore[arg-type]
        area="runner",
        family="10",
        defense_dimension="D1",
        labels=labels,
        annotations=_annotations(),
        dedupe_key=DedupeKeyGen.generate("runner_claim_stale", "10", labels, critical_labels),
        fired_at=FIXED_NOW,
        resolved_at=None,
        critical_labels=critical_labels,
    )


def test_dedupe_key_gen_matches_contract_formula() -> None:
    labels = _labels()
    expected = hashlib.sha256("runner_claim_stale\x1f10\x1finstance=runner-a".encode("utf-8")).hexdigest()[:16]

    assert DedupeKeyGen.generate("runner_claim_stale", "10", labels, ("instance",)) == expected


def test_cardinality_validator_rejects_more_than_ten_distinct_values_per_label() -> None:
    validator = CardinalityValidator(clock=lambda: FIXED_NOW)
    for i in range(10):
        validator.validate(_envelope(f"runner-{i}"))

    with pytest.raises(CardinalityLimitExceeded) as exc:
        validator.validate(_envelope("runner-10"))

    assert exc.value.meta_alert.alertname == "OmniSightAlertCardinalityCap"
    assert exc.value.meta_alert.labels["offending_rule"] == "runner_claim_stale"
    assert exc.value.meta_alert.labels["offending_label"] == "instance"


def test_cardinality_validator_rolls_window_before_rejecting() -> None:
    now = FIXED_NOW
    validator = CardinalityValidator(clock=lambda: now)
    for i in range(10):
        validator.validate(_envelope(f"runner-{i}"))

    now = FIXED_NOW + timedelta(hours=25)
    validator.validate(_envelope("runner-10"))


def test_stdout_email_adapter_routes_info_to_structured_stdout_only() -> None:
    stream = io.StringIO()
    sent: list[EmailMessage] = []
    adapter = StdoutEmailAdapter(
        smtp=SmtpConfig("smtp.test", 587, "alerts@example.test", ("oncall@example.test",)),
        stream=stream,
        send_fn=lambda _smtp, msg: sent.append(msg),
    )

    adapter.deliver(_envelope(severity="info"))

    payload = json.loads(stream.getvalue())
    assert payload["alertname"] == "runner_claim_stale"
    assert payload["annotations"]["summary"] == "Runner claim stale"
    assert payload["critical_labels"] == ["instance"]
    assert sent == []


def test_stdout_email_adapter_routes_warn_to_stdout_and_email_with_canonical_body() -> None:
    stream = io.StringIO()
    sent: list[EmailMessage] = []
    adapter = StdoutEmailAdapter(
        smtp=SmtpConfig("smtp.test", 2525, "alerts@example.test", ("oncall@example.test",)),
        stream=stream,
        send_fn=lambda _smtp, msg: sent.append(msg),
    )
    envelope = _envelope()

    adapter.deliver(envelope)

    assert json.loads(stream.getvalue()) == canonical_envelope(envelope)
    assert sent[0]["Subject"] == "[warn] Runner claim stale"
    assert sent[0]["From"] == "alerts@example.test"
    assert sent[0]["To"] == "oncall@example.test"
    assert json.loads(sent[0].get_content()) == canonical_envelope(envelope)


def test_stdout_email_adapter_from_env_picks_up_smtp_config() -> None:
    adapter = StdoutEmailAdapter.from_env(
        {
            "OMNISIGHT_NOTIFIER_SMTP_HOST": "smtp.example.test:2525",
            "OMNISIGHT_NOTIFIER_EMAIL_RECIPIENTS": "oncall@example.test, sre@example.test",
            "OMNISIGHT_NOTIFIER_SMTP_FROM": "alerts@example.test",
            "OMNISIGHT_NOTIFIER_SMTP_USER": "smtp-user",
            "OMNISIGHT_NOTIFIER_SMTP_PASSWORD": "smtp-pass",
        }
    )

    assert adapter.smtp is not None
    assert adapter.smtp.host == "smtp.example.test"
    assert adapter.smtp.port == 2525
    assert adapter.smtp.recipients == ("oncall@example.test", "sre@example.test")
    assert adapter.smtp.username == "smtp-user"
    assert adapter.healthcheck().ok is True


def test_adapter_swap_stdout_to_mock_am_webhook_preserves_canonical_envelope() -> None:
    class MockAlertmanagerWebhookAdapter:
        name = "mock_am_webhook"

        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def deliver(self, envelope: AlertEnvelope) -> None:
            payload = canonical_envelope(envelope)
            labels = dict(payload["labels"])  # type: ignore[arg-type]
            labels["fingerprint"] = "am-generated"
            labels["receiver"] = "omnisight-alerts"
            self.payloads.append({**payload, "labels": labels, "startsAt": payload["fired_at"], "endsAt": ""})

        def healthcheck(self):
            return None

    stdout = io.StringIO()
    stdout_bridge = AlertBridge(
        StdoutEmailAdapter(stream=stdout),
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    am = MockAlertmanagerWebhookAdapter()
    am_bridge = AlertBridge(am, validator=CardinalityValidator(clock=lambda: FIXED_NOW), clock=lambda: FIXED_NOW)

    kwargs = {
        "alertname": "runner_claim_stale",
        "severity": "info",
        "area": "runner",
        "family": "10",
        "defense_dimension": "D1",
        "labels": _labels(),
        "annotations": _annotations(),
        "critical_labels": ("instance",),
    }
    stdout_bridge.fire(**kwargs)  # type: ignore[arg-type]
    am_bridge.fire(**kwargs)  # type: ignore[arg-type]

    assert json.loads(stdout.getvalue()) == canonical_envelope(am.payloads[0])


def test_synthetic_alert_fires_through_bridge_preserving_contract_fields() -> None:
    stream = io.StringIO()
    sent: list[EmailMessage] = []
    bridge = AlertBridge(
        StdoutEmailAdapter(
            smtp=SmtpConfig("smtp.test", 587, "alerts@example.test", ("oncall@example.test",)),
            stream=stream,
            send_fn=lambda _smtp, msg: sent.append(msg),
        ),
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )

    envelope = bridge.fire(
        alertname="runner_claim_stale",
        severity="warn",
        area="runner",
        family="10",
        defense_dimension="D1",
        labels=_labels(),
        annotations=_annotations(),
        critical_labels=("instance",),
    )

    payload = json.loads(stream.getvalue())
    assert payload == canonical_envelope(envelope)
    assert payload["alertname"] == "runner_claim_stale"
    assert payload["severity"] == "warn"
    assert payload["area"] == "runner"
    assert payload["family"] == "10"
    assert payload["defense_dimension"] == "D1"
    assert set(payload["annotations"]) == {"summary", "description", "runbook_url", "remediation_hint"}
    assert payload["dedupe_key"] == envelope.dedupe_key
    assert json.loads(sent[0].get_content()) == payload


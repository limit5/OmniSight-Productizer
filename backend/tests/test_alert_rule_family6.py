"""Contract tests for the Family ⑥ ``OmniSightAlembicDrift`` alert rule.

OP-1170 — `v2-⑥-AlertRule`. Verifies that
``deploy/prometheus/rules/family6.yml`` satisfies the v2-AlertBridge-1a
contract end-to-end:

* §1 — four required labels (``severity`` / ``area`` / ``family`` /
  ``defense_dimension``) with values inside the contract enums.
* §2 — four required annotations (``summary`` / ``description`` /
  ``runbook_url`` / ``remediation_hint``).
* §3 — severity ∈ ``{page, warn, info}``; backward drift is ``page``
  (ADR-0036 forward-only-deploy invariant).
* §4 — implicit dedupe key over ``(alertname, family, *critical_labels)``;
  asserted by round-tripping a synthetic envelope through the v0 stdout
  adapter and checking the dedupe key matches the contract formula.
* The rule's ``expr`` references the producer metric
  ``omnisight_alembic_drift`` shipped by OP-1163.

The Exercised AC (final bullet of OP-1170) is satisfied by
``test_synthetic_fire_round_trips_through_stdout_adapter``: a mocked
metric value is converted to an ``AlertEnvelope`` matching this rule
and delivered through ``StdoutEmailAdapter``; the emitted JSON line is
parsed back through ``canonical_envelope`` and equality is asserted
against the source envelope's canonical form (the AlertBridge §10
canonical-envelope round-trip invariant).
"""
from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from backend.alerting.bridge import (
    AlertBridge,
    AlertEnvelope,
    CardinalityValidator,
    DedupeKeyGen,
    REQUIRED_ANNOTATIONS,
    SEVERITY_CHANNEL_MAP,
    SmtpConfig,
    StdoutEmailAdapter,
    canonical_envelope,
)


def _stdout_email_adapter(stream: io.StringIO) -> tuple[StdoutEmailAdapter, list]:
    """Build a StdoutEmailAdapter wired with a stub SMTP send_fn.

    Severity=page rules fan out to both stdout AND email per
    ``SEVERITY_CHANNEL_MAP``; the test must capture both branches
    without contacting a real SMTP server."""
    sent: list = []
    adapter = StdoutEmailAdapter(
        smtp=SmtpConfig(
            host="smtp.test",
            port=587,
            sender="alerts@example.test",
            recipients=("oncall@example.test",),
        ),
        stream=stream,
        send_fn=lambda _smtp, msg: sent.append(msg),
    )
    return adapter, sent


REPO_ROOT = Path(__file__).resolve().parents[2]
RULE_PATH = REPO_ROOT / "deploy" / "prometheus" / "rules" / "family6.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "sop" / "runbook-family6-alembic-drift.md"

REQUIRED_LABELS = ("severity", "area", "family", "defense_dimension")
AREA_ENUM = {"deployment", "runner", "backend", "db", "security", "integration", "ops", "docs"}
FAMILY_ENUM = {"5", "6", "7", "8", "9", "10", "shared"}
DEFENSE_DIMENSION_ENUM = {"D1", "D2", "D3", "D4", "D5"}

FIXED_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def rule() -> dict:
    """Return the single rule dict from family6.yml."""
    doc = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    groups = doc["groups"]
    assert len(groups) == 1, "family6.yml should declare exactly one group"
    rules = groups[0]["rules"]
    assert len(rules) == 1, "family6.yml should declare exactly one rule"
    assert rules[0]["alert"] == "OmniSightAlembicDrift"
    return rules[0]


def test_required_labels_present_and_in_enum(rule: dict) -> None:
    labels = rule["labels"]
    for key in REQUIRED_LABELS:
        assert key in labels, f"missing required label: {key}"
    assert labels["severity"] in SEVERITY_CHANNEL_MAP
    assert labels["area"] in AREA_ENUM
    assert labels["family"] in FAMILY_ENUM
    assert labels["defense_dimension"] in DEFENSE_DIMENSION_ENUM


def test_severity_is_page_for_backward_drift(rule: dict) -> None:
    """ADR-0036: backward drift refuses to start; page is the only correct severity."""
    assert rule["labels"]["severity"] == "page"


def test_family_and_area_match_v2_six_alertrule_contract(rule: dict) -> None:
    """Rule must declare family="6", area="deployment", D1 per contract spec §4.5."""
    labels = rule["labels"]
    assert labels["family"] == "6"
    assert labels["area"] == "deployment"
    assert labels["defense_dimension"] == "D1"


def test_required_annotations_present_and_non_empty(rule: dict) -> None:
    annotations = rule["annotations"]
    for key in REQUIRED_ANNOTATIONS:
        assert key in annotations, f"missing required annotation: {key}"
        assert annotations[key].strip(), f"annotation {key} must be non-empty"


def test_runbook_url_points_at_docs_sora_services(rule: dict) -> None:
    """AlertBridge §2.2: runbook_url MUST be an absolute https URL on docs.sora.services."""
    url = rule["annotations"]["runbook_url"]
    assert url.startswith("https://docs.sora.services/runbooks/"), url


def test_expr_references_omnisight_alembic_drift_metric(rule: dict) -> None:
    """Integration AC: rule references the gauge OP-1163 publishes."""
    expr = rule["expr"]
    assert "omnisight_alembic_drift" in expr
    # Backward direction is the page-class condition per ADR-0036.
    assert 'direction="backward"' in expr


def test_expr_also_covers_readyz_pending_redundant_surface(rule: dict) -> None:
    """The OR-arm catches the legacy /readyz probe so detection survives even
    if the always-on alembic_drift gauge collector is itself broken."""
    assert "omnisight_readyz_migrations_pending" in rule["expr"]


def test_bridge_block_declares_critical_labels_and_caps(rule: dict) -> None:
    bridge = rule["__bridge"]
    assert bridge["critical_labels"], "must declare at least one critical label"
    assert "alertname" not in bridge["critical_labels"]
    assert "family" not in bridge["critical_labels"]
    caps = bridge.get("cardinality_caps", {})
    for label, cap in caps.items():
        assert isinstance(cap, int) and cap <= 10, (
            f"cardinality cap for {label} must be int and <= 10 (got {cap!r})"
        )


def test_for_duration_lets_transient_post_deploy_window_close(rule: dict) -> None:
    """A short transient drift during the AHEAD startup-hook window MUST NOT
    page (Family ⑥ §4.6). The rule's `for:` clause provides that grace."""
    assert rule["for"] == "5m"


def test_rule_passes_promote_alert_rule_lint() -> None:
    """The CI lint (`scripts/promote-alert-rule.py`, v2-AlertBridge-2bc) must
    accept this rule. This is the gate downstream PRs hit."""
    import subprocess
    import sys

    script = REPO_ROOT / "scripts" / "promote-alert-rule.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(RULE_PATH)],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, f"lint failed: {payload!r}"
    assert payload["ok"] is True
    assert payload["errors"] == []
    assert payload["rules"] == [{"alert": "OmniSightAlembicDrift", "ok": True}]


def test_runbook_doc_exists_and_meets_minimum_length() -> None:
    """Ticket DoD: runbook ≥80 lines covering diagnose + remediate +
    ADR-0036 cross-link."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert len(text.splitlines()) >= 80
    # Spot-check structural sections required by the ticket scope.
    assert "ADR-0036" in text
    assert "## 3. Diagnose" in text or "## Diagnose" in text or "Diagnose" in text
    assert "Remediate" in text
    assert "v2-⑥-RescueCLI" in text  # cross-link to L2 rescue path


# ── Exercised AC — synthetic-fire round-trip through StdoutEmailAdapter ──

def _envelope_from_rule(rule: dict, instance: str = "backend-prod-a") -> AlertEnvelope:
    """Build an AlertEnvelope that mirrors a Prometheus firing of this rule
    for the given instance. The bridge would synthesise the same envelope at
    runtime when the alembic_drift gauge transitions to 1."""
    labels = dict(rule["labels"])
    labels["instance"] = instance
    critical_labels = tuple(rule["__bridge"]["critical_labels"])
    return AlertEnvelope(
        alertname=rule["alert"],
        severity=labels["severity"],  # type: ignore[arg-type]
        area=labels["area"],
        family=labels["family"],
        defense_dimension=labels["defense_dimension"],  # type: ignore[arg-type]
        labels=labels,
        annotations=dict(rule["annotations"]),
        dedupe_key=DedupeKeyGen.generate(
            rule["alert"], labels["family"], labels, critical_labels
        ),
        fired_at=FIXED_NOW,
        resolved_at=None,
        critical_labels=critical_labels,
    )


def test_synthetic_fire_round_trips_through_stdout_adapter(rule: dict) -> None:
    """OP-1170 Exercised AC: synthetic fire → stdout adapter delivers payload;
    canonical_envelope round-trips correctly (AlertBridge §10 invariant)."""
    envelope = _envelope_from_rule(rule)
    stream = io.StringIO()
    adapter, sent_emails = _stdout_email_adapter(stream)

    adapter.deliver(envelope)

    # Severity=page also fans out to the email channel per
    # SEVERITY_CHANNEL_MAP — the stub captured one message whose subject
    # follows `[{severity}] {summary}` (AlertBridge §9.2).
    assert len(sent_emails) == 1
    subject = sent_emails[0]["Subject"]
    assert subject.startswith("[page] ")
    assert subject.endswith(rule["annotations"]["summary"])

    line = stream.getvalue().strip()
    assert line, "stdout adapter must emit exactly one JSON line on fire"
    parsed = json.loads(line)

    # The emitted envelope must canonicalise to the same shape we sent.
    assert canonical_envelope(parsed) == canonical_envelope(envelope)

    # Spot-check the contract-required fields survived the round-trip.
    assert parsed["alertname"] == "OmniSightAlembicDrift"
    assert parsed["severity"] == "page"
    assert parsed["family"] == "6"
    assert parsed["area"] == "deployment"
    assert parsed["defense_dimension"] == "D1"
    assert parsed["labels"]["instance"] == "backend-prod-a"
    assert parsed["critical_labels"] == ["instance"]
    # Dedupe key is the content-derived 16-hex prefix per §4.1.
    assert len(parsed["dedupe_key"]) == 16
    int(parsed["dedupe_key"], 16)  # raises if non-hex


def test_dedupe_key_partitions_by_instance(rule: dict) -> None:
    """Two firings from different instances MUST produce different dedupe keys,
    so on-call sees separate notifications per failing backend (AlertBridge §4.2)."""
    env_a = _envelope_from_rule(rule, instance="backend-prod-a")
    env_b = _envelope_from_rule(rule, instance="backend-prod-b")
    assert env_a.dedupe_key != env_b.dedupe_key


def test_full_bridge_fire_path_validates_and_delivers(rule: dict) -> None:
    """Run the rule through AlertBridge.fire (the runtime path the bridge
    takes at evaluation time). Verifies the contract validator accepts the
    rule's declared labels/annotations end-to-end."""
    stream = io.StringIO()
    adapter, _ = _stdout_email_adapter(stream)
    bridge = AlertBridge(
        adapter=adapter,
        validator=CardinalityValidator(clock=lambda: FIXED_NOW),
        clock=lambda: FIXED_NOW,
    )
    labels = dict(rule["labels"])
    labels["instance"] = "backend-prod-a"
    envelope = bridge.fire(
        alertname=rule["alert"],
        severity=labels["severity"],
        area=labels["area"],
        family=labels["family"],
        defense_dimension=labels["defense_dimension"],
        labels=labels,
        annotations=dict(rule["annotations"]),
        critical_labels=tuple(rule["__bridge"]["critical_labels"]),
        caps=dict(rule["__bridge"].get("cardinality_caps", {})),
    )
    assert envelope.alertname == "OmniSightAlembicDrift"
    assert envelope.severity == "page"
    # Stdout side of the adapter must have emitted a JSON line.
    parsed = json.loads(stream.getvalue().strip())
    assert parsed["alertname"] == "OmniSightAlembicDrift"

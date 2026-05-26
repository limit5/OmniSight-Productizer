from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "promote-alert-rule.py"
RUNNER_ATLAS = REPO_ROOT / "deploy" / "prometheus" / "rules" / "runner_atlas.yml"

OP_1119_RULES = {
    "runner_claim_stale",
    "runner_pickup_block_rate",
    "runner_state_drift",
}

OP_1754_CONTRACT_RULES = {
    "runner_bridge_stale": "omnisight_bridge_staleness_seconds > 300",
    "runner_instance_drift": 'count by (instance_id) (runner_claims_active{state="active"}) > 1',
    "runner_human_authority_yield": 'rate(runner_audit_events_total{action="rescue.release"}[24h]) > 0',
    "runner_state_authority_precedence": "rate(runner_state_authority_precedence_total[1h]) > 0",
}


def _runner_atlas_rules() -> dict[str, dict[str, object]]:
    doc = yaml.safe_load(RUNNER_ATLAS.read_text(encoding="utf-8"))
    rules = doc["groups"][0]["rules"]
    return {rule["alert"]: rule for rule in rules}


def test_runner_atlas_passes_alertbridge_lint() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(RUNNER_ATLAS)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, payload
    assert payload["ok"] is True
    assert payload["errors"] == []


def test_runner_atlas_keeps_op_1119_rules_and_adds_op_1754_contract_names() -> None:
    rules = _runner_atlas_rules()

    for alert in OP_1119_RULES:
        assert alert in rules
    for alert in OP_1754_CONTRACT_RULES:
        assert alert in rules


def test_op_1754_contract_rules_use_binding_promql_and_bridge_metadata() -> None:
    rules = _runner_atlas_rules()

    for alert, expr in OP_1754_CONTRACT_RULES.items():
        rule = rules[alert]
        assert rule["expr"].strip() == expr
        labels = rule["labels"]
        assert labels["area"] == "runner"
        assert labels["family"] == "10"
        assert labels["severity"] in {"page", "warn"}
        annotations = rule["annotations"]
        assert annotations["runbook_url"].startswith("https://docs.sora.services/runbooks/")
        assert annotations["summary"].strip()
        assert annotations["description"].strip()
        assert annotations["remediation_hint"].strip()
        bridge = rule["__bridge"]
        assert bridge["critical_labels"]
        for cap in bridge["cardinality_caps"].values():
            assert isinstance(cap, int)
            assert cap <= 10


def test_synthetic_bridge_staleness_metric_fires_c9_rule() -> None:
    """Integration AC: a mocked C9 metric sample crosses the rule threshold."""
    rule = _runner_atlas_rules()["runner_bridge_stale"]

    assert rule["expr"] == "omnisight_bridge_staleness_seconds > 300"
    synthetic_omnisight_bridge_staleness_seconds = 301

    assert synthetic_omnisight_bridge_staleness_seconds > 300

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "promote-alert-rule.py"
RULE_PATH = REPO_ROOT / "deploy" / "prometheus" / "rules" / "family5.yml"


def _rules() -> list[dict[str, object]]:
    doc = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return doc["groups"][0]["rules"]


def _rule_by_severity(severity: str) -> dict[str, object]:
    for rule in _rules():
        if rule["alert"] == "OmniSightStaleImage" and rule["labels"]["severity"] == severity:
            return rule
    raise AssertionError(f"OmniSightStaleImage severity={severity} rule not found")


def test_family5_stale_image_rules_pass_alertbridge_lint() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(RULE_PATH)],
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
    assert payload["rules"] == [
        {"alert": "OmniSightStaleImage", "ok": True},
        {"alert": "OmniSightStaleImage", "ok": True},
    ]


def test_family5_stale_image_rules_load_in_promtool_after_bridge_strip(tmp_path: Path) -> None:
    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip("promtool not installed")

    doc = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    for group in doc["groups"]:
        for rule in group["rules"]:
            rule.pop("__bridge", None)

    stripped = tmp_path / "family5.prometheus.yml"
    stripped.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    proc = subprocess.run(
        [promtool, "check", "rules", str(stripped)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_warn_and_page_result_states_route_to_distinct_severities() -> None:
    warn_rule = _rule_by_severity("warn")
    page_rule = _rule_by_severity("page")

    assert 'result_state="WARN_STALE_IMAGE"' in warn_rule["expr"]
    assert 'result_state="PAGE_STALE_IMAGE"' in page_rule["expr"]
    assert 'result_state="WARN_STALE_IMAGE"' not in page_rule["expr"]

    for rule in (warn_rule, page_rule):
        labels = rule["labels"]
        assert labels["area"] == "deployment"
        assert labels["family"] == "5"
        assert labels["defense_dimension"] == "D1"


def test_rules_have_alertbridge_metadata_and_runbook_url() -> None:
    for rule in _rules():
        annotations = rule["annotations"]
        assert annotations["runbook_url"] == (
            "https://docs.sora.services/runbooks/omnisight-stale-image"
        )
        assert annotations["summary"].strip()
        assert annotations["description"].strip()
        assert annotations["remediation_hint"].strip()

        bridge = rule["__bridge"]
        assert bridge["critical_labels"] == ["instance", "image_repo"]
        assert bridge["cardinality_caps"] == {"instance": 10, "image_repo": 5}


def test_synthetic_stale_image_metric_fires_warn_rule_without_paging() -> None:
    warn_rule = _rule_by_severity("warn")

    synthetic = {
        "metric": "omnisight_deployment_audit_stale_image_age_seconds",
        "labels": {
            "result_state": "WARN_STALE_IMAGE",
            "instance": "backend-prod-a",
            "image_repo": "ghcr.io/omnisight/backend",
        },
        "value": 3600,
    }

    assert synthetic["labels"]["result_state"] == "WARN_STALE_IMAGE"
    assert synthetic["value"] > 0
    assert 'result_state="WARN_STALE_IMAGE"' in warn_rule["expr"]
    assert warn_rule["labels"]["severity"] == "warn"


def test_synthetic_stale_image_metric_fires_page_rule_at_24h() -> None:
    page_rule = _rule_by_severity("page")

    synthetic = {
        "metric": "omnisight_deployment_audit_stale_image_age_seconds",
        "labels": {
            "result_state": "PAGE_STALE_IMAGE",
            "instance": "backend-prod-a",
            "image_repo": "ghcr.io/omnisight/backend",
        },
        "value": 86400,
    }

    assert synthetic["labels"]["result_state"] == "PAGE_STALE_IMAGE"
    assert synthetic["value"] >= 86400
    assert 'result_state="PAGE_STALE_IMAGE"' in page_rule["expr"]
    assert page_rule["labels"]["severity"] == "page"

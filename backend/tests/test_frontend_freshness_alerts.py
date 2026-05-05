"""BP.W3.14 — frontend freshness Prometheus alert rule contract."""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERTS = PROJECT_ROOT / "deploy" / "observability" / "prometheus" / "frontend-freshness-alerts.yml"


def test_frontend_freshness_alert_rule_pins_threshold() -> None:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    rules = doc["groups"][0]["rules"]
    rule = next(r for r in rules if r.get("alert") == "OmniSightFrontendBuildLagHigh")

    assert rule["expr"] == "omnisight_frontend_build_lag_commits >= 10"
    assert rule["for"] == "5m"
    assert rule["labels"]["severity"] == "critical"
    assert rule["labels"]["subsystem"] == "frontend_deploy"

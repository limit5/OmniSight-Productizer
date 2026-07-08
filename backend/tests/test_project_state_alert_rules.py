"""OP-2560 — project-state anti-hollow alert rule contracts.

Pure-Python checks only: promtool is intentionally not required in this
environment. The tests pin the loadable Prometheus surface, prod mount,
specific ``rule_files`` entry, and Grafana threshold alignment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES = (
    PROJECT_ROOT
    / "deploy"
    / "observability"
    / "prometheus"
    / "project_state_health.yml"
)
BRIDGE_RULES_DIR = PROJECT_ROOT / "deploy" / "prometheus" / "rules"
PROMETHEUS_CONFIG = PROJECT_ROOT / "configs" / "prometheus.yml"
COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"
DASHBOARD = (
    PROJECT_ROOT
    / "deploy"
    / "observability"
    / "grafana"
    / "project_state_health.json"
)

EXPECTED_RULES = {
    "ProjectStateStructuralHollow": {
        "threshold": 0.8,
        "operator": "<",
        "severity": "warning",
        "for": "15m",
    },
    "ProjectStateHalfHollow": {
        "threshold": 0.8,
        "operator": ">",
        "severity": "info",
        "for": "30m",
    },
    "ProjectStateNoTraffic": {
        "threshold": None,
        "operator": "absent",
        "severity": "info",
        "for": None,
    },
    "ProjectStateLatencyHigh": {
        "threshold": 2,
        "operator": ">",
        "severity": "warning",
        "for": None,
    },
}


def _rules_doc() -> dict[str, Any]:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))


def _all_alerts() -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for group in _rules_doc().get("groups", []):
        for rule in group.get("rules", []):
            if "alert" in rule:
                alerts.append(rule)
    return alerts


def _rule(name: str) -> dict[str, Any]:
    for rule in _all_alerts():
        if rule.get("alert") == name:
            return rule
    raise AssertionError(f"alert {name!r} not found")


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _panels_with_expr(metric: str) -> list[dict[str, Any]]:
    panels = []
    for panel in _dashboard().get("panels", []):
        exprs = [target.get("expr", "") for target in panel.get("targets", [])]
        if any(metric in expr for expr in exprs):
            panels.append(panel)
    return panels


def _threshold_values(panel: dict[str, Any]) -> set[float]:
    steps = (
        panel.get("fieldConfig", {})
        .get("defaults", {})
        .get("thresholds", {})
        .get("steps", [])
    )
    return {step["value"] for step in steps if step.get("value") is not None}


def test_rule_file_uses_loadable_plain_prometheus_surface() -> None:
    assert RULES.exists()
    assert RULES.relative_to(PROJECT_ROOT) == Path(
        "deploy/observability/prometheus/project_state_health.yml"
    )
    assert not (BRIDGE_RULES_DIR / "project_state_health.yml").exists()


def test_rule_file_has_no_bridge_or_alertmanager_shape() -> None:
    text = RULES.read_text(encoding="utf-8")
    doc = _rules_doc()

    assert "__bridge" not in text
    assert "alerting" not in doc
    assert "route" not in doc


def test_exact_alert_names_and_labels_are_pinned() -> None:
    alerts = _all_alerts()

    assert [rule["alert"] for rule in alerts] == list(EXPECTED_RULES)
    for name, expected in EXPECTED_RULES.items():
        rule = _rule(name)
        assert rule["labels"]["severity"] == expected["severity"]
        if expected["for"] is None:
            assert "for" not in rule
        else:
            assert rule["for"] == expected["for"]


def test_alert_exprs_match_exact_ratio_and_absence_contract() -> None:
    assert (
        _rule("ProjectStateStructuralHollow")["expr"]
        == 'sum(rate(omnisight_project_state_axis_total{axis="structural",content="non_empty"}[15m])) / sum(rate(omnisight_project_state_axis_total{axis="structural"}[15m])) < 0.8'
    )
    assert (
        _rule("ProjectStateHalfHollow")["expr"]
        == 'sum by (half)(rate(omnisight_project_state_structural_half_total{useful="false"}[30m])) / sum by (half)(rate(omnisight_project_state_structural_half_total[30m])) > 0.8'
    )
    assert (
        _rule("ProjectStateNoTraffic")["expr"]
        == "absent(rate(omnisight_project_state_calls_total[30m]))"
    )
    assert (
        _rule("ProjectStateLatencyHigh")["expr"]
        == "histogram_quantile(0.95, sum(rate(omnisight_project_state_axis_latency_seconds_bucket[5m])) by (le)) > 2"
    )


def test_alert_exprs_are_ratio_or_absence_only() -> None:
    for rule in _all_alerts():
        expr = rule["expr"]
        if rule["alert"] == "ProjectStateNoTraffic":
            assert expr.startswith("absent(")
            continue

        assert "/" in expr or "histogram_quantile(" in expr
        assert not re.search(r"\bcount\s*\(", expr)
        assert "== 0" not in expr


def test_prod_prometheus_mounts_observability_rule_directory() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    prometheus = compose["services"]["prometheus"]

    assert (
        "./deploy/observability/prometheus:/etc/prometheus/obs-rules:ro"
        in prometheus["volumes"]
    )


def test_prometheus_config_loads_specific_file_without_glob_or_alerting() -> None:
    config = yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8"))

    assert config["rule_files"] == [
        "/etc/prometheus/obs-rules/project_state_health.yml"
    ]
    assert not any("*" in entry for entry in config["rule_files"])
    assert "alerting" not in config


def test_dashboard_exists_and_contains_required_project_state_queries() -> None:
    assert DASHBOARD.exists()
    dashboard = _dashboard()
    exprs = [
        target.get("expr", "")
        for panel in dashboard.get("panels", [])
        for target in panel.get("targets", [])
    ]

    assert any("omnisight_project_state_axis_total" in expr for expr in exprs)
    assert any(
        "omnisight_project_state_structural_half_total" in expr for expr in exprs
    )
    assert any(
        "omnisight_project_state_axis_latency_seconds_bucket" in expr
        for expr in exprs
    )
    assert any("omnisight_project_state_calls_total" in expr for expr in exprs)


def test_dashboard_thresholds_match_alert_thresholds() -> None:
    structural_panels = _panels_with_expr("omnisight_project_state_axis_total")
    half_panels = _panels_with_expr("omnisight_project_state_structural_half_total")
    latency_panels = _panels_with_expr(
        "omnisight_project_state_axis_latency_seconds_bucket"
    )

    assert any(0.8 in _threshold_values(panel) for panel in structural_panels)
    assert any(0.8 in _threshold_values(panel) for panel in half_panels)
    assert any(2 in _threshold_values(panel) for panel in latency_panels)

"""OP-2563 — Alertmanager deployment + alert-routing structure contracts.

Offline / no-docker by design: the runner sandbox has no docker-in-docker,
so Code-AC green is pure config-correctness — parse the committed configs
and pin their shape. ``amtool`` / ``promtool`` validation runs only when
the binary happens to be on PATH (skip, never fail, when absent).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALERTMANAGER_CONFIG = PROJECT_ROOT / "configs" / "alertmanager.yml"
PROMETHEUS_CONFIG = PROJECT_ROOT / "configs" / "prometheus.yml"
COMPOSE = PROJECT_ROOT / "docker-compose.prod.yml"

SECRET_PATTERNS = [
    # Real webhook/endpoint URLs (placeholders like <YOUR-...> don't match).
    re.compile(r"https?://(?!<)[\w.-]+"),
    re.compile(r"hooks\.slack\.com"),
    re.compile(r"xox[baprs]-"),  # Slack token prefixes
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r"bearer\s+\S", re.IGNORECASE),
]


def _alertmanager_doc() -> dict[str, Any]:
    return yaml.safe_load(ALERTMANAGER_CONFIG.read_text(encoding="utf-8"))


def _prometheus_doc() -> dict[str, Any]:
    return yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8"))


def _compose_doc() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# ── (a) alertmanager.yml route tree + secret-free receivers ─────────────


def test_route_tree_has_default_receiver_and_critical_child_route() -> None:
    route = _alertmanager_doc()["route"]

    assert route["receiver"] == "default"
    assert route["group_by"] == ["alertname", "severity"]
    assert route["group_wait"] == "30s"
    assert route["group_interval"] == "5m"
    assert route["repeat_interval"] == "4h"

    critical_routes = [
        child
        for child in route.get("routes", [])
        if 'severity="critical"' in child.get("matchers", [])
    ]
    assert len(critical_routes) == 1
    assert critical_routes[0]["receiver"] == "critical"


def test_both_receivers_exist_as_name_only_blackholes() -> None:
    receivers = _alertmanager_doc()["receivers"]
    by_name = {receiver["name"]: receiver for receiver in receivers}

    assert set(by_name) == {"default", "critical"}
    for receiver in by_name.values():
        # Name-only blackhole: any integration block means a channel was
        # committed instead of operator-supplied (runbook activation path).
        assert set(receiver) == {"name"}


def test_alertmanager_config_carries_no_committed_secret_or_url() -> None:
    text = ALERTMANAGER_CONFIG.read_text(encoding="utf-8")
    live_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    live_text = "\n".join(live_lines)

    for pattern in SECRET_PATTERNS:
        assert not pattern.search(live_text), f"matched {pattern.pattern!r}"


# ── (b) prometheus.yml alerting block + untouched S4 surface ────────────


def test_prometheus_alerting_block_targets_alertmanager() -> None:
    alerting = _prometheus_doc()["alerting"]

    targets = [
        target
        for alertmanager in alerting["alertmanagers"]
        for static in alertmanager["static_configs"]
        for target in static["targets"]
    ]
    assert targets == ["alertmanager:9093"]


def test_prometheus_existing_blocks_are_untouched() -> None:
    config = _prometheus_doc()

    assert config["global"] == {
        "scrape_interval": "15s",
        "evaluation_interval": "15s",
    }
    assert config["rule_files"] == [
        "/etc/prometheus/obs-rules/project_state_health.yml"
    ]
    assert config["scrape_configs"] == [
        {
            "job_name": "omnisight-backend",
            "metrics_path": "/api/v1/metrics",
            "static_configs": [
                {"targets": ["backend-a:8000", "backend-b:8001"]}
            ],
        }
    ]


# ── compose service shape ────────────────────────────────────────────────


def test_compose_alertmanager_service_shape() -> None:
    compose = _compose_doc()
    alertmanager = compose["services"]["alertmanager"]

    assert alertmanager["image"] == "prom/alertmanager:v0.27.0"
    assert alertmanager["profiles"] == ["observability"]
    assert "9093:9093" in alertmanager["ports"]
    assert alertmanager["restart"] == "unless-stopped"
    assert (
        "./configs/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro"
        in alertmanager["volumes"]
    )
    assert "omnisight-alertmanager:/alertmanager" in alertmanager["volumes"]
    assert alertmanager["command"] == [
        "--config.file=/etc/alertmanager/alertmanager.yml",
        "--storage.path=/alertmanager",
    ]
    assert alertmanager["healthcheck"]["test"] == [
        "CMD", "wget", "-q", "--spider", "-T", "3",
        "http://127.0.0.1:9093/-/healthy",
    ]
    assert compose["volumes"]["omnisight-alertmanager"] == {"driver": "local"}

    prometheus = compose["services"]["prometheus"]
    assert prometheus["depends_on"]["alertmanager"] == {
        "condition": "service_healthy"
    }


# ── (c) optional binary validation — SKIP when absent ───────────────────


@pytest.mark.skipif(
    shutil.which("amtool") is None, reason="amtool not on PATH"
)
def test_amtool_check_config_passes() -> None:
    result = subprocess.run(
        ["amtool", "check-config", str(ALERTMANAGER_CONFIG)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(
    shutil.which("promtool") is None, reason="promtool not on PATH"
)
def test_promtool_check_config_passes() -> None:
    result = subprocess.run(
        ["promtool", "check", "config", str(PROMETHEUS_CONFIG)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

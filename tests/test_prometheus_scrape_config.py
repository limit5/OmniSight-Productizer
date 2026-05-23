"""OP-1635 - Prometheus scrapes both production backend replicas."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMETHEUS_CONFIG = REPO_ROOT / "configs" / "prometheus.yml"


def _prometheus_config() -> dict:
    return yaml.safe_load(PROMETHEUS_CONFIG.read_text(encoding="utf-8"))


def test_backend_scrape_job_targets_both_replicas_with_correct_ports() -> None:
    config = _prometheus_config()
    jobs = {
        job["job_name"]: job
        for job in config["scrape_configs"]
    }

    backend_job = jobs["omnisight-backend"]
    targets = [
        target
        for static_config in backend_job["static_configs"]
        for target in static_config["targets"]
    ]

    assert targets == ["backend-a:8000", "backend-b:8001"]

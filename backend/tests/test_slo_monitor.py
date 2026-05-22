"""OP-772 — SLO monitor and auto-rollback regression tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from backend.slo_monitor import (
    APP_SERVICES,
    ComposeRollbackExecutor,
    ConsecutiveBreachDetector,
    RouteWindow,
    SloMonitor,
    image_refs_for_tag,
    load_slo_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SLO_CONFIG = PROJECT_ROOT / "config" / "slos.yaml"
SYSTEMD_SERVICE = PROJECT_ROOT / "deploy" / "systemd" / "omnisight-slo-monitor.service"
DEPLOY_PROD = PROJECT_ROOT / "scripts" / "deploy-prod.sh"
TEST_REGISTRY = "sora.services:49160/omnisight/omnisight-productizer"


class _Source:
    def __init__(self, windows: list[list[RouteWindow]]) -> None:
        self.windows = list(windows)
        self.calls = 0

    def fetch(self, window_seconds: int):
        self.calls += 1
        return self.windows.pop(0)


class _Rollback:
    def __init__(self):
        self.calls = 0

    def rollback(self, timeout_seconds: int):
        from backend.slo_monitor import RollbackResult

        self.calls += 1
        return RollbackResult("rolled_back", "v1.2.2", 42.0, "ok")


def test_slo_definitions_in_config_match_op_772_thresholds() -> None:
    cfg = load_slo_config(SLO_CONFIG)

    assert cfg.interval_seconds == 30
    assert cfg.post_deploy_monitor_seconds == 3600
    assert cfg.breach_consecutive_windows == 3
    assert cfg.rollback_max_seconds == 300
    assert cfg.error_budget_seconds == 3600
    default = cfg.routes["*"]
    assert default.error_rate_lt == 0.005
    assert default.p95_latency_ms_lt == 500
    assert default.success_rate_gt == 0.995


def test_monitor_systemd_worker_runs_persistent_30s_loop() -> None:
    text = SYSTEMD_SERVICE.read_text(encoding="utf-8")
    cfg = yaml.safe_load(SLO_CONFIG.read_text(encoding="utf-8"))

    assert "Type=simple" in text
    assert "Restart=always" in text
    assert "python -m backend.slo_monitor" in text
    assert cfg["interval_seconds"] == 30


def test_detector_requires_three_consecutive_30s_windows() -> None:
    cfg = load_slo_config(SLO_CONFIG)
    detector = ConsecutiveBreachDetector(cfg)
    bad = RouteWindow(route="/api/workflows", error_rate=0.006, p95_latency_ms=200, success_rate=0.999)

    assert detector.observe([bad]) == []
    assert detector.observe([bad]) == []
    breaches = detector.observe([bad])

    assert len(breaches) == 1
    assert breaches[0].route == "/api/workflows"
    assert breaches[0].metric == "error_rate"
    assert breaches[0].windows == 3


def test_synthetic_regression_rolls_back_after_three_windows(monkeypatch) -> None:
    cfg = load_slo_config(SLO_CONFIG)
    bad = RouteWindow(route="/api/workflows", error_rate=0.006, p95_latency_ms=510, success_rate=0.994)
    source = _Source([[bad], [bad], [bad]])
    rollback = _Rollback()
    notified: list[str] = []

    async def fake_breach(breach):
        notified.append(f"breach:{breach.metric}")

    async def fake_rollback(result):
        notified.append(f"rollback:{result.status}")

    monkeypatch.setattr("backend.slo_monitor.notify_slo_breach", fake_breach)
    monkeypatch.setattr("backend.slo_monitor.notify_rollback", fake_rollback)

    result = SloMonitor(
        config=cfg,
        source=source,
        rollback=rollback,
        sleeper=lambda _seconds: None,
    ).run_for(120)

    assert result is not None
    assert result.status == "rolled_back"
    assert rollback.calls == 1
    assert notified == ["breach:error_rate", "rollback:rolled_back"]


def test_compose_rollback_checks_previous_images_before_redeploy() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(args, *, env=None, timeout=None):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    rollback = ComposeRollbackExecutor(
        previous_tag="v1.2.2",
        namespace="acme",
        runner=runner,
        env={"OMNISIGHT_REGISTRY": TEST_REGISTRY},
    )

    result = rollback.rollback(timeout_seconds=300)

    assert result.status == "rolled_back"
    assert image_refs_for_tag(
        "acme",
        "v1.2.2",
        env={"OMNISIGHT_REGISTRY": TEST_REGISTRY},
    ) == (
        f"{TEST_REGISTRY}/backend:v1.2.2",
        f"{TEST_REGISTRY}/frontend:v1.2.2",
    )
    assert calls[0] == ("docker", "manifest", "inspect", f"{TEST_REGISTRY}/backend:v1.2.2")
    assert calls[1] == ("docker", "manifest", "inspect", f"{TEST_REGISTRY}/frontend:v1.2.2")
    assert calls[2][-len(APP_SERVICES):] == APP_SERVICES
    assert calls[3][-len(APP_SERVICES):] == APP_SERVICES


def test_compose_rollback_halts_when_previous_image_missing() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(args, *, env=None, timeout=None):
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="missing")

    rollback = ComposeRollbackExecutor(
        previous_tag="v1.2.2",
        namespace="acme",
        runner=runner,
        env={"OMNISIGHT_REGISTRY": TEST_REGISTRY},
    )

    result = rollback.rollback(timeout_seconds=300)

    assert result.status == "halted"
    assert "missing previous image" in result.detail
    assert len(calls) == 2


def test_deploy_prod_persists_current_and_previous_tags_for_monitor() -> None:
    text = DEPLOY_PROD.read_text(encoding="utf-8")

    assert "OMNISIGHT_PREVIOUS_IMAGE_TAG" in text
    assert "OMNISIGHT_IMAGE_TAG" in text
    assert "omnisight-slo-monitor.service" in text

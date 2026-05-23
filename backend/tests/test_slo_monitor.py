"""OP-1634 -- canonical SLO monitor wiring regression tests."""

from __future__ import annotations

from pathlib import Path

from backend.orchestrator import slo_monitor as sm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SLO_THRESHOLDS = PROJECT_ROOT / "config" / "slo_thresholds.yaml"
RETIRED_SLO_CONFIG = PROJECT_ROOT / "config" / "slos.yaml"
SYSTEMD_SERVICE = PROJECT_ROOT / "deploy" / "systemd" / "omnisight-slo-monitor.service"
DEPLOY_PROD = PROJECT_ROOT / "scripts" / "deploy-prod.sh"


def test_backend_slo_monitor_is_deprecation_shim() -> None:
    import backend.slo_monitor as legacy

    assert legacy.main is sm.main
    assert legacy.SloMonitor is sm.SloMonitor
    assert legacy.DEFAULT_CONFIG_PATH == sm.DEFAULT_CONFIG_PATH


def test_slo_monitor_systemd_unit_runs_canonical_orchestrator_from_bridge() -> None:
    text = SYSTEMD_SERVICE.read_text(encoding="utf-8")

    assert "WorkingDirectory=%h/sora-bridge" in text
    assert "EnvironmentFile=-%h/sora-bridge/.env" in text
    assert (
        "ExecStart=%h/sora-bridge/backend/.venv/bin/python "
        "-m backend.orchestrator.slo_monitor"
    ) in text
    assert "python -m backend.slo_monitor" not in text
    assert "%h/work/sora/OmniSight-Productizer" not in text


def test_slo_thresholds_are_single_canonical_config_source() -> None:
    thresholds = sm.load_thresholds(SLO_THRESHOLDS)

    assert sm.DEFAULT_CONFIG_PATH == SLO_THRESHOLDS
    assert not RETIRED_SLO_CONFIG.exists()
    assert thresholds.error_rate_max == 0.01
    assert thresholds.p95_latency_ms_max == 500
    assert thresholds.sample_interval_seconds == 30
    assert thresholds.breach_sustain_seconds == 120
    assert thresholds.cooldown_seconds == 600


def test_missing_canary_state_defaults_to_full_rollback(monkeypatch) -> None:
    rolled_back: list[str] = []

    def _missing_state():
        raise FileNotFoundError("canary_state.json")

    class _CompatibleMigrationProbe:
        def check(self, *, previous_tag: str) -> None:
            assert previous_tag == "v1.2.2"

    class _FakeProductionDeployOrchestrator:
        def _rollback(self, tag: str) -> None:
            rolled_back.append(tag)

    monkeypatch.setattr("backend.canary_rollout.load_state", _missing_state)
    monkeypatch.setattr(
        "backend.production_release.ProductionDeployOrchestrator",
        _FakeProductionDeployOrchestrator,
    )
    monkeypatch.setenv("OMNISIGHT_PROD_CURRENT_IMAGE_TAG", "v1.2.3")
    monkeypatch.setenv("OMNISIGHT_PREVIOUS_IMAGE_TAG", "v1.2.2")

    rollback = sm.build_default_rollback(
        migration_safety_probe=_CompatibleMigrationProbe()
    )

    assert rollback.mode == "full"
    outcome = rollback.trigger("slo breach")
    assert outcome.mode == "full"
    assert outcome.status == "rolled_back"
    assert rolled_back == ["v1.2.3"]


def test_deploy_prod_persists_current_and_previous_tags_for_monitor() -> None:
    text = DEPLOY_PROD.read_text(encoding="utf-8")

    assert "OMNISIGHT_PREVIOUS_IMAGE_TAG" in text
    assert "OMNISIGHT_IMAGE_TAG" in text
    assert "omnisight-slo-monitor.service" in text

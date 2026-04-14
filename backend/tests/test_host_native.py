"""Phase 59 tests — Host-Native target detection + chooser overrides."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def _manifest(tmp_path, monkeypatch):
    """Patch host_native._PROJECT_ROOT to a temp dir + bust the cache."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    from backend import host_native as hn
    monkeypatch.setattr(hn, "_PROJECT_ROOT", tmp_path)
    hn._bust_cache()

    def _set(content: str) -> None:
        (cfg / "hardware_manifest.yaml").write_text(content, encoding="utf-8")
        hn._bust_cache()
    return _set


def test_no_manifest_returns_empty(_manifest):
    from backend import host_native as hn
    assert hn.target_platform_id() == ""
    assert hn.project_track() == ""
    assert hn.is_host_native() is False
    assert hn.should_use_app_only_pipeline() is False


def test_aarch64_target_not_host_native(_manifest):
    _manifest("project:\n  target_platform: 'aarch64'\n")
    from backend import host_native as hn
    assert hn.is_host_native() is False


def test_host_native_target(_manifest):
    _manifest("project:\n  target_platform: 'host_native'\n")
    from backend import host_native as hn
    assert hn.is_host_native() is True


def test_app_only_track(_manifest):
    _manifest(
        """project:
  target_platform: 'host_native'
  project_track: 'app_only'
"""
    )
    from backend import host_native as hn
    assert hn.should_use_app_only_pipeline() is True
    assert hn.app_only_phases() == ["concept", "build", "test", "deploy"]


def test_context_dict_compact(_manifest):
    _manifest(
        """project:
  target_platform: 'host_native'
  project_track: 'app_only'
"""
    )
    from backend import host_native as hn
    ctx = hn.context_dict()
    assert ctx["is_host_native"] is True
    assert ctx["project_track"] == "app_only"
    assert ctx["app_only_pipeline"] is True
    assert "host_arch" in ctx and ctx["host_arch"] != ""


def test_chooser_deploy_host_native_high_confidence():
    """deploy/dev_board chooser must lift confidence under host-native."""
    from backend import decision_defaults as dd
    options = [{"id": "go", "label": "go"}, {"id": "abort", "label": "abort"}]
    ctx_native = dd.Context(
        kind="deploy/dev_board", severity="risky",
        options=options, default_option_id="go",
        is_host_native=True,
    )
    ctx_cross = dd.Context(
        kind="deploy/dev_board", severity="risky",
        options=options, default_option_id="go",
        is_host_native=False,
    )
    cn = dd.consult(ctx_native)
    cc = dd.consult(ctx_cross)
    assert cn is not None and cc is not None
    assert cn.confidence > cc.confidence
    assert cn.confidence >= 0.9
    assert cc.confidence < 0.8


def test_chooser_binary_execute_native_vs_qemu():
    from backend import decision_defaults as dd
    opts = [{"id": "run", "label": "run"}]
    cn = dd.consult(dd.Context(kind="binary/execute", severity="risky",
                                options=opts, default_option_id="run",
                                is_host_native=True))
    cq = dd.consult(dd.Context(kind="binary/execute", severity="risky",
                                options=opts, default_option_id="run",
                                is_host_native=False))
    assert cn is not None and cq is not None
    assert cn.confidence == 0.95
    assert cq.confidence == 0.7


def test_host_native_profile_yaml_exists():
    """Operator-facing: configs/platforms/host_native.yaml must exist
    and be parseable."""
    import yaml
    p = Path(__file__).resolve().parents[2] / "configs" / "platforms" / "host_native.yaml"
    assert p.exists(), f"missing {p}"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["platform"] == "host_native"
    assert data["toolchain"] == "gcc"
    assert data["qemu"] == ""

"""Phase 60 forecast tests — covers v0 template baseline + v1 history
overlay confidence ladder."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def _empty_manifest(tmp_path):
    """Empty manifest → driver track inferred (no sensor/algorithm)."""
    p = tmp_path / "hwm.yaml"
    p.write_text("project:\n  name: ''\n  target_platform: ''\n", encoding="utf-8")
    return p


@pytest.fixture()
def _app_only_manifest(tmp_path):
    p = tmp_path / "hwm-app.yaml"
    p.write_text(
        """project:
  name: 'X'
  target_platform: 'host_native'
  project_track: 'app_only'
sensor: {}
""",
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def _full_stack_manifest(tmp_path):
    p = tmp_path / "hwm-full.yaml"
    p.write_text(
        """project:
  name: 'Big'
  target_platform: 'aarch64'
  project_track: 'full_stack'
sensor:
  model: IMX678
""",
        encoding="utf-8",
    )
    return p


def test_v0_template_baseline_no_history(monkeypatch, _empty_manifest):
    """No DB → confidence 0.5, method=template."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", "/tmp/nonexistent-forecast.db")
    Path("/tmp/nonexistent-forecast.db").unlink(missing_ok=True)
    from backend import forecast
    f = forecast.from_manifest(_empty_manifest)
    assert f.confidence == 0.5
    assert f.method == "template"
    assert f.tasks.total > 0
    assert f.agents.total > 0


def test_app_only_track_lighter_than_full_stack(_app_only_manifest, _full_stack_manifest):
    from backend import forecast
    a = forecast.from_manifest(_app_only_manifest)
    b = forecast.from_manifest(_full_stack_manifest)
    assert a.tasks.total < b.tasks.total
    assert a.agents.total < b.agents.total
    assert a.duration.total_hours < b.duration.total_hours


def test_v1_history_blend_at_5_samples(monkeypatch, tmp_path, _full_stack_manifest):
    """5..19 samples → confidence 0.70, method=template+history."""
    db = tmp_path / "h.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db))
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE token_usage (model TEXT, total_tokens INT, request_count INT);
        CREATE TABLE simulations (duration_ms INT);
        INSERT INTO token_usage VALUES ('claude', 50000, 10);
        INSERT INTO simulations VALUES (300000), (600000), (450000), (550000), (700000);
    """)
    con.commit(); con.close()

    from backend import forecast
    f = forecast.from_manifest(_full_stack_manifest)
    assert f.confidence == 0.70
    assert f.method == "template+history"


def test_v1_history_full_at_20_samples(monkeypatch, tmp_path, _full_stack_manifest):
    """≥20 samples → confidence 0.80, method=history."""
    db = tmp_path / "h.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(db))
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE token_usage (model TEXT, total_tokens INT, request_count INT);
        CREATE TABLE simulations (duration_ms INT);
        INSERT INTO token_usage VALUES ('claude', 200000, 30);
    """)
    for i in range(25):
        con.execute("INSERT INTO simulations (duration_ms) VALUES (?)", (300000 + i * 10000,))
    con.commit(); con.close()

    from backend import forecast
    f = forecast.from_manifest(_full_stack_manifest)
    assert f.confidence == 0.80
    assert f.method == "history"


def test_profile_sensitivity_present(_full_stack_manifest):
    from backend import forecast
    f = forecast.from_manifest(_full_stack_manifest)
    profiles = {p.profile for p in f.profile_sensitivity}
    assert profiles == {"STRICT", "BALANCED", "AUTONOMOUS", "GHOST"}
    # AUTONOMOUS must be cheaper than STRICT
    by_p = {p.profile: p.hours for p in f.profile_sensitivity}
    assert by_p["AUTONOMOUS"] < by_p["BALANCED"] < by_p["STRICT"]


def test_cost_uses_pricing(_full_stack_manifest):
    from backend import forecast
    f = forecast.from_manifest(_full_stack_manifest, provider="anthropic")
    assert f.cost.provider == "anthropic"
    assert f.cost.total_usd > 0
    # ollama free → cost should be zero
    f2 = forecast.from_manifest(_full_stack_manifest, provider="ollama")
    assert f2.cost.total_usd == 0.0

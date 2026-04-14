"""Phase 58 tests — Smart Defaults + Profiles."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
async def _profile_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "p.db")
        monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", path)
        from backend import config as _cfg
        _cfg.settings.database_path = path
        from backend import db
        db._DB_PATH = db._resolve_db_path()
        await db.init()
        # Reset profile + decision engine state
        from backend import decision_profiles as dp, decision_engine as de
        dp._reset_for_tests()
        de._reset_for_tests()
        yield (db, dp, de)
        await db.close()


@pytest.mark.asyncio
async def test_default_profile_strict(_profile_db):
    _, dp, _de = _profile_db
    assert dp.get_current_id() == "STRICT"
    assert dp.get_profile().threshold_risky == 2.0  # always queue


@pytest.mark.asyncio
async def test_set_profile_balanced(_profile_db):
    _, dp, _de = _profile_db
    p = dp.set_profile("BALANCED")
    assert p.id == "BALANCED"
    assert p.threshold_risky == 0.7


@pytest.mark.asyncio
async def test_ghost_blocked_without_env(_profile_db, monkeypatch):
    _, dp, _de = _profile_db
    monkeypatch.delenv("OMNISIGHT_ALLOW_GHOST_PROFILE", raising=False)
    monkeypatch.delenv("OMNISIGHT_ENV", raising=False)
    with pytest.raises(dp.GhostNotAllowed):
        dp.set_profile("GHOST")


@pytest.mark.asyncio
async def test_ghost_allowed_with_double_gate(_profile_db, monkeypatch):
    _, dp, _de = _profile_db
    monkeypatch.setenv("OMNISIGHT_ALLOW_GHOST_PROFILE", "true")
    monkeypatch.setenv("OMNISIGHT_ENV", "staging")
    p = dp.set_profile("GHOST")
    assert p.id == "GHOST"
    assert p.auto_critical is True


@pytest.mark.asyncio
async def test_strict_profile_keeps_risky_queued(_profile_db):
    _, dp, de = _profile_db
    dp.set_profile("STRICT")
    de.set_mode("supervised")
    dec = de.propose(
        kind="stuck/repeat_error",
        title="model stuck on import error",
        severity="risky",
        options=[
            {"id": "switch_model", "label": "switch"},
            {"id": "retry_same", "label": "retry"},
        ],
        default_option_id="switch_model",
    )
    # STRICT thresholds = 2.0 → never auto-resolves
    assert dec.status == de.DecisionStatus.pending
    assert dec.chosen_option_id is None


@pytest.mark.asyncio
async def test_balanced_profile_auto_resolves_high_confidence_risky(_profile_db):
    _, dp, de = _profile_db
    dp.set_profile("BALANCED")
    de.set_mode("supervised")
    dec = de.propose(
        kind="stuck/repeat_error",      # chooser confidence 0.92
        title="model stuck",
        severity="risky",
        options=[
            {"id": "switch_model", "label": "switch"},
            {"id": "retry_same", "label": "retry"},
        ],
        default_option_id="switch_model",
    )
    assert dec.status == de.DecisionStatus.auto_executed
    assert dec.chosen_option_id == "switch_model"
    # Confidence + rationale recorded in source
    assert "chooser_confidence" in dec.source
    assert dec.source["chooser_confidence"] >= 0.7
    assert dec.source["profile_id"] == "BALANCED"


@pytest.mark.asyncio
async def test_balanced_keeps_destructive_queued(_profile_db):
    _, dp, de = _profile_db
    dp.set_profile("BALANCED")
    de.set_mode("supervised")
    dec = de.propose(
        kind="stuck/blocked_forever",
        title="task blocked > 1h",
        severity="destructive",
        options=[
            {"id": "escalate", "label": "escalate"},
            {"id": "retry_same", "label": "retry"},
        ],
        default_option_id="escalate",
    )
    # BALANCED.threshold_destructive=2.0 → still queues
    assert dec.status == de.DecisionStatus.pending


@pytest.mark.asyncio
async def test_critical_kind_queues_unless_ghost(_profile_db):
    _, dp, de = _profile_db
    dp.set_profile("AUTONOMOUS")
    de.set_mode("supervised")
    dec = de.propose(
        kind="git_push/main",          # critical
        title="push to main",
        severity="destructive",
        options=[{"id": "go", "label": "go"}, {"id": "abort", "label": "abort"}],
        default_option_id="abort",
    )
    # AUTONOMOUS.auto_critical=False → critical kinds still queue even
    # when severity threshold would otherwise allow auto.
    assert dec.status == de.DecisionStatus.pending


@pytest.mark.asyncio
async def test_chooser_unknown_kind_falls_through(_profile_db):
    _, dp, de = _profile_db
    dp.set_profile("BALANCED")
    de.set_mode("supervised")
    dec = de.propose(
        kind="totally/unknown/kind",
        title="custom decision",
        severity="risky",
        options=[{"id": "ok", "label": "ok"}],
        default_option_id="ok",
    )
    # No chooser registered → no auto in BALANCED with severity=risky
    # (rule didn't fire either) → pending
    assert dec.status == de.DecisionStatus.pending

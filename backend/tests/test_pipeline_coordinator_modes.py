"""Tests for the pipeline-coordinator personality-mode system (AUDIT-29f-5 / OP-1003).

Each Acceptance Criterion maps to at least one named test so the AC ↔ test
mapping in the JIRA verification comment is mechanical:

  Code AC   → test_situation_profile_has_four_axes,
              test_select_mode_profile_combos (the parametrized
              (profile combo, expected mode) table — AC #5),
              test_three_mode_behavior_overlays,
              test_operator_override_label_parsing,
              test_mode_selector_dispatch
  Integration → test_engine_selects_mode_before_evaluation,
                test_engine_idle_tick_stays_skeleton,
                test_behavior_visible_in_decision_log
  Exercised → test_each_mode_fires_in_shadow_log,
              test_operator_override_observed_in_shadow_log

The skeleton seam (idle tick → "skeleton") stays covered by
test_pipeline_coordinator_skeleton.py; these tests cover the real per-situation
modes layered on top.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.agents.pipeline_coordinator import CoordinatorConfig, PipelineCoordinator
from backend.agents.pipeline_coordinator_modes import (
    DEFAULT_MODE,
    EXECUTION_MODE,
    INVESTIGATION_MODE,
    RESCUE_MODE,
    SKELETON_MODE,
    ExecutionMode,
    InvestigationMode,
    Level,
    ModeProfile,
    ModeSelector,
    RescueMode,
    SituationProfile,
    mode_behavior,
    parse_mode_override,
    select_mode,
)
from backend.agents.pipeline_coordinator_rules import DecisionContext, DecisionEngine
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot


def _now() -> datetime:
    return datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Code AC: SituationProfile dataclass with 4 axes ───────────────────


def test_situation_profile_has_four_axes() -> None:
    """Code AC: SituationProfile carries the 4 (§7.1) decision axes."""
    p = SituationProfile(
        urgency=Level.HIGH,
        risk=Level.MEDIUM,
        novelty=Level.LOW,
        reversibility=Level.HIGH,
    )
    assert (p.urgency, p.risk, p.novelty, p.reversibility) == (
        Level.HIGH,
        Level.MEDIUM,
        Level.LOW,
        Level.HIGH,
    )
    # Axes coerce from the bare string form for ergonomics.
    assert SituationProfile(urgency="high").urgency is Level.HIGH
    # Distress signals default to the no-distress values.
    assert SituationProfile().in_distress is False
    # Serialisable for the decision log.
    assert p.to_record()["urgency"] == "high"


def test_situation_profile_validation() -> None:
    with pytest.raises(ValueError):
        SituationProfile(runner_stuck_hours=-1.0)
    with pytest.raises(ValueError):
        SituationProfile(revert_count=-1)
    with pytest.raises(ValueError):
        SituationProfile(urgency="extreme")  # not a Level


# ── Code AC #5: each (profile combo, expected mode) pair verified ─────


@pytest.mark.parametrize(
    "profile, expected",
    [
        # ExecutionMode: high urgency + low novelty + low risk (§7.2).
        (
            SituationProfile(urgency=Level.HIGH, novelty=Level.LOW, risk=Level.LOW),
            EXECUTION_MODE,
        ),
        # InvestigationMode: low urgency + high novelty (§7.2).
        (
            SituationProfile(urgency=Level.LOW, novelty=Level.HIGH),
            INVESTIGATION_MODE,
        ),
        # RescueMode: runner stuck > 24h (§7.2 distress trigger).
        (SituationProfile(runner_stuck_hours=25.0), RESCUE_MODE),
        # RescueMode: > 5 reverts on one ticket (§7.2 distress trigger).
        (SituationProfile(revert_count=6), RESCUE_MODE),
        # Distress short-circuits even an execution-looking profile.
        (
            SituationProfile(
                urgency=Level.HIGH, novelty=Level.LOW, risk=Level.LOW, revert_count=6
            ),
            RESCUE_MODE,
        ),
        # Boundary: exactly 24h / 5 reverts is NOT yet distress (strict >).
        (
            SituationProfile(urgency=Level.HIGH, novelty=Level.LOW, risk=Level.LOW,
                             runner_stuck_hours=24.0, revert_count=5),
            EXECUTION_MODE,
        ),
        # High urgency but high risk → not ExecutionMode → default fast path.
        (
            SituationProfile(urgency=Level.HIGH, novelty=Level.LOW, risk=Level.HIGH),
            DEFAULT_MODE,
        ),
        # High novelty but also high urgency → not InvestigationMode → default.
        (
            SituationProfile(urgency=Level.HIGH, novelty=Level.HIGH),
            DEFAULT_MODE,
        ),
        # Middle-of-the-road profile → default steady-state mode.
        (
            SituationProfile(urgency=Level.MEDIUM, risk=Level.MEDIUM, novelty=Level.MEDIUM),
            DEFAULT_MODE,
        ),
    ],
)
def test_select_mode_profile_combos(profile: SituationProfile, expected: str) -> None:
    """Code AC #5: every (profile combo, expected mode) pair is verified."""
    assert select_mode(profile) == expected


def test_default_mode_is_execution() -> None:
    """The steady-state default is ExecutionMode (Tier-1 fast path)."""
    assert DEFAULT_MODE == EXECUTION_MODE


# ── Code AC: operator override beats profile (§7.3) ───────────────────


def test_operator_override_beats_profile() -> None:
    """A coord-mode override forces its mode regardless of the profile."""
    investigation_profile = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)
    assert select_mode(investigation_profile) == INVESTIGATION_MODE
    assert select_mode(investigation_profile, override="execution") == EXECUTION_MODE
    # Distress would normally win — override still beats it.
    distress = SituationProfile(revert_count=9)
    assert select_mode(distress, override="investigation") == INVESTIGATION_MODE


def test_unrecognised_override_falls_through_to_profile() -> None:
    """An operator typo in the override is ignored, not crashed on."""
    p = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)
    assert select_mode(p, override="nonsense-mode") == INVESTIGATION_MODE


def test_operator_override_label_parsing() -> None:
    """Code AC: coord-mode:<mode> label resolves to a canonical mode name."""
    assert parse_mode_override(["coord-mode:execution"]) == EXECUTION_MODE
    assert parse_mode_override(["coord-mode:investigation"]) == INVESTIGATION_MODE
    assert parse_mode_override(["coord-mode:rescue"]) == RESCUE_MODE
    # Canonical name + case-insensitive.
    assert parse_mode_override(["coord-mode:ExecutionMode"]) == EXECUTION_MODE
    assert parse_mode_override(["COORD-MODE:RESCUE"]) == RESCUE_MODE
    # Later-phase modes are recognised by name even pre-implementation (§7.2).
    assert parse_mode_override(["coord-mode:triage"]) == "TriageMode"
    # Non-coord labels and empty inputs → no override.
    assert parse_mode_override(["needs-coordinator", "tier:M"]) is None
    assert parse_mode_override([]) is None
    assert parse_mode_override(None) is None
    # Unknown coord-mode slug → no override.
    assert parse_mode_override(["coord-mode:bogus"]) is None


# ── Code AC: 3 mode classes with behavior overrides (§7.2) ────────────


def test_three_mode_behavior_overlays() -> None:
    """Code AC: ExecutionMode / InvestigationMode / RescueMode behavior knobs."""
    # ExecutionMode — Tier-1 dominates, Tier-2 only on conflict, narrow context.
    assert ExecutionMode.tier1_dominant is True
    assert ExecutionMode.tier2_policy == "only_on_conflict"
    assert ExecutionMode.consults_llm_every_decision is False
    assert ExecutionMode.files_followup_ticket is False

    # InvestigationMode — always Tier-2, wide context (10 hops / 20 lessons),
    # files a research/spike follow-up.
    assert InvestigationMode.tier2_policy == "always"
    assert InvestigationMode.llm_context_hops == 10
    assert InvestigationMode.llm_lessons_window == 20
    assert InvestigationMode.files_followup_ticket is True
    assert InvestigationMode.followup_ticket_kind == "research-spike"

    # RescueMode — full root-cause, files runner-blocked-pattern, writes a lesson.
    assert RescueMode.requires_root_cause is True
    assert RescueMode.files_followup_ticket is True
    assert RescueMode.followup_ticket_kind == "runner-blocked-pattern"
    assert RescueMode.writes_lesson is True

    # Registry lookup resolves names → overlays; idle/later-phase → None.
    assert mode_behavior(EXECUTION_MODE) is ExecutionMode
    assert mode_behavior(INVESTIGATION_MODE) is InvestigationMode
    assert mode_behavior(RESCUE_MODE) is RescueMode
    assert mode_behavior(SKELETON_MODE) is None
    assert mode_behavior("TriageMode") is None


# ── Backward-compat: ModeSelector dispatch (skeleton seam preserved) ──


def test_mode_selector_dispatch() -> None:
    sel = ModeSelector()
    # Idle tick / legacy neutral profile → skeleton (unchanged contract).
    assert sel.select() == SKELETON_MODE
    assert sel.select(None) == SKELETON_MODE
    assert sel.select(ModeProfile()) == SKELETON_MODE
    # Real situation → a Phase-6 mode.
    assert sel.select(SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)) == INVESTIGATION_MODE
    # Operator authority is absolute even on an idle tick (no profile).
    assert sel.select(override="rescue") == RESCUE_MODE


# ── Integration AC: engine selects mode before rule evaluation ────────


def _ctx(situation: SituationProfile | None = None, override: str | None = None) -> DecisionContext:
    return DecisionContext(
        now=_now(),
        capacity=CapacitySnapshot.empty(captured_at=_now()),
        situation=situation,
        mode_override=override,
    )


def test_engine_selects_mode_before_evaluation() -> None:
    """Integration AC: engine calls ModeSelector → mode + behavior on result."""
    engine = DecisionEngine()
    result = engine.evaluate(_ctx(SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)))
    assert result.mode == INVESTIGATION_MODE
    assert result.mode_behavior is InvestigationMode
    # Operator override is respected through the engine.
    forced = engine.evaluate(
        _ctx(SituationProfile(urgency=Level.LOW, novelty=Level.HIGH), override="execution")
    )
    assert forced.mode == EXECUTION_MODE
    assert forced.mode_behavior is ExecutionMode


def test_engine_idle_tick_stays_skeleton() -> None:
    """Integration AC: no situation → engine keeps the idle skeleton mode."""
    result = DecisionEngine().evaluate(_ctx(situation=None))
    assert result.mode == SKELETON_MODE
    assert result.mode_behavior is None


# ── Exercised AC: each mode fires in the shadow (decision) log ────────


def _shadow_coordinator(tmp_path: Path) -> PipelineCoordinator:
    base = tmp_path / "coordinator"
    cfg = CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
    )
    return PipelineCoordinator(cfg, clock=_now)


def _read_modes(directory: Path) -> list[dict]:
    records: list[dict] = []
    for f in sorted(directory.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return [r for r in records if r.get("event") == "decision_tick"]


def test_each_mode_fires_in_shadow_log(tmp_path: Path) -> None:
    """Exercised AC: >=1 observation of each mode firing in the decision log.

    Drives the daemon's per-situation seam through one tick per mode and
    asserts each leaves an auditable, dry-run decision-log entry stamped with
    the mode + its behavior knobs.
    """
    coord = _shadow_coordinator(tmp_path)
    situations = {
        EXECUTION_MODE: (SituationProfile(urgency=Level.HIGH, novelty=Level.LOW, risk=Level.LOW), None),
        INVESTIGATION_MODE: (SituationProfile(urgency=Level.LOW, novelty=Level.HIGH), None),
        RESCUE_MODE: (SituationProfile(runner_stuck_hours=48.0), None),
    }
    for situation, override in situations.values():
        coord._current_situation = lambda s=situation, o=override: (s, o)  # type: ignore[method-assign]
        coord.run_once()

    logged_modes = [r["mode"] for r in _read_modes(coord.config.decision_log_dir)]
    assert EXECUTION_MODE in logged_modes
    assert INVESTIGATION_MODE in logged_modes
    assert RESCUE_MODE in logged_modes
    # Shadow mode: every entry is a dry-run no-op (it observes, doesn't act).
    assert all(r["dry_run"] is True and r["actions"] == [] for r in _read_modes(coord.config.decision_log_dir))
    # The behavior knobs are auditable in the log for a fired mode.
    investigation = next(r for r in _read_modes(coord.config.decision_log_dir) if r["mode"] == INVESTIGATION_MODE)
    assert investigation["mode_behavior"]["tier2_policy"] == "always"
    assert investigation["mode_behavior"]["llm_context_hops"] == 10


def test_operator_override_observed_in_shadow_log(tmp_path: Path) -> None:
    """Exercised AC: a coord-mode:* operator override is observed working.

    The situation profiles to InvestigationMode, but the operator override
    label forces ExecutionMode — and the forced mode is what lands in the log.
    """
    coord = _shadow_coordinator(tmp_path)
    profile = SituationProfile(urgency=Level.LOW, novelty=Level.HIGH)
    override = parse_mode_override(["coord-mode:execution"])
    coord._current_situation = lambda: (profile, override)  # type: ignore[method-assign]
    coord.run_once()

    [record] = _read_modes(coord.config.decision_log_dir)
    assert record["mode"] == EXECUTION_MODE  # override won over the profile

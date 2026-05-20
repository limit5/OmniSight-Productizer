"""Tier-1 deterministic rule tests (OP-1001 / AUDIT-29f-3) — ADR-0021 §5.1.

Coverage map (Code AC: "each rule unit-tested, positive + negative"):

  operator-keep-out               → test_operator_keep_out_{fires,abstains,vetoes}
  chain-deadlock                  → test_chain_deadlock_{cycle,acyclic}
  revert-loop-quarantine          → test_revert_loop_{fires,under_threshold,already}
  merger-repeat-fail              → test_merger_repeat_fail_{fires,under_threshold}
  stuck-in-progress               → test_stuck_in_progress_{fires,fresh,live_runner}
  stale-claim-cleanup             → test_stale_claim_{fires,fresh,live_runner}
  runner-blocked-marker-stale     → test_runner_blocked_marker_{fires,blocker_open}
  dependency-out-of-area          → test_dependency_out_of_area_{fires,no_marker,ambiguous}
  capability-blocked-known-locale → test_capability_blocked_{fires,wrong_type}
  wrong-class-routing             → test_wrong_class_routing_{fires,no_codex_capacity}

Registry + engine: test_registry_* / test_engine_* / test_startup_log_*
Integration (via the daemon): test_integration_synthetic_ticket_*
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.agents import pipeline_coordinator_rules as rules
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot, RunnerCapacity
from backend.agents.pipeline_coordinator_rules import (
    ACTION_ESCALATE,
    ACTION_FILE_TICKET,
    ACTION_MENTION_OPERATOR,
    ACTION_RELABEL,
    ACTION_TRANSITION,
    Action,
    Comment,
    DecisionContext,
    NoopAction,
    Tier1RuleEngine,
    Ticket,
    WorkGraph,
    load_registry,
)

NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
STALE = NOW - timedelta(seconds=rules._stale_threshold_seconds() + 60)
FRESH = NOW - timedelta(seconds=60)


def _ctx(ticket: Ticket | None = None, *, tickets=None, capacity=None) -> DecisionContext:
    return DecisionContext(
        now=NOW,
        capacity=capacity or CapacitySnapshot.empty(captured_at=NOW),
        ticket=ticket,
        tickets=tickets or {},
    )


def _ticket(key: str = "OP-2000", **kw) -> Ticket:
    return Ticket(key=key, **kw)


# ── Action vocabulary (Code AC: "Action union type defined") ──────────


def test_action_kinds_constrained_to_allowed_set() -> None:
    with pytest.raises(ValueError):
        Action(kind="rm-rf-prod")  # not in §6.1 allowed set
    # Every factory yields an allowed kind.
    assert Action.relabel("OP-1", add=("x",)).kind == ACTION_RELABEL
    assert Action.file_ticket(blocking="OP-1", target_area="db", description="d").kind == ACTION_FILE_TICKET
    assert Action.transition("OP-1", to_status="To Do").kind == ACTION_TRANSITION
    assert Action.mention_operator("OP-1", message="m").kind == ACTION_MENTION_OPERATOR
    assert Action.escalate("OP-1", reason="r").kind == ACTION_ESCALATE
    assert NoopAction().kind == "noop"


def test_action_factories_default_dry_run_true() -> None:
    """Rules never flip dry_run — the action layer / acting mode owns that."""
    assert Action.relabel("OP-1", add=("x",)).dry_run is True
    assert Action.escalate("OP-1", reason="r").dry_run is True


# ── operator-keep-out (#10, priority 0) ───────────────────────────────


def test_operator_keep_out_fires() -> None:
    action = rules.operator_keep_out(_ctx(_ticket(labels=("coord-skip",))))
    assert isinstance(action, NoopAction)


def test_operator_keep_out_abstains_without_label() -> None:
    assert rules.operator_keep_out(_ctx(_ticket(labels=("tier:M",)))) is None
    assert rules.operator_keep_out(_ctx(None)) is None


def test_operator_keep_out_vetoes_other_rules() -> None:
    """coord-skip short-circuits the whole registry (priority 0)."""
    t = _ticket(labels=("coord-skip",), revert_count_24h=99)  # would trip revert-loop
    result = Tier1RuleEngine().evaluate(_ctx(t))
    assert result.rule_name == "operator-keep-out"
    assert result.is_noop


# ── chain-deadlock (#8, priority 5) ───────────────────────────────────


def test_chain_deadlock_detects_cycle() -> None:
    a = _ticket("OP-A", blocked_by=("OP-B",))
    b = _ticket("OP-B", blocked_by=("OP-A",))
    action = rules.chain_deadlock(_ctx(a, tickets={"OP-A": a, "OP-B": b}))
    assert action is not None and action.kind == ACTION_MENTION_OPERATOR
    assert action.params["urgency"] == "high"


def test_chain_deadlock_abstains_when_acyclic() -> None:
    a = _ticket("OP-A", blocked_by=("OP-B",))
    b = _ticket("OP-B", blocked_by=())
    assert rules.chain_deadlock(_ctx(a, tickets={"OP-A": a, "OP-B": b})) is None


# ── revert-loop-quarantine (#5, priority 10) ──────────────────────────


def test_revert_loop_fires_over_threshold() -> None:
    action = rules.revert_loop_quarantine(_ctx(_ticket(revert_count_24h=6)))
    assert action is not None and action.kind == ACTION_RELABEL
    assert "coord-quarantine" in action.params["add"]


def test_revert_loop_abstains_under_threshold() -> None:
    assert rules.revert_loop_quarantine(_ctx(_ticket(revert_count_24h=5))) is None


def test_revert_loop_abstains_when_already_quarantined() -> None:
    t = _ticket(revert_count_24h=9, labels=("coord-quarantine",))
    assert rules.revert_loop_quarantine(_ctx(t)) is None


# ── merger-repeat-fail (#4, priority 20) ──────────────────────────────


def test_merger_repeat_fail_fires() -> None:
    action = rules.merger_repeat_fail(_ctx(_ticket(merger_fail_count=4)))
    assert action is not None and action.kind == ACTION_ESCALATE


def test_merger_repeat_fail_abstains_under_threshold() -> None:
    assert rules.merger_repeat_fail(_ctx(_ticket(merger_fail_count=3))) is None


# ── stuck-in-progress (#7, priority 30) ───────────────────────────────


def test_stuck_in_progress_fires() -> None:
    t = _ticket(status="進行中", in_progress_since=STALE, has_live_runner=False)
    action = rules.stuck_in_progress(_ctx(t))
    assert action is not None and action.kind == ACTION_TRANSITION
    assert action.params["to_status"] == "To Do"


def test_stuck_in_progress_abstains_when_fresh() -> None:
    t = _ticket(status="進行中", in_progress_since=FRESH, has_live_runner=False)
    assert rules.stuck_in_progress(_ctx(t)) is None


def test_stuck_in_progress_abstains_with_live_runner() -> None:
    t = _ticket(status="進行中", in_progress_since=STALE, has_live_runner=True)
    assert rules.stuck_in_progress(_ctx(t)) is None


# ── stale-claim-cleanup (#2, priority 40) ─────────────────────────────


def test_stale_claim_fires() -> None:
    t = _ticket(
        labels=("claim:default:0017-abc", "tier:M"),
        claim_started_at=STALE,
        has_live_runner=False,
    )
    action = rules.stale_claim_cleanup(_ctx(t))
    assert action is not None and action.kind == ACTION_RELABEL
    assert "claim:default:0017-abc" in action.params["remove"]


def test_stale_claim_abstains_when_fresh() -> None:
    t = _ticket(labels=("claim:default:0017-abc",), claim_started_at=FRESH)
    assert rules.stale_claim_cleanup(_ctx(t)) is None


def test_stale_claim_abstains_with_live_runner() -> None:
    t = _ticket(
        labels=("claim:default:0017-abc",), claim_started_at=STALE, has_live_runner=True
    )
    assert rules.stale_claim_cleanup(_ctx(t)) is None


# ── runner-blocked-marker-stale (#3, priority 50) ─────────────────────


def test_runner_blocked_marker_fires_when_blocker_published() -> None:
    blocked = _ticket("OP-A", labels=("runner-blocked:waiting-OP-B",))
    blocker = _ticket("OP-B", status="公開済み")
    action = rules.runner_blocked_marker_stale(
        _ctx(blocked, tickets={"OP-A": blocked, "OP-B": blocker})
    )
    assert action is not None and action.kind == ACTION_RELABEL
    assert "runner-blocked:waiting-OP-B" in action.params["remove"]


def test_runner_blocked_marker_abstains_when_blocker_open() -> None:
    blocked = _ticket("OP-A", labels=("runner-blocked:waiting-OP-B",))
    blocker = _ticket("OP-B", status="進行中")
    assert (
        rules.runner_blocked_marker_stale(
            _ctx(blocked, tickets={"OP-A": blocked, "OP-B": blocker})
        )
        is None
    )


# ── dependency-out-of-area (#1, priority 60) ──────────────────────────


def test_dependency_out_of_area_fires() -> None:
    t = _ticket(
        areas=("backend",),
        labels=("needs-coordinator",),
        comments=(
            Comment(
                "[runner-discovered-dependency] needs a schema change in area:db first"
            ),
        ),
    )
    action = rules.dependency_out_of_area(_ctx(t))
    assert action is not None and action.kind == ACTION_FILE_TICKET
    assert action.params["target_area"] == "db"


def test_dependency_out_of_area_abstains_without_marker() -> None:
    t = _ticket(
        areas=("backend",),
        labels=("needs-coordinator",),
        comments=(Comment("just a normal status update mentioning db somewhere"),),
    )
    assert rules.dependency_out_of_area(_ctx(t)) is None


def test_dependency_out_of_area_abstains_when_ambiguous() -> None:
    """Two known out-of-area names → defer to Tier-2 (never guess)."""
    t = _ticket(
        areas=("backend",),
        labels=("needs-coordinator",),
        comments=(
            Comment("[runner-discovered-dependency] touches area:db and area:frontend"),
        ),
    )
    assert rules.dependency_out_of_area(_ctx(t)) is None


def test_dependency_out_of_area_abstains_without_label() -> None:
    t = _ticket(
        areas=("backend",),
        comments=(Comment("[runner-discovered-dependency] area:db"),),
    )
    assert rules.dependency_out_of_area(_ctx(t)) is None


# ── capability-blocked-known-locale (#6, priority 70) ─────────────────


def test_capability_blocked_fires_on_story() -> None:
    t = _ticket(
        issuetype="ストーリー",
        comments=(Comment("[runner-capability-blocked] run_migration not permitted"),),
    )
    action = rules.capability_blocked_known_locale(_ctx(t))
    assert action is not None and action.kind == ACTION_RELABEL
    assert "capability:enable=*" in action.params["add"]


def test_capability_blocked_abstains_on_non_story() -> None:
    t = _ticket(
        issuetype="Bug",
        comments=(Comment("[runner-capability-blocked] run_migration not permitted"),),
    )
    assert rules.capability_blocked_known_locale(_ctx(t)) is None


# ── wrong-class-routing (#9, priority 80) ─────────────────────────────


def _codex_capacity() -> CapacitySnapshot:
    return CapacitySnapshot(
        captured_at=NOW,
        runners={"subscription-codex": RunnerCapacity("subscription-codex", free_slots=2)},
    )


def test_wrong_class_routing_fires_comment_only() -> None:
    t = _ticket(areas=("backend",), labels=("class:subscription-claude",))
    action = rules.wrong_class_routing(_ctx(t, capacity=_codex_capacity()))
    assert action is not None and action.kind == ACTION_MENTION_OPERATOR
    assert action.params["urgency"] == "low"  # suggestion, never a relabel


def test_wrong_class_routing_abstains_without_codex_capacity() -> None:
    t = _ticket(areas=("backend",), labels=("class:subscription-claude",))
    assert rules.wrong_class_routing(_ctx(t)) is None  # empty capacity


# ── Registry + priority ordering ──────────────────────────────────────


def test_registry_loads_exactly_ten_rules() -> None:
    reg = load_registry()
    assert len(reg) == 10


def test_registry_is_priority_ordered_keep_out_first() -> None:
    reg = load_registry()
    names = [r.name for r in reg]
    assert names[0] == "operator-keep-out"  # priority 0, absolute veto
    priorities = [r.priority for r in reg]
    assert priorities == sorted(priorities)  # ascending


def test_registry_names_match_adr_5_1() -> None:
    expected = {
        "dependency-out-of-area",
        "stale-claim-cleanup",
        "runner-blocked-marker-stale",
        "merger-repeat-fail",
        "revert-loop-quarantine",
        "capability-blocked-known-locale",
        "stuck-in-progress",
        "chain-deadlock",
        "wrong-class-routing",
        "operator-keep-out",
    }
    assert {r.name for r in load_registry()} == expected


# ── Engine ────────────────────────────────────────────────────────────


def test_engine_first_match_wins_by_priority() -> None:
    """A ticket that trips two rules fires the higher-priority one."""
    # merger-repeat-fail (20) outranks stuck-in-progress (30).
    t = _ticket(
        status="進行中",
        in_progress_since=STALE,
        has_live_runner=False,
        merger_fail_count=4,
    )
    result = Tier1RuleEngine().evaluate(_ctx(t))
    assert result.rule_name == "merger-repeat-fail"
    assert result.tier == 1


def test_engine_no_match_returns_tier1_noop() -> None:
    result = Tier1RuleEngine().evaluate(_ctx(None))
    assert result.is_noop
    assert result.reason == rules.TIER1_NO_MATCH_REASON
    assert result.rule_name is None


def test_startup_log_lists_rule_registry(caplog) -> None:
    """Deploy AC: rule registry visible in a startup log line."""
    with caplog.at_level(logging.INFO, logger="backend.agents.pipeline_coordinator_rules"):
        Tier1RuleEngine()
    line = next(m for m in caplog.messages if "rule registry loaded" in m)
    assert "10 rules" in line
    assert "operator-keep-out@0" in line


# ── Integration through the daemon (Integration AC) ───────────────────


def _read_log(directory: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted(directory.glob("*.jsonl")):
        out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return out


def test_integration_synthetic_ticket_fires_rule_into_decision_log(tmp_path: Path) -> None:
    """Integration AC: synthetic ticket → known rule → action in decision log.

    Wires the real Tier-1 engine + shadow action layer into the daemon, feeds
    a synthetic capability-blocked Story via the work-graph provider, runs one
    tick, and asserts the fired rule + its (shadow) action are recorded.
    """
    from backend.agents.pipeline_coordinator import (
        CoordinatorConfig,
        PipelineCoordinator,
    )

    base = tmp_path / "coord"
    cfg = CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
    )
    story = Ticket(
        key="OP-7777",
        issuetype="ストーリー",
        comments=(Comment("[runner-capability-blocked] run_migration not permitted"),),
    )
    coord = PipelineCoordinator(
        cfg,
        engine=Tier1RuleEngine(),
        clock=lambda: NOW,
        work_graph_provider=lambda: WorkGraph(focal=story, tickets={"OP-7777": story}),
    )

    result = coord.run_once()

    assert result.rule_name == "capability-blocked-known-locale"
    [record] = [r for r in _read_log(cfg.decision_log_dir) if r["event"] == "decision_tick"]
    assert record["rule_name"] == "capability-blocked-known-locale"
    assert record["tier"] == 1
    assert record["actions"][0]["kind"] == ACTION_RELABEL
    # Shadow action layer recorded the would-be execution (no JIRA mutation).
    assert record["action_results"][0]["executed"] is False
    assert record["action_results"][0]["shadow"] is True


def test_integration_idle_tick_is_clean_noop(tmp_path: Path) -> None:
    """Empty work-graph tick → Tier-1 abstains → no-op, no action records."""
    from backend.agents.pipeline_coordinator import (
        CoordinatorConfig,
        PipelineCoordinator,
    )

    base = tmp_path / "coord"
    cfg = CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
    )
    coord = PipelineCoordinator(cfg, engine=Tier1RuleEngine(), clock=lambda: NOW)
    result = coord.run_once()
    assert result.is_noop
    [record] = [r for r in _read_log(cfg.decision_log_dir) if r["event"] == "decision_tick"]
    assert record["actions"] == []
    assert "action_results" not in record

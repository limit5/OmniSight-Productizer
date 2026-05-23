"""Cold-start 4-phase + L6 crash-replay tests for the pipeline coordinator.

OP-1551 — follow-up to OP-1005 (AUDIT-29f-7, Gerrit #1084). The cold-start
orchestrator landed with zero unit tests; this module closes that
Exercised-AC gap. Everything is driven through an injected
``FakeColdStartGateway`` so the 4-phase decision tree (ADR-0021 §3.3) and the
L6 crash-replay (ADR §9 L6) are verified as a pure, deterministic decision
tree with no real systemctl / git / JIRA I/O.

AC mapping (OP-1551):

  Code AC
   - FakeColdStartGateway implements the ColdStartGateway Protocol
       → test_fake_gateway_satisfies_coldstart_protocol
   - startup() runs 4 phases in order, populated report, began+complete events
       → test_startup_runs_four_phases_in_order_and_emits_lifecycle_events
   - _startup_1_infra: start down units / still-down / hard-down halt
       → test_startup_1_starts_down_units,
         test_startup_1_recheck_clears_transient_down_unit,
         test_startup_1_halts_and_mentions_operator_when_unit_stays_down
   - _l6_crash_recovery: 24h window / per-entry event / empty no-op
       → test_l6_replays_only_last_24h,
         test_l6_clean_shutdown_is_not_recovered,
         test_l6_resolves_interrupted_actions_by_kind,
         test_l6_no_preceding_real_action_is_noop,
         test_l6_empty_or_missing_log_is_noop
   - _startup_2_reconcile: interrupted-ticket decision tree (§3.3)
       → test_startup_2_reconcile_decision_tree,
         test_startup_2_mergeable_beats_branch_commits
   - _startup_3_sweep: stale claims / orphan assignees / dead waiting markers
       → test_startup_3_sweep_applies_plan
   - Fail-open: degraded collaborator never wedges startup before the loop
       → test_startup_reaches_loop_with_fully_degraded_gateway,
         test_production_gateway_swallows_backend_failures
   - run_deployment_audit / _last_json_line / _build_stale_sweep_plan helpers
       → test_run_deployment_audit_parses_units,
         test_run_deployment_audit_missing_script_is_empty,
         test_last_json_line_variants,
         test_build_stale_sweep_plan_classifies_hygiene

  Deploy AC      → standard pytest, tmp_path + fakes, no real infra.
  Integration AC → runs alongside the rest of the coordinator suite.
  Exercised AC   → direct pytest of this file exits 0.

OP-1555 — shadow-gate the cold-start recovery (sibling of OP-1552). The
acting=False (observe-only) path must RECORD would-be reconcile/sweep/infra
actions but call NO live mutator:

  - ShadowColdStartGateway forwards reads, no-ops mutators
      → test_shadow_gateway_satisfies_protocol_and_forwards_reads
  - shadow boot: zero mutator calls, began/complete + would-be records written
      → test_shadow_boot_records_actions_but_fires_zero_mutators
  - acting boot: mutators fire as today (contrast)
      → test_acting_boot_fires_mutators_as_today
  - build_default_coordinator threads acting → cold-start gateway
      → test_default_cold_start_gateway_shadow_vs_acting,
        test_build_default_coordinator_threads_acting_to_cold_start
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents import pipeline_coordinator as pc
from backend.agents.pipeline_coordinator import (
    COLD_START_BEGAN_EVENT,
    COLD_START_COMPLETE_EVENT,
    CRASH_RECOVERY_EVENT,
    L6_REPLAY_WINDOW_HOURS,
    STEADY_STATE_ENTERED_EVENT,
    ColdStartGateway,
    ColdStartReport,
    CoordinatorConfig,
    InfraAuditResult,
    InfraUnit,
    InterruptedTicket,
    JiraDispatchColdStartGateway,
    PipelineCoordinator,
    ShadowColdStartGateway,
    StaleSweepPlan,
    _build_stale_sweep_plan,
    _default_cold_start_gateway,
    _last_json_line,
    build_default_coordinator,
    run_deployment_audit,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_FILE_TICKET,
    ACTION_MENTION_OPERATOR,
    ACTION_NOOP,
    ACTION_RELABEL,
    ACTION_TRANSITION,
    Action,
    DecisionContext,
)


# ── Fakes ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeColdStartGateway:
    """In-memory :class:`ColdStartGateway` recording every call.

    Behaviour is fully data-driven so each phase can be steered without
    real I/O. ``audits`` is a sequence of :class:`InfraAuditResult` returned
    in order (the last one repeats), so the Startup-1 re-verify (a second
    ``audit_infra`` after starts) can return a different verdict than the
    first pass.
    """

    def __init__(
        self,
        *,
        audits: list[InfraAuditResult] | None = None,
        start_results: dict[str, bool] | None = None,
        interrupted: list[InterruptedTicket] | None = None,
        live_runners: tuple[str, ...] = (),
        mergeable: tuple[str, ...] = (),
        with_commits: tuple[str, ...] = (),
        live_labels: dict[str, tuple[str, ...]] | None = None,
        sweep_plan: StaleSweepPlan | None = None,
    ) -> None:
        self._audits = list(audits) if audits else [InfraAuditResult()]
        self._start_results = dict(start_results or {})
        self._interrupted = list(interrupted or [])
        self._live = set(live_runners)
        self._mergeable = set(mergeable)
        self._commits = set(with_commits)
        self._live_labels = dict(live_labels or {})
        self._sweep_plan = sweep_plan or StaleSweepPlan()
        # Call records (assertion surface).
        self.audit_calls = 0
        self.started: list[str] = []
        self.marked_resumable: list[str] = []
        self.transitioned: list[str] = []
        self.reset_todo: list[str] = []
        self.removed_labels: list[tuple[str, str]] = []
        self.cleared_assignees: list[str] = []
        self.operator_mentions: list[tuple[str, str, str]] = []

    # ── Startup-1 ──
    def audit_infra(self) -> InfraAuditResult:
        idx = min(self.audit_calls, len(self._audits) - 1)
        self.audit_calls += 1
        return self._audits[idx]

    def start_unit(self, unit: str) -> bool:
        self.started.append(unit)
        return self._start_results.get(unit, True)

    # ── Startup-2 ──
    def interrupted_tickets(self) -> list[InterruptedTicket]:
        return list(self._interrupted)

    def has_live_runner(self, key: str) -> bool:
        return key in self._live

    def gerrit_change_mergeable(self, key: str) -> bool:
        return key in self._mergeable

    def branch_has_commits(self, key: str) -> bool:
        return key in self._commits

    def ticket_labels(self, key: str) -> tuple[str, ...]:
        return self._live_labels.get(key, ())

    def mark_resumable(self, key: str) -> None:
        self.marked_resumable.append(key)

    def transition_under_review(self, key: str) -> None:
        self.transitioned.append(key)

    def reset_to_todo(self, key: str) -> None:
        self.reset_todo.append(key)

    # ── Startup-3 ──
    def stale_sweep_plan(self) -> StaleSweepPlan:
        return self._sweep_plan

    def remove_label(self, key: str, label: str) -> None:
        self.removed_labels.append((key, label))

    def clear_assignee(self, key: str) -> None:
        self.cleared_assignees.append(key)

    # ── shared ──
    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None:
        self.operator_mentions.append((key, message, urgency))


class RecordingReplayExecutor:
    def __init__(self, *, already_applied: set[str] | None = None) -> None:
        self.already_applied = set(already_applied or set())
        self.calls: list[tuple[str, str]] = []

    def __call__(self, action: Action, ctx: DecisionContext) -> dict[str, Any]:
        key = f"{action.kind}:{action.target}"
        self.calls.append((action.kind, action.target))
        if key in self.already_applied:
            return {
                "kind": action.kind,
                "target": action.target,
                "executed": False,
                "reason": "idempotent_skip",
            }
        self.already_applied.add(key)
        return {"kind": action.kind, "target": action.target, "executed": True}


class DegradedGateway:
    """A fail-open gateway that yields empty / conservative answers for every
    method (the contract the production adapters honour: never raise, always
    degrade). Used to prove a degraded environment cannot wedge startup."""

    def audit_infra(self) -> InfraAuditResult:
        return InfraAuditResult()

    def start_unit(self, unit: str) -> bool:
        return True

    def interrupted_tickets(self) -> list[InterruptedTicket]:
        return []

    def has_live_runner(self, key: str) -> bool:
        return False

    def gerrit_change_mergeable(self, key: str) -> bool:
        return False

    def branch_has_commits(self, key: str) -> bool:
        return False

    def ticket_labels(self, key: str) -> tuple[str, ...]:
        return ()

    def mark_resumable(self, key: str) -> None:
        return None

    def transition_under_review(self, key: str) -> None:
        return None

    def reset_to_todo(self, key: str) -> None:
        return None

    def stale_sweep_plan(self) -> StaleSweepPlan:
        return StaleSweepPlan()

    def remove_label(self, key: str, label: str) -> None:
        return None

    def clear_assignee(self, key: str) -> None:
        return None

    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None:
        return None


# ── Helpers ───────────────────────────────────────────────────────────


def _config(
    tmp_path: Path,
    *,
    cold_start_max_infra: int = 100,
    cold_start_max_reconcile: int = 100,
    cold_start_max_sweep: int = 100,
    jira_agent_class: str = "subscription-claude",
) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        jira_agent_class=jira_agent_class,
        heartbeat_interval_seconds=60.0,
        tick_interval_seconds=60.0,
        sweep_interval_seconds=3600.0,
        cold_start_max_infra=cold_start_max_infra,
        cold_start_max_reconcile=cold_start_max_reconcile,
        cold_start_max_sweep=cold_start_max_sweep,
    )


def _coordinator(
    tmp_path: Path,
    gateway: Any,
    clock: FakeClock | None = None,
    config: CoordinatorConfig | None = None,
    **kw: Any,
) -> PipelineCoordinator:
    return PipelineCoordinator(
        config or _config(tmp_path),
        clock=clock or FakeClock(),
        cold_start_gateway=gateway,
        **kw,
    )


def _read_log_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for f in sorted(directory.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _units(*specs: tuple[str, bool] | tuple[str, bool, str]) -> InfraAuditResult:
    units: list[InfraUnit] = []
    for spec in specs:
        if len(spec) == 2:
            name, live = spec
            units.append(InfraUnit(name=name, live=live))
        else:
            name, live, expected = spec
            units.append(InfraUnit(name=name, live=live, expected=expected))
    return InfraAuditResult(units=tuple(units))


# ── Code AC: FakeColdStartGateway implements the Protocol ─────────────


def test_fake_gateway_satisfies_coldstart_protocol() -> None:
    """The fake (and the degraded fake) structurally satisfy the runtime
    Protocol, so the seam tested here is the same one production wires."""
    assert isinstance(FakeColdStartGateway(), ColdStartGateway)
    assert isinstance(DegradedGateway(), ColdStartGateway)
    # Every method named in the OP-1551 Code AC is present on the fake.
    for method in (
        "audit_infra", "start_unit", "interrupted_tickets", "has_live_runner",
        "gerrit_change_mergeable", "branch_has_commits", "mark_resumable",
        "ticket_labels", "transition_under_review", "reset_to_todo", "stale_sweep_plan",
        "remove_label", "clear_assignee", "mention_operator",
    ):
        assert callable(getattr(FakeColdStartGateway(), method)), method


# ── Code AC: startup() runs the 4 phases in order ─────────────────────


def test_startup_runs_four_phases_in_order_and_emits_lifecycle_events(tmp_path: Path) -> None:
    """startup() walks Startup-1 → L6 → Startup-2 → Startup-3 → Startup-4,
    returns a populated report, and brackets the run with began/complete."""
    gw = FakeColdStartGateway(
        audits=[_units(("coordinator.service", False))],
        start_results={"coordinator.service": True},
        interrupted=[InterruptedTicket(key="OP-700", status="In Progress", stale=True)],
        sweep_plan=StaleSweepPlan(orphan_assignees=("OP-701",)),
    )
    coord = _coordinator(tmp_path, gw)

    report = coord.startup()

    # Populated report across the phases that had work.
    assert report.started_units == ["coordinator.service"]
    assert report.reconciled == [
        {"ticket": "OP-700", "classification": "no-commits-stale", "action": "revert_todo"}
    ]
    assert report.swept == [{"ticket": "OP-701", "sweep": "orphan-assignee", "labels": []}]
    assert report.entered_loop is True
    assert report.phase1_halted is False

    events = [r["event"] for r in _read_log_records(coord.config.decision_log_dir)]
    assert events[0] == COLD_START_BEGAN_EVENT
    assert events[-1] == COLD_START_COMPLETE_EVENT
    assert STEADY_STATE_ENTERED_EVENT in events

    # Relative phase ordering: phase 1 record precedes phase 2 precedes phase 3,
    # and steady-state entry comes after all of them.
    def _first(predicate) -> int:
        recs = _read_log_records(coord.config.decision_log_dir)
        return next(i for i, r in enumerate(recs) if predicate(r))

    i_p1 = _first(lambda r: r.get("phase") == 1)
    i_p2 = _first(lambda r: r.get("phase") == 2)
    i_p3 = _first(lambda r: r.get("phase") == 3)
    i_loop = _first(lambda r: r["event"] == STEADY_STATE_ENTERED_EVENT)
    assert i_p1 < i_p2 < i_p3 < i_loop


# ── Code AC: _startup_1_infra ─────────────────────────────────────────


def test_startup_1_starts_down_units(tmp_path: Path) -> None:
    """Down units are started; a single clean audit means no re-verify."""
    gw = FakeColdStartGateway(
        audits=[_units(("a.service", False), ("b.service", False), ("c.service", True))],
        start_results={"a.service": True, "b.service": True},
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_1_infra(report)

    assert gw.started == ["a.service", "b.service"]
    assert report.started_units == ["a.service", "b.service"]
    assert report.still_down_units == []
    assert report.phase1_halted is False
    assert gw.audit_calls == 1  # no re-verify when nothing stayed down
    assert gw.operator_mentions == []


def test_startup_1_recheck_clears_transient_down_unit(tmp_path: Path) -> None:
    """A unit whose start returns False but is live on re-verify does NOT
    halt — the re-audit is authoritative (started slow, now up)."""
    gw = FakeColdStartGateway(
        audits=[_units(("slow.service", False)), _units(("slow.service", True))],
        start_results={"slow.service": False},
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_1_infra(report)

    assert gw.started == ["slow.service"]
    assert gw.audit_calls == 2  # re-verified because start reported failure
    assert report.still_down_units == []
    assert report.phase1_halted is False
    assert gw.operator_mentions == []


def test_startup_1_halts_and_mentions_operator_when_unit_stays_down(tmp_path: Path) -> None:
    """A hard-down expected-live unit: still down on re-verify → record in
    still_down_units, phase1_halted, @-operator. Must NOT raise, and startup()
    stops before reconcile/sweep (entering a degraded loop is worse)."""
    gw = FakeColdStartGateway(
        audits=[_units(("dead.service", False)), _units(("dead.service", False))],
        start_results={"dead.service": False},
        interrupted=[InterruptedTicket(key="OP-1", stale=True)],
        sweep_plan=StaleSweepPlan(orphan_assignees=("OP-2",)),
    )
    coord = _coordinator(tmp_path, gw)

    report = coord.startup()  # does not raise

    assert report.phase1_halted is True
    assert report.still_down_units == ["dead.service"]
    assert report.entered_loop is False
    # Operator was @-mentioned at high urgency on the infra-level alert.
    assert len(gw.operator_mentions) == 1
    key, _msg, urgency = gw.operator_mentions[0]
    assert (key, urgency) == ("OP", "high")
    # Halt is hard: reconcile + sweep never ran.
    assert gw.transitioned == [] and gw.reset_todo == [] and gw.cleared_assignees == []

    events = [r["event"] for r in _read_log_records(coord.config.decision_log_dir)]
    assert events[-1] == COLD_START_COMPLETE_EVENT
    complete = _read_log_records(coord.config.decision_log_dir)[-1]
    assert complete["halted_at"] == "startup-1"


def test_infra_audit_down_filters_to_expected_live_units() -> None:
    """Startup-1 only receives expected=yes units that are not live."""
    audit = _units(
        ("release-milestone-checker.timer", False, "yes"),
        ("sora-bridge-sync.timer", True, "yes"),
        ("auto-promote-main.service", False, "n-a"),
        ("staging-gate-smoke.timer", False, "gated"),
        ("unknown.service", False, "unexpected"),
    )

    assert audit.down == ("release-milestone-checker.timer",)


# ── Code AC: _l6_crash_recovery ───────────────────────────────────────


def _seed_log(coord: PipelineCoordinator, records: list[dict[str, Any]]) -> None:
    """Write raw records into the day-partitioned decision log (the L6 reader
    picks the day-file from each record's ``ts``)."""
    for rec in records:
        coord._decision_log.append(rec)


def test_l6_replays_only_last_24h(tmp_path: Path) -> None:
    """Only crashes inside the L6_REPLAY_WINDOW_HOURS window are replayed;
    an older unmatched shutdown is ignored. One event per replayed entry."""
    clock = FakeClock()
    gw = FakeColdStartGateway()
    coord = _coordinator(tmp_path, gw, clock=clock)
    now = clock()
    old_ts = (now - timedelta(hours=L6_REPLAY_WINDOW_HOURS + 2)).isoformat()
    recent_ts = (now - timedelta(hours=3)).isoformat()
    _seed_log(
        coord,
        [
            {"ts": old_ts, "event": "shutdown_began", "decision_id": "old-crash"},
            {"ts": recent_ts, "event": "shutdown_began", "decision_id": "recent-crash"},
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert [c["crashed_drain_id"] for c in report.crash_recoveries] == ["recent-crash"]
    applied = [
        r for r in _read_log_records(coord.config.decision_log_dir)
        if r["event"] == CRASH_RECOVERY_EVENT
    ]
    assert len(applied) == 1
    assert applied[0]["crashed_drain_id"] == "recent-crash"


def test_l6_clean_shutdown_is_not_recovered(tmp_path: Path) -> None:
    """A shutdown_began paired with a matching shutdown_complete (same
    decision_id) was a clean drain — nothing to recover."""
    clock = FakeClock()
    coord = _coordinator(tmp_path, FakeColdStartGateway(), clock=clock)
    ts = (clock() - timedelta(hours=1)).isoformat()
    _seed_log(
        coord,
        [
            {"ts": ts, "event": "shutdown_began", "decision_id": "clean"},
            {"ts": ts, "event": "shutdown_complete", "decision_id": "clean"},
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert report.crash_recoveries == []


def test_l6_resolves_interrupted_actions_by_kind(tmp_path: Path) -> None:
    """The real-action tick preceding an unmatched drain is resolved per kind:
    idempotent relabel/transition → completed (Under-Review transition is
    re-issued); file_ticket → escalated via @-operator; anything else → noop."""
    clock = FakeClock()
    gw = FakeColdStartGateway()
    coord = _coordinator(tmp_path, gw, clock=clock)
    tick_ts = (clock() - timedelta(hours=2)).isoformat()
    drain_ts = (clock() - timedelta(hours=1)).isoformat()
    _seed_log(
        coord,
        [
            {
                "ts": tick_ts,
                "event": "decision_tick",
                "dry_run": False,
                "actions": [
                    {"kind": ACTION_RELABEL, "target": "OP-10"},
                    {
                        "kind": ACTION_TRANSITION,
                        "target": "OP-11",
                        "params": {"to_status": "Under Review"},
                    },
                    {"kind": ACTION_FILE_TICKET, "target": "OP-12"},
                    {"kind": ACTION_NOOP, "target": ""},
                ],
            },
            {"ts": drain_ts, "event": "shutdown_began", "decision_id": "crash-x"},
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    [recovery] = report.crash_recoveries
    resolutions = {(r["kind"], r["target"]): r["resolution"] for r in recovery["resolutions"]}
    assert resolutions[(ACTION_RELABEL, "OP-10")] == "completed"
    assert resolutions[(ACTION_TRANSITION, "OP-11")] == "completed"
    assert resolutions[(ACTION_FILE_TICKET, "OP-12")] == "escalated"
    assert resolutions[(ACTION_NOOP, "")] == "noop"
    # Side effects: the Under-Review transition is re-issued; file_ticket is
    # surfaced to the operator at medium urgency.
    assert gw.transitioned == ["OP-11"]
    assert len(gw.operator_mentions) == 1
    assert gw.operator_mentions[0][0] == "OP-12"
    assert gw.operator_mentions[0][2] == "medium"


def test_l6_replays_missing_tick_intent_under_stable_keys(tmp_path: Path) -> None:
    """An intent with no matching decision_tick is replayed through the action
    layer under the stable keys captured before the original execution."""
    clock = FakeClock()
    executor = RecordingReplayExecutor()
    coord = _coordinator(
        tmp_path,
        FakeColdStartGateway(),
        clock=clock,
        action_executor=executor,
    )
    ts = (clock() - timedelta(hours=1)).isoformat()
    _seed_log(
        coord,
        [
            {
                "ts": ts,
                "event": "tick_intent",
                "decision_id": "intent-missing-outcome",
                "dry_run": False,
                "actions": [
                    {
                        "kind": ACTION_TRANSITION,
                        "target": "OP-1617",
                        "params": {"to_status": "Under Review"},
                        "dry_run": False,
                        "idem_keys": ["coord:transition:OP-1617:ea04b4efdc91378d:transition"],
                    }
                ],
            }
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert executor.calls == [(ACTION_TRANSITION, "OP-1617")]
    [recovery] = report.crash_recoveries
    assert recovery["source_event"] == "tick_intent"
    assert recovery["crashed_decision_id"] == "intent-missing-outcome"
    assert recovery["resolutions"] == [
        {
            "kind": ACTION_TRANSITION,
            "target": "OP-1617",
            "resolution": "completed",
            "idem_keys": ["coord:transition:OP-1617:ea04b4efdc91378d:transition"],
            "action_result": {
                "kind": ACTION_TRANSITION,
                "target": "OP-1617",
                "executed": True,
            },
        }
    ]


def test_l6_tick_intent_with_matching_outcome_is_not_replayed(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = RecordingReplayExecutor()
    coord = _coordinator(
        tmp_path,
        FakeColdStartGateway(),
        clock=clock,
        action_executor=executor,
    )
    ts = (clock() - timedelta(hours=1)).isoformat()
    action = {
        "kind": ACTION_MENTION_OPERATOR,
        "target": "OP-1617",
        "params": {"message": "note"},
        "dry_run": False,
        "idem_keys": ["coord:mention_operator:OP-1617:ed46013ec0574c8e:comment"],
    }
    _seed_log(
        coord,
        [
            {
                "ts": ts,
                "event": "tick_intent",
                "decision_id": "intent-complete",
                "dry_run": False,
                "actions": [action],
            },
            {
                "ts": ts,
                "event": "decision_tick",
                "decision_id": "intent-complete",
                "dry_run": False,
                "actions": [action],
                "action_results": [{"kind": ACTION_MENTION_OPERATOR, "executed": True}],
            },
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert executor.calls == []
    assert report.crash_recoveries == []


def test_l6_tick_intent_outside_idempotency_window_is_skipped(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = RecordingReplayExecutor()
    coord = _coordinator(
        tmp_path,
        FakeColdStartGateway(),
        clock=clock,
        action_executor=executor,
    )
    ts = (clock() - timedelta(hours=L6_REPLAY_WINDOW_HOURS, seconds=1)).isoformat()
    _seed_log(
        coord,
        [
            {
                "ts": ts,
                "event": "tick_intent",
                "decision_id": "intent-expired",
                "dry_run": False,
                "actions": [
                    {
                        "kind": ACTION_TRANSITION,
                        "target": "OP-1617",
                        "params": {"to_status": "Under Review"},
                        "dry_run": False,
                        "idem_keys": ["coord:transition:OP-1617:ea04b4efdc91378d:transition"],
                    }
                ],
            }
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert executor.calls == []
    assert report.crash_recoveries == []


def test_l6_no_preceding_real_action_is_noop(tmp_path: Path) -> None:
    """An unmatched drain with no preceding real-action tick (e.g. only
    dry-run ticks before the crash) resolves to a single noop."""
    clock = FakeClock()
    gw = FakeColdStartGateway()
    coord = _coordinator(tmp_path, gw, clock=clock)
    _seed_log(
        coord,
        [
            {
                "ts": (clock() - timedelta(hours=2)).isoformat(),
                "event": "decision_tick",
                "dry_run": True,  # shadow tick — not a real in-flight action
                "actions": [{"kind": ACTION_RELABEL, "target": "OP-99"}],
            },
            {"ts": (clock() - timedelta(hours=1)).isoformat(),
             "event": "shutdown_began", "decision_id": "crash-y"},
        ],
    )
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    [recovery] = report.crash_recoveries
    assert [r["resolution"] for r in recovery["resolutions"]] == ["noop"]
    assert gw.transitioned == []
    assert gw.operator_mentions == []


def test_l6_empty_or_missing_log_is_noop(tmp_path: Path) -> None:
    """A missing/empty decision log replays nothing (fresh host, first boot)."""
    coord = _coordinator(tmp_path, FakeColdStartGateway())
    report = ColdStartReport()

    coord._l6_crash_recovery(report)

    assert report.crash_recoveries == []


# ── Code AC: _startup_2_reconcile (interrupted-ticket decision tree) ──


def test_startup_2_reconcile_decision_tree(tmp_path: Path) -> None:
    """Each §3.3 branch routes correctly: live-runner → leave; mergeable →
    Under Review; branch commits → resumable; stale+no-work → revert To Do;
    ambiguous (recent, no work) → @-operator."""
    interrupted = [
        InterruptedTicket(key="OP-LIVE", stale=True),
        InterruptedTicket(key="OP-MERGE", stale=True),
        InterruptedTicket(key="OP-BRANCH", stale=True),
        InterruptedTicket(key="OP-STALE", stale=True),
        InterruptedTicket(key="OP-AMBIG", stale=False),
    ]
    gw = FakeColdStartGateway(
        interrupted=interrupted,
        live_runners=("OP-LIVE",),
        mergeable=("OP-MERGE",),
        with_commits=("OP-BRANCH",),
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_2_reconcile(report)

    by_key = {r["ticket"]: (r["classification"], r["action"]) for r in report.reconciled}
    assert by_key["OP-LIVE"] == ("live-runner", "leave")
    assert by_key["OP-MERGE"] == ("gerrit-mergeable", "under_review")
    assert by_key["OP-BRANCH"] == ("branch-has-commits", "resume")
    assert by_key["OP-STALE"] == ("no-commits-stale", "revert_todo")
    assert by_key["OP-AMBIG"] == ("ambiguous", "operator")

    assert gw.transitioned == ["OP-MERGE"]
    assert gw.marked_resumable == ["OP-BRANCH"]
    assert gw.reset_todo == ["OP-STALE"]
    assert [m[0] for m in gw.operator_mentions] == ["OP-AMBIG"]
    assert gw.operator_mentions[0][2] == "medium"
    # The live-runner ticket is left completely untouched.
    assert "OP-LIVE" not in gw.transitioned + gw.marked_resumable + gw.reset_todo


def test_startup_2_mergeable_beats_branch_commits(tmp_path: Path) -> None:
    """(b) precedes (a): a mergeable change means the work is finished and only
    missed its transition — that beats re-running a branch with commits."""
    gw = FakeColdStartGateway(
        interrupted=[InterruptedTicket(key="OP-BOTH", stale=True)],
        mergeable=("OP-BOTH",),
        with_commits=("OP-BOTH",),
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_2_reconcile(report)

    assert gw.transitioned == ["OP-BOTH"]
    assert gw.marked_resumable == []
    assert report.reconciled[0]["classification"] == "gerrit-mergeable"


def test_startup_2_live_coord_guard_skips_each_mutator(tmp_path: Path) -> None:
    """OP-1622: Startup-2 re-reads labels at the mutation boundary and skips
    every reconcile mutator when coord-skip / coord-quarantine appeared after
    the initial interrupted-ticket snapshot."""
    gw = FakeColdStartGateway(
        interrupted=[
            InterruptedTicket(key="OP-MERGE", stale=True),
            InterruptedTicket(key="OP-BRANCH", stale=True),
            InterruptedTicket(key="OP-STALE", stale=True),
            InterruptedTicket(key="OP-AMBIG", stale=False),
        ],
        mergeable=("OP-MERGE",),
        with_commits=("OP-BRANCH",),
        live_labels={
            "OP-MERGE": ("coord-skip",),
            "OP-BRANCH": ("coord-quarantine",),
            "OP-STALE": ("coord-skip",),
            "OP-AMBIG": ("coord-quarantine",),
        },
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_2_reconcile(report)

    assert gw.transitioned == []
    assert gw.marked_resumable == []
    assert gw.reset_todo == []
    assert gw.operator_mentions == []
    assert {r["ticket"]: r["action"] for r in report.reconciled} == {
        "OP-MERGE": "coord_skip",
        "OP-BRANCH": "coord_skip",
        "OP-STALE": "coord_skip",
        "OP-AMBIG": "coord_skip",
    }


# ── Code AC: _startup_3_sweep ─────────────────────────────────────────


def test_startup_3_sweep_applies_plan(tmp_path: Path) -> None:
    """Stale claims → drop each claim label + clear assignee; orphan assignees
    → clear assignee; resolved-waiting → drop each dead waiting marker."""
    plan = StaleSweepPlan(
        stale_claims={"OP-A": ("claim:default:OP-A", "claim:lock:OP-A")},
        orphan_assignees=("OP-B",),
        resolved_waiting={"OP-C": ("runner-blocked:waiting-OP-50",)},
    )
    gw = FakeColdStartGateway(sweep_plan=plan)
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_3_sweep(report)

    assert ("OP-A", "claim:default:OP-A") in gw.removed_labels
    assert ("OP-A", "claim:lock:OP-A") in gw.removed_labels
    assert ("OP-C", "runner-blocked:waiting-OP-50") in gw.removed_labels
    # Stale-claim ticket also has its assignee cleared; orphan does too.
    assert gw.cleared_assignees == ["OP-A", "OP-B"]

    sweeps = {(r["ticket"], r["sweep"]) for r in report.swept}
    assert sweeps == {("OP-A", "stale-claim"), ("OP-B", "orphan-assignee"), ("OP-C", "resolved-waiting")}


def test_startup_3_live_coord_guard_skips_each_mutator(tmp_path: Path) -> None:
    """OP-1622: Startup-3 re-reads labels before remove_label / clear_assignee
    and records coord_skip instead of mutating guarded tickets."""
    plan = StaleSweepPlan(
        stale_claims={"OP-A": ("claim:default:OP-A",)},
        orphan_assignees=("OP-B",),
        resolved_waiting={"OP-C": ("runner-blocked:waiting-OP-50",)},
    )
    gw = FakeColdStartGateway(
        sweep_plan=plan,
        live_labels={
            "OP-A": ("coord-skip",),
            "OP-B": ("coord-quarantine",),
            "OP-C": ("coord-skip",),
        },
    )
    coord = _coordinator(tmp_path, gw)
    report = ColdStartReport()

    coord._startup_3_sweep(report)

    assert gw.removed_labels == []
    assert gw.cleared_assignees == []
    assert {(r["ticket"], r["sweep"]) for r in report.swept} == {
        ("OP-A", "coord_skip"),
        ("OP-B", "coord_skip"),
        ("OP-C", "coord_skip"),
    }


def test_cold_start_per_phase_caps_limit_mutators(tmp_path: Path) -> None:
    """OP-1618: Startup-1/2/3 caps are enforced at mutator call sites."""
    cfg = _config(
        tmp_path,
        cold_start_max_infra=1,
        cold_start_max_reconcile=1,
        cold_start_max_sweep=1,
    )

    infra = FakeColdStartGateway(
        audits=[
            _units(("a.service", False), ("b.service", False)),
            _units(("b.service", False)),
        ],
        start_results={"a.service": True, "b.service": True},
    )
    coord = _coordinator(tmp_path, infra, config=cfg)
    report = ColdStartReport()
    coord._startup_1_infra(report)
    assert infra.started == ["a.service"]
    assert report.phase1_halted is True

    reconcile = FakeColdStartGateway(
        interrupted=[
            InterruptedTicket(key="OP-MERGE", stale=True),
            InterruptedTicket(key="OP-BRANCH", stale=True),
            InterruptedTicket(key="OP-STALE", stale=True),
        ],
        mergeable=("OP-MERGE",),
        with_commits=("OP-BRANCH",),
    )
    coord = _coordinator(tmp_path, reconcile, config=cfg)
    report = ColdStartReport()
    coord._startup_2_reconcile(report)
    assert reconcile.transitioned == ["OP-MERGE"]
    assert reconcile.marked_resumable == []
    assert reconcile.reset_todo == []
    assert [r["action"] for r in report.reconciled] == [
        "under_review",
        "phase_cap",
        "phase_cap",
    ]

    sweep = FakeColdStartGateway(
        sweep_plan=StaleSweepPlan(
            stale_claims={"OP-A": ("claim:default:OP-A",)},
            orphan_assignees=("OP-B",),
        )
    )
    coord = _coordinator(tmp_path, sweep, config=cfg)
    report = ColdStartReport()
    coord._startup_3_sweep(report)
    assert sweep.removed_labels == [("OP-A", "claim:default:OP-A")]
    assert sweep.cleared_assignees == []
    assert report.swept == [
        {"ticket": "OP-A", "sweep": "stale-claim", "labels": ["claim:default:OP-A"]}
    ]


# ── Code AC: Fail-open ────────────────────────────────────────────────


def test_startup_reaches_loop_with_fully_degraded_gateway(tmp_path: Path) -> None:
    """A degraded collaborator (every method yields empty/conservative) never
    wedges startup: all 4 phases run as no-ops and entered_loop is reached."""
    coord = _coordinator(tmp_path, DegradedGateway())

    report = coord.startup()

    assert report.entered_loop is True
    assert report.phase1_halted is False
    assert report.started_units == []
    assert report.reconciled == []
    assert report.swept == []
    assert report.crash_recoveries == []


def test_production_gateway_swallows_backend_failures(monkeypatch, tmp_path: Path) -> None:
    """The production gateway is the layer that makes the seam fail-open: a
    backend that is unreachable (JIRA client absent) or raises is swallowed
    into a safe default rather than propagating out of startup. This is the
    contract the orchestrator relies on (ADR-0021 §3.3 fail-safe)."""
    gw = JiraDispatchColdStartGateway(repo_root=tmp_path)

    # Audit with no deployment-audit.sh present → empty result, no subprocess.
    assert gw.audit_infra() == InfraAuditResult()

    # JIRA-backed reads/writes with no client degrade quietly.
    gw._client = None
    gw._client_tried = True  # short-circuit lazy make_client (no network)
    assert gw.interrupted_tickets() == []
    assert gw.stale_sweep_plan() == StaleSweepPlan()
    assert gw.ticket_labels("OP-1") == ()
    # Void methods must not raise even with no client behind them.
    gw.transition_under_review("OP-1")
    gw.reset_to_todo("OP-1")
    gw.mark_resumable("OP-1")
    gw.remove_label("OP-1", "claim:default:OP-1")
    gw.clear_assignee("OP-1")
    gw.mention_operator("OP-1", "down", urgency="high")

    # A client present but whose request path raises is also swallowed.
    class _Boom:
        project_key = "OP"

    gw._client = _Boom()

    def _raise(*a, **k):
        raise RuntimeError("jira down")

    from backend.agents import jira_dispatch

    monkeypatch.setattr(jira_dispatch, "_request", _raise)
    assert gw.interrupted_tickets() == []
    assert gw.stale_sweep_plan() == StaleSweepPlan()
    assert gw.ticket_labels("OP-1") == ()


# ── Code AC: helper coverage ──────────────────────────────────────────


def test_run_deployment_audit_parses_units(tmp_path: Path) -> None:
    """run_deployment_audit runs the audit script and maps systemd-unit /
    systemd-timer JSONL rows to InfraUnits (live = status == 'OK', expected
    from the row); non-unit rows are ignored."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "deployment-audit.sh"
    rows = {
        "rows": [
            {
                "kind": "systemd-unit",
                "name": "coordinator.service",
                "status": "OK",
                "expected": "yes",
            },
            {
                "kind": "systemd-unit",
                "name": "bridge.service",
                "status": "FAIL",
                "expected": "yes",
            },
            {
                "kind": "systemd-timer",
                "name": "audit.timer",
                "status": "OK",
                "expected": "yes",
            },
            {
                "kind": "systemd-unit",
                "name": "auto-promote-main.service",
                "status": "FAIL",
                "expected": "n-a",
            },
            {
                "kind": "systemd-timer",
                "name": "staging-gate-smoke.timer",
                "status": "FAIL",
                "expected": "gated",
            },
            {
                "kind": "systemd-unit",
                "name": "missing-expected.service",
                "status": "FAIL",
            },
            {
                "kind": "systemd-unit",
                "name": "unknown-expected.service",
                "status": "FAIL",
                "expected": "unknown",
            },
            {"kind": "http", "name": "api", "status": "OK"},
        ]
    }
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' '{json.dumps(rows)}' > \"$DEPLOYMENT_AUDIT_JSONL_LOG\"\n",
        encoding="utf-8",
    )

    result = run_deployment_audit(repo_root=tmp_path, timeout_seconds=15.0)

    assert {u.name for u in result.units} == {
        "coordinator.service",
        "bridge.service",
        "audit.timer",
        "auto-promote-main.service",
        "staging-gate-smoke.timer",
        "missing-expected.service",
        "unknown-expected.service",
    }
    assert {u.name: u.expected for u in result.units} == {
        "coordinator.service": "yes",
        "bridge.service": "yes",
        "audit.timer": "yes",
        "auto-promote-main.service": "n-a",
        "staging-gate-smoke.timer": "gated",
        "missing-expected.service": None,
        "unknown-expected.service": "unknown",
    }
    assert result.down == ("bridge.service",)


def test_run_deployment_audit_missing_script_is_empty(tmp_path: Path) -> None:
    """No deployment-audit.sh → empty result (degrade, never raise)."""
    assert run_deployment_audit(repo_root=tmp_path) == InfraAuditResult()


def test_last_json_line_variants(tmp_path: Path) -> None:
    """_last_json_line returns the last non-blank JSON object, tolerating
    trailing blank lines; a missing file or a torn last line yields None."""
    # Missing file.
    assert _last_json_line(tmp_path / "nope.jsonl") is None

    # Empty file.
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _last_json_line(empty) is None

    # Last valid object wins; trailing blank lines are skipped.
    good = tmp_path / "good.jsonl"
    good.write_text(
        json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2, "rows": []}) + "\n\n\n",
        encoding="utf-8",
    )
    assert _last_json_line(good) == {"n": 2, "rows": []}

    # Truncated/torn last line → None (cannot trust a half-written record).
    torn = tmp_path / "torn.jsonl"
    torn.write_text(json.dumps({"n": 1}) + '\n{"rows": [trunc', encoding="utf-8")
    assert _last_json_line(torn) is None


def test_interrupted_tickets_jql_excludes_operator_intent_and_fetches_labels(
    monkeypatch, tmp_path: Path
) -> None:
    """OP-1622: Startup-2 selection excludes operator-intent labels and fetches
    labels so the gateway can defensively drop any guarded issue in the
    returned snapshot."""
    from backend.agents import jira_dispatch

    captured: dict[str, Any] = {}

    def _request(client, method, path, payload):
        captured.update({"method": method, "path": path, "payload": payload})
        return {
            "issues": [
                {
                    "key": "OP-PLAIN",
                    "fields": {
                        "status": {"name": "In Progress"},
                        "updated": "2026-05-20T00:00:00.000+0000",
                        "labels": [],
                    },
                },
                {
                    "key": "OP-SKIP",
                    "fields": {
                        "status": {"name": "In Progress"},
                        "updated": "2026-05-20T00:00:00.000+0000",
                        "labels": ["coord-skip"],
                    },
                },
                {
                    "key": "OP-TIER-X",
                    "fields": {
                        "status": {"name": "In Progress"},
                        "updated": "2026-05-20T00:00:00.000+0000",
                        "labels": ["tier:X"],
                    },
                },
                {
                    "key": "OP-META",
                    "fields": {
                        "status": {"name": "In Progress"},
                        "updated": "2026-05-20T00:00:00.000+0000",
                        "labels": ["priority:meta"],
                    },
                },
            ]
        }

    monkeypatch.setattr(jira_dispatch, "_request", _request)

    class _Client:
        project_key = "OP"

    gw = JiraDispatchColdStartGateway(repo_root=tmp_path)
    gw._client = _Client()
    gw._client_tried = True

    tickets = gw.interrupted_tickets()

    assert [ticket.key for ticket in tickets] == ["OP-PLAIN"]
    assert tickets[0].labels == ()
    jql = captured["payload"]["jql"]
    assert 'labels not in ("coord-skip","coord-quarantine","tier:X","priority:meta","type:meta")' in jql
    assert captured["payload"]["fields"] == ["status", "updated", "labels"]


def test_build_stale_sweep_plan_classifies_hygiene(monkeypatch, tmp_path: Path) -> None:
    """_build_stale_sweep_plan turns a JIRA snapshot into the three hygiene
    classes: orphaned claim:* with no live runner, To-Do bot-assigned with no
    claim, and waiting-X markers whose blocker X is already published."""
    from backend.agents import jira_dispatch

    captured: dict[str, Any] = {}
    issues = {
        "issues": [
            {
                "key": "OP-A",
                "fields": {
                    "status": {"name": "In Progress"},
                    "labels": ["claim:default:OP-A"],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-B",
                "fields": {
                    "status": {"name": "To Do"},
                    "labels": [],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-C",
                "fields": {
                    "status": {"name": "In Progress"},
                    "labels": ["runner-blocked:waiting-OP-50"],
                    "assignee": None,
                },
            },
            {
                "key": "OP-D",
                "fields": {
                    "status": {"name": "In Progress"},
                    "labels": ["coord-skip", "claim:default:OP-D"],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-E",
                "fields": {
                    "status": {"name": "Published"},
                    "labels": ["claim:default:OP-E"],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-F",
                "fields": {
                    "status": {"name": "Archived"},
                    "labels": ["claim:default:OP-F"],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-G",
                "fields": {
                    "status": {"name": "In Progress"},
                    "labels": ["coord-quarantine", "claim:default:OP-G"],
                    "assignee": {"accountId": "bot"},
                },
            },
            {
                "key": "OP-H",
                "fields": {
                    "status": {"name": "To Do"},
                    "labels": ["tier:X"],
                    "assignee": {"accountId": "bot"},
                },
            },
        ]
    }

    def _request(client, method, path, payload):
        captured.update({"method": method, "path": path, "payload": payload})
        return issues

    monkeypatch.setattr(jira_dispatch, "_request", _request)
    monkeypatch.setattr(
        jira_dispatch, "get_issue_status",
        lambda client, key: "Published" if key == "OP-50" else "In Progress",
    )
    # No live runner for any ticket (avoid scanning /proc).
    monkeypatch.setattr(pc, "_ticket_has_live_runner", lambda key, repo_root=None: False)

    class _Client:
        project_key = "OP"

    plan = _build_stale_sweep_plan(_Client(), repo_root=tmp_path)

    assert plan.stale_claims == {"OP-A": ("claim:default:OP-A",)}
    assert plan.orphan_assignees == ("OP-B",)
    assert plan.resolved_waiting == {"OP-C": ("runner-blocked:waiting-OP-50",)}
    assert 'labels not in ("coord-skip","coord-quarantine","tier:X")' in captured["payload"]["jql"]


# ── OP-1555: shadow-gate cold-start (observe-only canary) ─────────────
#
# Closes the cold-start observe-only gap (sibling of OP-1552): when
# acting=False the ColdStartGateway must RECORD would-be reconcile/sweep/
# infra actions to the decision log but call NO live mutator. Mirrors the
# ShadowActionExecutor / sprint_replan(shadow=not acting) wiring.


def _full_scenario_gateway() -> FakeColdStartGateway:
    """A gateway whose world exercises every cold-start mutator at once:
    a down unit (start_unit), the four §3.3 reconcile branches that mutate
    (transition / mark_resumable / reset_to_todo / @-operator), and all three
    Startup-3 sweep classes (remove_label / clear_assignee)."""
    return FakeColdStartGateway(
        audits=[_units(("coordinator.service", False))],
        # start would *fail* if actually invoked — proves shadow never calls it.
        start_results={"coordinator.service": False},
        interrupted=[
            InterruptedTicket(key="OP-MERGE", stale=True),
            InterruptedTicket(key="OP-BRANCH", stale=True),
            InterruptedTicket(key="OP-STALE", stale=True),
            InterruptedTicket(key="OP-AMBIG", stale=False),
        ],
        mergeable=("OP-MERGE",),
        with_commits=("OP-BRANCH",),
        sweep_plan=StaleSweepPlan(
            stale_claims={"OP-A": ("claim:default:OP-A",)},
            orphan_assignees=("OP-B",),
            resolved_waiting={"OP-C": ("runner-blocked:waiting-OP-50",)},
        ),
    )


def test_shadow_gateway_satisfies_protocol_and_forwards_reads(tmp_path: Path) -> None:
    """ShadowColdStartGateway structurally satisfies the Protocol, advertises
    acting=False, forwards every read to its delegate, and routes every
    mutator to a record-only no-op (the delegate sees zero mutator calls)."""
    fake = _full_scenario_gateway()
    gw = ShadowColdStartGateway(fake)

    assert isinstance(gw, ColdStartGateway)
    assert gw.acting is False

    # Reads forwarded unchanged.
    assert gw.audit_infra() == fake.audit_infra()  # delegate consulted
    assert gw.interrupted_tickets() == fake.interrupted_tickets()
    assert gw.stale_sweep_plan() == fake.stale_sweep_plan()
    assert gw.gerrit_change_mergeable("OP-MERGE") is True
    assert gw.branch_has_commits("OP-BRANCH") is True
    assert gw.has_live_runner("OP-MERGE") is False
    assert gw.ticket_labels("OP-MERGE") == ()

    # Mutators record-only: delegate is never touched, start_unit reports
    # success so an observe-only boot proceeds rather than halting.
    assert gw.start_unit("coordinator.service") is True
    gw.mark_resumable("OP-BRANCH")
    gw.transition_under_review("OP-MERGE")
    gw.reset_to_todo("OP-STALE")
    gw.remove_label("OP-A", "claim:default:OP-A")
    gw.clear_assignee("OP-B")
    gw.mention_operator("OP-AMBIG", "ambiguous", urgency="medium")

    assert fake.started == []
    assert fake.marked_resumable == []
    assert fake.transitioned == []
    assert fake.reset_todo == []
    assert fake.removed_labels == []
    assert fake.cleared_assignees == []
    assert fake.operator_mentions == []


def test_shadow_boot_records_actions_but_fires_zero_mutators(tmp_path: Path) -> None:
    """Integration AC: a shadow boot (acting=False) drives the full 4-phase
    recovery to its loop, the wrapped gateway receives ZERO mutator calls, yet
    cold_start_began/complete + the per-phase would-be-action records ARE
    written to the decision log."""
    fake = _full_scenario_gateway()
    coord = _coordinator(tmp_path, ShadowColdStartGateway(fake))

    report = coord.startup()

    # Boot reached the loop (the down unit did NOT wedge Startup-1 — shadow
    # start_unit reports success and records the would-be start).
    assert report.entered_loop is True
    assert report.phase1_halted is False
    assert report.started_units == ["coordinator.service"]

    # ZERO live mutator calls on the wrapped gateway.
    assert fake.started == []
    assert fake.marked_resumable == []
    assert fake.transitioned == []
    assert fake.reset_todo == []
    assert fake.removed_labels == []
    assert fake.cleared_assignees == []
    assert fake.operator_mentions == []
    # But the reads happened — observation is the point.
    assert fake.audit_calls >= 1

    # The would-be actions ARE recorded in the report + decision log.
    recon = {r["ticket"]: (r["classification"], r["action"]) for r in report.reconciled}
    assert recon["OP-MERGE"] == ("gerrit-mergeable", "under_review")
    assert recon["OP-BRANCH"] == ("branch-has-commits", "resume")
    assert recon["OP-STALE"] == ("no-commits-stale", "revert_todo")
    assert recon["OP-AMBIG"] == ("ambiguous", "operator")
    assert {r["ticket"] for r in report.swept} == {"OP-A", "OP-B", "OP-C"}

    records = _read_log_records(coord.config.decision_log_dir)
    events = [r["event"] for r in records]
    assert events[0] == COLD_START_BEGAN_EVENT
    assert events[-1] == COLD_START_COMPLETE_EVENT
    # Phase-2 reconcile + phase-3 sweep would-be-action records are present.
    phase2 = {r["ticket"] for r in records if r.get("phase") == 2}
    phase3 = {r["ticket"] for r in records if r.get("phase") == 3}
    assert {"OP-MERGE", "OP-BRANCH", "OP-STALE", "OP-AMBIG"} <= phase2
    assert {"OP-A", "OP-B", "OP-C"} <= phase3


def test_acting_boot_fires_mutators_as_today(tmp_path: Path) -> None:
    """Integration AC (contrast): the SAME scenario driven by the live gateway
    (acting=True, no shadow wrap) fires every mutator exactly as before."""
    fake = _full_scenario_gateway()
    # start succeeds for the acting path so it does not halt at Startup-1.
    fake._start_results = {"coordinator.service": True}
    coord = _coordinator(tmp_path, fake)

    report = coord.startup()

    assert report.entered_loop is True
    assert fake.started == ["coordinator.service"]
    assert fake.transitioned == ["OP-MERGE"]
    assert fake.marked_resumable == ["OP-BRANCH"]
    assert fake.reset_todo == ["OP-STALE"]
    assert [m[0] for m in fake.operator_mentions] == ["OP-AMBIG"]
    assert ("OP-A", "claim:default:OP-A") in fake.removed_labels
    assert ("OP-C", "runner-blocked:waiting-OP-50") in fake.removed_labels
    assert fake.cleared_assignees == ["OP-A", "OP-B"]


def test_default_cold_start_gateway_shadow_vs_acting(tmp_path: Path) -> None:
    """_default_cold_start_gateway returns the observe-only ShadowColdStartGateway
    by default (acting=False) and the live JiraDispatchColdStartGateway only
    when acting=True."""
    cfg = _config(tmp_path, jira_agent_class="subscription-claude")
    shadow = _default_cold_start_gateway(cfg)
    assert isinstance(shadow, ShadowColdStartGateway)
    assert isinstance(shadow._delegate, JiraDispatchColdStartGateway)
    assert shadow._delegate._agent_class == "subscription-claude"

    live = _default_cold_start_gateway(cfg, acting=True)
    assert isinstance(live, JiraDispatchColdStartGateway)
    assert live._agent_class == "subscription-claude"


def test_build_default_coordinator_threads_acting_to_cold_start(
    monkeypatch, tmp_path: Path
) -> None:
    """Code AC: build_default_coordinator threads the single acting switch to the
    cold-start gateway — shadow when not acting, live gateway when acting — the
    same switch that flips sprint_replan(shadow=not acting)."""
    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_hybrid_engine",
        lambda *, decision_log_dir: object(),
    )
    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_default_sprint_replan_handler",
        lambda **kwargs: None,
    )
    cfg = _config(tmp_path)

    shadow_coord = build_default_coordinator(cfg)  # acting=False default
    assert isinstance(shadow_coord._cold_start_gateway, ShadowColdStartGateway)

    acting_coord = build_default_coordinator(
        cfg,
        acting=True,
        action_executor=lambda action, ctx: {"executed": True},
    )
    assert isinstance(acting_coord._cold_start_gateway, JiraDispatchColdStartGateway)

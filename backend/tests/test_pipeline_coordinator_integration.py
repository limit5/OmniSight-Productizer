"""Full pipeline-coordinator integration scenario (OP-1010 / AUDIT-29f-12).

This module keeps the scenario entirely in-memory while exercising the same
daemon seams production wires:

  Code AC
    - Scenario A: runner lacks push capability, coordinator applies a
      capability enable label
    - Scenario B: repeated merger failure escalates with full context
    - Scenario C: stale claim from a dead runner is cleaned and pickable again

  Deploy AC      -> standard pytest under backend/tests, tmp_path + fakes only.
  Integration AC -> Tier-1 + Tier-2 + Personality + Capacity + event sources +
                    action layer all participate in one coordinator flow.
  Exercised AC   -> direct pytest of this file exits 0; production-observation
                    AC remains operator evidence, not unit-testable here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    InfraAuditResult,
    InterruptedTicket,
    PipelineCoordinator,
    StaleSweepPlan,
)
from backend.agents.pipeline_coordinator_capacity import (
    CapacitySnapshot,
    RunnerCapacity,
)
from backend.agents.pipeline_coordinator_llm_consultation import (
    HYBRID_ENGINE_VERSION,
    TIER2_CONSULTED_REASON,
    HybridDecisionEngine,
    Tier2Outcome,
)
from backend.agents.pipeline_coordinator_modes import (
    RESCUE_MODE,
    Level,
    SituationProfile,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_ESCALATE,
    ACTION_RELABEL,
    CAPABILITY_ENABLE_LABEL,
    Action,
    Comment,
    DecisionContext,
    Ticket,
    WorkGraph,
)

NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
STALE = NOW - timedelta(hours=2)


class FakeClock:
    def __init__(self) -> None:
        self.t = NOW

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeBridgeTailer:
    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)

    def poll(self) -> list[dict[str, Any]]:
        if not self.batches:
            return []
        return self.batches.pop(0)


class FakeJiraPoller:
    def __init__(self, *batches: list[dict[str, Any]]) -> None:
        self.batches = list(batches)

    def poll(self) -> list[dict[str, Any]]:
        if not self.batches:
            return []
        return self.batches.pop(0)


class PipelineWorld:
    """In-memory JIRA/capability world shared by event, startup, and actions."""

    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {
            "OP-A": Ticket(
                key="OP-A",
                issuetype="Story",
                labels=("needs-coordinator",),
                comments=(
                    Comment(
                        "[runner-capability-blocked] runner cannot push: "
                        "missing gerrit_push capability"
                    ),
                ),
            ),
            "OP-B": Ticket(
                key="OP-B",
                labels=("needs-coordinator", "coord-mode:rescue"),
                comments=(
                    Comment(
                        "merger failed 4 times; change Iabcdef has conflicts "
                        "in src/a.py and needs operator merge context"
                    ),
                ),
                merger_fail_count=4,
            ),
            "OP-C": Ticket(
                key="OP-C",
                status="To Do",
                assignee="codex-bot",
                labels=("tier:M", "claim:default:0001715518859123456-deadbeef"),
                claim_started_at=STALE,
                has_live_runner=False,
            ),
        }
        self.removed_labels: list[tuple[str, str]] = []
        self.cleared_assignees: list[str] = []
        self.operator_mentions: list[tuple[str, str, str]] = []
        self.action_results: list[dict[str, Any]] = []

    def graph_for_events(self, events: list[dict[str, Any]]) -> WorkGraph:
        keys = [
            str(e.get("ticket_key") or (e.get("payload") or {}).get("key") or "")
            for e in events
        ]
        key = next((k for k in keys if k in self.tickets), "")
        focal = self.tickets.get(key)
        return WorkGraph(focal=focal, tickets=self.tickets)

    def profile_for(
        self,
        ticket: Ticket | None,
    ) -> tuple[SituationProfile | None, str | None]:
        if ticket is None:
            return None, None
        if "coord-mode:rescue" in ticket.labels:
            return (
                SituationProfile(
                    urgency=Level.HIGH,
                    risk=Level.MEDIUM,
                    novelty=Level.MEDIUM,
                    reversibility=Level.LOW,
                    runner_stuck_hours=25.0,
                ),
                RESCUE_MODE,
            )
        return (
            SituationProfile(
                urgency=Level.HIGH,
                risk=Level.LOW,
                novelty=Level.LOW,
                reversibility=Level.HIGH,
            ),
            None,
        )

    def execute(self, action: Action, _ctx: DecisionContext) -> dict[str, Any]:
        if action.kind == ACTION_RELABEL:
            ticket = self.tickets[action.target]
            labels = set(ticket.labels)
            for label in action.params.get("remove", []):
                labels.discard(label)
                self.removed_labels.append((action.target, label))
            for label in action.params.get("add", []):
                labels.add(label)
            self.tickets[action.target] = Ticket(
                key=ticket.key,
                status=ticket.status,
                issuetype=ticket.issuetype,
                areas=ticket.areas,
                labels=tuple(sorted(labels)),
                assignee=ticket.assignee,
                comments=ticket.comments,
                in_progress_since=ticket.in_progress_since,
                claim_started_at=ticket.claim_started_at,
                has_live_runner=ticket.has_live_runner,
                blocked_by=ticket.blocked_by,
                merger_fail_count=ticket.merger_fail_count,
                revert_count_24h=ticket.revert_count_24h,
            )
        elif action.kind == ACTION_ESCALATE:
            self.operator_mentions.append(
                (action.target, str(action.params.get("reason", "")), "high")
            )
        result = {
            "kind": action.kind,
            "target": action.target,
            "executed": True,
            "shadow": False,
        }
        self.action_results.append(result)
        return result

    # ── ColdStartGateway subset used by startup() ──
    def audit_infra(self) -> InfraAuditResult:
        return InfraAuditResult()

    def start_unit(self, unit: str) -> bool:
        return True

    def interrupted_tickets(self) -> list[InterruptedTicket]:
        return []

    def has_live_runner(self, key: str) -> bool:
        return self.tickets[key].has_live_runner

    def gerrit_change_mergeable(self, key: str) -> bool:
        return False

    def branch_has_commits(self, key: str) -> bool:
        return False

    def mark_resumable(self, key: str) -> None:
        return None

    def transition_under_review(self, key: str) -> None:
        return None

    def reset_to_todo(self, key: str) -> None:
        return None

    def stale_sweep_plan(self) -> StaleSweepPlan:
        stale_claims = {
            key: tuple(label for label in ticket.labels if label.startswith("claim:"))
            for key, ticket in self.tickets.items()
            if not ticket.has_live_runner
        }
        return StaleSweepPlan(
            stale_claims={k: v for k, v in stale_claims.items() if v},
        )

    def remove_label(self, key: str, label: str) -> None:
        self.removed_labels.append((key, label))
        ticket = self.tickets[key]
        self.tickets[key] = Ticket(
            key=ticket.key,
            status=ticket.status,
            issuetype=ticket.issuetype,
            areas=ticket.areas,
            labels=tuple(l for l in ticket.labels if l != label),
            assignee=ticket.assignee,
            comments=ticket.comments,
            in_progress_since=ticket.in_progress_since,
            claim_started_at=ticket.claim_started_at,
            has_live_runner=ticket.has_live_runner,
            blocked_by=ticket.blocked_by,
            merger_fail_count=ticket.merger_fail_count,
            revert_count_24h=ticket.revert_count_24h,
        )

    def clear_assignee(self, key: str) -> None:
        self.cleared_assignees.append(key)
        ticket = self.tickets[key]
        self.tickets[key] = Ticket(
            key=ticket.key,
            status=ticket.status,
            issuetype=ticket.issuetype,
            areas=ticket.areas,
            labels=ticket.labels,
            assignee=None,
            comments=ticket.comments,
            in_progress_since=ticket.in_progress_since,
            claim_started_at=ticket.claim_started_at,
            has_live_runner=ticket.has_live_runner,
            blocked_by=ticket.blocked_by,
            merger_fail_count=ticket.merger_fail_count,
            revert_count_24h=ticket.revert_count_24h,
        )

    def mention_operator(self, key: str, message: str, *, urgency: str = "high") -> None:
        self.operator_mentions.append((key, message, urgency))


class ContextEscalatingConsultant:
    """Tier-2 seam returning the operator escalation Scenario B expects."""

    def consult(
        self,
        ctx: DecisionContext,
        *,
        behavior: Any = None,
        trigger: str = "tier1_no_match",
    ) -> Tier2Outcome:
        assert ctx.ticket is not None
        assert behavior is not None and behavior.requires_root_cause is True
        assert ctx.capacity.runners["subscription-codex"].free_slots == 1
        reason = (
            "[merger-repeat-fail] OP-B merger failed 4 times; "
            "full context: change Iabcdef, conflicts in src/a.py, "
            "mode=RescueMode, free_codex_slots=1"
        )
        return Tier2Outcome(
            actions=(Action.escalate(ctx.ticket.key, reason=reason),),
            reason=TIER2_CONSULTED_REASON,
            llm_consultation={
                "trigger": trigger,
                "confidence": "high",
                "decision_rationale": reason,
                "cost_usd": 0.01,
                "lessons_cited": ["L-OP-870-blockedby.md"],
            },
        )


def _config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        heartbeat_interval_seconds=60.0,
        tick_interval_seconds=60.0,
        sweep_interval_seconds=3600.0,
    )


def _read_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_full_pipeline_scenarios_flow_through_coordinator(tmp_path: Path) -> None:
    """Scenarios A/B/C run through startup, event subscribers, engine, actions.

    Sequence:
      1. startup cleans OP-C's dead claim so it is pickable again;
      2. bridge event for OP-A drives a Tier-1 capability-enable relabel;
      3. JIRA event for OP-B drives RescueMode Tier-2 operator escalation.
    """
    clock = FakeClock()
    world = PipelineWorld()
    coord_ref: dict[str, PipelineCoordinator] = {}

    def _work_graph() -> WorkGraph:
        return world.graph_for_events(coord_ref["coord"]._pending_events)

    def _capacity() -> CapacitySnapshot:
        return CapacitySnapshot(
            captured_at=clock(),
            runners={
                "subscription-codex": RunnerCapacity(
                    "subscription-codex",
                    free_slots=1,
                    budget_remaining=12.0,
                    tokens_in_current_week=1200,
                    weekly_cap=100000,
                )
            },
        )

    engine = HybridDecisionEngine(consultant=ContextEscalatingConsultant())
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=engine,
        clock=clock,
        capacity_provider=_capacity,
        work_graph_provider=_work_graph,
        action_executor=world.execute,
        cold_start_gateway=world,
        bridge_tailer=FakeBridgeTailer(
            [
                {
                    "source": "bridge",
                    "trigger": "bridge-event:runner-capability-blocked",
                    "ticket_key": "OP-A",
                    "payload": {"type": "runner-capability-blocked", "key": "OP-A"},
                }
            ],
            [],
        ),
        jira_poller=FakeJiraPoller(
            [],
            [
                {
                    "source": "jira",
                    "trigger": "jira-poll",
                    "ticket_key": "OP-B",
                    "payload": {"key": "OP-B", "fields": {"summary": "merge stuck"}},
                }
            ],
        ),
    )
    coord_ref["coord"] = coord

    def _current_situation() -> tuple[SituationProfile | None, str | None]:
        return world.profile_for(coord._work_graph.focal)

    coord._current_situation = _current_situation  # type: ignore[method-assign]

    startup_report = coord.startup()
    assert startup_report.entered_loop is True
    assert startup_report.swept == [
        {
            "ticket": "OP-C",
            "sweep": "stale-claim",
            "labels": ["claim:default:0001715518859123456-deadbeef"],
        }
    ]
    assert world.tickets["OP-C"].assignee is None
    assert not any(label.startswith("claim:") for label in world.tickets["OP-C"].labels)

    scenario_a = coord.run_once()
    clock.advance(60)
    scenario_b = coord.run_once()

    assert scenario_a.tier == 1
    assert scenario_a.rule_name == "capability-blocked-known-locale"
    assert scenario_a.actions[0].kind == ACTION_RELABEL
    assert CAPABILITY_ENABLE_LABEL in world.tickets["OP-A"].labels

    assert scenario_b.tier == 2
    assert scenario_b.reason == TIER2_CONSULTED_REASON
    assert scenario_b.mode == RESCUE_MODE
    assert scenario_b.actions[0].kind == ACTION_ESCALATE
    assert "full context: change Iabcdef" in scenario_b.actions[0].params["reason"]

    records = _read_records(coord.config.decision_log_dir)
    sequence = [
        (
            r["event"],
            r.get("ticket_key") or r.get("ticket"),
            r.get("rule_name"),
            r.get("tier"),
            r.get("mode"),
        )
        for r in records
        if r["event"] in ("startup_phase", "coordinator_source_event", "decision_tick")
    ]
    assert sequence == [
        ("startup_phase", "OP-C", None, None, None),
        ("coordinator_source_event", "OP-A", None, None, None),
        (
            "decision_tick",
            None,
            "capability-blocked-known-locale",
            1,
            "ExecutionMode",
        ),
        ("coordinator_source_event", "OP-B", None, None, None),
        ("decision_tick", None, None, 2, RESCUE_MODE),
    ]

    tick_a, tick_b = [r for r in records if r["event"] == "decision_tick"]
    assert tick_a["engine_version"] == HYBRID_ENGINE_VERSION
    assert tick_a["actions"][0]["params"]["add"] == [CAPABILITY_ENABLE_LABEL]
    assert tick_a["action_results"] == [
        {"kind": ACTION_RELABEL, "target": "OP-A", "executed": True, "shadow": False}
    ]
    assert tick_b["mode_behavior"]["tier2_policy"] == "always"
    assert tick_b["mode_behavior"]["llm_context_hops"] == 10
    assert tick_b["llm_consultation"]["trigger"] == "mode_requires_tier2"
    assert tick_b["llm_consultation"]["lessons_cited"] == ["L-OP-870-blockedby.md"]
    assert tick_b["action_results"] == [
        {"kind": ACTION_ESCALATE, "target": "OP-B", "executed": True, "shadow": False}
    ]
    assert world.operator_mentions == [
        (
            "OP-B",
            "[merger-repeat-fail] OP-B merger failed 4 times; full context: "
            "change Iabcdef, conflicts in src/a.py, mode=RescueMode, "
            "free_codex_slots=1",
            "high",
        )
    ]

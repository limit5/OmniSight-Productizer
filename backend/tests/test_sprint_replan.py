"""Sprint re-plan shadow-gate tests (OP-1552 / AUDIT-29f-coord).

Coverage map:

  Code AC
    shadow mode records but does not mutate  → test_shadow_mode_records_actions_without_gateway_calls
    acting mode mutates through gateway      → test_acting_mode_calls_gateway
    unsupported action kinds dropped         → test_unsupported_replan_actions_are_dropped
    default coordinator shadow wiring        → test_build_default_coordinator_shadow_wires_replan

  Integration AC
    budget-degrade path                      → test_budget_degrade_alert_is_recorded_in_shadow
    capacity-insufficient path               → test_capacity_insufficient_adds_operator_alert
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    build_default_coordinator,
)
from backend.agents.pipeline_coordinator_capacity import (
    CapacitySnapshot,
    RunnerCapacity,
)
from backend.agents.pipeline_coordinator_llm_consultation import (
    BudgetGuard,
    LLMConsultationConfig,
    Tier2Consultant,
    Tier2Outcome,
)
from backend.agents.pipeline_coordinator_rules import (
    ACTION_FILE_TICKET,
    ACTION_MENTION_OPERATOR,
    ACTION_RELABEL,
    ACTION_TRANSITION,
    Action,
    DecisionContext,
)
from backend.agents.sprint_replan import (
    DEFAULT_OPERATOR_TICKET,
    PickableTicket,
    SprintReplanHandler,
)

NOW = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FakeGateway:
    pickable: Sequence[PickableTicket]
    calls: list[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = field(default_factory=list)

    def list_pickable(self, max_results: int) -> Sequence[PickableTicket]:
        return tuple(self.pickable[:max_results])

    def add_label(self, key: str, label: str) -> None:
        self.calls.append(("add_label", (key, label), {}))

    def remove_label(self, key: str, label: str) -> None:
        self.calls.append(("remove_label", (key, label), {}))

    def file_scope_review_ticket(
        self,
        *,
        source_key: str,
        target_area: str,
        description: str,
    ) -> str:
        self.calls.append(
            (
                "file_scope_review_ticket",
                (),
                {
                    "source_key": source_key,
                    "target_area": target_area,
                    "description": description,
                },
            )
        )
        return "OP-SCOPE-1"

    def mention_operator(self, key: str, message: str, *, urgency: str) -> None:
        self.calls.append(
            ("mention_operator", (key, message), {"urgency": urgency})
        )


class _FakeConsultant:
    def __init__(self, actions: Sequence[Action]) -> None:
        self._actions = tuple(actions)
        self.contexts: list[DecisionContext] = []

    def consult(
        self,
        ctx: DecisionContext,
        *,
        behavior: object | None = None,
        trigger: str = "",
    ) -> Tier2Outcome:
        self.contexts.append(ctx)
        return Tier2Outcome(
            actions=self._actions,
            reason="fake",
            llm_consultation={"trigger": trigger, "confidence": "high"},
        )


def _capacity(slots: int) -> CapacitySnapshot:
    return CapacitySnapshot(
        captured_at=NOW,
        runners={"subscription-codex": RunnerCapacity("subscription-codex", free_slots=slots)},
    )


def _handler(
    *,
    actions: Sequence[Action],
    gateway: _FakeGateway,
    slots: int = 3,
    shadow: bool = True,
) -> SprintReplanHandler:
    return SprintReplanHandler(
        gateway=gateway,
        consultant=_FakeConsultant(actions),  # type: ignore[arg-type]
        capacity_provider=lambda: _capacity(slots),
        clock=lambda: NOW,
        shadow=shadow,
    )


def test_shadow_mode_records_actions_without_gateway_calls() -> None:
    gateway = _FakeGateway((PickableTicket("OP-1"),))
    handler = _handler(
        gateway=gateway,
        actions=(
            Action.relabel("OP-1", add=("coord-picked",), remove=("todo",)),
            Action.file_ticket(
                blocking="OP-1",
                target_area="backend",
                description="ambiguous boundary",
            ),
            Action.mention_operator(
                DEFAULT_OPERATOR_TICKET,
                message="needs operator review",
                urgency="high",
            ),
        ),
        shadow=True,
    )

    first = handler.run()
    second = handler.run()

    assert gateway.calls == []
    assert first.actions == second.actions
    assert all(row["executed"] is False for row in first.action_results)
    assert all(row["shadow"] is True for row in first.action_results)
    assert [row["kind"] for row in first.action_results] == [
        ACTION_RELABEL,
        ACTION_FILE_TICKET,
        ACTION_MENTION_OPERATOR,
    ]
    assert first.scope_review_tickets == ()


def test_acting_mode_calls_gateway() -> None:
    gateway = _FakeGateway((PickableTicket("OP-1"),))
    handler = _handler(
        gateway=gateway,
        actions=(
            Action.relabel("OP-1", add=("coord-picked",), remove=("todo",)),
            Action.file_ticket(
                blocking="OP-1",
                target_area="backend",
                description="ambiguous boundary",
            ),
            Action.mention_operator(
                DEFAULT_OPERATOR_TICKET,
                message="needs operator review",
                urgency="high",
            ),
        ),
        shadow=False,
    )

    result = handler.run()

    assert [call[0] for call in gateway.calls] == [
        "add_label",
        "remove_label",
        "file_scope_review_ticket",
        "mention_operator",
    ]
    assert all(row["executed"] is True for row in result.action_results)
    assert result.scope_review_tickets == ("OP-SCOPE-1",)


def test_unsupported_replan_actions_are_dropped() -> None:
    gateway = _FakeGateway((PickableTicket("OP-1"),))
    handler = _handler(
        gateway=gateway,
        actions=(Action.transition("OP-1", to_status="In Progress"),),
        shadow=False,
    )

    result = handler.run()

    assert result.actions == ()
    assert result.action_results == ()
    assert gateway.calls == []


def test_budget_degrade_alert_is_recorded_in_shadow() -> None:
    gateway = _FakeGateway((PickableTicket("OP-1"),))
    consultant = Tier2Consultant(
        config=LLMConsultationConfig(daily_budget_usd=0.0),
        budget=BudgetGuard(0.0, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    handler = SprintReplanHandler(
        gateway=gateway,
        consultant=consultant,
        capacity_provider=lambda: _capacity(1),
        clock=lambda: NOW,
        shadow=True,
    )

    result = handler.run()

    assert gateway.calls == []
    assert result.llm_consultation["degraded"] == "budget_cap"
    assert [action.kind for action in result.actions] == [ACTION_MENTION_OPERATOR]
    assert result.actions[0].target == DEFAULT_OPERATOR_TICKET
    assert result.action_results[0]["executed"] is False
    assert result.action_results[0]["shadow"] is True


def test_capacity_insufficient_adds_operator_alert() -> None:
    gateway = _FakeGateway((PickableTicket("OP-1"), PickableTicket("OP-2")))
    handler = _handler(gateway=gateway, actions=(), slots=1, shadow=True)

    result = handler.run()

    assert result.capacity_insufficient is True
    assert [action.kind for action in result.actions] == [ACTION_MENTION_OPERATOR]
    assert result.actions[0].params["urgency"] == "high"
    assert "capacity insufficient" in result.actions[0].params["message"]
    assert gateway.calls == []


def test_build_default_coordinator_shadow_wires_replan(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _fake_engine(*, decision_log_dir: Path):
        captured["decision_log_dir"] = decision_log_dir

        class _Engine:
            engine_version = "fake"

            def evaluate(self, ctx: DecisionContext):
                from backend.agents.pipeline_coordinator_rules import DecisionResult

                return DecisionResult.noop(reason="fake")

        return _Engine()

    def _fake_replan_handler(**kwargs):
        captured["replan_kwargs"] = kwargs
        return None

    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_hybrid_engine",
        _fake_engine,
    )
    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_default_sprint_replan_handler",
        _fake_replan_handler,
    )
    cfg = CoordinatorConfig(
        config_dir=tmp_path,
        heartbeat_path=tmp_path / "heartbeat",
        decision_log_dir=tmp_path / "decision-log",
    )

    build_default_coordinator(cfg)

    assert captured["decision_log_dir"] == cfg.decision_log_dir
    assert captured["replan_kwargs"]["shadow"] is True


def test_build_default_coordinator_acting_requires_shared_switch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_hybrid_engine",
        lambda *, decision_log_dir: object(),
    )
    monkeypatch.setattr(
        "backend.agents.pipeline_coordinator.build_default_sprint_replan_handler",
        lambda **kwargs: captured.setdefault("replan_kwargs", kwargs),
    )
    cfg = CoordinatorConfig(
        config_dir=tmp_path,
        heartbeat_path=tmp_path / "heartbeat",
        decision_log_dir=tmp_path / "decision-log",
    )

    with pytest.raises(ValueError):
        build_default_coordinator(cfg, acting=True)

    def _executor(action: Action, ctx: DecisionContext) -> dict[str, Any]:
        return {"kind": action.kind, "target": action.target, "executed": True}

    build_default_coordinator(cfg, acting=True, action_executor=_executor)

    assert captured["replan_kwargs"]["shadow"] is False

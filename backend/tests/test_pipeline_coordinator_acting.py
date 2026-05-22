from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    LiveActionExecutor,
    PipelineCoordinator,
)
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot
from backend.agents.pipeline_coordinator_rules import (
    ACTION_MENTION_OPERATOR,
    ACTION_TRANSITION,
    Action,
    DecisionContext,
    DecisionResult,
    NoopAction,
)


NOW = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)


class FixedEngine:
    engine_version = "fixed-test"

    def __init__(self, result: DecisionResult) -> None:
        self.result = result

    def evaluate(self, ctx: DecisionContext) -> DecisionResult:
        return self.result


class RecordingExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    def __call__(self, action: Action, ctx: DecisionContext) -> dict[str, Any]:
        self.actions.append(action)
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": not action.dry_run,
            "dry_run": action.dry_run,
        }


class CrashingExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    def __call__(self, action: Action, ctx: DecisionContext) -> dict[str, Any]:
        self.actions.append(action)
        raise RuntimeError("simulated crash after intent")


class FailingThenRecordingExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    def __call__(self, action: Action, ctx: DecisionContext) -> dict[str, Any]:
        self.actions.append(action)
        if len(self.actions) == 1:
            raise RuntimeError("boom")
        return {
            "kind": action.kind,
            "target": action.target,
            "executed": not action.dry_run,
            "dry_run": action.dry_run,
        }


class RecordingLearningLoop:
    def __init__(self) -> None:
        self.writebacks: list[dict[str, Any]] = []

    def seed_schedule(self, now: datetime) -> None:
        pass

    def write_back_decision(self, record: dict[str, Any]) -> None:
        self.writebacks.append(record)

    def maybe_run(self, now: datetime) -> None:
        pass


class RecordingJiraClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any, str | None]] = []
        self.labels: tuple[str, ...] = ()

    def record(self, op: str, key: str, payload: Any, idem_key: str | None) -> None:
        self.calls.append((op, key, payload, idem_key))


def _config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
    )


def _capacity() -> CapacitySnapshot:
    return CapacitySnapshot.empty(captured_at=NOW)


def _read_log_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for f in sorted(directory.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _decision_tick_records(directory: Path) -> list[dict[str, Any]]:
    return [r for r in _read_log_records(directory) if r["event"] == "decision_tick"]


def _intent_records(directory: Path) -> list[dict[str, Any]]:
    return [r for r in _read_log_records(directory) if r["event"] == "tick_intent"]


def _decision_result(*, actions: tuple[Action, ...]) -> DecisionResult:
    return DecisionResult(
        decision_id="decision-op-1615",
        actions=actions,
        reason="acting-test",
        mode="tier1",
        engine_version=FixedEngine.engine_version,
        tier=2,
    )


def test_run_once_acting_flips_actions_rebuilds_result_and_logs_live_dry_run(
    tmp_path: Path,
) -> None:
    original = _decision_result(
        actions=(
            Action.mention_operator("OP-1615", message="note"),
            NoopAction(),
        )
    )
    executor = RecordingExecutor()
    learning_loop = RecordingLearningLoop()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        learning_loop=learning_loop,
        acting=True,
    )

    result = coord.run_once()

    assert result.decision_id == original.decision_id
    assert result is not original
    assert result.actions[0].dry_run is False
    assert isinstance(result.actions[1], NoopAction)
    assert executor.actions == [result.actions[0]]
    [intent] = _intent_records(coord.config.decision_log_dir)
    [record] = _decision_tick_records(coord.config.decision_log_dir)
    assert intent["decision_id"] == original.decision_id
    assert intent["actions"][0]["idem_keys"] == [
        "coord:mention_operator:OP-1615:8a46d55d7185a5f3:comment"
    ]
    assert record["decision_id"] == original.decision_id
    assert record["dry_run"] is False
    assert record["actions"][0]["dry_run"] is False
    assert learning_loop.writebacks == [record]


def test_run_once_shadow_leaves_action_dry_run_true(tmp_path: Path) -> None:
    original = _decision_result(
        actions=(Action.mention_operator("OP-1615", message="note"),)
    )
    executor = RecordingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
    )

    result = coord.run_once()

    assert result is original
    assert result.actions[0].dry_run is True
    assert executor.actions == [original.actions[0]]
    [intent] = _intent_records(coord.config.decision_log_dir)
    [record] = _decision_tick_records(coord.config.decision_log_dir)
    assert intent["dry_run"] is True
    assert record["dry_run"] is True
    assert record["actions"][0]["dry_run"] is True


def test_run_once_writes_intent_before_execute(tmp_path: Path) -> None:
    original = _decision_result(
        actions=(Action.transition("OP-1617", to_status="Under Review"),)
    )
    executor = CrashingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        acting=True,
    )

    coord.run_once()

    records = _read_log_records(coord.config.decision_log_dir)
    assert [r["event"] for r in records] == ["tick_intent", "decision_tick"]
    intent, outcome = records
    assert intent["decision_id"] == outcome["decision_id"] == original.decision_id
    assert intent["dry_run"] is False
    assert intent["actions"] == [
        {
            "kind": ACTION_TRANSITION,
            "target": "OP-1617",
            "params": {"to_status": "Under Review"},
            "dry_run": False,
            "idem_keys": ["coord:transition:OP-1617:e7d18f44ba0728c1:transition"],
        }
    ]
    assert outcome["action_results"][0]["error"] == "simulated crash after intent"


def test_run_once_caps_actions_and_logs_tick_cap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK", "1")
    original = _decision_result(
        actions=(
            Action.mention_operator("OP-1616-A", message="a"),
            Action.mention_operator("OP-1616-B", message="b"),
            Action.mention_operator("OP-1616-C", message="c"),
        )
    )
    executor = RecordingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        acting=True,
    )

    coord.run_once()

    assert [a.target for a in executor.actions] == ["OP-1616-A"]
    [record] = _decision_tick_records(coord.config.decision_log_dir)
    assert record["action_results"] == [
        {
            "kind": ACTION_MENTION_OPERATOR,
            "target": "OP-1616-A",
            "executed": True,
            "dry_run": False,
        },
        {
            "kind": ACTION_MENTION_OPERATOR,
            "target": "OP-1616-B",
            "executed": False,
            "reason": "tick_cap",
        },
        {
            "kind": ACTION_MENTION_OPERATOR,
            "target": "OP-1616-C",
            "executed": False,
            "reason": "tick_cap",
        },
    ]


def test_run_once_executor_exception_still_records_tick(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK", "2")
    original = _decision_result(
        actions=(
            Action.mention_operator("OP-1616-A", message="a"),
            Action.mention_operator("OP-1616-B", message="b"),
        )
    )
    executor = FailingThenRecordingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        acting=True,
    )

    coord.run_once()

    [record] = _decision_tick_records(coord.config.decision_log_dir)
    assert record["decision_id"] == original.decision_id
    assert record["action_results"][0] == {
        "kind": ACTION_MENTION_OPERATOR,
        "target": "OP-1616-A",
        "executed": False,
        "error": "boom",
    }
    assert record["action_results"][1] == {
        "kind": ACTION_MENTION_OPERATOR,
        "target": "OP-1616-B",
        "executed": True,
        "dry_run": False,
    }


def test_run_once_kill_switch_forces_observe_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNISIGHT_COORDINATOR_ACTING_KILL", "1")
    original = _decision_result(
        actions=(Action.mention_operator("OP-1616", message="note", dry_run=False),)
    )
    executor = RecordingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        acting=True,
    )

    result = coord.run_once()

    assert result.actions[0].dry_run is True
    assert executor.actions[0].dry_run is True
    [record] = _decision_tick_records(coord.config.decision_log_dir)
    assert record["dry_run"] is True
    assert record["action_results"] == [
        {
            "kind": ACTION_MENTION_OPERATOR,
            "target": "OP-1616",
            "executed": False,
            "dry_run": True,
        }
    ]


def test_run_once_fairness_cursor_advances_across_capped_ticks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNISIGHT_COORDINATOR_MAX_ACTIONS_PER_TICK", "1")
    original = _decision_result(
        actions=(
            Action.mention_operator("OP-1616-A", message="a"),
            Action.mention_operator("OP-1616-B", message="b"),
            Action.mention_operator("OP-1616-C", message="c"),
        )
    )
    executor = RecordingExecutor()
    coord = PipelineCoordinator(
        _config(tmp_path),
        engine=FixedEngine(original),
        clock=lambda: NOW,
        capacity_provider=_capacity,
        action_executor=executor,
        acting=True,
    )

    coord.run_once()
    coord.run_once()
    coord.run_once()

    assert [a.target for a in executor.actions] == [
        "OP-1616-A",
        "OP-1616-B",
        "OP-1616-C",
    ]


def test_live_executor_default_denies_dry_run_actions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from backend.agents import jira_dispatch as dispatch

    client = RecordingJiraClient()
    monkeypatch.setattr(dispatch, "fetch_ticket_labels", lambda client, key: ())
    monkeypatch.setattr(
        dispatch,
        "add_comment",
        lambda client, key, text, idem_key=None: client.record(
            "add_comment", key, text, idem_key
        ),
    )

    executor = LiveActionExecutor(_config(tmp_path), client=client)
    result = executor.execute(
        Action(ACTION_MENTION_OPERATOR, "OP-1615", {"message": "note"}, dry_run=True),
        DecisionContext(now=NOW, capacity=_capacity()),
    )

    assert result == {
        "kind": ACTION_MENTION_OPERATOR,
        "target": "OP-1615",
        "executed": False,
        "reason": "dry_run",
    }
    assert client.calls == []

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.agents.pipeline_coordinator import (
    CoordinatorConfig,
    LiveActionExecutor,
    NEEDS_OPERATOR_ACTION_LABEL,
)
from backend.agents import pipeline_coordinator
from backend.agents.pipeline_coordinator_capacity import CapacitySnapshot
from backend.agents.pipeline_coordinator_rules import (
    ACTION_ESCALATE,
    ACTION_FILE_TICKET,
    ACTION_MARK_FOR_FOLLOWUP,
    ACTION_MENTION_OPERATOR,
    ACTION_NOOP,
    ACTION_RELABEL,
    ACTION_TRANSITION,
    Action,
    DecisionContext,
)


NOW = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)


class RecordingJiraClient:
    def __init__(self, agent_class: str = "subscription-codex") -> None:
        self.agent_class = agent_class
        self.calls: list[tuple[str, str, Any, str | None]] = []
        self.seen_idem_keys: set[str] = set()
        self.labels: tuple[str, ...] = ()
        self.status = "In Progress"

    def record(self, op: str, key: str, payload: Any, idem_key: str | None) -> bool:
        if idem_key in self.seen_idem_keys:
            return False
        if idem_key is not None:
            self.seen_idem_keys.add(idem_key)
        self.calls.append((op, key, payload, idem_key))
        return True


@pytest.fixture
def ctx() -> DecisionContext:
    return DecisionContext(now=NOW, capacity=CapacitySnapshot.empty(captured_at=NOW))


@pytest.fixture
def config(tmp_path: Path) -> CoordinatorConfig:
    base = tmp_path / "coordinator"
    return CoordinatorConfig(
        config_dir=base,
        heartbeat_path=base / "heartbeat",
        decision_log_dir=base / "decision-log",
        jira_agent_class="subscription-codex",
    )


@pytest.fixture
def jira_dispatch(monkeypatch: pytest.MonkeyPatch):
    from backend.agents import jira_dispatch as dispatch

    clients: list[RecordingJiraClient] = []

    def make_client(agent_class: str) -> RecordingJiraClient:
        client = RecordingJiraClient(agent_class)
        clients.append(client)
        return client

    def add_comment(
        client: RecordingJiraClient, key: str, text: str, idem_key: str | None = None
    ) -> None:
        client.record("add_comment", key, text, idem_key)

    def add_label(
        client: RecordingJiraClient, key: str, label: str, idem_key: str | None = None
    ) -> None:
        client.record("add_label", key, label, idem_key)

    def remove_label(
        client: RecordingJiraClient, key: str, label: str, idem_key: str | None = None
    ) -> None:
        client.record("remove_label", key, label, idem_key)

    def clear_assignee(
        client: RecordingJiraClient, key: str, idem_key: str | None = None
    ) -> None:
        client.record("clear_assignee", key, None, idem_key)

    def transition_back_to_todo(
        client: RecordingJiraClient,
        key: str,
        reason: str,
        idem_key: str | None = None,
        **_: Any,
    ) -> None:
        client.record("transition_back_to_todo", key, reason, idem_key)

    def transition_to_under_review_if_needed(
        client: RecordingJiraClient,
        key: str,
        idem_key: str | None = None,
        **_: Any,
    ) -> bool:
        return client.record("transition_to_under_review_if_needed", key, None, idem_key)

    def fetch_ticket_labels(client: RecordingJiraClient, key: str) -> tuple[str, ...]:
        return client.labels

    def get_issue_status(client: RecordingJiraClient, key: str) -> str:
        return client.status

    monkeypatch.setattr(dispatch, "make_client", make_client)
    monkeypatch.setattr(dispatch, "add_comment", add_comment)
    monkeypatch.setattr(dispatch, "add_label", add_label)
    monkeypatch.setattr(dispatch, "remove_label", remove_label)
    monkeypatch.setattr(dispatch, "clear_assignee", clear_assignee)
    monkeypatch.setattr(dispatch, "transition_back_to_todo", transition_back_to_todo)
    monkeypatch.setattr(
        dispatch,
        "transition_to_under_review_if_needed",
        transition_to_under_review_if_needed,
    )
    monkeypatch.setattr(dispatch, "fetch_ticket_labels", fetch_ticket_labels)
    monkeypatch.setattr(dispatch, "get_issue_status", get_issue_status)
    monkeypatch.setattr(
        pipeline_coordinator,
        "_ticket_has_live_runner",
        lambda key, **_: False,
    )
    return clients


def test_live_executor_builds_jira_client_from_config(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)

    result = executor.execute(
        Action(
            ACTION_MENTION_OPERATOR,
            "OP-1613",
            {"message": "operator note", "urgency": "medium"},
            dry_run=False,
        ),
        ctx,
    )

    assert result["executed"] is True
    assert jira_dispatch[0].agent_class == "subscription-codex"


def test_live_executor_dispatches_each_action_kind(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)
    actions = [
        Action(ACTION_TRANSITION, "OP-1", {"to_status": "Under Review"}, dry_run=False),
        Action(ACTION_TRANSITION, "OP-2", {"to_status": "To Do"}, dry_run=False),
        Action(
            ACTION_RELABEL,
            "OP-3",
            {"add": ["coord-ready"], "remove": ["claim:codex-2"]},
            dry_run=False,
        ),
        Action(
            ACTION_MENTION_OPERATOR,
            "OP-4",
            {"message": "help needed", "urgency": "high"},
            dry_run=False,
        ),
        Action(
            ACTION_MARK_FOR_FOLLOWUP,
            "OP-5",
            {"when": "2026-05-24", "why": "resume tomorrow"},
            dry_run=False,
        ),
        Action(ACTION_ESCALATE, "OP-6", {"reason": "blocked"}, dry_run=False),
        Action(ACTION_FILE_TICKET, "OP-7", {"description": "needs docs"}, dry_run=False),
        Action(ACTION_NOOP, "", {}, dry_run=False),
    ]

    results = [executor.execute(action, ctx) for action in actions]

    assert results[-1] == {
        "kind": ACTION_NOOP,
        "target": "",
        "executed": False,
        "reason": "noop",
    }
    assert {r["kind"] for r in results} == {
        ACTION_TRANSITION,
        ACTION_RELABEL,
        ACTION_MENTION_OPERATOR,
        ACTION_MARK_FOR_FOLLOWUP,
        ACTION_ESCALATE,
        ACTION_FILE_TICKET,
        ACTION_NOOP,
    }
    assert results[6]["executed"] is False
    assert results[6]["reason"] == "requires_operator"
    assert [line["kind"] for line in results[6]["outcome"]] == [
        ACTION_FILE_TICKET,
        ACTION_MENTION_OPERATOR,
    ]
    ops = [call[0] for call in jira_dispatch[0].calls]
    assert ops == [
        "transition_to_under_review_if_needed",
        "transition_back_to_todo",
        "add_label",
        "remove_label",
        "clear_assignee",
        "add_comment",
        "add_label",
        "add_label",
        "add_comment",
        "add_label",
        "add_comment",
        "add_comment",
        "add_label",
    ]
    assert all(call[3] is None or call[3].startswith("coord:") for call in jira_dispatch[0].calls)


def test_escalate_always_adds_operator_label_and_reason_comment(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)

    result = executor.execute(
        Action(ACTION_ESCALATE, "OP-1613", {"reason": "human decision"}, dry_run=False),
        ctx,
    )

    assert result["executed"] is True
    assert jira_dispatch[0].calls == [
        (
            "add_label",
            "OP-1613",
            NEEDS_OPERATOR_ACTION_LABEL,
            jira_dispatch[0].calls[0][3],
        ),
        ("add_comment", "OP-1613", "human decision", jira_dispatch[0].calls[1][3]),
    ]
    assert jira_dispatch[0].calls[0][3].endswith(":label")
    assert jira_dispatch[0].calls[1][3].endswith(":comment")


def test_idem_key_ignores_volatile_decision_id(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)
    first = Action(
        ACTION_MENTION_OPERATOR,
        "OP-1613",
        {"message": "same text", "urgency": "medium", "decision_id": "tick-a"},
        dry_run=False,
    )
    second = Action(
        ACTION_MENTION_OPERATOR,
        "OP-1613",
        {"message": "same text", "urgency": "medium", "decision_id": "tick-b"},
        dry_run=False,
    )

    executor.execute(first, ctx)
    executor.execute(second, ctx)

    assert jira_dispatch[0].calls == [
        ("add_comment", "OP-1613", "same text", jira_dispatch[0].calls[0][3])
    ]


def test_reworded_comment_uses_new_idem_key(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)

    executor.execute(
        Action(
            ACTION_MENTION_OPERATOR,
            "OP-1613",
            {"message": "first text", "urgency": "medium"},
            dry_run=False,
        ),
        ctx,
    )
    executor.execute(
        Action(
            ACTION_MENTION_OPERATOR,
            "OP-1613",
            {"message": "second text", "urgency": "medium"},
            dry_run=False,
        ),
        ctx,
    )

    idem_keys = [call[3] for call in jira_dispatch[0].calls]
    assert len(idem_keys) == 2
    assert idem_keys[0] != idem_keys[1]


def test_composite_relabel_uses_distinct_idem_keys(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    executor = LiveActionExecutor(config)

    executor.execute(
        Action(
            ACTION_RELABEL,
            "OP-1613",
            {"add": ["coord-a", "coord-b"], "remove": []},
            dry_run=False,
        ),
        ctx,
    )

    assert [call[0] for call in jira_dispatch[0].calls] == ["add_label", "add_label"]
    assert jira_dispatch[0].calls[0][3].endswith(":add:coord-a")
    assert jira_dispatch[0].calls[1][3].endswith(":add:coord-b")
    assert jira_dispatch[0].calls[0][3] != jira_dispatch[0].calls[1][3]


@pytest.mark.parametrize("label", ["coord-skip", "coord-quarantine"])
def test_live_executor_coord_skip_labels_block_mutation(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
    label: str,
) -> None:
    client = RecordingJiraClient()
    client.labels = (label,)
    executor = LiveActionExecutor(config, client=client)

    result = executor.execute(
        Action(
            ACTION_MENTION_OPERATOR,
            "OP-1614",
            {"message": "operator note", "urgency": "medium"},
            dry_run=False,
        ),
        ctx,
    )

    assert result == {
        "kind": ACTION_MENTION_OPERATOR,
        "target": "OP-1614",
        "executed": False,
        "reason": "coord_skip",
    }
    assert client.calls == []


def test_live_executor_label_read_failure_blocks_mutation(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    monkeypatch: pytest.MonkeyPatch,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    from backend.agents import jira_dispatch as dispatch

    def boom(client: RecordingJiraClient, key: str) -> tuple[str, ...]:
        raise RuntimeError("labels unavailable")

    monkeypatch.setattr(dispatch, "fetch_ticket_labels", boom)
    executor = LiveActionExecutor(config)

    result = executor.execute(
        Action(
            ACTION_RELABEL,
            "OP-1614",
            {"add": ["coord-ready"], "remove": []},
            dry_run=False,
        ),
        ctx,
    )

    assert result == {
        "kind": ACTION_RELABEL,
        "target": "OP-1614",
        "executed": False,
        "reason": "label_read_failed",
    }
    assert jira_dispatch[0].calls == []


@pytest.mark.parametrize("status", ["To Do", "Under Review"])
def test_live_executor_to_do_transition_rechecks_status_before_mutation(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    jira_dispatch: list[RecordingJiraClient],
    status: str,
) -> None:
    client = RecordingJiraClient()
    client.status = status
    executor = LiveActionExecutor(config, client=client)

    result = executor.execute(
        Action(ACTION_TRANSITION, "OP-1614", {"to_status": "To Do"}, dry_run=False),
        ctx,
    )

    assert result == {
        "kind": ACTION_TRANSITION,
        "target": "OP-1614",
        "executed": False,
        "reason": "context_stale",
    }
    assert client.calls == []


def test_live_executor_to_do_transition_rechecks_live_runner_before_mutation(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    monkeypatch: pytest.MonkeyPatch,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    monkeypatch.setattr(
        pipeline_coordinator,
        "_ticket_has_live_runner",
        lambda key, **_: True,
    )
    client = RecordingJiraClient()
    executor = LiveActionExecutor(config, client=client)

    result = executor.execute(
        Action(ACTION_TRANSITION, "OP-1614", {"to_status": "To Do"}, dry_run=False),
        ctx,
    )

    assert result == {
        "kind": ACTION_TRANSITION,
        "target": "OP-1614",
        "executed": False,
        "reason": "context_stale",
    }
    assert client.calls == []


def test_live_executor_to_do_transition_probe_error_fails_closed(
    config: CoordinatorConfig,
    ctx: DecisionContext,
    monkeypatch: pytest.MonkeyPatch,
    jira_dispatch: list[RecordingJiraClient],
) -> None:
    def boom(key: str, **_: Any) -> bool:
        raise RuntimeError("runner probe failed")

    monkeypatch.setattr(pipeline_coordinator, "_ticket_has_live_runner", boom)
    client = RecordingJiraClient()
    executor = LiveActionExecutor(config, client=client)

    result = executor.execute(
        Action(ACTION_TRANSITION, "OP-1614", {"to_status": "To Do"}, dry_run=False),
        ctx,
    )

    assert result == {
        "kind": ACTION_TRANSITION,
        "target": "OP-1614",
        "executed": False,
        "reason": "context_stale",
    }
    assert client.calls == []


def test_unknown_action_kind_is_unsupported(config: CoordinatorConfig, ctx: DecisionContext) -> None:
    executor = LiveActionExecutor(config, client=RecordingJiraClient())
    action = SimpleNamespace(kind="unknown_kind", target="OP-1613", params={})

    result = executor.execute(action, ctx)  # type: ignore[arg-type]

    assert result == {
        "kind": "unknown_kind",
        "target": "OP-1613",
        "executed": False,
        "reason": "unsupported",
    }

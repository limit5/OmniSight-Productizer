"""BP.H.2 / BP.H.2.b — Contract tests for ``backend.red_card``.

Pins the red-card gate only: three consecutive ``Verified -1`` labels
for the same agent on the same JIRA ticket cut API access and mark the
ticket blocked.  BP.H.2.b adds recursive subcall-budget yellow/red-card
coverage.  The notification ``is_red_card`` row remains intentionally
out of scope here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.red_card import (
    JIRA_BLOCKED_PREFIX,
    RED_CARD_REASON,
    RED_CARD_THRESHOLD,
    RECURSIVE_SUBCALL_BUDGET_REASON,
    SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS,
    SUBCALL_BUDGET_RED_THRESHOLD,
    SUBCALL_BUDGET_YELLOW_THRESHOLD,
    VERIFIED_MINUS_ONE,
    RecursiveSubcallBudgetDeps,
    RecursiveSubcallBudgetEvent,
    RecursiveSubcallBudgetHumanEscalationResult,
    RecursiveSubcallBudgetOverride,
    RecursiveSubcallBudgetSlowDownResult,
    RedCardApiCutResult,
    RedCardDeps,
    RedCardJiraBlockResult,
    RedCardReviewEvent,
    evaluate_recursive_subcall_budget,
    evaluate_red_card,
    execute_recursive_subcall_budget,
    execute_red_card,
    parse_recursive_subcall_history,
    parse_review_history,
)


def _run(coro):
    return asyncio.run(coro)


def _event(
    label: str = VERIFIED_MINUS_ONE,
    *,
    agent_id: str = "agent-alpha",
    jira_ticket: str = "PROJ-42",
    api_key_id: str = "ak-alpha",
    patchset: str = "1",
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "jira_ticket": jira_ticket,
        "verified_label": label,
        "api_key_id": api_key_id,
        "patchset": patchset,
    }


def _subcall(
    *,
    root_task_id: str = "root-1",
    agent_id: str = "agent-alpha",
    guild_id: str = "backend",
    jira_ticket: str = "PROJ-42",
    api_key_id: str = "ak-alpha",
    subcall_id: str = "call-1",
) -> dict[str, Any]:
    return {
        "root_task_id": root_task_id,
        "agent_id": agent_id,
        "guild_id": guild_id,
        "jira_ticket": jira_ticket,
        "api_key_id": api_key_id,
        "subcall_id": subcall_id,
    }


def _subcalls(count: int, **overrides: Any) -> list[dict[str, Any]]:
    return [
        _subcall(subcall_id=f"call-{idx}", **overrides)
        for idx in range(1, count + 1)
    ]


class _StubApiDisabler:
    def __init__(self):
        self.calls: list[dict[str, str]] = []

    async def disable_agent_api(self, *, agent_id, api_key_id, reason):
        self.calls.append({
            "agent_id": agent_id,
            "api_key_id": api_key_id,
            "reason": reason,
        })
        return RedCardApiCutResult(
            ok=True,
            agent_id=agent_id,
            api_key_id=api_key_id,
            reason=reason,
        )


class _StubJiraBlocker:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def mark_blocked(
        self,
        *,
        ticket,
        agent_id,
        consecutive_failures,
        reason,
    ):
        self.calls.append({
            "ticket": ticket,
            "agent_id": agent_id,
            "consecutive_failures": consecutive_failures,
            "reason": reason,
        })
        return RedCardJiraBlockResult(ok=True, ticket=ticket, reason=reason)


class _StubSubcallSlower:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def slow_down(self, *, root_task_id, agent_id, seconds, reason):
        self.calls.append({
            "root_task_id": root_task_id,
            "agent_id": agent_id,
            "seconds": seconds,
            "reason": reason,
        })
        return RecursiveSubcallBudgetSlowDownResult(
            ok=True,
            root_task_id=root_task_id,
            agent_id=agent_id,
            slow_down_seconds=seconds,
            reason=reason,
        )


class _StubSubcallHumanEscalator:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def escalate_human(
        self,
        *,
        root_task_id,
        agent_id,
        guild_id,
        ticket,
        subcall_count,
        reason,
    ):
        self.calls.append({
            "root_task_id": root_task_id,
            "agent_id": agent_id,
            "guild_id": guild_id,
            "ticket": ticket,
            "subcall_count": subcall_count,
            "reason": reason,
        })
        return RecursiveSubcallBudgetHumanEscalationResult(
            ok=True,
            root_task_id=root_task_id,
            agent_id=agent_id,
            ticket=ticket,
            reason=reason,
        )


def test_constants_pin_red_card_contract() -> None:
    assert VERIFIED_MINUS_ONE == "Verified -1"
    assert RED_CARD_THRESHOLD == 3
    assert JIRA_BLOCKED_PREFIX == "[BLOCKED]"
    assert RED_CARD_REASON == "red_card_verified_minus_one_streak"
    assert SUBCALL_BUDGET_YELLOW_THRESHOLD == 3
    assert SUBCALL_BUDGET_RED_THRESHOLD == 5
    assert SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS == 30
    assert RECURSIVE_SUBCALL_BUDGET_REASON == "recursive_subcall_budget_exceeded"


def test_parse_review_history_accepts_mapping_aliases() -> None:
    events = parse_review_history([
        {
            "agent": "agent-alpha",
            "ticket": "PROJ-42",
            "score": "-1",
            "key_id": "ak-1",
        },
        {"bad": "row"},
    ])
    assert events == (
        RedCardReviewEvent(
            agent_id="agent-alpha",
            jira_ticket="PROJ-42",
            verified_label="Verified -1",
            api_key_id="ak-1",
        ),
    )


def test_two_failures_warn_but_do_not_red_card() -> None:
    decision = evaluate_red_card([_event(), _event(patchset="2")])
    assert decision.status == "warn"
    assert decision.consecutive_failures == 2
    assert decision.should_cut_api is False
    assert decision.should_block_jira is False


def test_three_consecutive_failures_red_card() -> None:
    decision = evaluate_red_card([
        _event(patchset="1"),
        _event(patchset="2"),
        _event(patchset="3"),
    ])
    assert decision.status == "red_card"
    assert decision.consecutive_failures == 3
    assert decision.reason == RED_CARD_REASON
    assert decision.api_key_id == "ak-alpha"
    assert decision.patchset == "3"
    assert decision.should_cut_api is True
    assert decision.should_block_jira is True


def test_non_failure_resets_consecutive_streak() -> None:
    decision = evaluate_red_card([
        _event(patchset="1"),
        _event(patchset="2"),
        _event("Verified +1", patchset="3"),
    ])
    assert decision.status == "clear"
    assert decision.consecutive_failures == 0


def test_streak_is_scoped_to_same_agent_and_ticket() -> None:
    decision = evaluate_red_card([
        _event(agent_id="agent-alpha", jira_ticket="PROJ-42"),
        _event(agent_id="agent-beta", jira_ticket="PROJ-42"),
        _event(agent_id="agent-alpha", jira_ticket="PROJ-99"),
        _event(agent_id="agent-alpha", jira_ticket="PROJ-42", patchset="2"),
        _event(agent_id="agent-alpha", jira_ticket="PROJ-42", patchset="3"),
    ])
    assert decision.agent_id == "agent-alpha"
    assert decision.jira_ticket == "PROJ-42"
    assert decision.status == "red_card"
    assert decision.consecutive_failures == 3


def test_explicit_scope_can_evaluate_non_latest_pair() -> None:
    decision = evaluate_red_card(
        [
            _event(agent_id="agent-alpha", jira_ticket="PROJ-42"),
            _event(agent_id="agent-alpha", jira_ticket="PROJ-42"),
            _event(agent_id="agent-alpha", jira_ticket="PROJ-42"),
            _event("Verified +1", agent_id="agent-beta", jira_ticket="PROJ-99"),
        ],
        agent_id="agent-alpha",
        jira_ticket="PROJ-42",
    )
    assert decision.status == "red_card"
    assert decision.consecutive_failures == 3


def test_empty_history_is_clear() -> None:
    decision = evaluate_red_card([], agent_id="agent-alpha", jira_ticket="PROJ-42")
    assert decision.status == "clear"
    assert decision.consecutive_failures == 0


def test_custom_threshold_is_supported_for_staging() -> None:
    decision = evaluate_red_card([_event(), _event(patchset="2")], threshold=2)
    assert decision.status == "red_card"
    assert decision.threshold == 2


def test_execute_red_card_noops_before_threshold() -> None:
    api = _StubApiDisabler()
    jira = _StubJiraBlocker()
    result = _run(execute_red_card(
        [_event(), _event(patchset="2")],
        deps=RedCardDeps(api_disabler=api, jira_blocker=jira),
    ))
    assert result.performed is False
    assert result.api_cut is None
    assert result.jira_block is None
    assert api.calls == []
    assert jira.calls == []


def test_execute_red_card_cuts_api_and_blocks_jira() -> None:
    api = _StubApiDisabler()
    jira = _StubJiraBlocker()
    result = _run(execute_red_card(
        [_event(patchset="1"), _event(patchset="2"), _event(patchset="3")],
        deps=RedCardDeps(api_disabler=api, jira_blocker=jira),
    ))
    assert result.performed is True
    assert result.api_cut is not None
    assert result.api_cut.ok is True
    assert result.jira_block is not None
    assert result.jira_block.ok is True
    assert api.calls == [{
        "agent_id": "agent-alpha",
        "api_key_id": "ak-alpha",
        "reason": RED_CARD_REASON,
    }]
    assert jira.calls == [{
        "ticket": "PROJ-42",
        "agent_id": "agent-alpha",
        "consecutive_failures": 3,
        "reason": RED_CARD_REASON,
    }]


def test_parse_recursive_subcall_history_accepts_mapping_aliases() -> None:
    events = parse_recursive_subcall_history([
        {
            "task_id": "root-1",
            "agent": "agent-alpha",
            "guild": "backend",
            "ticket": "PROJ-42",
            "key_id": "ak-1",
            "request_id": "call-1",
            "recursion_depth": "2",
        },
        {"bad": "row"},
    ])
    assert events == (
        RecursiveSubcallBudgetEvent(
            root_task_id="root-1",
            agent_id="agent-alpha",
            guild_id="backend",
            jira_ticket="PROJ-42",
            api_key_id="ak-1",
            subcall_id="call-1",
            depth=2,
        ),
    )


def test_three_subcalls_are_clear_because_threshold_is_strictly_greater() -> None:
    decision = evaluate_recursive_subcall_budget(_subcalls(3))
    assert decision.status == "clear"
    assert decision.subcall_count == 3
    assert decision.should_warn is False
    assert decision.should_slow_down is False


def test_four_subcalls_yellow_card_warns_and_slows_down() -> None:
    decision = evaluate_recursive_subcall_budget(_subcalls(4))
    assert decision.status == "yellow_card"
    assert decision.subcall_count == 4
    assert decision.reason == RECURSIVE_SUBCALL_BUDGET_REASON
    assert decision.slow_down_seconds == SUBCALL_BUDGET_DEFAULT_SLOW_DOWN_SECONDS
    assert decision.should_warn is True
    assert decision.should_slow_down is True
    assert decision.should_cut_api is False
    assert decision.should_escalate_human is False


def test_six_subcalls_red_card_cuts_agent_and_escalates_human() -> None:
    decision = evaluate_recursive_subcall_budget(_subcalls(6))
    assert decision.status == "red_card"
    assert decision.subcall_count == 6
    assert decision.reason == RECURSIVE_SUBCALL_BUDGET_REASON
    assert decision.slow_down_seconds == 0
    assert decision.should_cut_api is True
    assert decision.should_escalate_human is True
    assert decision.api_key_id == "ak-alpha"


def test_subcall_budget_is_scoped_to_root_task() -> None:
    history = [
        *_subcalls(3, root_task_id="root-1"),
        *_subcalls(3, root_task_id="root-2"),
    ]
    decision = evaluate_recursive_subcall_budget(history)
    assert decision.root_task_id == "root-2"
    assert decision.status == "clear"
    assert decision.subcall_count == 3


def test_explicit_root_task_can_evaluate_non_latest_subcall_scope() -> None:
    history = [
        *_subcalls(6, root_task_id="root-1"),
        _subcall(root_task_id="root-2"),
    ]
    decision = evaluate_recursive_subcall_budget(history, root_task_id="root-1")
    assert decision.root_task_id == "root-1"
    assert decision.status == "red_card"
    assert decision.subcall_count == 6


def test_per_guild_override_changes_yellow_and_red_thresholds() -> None:
    decision = evaluate_recursive_subcall_budget(
        _subcalls(6, guild_id="forensics"),
        guild_overrides={
            "forensics": RecursiveSubcallBudgetOverride(
                yellow_threshold=6,
                red_threshold=8,
                slow_down_seconds=45,
            ),
        },
    )
    assert decision.status == "clear"
    assert decision.yellow_threshold == 6
    assert decision.red_threshold == 8
    assert decision.slow_down_seconds == 0

    yellow = evaluate_recursive_subcall_budget(
        _subcalls(7, guild_id="forensics"),
        guild_overrides={"forensics": {"yellow": 6, "red": 8, "slow_down": 45}},
    )
    assert yellow.status == "yellow_card"
    assert yellow.slow_down_seconds == 45


def test_execute_recursive_subcall_budget_noops_when_clear() -> None:
    api = _StubApiDisabler()
    slower = _StubSubcallSlower()
    human = _StubSubcallHumanEscalator()
    result = _run(execute_recursive_subcall_budget(
        _subcalls(3),
        deps=RecursiveSubcallBudgetDeps(
            api_disabler=api,
            slower=slower,
            human_escalator=human,
        ),
    ))
    assert result.performed is False
    assert result.slow_down is None
    assert result.api_cut is None
    assert result.human_escalation is None
    assert api.calls == []
    assert slower.calls == []
    assert human.calls == []


def test_execute_recursive_subcall_budget_yellow_card_slows_only() -> None:
    api = _StubApiDisabler()
    slower = _StubSubcallSlower()
    human = _StubSubcallHumanEscalator()
    result = _run(execute_recursive_subcall_budget(
        _subcalls(4),
        deps=RecursiveSubcallBudgetDeps(
            api_disabler=api,
            slower=slower,
            human_escalator=human,
        ),
    ))
    assert result.performed is True
    assert result.slow_down is not None
    assert result.slow_down.slow_down_seconds == 30
    assert result.api_cut is None
    assert result.human_escalation is None
    assert api.calls == []
    assert slower.calls == [{
        "root_task_id": "root-1",
        "agent_id": "agent-alpha",
        "seconds": 30,
        "reason": RECURSIVE_SUBCALL_BUDGET_REASON,
    }]
    assert human.calls == []


def test_execute_recursive_subcall_budget_red_card_cuts_and_escalates() -> None:
    api = _StubApiDisabler()
    slower = _StubSubcallSlower()
    human = _StubSubcallHumanEscalator()
    result = _run(execute_recursive_subcall_budget(
        _subcalls(6),
        deps=RecursiveSubcallBudgetDeps(
            api_disabler=api,
            slower=slower,
            human_escalator=human,
        ),
    ))
    assert result.performed is True
    assert result.slow_down is None
    assert result.api_cut is not None
    assert result.api_cut.ok is True
    assert result.human_escalation is not None
    assert result.human_escalation.ok is True
    assert api.calls == [{
        "agent_id": "agent-alpha",
        "api_key_id": "ak-alpha",
        "reason": RECURSIVE_SUBCALL_BUDGET_REASON,
    }]
    assert slower.calls == []
    assert human.calls == [{
        "root_task_id": "root-1",
        "agent_id": "agent-alpha",
        "guild_id": "backend",
        "ticket": "PROJ-42",
        "subcall_count": 6,
        "reason": RECURSIVE_SUBCALL_BUDGET_REASON,
    }]

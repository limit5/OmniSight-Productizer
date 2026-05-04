"""BP.H.2 — Contract tests for ``backend.red_card``.

Pins the red-card gate only: three consecutive ``Verified -1`` labels
for the same agent on the same JIRA ticket cut API access and mark the
ticket blocked.  The recursive subcall-budget and notification
``is_red_card`` rows are sibling tasks and are intentionally out of
scope here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.red_card import (
    JIRA_BLOCKED_PREFIX,
    RED_CARD_REASON,
    RED_CARD_THRESHOLD,
    VERIFIED_MINUS_ONE,
    RedCardApiCutResult,
    RedCardDeps,
    RedCardJiraBlockResult,
    RedCardReviewEvent,
    evaluate_red_card,
    execute_red_card,
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


def test_constants_pin_red_card_contract() -> None:
    assert VERIFIED_MINUS_ONE == "Verified -1"
    assert RED_CARD_THRESHOLD == 3
    assert JIRA_BLOCKED_PREFIX == "[BLOCKED]"
    assert RED_CARD_REASON == "red_card_verified_minus_one_streak"


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

"""OP-2640 execution-contract tests (offline)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from typing import get_args

import pytest

from backend.agents import execution_contract
from backend.agents.execution_contract import (
    ATTEMPT_OUTCOMES,
    GRANT_STATES,
    GRANT_TRANSITIONS,
    RECOVERY_MODES,
    RESUME_STATES,
    RESUME_TRANSITIONS,
    Applied,
    DefinitelyNotApplied,
    ExecOutcome,
    StoredAction,
    TerminalPlan,
    Unknown,
    is_valid_grant_transition,
    is_valid_resume_transition,
    resolve_terminal,
)


def _outcomes() -> tuple[ExecOutcome, ...]:
    return (
        Applied(result={"receipt": "ok"}, evidence="sink receipt"),
        DefinitelyNotApplied(error="rejected", evidence="pre-write rejection"),
        Unknown(error="connection lost"),
    )


def test_exec_outcome_is_exact_frozen_distinguishable_tagged_union() -> None:
    outcomes = _outcomes()

    assert get_args(ExecOutcome) == (Applied, DefinitelyNotApplied, Unknown)
    assert isinstance(outcomes[0], Applied)
    assert isinstance(outcomes[1], DefinitelyNotApplied)
    assert isinstance(outcomes[2], Unknown)
    assert len({type(outcome) for outcome in outcomes}) == 3

    for outcome in outcomes:
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.extra = "mutation"  # type: ignore[attr-defined]


def test_resolve_terminal_covers_frozen_mapping_and_unknown_mode() -> None:
    applied, definitely_not_applied, unknown = _outcomes()
    applied_plan = TerminalPlan("consumed", "done", True, False)
    not_applied_plan = TerminalPlan("failed", "failed", False, True)

    for recovery_mode in RECOVERY_MODES:
        assert resolve_terminal(applied, recovery_mode) == applied_plan
        assert (
            resolve_terminal(definitely_not_applied, recovery_mode) == not_applied_plan
        )

    assert resolve_terminal(unknown, "non_replayable") == TerminalPlan(
        "manual",
        "manual",
        False,
        True,
    )
    for recovery_mode in ("sink_idempotency_key", "read_after_write"):
        assert resolve_terminal(unknown, recovery_mode) == TerminalPlan(
            "executing",
            "queued",
            False,
            True,
        )

    for outcome in _outcomes():
        with pytest.raises(ValueError, match="unknown recovery mode"):
            resolve_terminal(outcome, "retry_anything")


def test_resolve_terminal_emits_legal_transition_or_executing_self_loop() -> None:
    for outcome in _outcomes():
        for recovery_mode in RECOVERY_MODES:
            plan = resolve_terminal(outcome, recovery_mode)
            assert plan.grant_next == "executing" or is_valid_grant_transition(
                "executing",
                plan.grant_next,
            )


def test_frozen_transition_validators_and_terminal_out_edges() -> None:
    for current, next_states in GRANT_TRANSITIONS.items():
        for next_state in next_states:
            assert is_valid_grant_transition(current, next_state)
    for current, next_states in RESUME_TRANSITIONS.items():
        for next_state in next_states:
            assert is_valid_resume_transition(current, next_state)

    assert not is_valid_grant_transition("consumed", "executing")
    assert not is_valid_resume_transition("done", "claimed")
    assert not is_valid_grant_transition("unknown", "pending")
    assert not is_valid_resume_transition("unknown", "queued")

    for terminal in ("consumed", "expired", "failed"):
        assert GRANT_TRANSITIONS[terminal] == frozenset()
    for terminal in ("done", "manual", "failed"):
        assert RESUME_TRANSITIONS[terminal] == frozenset()


def test_pending_to_expired_is_valid_claim_terminalization_transition() -> None:
    assert is_valid_grant_transition("pending", "expired")
    assert not is_valid_grant_transition("executing", "expired")


def test_stored_action_and_contract_records_are_frozen() -> None:
    stored_action = StoredAction(
        grant_id="grant-1",
        idempotency_key="grant-1:action-1",
        recovery_mode="sink_idempotency_key",
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version="v1",
        canonical_target="/workspace/result.txt",
        executable_args={"content": "done"},
    )
    records = (
        *_outcomes(),
        stored_action,
        TerminalPlan("consumed", "done", True, False),
    )

    for record in records:
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.extra = "mutation"  # type: ignore[attr-defined]


def test_state_vocabularies_match_frozen_schema_sets() -> None:
    assert GRANT_STATES == frozenset({
        "pending",
        "executing",
        "consumed",
        "expired",
        "failed",
        "manual",
    })
    assert RESUME_STATES == frozenset({"queued", "claimed", "done", "manual", "failed"})
    assert ATTEMPT_OUTCOMES == frozenset({
        "applied",
        "definitely_not_applied",
        "unknown",
    })
    assert RECOVERY_MODES == frozenset({
        "non_replayable",
        "sink_idempotency_key",
        "read_after_write",
    })


def test_execution_contract_module_is_stdlib_only_leaf() -> None:
    source = pathlib.Path(execution_contract.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    for module in imported:
        assert not module.startswith("backend"), f"backend import in leaf: {module}"

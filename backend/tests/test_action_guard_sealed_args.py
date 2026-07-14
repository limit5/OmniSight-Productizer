"""OP-2662 — dormant authorization-to-execution argument binding."""

from __future__ import annotations

from backend.agents.action_guard import GuardOutcome, effective_execution_args


def _outcome(*, sealed_args=None) -> GuardOutcome:
    return GuardOutcome(
        proceed=True,
        decision=None,
        family="code_write",
        mode="shadow",
        sealed_args=sealed_args,
    )


def test_guard_outcome_sealed_args_defaults_to_none() -> None:
    outcome = GuardOutcome(
        proceed=True,
        decision=None,
        family="code_write",
        mode="shadow",
    )
    assert outcome.sealed_args is None


def test_effective_execution_args_none_returns_live_object() -> None:
    live = {"path": "live"}
    assert effective_execution_args(_outcome(), live) is live


def test_effective_execution_args_returns_sealed_object_by_identity() -> None:
    live = {"path": "live"}
    sealed = {"path": "sealed"}
    assert effective_execution_args(_outcome(sealed_args=sealed), live) is sealed


def test_effective_execution_args_honors_empty_mapping_seal() -> None:
    live = {"path": "live"}
    sealed: dict[str, object] = {}
    result = effective_execution_args(_outcome(sealed_args=sealed), live)
    assert result is sealed
    assert result is not live

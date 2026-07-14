"""OP-2663 — latched enforce refinement and sealed execution."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from backend.agents import (
    action_canonicalize,
    action_guard,
    canonicalize_code_write_file,
    shadow_root_registry,
)
from backend.agents.action_guard import GuardOutcome, guard_tool_dispatch
from backend.agents.execution_context import ExecutionContext, for_service
from backend.agents.tool_dispatcher import ToolDispatcher


_ADAPTER = "runner_sdk"
_TOOL = "str_replace_based_edit_tool"
_SCHEMA = "v1"


@pytest.fixture(autouse=True)
def _isolate_refine_state(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Restore every process-local registry and mode changed by a test."""
    canonical_state = action_canonicalize._snapshot_state_for_tests()
    bootstrap_ok = action_guard._CANONICAL_BOOTSTRAP_OK
    action_canonicalize.reset_for_tests()
    canonicalize_code_write_file.register_code_write_file_canonicalizers()
    action_canonicalize.freeze_registry()
    shadow_root_registry.reset_for_tests()
    action_guard._CANONICAL_BOOTSTRAP_OK = True
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    action_guard.reload_mode_matrix_for_tests()
    try:
        yield
    finally:
        monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
        action_guard.reload_mode_matrix_for_tests()
        shadow_root_registry.reset_for_tests()
        action_canonicalize._restore_state_for_tests(canonical_state)
        action_guard._CANONICAL_BOOTSTRAP_OK = bootstrap_ok


def _bound_context() -> ExecutionContext:
    return for_service(
        service_name="runner-launcher",
        tenant_id="t-default",
        request_id="r1",
        roles=("runner",),
        authorization_source="server_launcher",
    )


def _set_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OMNISIGHT_ACTION_GUARD_MODE",
        "runner_sdk:code_write=enforce",
    )
    action_guard.reload_mode_matrix_for_tests()


def _publish_root(root: Path) -> None:
    shadow_root_registry.register_shadow_root(
        _ADAPTER,
        _TOOL,
        _SCHEMA,
        workspace_root=str(root),
    )
    shadow_root_registry.freeze_registry()


def _guard(
    raw_args: dict,
    *,
    adapter_namespace: str = _ADAPTER,
    tool_name: str = _TOOL,
) -> GuardOutcome:
    return guard_tool_dispatch(
        adapter_namespace=adapter_namespace,
        tool_name=tool_name,
        schema_version=_SCHEMA,
        raw_args=raw_args,
        execution_context=_bound_context(),
    )


def _view_args(root: Path) -> dict[str, object]:
    return {"command": "view", "path": str(root / "view.py")}


def test_default_shadow_keeps_name_outcome_and_no_seal(tmp_path: Path) -> None:
    outcome = _guard(_view_args(tmp_path))

    assert outcome.proceed is True
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.family == "code_write"
    assert outcome.mode == "shadow"
    assert outcome.blocked_reason is None
    assert outcome.sealed_args is None


def test_enforce_view_refines_to_allow_and_seals_frozen_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)
    raw_args = _view_args(tmp_path)

    outcome = _guard(raw_args)

    assert outcome.proceed is True
    assert outcome.decision is not None
    assert outcome.decision.verdict == "allow"
    assert outcome.family == "read_only"
    assert outcome.mode == "enforce"
    assert outcome.blocked_reason is None
    assert type(outcome.sealed_args) is dict
    assert outcome.sealed_args == raw_args
    assert outcome.sealed_args is not raw_args


def test_enforce_mutating_command_remains_blocked_without_seal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)

    outcome = _guard(
        {
            "command": "str_replace",
            "path": str(tmp_path / "edit.py"),
            "old_str": "old",
            "new_str": "new",
        }
    )

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.family == "code_write"
    assert outcome.mode == "enforce"
    assert outcome.blocked_reason == "requires_grant"
    assert outcome.sealed_args is None


def test_refine_exception_latches_block_and_never_executes_live_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)

    def raise_refine(*_args, **_kwargs):
        raise RuntimeError("refine failed")

    monkeypatch.setattr(action_guard, "_refine_enforce_outcome", raise_refine)
    calls: list[Mapping[str, object]] = []

    async def handler(args: Mapping[str, object]) -> str:
        calls.append(args)
        return "unexpected"

    dispatcher = ToolDispatcher()
    dispatcher.register(_TOOL, handler)
    result = asyncio.run(
        dispatcher.execute(
            "tu1",
            _TOOL,
            {
                "command": "str_replace",
                "path": str(tmp_path / "edit.py"),
                "old_str": "old",
                "new_str": "new",
            },
            execution_context=_bound_context(),
        )
    )

    assert result.is_error is True
    assert json.loads(result.content)["blocked_reason"] == (
        "canonicalization_rejected"
    )
    assert calls == []


def test_unknown_adapter_enforce_without_explicit_entry_keeps_name_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_refine(*_args, **_kwargs):
        pytest.fail("an implicit enforce mode must not enter refinement")

    monkeypatch.setattr(
        action_guard,
        "_refine_enforce_outcome",
        unexpected_refine,
    )

    outcome = _guard(
        _view_args(tmp_path),
        adapter_namespace="unknown_adapter",
    )

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.family == "code_write"
    assert outcome.mode == "enforce"
    assert outcome.blocked_reason == "requires_grant"
    assert outcome.sealed_args is None


def test_bootstrap_failure_keeps_name_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    monkeypatch.setattr(action_guard, "_CANONICAL_BOOTSTRAP_OK", False)

    outcome = _guard(_view_args(tmp_path))

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.family == "code_write"
    assert outcome.blocked_reason == "requires_grant"
    assert outcome.sealed_args is None


def test_enforce_view_without_authoritative_context_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)

    outcome = _guard(_view_args(tmp_path))

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.blocked_reason == "no_authoritative_context"
    assert outcome.sealed_args is None


@pytest.mark.parametrize(
    "bad_value",
    [object(), "\ud800"],
    ids=["non_serializable", "lone_surrogate"],
)
def test_invalid_freeze_input_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_value: object,
) -> None:
    _set_enforce(monkeypatch)
    raw_args = _view_args(tmp_path)
    raw_args["bad"] = bad_value

    outcome = _guard(raw_args)

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.blocked_reason == "canonicalization_rejected"
    assert outcome.sealed_args is None


def test_oversized_freeze_input_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    raw_args = _view_args(tmp_path)
    raw_args["padding"] = "x" * (action_guard._MAX_FREEZE_ARGS_BYTES + 1)

    outcome = _guard(raw_args)

    assert outcome.proceed is False
    assert outcome.blocked_reason == "canonicalization_rejected"
    assert outcome.sealed_args is None


@pytest.mark.parametrize(
    "reason",
    ["canonicalization_failed:bad_path", "authorize_canonical_denied"],
)
def test_canonicalization_reasons_map_to_bounded_reason(reason: str) -> None:
    assert (
        action_guard._bounded_blocked_reason(reason)
        == "canonicalization_rejected"
    )


def test_name_allow_and_unknown_deny_skip_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNISIGHT_ACTION_GUARD_MODE",
        "runner_sdk:read_only=enforce",
    )
    action_guard.reload_mode_matrix_for_tests()

    def unexpected_refine(*_args, **_kwargs):
        pytest.fail("only requires_grant decisions may refine")

    monkeypatch.setattr(
        action_guard,
        "_refine_enforce_outcome",
        unexpected_refine,
    )

    allowed = _guard({}, tool_name="Read")
    denied = _guard(
        {},
        adapter_namespace="unknown_adapter",
        tool_name="definitely_unknown",
    )

    assert allowed.proceed is True
    assert allowed.decision is not None
    assert allowed.decision.verdict == "allow"
    assert allowed.mode == "enforce"
    assert allowed.sealed_args is None
    assert denied.proceed is False
    assert denied.decision is not None
    assert denied.decision.reason == "unknown_tool_default_deny"
    assert denied.mode == "enforce"
    assert denied.sealed_args is None


def test_refine_exception_never_escapes_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)

    def raise_refine(*_args, **_kwargs):
        raise RuntimeError("refine failed")

    monkeypatch.setattr(action_guard, "_refine_enforce_outcome", raise_refine)

    outcome = _guard(_view_args(tmp_path))

    assert outcome.proceed is False
    assert outcome.decision is not None
    assert outcome.decision.verdict == "requires_grant"
    assert outcome.blocked_reason == "canonicalization_rejected"
    assert outcome.sealed_args is None


def test_refine_base_exception_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)

    def interrupt_refine(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        action_guard,
        "_refine_enforce_outcome",
        interrupt_refine,
    )

    with pytest.raises(KeyboardInterrupt):
        _guard(_view_args(tmp_path))


def test_refined_seal_reaches_dispatch_handler_by_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_enforce(monkeypatch)
    _publish_root(tmp_path)
    sealed: list[Mapping[str, object]] = []
    received: list[Mapping[str, object]] = []
    real_refine = action_guard._refine_enforce_outcome

    def record_refine(*args, **kwargs):
        outcome = real_refine(*args, **kwargs)
        assert outcome.sealed_args is not None
        sealed.append(outcome.sealed_args)
        return outcome

    monkeypatch.setattr(action_guard, "_refine_enforce_outcome", record_refine)

    async def handler(args: Mapping[str, object]) -> str:
        received.append(args)
        return "viewed"

    dispatcher = ToolDispatcher()
    dispatcher.register(_TOOL, handler)
    raw_args = _view_args(tmp_path)

    result = asyncio.run(
        dispatcher.execute(
            "tu1",
            _TOOL,
            raw_args,
            execution_context=_bound_context(),
        )
    )

    assert result.is_error is False
    assert result.content == "viewed"
    assert len(sealed) == len(received) == 1
    assert received[0] is sealed[0]
    assert received[0] is not raw_args
    assert received[0] == raw_args

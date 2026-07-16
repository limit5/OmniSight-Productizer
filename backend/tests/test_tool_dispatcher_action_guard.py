"""U6-0 T7b — fault-injection tests for the ToolDispatcher action-guard wiring.

``ToolDispatcher.execute`` is the single runner-SDK dispatch chokepoint
(adapter_namespace="runner_sdk"): all three wrappers (``ToolWrapper``,
``invoke_tool``, ``DetectorAwareDispatcher``) funnel through it, so one
guard call covers the whole runner surface. Tool names below are REAL
schema-registered AND registry-classified names (``Write``/``Read``…);
handlers are fakes. Also covers the outright proficiency fail-open fix
(codex M8): a gate ERROR now fails CLOSED for non-read-only tools.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from backend import db_pool
from backend.agents import action_challenge, action_guard, provenance, tool_dispatcher
from backend.agents import execution_context as ec
from backend.agents.action_canonicalize import PreparedAction
from backend.agents.authorization_kernel import AuthorizationDecision
from backend.agents.circuit_breaker import CircuitBreaker
from backend.agents.context_reset import DetectorAwareDispatcher
from backend.agents.loop_detector import LoopDetector
from backend.agents.memory_tool_handler import MEMORY_TOOL_NAME
from backend.agents.tool_call_wrapper import invoke_tool, reset_circuits_for_tests
from backend.agents.tool_dispatcher import ToolDispatcher
from backend.agents.tool_registry import OperationDescriptor
from backend.agents.tool_registry import resolve as resolve_tool
from backend.agents.tool_wrapper import ToolWrapper


# ── shared fixtures ──────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_guard_matrix(monkeypatch: pytest.MonkeyPatch):
    """Each test starts AND ends on the default all-shadow matrix.

    pytest's monkeypatch restores the ENV at teardown but NOT the
    already-reloaded module-global matrix — reload on both edges.
    """
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    action_guard.reload_mode_matrix_for_tests()
    yield
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    action_guard.reload_mode_matrix_for_tests()


# Real SDK schema names that are ALSO registry-classified (register()
# validates against tool_schemas; the guard resolves via tool_registry).
_MUTATING = "Write"     # mutating / code_write
_READ_ONLY = "Read"     # read_only / read_only


def _enforce(monkeypatch: pytest.MonkeyPatch, adapter: str, tool_name: str) -> str:
    """Flip ``(adapter, family-of(tool_name))`` to enforce; return the family."""
    family = resolve_tool(tool_name).family
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", f"{adapter}:{family}=enforce")
    action_guard.reload_mode_matrix_for_tests()
    return family


def _ctx_service() -> ec.ExecutionContext:
    return ec.for_service(
        service_name="runner-launcher",
        tenant_id="t-default",
        request_id="r1",
        roles=("runner",),
        authorization_source="server_launcher",
    )


def _make_handler(result: str = "ok"):
    calls: list[dict] = []

    async def _handler(args: dict) -> str:
        calls.append(args)
        return result

    return _handler, calls


def _dispatcher(tool_name: str = _MUTATING, result: str = "ok"):
    d = ToolDispatcher()
    handler, calls = _make_handler(result)
    d.register(tool_name, handler)
    return d, calls


def _payload(res) -> dict:
    return json.loads(res.content)


def _prepared() -> PreparedAction:
    return PreparedAction(
        operation_descriptor=OperationDescriptor(
            tool_name=_MUTATING,
            effect="mutating",
            family="code_write",
        ),
        canonical_target="/workspace/output.txt",
        executable_args={"file_path": "/workspace/output.txt"},
        human_rendering={"summary": "Write output.txt"},
    )


def _decision(prepared: PreparedAction) -> AuthorizationDecision:
    return AuthorizationDecision(
        verdict="requires_grant",
        operation_descriptor=prepared.operation_descriptor,
        reason="mutating_needs_grant:code_write",
        execution_context=_ctx_service(),
    )


def _turn_provenance() -> provenance.ModelSnapshot:
    return provenance.ModelSnapshot(
        provenance._build_snapshot(
            (),
            completeness="complete",
            omissions=(),
            snapshot_id="psnap-runner-challenge",
        )
    )


def _install_blocked_guard(
    monkeypatch: pytest.MonkeyPatch,
    outcome: action_guard.GuardOutcome,
) -> None:
    monkeypatch.setattr(
        tool_dispatcher,
        "guard_tool_dispatch",
        lambda **_kwargs: outcome,
    )


def _seal_guard(
    monkeypatch: pytest.MonkeyPatch,
    sealed_args: dict | None,
) -> None:
    outcome = action_guard.GuardOutcome(
        proceed=True,
        decision=None,
        family="code_write",
        mode="shadow",
        sealed_args=sealed_args,
    )
    monkeypatch.setattr(
        tool_dispatcher,
        "guard_tool_dispatch",
        lambda **_kwargs: outcome,
    )


# ── 1. shadow default: mutating handler runs ─────────────────────────────
def test_shadow_default_runs_mutating_handler() -> None:
    d, calls = _dispatcher(_MUTATING, "wrote")
    res = asyncio.run(d.execute("tu1", _MUTATING, {"file_path": "x"}))
    assert calls, "default all-shadow matrix must observe-and-proceed"
    assert not res.is_error
    assert res.content == "wrote"


# ── 2. enforce blocks: handler NOT called ────────────────────────────────
def test_enforce_blocks_mutating_handler_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, calls = _dispatcher()
    _enforce(monkeypatch, "runner_sdk", _MUTATING)
    res = asyncio.run(
        d.execute("tu1", _MUTATING, {}, execution_context=_ctx_service())
    )
    assert not calls, "enforce must block BEFORE the handler runs"
    assert res.is_error
    payload = _payload(res)
    assert payload["error"] == "action_guard_denied"
    assert payload["blocked_reason"] == "requires_grant"
    assert payload["mode"] == "enforce"
    assert payload["retryable"] is False


# ── 3. (adapter, family) keying: chat enforce does not leak ──────────────
def test_chat_enforce_does_not_leak_to_runner_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enforce(monkeypatch, "chat", _MUTATING)
    d, calls = _dispatcher()
    res = asyncio.run(
        d.execute("tu1", _MUTATING, {}, execution_context=_ctx_service())
    )
    assert calls, "chat-scoped enforce must not block the runner_sdk adapter"
    assert not res.is_error


# ── 4. ctx threading: unbound / bound / read-only ────────────────────────
def test_missing_ctx_enforce_blocks_unbound_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, calls = _dispatcher()
    _enforce(monkeypatch, "runner_sdk", _MUTATING)
    res = asyncio.run(d.execute("tu1", _MUTATING, {}))
    assert not calls
    assert res.is_error
    assert _payload(res)["blocked_reason"] == "unbound_principal"


def test_missing_ctx_shadow_proceeds() -> None:
    d, calls = _dispatcher()
    res = asyncio.run(d.execute("tu1", _MUTATING, {}))
    assert calls, "shadow observes-and-proceeds even for an unbound principal"
    assert not res.is_error


def test_bound_service_ctx_enforce_blocks_requires_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d, calls = _dispatcher()
    _enforce(monkeypatch, "runner_sdk", _MUTATING)
    res = asyncio.run(
        d.execute("tu1", _MUTATING, {}, execution_context=_ctx_service())
    )
    assert not calls
    assert _payload(res)["blocked_reason"] == "requires_grant"


def test_read_only_allowed_under_shadow_and_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default shadow.
    d, calls = _dispatcher(_READ_ONLY, "contents")
    res = asyncio.run(d.execute("tu1", _READ_ONLY, {}))
    assert calls and not res.is_error

    # Enforce on the read_only family: allow is allow regardless of mode.
    _enforce(monkeypatch, "runner_sdk", _READ_ONLY)
    d2, calls2 = _dispatcher(_READ_ONLY, "contents")
    res2 = asyncio.run(
        d2.execute("tu2", _READ_ONLY, {}, execution_context=_ctx_service())
    )
    assert calls2 and not res2.is_error


# ── 5. guard-internals raise: error outcome, no escape ───────────────────
def test_guard_raise_shadow_proceeds_enforce_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch the guard's stage-1-only for_unbound (NOT resolve_mode — the
    # stage-1 except handler itself calls resolve_mode(adapter,
    # "__error__"), so an unconditional raiser there would double-raise
    # and escape) and call execute WITHOUT a ctx to hit the fallback.
    def _boom():
        raise RuntimeError("guard internals exploded")

    monkeypatch.setattr(action_guard, "for_unbound", _boom)

    # All-shadow: error outcome observes-and-proceeds; nothing escapes.
    d, calls = _dispatcher()
    res = asyncio.run(d.execute("tu1", _MUTATING, {}))
    assert calls, "stage-1 raise in all-shadow must proceed (error outcome)"
    assert not res.is_error

    # Any runner_sdk enforce entry ⇒ the error fails closed; no escape.
    _enforce(monkeypatch, "runner_sdk", _MUTATING)
    d2, calls2 = _dispatcher()
    res2 = asyncio.run(d2.execute("tu2", _MUTATING, {}))
    assert not calls2
    assert res2.is_error
    payload = _payload(res2)
    assert payload["error"] == "action_guard_denied"
    assert payload["blocked_reason"] == "guard_error"


# ── 6. proficiency-gate error path: fail-closed fix (codex M8) ───────────
async def _raising_gate(_tool_name: str, _agent_id: str) -> bool:
    raise RuntimeError("gate down")


async def _refusing_gate(_tool_name: str, _agent_id: str) -> bool:
    return False


def test_proficiency_gate_error_fails_closed_for_mutating() -> None:
    d, calls = _dispatcher()
    d.set_proficiency_gate(_raising_gate, agent_id="a1")
    res = asyncio.run(d.execute("tu1", _MUTATING, {}))
    assert not calls, "gate error + mutating tool must fail CLOSED (no fail-open)"
    assert res.is_error
    payload = _payload(res)
    assert payload["error"] == "tool_proficiency_gate_error"
    assert payload["retryable"] is False


def test_proficiency_gate_error_lets_read_only_proceed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    d, calls = _dispatcher(_READ_ONLY, "contents")
    d.set_proficiency_gate(_raising_gate, agent_id="a1")
    with caplog.at_level("WARNING", logger="backend.agents.tool_dispatcher"):
        res = asyncio.run(d.execute("tu1", _READ_ONLY, {}))
    assert calls, "gate error + authoritatively read-only tool proceeds"
    assert not res.is_error
    assert any("read-only proceeds" in r.message for r in caplog.records)


def test_proficiency_gate_refusal_still_yields_insufficient() -> None:
    d, calls = _dispatcher()
    d.set_proficiency_gate(_refusing_gate, agent_id="a1")
    res = asyncio.run(d.execute("tu1", _MUTATING, {}))
    assert not calls
    assert _payload(res)["error"] == "tool_proficiency_insufficient"


# ── 7. wrapper inventory (codex M12): all paths hit the ONE guard ────────
def _install_guard_recorder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def _recorder(**kwargs):
        calls.append(kwargs)
        return action_guard.GuardOutcome(
            proceed=True, decision=None, family="code_write", mode="shadow"
        )

    monkeypatch.setattr(tool_dispatcher, "guard_tool_dispatch", _recorder)
    return calls


def test_all_three_wrappers_funnel_through_the_guarded_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _install_guard_recorder(monkeypatch)
    d, handler_calls = _dispatcher()

    # Path 1 — ToolWrapper.invoke (tool_wrapper.py).
    wrapper = ToolWrapper(dispatcher=d, breaker=CircuitBreaker(name="t7b-test"))
    res1 = asyncio.run(wrapper.invoke("tu-a", _MUTATING, {"file_path": "a"}))
    assert not res1.is_error
    assert len(recorded) == 1

    # Path 2 — invoke_tool (tool_call_wrapper.py).
    reset_circuits_for_tests()
    res2 = asyncio.run(
        invoke_tool(_MUTATING, {"file_path": "b"}, dispatcher=d, tool_use_id="tu-b")
    )
    assert not res2.is_error
    assert len(recorded) == 2

    # Path 3 — DetectorAwareDispatcher.execute (context_reset.py), with a
    # real ctx: assert the recorder received THAT ctx (threaded, not lost).
    ctx = _ctx_service()
    detector_dispatcher = DetectorAwareDispatcher(
        inner=d, detector=LoopDetector(ticket_key="OP-2609-test")
    )
    res3 = asyncio.run(
        detector_dispatcher.execute(
            "tu-c", _MUTATING, {"file_path": "c"}, execution_context=ctx
        )
    )
    assert not res3.is_error
    assert len(recorded) == 3
    assert recorded[2]["execution_context"] is ctx

    # No wrapper reached the handler except via the guarded execute.
    assert len(handler_calls) == 3
    assert all(call["adapter_namespace"] == "runner_sdk" for call in recorded)


def test_wrapper_modules_have_no_direct_handler_access() -> None:
    agents_dir = (
        pathlib.Path(__file__).resolve().parents[1] / "agents"
    )
    for module in ("tool_wrapper.py", "tool_call_wrapper.py", "context_reset.py"):
        text = (agents_dir / module).read_text(encoding="utf-8")
        assert "_handlers" not in text, (
            f"{module} must reach handlers only via ToolDispatcher.execute"
        )


# ── 8. Memory Tool coverage note ─────────────────────────────────────────
def test_memory_tool_is_classified_memory_write() -> None:
    # bind_memory_tool registers MEMORY_TOOL_NAME on this same dispatcher
    # class, so the chokepoint guard covers it. The raw handle() write-sink
    # hardening remains T8 (out of scope here).
    assert MEMORY_TOOL_NAME == "memory"
    descriptor = resolve_tool(MEMORY_TOOL_NAME)
    assert descriptor.family == "memory_write"
    assert descriptor.effect == "mutating"


# ── 9. sealed authorization args bind every handler path ─────────────────
def test_async_handler_executes_sealed_args_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = {"file_path": "sealed"}
    live = {"file_path": "live"}
    _seal_guard(monkeypatch, sealed)
    d, calls = _dispatcher()

    res = asyncio.run(d.execute("tu-sealed-async", _MUTATING, live))

    assert not res.is_error
    assert calls[0] is sealed
    assert calls[0] is not live


def test_sync_handler_executes_sealed_args_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = {"file_path": "sealed"}
    live = {"file_path": "live"}
    calls: list[dict] = []

    def _sync_handler(args: dict) -> str:
        calls.append(args)
        return "ok"

    _seal_guard(monkeypatch, sealed)
    d = ToolDispatcher()
    d.register(_MUTATING, _sync_handler)

    res = asyncio.run(d.execute("tu-sealed-sync", _MUTATING, live))

    assert not res.is_error
    assert calls[0] is sealed
    assert calls[0] is not live


def test_async_handler_executes_live_args_when_seal_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"file_path": "live"}
    _seal_guard(monkeypatch, None)
    d, calls = _dispatcher()

    res = asyncio.run(d.execute("tu-live-async", _MUTATING, live))

    assert not res.is_error
    assert calls[0] is live


def test_sync_handler_executes_live_args_when_seal_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {"file_path": "live"}
    calls: list[dict] = []

    def _sync_handler(args: dict) -> str:
        calls.append(args)
        return "ok"

    _seal_guard(monkeypatch, None)
    d = ToolDispatcher()
    d.register(_MUTATING, _sync_handler)

    res = asyncio.run(d.execute("tu-live-sync", _MUTATING, live))

    assert not res.is_error
    assert calls[0] is live


def test_handler_error_diagnostics_use_executed_sealed_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = {"file_path": "sealed"}
    live = {"file_path": "live"}
    handler_calls: list[dict] = []
    diagnostic_calls: list[dict] = []

    async def _raising_handler(args: dict) -> str:
        handler_calls.append(args)
        raise RuntimeError("handler exploded")

    def _record_diagnostic(
        _exc: Exception,
        _tool_name: str,
        execution_args: dict,
    ) -> tool_dispatcher.ToolError:
        diagnostic_calls.append(execution_args)
        return tool_dispatcher.ToolError(
            error="tool_raised",
            error_type="RuntimeError",
            retryable=False,
            hint="handler exploded",
        )

    _seal_guard(monkeypatch, sealed)
    monkeypatch.setattr(
        tool_dispatcher,
        "_tool_error_from_exception",
        _record_diagnostic,
    )
    d = ToolDispatcher()
    d.register(_MUTATING, _raising_handler)

    res = asyncio.run(d.execute("tu-sealed-error", _MUTATING, live))

    assert res.is_error
    assert handler_calls[0] is sealed
    assert diagnostic_calls[0] is sealed
    assert diagnostic_calls[0] is not live


# ── 10. blocked runner challenge creation ────────────────────────────────
def test_guard_outcome_echoes_coordinates_and_failsafe_stays_empty() -> None:
    outcome = action_guard.guard_tool_dispatch(
        adapter_namespace="runner_sdk",
        tool_name=_MUTATING,
        schema_version="v1",
        raw_args={},
        execution_context=_ctx_service(),
    )

    assert outcome.mode == "shadow"
    assert outcome.adapter_namespace == "runner_sdk"
    assert outcome.schema_version == "v1"
    assert action_guard._FAILSAFE_OUTCOME.challenge_prepared is None
    assert action_guard._FAILSAFE_OUTCOME.adapter_namespace == ""
    assert action_guard._FAILSAFE_OUTCOME.schema_version == ""


def test_runner_shadow_block_is_byte_identical_without_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = action_guard.GuardOutcome(
        proceed=False,
        decision=None,
        family="code_write",
        mode="shadow",
        blocked_reason="requires_grant",
    )
    _install_blocked_guard(monkeypatch, outcome)
    monkeypatch.setattr(
        db_pool,
        "get_pool",
        lambda: pytest.fail("shadow block must not access the pool"),
    )
    d, calls = _dispatcher()

    result = asyncio.run(d.execute("tu-shadow-block", _MUTATING, {}))

    assert calls == []
    assert result.is_error is True
    assert result.content == (
        '{"error": "action_guard_denied", '
        '"error_type": "ActionGuardDenied", "retryable": false, '
        '"hint": "action guard denied Write: requires_grant", '
        '"suggested_tool": null, "suggested_args": null, '
        '"tool_name": "Write", "blocked_reason": "requires_grant", '
        '"mode": "shadow"}'
    )
    assert "challenge_id" not in _payload(result)


def test_runner_enforce_challenge_surfaces_id_and_threads_sealed_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared()
    decision = _decision(prepared)
    turn_provenance = _turn_provenance()
    outcome = action_guard.GuardOutcome(
        proceed=False,
        decision=decision,
        family="code_write",
        mode="enforce",
        blocked_reason="requires_grant",
        challenge_prepared=prepared,
        adapter_namespace="runner_sdk",
        schema_version="v1",
    )
    _install_blocked_guard(monkeypatch, outcome)
    pool = object()
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    recorded: dict[str, object] = {}

    async def _create(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return "chal-XYZ"

    monkeypatch.setattr(
        action_challenge,
        "create_challenge_from_block",
        _create,
    )
    d, calls = _dispatcher()

    result = asyncio.run(
        d.execute(
            "tu-enforce-challenge",
            _MUTATING,
            {},
            turn_provenance=turn_provenance,
        )
    )

    payload = _payload(result)
    assert calls == []
    assert result.is_error is True
    assert payload["challenge_id"] == "chal-XYZ"
    assert "pending approval, challenge=chal-XYZ" in payload["hint"]
    assert payload["retryable"] is False
    args = recorded["args"]
    assert isinstance(args, tuple)
    assert args[0] is pool
    assert args[1] is prepared
    assert args[2] is decision.execution_context
    assert args[3] is turn_provenance
    assert recorded["kwargs"] == {
        "adapter_namespace": "runner_sdk",
        "tool_name": prepared.operation_descriptor.tool_name,
        "schema_version": "v1",
    }


def test_runner_challenge_get_pool_failure_returns_bare_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared()
    outcome = action_guard.GuardOutcome(
        proceed=False,
        decision=_decision(prepared),
        family="code_write",
        mode="enforce",
        blocked_reason="requires_grant",
        challenge_prepared=prepared,
        adapter_namespace="runner_sdk",
        schema_version="v1",
    )
    _install_blocked_guard(monkeypatch, outcome)

    def _raise_get_pool():
        raise RuntimeError("pool is not initialized")

    monkeypatch.setattr(db_pool, "get_pool", _raise_get_pool)
    monkeypatch.setattr(
        action_challenge,
        "create_challenge_from_block",
        lambda *_args, **_kwargs: pytest.fail("helper must not run"),
    )
    d, calls = _dispatcher()

    result = asyncio.run(d.execute("tu-pool-failure", _MUTATING, {}))

    payload = _payload(result)
    assert calls == []
    assert result.is_error is True
    assert payload["hint"] == "action guard denied Write: requires_grant"
    assert "challenge_id" not in payload


def test_runner_challenge_timeout_returns_bare_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared()
    outcome = action_guard.GuardOutcome(
        proceed=False,
        decision=_decision(prepared),
        family="code_write",
        mode="enforce",
        blocked_reason="requires_grant",
        challenge_prepared=prepared,
        adapter_namespace="runner_sdk",
        schema_version="v1",
    )
    _install_blocked_guard(monkeypatch, outcome)
    monkeypatch.setattr(db_pool, "get_pool", object)
    monkeypatch.setattr(tool_dispatcher, "CHALLENGE_CREATE_TIMEOUT_S", 0.001)

    async def _slow_create(*_args, **_kwargs):
        await asyncio.sleep(1)
        return "chal-too-late"

    monkeypatch.setattr(
        action_challenge,
        "create_challenge_from_block",
        _slow_create,
    )
    d, calls = _dispatcher()

    result = asyncio.run(d.execute("tu-timeout", _MUTATING, {}))

    payload = _payload(result)
    assert calls == []
    assert result.is_error is True
    assert payload["hint"] == "action guard denied Write: requires_grant"
    assert "challenge_id" not in payload


@pytest.mark.parametrize(
    ("adapter_namespace", "schema_version", "with_decision"),
    [
        ("", "v1", True),
        ("runner_sdk", "", True),
        ("runner_sdk", "v1", False),
    ],
    ids=("empty-adapter", "empty-schema", "missing-decision"),
)
def test_runner_challenge_missing_invariant_skips_creation(
    monkeypatch: pytest.MonkeyPatch,
    adapter_namespace: str,
    schema_version: str,
    with_decision: bool,
) -> None:
    prepared = _prepared()
    outcome = action_guard.GuardOutcome(
        proceed=False,
        decision=_decision(prepared) if with_decision else None,
        family="code_write",
        mode="enforce",
        blocked_reason="requires_grant",
        challenge_prepared=prepared,
        adapter_namespace=adapter_namespace,
        schema_version=schema_version,
    )
    _install_blocked_guard(monkeypatch, outcome)
    monkeypatch.setattr(
        db_pool,
        "get_pool",
        lambda: pytest.fail("incomplete carrier must not access the pool"),
    )
    monkeypatch.setattr(
        action_challenge,
        "create_challenge_from_block",
        lambda *_args, **_kwargs: pytest.fail("helper must not run"),
    )
    d, calls = _dispatcher()

    result = asyncio.run(d.execute("tu-missing-invariant", _MUTATING, {}))

    payload = _payload(result)
    assert calls == []
    assert result.is_error is True
    assert payload["hint"] == "action guard denied Write: requires_grant"
    assert "challenge_id" not in payload

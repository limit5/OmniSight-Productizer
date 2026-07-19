"""U6-0 T7a — fault-injection tests for the nodes.py action-guard wiring.

Covers the three adapter sites wired by T7a:

* chat ``_run_tool_rounds``  (adapter_namespace="chat")
* specialist ``tool_executor_node``  ("specialist") + the PEP fail-open
  fix (on a PEP gateway error only an authoritatively-classified
  read-only tool may run; mutating/unknown fails CLOSED)
* A2A ``external_agent_node``  ("a2a", dormant in prod) + the explicit
  tenant-mismatch denial (never a Python ``assert``)

Per site: enforce ⇒ handler NOT run + ``[BLOCKED]``; shadow ⇒ handler
runs; kernel-raise ⇒ error outcome (shadow proceeds / enforce blocks).
The guard resolves families via the REAL tool registry, so tool names
here are real registered names (fake handlers, real classification).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.agents import action_guard, nodes
from backend.agents import execution_context as ec
from backend.agents.nodes import external_agent_node_factory, tool_executor_node
from backend.agents.provenance import (
    A2A_RESULT,
    Attestation,
    active_collector,
    digest,
    provenance_scope,
)
from backend.agents.state import GraphState, ToolCall
from backend.agents.tool_registry import resolve as resolve_tool
from backend.auth import User


# ── shared fixtures ──────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_guard_matrix(monkeypatch: pytest.MonkeyPatch):
    """Each test starts from the default all-shadow matrix.

    pytest's monkeypatch restores the ENV at teardown but NOT the already-
    reloaded module-global matrix — reload at the start of every test.
    """
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    action_guard.reload_mode_matrix_for_tests()
    yield
    monkeypatch.delenv("OMNISIGHT_ACTION_GUARD_MODE", raising=False)
    action_guard.reload_mode_matrix_for_tests()


def _enforce(monkeypatch: pytest.MonkeyPatch, adapter: str, tool_name: str) -> str:
    """Flip ``(adapter, family-of(tool_name))`` to enforce; return the family."""
    family = resolve_tool(tool_name).family
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", f"{adapter}:{family}=enforce")
    action_guard.reload_mode_matrix_for_tests()
    return family


def _ctx_human(tenant_id: str = "t-default") -> ec.ExecutionContext:
    return ec.for_human(
        user=User(id="u1", email="u@x", name="U", role="operator"),
        tenant_id=tenant_id,
        session_id="s1",
        request_id="r1",
        message_id="m1",
        authorization_source="chat",
    )


class _FakeTool:
    """Minimal TOOL_MAP entry: records invocations, returns a canned result."""

    def __init__(self, result: str = "[OK] done") -> None:
        self.calls: list[dict] = []
        self._result = result

    async def ainvoke(self, args: dict) -> str:
        self.calls.append(args)
        return self._result


def _seal_guard(monkeypatch, sealed_args: dict | None) -> None:
    outcome = action_guard.GuardOutcome(
        proceed=True,
        decision=None,
        family="code_write",
        mode="shadow",
        sealed_args=sealed_args,
    )
    monkeypatch.setattr(
        nodes,
        "guard_tool_dispatch",
        lambda **_kwargs: outcome,
    )


# ── chat: _run_tool_rounds ───────────────────────────────────────────────
class _Reply:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _ScriptedLLM:
    """Returns the next scripted reply per invoke (then a plain answer)."""

    def __init__(self, script: list[_Reply] | None = None) -> None:
        self._script = list(script or [])

    def invoke(self, _convo):
        if self._script:
            return self._script.pop(0)
        return _Reply(content="done")


# supervisor_requeue_ticket is a REAL registered mutating tool name;
# supervisor_ticket_detail is a REAL read-only one. Handlers are fakes.
_CHAT_MUTATING = "supervisor_requeue_ticket"
_CHAT_READ_ONLY = "supervisor_ticket_detail"


def _run_chat(
    monkeypatch,
    tool: _FakeTool,
    tool_name: str,
    *,
    execution_context=None,
    tool_args: dict | None = None,
):
    monkeypatch.setattr(nodes, "TOOL_MAP", {tool_name: tool})
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    live_args = {"ticket_key": "OP-1"} if tool_args is None else tool_args
    first = _Reply(
        tool_calls=[{"name": tool_name, "args": live_args, "id": "c1"}]
    )
    convo: list = ["sys", "user"]
    resp = asyncio.run(
        nodes._run_tool_rounds(
            first, convo, _ScriptedLLM(), _ScriptedLLM(),
            execution_context=execution_context,
        )
    )
    return resp, convo, tool


def test_chat_shadow_default_runs_mutating_tool(monkeypatch):
    _, _, tool = _run_chat(monkeypatch, _FakeTool(), _CHAT_MUTATING)
    assert tool.calls, "shadow (default matrix) must observe-and-proceed"


def test_chat_enforce_blocks_mutating_tool_handler_not_run(monkeypatch):
    _enforce(monkeypatch, "chat", _CHAT_MUTATING)
    tool = _FakeTool()
    monkeypatch.setattr(nodes, "TOOL_MAP", {_CHAT_MUTATING: tool})
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    progress: list[tuple] = []
    monkeypatch.setattr(
        nodes, "emit_tool_progress", lambda *a, **k: progress.append((a, k))
    )
    first = _Reply(tool_calls=[{"name": _CHAT_MUTATING, "args": {"ticket_key": "OP-1"}, "id": "c1"}])
    asyncio.run(
        nodes._run_tool_rounds(
            first, ["sys", "user"], _ScriptedLLM(), _ScriptedLLM(),
            execution_context=_ctx_human(),
        )
    )
    assert not tool.calls, "enforce must block BEFORE the handler runs"
    blocked = [a for a, _k in progress if len(a) >= 3 and str(a[2]).startswith("[BLOCKED]")]
    assert blocked, "a [BLOCKED] result must be emitted"


def test_chat_enforce_still_allows_read_only(monkeypatch):
    # Enforce the READ-ONLY family for chat: allow is allow regardless of mode.
    _enforce(monkeypatch, "chat", _CHAT_READ_ONLY)
    _, _, tool = _run_chat(
        monkeypatch, _FakeTool("[SUPERVISOR] OP-1 detail"), _CHAT_READ_ONLY,
        execution_context=_ctx_human(),
    )
    assert tool.calls, "read_only ⇒ allow, even under enforce"


def test_chat_kernel_raise_shadow_proceeds_enforce_blocks(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(action_guard, "authorize_action", _boom)
    # Shadow (default): error outcome observes-and-proceeds.
    _, _, tool = _run_chat(monkeypatch, _FakeTool(), _CHAT_MUTATING)
    assert tool.calls, "stage-1 raise in all-shadow must proceed (error outcome)"
    # Any enforce entry for the chat adapter ⇒ the error fails closed.
    _enforce(monkeypatch, "chat", _CHAT_MUTATING)
    _, _, tool2 = _run_chat(monkeypatch, _FakeTool(), _CHAT_MUTATING)
    assert not tool2.calls, "stage-1 raise must fail CLOSED when the adapter enforces"


def test_chat_blocked_write_burns_no_budget(monkeypatch):
    _enforce(monkeypatch, "chat", _CHAT_MUTATING)
    tool = _FakeTool()
    monkeypatch.setattr(nodes, "TOOL_MAP", {_CHAT_MUTATING: tool})
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    progress: list[str] = []
    monkeypatch.setattr(
        nodes, "emit_tool_progress",
        lambda _n, _s, out, *a, **k: progress.append(str(out)),
    )
    monkeypatch.setattr(nodes, "MAX_WRITE_CALLS_PER_TURN", 1)
    # Two identical blocked writes in one round: with budget=1, if a blocked
    # call HAD burned budget, the second would surface as a budget [FAILED]
    # instead of [BLOCKED]. (`_run_tool_rounds` copies the convo list, so
    # assertions go through the captured progress events.)
    first = _Reply(tool_calls=[
        {"name": _CHAT_MUTATING, "args": {"ticket_key": "OP-1"}, "id": "c1"},
        {"name": _CHAT_MUTATING, "args": {"ticket_key": "OP-1"}, "id": "c2"},
    ])
    asyncio.run(
        nodes._run_tool_rounds(
            first, ["sys", "user"], _ScriptedLLM(), _ScriptedLLM(),
            execution_context=_ctx_human(),
        )
    )
    assert not tool.calls
    blocked = [p for p in progress if p.startswith("[BLOCKED]")]
    assert len(blocked) == 2, f"both calls surface [BLOCKED]; got {progress}"
    assert not any(p.startswith("[FAILED]") for p in progress), (
        "a blocked write must not consume the write budget"
    )


def test_chat_executes_sealed_args_by_identity(monkeypatch):
    sealed = {"ticket_key": "OP-sealed"}
    live = {"ticket_key": "OP-live"}
    _seal_guard(monkeypatch, sealed)

    _, _, tool = _run_chat(
        monkeypatch,
        _FakeTool(),
        _CHAT_MUTATING,
        tool_args=live,
    )

    assert tool.calls[0] is sealed
    assert tool.calls[0] is not live


def test_chat_executes_live_args_when_seal_is_none(monkeypatch):
    live = {"ticket_key": "OP-live"}
    _seal_guard(monkeypatch, None)

    _, _, tool = _run_chat(
        monkeypatch,
        _FakeTool(),
        _CHAT_MUTATING,
        tool_args=live,
    )

    assert tool.calls[0] is live


# ── specialist: tool_executor_node ───────────────────────────────────────
_SPEC_MUTATING = "write_file"   # real registered mutating (code_write)
_SPEC_READ_ONLY = "read_file"   # real registered read-only


class _PepAllow:
    class _Dec:
        def __init__(self) -> None:
            from backend import pep_gateway as _pep
            # PepAction members are auto_allow / hold / deny — anything
            # that is not `deny` lets the node proceed to the guard.
            self.action = _pep.PepAction.auto_allow
            self.reason = "test-allow"

    @staticmethod
    async def evaluate(**_kw):
        return _PepAllow._Dec()


def _spec_state(tool_name: str, execution_context=None) -> GraphState:
    arguments = {"path": "x"}
    if tool_name == _SPEC_MUTATING:
        # A registered write_file reaches canonical enforcement in G6b-2b;
        # keep this fixture schema-valid so it tests the mutating verdict.
        arguments["content"] = "x"
    return GraphState(
        tool_calls=[ToolCall(tool_name=tool_name, arguments=arguments)],
        execution_context=execution_context,
    )


def _patch_spec_common(monkeypatch, tool: _FakeTool, tool_name: str) -> list:
    from backend import pep_gateway as _pep

    monkeypatch.setattr(nodes, "TOOL_MAP", {tool_name: tool})
    monkeypatch.setattr(nodes, "emit_pipeline_phase", lambda *a, **k: None)
    progress: list = []
    monkeypatch.setattr(
        nodes, "emit_tool_progress", lambda *a, **k: progress.append((a, k))
    )
    monkeypatch.setattr(nodes, "set_active_workspace", lambda *a, **k: None)
    monkeypatch.setattr(_pep, "evaluate", _PepAllow.evaluate)
    return progress


def test_specialist_shadow_default_runs_tool(monkeypatch):
    tool = _FakeTool()
    _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)
    update = asyncio.run(tool_executor_node(_spec_state(_SPEC_MUTATING)))
    assert tool.calls
    assert update["tool_results"][0].success


def test_specialist_enforce_blocks_handler_not_run(monkeypatch):
    _enforce(monkeypatch, "specialist", _SPEC_MUTATING)
    tool = _FakeTool()
    _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)
    update = asyncio.run(
        tool_executor_node(_spec_state(_SPEC_MUTATING, _ctx_human()))
    )
    assert not tool.calls
    res = update["tool_results"][0]
    assert not res.success and res.output.startswith("[BLOCKED]")
    assert "requires_grant" in res.output


def test_specialist_chat_enforce_does_not_leak_to_specialist(monkeypatch):
    # (adapter, family) keying: enforcing chat:code_write leaves the
    # SPECIALIST adapter in shadow for the same family.
    family = resolve_tool(_SPEC_MUTATING).family
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", f"chat:{family}=enforce")
    action_guard.reload_mode_matrix_for_tests()
    tool = _FakeTool()
    _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)
    asyncio.run(tool_executor_node(_spec_state(_SPEC_MUTATING)))
    assert tool.calls, "chat-scoped enforce must not block the specialist adapter"


def test_specialist_pep_raise_fails_closed_for_mutating(monkeypatch):
    from backend import pep_gateway as _pep

    tool = _FakeTool()
    progress = _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)

    async def _pep_boom(**_kw):
        raise RuntimeError("pep down")

    monkeypatch.setattr(_pep, "evaluate", _pep_boom)
    update = asyncio.run(tool_executor_node(_spec_state(_SPEC_MUTATING)))
    assert not tool.calls, "PEP error + mutating tool must fail CLOSED (no fail-open)"
    res = update["tool_results"][0]
    assert not res.success and res.output.startswith("[BLOCKED]")
    assert "fail-closed" in res.output
    del progress


def test_specialist_pep_raise_lets_read_only_proceed(monkeypatch):
    from backend import pep_gateway as _pep

    tool = _FakeTool("file contents")
    _patch_spec_common(monkeypatch, tool, _SPEC_READ_ONLY)

    async def _pep_boom(**_kw):
        raise RuntimeError("pep down")

    monkeypatch.setattr(_pep, "evaluate", _pep_boom)
    update = asyncio.run(tool_executor_node(_spec_state(_SPEC_READ_ONLY)))
    assert tool.calls, "PEP error + authoritatively read-only tool proceeds"
    assert update["tool_results"][0].success


def test_specialist_executes_sealed_args_by_identity(monkeypatch):
    sealed = {"path": "sealed"}
    state = _spec_state(_SPEC_MUTATING)
    live = state.tool_calls[0].arguments
    tool = _FakeTool()
    _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)
    _seal_guard(monkeypatch, sealed)

    update = asyncio.run(tool_executor_node(state))

    assert update["tool_results"][0].success
    assert tool.calls[0] is sealed
    assert tool.calls[0] is not live


def test_specialist_executes_live_args_when_seal_is_none(monkeypatch):
    state = _spec_state(_SPEC_MUTATING)
    live = state.tool_calls[0].arguments
    tool = _FakeTool()
    _patch_spec_common(monkeypatch, tool, _SPEC_MUTATING)
    _seal_guard(monkeypatch, None)

    update = asyncio.run(tool_executor_node(state))

    assert update["tool_results"][0].success
    assert tool.calls[0] is live


# ── A2A: external_agent_node (dormant in prod; guarded defensively) ──────
class _FakeEndpoint:
    agent_name = "demo-agent"


class _FakeA2AClient:
    def __init__(self, log: list) -> None:
        self._log = log

    async def invoke(self, agent_name: str, payload: dict) -> object:
        self._log.append((agent_name, payload))

        class _R:
            payload = {"status": "ok"}

        return _R()


class _FakeRegistry:
    def __init__(self) -> None:
        self.endpoint_calls: list = []
        self.invocations: list = []

    async def get_endpoint(self, agent_id: str, *, require_enabled: bool):
        self.endpoint_calls.append(agent_id)
        return _FakeEndpoint()

    async def build_client(self, agent_id: str, *, tenant_id: str, bearer_token: str):
        return _FakeA2AClient(self.invocations)


def _a2a_node(registry: _FakeRegistry):
    return external_agent_node_factory(
        "demo-agent", registry=registry, tenant_id="t-node",
    )


def test_a2a_shadow_same_tenant_invokes(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    reg = _FakeRegistry()
    node = _a2a_node(reg)
    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-node"))))
    assert reg.invocations, "shadow + matching tenant must invoke"
    assert update["tool_results"][0].success


def test_a2a_response_records_unattested_provenance(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    reg = _FakeRegistry()
    node = _a2a_node(reg)

    with provenance_scope() as col:
        update = asyncio.run(
            node(GraphState(execution_context=_ctx_human("t-node")))
        )

    assert update["tool_results"][0].success
    assert len(col._records) == 1
    record = col._records[0]
    assert record.source_kind == A2A_RESULT
    assert record.source_id == "a2a:demo-agent"
    assert record.attestation == Attestation.UNATTESTED
    assert record.content_digest == digest(
        json.dumps({"status": "ok"}, ensure_ascii=False, sort_keys=True)
    )


def test_a2a_response_without_provenance_scope_is_noop(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    observed_collectors = []
    original_record_content = nodes.record_content

    def _observe_collector(collector, *args, **kwargs):  # noqa: ANN001
        observed_collectors.append(collector)
        return original_record_content(collector, *args, **kwargs)

    monkeypatch.setattr(nodes, "record_content", _observe_collector)
    reg = _FakeRegistry()
    node = _a2a_node(reg)

    assert active_collector() is None
    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-node"))))

    expected_output = json.dumps(
        {"status": "ok"}, ensure_ascii=False, sort_keys=True
    )
    assert update["tool_results"][0].output == expected_output
    assert update["messages"][0].content == expected_output
    assert observed_collectors == [None]
    assert active_collector() is None


def test_a2a_response_capture_is_behavior_neutral(monkeypatch):
    events = []
    monkeypatch.setattr(
        nodes,
        "emit_tool_progress",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    without_scope_reg = _FakeRegistry()
    without_scope = asyncio.run(
        _a2a_node(without_scope_reg)(
            GraphState(execution_context=_ctx_human("t-node"))
        )
    )
    without_scope_events = list(events)
    events.clear()

    with_scope_reg = _FakeRegistry()
    with provenance_scope():
        with_scope = asyncio.run(
            _a2a_node(with_scope_reg)(
                GraphState(execution_context=_ctx_human("t-node"))
            )
        )

    assert with_scope == without_scope
    assert events == without_scope_events
    assert with_scope_reg.invocations == without_scope_reg.invocations


def test_a2a_tenant_mismatch_is_explicit_denial_before_endpoint(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    reg = _FakeRegistry()
    node = _a2a_node(reg)
    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-OTHER"))))
    assert not reg.endpoint_calls, "denial must precede endpoint/client work"
    assert not reg.invocations
    res = update["tool_results"][0]
    assert not res.success and res.output.startswith("[BLOCKED]")
    assert "tenant mismatch" in res.output


def test_a2a_enforce_blocks_before_invoke(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    # external_agent:<id> resolves via the registry prefix → delegation.
    family = resolve_tool("external_agent:demo-agent").family
    monkeypatch.setenv("OMNISIGHT_ACTION_GUARD_MODE", f"a2a:{family}=enforce")
    action_guard.reload_mode_matrix_for_tests()
    reg = _FakeRegistry()
    node = _a2a_node(reg)
    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-node"))))
    assert not reg.endpoint_calls and not reg.invocations
    res = update["tool_results"][0]
    assert not res.success and res.output.startswith("[BLOCKED]")


def test_a2a_no_ctx_dormant_shadow_proceeds(monkeypatch):
    # Dormant path: no execution_context on state (ctx=None ⇒ for_unbound
    # inside the guard; shadow observes) and no tenant check possible.
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    reg = _FakeRegistry()
    node = _a2a_node(reg)
    update = asyncio.run(node(GraphState()))
    assert reg.invocations
    assert update["tool_results"][0].success


def test_a2a_unexpected_seal_fails_closed_before_endpoint(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    _seal_guard(monkeypatch, {"agent_id": "demo-agent"})
    reg = _FakeRegistry()
    node = _a2a_node(reg)

    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-node"))))

    assert not reg.endpoint_calls
    assert not reg.invocations
    result = update["tool_results"][0]
    assert not result.success
    assert result.output.startswith("[BLOCKED]")
    assert "unexpected authorization seal" in result.output


def test_a2a_none_seal_invokes_normally(monkeypatch):
    monkeypatch.setattr(nodes, "emit_tool_progress", lambda *a, **k: None)
    _seal_guard(monkeypatch, None)
    reg = _FakeRegistry()
    node = _a2a_node(reg)

    update = asyncio.run(node(GraphState(execution_context=_ctx_human("t-node"))))

    assert reg.invocations
    assert update["tool_results"][0].success


# ── U6-7 (INV-4): GuardOutcome.request_local_intent — observational only ──
def test_guard_outcome_intent_false_without_declared_scope():
    """No declared run-state ⇒ False on the outcome — and the verdict/proceed
    are exactly what they would be anyway (observational field)."""
    out = action_guard.guard_tool_dispatch(
        adapter_namespace="chat", tool_name="create_task", raw_args={},
        execution_context=_ctx_human(),
    )
    assert out.request_local_intent is False
    assert out.proceed is True  # default all-shadow matrix


def test_guard_outcome_intent_true_only_for_the_declaring_request():
    from backend.agents import u6_request_intent as ri

    ctx = _ctx_human()
    with ri.request_intent_scope(ri.declare_request_local_intent(ctx)):
        same = action_guard.guard_tool_dispatch(
            adapter_namespace="chat", tool_name="create_task", raw_args={},
            execution_context=ctx,
        )
        other = ec.for_human(
            user=User(id="u1", email="u@x", name="U", role="operator"),
            tenant_id="t-default", session_id="s1",
            request_id="r2-other", message_id="m1", authorization_source="chat",
        )
        cross = action_guard.guard_tool_dispatch(
            adapter_namespace="chat", tool_name="create_task", raw_args={},
            execution_context=other,
        )
    assert same.request_local_intent is True
    assert cross.request_local_intent is False  # request-LOCAL, never standing


def test_guard_outcome_intent_never_moves_the_verdict(monkeypatch):
    """With vs without intent, the verdict/proceed/blocked_reason are
    byte-identical — INV-4's field distinguishes, it does not authorize."""
    from backend.agents import u6_request_intent as ri

    _enforce(monkeypatch, "chat", "create_task")
    ctx = _ctx_human()
    without = action_guard.guard_tool_dispatch(
        adapter_namespace="chat", tool_name="create_task", raw_args={},
        execution_context=ctx,
    )
    with ri.request_intent_scope(ri.declare_request_local_intent(ctx)):
        with_intent = action_guard.guard_tool_dispatch(
            adapter_namespace="chat", tool_name="create_task", raw_args={},
            execution_context=ctx,
        )
    assert (without.proceed, without.blocked_reason) == (
        with_intent.proceed, with_intent.blocked_reason,
    )
    assert without.decision.verdict == with_intent.decision.verdict
    assert without.request_local_intent is False
    assert with_intent.request_local_intent is True


def test_guard_outcome_intent_false_for_unbound_and_error_paths(monkeypatch):
    from backend.agents import u6_request_intent as ri

    ctx = _ctx_human()
    with ri.request_intent_scope(ri.declare_request_local_intent(ctx)):
        # unbound principal (ctx=None) can never satisfy intent
        unbound = action_guard.guard_tool_dispatch(
            adapter_namespace="chat", tool_name="create_task", raw_args={},
            execution_context=None,
        )
        # stage-1 raise ⇒ failsafe/error outcome keeps the fail-closed default
        monkeypatch.setattr(
            action_guard, "authorize_action",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kernel boom")),
        )
        errored = action_guard.guard_tool_dispatch(
            adapter_namespace="chat", tool_name="create_task", raw_args={},
            execution_context=ctx,
        )
    assert unbound.request_local_intent is False
    assert errored.request_local_intent is False
    assert errored.error_reason is not None

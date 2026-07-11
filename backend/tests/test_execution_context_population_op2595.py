"""OP-2595 — U6-0 T4b: ExecutionContext population at run_graph entry points.

Populate-only / dormant-consume: nothing reads ``GraphState.execution_context``
for authorization yet (that is T6). These tests assert only that the field
lands on the state with the right shape and principal type at every entry
point (chat / A2A / invoke / orchestration).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend import auth as _auth
from backend import pep_gateway as _pep
from backend.agents.execution_context import ExecutionContext
from backend.agents.state import GraphState
from backend.routers import a2a_inbound


# ─── Helpers ───

def _operator() -> _auth.User:
    return _auth.User(
        id="u-2595",
        email="op@example.com",
        name="Op",
        role="operator",
        tenant_id="tenant-2595",
    )


async def _allow_pep(**kwargs):
    return _pep.PepDecision(
        id="pep-2595",
        ts=0.0,
        agent_id=kwargs.get("agent_id", ""),
        tool=kwargs["tool"],
        command="",
        tier=kwargs["tier"],
        action=_pep.PepAction.auto_allow,
    )


async def _noop_audit(**kwargs):
    return None


# ─── Chat path: for_human, authorization_source="chat" ───

@pytest.mark.asyncio
async def test_chat_run_pipeline_populates_for_human_execution_context(
    monkeypatch,
) -> None:
    """``_run_pipeline`` threads the caller-built for_human context into
    ``run_graph`` so ``GraphState.execution_context.principal_type=='human'``
    and ``actor_id==user.id`` land on the state the graph returns.
    """
    from backend.routers import chat as chat_router
    from backend.agents import execution_context as _ec

    captured: dict = {}

    async def _fake_run_graph(user_msg, **kwargs) -> GraphState:
        captured["execution_context"] = kwargs.get("execution_context")
        return GraphState(
            user_command=user_msg,
            answer="ok",
            routed_to="general",
            execution_context=kwargs.get("execution_context"),
        )

    monkeypatch.setattr(chat_router, "run_graph", _fake_run_graph)

    user = _operator()
    ctx = _ec.for_human(
        user=user,
        tenant_id=user.tenant_id,
        session_id="sess-xyz",
        request_id=uuid.uuid4().hex,
        message_id="msg-abc",
        authorization_source="chat",
    )
    reply = await chat_router._run_pipeline(
        "hello", prior_messages=None, model_name="",
        execution_context=ctx,
    )
    assert reply.content == "ok"
    ec = captured["execution_context"]
    assert isinstance(ec, ExecutionContext)
    assert ec.principal_type == "human"
    assert ec.actor_id == user.id
    assert ec.tenant_id == user.tenant_id
    assert ec.authorization_source == "chat"
    assert ec.message_id == "msg-abc"
    assert ec.session_id == "sess-xyz"


def test_bind_chat_context_forwards_tenant_id(monkeypatch) -> None:
    """``_bind_chat_context`` now passes tenant_id to set_chat_context so
    the tenant contextvar is non-empty on the chat path."""
    from backend.routers import chat as chat_router

    seen: dict = {}

    def _fake_set_chat_context(
        user_id: str, session_id: str, tenant_id: str = "",
    ) -> None:
        seen["user_id"] = user_id
        seen["session_id"] = session_id
        seen["tenant_id"] = tenant_id

    import backend.agents.tools as _tools
    monkeypatch.setattr(_tools, "set_chat_context", _fake_set_chat_context)

    chat_router._bind_chat_context("u-1", "s-1", "tenant-Z")
    assert seen == {"user_id": "u-1", "session_id": "s-1", "tenant_id": "tenant-Z"}


# ─── A2A path: for_human, authorization_source="a2a" ───

@pytest.mark.asyncio
async def test_a2a_authenticated_caller_retains_human_principal(monkeypatch) -> None:
    """An authenticated A2A caller must NEVER be downgraded to a machine
    principal. ``_run_a2a_graph(user=user, …)`` mints a ``for_human``
    context with ``authorization_source=='a2a'`` and passes it through.
    """
    captured: dict = {}

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        captured["execution_context"] = kwargs.get("execution_context")
        return GraphState(
            user_command=command,
            routed_to="hal",
            answer="hal answer",
            execution_context=kwargs.get("execution_context"),
        )

    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)

    graph = await a2a_inbound._run_a2a_graph(
        command="inspect",
        agent_name="hal",
        invocation_id="a2a-abc",
        user=_operator(),
    )
    ec = captured["execution_context"]
    assert isinstance(ec, ExecutionContext)
    assert ec.principal_type == "human"
    assert ec.authorization_source == "a2a"
    assert ec.actor_id == "u-2595"
    assert ec.tenant_id == "tenant-2595"
    # Message_id doubles as the a2a invocation id.
    assert ec.message_id == "a2a-abc"
    # The state that surfaces from the helper also carries the context.
    assert graph.execution_context is ec


@pytest.mark.asyncio
async def test_a2a_default_user_none_keeps_helper_callable_without_context(
    monkeypatch,
) -> None:
    """Legacy callers that haven't been ported to pass ``user`` are still
    safe — the default keeps the helper callable and leaves the context
    unset (the T6 kernel treats missing context conservatively)."""
    captured: dict = {}

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        captured["execution_context"] = kwargs.get("execution_context")
        return GraphState(user_command=command, answer="ok")

    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)

    await a2a_inbound._run_a2a_graph(
        command="ping",
        agent_name="hal",
        invocation_id="a2a-legacy",
    )
    assert captured["execution_context"] is None


@pytest.mark.asyncio
async def test_a2a_endpoint_populates_human_context_on_returned_state(
    monkeypatch,
) -> None:
    """End-to-end (sync JSON): the operator-authenticated A2A endpoint
    hands a ``for_human`` context down through ``_run_a2a_graph`` →
    ``run_graph`` — the run_graph fake absorbs it via ``**kwargs``."""
    app = FastAPI()
    app.include_router(a2a_inbound.router)
    app.dependency_overrides[_auth.require_operator] = _operator
    captured: dict = {}

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        captured["ec"] = kwargs.get("execution_context")
        return GraphState(
            user_command=command, routed_to="hal", answer="ok",
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "inspect"})
    assert res.status_code == 200
    ec = captured["ec"]
    assert isinstance(ec, ExecutionContext)
    assert ec.principal_type == "human"
    assert ec.authorization_source == "a2a"


# ─── Invoke path: for_machine ───

@pytest.mark.asyncio
async def test_invoke_execute_actions_populates_machine_context(monkeypatch) -> None:
    """The streaming ``_execute_actions`` call site mints a for_machine
    context with the pinned ``agent-invoke-execute-actions`` label so the
    kernel sees an explicit non-human principal (no ambient authority).
    """
    from backend.routers import invoke as invoke_router

    captured: list = []

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        captured.append(kwargs.get("execution_context"))
        return GraphState(user_command=command, answer="", routed_to="general")

    monkeypatch.setattr(invoke_router, "run_graph", _fake_run_graph)

    state = {"running_agents": []}
    async_gen = invoke_router._execute_actions(
        [{"type": "command", "command": "run a probe"}], state,
    )
    async for _ in async_gen:
        pass
    assert len(captured) == 1
    ec = captured[0]
    assert isinstance(ec, ExecutionContext)
    assert ec.principal_type == "machine"
    assert ec.actor_id == "agent-invoke-execute-actions"
    assert ec.authorization_source == "internal_scheduler"


# ─── Orchestration path: for_machine ───

@pytest.mark.asyncio
async def test_orchestration_monolith_dispatch_populates_machine_context(
    monkeypatch,
) -> None:
    """The monolith dispatcher builds a ``for_machine`` context with the
    pinned ``orchestration`` label — the identity-less orchestrator seam
    must be an explicit machine principal, never implicit ambient authority.
    """
    from backend import orchestration_mode as _om
    from backend.agents import graph as _graph_mod

    captured: dict = {}

    async def _fake_run_graph(**kwargs) -> GraphState:
        captured["ec"] = kwargs.get("execution_context")
        return GraphState(
            user_command=kwargs.get("user_command", ""),
            answer="ok",
            routed_to="general",
        )

    monkeypatch.setattr(_graph_mod, "run_graph", _fake_run_graph)

    outcome = await _om._monolith_dispatch(
        _om.DispatchRequest(user_command="ping"),
    )
    assert outcome.ok is True
    ec = captured["ec"]
    assert isinstance(ec, ExecutionContext)
    assert ec.principal_type == "machine"
    assert ec.actor_id == "orchestration"
    assert ec.authorization_source == "internal_scheduler"


# ─── GraphState field carries the context through ───

def test_graph_state_execution_context_field_default_is_none() -> None:
    """Populate-only: ``GraphState()`` with no context stays ``None`` so
    legacy code paths that build state directly keep working during the
    rollout window."""
    state = GraphState(user_command="x")
    assert state.execution_context is None


def test_graph_state_stores_execution_context_when_supplied() -> None:
    from backend.agents import execution_context as _ec

    ec = _ec.for_machine(service_name="probe", request_id="r-1")
    state = GraphState(user_command="x", execution_context=ec)
    assert state.execution_context is ec
    assert state.execution_context.principal_type == "machine"

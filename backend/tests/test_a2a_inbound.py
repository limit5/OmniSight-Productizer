"""BP.A2A.2 — inbound AgentCard discovery and invocation router tests."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from backend import auth as _auth
from backend import pep_gateway as _pep
from backend.agents.state import GraphState, ToolResult
from backend.routers import a2a_inbound


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(a2a_inbound.router)
    return app


def _operator() -> _auth.User:
    return _auth.User(
        id="u-a2a",
        email="operator@example.com",
        name="Operator",
        role="operator",
        tenant_id="tenant-a2a",
    )


async def _allow_pep(**kwargs):
    return _pep.PepDecision(
        id="pep-a2a-test",
        ts=0.0,
        agent_id=kwargs.get("agent_id", ""),
        tool=kwargs["tool"],
        command="",
        tier=kwargs["tier"],
        action=_pep.PepAction.auto_allow,
    )


@pytest.mark.asyncio
async def test_agent_card_discovery_uses_well_known_root_and_forwarded_host() -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        res = await client.get(
            "/.well-known/agent.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "omnisight.example.com",
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["protocol"] == "a2a"
    assert body["url"] == "https://omnisight.example.com/.well-known/agent.json"
    assert body["endpoints"]["invoke_url_template"] == (
        "https://omnisight.example.com/a2a/invoke/{agent_name}"
    )
    assert any(cap["agent_name"] == "hal" for cap in body["capabilities"])


@pytest.mark.asyncio
async def test_sync_invoke_runs_graph_after_operator_auth_and_pep(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    pep_calls: list[dict] = []

    async def _fake_pep(**kwargs):
        pep_calls.append(kwargs)
        return await _allow_pep(**kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(
            user_command=command,
            routed_to="hal",
            answer="HAL answer",
            tool_results=[
                ToolResult(tool_name="read_file", output="ok", success=True),
            ],
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _fake_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/hal",
            json={"message": "inspect HAL driver"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["agent_name"] == "hal"
    assert body["status"] == "completed"
    assert body["routed_to"] == "hal"
    assert body["answer"] == "HAL answer"
    assert body["pep_action"] == "auto_allow"
    assert pep_calls[0]["tool"] == a2a_inbound.A2A_INVOKE_PEP_TOOL
    assert pep_calls[0]["guild_id"] == "hal"
    assert pep_calls[0]["arguments"]["tenant_id"] == "tenant-a2a"


@pytest.mark.asyncio
async def test_streaming_invoke_emits_a2a_sse_event_order(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(
            user_command=command,
            routed_to="bsp",
            answer="BSP answer",
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/bsp?stream=true",
            json={"input": "bring up board support"},
        )

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    event_lines = [
        line
        for line in res.text.splitlines()
        if line.startswith("event: ")
    ]
    assert event_lines == [
        "event: task_submitted",
        "event: task_working",
        "event: artifact_delta",
        "event: task_completed",
    ]


@pytest.mark.asyncio
async def test_invoke_rejects_unknown_agent_before_pep(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fail_pep(**kwargs):
        raise AssertionError("PEP should not run for unknown agents")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _fail_pep)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/not-a-real-agent",
            json={"message": "hello"},
        )

    assert res.status_code == 404


@pytest.mark.asyncio
async def test_pep_deny_blocks_invoke(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _deny_pep(**kwargs):
        return _pep.PepDecision(
            id="pep-deny",
            ts=0.0,
            agent_id=kwargs.get("agent_id", ""),
            tool=kwargs["tool"],
            command="",
            tier=kwargs["tier"],
            action=_pep.PepAction.deny,
            rule="a2a_policy",
            reason="blocked",
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _deny_pep)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/architect",
            json={"message": "design this"},
        )

    assert res.status_code == 403
    assert res.json()["detail"]["reason"] == "pep_denied"

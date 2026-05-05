"""BP.A2A.2 — inbound AgentCard discovery and invocation router tests."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from backend import auth as _auth
from backend import pep_gateway as _pep
from backend.api_keys import ApiKey
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


async def _noop_audit(**kwargs):
    return None


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
async def test_provider_agent_card_discovery_exposes_provider_endpoint_templates() -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        res = await client.get(
            "/.well-known/a2a/providers/openai/agent.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "omnisight.example.com",
            },
        )

    assert res.status_code == 200
    body = res.json()
    assert body["protocol"] == "a2a"
    assert body["provider"] == "OpenAI"
    assert body["url"] == (
        "https://omnisight.example.com/.well-known/a2a/providers/openai/agent.json"
    )
    assert body["endpoints"]["invoke_url_template"] == (
        "https://omnisight.example.com/a2a/providers/openai/invoke/{agent_name}"
    )
    hal = next(cap for cap in body["capabilities"] if cap["agent_name"] == "hal")
    assert hal["source"] == "provider_specialist"
    assert hal["provider_id"] == "openai"
    assert hal["model_spec"].startswith("openai:")
    assert "ChatOpenAI" not in str(body)


def test_api_key_scope_allows_a2a_oauth_style_discover_and_invoke() -> None:
    key = ApiKey(
        id="ak-a2a",
        name="a2a",
        key_prefix="omni_a2a",
        scopes=["a2a:discover:*", "a2a:invoke:*"],
    )

    assert key.scope_allows("/.well-known/agent.json") is True
    assert key.scope_allows("/a2a/invoke/hal") is True
    assert key.scope_allows("/a2a/invoke/bsp") is True
    assert key.scope_allows("/api-keys") is False


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
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
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
async def test_provider_invoke_routes_to_graph_with_provider_model_spec(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    graph_calls: list[dict] = []

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        graph_calls.append({"command": command, **kwargs})
        return GraphState(
            user_command=command,
            routed_to="reviewer",
            answer="reviewer answer",
            model_name=kwargs.get("model_name", ""),
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/providers/openrouter/invoke/reviewer",
            json={"message": "review the patch"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["provider_id"] == "openrouter"
    assert body["agent_name"] == "reviewer"
    assert body["model_spec"].startswith("openrouter:")
    assert graph_calls == [
        {
            "command": "review the patch",
            "agent_sub_type": "reviewer",
            "model_name": body["model_spec"],
            "task_id": body["invocation_id"],
        }
    ]


@pytest.mark.asyncio
async def test_provider_invoke_rejects_unknown_provider_before_pep(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fail_pep(**kwargs):
        raise AssertionError("PEP should not run for unknown providers")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _fail_pep)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/providers/not-real/invoke/hal",
            json={"message": "inspect HAL driver"},
        )

    assert res.status_code == 404


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
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
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


@pytest.mark.asyncio
async def test_invoke_rate_limit_is_per_tenant_agent_card(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    keys: list[str] = []

    class _DenyLimiter:
        def allow(self, key: str, capacity: int, window_seconds: float):
            keys.append(key)
            return False, 2.0

    async def _fail_run_graph(*args, **kwargs) -> GraphState:
        raise AssertionError("rate limit should block before graph runs")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fail_run_graph)

    from backend import rate_limit as _rate_limit
    monkeypatch.setattr(_rate_limit, "get_limiter", lambda: _DenyLimiter())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/hal",
            json={"message": "inspect HAL driver"},
        )

    assert res.status_code == 429
    assert res.headers["retry-after"] == "3"
    assert keys == ["a2a:agent:tenant-a2a:hal"]


@pytest.mark.asyncio
async def test_sync_invoke_audit_writes_hashes_not_plaintext(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    audit_calls: list[dict] = []

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(
            user_command=command,
            routed_to="hal",
            answer="sensitive answer should not enter audit",
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/hal",
            json={"message": "secret command should not enter audit"},
        )

    assert res.status_code == 200
    audit = audit_calls[0]
    assert audit["action"] == "a2a_agent_invoked"
    assert audit["tenant_id"] == "tenant-a2a"
    assert audit["before"]["agent_name"] == "hal"
    assert len(audit["before"]["request_hash"]) == 64
    assert len(audit["after"]["response_hash"]) == 64
    assert audit["after"]["status"] == "completed"
    assert "secret command" not in str(audit)
    assert "sensitive answer" not in str(audit)

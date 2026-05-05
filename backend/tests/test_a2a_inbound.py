"""BP.A2A.2 — inbound AgentCard discovery and invocation router tests."""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
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


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    """Clear sse_starlette's loop-bound exit event between SSE tests."""
    try:
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None
        AppStatus.should_exit = False
    except Exception:  # pragma: no cover - sse_starlette is a test dependency.
        pass
    yield


def _operator() -> _auth.User:
    return _auth.User(
        id="u-a2a",
        email="operator@example.com",
        name="Operator",
        role="operator",
        tenant_id="tenant-a2a",
    )


def _operator_for(tenant_id: str, email: str | None = None) -> _auth.User:
    return _auth.User(
        id=f"u-{tenant_id}",
        email=email or f"{tenant_id}@example.com",
        name="Operator",
        role="operator",
        tenant_id=tenant_id,
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


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event: "):
            event_name = line.removeprefix("event: ")
            data_lines = []
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
        elif line == "" and event_name is not None:
            events.append((event_name, json.loads("\n".join(data_lines))))
            event_name = None
            data_lines = []
    if event_name is not None:
        events.append((event_name, json.loads("\n".join(data_lines))))
    return events


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


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"command": "run command"}, "run command"),
        ({"message": "run message"}, "run message"),
        ({"task": "run task"}, "run task"),
        ({"input": "run input"}, "run input"),
        ({"input": {"command": "nested command"}}, "nested command"),
        ({"input": {"message": "nested message"}}, "nested message"),
        ({"input": {"task": "nested task"}}, "nested task"),
        ({"input": {"prompt": "nested prompt"}}, "nested prompt"),
        ({"input": {"text": "nested text"}}, "nested text"),
    ],
)
def test_coerce_command_accepts_a2a_payload_shapes(payload, expected) -> None:
    body = a2a_inbound.A2AInvokeRequest.model_validate(payload)

    assert a2a_inbound._coerce_command(body) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"input": ""},
        {"message": ""},
        {"input": {"prompt": ""}},
        {"input": {"unknown": "value"}},
    ],
)
def test_coerce_command_rejects_empty_a2a_payload_shapes(payload) -> None:
    body = a2a_inbound.A2AInvokeRequest.model_validate(payload)

    with pytest.raises(HTTPException) as exc:
        a2a_inbound._coerce_command(body)

    assert exc.value.status_code == 422


def test_hash_jsonable_is_stable_for_audit_replay_metadata() -> None:
    left = {"b": 2, "a": {"z": True, "n": None}}
    right = {"a": {"n": None, "z": True}, "b": 2}

    assert a2a_inbound._hash_jsonable(left) == a2a_inbound._hash_jsonable(right)
    assert len(a2a_inbound._hash_jsonable(left)) == 64


@pytest.mark.parametrize(
    ("headers", "expected_base"),
    [
        (
            {
                "x-forwarded-proto": "https",
                "x-forwarded-host": "edge.example.com",
            },
            "https://edge.example.com",
        ),
        (
            {
                "x-forwarded-proto": "https, http",
                "x-forwarded-host": "first.example.com, second.example.com",
            },
            "https://first.example.com",
        ),
        ({}, "http://internal"),
    ],
)
@pytest.mark.asyncio
async def test_discovery_base_url_uses_forwarded_origin(headers, expected_base) -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://internal") as client:
        res = await client.get("/.well-known/agent.json", headers=headers)

    assert res.status_code == 200
    body = res.json()
    assert body["url"] == expected_base + "/.well-known/agent.json"
    assert body["endpoints"]["discovery_url"] == expected_base + "/.well-known/agent.json"


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "description",
        "schema_version",
        "protocol",
        "provider",
        "url",
        "capabilities",
        "endpoints",
        "auth",
        "streaming",
        "protocol_capabilities",
    ],
)
@pytest.mark.asyncio
async def test_agent_card_discovery_shape_contains_required_top_level_fields(field) -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/.well-known/agent.json")

    assert res.status_code == 200
    assert field in res.json()


@pytest.mark.parametrize("agent_name", ["architect", "bsp", "hal", "orchestrator", "hd"])
@pytest.mark.asyncio
async def test_agent_card_capabilities_expose_sync_and_stream_urls(agent_name) -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://a2a.test") as client:
        res = await client.get("/.well-known/agent.json")

    cap = next(item for item in res.json()["capabilities"] if item["agent_name"] == agent_name)
    assert cap["endpoint_url"] == f"https://a2a.test/a2a/invoke/{agent_name}"
    assert cap["stream_endpoint_url"] == (
        f"https://a2a.test/a2a/invoke/{agent_name}?stream=true"
    )


@pytest.mark.parametrize("provider_id", ["anthropic", "openai", "google", "xai", "groq"])
@pytest.mark.asyncio
async def test_provider_agent_card_shape_is_provider_scoped(provider_id) -> None:
    app = _app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="https://a2a.test") as client:
        res = await client.get(f"/.well-known/a2a/providers/{provider_id}/agent.json")

    assert res.status_code == 200
    body = res.json()
    assert body["protocol"] == "a2a"
    assert body["url"] == (
        f"https://a2a.test/.well-known/a2a/providers/{provider_id}/agent.json"
    )
    assert body["endpoints"]["stream_url_template"] == (
        f"https://a2a.test/a2a/providers/{provider_id}/invoke/{{agent_name}}?stream=true"
    )


@pytest.mark.asyncio
async def test_discovery_auth_failure_short_circuits_audit(monkeypatch) -> None:
    app = _app()

    async def _reject_viewer():
        raise HTTPException(status_code=401, detail="missing bearer")

    async def _fail_audit(**kwargs):
        raise AssertionError("audit should not run when auth fails")

    app.dependency_overrides[_auth.require_viewer] = _reject_viewer
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fail_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/.well-known/agent.json")

    assert res.status_code == 401


@pytest.mark.asyncio
async def test_invoke_auth_failure_short_circuits_pep(monkeypatch) -> None:
    app = _app()

    async def _reject_operator():
        raise HTTPException(status_code=401, detail="missing bearer")

    async def _fail_pep(**kwargs):
        raise AssertionError("PEP should not run when auth fails")

    app.dependency_overrides[_auth.require_operator] = _reject_operator
    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _fail_pep)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "hello"})

    assert res.status_code == 401


@pytest.mark.asyncio
async def test_viewer_cannot_invoke_operator_only_a2a(monkeypatch) -> None:
    app = _app()

    async def _reject_operator():
        raise HTTPException(status_code=403, detail="Requires role=operator")

    async def _fail_graph(*args, **kwargs) -> GraphState:
        raise AssertionError("graph should not run for role failure")

    app.dependency_overrides[_auth.require_operator] = _reject_operator
    monkeypatch.setattr(a2a_inbound, "run_graph", _fail_graph)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "hello"})

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_discovery_rate_limit_uses_agent_card_bucket(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_viewer] = _operator
    keys: list[str] = []

    class _DenyLimiter:
        def allow(self, key: str, capacity: int, window_seconds: float):
            keys.append(key)
            return False, 1.0

    from backend import rate_limit as _rate_limit
    monkeypatch.setattr(_rate_limit, "get_limiter", lambda: _DenyLimiter())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/.well-known/agent.json")

    assert res.status_code == 429
    assert res.json()["detail"]["agent_name"] == "agent-card"
    assert keys == ["a2a:agent:tenant-a2a:agent-card"]


@pytest.mark.asyncio
async def test_provider_discovery_rate_limit_uses_provider_card_bucket(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_viewer] = _operator
    keys: list[str] = []

    class _DenyLimiter:
        def allow(self, key: str, capacity: int, window_seconds: float):
            keys.append(key)
            return False, 4.0

    from backend import rate_limit as _rate_limit
    monkeypatch.setattr(_rate_limit, "get_limiter", lambda: _DenyLimiter())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/.well-known/a2a/providers/openai/agent.json")

    assert res.status_code == 429
    assert res.headers["retry-after"] == "5"
    assert keys == ["a2a:agent:tenant-a2a:provider:openai:agent-card"]


@pytest.mark.asyncio
async def test_provider_invoke_rate_limit_uses_provider_agent_bucket(monkeypatch) -> None:
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
            "/a2a/providers/openai/invoke/hal",
            json={"message": "inspect HAL driver"},
        )

    assert res.status_code == 429
    assert keys == ["a2a:agent:tenant-a2a:openai:hal"]


@pytest.mark.asyncio
async def test_invoke_rate_limit_isolated_between_tenants(monkeypatch) -> None:
    current_user = [_operator_for("tenant-a")]
    app = _app()
    app.dependency_overrides[_auth.require_operator] = lambda: current_user[0]
    counts: dict[str, int] = {}

    class _OnePerKeyLimiter:
        def allow(self, key: str, capacity: int, window_seconds: float):
            counts[key] = counts.get(key, 0) + 1
            return counts[key] <= 1, 9.0

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="hal", answer="ok")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    from backend import rate_limit as _rate_limit
    monkeypatch.setattr(_rate_limit, "get_limiter", lambda: _OnePerKeyLimiter())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/a2a/invoke/hal", json={"message": "one"})
        second = await client.post("/a2a/invoke/hal", json={"message": "two"})
        current_user[0] = _operator_for("tenant-b")
        third = await client.post("/a2a/invoke/hal", json={"message": "three"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert third.status_code == 200
    assert counts["a2a:agent:tenant-a:hal"] == 2
    assert counts["a2a:agent:tenant-b:hal"] == 1


@pytest.mark.asyncio
async def test_invoke_rate_limit_isolated_between_agent_cards(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    counts: dict[str, int] = {}

    class _OnePerKeyLimiter:
        def allow(self, key: str, capacity: int, window_seconds: float):
            counts[key] = counts.get(key, 0) + 1
            return counts[key] <= 1, 7.0

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to=kwargs["agent_sub_type"], answer="ok")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    from backend import rate_limit as _rate_limit
    monkeypatch.setattr(_rate_limit, "get_limiter", lambda: _OnePerKeyLimiter())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        hal = await client.post("/a2a/invoke/hal", json={"message": "one"})
        bsp = await client.post("/a2a/invoke/bsp", json={"message": "two"})
        hal_again = await client.post("/a2a/invoke/hal", json={"message": "three"})

    assert hal.status_code == 200
    assert bsp.status_code == 200
    assert hal_again.status_code == 429
    assert counts["a2a:agent:tenant-a2a:hal"] == 2
    assert counts["a2a:agent:tenant-a2a:bsp"] == 1


@pytest.mark.asyncio
async def test_streaming_invoke_emits_json_chunks_in_order(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="bsp", answer="BSP answer")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/invoke/bsp",
            json={"input": "bring up board support", "stream": True},
        )

    events = _sse_events(res.text)
    assert [name for name, _data in events] == [
        "task_submitted",
        "task_working",
        "artifact_delta",
        "task_completed",
    ]
    assert events[0][1]["agent_name"] == "bsp"
    assert events[2][1]["answer"] == "BSP answer"
    assert events[3][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_streaming_invoke_failure_ends_with_task_failed(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="hal", last_error="boom")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal?stream=true", json={"message": "fail"})

    events = _sse_events(res.text)
    assert events[-1][0] == "task_failed"
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["last_error"] == "boom"


@pytest.mark.asyncio
async def test_provider_streaming_chunks_include_model_spec(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(
            user_command=command,
            routed_to="reviewer",
            answer="review ok",
            model_name=kwargs["model_name"],
        )

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _noop_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/providers/openrouter/invoke/reviewer?stream=true",
            json={"message": "review"},
        )

    events = _sse_events(res.text)
    assert events[0][1]["provider_id"] == "openrouter"
    assert events[1][1]["model_spec"].startswith("openrouter:")
    assert events[-1][1]["provider_id"] == "openrouter"
    assert events[-1][1]["model_spec"].startswith("openrouter:")


@pytest.mark.asyncio
async def test_sync_invoke_audit_record_can_replay_invocation_metadata(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    audit_calls: list[dict] = []

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="hal", answer="done")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "inspect"})

    body = res.json()
    replay = audit_calls[0]
    assert replay["entity_id"] == body["invocation_id"]
    assert replay["before"]["tenant_id"] == "tenant-a2a"
    assert replay["before"]["agent_name"] == "hal"
    assert replay["before"]["stream"] is False
    assert replay["after"]["tenant_id"] == "tenant-a2a"
    assert replay["after"]["status"] == "completed"
    assert replay["after"]["pep_decision_id"] == body["pep_decision_id"]


@pytest.mark.asyncio
async def test_streaming_audit_record_can_replay_chunked_invocation_metadata(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    audit_calls: list[dict] = []

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="bsp", answer="done")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/bsp?stream=true", json={"message": "bring up"})

    events = _sse_events(res.text)
    replay = audit_calls[0]
    assert replay["entity_id"] == events[0][1]["invocation_id"]
    assert replay["before"]["stream"] is True
    assert replay["after"]["status"] == events[-1][1]["status"] == "completed"
    assert len(replay["before"]["request_hash"]) == 64
    assert len(replay["after"]["response_hash"]) == 64


@pytest.mark.asyncio
async def test_provider_sync_audit_record_keeps_provider_replay_metadata(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    audit_calls: list[dict] = []

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="reviewer", answer="done")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/a2a/providers/openai/invoke/reviewer",
            json={"message": "review"},
        )

    replay = audit_calls[0]
    assert res.status_code == 200
    assert replay["action"] == "a2a_provider_agent_invoked"
    assert replay["before"]["provider_id"] == "openai"
    assert replay["before"]["model_spec"].startswith("openai:")
    assert replay["after"]["provider_id"] == "openai"


@pytest.mark.asyncio
async def test_graph_exception_returns_failed_payload_for_replay(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = _operator
    audit_calls: list[dict] = []

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _raise_graph(command: str, **kwargs) -> GraphState:
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _allow_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _raise_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "fail"})

    body = res.json()
    assert res.status_code == 200
    assert body["status"] == "failed"
    assert body["last_error"] == "RuntimeError: graph exploded"
    assert audit_calls[0]["after"]["status"] == "failed"


@pytest.mark.asyncio
async def test_tenant_identity_flows_to_pep_and_audit(monkeypatch) -> None:
    app = _app()
    app.dependency_overrides[_auth.require_operator] = lambda: _operator_for(
        "tenant-special",
        "special@example.com",
    )
    pep_calls: list[dict] = []
    audit_calls: list[dict] = []

    async def _fake_pep(**kwargs):
        pep_calls.append(kwargs)
        return await _allow_pep(**kwargs)

    async def _fake_audit(**kwargs):
        audit_calls.append(kwargs)

    async def _fake_run_graph(command: str, **kwargs) -> GraphState:
        return GraphState(user_command=command, routed_to="hal", answer="ok")

    monkeypatch.setattr(a2a_inbound._pep, "evaluate", _fake_pep)
    monkeypatch.setattr(a2a_inbound, "run_graph", _fake_run_graph)
    monkeypatch.setattr(a2a_inbound, "_audit_a2a_event", _fake_audit)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/a2a/invoke/hal", json={"message": "hello"})

    assert res.status_code == 200
    assert pep_calls[0]["arguments"]["tenant_id"] == "tenant-special"
    assert pep_calls[0]["arguments"]["caller"] == "special@example.com"
    assert audit_calls[0]["tenant_id"] == "tenant-special"
    assert audit_calls[0]["actor"] == "special@example.com"

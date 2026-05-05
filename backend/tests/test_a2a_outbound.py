"""BP.A2A.5 -- outbound A2A client tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import HTTPException
import httpx
import pytest

from backend import auth
from backend import circuit_breaker
from backend.a2a import client as a2a_client_module
from backend.a2a.agent_card import build_agent_card
from backend.a2a.client import (
    A2AClient,
    A2ACircuitOpen,
    A2ARequestFailed,
    A2AStreamEvent,
    A2ATimeout,
)
from backend.agents.external_agent_registry import (
    ExternalAgentDisabledError,
    ExternalAgentEndpoint,
    ExternalAgentNotFoundError,
    ExternalAgentRegistry,
)
from backend.agents.nodes import external_agent_node_factory
from backend.agents.state import GraphState, ToolResult


BASE_URL = "https://remote-agent.example.com"
TENANT_ID = "tenant-a2a-outbound"


@pytest.fixture(autouse=True)
def _reset_breaker_state():
    circuit_breaker._reset_for_tests()
    yield
    circuit_breaker._reset_for_tests()


def _client_factory(handler):
    def _factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return _factory


async def _no_sleep(_delay: float) -> None:
    return None


def _agent_card_response() -> httpx.Response:
    return httpx.Response(200, json=build_agent_card(BASE_URL).model_dump(mode="json"))


def _operator(tenant_id: str = TENANT_ID) -> auth.User:
    return auth.User(
        id=f"u-{tenant_id}",
        email=f"{tenant_id}@example.com",
        name="Operator",
        role="operator",
        tenant_id=tenant_id,
    )


def _viewer(tenant_id: str = TENANT_ID) -> auth.User:
    return auth.User(
        id=f"viewer-{tenant_id}",
        email=f"viewer-{tenant_id}@example.com",
        name="Viewer",
        role="viewer",
        tenant_id=tenant_id,
    )


def _request(registry: ExternalAgentRegistry):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(external_agent_registry=registry)
        )
    )


def _endpoint(**overrides) -> ExternalAgentEndpoint:
    values = {
        "agent_id": "threat-intel",
        "display_name": "Threat Intel",
        "base_url": BASE_URL + "/",
        "agent_name": "intel",
        "description": "partner A2A endpoint",
        "tags": ("secops", "intel", "secops"),
        "capabilities": ("ioc_enrichment", "cve_triage", "ioc_enrichment"),
    }
    values.update(overrides)
    return ExternalAgentEndpoint(**values)


def test_client_normalises_base_url_and_minimum_attempts() -> None:
    client = A2AClient(
        BASE_URL + "///",
        tenant_id=TENANT_ID,
        max_attempts=0,
    )

    assert client.base_url == BASE_URL
    assert client.max_attempts == 1


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("", "base_url is required"),
        ("/relative", "base_url must start"),
        ("ftp://remote-agent.example.com", "base_url must start"),
    ],
)
def test_client_rejects_invalid_base_url(base_url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        A2AClient(base_url, tenant_id=TENANT_ID)


def test_client_requires_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        A2AClient(BASE_URL, tenant_id="")


@pytest.mark.asyncio
async def test_fetch_agent_card_caches_for_one_hour_by_default() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return _agent_card_response()

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    first = await client.fetch_agent_card()
    second = await client.fetch_agent_card()

    assert first == second
    assert calls == [BASE_URL + "/.well-known/agent.json"]


@pytest.mark.asyncio
async def test_fetch_agent_card_force_refresh_bypasses_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _agent_card_response()

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    await client.fetch_agent_card()
    await client.fetch_agent_card(force_refresh=True)

    assert calls == 2


@pytest.mark.asyncio
async def test_fetch_agent_card_cache_expires_after_ttl(monkeypatch) -> None:
    calls = 0
    now = 1_000.0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _agent_card_response()

    monkeypatch.setattr(a2a_client_module.time, "time", lambda: now)
    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        cache_ttl_s=10.0,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    await client.fetch_agent_card()
    now = 1_009.0
    await client.fetch_agent_card()
    now = 1_011.0
    await client.fetch_agent_card()

    assert calls == 2


@pytest.mark.asyncio
async def test_fetch_agent_card_rejects_invalid_card_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"protocol": "a2a"})

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(A2ARequestFailed, match="AgentCard"):
        await client.fetch_agent_card()


@pytest.mark.asyncio
async def test_invoke_posts_json_with_tenant_and_bearer_headers() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["tenant"] = request.headers["x-omnisight-tenant-id"]
        captured["auth"] = request.headers["authorization"]
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"status": "completed", "answer": "ok"})

    client = A2AClient(
        BASE_URL + "/",
        tenant_id=TENANT_ID,
        bearer_token="tok-a2a",
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    result = await client.invoke("hal", {"message": "inspect driver"})

    assert result.payload == {"status": "completed", "answer": "ok"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/a2a/invoke/hal"
    assert captured["tenant"] == TENANT_ID
    assert captured["auth"] == "Bearer tok-a2a"
    assert json.loads(captured["body"]) == {"message": "inspect driver"}


@pytest.mark.asyncio
async def test_invoke_url_escapes_agent_name_path_segments() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"status": "completed"})

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    await client.invoke("partner/agent", {"message": "route"})

    assert captured == ["/a2a/invoke/partner%2Fagent"]


@pytest.mark.asyncio
async def test_invoke_stream_flag_adds_query_param() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.query.decode())
        return httpx.Response(200, json={"status": "completed"})

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    await client.invoke("hal", {"message": "stream please"}, stream=True)

    assert captured == ["stream=true"]


@pytest.mark.asyncio
async def test_invoke_empty_json_response_returns_empty_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    result = await client.invoke("hal", {"message": "noop"})

    assert result.payload == {}


@pytest.mark.asyncio
async def test_invoke_rejects_invalid_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{not-json")

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(A2ARequestFailed, match="remote A2A request failed") as excinfo:
        await client.invoke("hal", {"message": "bad-json"})
    assert isinstance(excinfo.value.__cause__, A2ARequestFailed)
    assert "invalid JSON" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
async def test_invoke_rejects_non_object_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "object"])

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(A2ARequestFailed, match="remote A2A request failed") as excinfo:
        await client.invoke("hal", {"message": "bad-shape"})
    assert isinstance(excinfo.value.__cause__, A2ARequestFailed)
    assert "must be an object" in str(excinfo.value.__cause__)


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_429_without_opening_circuit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"detail": "rate limited"})
        return httpx.Response(200, json={"status": "completed"})

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        max_attempts=2,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    result = await client.invoke("hal", {"message": "retry"})

    assert calls == 2
    assert result.payload == {"status": "completed"}
    assert circuit_breaker.is_open(TENANT_ID, "a2a", client.circuit_fingerprint) is False


@pytest.mark.asyncio
async def test_stream_invoke_parses_sse_event_order_and_json_data() -> None:
    sse = "\n".join([
        "event: task_submitted",
        'data: {"id": "task-1"}',
        "",
        "event: artifact_delta",
        'data: {"delta": "chunk"}',
        "",
        "event: task_completed",
        'data: {"status": "completed"}',
        "",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/a2a/invoke/bsp"
        assert request.url.params["stream"] == "true"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    events = [event async for event in client.stream_invoke("bsp", {"input": "bring up"})]

    assert events == [
        A2AStreamEvent(event="task_submitted", data={"id": "task-1"}),
        A2AStreamEvent(event="artifact_delta", data={"delta": "chunk"}),
        A2AStreamEvent(event="task_completed", data={"status": "completed"}),
    ]


@pytest.mark.asyncio
async def test_stream_invoke_parses_id_retry_comment_and_raw_data() -> None:
    sse = "\n".join([
        ": keepalive",
        "id: evt-1",
        "retry: 1500",
        "event: artifact_delta",
        "data: raw chunk",
        "",
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse,
        )

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    events = [event async for event in client.stream_invoke("bsp", {"input": "x"})]

    assert events == [
        A2AStreamEvent(
            event="artifact_delta",
            data="raw chunk",
            id="evt-1",
            retry=1500,
        )
    ]


@pytest.mark.asyncio
async def test_stream_invoke_flushes_trailing_event_without_blank_line() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='event: done\ndata: {"ok": true}')

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    events = [event async for event in client.stream_invoke("bsp", {"input": "x"})]

    assert events == [A2AStreamEvent(event="done", data={"ok": True})]


@pytest.mark.asyncio
async def test_stream_retry_recovers_after_transient_500() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="down")
        return httpx.Response(200, text='event: done\ndata: {"ok": true}\n\n')

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        max_attempts=2,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    events = [event async for event in client.stream_invoke("bsp", {"input": "x"})]

    assert calls == 2
    assert events == [A2AStreamEvent(event="done", data={"ok": True})]
    assert circuit_breaker.is_open(TENANT_ID, "a2a", client.circuit_fingerprint) is False


@pytest.mark.asyncio
async def test_retry_opens_per_tenant_circuit_after_repeated_remote_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "down"})

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        max_attempts=2,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(A2ARequestFailed):
        await client.invoke("intel", {"message": "triage"})

    assert calls == 2
    assert circuit_breaker.is_open(
        TENANT_ID,
        "a2a",
        client.circuit_fingerprint,
    )

    other_tenant = A2AClient(
        BASE_URL,
        tenant_id="tenant-other",
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )
    assert circuit_breaker.is_open(
        other_tenant.tenant_id,
        "a2a",
        other_tenant.circuit_fingerprint,
    ) is False


@pytest.mark.asyncio
async def test_open_circuit_short_circuits_before_http_request() -> None:
    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(lambda request: httpx.Response(200, json={})),
        sleep=_no_sleep,
    )
    circuit_breaker.record_failure(TENANT_ID, "a2a", client.circuit_fingerprint)

    with pytest.raises(A2ACircuitOpen):
        await client.invoke("hal", {"message": "blocked"})


@pytest.mark.asyncio
async def test_success_closes_existing_a2a_circuit() -> None:
    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(
            lambda request: httpx.Response(200, json={"status": "completed"})
        ),
        sleep=_no_sleep,
    )
    circuit_breaker.record_failure(TENANT_ID, "a2a", client.circuit_fingerprint)
    circuit_breaker.record_success(TENANT_ID, "a2a", client.circuit_fingerprint)

    result = await client.invoke("hal", {"message": "recover"})

    assert result.payload == {"status": "completed"}
    assert circuit_breaker.is_open(TENANT_ID, "a2a", client.circuit_fingerprint) is False


def test_circuit_fingerprint_is_stable_per_base_url() -> None:
    first = A2AClient(BASE_URL, tenant_id=TENANT_ID)
    second = A2AClient(BASE_URL + "/", tenant_id=TENANT_ID)
    other = A2AClient("https://other-agent.example.com", tenant_id=TENANT_ID)

    assert first.circuit_fingerprint == second.circuit_fingerprint
    assert first.circuit_fingerprint != other.circuit_fingerprint


@pytest.mark.asyncio
async def test_timeout_raises_timeout_and_opens_circuit_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        max_attempts=2,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(A2ATimeout):
        await client.invoke("hal", {"message": "slow"})

    assert circuit_breaker.is_open(TENANT_ID, "a2a", client.circuit_fingerprint)


@pytest.mark.asyncio
async def test_cancellation_propagates_without_opening_circuit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    client = A2AClient(
        BASE_URL,
        tenant_id=TENANT_ID,
        client_factory=_client_factory(handler),
        sleep=_no_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await client.invoke("hal", {"message": "cancel"})

    assert circuit_breaker.is_open(TENANT_ID, "a2a", client.circuit_fingerprint) is False


class _FakeExternalAgentClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def invoke(self, agent_name: str, payload: dict):
        self.calls.append((agent_name, payload))
        return type("_Result", (), {"payload": self.payload})()


class _FailingExternalAgentClient:
    async def invoke(self, agent_name: str, payload: dict):
        raise A2ARequestFailed("remote failed")


class _FakeExternalAgentRegistry:
    def __init__(self, client: _FakeExternalAgentClient, *, bearer_token: str = "tok-a2a"):
        self.endpoint = ExternalAgentEndpoint(
            agent_id="threat-intel",
            display_name="Threat Intel",
            base_url=BASE_URL,
            agent_name="intel",
        )
        self.client = client
        self.bearer_token = bearer_token

    async def get_endpoint(self, agent_id: str, *, require_enabled: bool = False):
        assert agent_id == "threat-intel"
        assert require_enabled is True
        return self.endpoint

    async def build_client(
        self,
        agent_id: str,
        *,
        tenant_id: str,
        bearer_token: str = "",
    ):
        assert agent_id == "threat-intel"
        assert tenant_id == TENANT_ID
        assert bearer_token == self.bearer_token
        return self.client


def test_external_agent_node_factory_requires_agent_id() -> None:
    with pytest.raises(ValueError, match="agent_id is required"):
        external_agent_node_factory(
            "",
            registry=_FakeExternalAgentRegistry(_FakeExternalAgentClient({})),
            tenant_id=TENANT_ID,
        )


def test_external_agent_node_factory_requires_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        external_agent_node_factory(
            "threat-intel",
            registry=_FakeExternalAgentRegistry(_FakeExternalAgentClient({})),
            tenant_id="",
        )


@pytest.mark.asyncio
async def test_external_agent_node_invokes_a2a_and_writes_tool_results(monkeypatch) -> None:
    events: list[tuple[str, str, str, bool | None]] = []
    client = _FakeExternalAgentClient({"status": "completed", "answer": "ioc enriched"})
    registry = _FakeExternalAgentRegistry(client)

    def _capture_tool_progress(tool_name, phase, output="", **extra):
        events.append((tool_name, phase, output, extra.get("success")))

    monkeypatch.setattr("backend.agents.nodes.emit_tool_progress", _capture_tool_progress)

    node = external_agent_node_factory(
        "threat-intel",
        registry=registry,
        tenant_id=TENANT_ID,
        bearer_token="tok-a2a",
    )
    update = await node(
        GraphState(
            user_command="enrich 1.2.3.4",
            task_id="task-a2a",
            routed_to="validator",
            tool_results=[
                ToolResult(tool_name="ioc_extract", output="1.2.3.4"),
            ],
        )
    )

    assert client.calls == [
        (
            "intel",
            {
                "command": "enrich 1.2.3.4",
                "task_id": "task-a2a",
                "routed_to": "validator",
                "secondary_routes": [],
                "workspace_path": None,
                "tool_results": [
                    {
                        "tool_name": "ioc_extract",
                        "output": "1.2.3.4",
                        "success": True,
                    }
                ],
            },
        )
    ]
    assert update["tool_results"] == [
        ToolResult(
            tool_name="external_agent:threat-intel",
            output='{"answer": "ioc enriched", "status": "completed"}',
            success=True,
        )
    ]
    assert update["messages"][0].content == '{"answer": "ioc enriched", "status": "completed"}'
    assert events[0][:2] == ("external_agent:threat-intel", "start")
    assert events[-1] == (
        "external_agent:threat-intel",
        "done",
        '{"answer": "ioc enriched", "status": "completed"}',
        True,
    )


@pytest.mark.asyncio
async def test_external_agent_node_marks_failed_a2a_status_as_tool_error(monkeypatch) -> None:
    events: list[tuple[str, str, str, bool | None]] = []
    client = _FakeExternalAgentClient({"status": "failed", "last_error": "denied"})
    registry = _FakeExternalAgentRegistry(client, bearer_token="")

    monkeypatch.setattr(
        "backend.agents.nodes.emit_tool_progress",
        lambda tool_name, phase, output="", **extra: events.append(
            (tool_name, phase, output, extra.get("success"))
        ),
    )

    node = external_agent_node_factory(
        "threat-intel",
        registry=registry,
        tenant_id=TENANT_ID,
    )
    update = await node(GraphState(user_command="triage cve"))

    assert update["tool_results"][0].tool_name == "external_agent:threat-intel"
    assert update["tool_results"][0].success is False
    assert update["tool_results"][0].output == (
        '{"last_error": "denied", "status": "failed"}'
    )
    assert events[-1] == (
        "external_agent:threat-intel",
        "error",
        '{"last_error": "denied", "status": "failed"}',
        False,
    )


@pytest.mark.asyncio
async def test_external_agent_node_uses_custom_payload_builder(monkeypatch) -> None:
    client = _FakeExternalAgentClient({"status": "completed", "answer": "ok"})
    registry = _FakeExternalAgentRegistry(client)
    monkeypatch.setattr(
        "backend.agents.nodes.emit_tool_progress",
        lambda *args, **kwargs: None,
    )

    node = external_agent_node_factory(
        "threat-intel",
        registry=registry,
        tenant_id=TENANT_ID,
        bearer_token="tok-a2a",
        payload_builder=lambda state: {"custom": state.user_command},
    )
    update = await node(GraphState(user_command="custom payload"))

    assert client.calls == [("intel", {"custom": "custom payload"})]
    assert update["tool_results"][0].success is True


@pytest.mark.asyncio
async def test_external_agent_node_returns_tool_error_when_registry_lookup_fails(
    monkeypatch,
) -> None:
    class _MissingRegistry:
        async def get_endpoint(self, agent_id: str, *, require_enabled: bool = False):
            raise ExternalAgentNotFoundError("missing")

    monkeypatch.setattr(
        "backend.agents.nodes.emit_tool_progress",
        lambda *args, **kwargs: None,
    )
    node = external_agent_node_factory(
        "missing-agent",
        registry=_MissingRegistry(),
        tenant_id=TENANT_ID,
    )

    update = await node(GraphState(user_command="triage"))

    assert update["tool_results"][0].tool_name == "external_agent:missing-agent"
    assert update["tool_results"][0].success is False
    assert "missing" in update["tool_results"][0].output


@pytest.mark.asyncio
async def test_external_agent_node_returns_tool_error_when_client_invoke_fails(
    monkeypatch,
) -> None:
    registry = _FakeExternalAgentRegistry(_FailingExternalAgentClient())  # type: ignore[arg-type]
    monkeypatch.setattr(
        "backend.agents.nodes.emit_tool_progress",
        lambda *args, **kwargs: None,
    )
    node = external_agent_node_factory(
        "threat-intel",
        registry=registry,
        tenant_id=TENANT_ID,
        bearer_token="tok-a2a",
    )

    update = await node(GraphState(user_command="triage"))

    assert update["tool_results"][0].success is False
    assert "remote failed" in update["tool_results"][0].output


def test_external_agent_endpoint_normalises_tags_capabilities_and_card_url() -> None:
    endpoint = _endpoint()

    assert endpoint.base_url == BASE_URL
    assert endpoint.agent_card_url == BASE_URL + "/.well-known/agent.json"
    assert endpoint.tags == ("intel", "secops")
    assert endpoint.capabilities == ("cve_triage", "ioc_enrichment")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"agent_id": "ThreatIntel"}, "agent_id must be a lowercase slug"),
        ({"agent_name": "Intel"}, "agent_name must be a lowercase slug"),
        ({"base_url": "/relative"}, "absolute http\\(s\\) URL"),
        ({"display_name": ""}, "display_name is required"),
    ],
)
def test_external_agent_endpoint_rejects_invalid_shape(overrides, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _endpoint(**overrides)


@pytest.mark.parametrize("auth_mode", ["bearer", "oauth2"])
def test_external_agent_endpoint_requires_token_ref_for_token_auth(auth_mode: str) -> None:
    with pytest.raises(ValueError, match="token_ref is required"):
        _endpoint(auth_mode=auth_mode, token_ref="")


def test_external_agent_endpoint_rejects_token_ref_for_no_auth() -> None:
    with pytest.raises(ValueError, match="token_ref must be empty"):
        _endpoint(auth_mode="none", token_ref="secret:a2a")


@pytest.mark.asyncio
async def test_external_agent_registry_register_upserts_and_preserves_registered_at() -> None:
    registry = ExternalAgentRegistry()

    first = await registry.register_endpoint(_endpoint(display_name="Threat Intel"))
    second = await registry.register_endpoint(_endpoint(display_name="Threat Intel v2"))

    assert second.registered_at == first.registered_at
    assert second.updated_at is not None
    assert second.display_name == "Threat Intel v2"


@pytest.mark.asyncio
async def test_external_agent_registry_list_sorts_and_filters_enabled() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(agent_id="z-agent", display_name="Zed"))
    await registry.register_endpoint(
        _endpoint(agent_id="a-agent", display_name="Alpha", enabled=False)
    )

    all_rows = await registry.list_endpoints()
    enabled_rows = await registry.list_endpoints(enabled_only=True)

    assert [row.agent_id for row in all_rows] == ["a-agent", "z-agent"]
    assert [row.agent_id for row in enabled_rows] == ["z-agent"]


@pytest.mark.asyncio
async def test_external_agent_registry_set_enabled_missing_and_disabled_errors() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint())

    disabled = await registry.set_enabled("threat-intel", False)

    assert disabled.enabled is False
    with pytest.raises(ExternalAgentNotFoundError):
        await registry.get_endpoint("missing")
    with pytest.raises(ExternalAgentDisabledError):
        await registry.get_endpoint("threat-intel", require_enabled=True)


@pytest.mark.asyncio
async def test_external_agent_registry_set_health_missing_is_noop() -> None:
    registry = ExternalAgentRegistry()

    await registry.set_health("missing", "healthy")

    assert await registry.list_endpoints() == []


@pytest.mark.asyncio
async def test_external_agent_registry_build_client_requires_enabled_endpoint() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(enabled=False))

    with pytest.raises(ExternalAgentDisabledError):
        await registry.build_client(
            "threat-intel",
            tenant_id=TENANT_ID,
            bearer_token="tok-a2a",
        )


@pytest.mark.asyncio
async def test_external_agent_registry_build_client_uses_tenant_token_and_base_url() -> None:
    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(base_url=BASE_URL + "/root/"))

    client = await registry.build_client(
        "threat-intel",
        tenant_id=TENANT_ID,
        bearer_token="tok-a2a",
    )

    assert client.base_url == BASE_URL + "/root"
    assert client.tenant_id == TENANT_ID
    assert client.bearer_token == "tok-a2a"


@pytest.mark.asyncio
async def test_external_agent_registry_store_instances_isolate_same_agent_id() -> None:
    tenant_a_registry = ExternalAgentRegistry()
    tenant_b_registry = ExternalAgentRegistry()

    await tenant_a_registry.register_endpoint(
        _endpoint(display_name="Tenant A Intel", base_url="https://a.example.com")
    )
    await tenant_b_registry.register_endpoint(
        _endpoint(display_name="Tenant B Intel", base_url="https://b.example.com")
    )

    assert (await tenant_a_registry.get_endpoint("threat-intel")).base_url == (
        "https://a.example.com"
    )
    assert (await tenant_b_registry.get_endpoint("threat-intel")).base_url == (
        "https://b.example.com"
    )


@pytest.mark.asyncio
async def test_external_agents_router_list_marks_viewer_response_read_only() -> None:
    from backend.routers import external_agents

    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint())

    res = await external_agents.list_external_agents(
        _request(registry),  # type: ignore[arg-type]
        actor=_viewer(),
    )
    body = json.loads(res.body)

    assert body["can_register"] is False
    assert body["external_agents"][0]["agent_id"] == "threat-intel"
    assert body["external_agents"][0]["agent_card_url"].endswith(
        "/.well-known/agent.json"
    )


@pytest.mark.asyncio
async def test_external_agents_router_registers_endpoint_payload_with_config() -> None:
    from backend.routers import external_agents

    registry = ExternalAgentRegistry()
    res = await external_agents.register_external_agent(
        external_agents.RegisterExternalAgentRequest(
            agent_id="partner-bsp",
            display_name="Partner BSP",
            base_url="https://partner.example.com/a2a/",
            agent_name="bsp",
            auth_mode="bearer",
            token_ref="secret:partner-bsp",
            tags=["bsp", "partner"],
            capabilities=["device_tree"],
            config={"timeout_s": 30},
        ),
        _request(registry),  # type: ignore[arg-type]
        _operator(),
    )
    body = json.loads(res.body)["external_agent"]

    assert body["agent_id"] == "partner-bsp"
    assert body["base_url"] == "https://partner.example.com/a2a"
    assert body["token_ref"] == "secret:partner-bsp"
    assert body["config"] == {"timeout_s": 30}
    assert body["registered_at"] is not None
    assert body["updated_at"] is not None


@pytest.mark.asyncio
async def test_external_agents_router_rejects_invalid_registration() -> None:
    from backend.routers import external_agents

    with pytest.raises(HTTPException) as excinfo:
        await external_agents.register_external_agent(
            external_agents.RegisterExternalAgentRequest(
                agent_id="Partner",
                display_name="Partner",
                base_url="https://partner.example.com",
                agent_name="bsp",
            ),
            _request(ExternalAgentRegistry()),  # type: ignore[arg-type]
            _operator(),
        )

    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_external_agents_router_patches_kill_switch() -> None:
    from backend.routers import external_agents

    registry = ExternalAgentRegistry()
    await registry.register_endpoint(_endpoint(agent_id="partner-bsp", agent_name="bsp"))

    res = await external_agents.patch_external_agent(
        "partner-bsp",
        external_agents.PatchExternalAgentRequest(enabled=False),
        _request(registry),  # type: ignore[arg-type]
        _operator(),
    )
    body = json.loads(res.body)["external_agent"]

    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_external_agents_router_patch_missing_returns_404() -> None:
    from backend.routers import external_agents

    with pytest.raises(HTTPException) as excinfo:
        await external_agents.patch_external_agent(
            "missing",
            external_agents.PatchExternalAgentRequest(enabled=False),
            _request(ExternalAgentRegistry()),  # type: ignore[arg-type]
            _operator(),
        )

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_external_agents_router_registry_injection_keeps_tenant_views_separate() -> None:
    from backend.routers import external_agents

    tenant_a_registry = ExternalAgentRegistry()
    tenant_b_registry = ExternalAgentRegistry()
    await tenant_a_registry.register_endpoint(
        _endpoint(display_name="Tenant A Intel", base_url="https://a.example.com")
    )
    await tenant_b_registry.register_endpoint(
        _endpoint(display_name="Tenant B Intel", base_url="https://b.example.com")
    )

    res_a = await external_agents.list_external_agents(
        _request(tenant_a_registry),  # type: ignore[arg-type]
        actor=_operator("tenant-a"),
    )
    res_b = await external_agents.list_external_agents(
        _request(tenant_b_registry),  # type: ignore[arg-type]
        actor=_operator("tenant-b"),
    )

    assert json.loads(res_a.body)["external_agents"][0]["base_url"] == (
        "https://a.example.com"
    )
    assert json.loads(res_b.body)["external_agents"][0]["base_url"] == (
        "https://b.example.com"
    )

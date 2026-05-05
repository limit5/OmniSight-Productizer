"""BP.A2A.5 -- outbound A2A client tests."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend import circuit_breaker
from backend.a2a.agent_card import build_agent_card
from backend.a2a.client import (
    A2AClient,
    A2ACircuitOpen,
    A2ARequestFailed,
    A2AStreamEvent,
    A2ATimeout,
)
from backend.agents.external_agent_registry import ExternalAgentEndpoint
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

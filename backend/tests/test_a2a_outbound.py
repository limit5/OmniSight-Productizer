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

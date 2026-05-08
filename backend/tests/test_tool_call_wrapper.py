"""OP-114 - tool-call wrapper contract tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.agents import tool_call_wrapper as wrapper
from backend.agents.tool_call_wrapper import RetryPolicy, invoke_tool, invoke_tool_sync
from backend.agents.tool_dispatcher import ToolDispatcher


@pytest.fixture(autouse=True)
def _reset_circuits() -> None:
    wrapper.reset_circuits_for_tests()


def _payload(result) -> dict[str, Any]:
    return json.loads(result.content)


@pytest.mark.asyncio
async def test_invoke_tool_success_path() -> None:
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda args: {"value": args["value"]})

    result = await invoke_tool("Read", {"value": 7}, dispatcher=dispatcher, tool_use_id="tu_ok")

    assert result.tool_use_id == "tu_ok"
    assert result.content == json.dumps({"value": 7}, ensure_ascii=False)
    assert not result.is_error


@pytest.mark.asyncio
async def test_invoke_tool_retries_transient_error_then_succeeds() -> None:
    dispatcher = ToolDispatcher()
    calls = 0

    def handler(_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("network timed out")
        return "ok"

    dispatcher.register("Read", handler)

    result = await invoke_tool(
        "Read",
        {},
        RetryPolicy(transient_retries=1, backoff_seconds=0),
        dispatcher=dispatcher,
    )

    assert calls == 2
    assert result.content == "ok"
    assert not result.is_error


@pytest.mark.asyncio
async def test_invoke_tool_stops_after_retry_exhaustion() -> None:
    dispatcher = ToolDispatcher()
    calls = 0

    def handler(_args):
        nonlocal calls
        calls += 1
        raise ConnectionError("network down")

    dispatcher.register("Read", handler)

    result = await invoke_tool(
        "Read",
        {},
        RetryPolicy(transient_retries=1, backoff_seconds=0),
        dispatcher=dispatcher,
    )

    assert calls == 2
    assert result.is_error
    assert _payload(result)["exception_type"] == "ConnectionError"


@pytest.mark.asyncio
async def test_invoke_tool_does_not_retry_structural_error() -> None:
    dispatcher = ToolDispatcher()
    calls = 0

    def handler(_args):
        nonlocal calls
        calls += 1
        raise ValueError("bad input")

    dispatcher.register("Read", handler)

    result = await invoke_tool(
        "Read",
        {},
        RetryPolicy(transient_retries=1, backoff_seconds=0),
        dispatcher=dispatcher,
    )

    assert calls == 1
    assert result.is_error
    assert _payload(result)["exception_type"] == "ValueError"


@pytest.mark.asyncio
async def test_invoke_tool_circuit_opens_after_three_failures() -> None:
    dispatcher = ToolDispatcher()
    calls = 0

    def handler(_args):
        nonlocal calls
        calls += 1
        raise ConnectionError("network down")

    dispatcher.register("Read", handler)
    policy = RetryPolicy(transient_retries=0, backoff_seconds=0)

    for _ in range(3):
        result = await invoke_tool("Read", {}, policy, dispatcher=dispatcher)
        assert result.is_error

    blocked = await invoke_tool("Read", {}, policy, dispatcher=dispatcher)

    assert calls == 3
    assert blocked.is_error
    assert _payload(blocked)["error"] == "circuit_open"


@pytest.mark.asyncio
async def test_invoke_tool_circuit_recovers_after_open_window(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(wrapper.time, "monotonic", lambda: now)
    dispatcher = ToolDispatcher()
    calls = 0

    def handler(_args):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise ConnectionError("network down")
        return "recovered"

    dispatcher.register("Read", handler)
    policy = RetryPolicy(transient_retries=0, backoff_seconds=0)

    for _ in range(3):
        await invoke_tool("Read", {}, policy, dispatcher=dispatcher)

    assert _payload(await invoke_tool("Read", {}, policy, dispatcher=dispatcher))[
        "error"
    ] == "circuit_open"

    now += 31.0
    recovered = await invoke_tool("Read", {}, policy, dispatcher=dispatcher)

    assert calls == 4
    assert recovered.content == "recovered"
    assert not recovered.is_error


def test_invoke_tool_sync_entry_point() -> None:
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _args: "sync ok")

    result = invoke_tool_sync("Read", {}, dispatcher=dispatcher)

    assert result.content == "sync ok"
    assert not result.is_error

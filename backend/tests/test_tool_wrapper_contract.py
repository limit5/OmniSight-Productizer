"""OP-118 / MP.W17.8 — top-5 tool wrapper contract tests."""

from __future__ import annotations

import json

import pytest

from backend.agents.circuit_breaker import CircuitBreaker
from backend.agents.tool_dispatcher import ToolDispatcher
from backend.agents.tool_wrapper import ToolWrapper, validate_handoff_envelopes


def _wrapper(dispatcher: ToolDispatcher, *, threshold: int = 3) -> ToolWrapper:
    return ToolWrapper(
        dispatcher=dispatcher,
        breaker=CircuitBreaker(
            "mp_w17_tool_wrapper",
            failure_threshold=threshold,
            recovery_timeout=60,
        ),
        sleep=_no_sleep,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_wrapper_invokes_registered_tool_by_name() -> None:
    calls: list[dict] = []
    dispatcher = ToolDispatcher()

    async def read_handler(payload: dict) -> dict:
        calls.append(payload)
        return {"read": payload["path"]}

    dispatcher.register("Read", read_handler)

    result = await _wrapper(dispatcher).invoke("tu-read", "Read", {"path": "a.py"})

    assert json.loads(result.content) == {"read": "a.py"}
    assert calls == [{"path": "a.py"}]
    assert result.is_error is False


@pytest.mark.asyncio
async def test_wrapper_retries_transient_tool_failure_then_succeeds() -> None:
    attempts = {"count": 0}
    dispatcher = ToolDispatcher()

    def flaky_handler(_payload: dict) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError("temporary outage")
        return "ok"

    dispatcher.register("Bash", flaky_handler)

    result = await _wrapper(dispatcher).invoke("tu-bash", "Bash", {})

    assert attempts["count"] == 2
    assert result.content == "ok"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_wrapper_does_not_retry_structural_dispatch_error() -> None:
    dispatcher = ToolDispatcher()
    dispatcher.register("Read", lambda _payload: "unused")

    result = await _wrapper(dispatcher).invoke("tu-missing", "Write", {})

    payload = json.loads(result.content)
    assert payload["error"] == "no_handler_registered"
    assert payload["tool_name"] == "Write"
    assert result.is_error is True


@pytest.mark.asyncio
async def test_wrapper_circuit_breaker_trips_after_three_exhausted_failures() -> None:
    calls = {"count": 0}
    dispatcher = ToolDispatcher()

    def down(_payload: dict) -> None:
        calls["count"] += 1
        raise ConnectionError("down")

    dispatcher.register("Read", down)
    wrapper = _wrapper(dispatcher, threshold=3)

    for idx in range(3):
        result = await wrapper.invoke(f"tu-{idx}", "Read", {})
        assert json.loads(result.content)["exception_type"] == "ConnectionError"

    blocked = await wrapper.invoke("tu-open", "Read", {})

    assert calls["count"] == 9
    assert wrapper.breaker.state == "open"
    assert json.loads(blocked.content) == {
        "error": "circuit_open",
        "tool_name": "Read",
    }


def test_handoff_envelope_rejects_duplicate_handoff_id() -> None:
    raw = [
        {
            "handoff_id": "h-1",
            "from_agent": "backend-a",
            "to_agent": "backend-b",
            "payload": {"summary": "first"},
        },
        {
            "handoff_id": "h-1",
            "from_agent": "backend-b",
            "to_agent": "backend-c",
            "payload": {"summary": "duplicate"},
        },
    ]

    with pytest.raises(ValueError, match="duplicate handoff_id: h-1"):
        validate_handoff_envelopes(raw)

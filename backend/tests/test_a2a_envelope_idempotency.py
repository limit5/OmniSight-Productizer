"""OP-115 -- A2A handoff envelope idempotency tests."""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.a2a_envelope import (
    HandoffEnvelope,
    HandoffReceiver,
    send_handoff,
)


def _handoff(handoff_id: str = "h-115") -> HandoffEnvelope:
    return HandoffEnvelope(
        handoff_id=handoff_id,
        from_agent="sender",
        to_agent="receiver",
        payload={"summary": "carry state"},
    )


@pytest.mark.asyncio
async def test_duplicate_handoff_id_rejected_with_structured_error() -> None:
    async def handler(envelope: HandoffEnvelope) -> dict:
        return {"status": "completed", "seen": envelope.payload["summary"]}

    receiver = HandoffReceiver(handler)
    first = await receiver.receive(_handoff())
    second = await receiver.receive(_handoff())

    assert first.payload == {"status": "completed", "seen": "carry state"}
    assert second.handoff_id == "h-115"
    assert second.from_agent == "receiver"
    assert second.to_agent == "sender"
    assert second.payload == {
        "status": "error",
        "error": {
            "code": "duplicate_handoff_id",
            "message": "handoff_id h-115 was already received",
        },
    }


@pytest.mark.asyncio
async def test_timeout_returns_error_and_retry_with_same_handoff_id_succeeds() -> None:
    calls = 0

    async def handler(envelope: HandoffEnvelope) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return {"status": "completed", "handoff_id": envelope.handoff_id}

    receiver = HandoffReceiver(handler)
    envelope = _handoff("h-retry")

    timed_out = await send_handoff(receiver, envelope, timeout_s=0.01)
    retried = await send_handoff(receiver, envelope, timeout_s=0.1)

    assert timed_out.payload["status"] == "error"
    assert timed_out.payload["error"]["code"] == "handoff_timeout"
    assert retried.payload == {"status": "completed", "handoff_id": "h-retry"}
    assert calls == 2

"""Fix-D D3 — EventBus pub/sub + backpressure coverage.

The SSE bus is the spine of the live UI. Regressions here usually
present as "events mysteriously stop arriving" or "subscribers leak",
both of which are painful to diagnose after the fact. These tests pin
the public contract: subscribe/publish/unsubscribe, telemetry
counters, and the drop-slow-subscriber backpressure policy.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.events import EventBus, bus as _singleton_bus


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fresh bus fixture — singleton would leak state between tests.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
def fresh_bus():
    return EventBus()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  subscribe / unsubscribe
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_subscribe_returns_queue_and_bumps_count(fresh_bus):
    assert fresh_bus.subscriber_count == 0
    q1 = fresh_bus.subscribe()
    assert isinstance(q1, asyncio.Queue)
    assert fresh_bus.subscriber_count == 1
    q2 = fresh_bus.subscribe()
    assert fresh_bus.subscriber_count == 2
    assert q1 is not q2


def test_unsubscribe_is_idempotent(fresh_bus):
    q = fresh_bus.subscribe()
    fresh_bus.unsubscribe(q)
    assert fresh_bus.subscriber_count == 0
    # Second call must not raise — set.discard semantics.
    fresh_bus.unsubscribe(q)
    assert fresh_bus.subscriber_count == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  publish — single + fan-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_publish_delivers_to_single_subscriber(fresh_bus):
    q = fresh_bus.subscribe()
    fresh_bus.publish("agent_update", {"agent_id": "a1", "status": "running"})
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert msg["event"] == "agent_update"
    payload = json.loads(msg["data"])
    assert payload["agent_id"] == "a1"
    assert payload["status"] == "running"
    assert "timestamp" in payload  # publish auto-stamps


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_subscribers(fresh_bus):
    q1 = fresh_bus.subscribe()
    q2 = fresh_bus.subscribe()
    q3 = fresh_bus.subscribe()
    fresh_bus.publish("tick", {"n": 1})
    m1 = await asyncio.wait_for(q1.get(), timeout=1)
    m2 = await asyncio.wait_for(q2.get(), timeout=1)
    m3 = await asyncio.wait_for(q3.get(), timeout=1)
    assert m1 == m2 == m3
    assert json.loads(m1["data"])["n"] == 1


@pytest.mark.asyncio
async def test_publish_preserves_caller_timestamp_if_set(fresh_bus):
    q = fresh_bus.subscribe()
    fresh_bus.publish("x", {"timestamp": "2026-04-14T00:00:00", "k": "v"})
    msg = await asyncio.wait_for(q.get(), timeout=1)
    payload = json.loads(msg["data"])
    assert payload["timestamp"] == "2026-04-14T00:00:00"


def test_publish_without_subscribers_does_not_raise(fresh_bus):
    # No running loop + no subscribers → must be a complete no-op.
    fresh_bus.publish("orphan_event", {"whatever": True})
    assert fresh_bus.subscriber_count == 0
    assert fresh_bus.subscriber_dropped == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Backpressure: slow subscriber gets dropped
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_slow_subscriber_is_dropped_and_counter_incs(fresh_bus, monkeypatch):
    # Shrink the per-subscriber queue so we can fill it without pushing 1000.
    real_queue = asyncio.Queue
    def small_queue(maxsize=1000):  # noqa: ARG001
        return real_queue(maxsize=2)
    monkeypatch.setattr("backend.events.asyncio.Queue", small_queue)

    slow = fresh_bus.subscribe()
    fast = fresh_bus.subscribe()

    # `fast` will keep draining; `slow` never drains → fills at msg #3.
    async def drain(q):
        while True:
            await q.get()

    drain_task = asyncio.create_task(drain(fast))
    try:
        for i in range(5):
            fresh_bus.publish("burst", {"i": i})
            await asyncio.sleep(0)  # let drain_task run

        # slow had maxsize=2 → after the 3rd put_nowait it got dropped.
        assert fresh_bus.subscriber_dropped >= 1
        assert slow not in fresh_bus._subscribers
        assert fast in fresh_bus._subscribers
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  emit_* helpers — public API surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.asyncio
async def test_emit_agent_update_routes_through_bus():
    from backend import events
    q = events.bus.subscribe()
    try:
        events.emit_agent_update("a1", "running", thought_chain="thinking")
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "agent_update"
        payload = json.loads(msg["data"])
        assert payload["agent_id"] == "a1"
        assert payload["status"] == "running"
        assert payload["thought_chain"] == "thinking"
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_tool_progress_truncates_long_output():
    from backend import events
    q = events.bus.subscribe()
    try:
        big = "x" * 5000
        events.emit_tool_progress("build", "done", output=big)
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert len(payload["output"]) == 1000  # hard cap in emit_tool_progress
    finally:
        events.bus.unsubscribe(q)


def test_singleton_bus_is_event_bus_instance():
    assert isinstance(_singleton_bus, EventBus)

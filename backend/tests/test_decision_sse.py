"""Phase 47 Batch D — SSE round-trip tests.

Subscribes directly to the EventBus (not the HTTP SSE stream) so we can
assert on specific event payloads without parsing the streaming format.
This still exercises the producer side end-to-end: HTTP → handler →
decision_engine → bus.publish → subscriber queue.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import decision_engine as de
from backend import budget_strategy as bs
from backend.events import bus


def _drain_matching(queue: asyncio.Queue, event_name: str, limit: int = 20) -> list[dict]:
    """Pull messages off the queue until empty, keep only matching events."""
    out = []
    for _ in range(limit):
        try:
            msg = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if msg.get("event") == event_name:
            out.append(json.loads(msg["data"]))
    return out


class TestDecisionSSE:

    def setup_method(self):
        de._reset_for_tests()
        bs._reset_for_tests()

    @pytest.mark.asyncio
    async def test_approve_emits_decision_resolved(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine",
                       options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
        q = bus.subscribe()
        try:
            r = await client.post(f"/api/v1/decisions/{d.id}/approve", json={"option_id": "b"})
            assert r.status_code == 200
            # event is emitted synchronously in publish() — already in the queue
            events = _drain_matching(q, "decision_resolved")
            assert len(events) == 1
            ev = events[0]
            assert ev["id"] == d.id
            assert ev["chosen_option_id"] == "b"
            assert ev["status"] == "approved"
            assert ev["resolver"] == "user"
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_reject_emits_with_sentinel_chosen(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine")
        q = bus.subscribe()
        try:
            r = await client.post(f"/api/v1/decisions/{d.id}/reject")
            assert r.status_code == 200
            events = _drain_matching(q, "decision_resolved")
            assert len(events) == 1
            # N8 contract: rejection uses the __rejected__ sentinel
            assert events[0]["chosen_option_id"] == "__rejected__"
            assert events[0]["status"] == "rejected"
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_undo_emits_decision_undone(self, client):
        de.set_mode("supervised")  # auto-executes
        d = de.propose("k", "t", severity="routine")
        q = bus.subscribe()
        try:
            r = await client.post(f"/api/v1/decisions/{d.id}/undo")
            assert r.status_code == 200
            events = _drain_matching(q, "decision_undone")
            assert len(events) == 1
            assert events[0]["id"] == d.id
            assert events[0]["status"] == "undone"
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_put_mode_emits_mode_changed(self, client):
        de.set_mode("manual")
        q = bus.subscribe()
        try:
            r = await client.put("/api/v1/operation-mode", json={"mode": "full_auto"})
            assert r.status_code == 200
            events = _drain_matching(q, "mode_changed")
            assert len(events) == 1
            ev = events[0]
            assert ev["mode"] == "full_auto"
            assert ev["previous"] == "manual"
            assert ev["parallel_cap"] == 4
            assert "in_flight" in ev and "over_cap" in ev
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_put_budget_strategy_emits_event(self, client):
        q = bus.subscribe()
        try:
            r = await client.put("/api/v1/budget-strategy", json={"strategy": "quality"})
            assert r.status_code == 200
            events = _drain_matching(q, "budget_strategy_changed")
            assert len(events) == 1
            assert events[0]["strategy"] == "quality"
            assert events[0]["previous"] == "balanced"
            assert events[0]["tuning"]["max_retries"] == 3
        finally:
            bus.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_sweep_emits_resolved_for_timed_out(self, client):
        de.set_mode("manual")
        d = de.propose("k", "t", severity="routine", timeout_s=0.05,
                       options=[{"id": "safe", "label": "S"}, {"id": "other", "label": "O"}])
        await asyncio.sleep(0.1)
        q = bus.subscribe()
        try:
            r = await client.post("/api/v1/decisions/sweep")
            assert r.status_code == 200
            events = _drain_matching(q, "decision_resolved")
            assert any(e["id"] == d.id and e["status"] == "timeout_default"
                       and e["chosen_option_id"] == "safe" for e in events)
        finally:
            bus.unsubscribe(q)


class TestSchemaContract:
    """Verify SSE schemas registered (N12 fix) match what publish() sends."""

    def setup_method(self):
        de._reset_for_tests()

    def test_schema_export_includes_source(self):
        from backend.sse_schemas import SSE_EVENT_SCHEMAS
        assert "decision_resolved" in SSE_EVENT_SCHEMAS
        fields = SSE_EVENT_SCHEMAS["decision_resolved"].model_fields
        assert "source" in fields

    def test_published_payload_fits_schema(self):
        from backend.sse_schemas import SSEDecision
        de.set_mode("manual")
        d = de.propose("kind", "title", severity="routine",
                       source={"agent_id": "a1"})
        # Pydantic should accept the dict form we actually publish
        validated = SSEDecision(**d.to_dict())
        assert validated.source == {"agent_id": "a1"}
        assert validated.id == d.id

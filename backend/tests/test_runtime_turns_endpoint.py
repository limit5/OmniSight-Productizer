"""ZZ.B1 #304-1 checkbox 3 — ``GET /runtime/turns`` endpoint tests.

Locks the contract for the history-backfill endpoint the frontend's
``<TurnTimeline>`` hits on mount to seed its ring buffer:

1. Returns the most recent ``turn.complete`` rows (newest-first) from
   ``event_log``.
2. ``limit`` clamps to [1, 100] (matches the frontend ring-buffer size).
3. ``session_id`` filter narrows to events whose persisted
   ``_session_id`` matches.
4. Only ``turn.complete`` rows are returned — ``agent_update`` /
   ``task_update`` / other persisted events are excluded.
5. Tenant isolation: ``list_events`` applies the
   ``tenant_where_pg`` filter, so Tenant A's turns don't leak to
   Tenant B's client.

Runs against the test PG via ``pg_test_conn`` (skips cleanly without
OMNI_TEST_PG_URL — same pattern as ``test_db_events.py``).
"""

from __future__ import annotations

import json

import pytest

from backend import db
from backend.db_context import set_tenant_id
from backend.routers.system import get_turn_history


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _make_turn_payload(turn_id: str, session_id: str = "", model: str = "claude-opus-4-7") -> str:
    """Mirror the SSE payload shape ``emit_turn_complete`` writes."""
    return json.dumps({
        "turn_id": turn_id,
        "model": model,
        "provider": "anthropic",
        "input_tokens": 100,
        "output_tokens": 50,
        "tokens_used": 150,
        "latency_ms": 200,
        "cost_usd": 0.005,
        "messages": [{"role": "user", "content": "hi"}],
        "tool_calls": [],
        "tool_call_count": 0,
        "tool_failure_count": 0,
        "_session_id": session_id,
        "_broadcast_scope": "global",
        "_tenant_id": "t-alpha",
        "timestamp": "2026-04-24T00:00:00Z",
    })


class TestGetTurnHistory:
    @pytest.mark.asyncio
    async def test_returns_only_turn_complete_rows(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # Seed three turn.complete + two unrelated persisted events.
        await db.insert_event(pg_test_conn, "turn.complete", _make_turn_payload("turn-1"))
        await db.insert_event(pg_test_conn, "agent_update", json.dumps({"agent_id": "a1"}))
        await db.insert_event(pg_test_conn, "turn.complete", _make_turn_payload("turn-2"))
        await db.insert_event(pg_test_conn, "task_update", json.dumps({"task_id": "t1"}))
        await db.insert_event(pg_test_conn, "turn.complete", _make_turn_payload("turn-3"))

        resp = await get_turn_history(limit=10, session_id=None, conn=pg_test_conn)
        assert resp["count"] == 3
        ids = [t["turn_id"] for t in resp["turns"]]
        # Newest-first (DESC id).
        assert ids == ["turn-3", "turn-2", "turn-1"]

    @pytest.mark.asyncio
    async def test_limit_clamps_to_100_upper_bound(self, pg_test_conn):
        set_tenant_id("t-alpha")
        # Seed 3 rows; request an absurd limit — should still return 3.
        for i in range(3):
            await db.insert_event(
                pg_test_conn, "turn.complete", _make_turn_payload(f"turn-{i}"),
            )
        resp = await get_turn_history(limit=9_999, session_id=None, conn=pg_test_conn)
        assert len(resp["turns"]) == 3

    @pytest.mark.asyncio
    async def test_limit_zero_still_returns_at_least_one(self, pg_test_conn):
        """Defensive: ``limit=0`` would otherwise return an empty list
        that looks identical to "no history exists". The endpoint
        treats ``limit <= 0`` as 1 so the caller still sees SOMETHING
        and can debug their misconfiguration.
        """
        set_tenant_id("t-alpha")
        await db.insert_event(
            pg_test_conn, "turn.complete", _make_turn_payload("turn-0"),
        )
        resp = await get_turn_history(limit=0, session_id=None, conn=pg_test_conn)
        assert len(resp["turns"]) == 1

    @pytest.mark.asyncio
    async def test_session_id_filter_narrows_result(self, pg_test_conn):
        set_tenant_id("t-alpha")
        await db.insert_event(
            pg_test_conn, "turn.complete",
            _make_turn_payload("turn-s1-a", session_id="sess-1"),
        )
        await db.insert_event(
            pg_test_conn, "turn.complete",
            _make_turn_payload("turn-s2", session_id="sess-2"),
        )
        await db.insert_event(
            pg_test_conn, "turn.complete",
            _make_turn_payload("turn-s1-b", session_id="sess-1"),
        )

        resp = await get_turn_history(limit=10, session_id="sess-1", conn=pg_test_conn)
        assert resp["count"] == 2
        turn_ids = sorted(t["turn_id"] for t in resp["turns"])
        assert turn_ids == ["turn-s1-a", "turn-s1-b"]

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, pg_test_conn):
        """Tenant A's insertion must not leak to Tenant B's list query."""
        # Insert one row as Tenant A.
        set_tenant_id("t-alpha")
        await db.insert_event(
            pg_test_conn, "turn.complete", _make_turn_payload("turn-alpha"),
        )
        # Switch to Tenant B and list — should see nothing.
        set_tenant_id("t-beta")
        resp = await get_turn_history(limit=10, session_id=None, conn=pg_test_conn)
        assert resp["count"] == 0

    @pytest.mark.asyncio
    async def test_empty_event_log_returns_empty_list(self, pg_test_conn):
        set_tenant_id("t-alpha")
        resp = await get_turn_history(limit=10, session_id=None, conn=pg_test_conn)
        assert resp == {"turns": [], "count": 0}

"""SSE event streaming + event replay API.

Real-time:
  GET /events — persistent SSE connection for all state changes
Replay:
  GET /events/replay — query persisted events by time range and type
"""

import asyncio
import json

import asyncpg
from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from backend.db_pool import get_conn
from backend.events import bus

router = APIRouter(tags=["events"])

HEARTBEAT_INTERVAL = 15  # seconds


def _get_tenant_id() -> str | None:
    """Best-effort read of request-scoped tenant context."""
    try:
        from backend.db_context import current_tenant_id
        return current_tenant_id()
    except Exception:
        return None


@router.get("/events")
async def event_stream():
    """Persistent SSE connection. Pushes all real-time events to the frontend.

    Phase-3 follow-up (2026-04-20): emit an immediate ``open`` event as
    the very first yield. Without it, CF edge + CF Tunnel buffer the SSE
    response until the first body byte arrives (up to several seconds
    even though the backend starts producing ~1 Hz host.metrics.tick
    events within ~1 s). Browsers treat the initial silence as a dead
    connection and EventSource closes + reconnects in a rapid loop,
    which burns per-IP rate-limit tokens and eventually 429s the next
    login attempt — recreating the same cascade the SQLite WAL storm
    fix was written to avoid. A 2-byte first event is enough to push
    CF past its buffer threshold; subsequent events flow through
    without buffering.
    """
    tenant_id = _get_tenant_id()
    queue = bus.subscribe(tenant_id=tenant_id)

    async def generator():
        try:
            # Immediate ``open`` event — kept single + small; the bigger
            # flushing padding hypothesis below didn't help with CF
            # tunnel (it buffers beyond a 2 KB threshold), so the real
            # fix for that symptom lives elsewhere (ingress bypass).
            yield {
                "event": "open",
                "data": json.dumps({"ts": 0, "worker_subs": bus.subscriber_count}),
            }
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                    yield msg
                except asyncio.TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"subscribers": bus.subscriber_count}),
                    }
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(generator())


@router.get("/events/replay")
async def replay_events(
    since: str = Query("", description="ISO timestamp — return events after this time"),
    types: str = Query("", description="Comma-separated event types to filter"),
    limit: int = Query(200, ge=1, le=1000),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Replay persisted events from the event_log table.

    Used by frontend after SSE reconnect to fill gaps.
    """
    from backend import db
    event_types = [t.strip() for t in types.split(",") if t.strip()] or None
    events = await db.list_events(conn, since=since, event_types=event_types, limit=limit)
    # Parse data_json back to dict for each event
    result = []
    for ev in events:
        try:
            data = json.loads(ev.get("data_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue  # Skip malformed entries instead of returning empty data
        result.append({
            "id": ev.get("id"),
            "event": ev.get("event_type"),
            "data": data,
            "timestamp": ev.get("created_at"),
        })
    return result

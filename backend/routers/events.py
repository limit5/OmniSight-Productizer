"""SSE event streaming + event replay API.

Real-time:
  GET /events — persistent SSE connection for all state changes
Replay:
  GET /events/replay — query persisted events by time range and type
"""

import asyncio
import json

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

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
    """Persistent SSE connection. Pushes all real-time events to the frontend."""
    tenant_id = _get_tenant_id()
    queue = bus.subscribe(tenant_id=tenant_id)

    async def generator():
        try:
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
):
    """Replay persisted events from the event_log table.

    Used by frontend after SSE reconnect to fill gaps.
    """
    from backend import db
    event_types = [t.strip() for t in types.split(",") if t.strip()] or None
    events = await db.list_events(since=since, event_types=event_types, limit=limit)
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

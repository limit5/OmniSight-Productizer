"""Persistent SSE endpoint — frontend subscribes once, receives all state changes.

Events:
  - agent_update:   Agent status/thoughtChain changed
  - task_update:    Task status/assignment changed
  - tool_progress:  Tool execution start/done/error
  - pipeline:       LangGraph pipeline phase changes
  - heartbeat:      Keep-alive every 15 seconds
"""

import asyncio
import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from backend.events import bus

router = APIRouter(tags=["events"])

HEARTBEAT_INTERVAL = 15  # seconds


@router.get("/events")
async def event_stream():
    """Persistent SSE connection. Pushes all real-time events to the frontend."""
    queue = bus.subscribe()

    async def generator():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
                    yield msg
                except asyncio.TimeoutError:
                    # Keep-alive heartbeat
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"subscribers": bus.subscriber_count}),
                    }
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(generator())

"""OP-820 -- SDK runner per-ticket cost tracker.

The SDK runner can pass :func:`sdk_cost_alert_sink` to ``CostGuard`` as
``alert_sink``. Each alert is folded into a process-local ticket ledger
and fanned out to subscribers of ``GET /sdk/cost?ticket=OP-123`` when
the request is an EventSource/SSE request.

Module-global state audit
-------------------------
One in-memory ledger and subscriber map. This mirrors existing
operator-dashboard process-local registries: it is live telemetry for
the current SDK runner workspace, not a durable billing ledger.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from backend import auth
from backend.agents.cost_guard import BudgetAlert


router = APIRouter(prefix="/sdk/cost", tags=["sdk-cost"])

SDK_COST_EVENT = "sdk.cost.updated"
_TICKET_RE = re.compile(r"^OP-[A-Z0-9]+$", re.IGNORECASE)
_ROLLING_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class SdkCostEvent:
    ticket: str
    alert_id: str
    scope: str
    period: str
    level: str
    action: str
    delta_usd: float
    observed_usd: float
    threshold_usd: float
    cumulative_usd: float
    fired_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "alert_id": self.alert_id,
            "scope": self.scope,
            "period": self.period,
            "level": self.level,
            "action": self.action,
            "delta_usd": round(self.delta_usd, 6),
            "observed_usd": round(self.observed_usd, 6),
            "threshold_usd": round(self.threshold_usd, 6),
            "cumulative_usd": round(self.cumulative_usd, 6),
            "fired_at": self.fired_at.isoformat(),
        }


class SdkCostTracker:
    def __init__(self) -> None:
        self._events: list[SdkCostEvent] = []
        self._last_observed_by_ticket: dict[str, float] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def record_alert(self, alert: BudgetAlert) -> SdkCostEvent | None:
        ticket = _ticket_from_alert(alert)
        if ticket is None:
            return None

        async with self._lock:
            previous = self._last_observed_by_ticket.get(ticket, 0.0)
            observed = max(float(alert.observed_usd), 0.0)
            delta = observed - previous if observed >= previous else observed
            self._last_observed_by_ticket[ticket] = max(previous, observed)
            event = SdkCostEvent(
                ticket=ticket,
                alert_id=alert.alert_id,
                scope=str(alert.scope),
                period=str(alert.period),
                level=str(alert.level),
                action=str(alert.action),
                delta_usd=max(delta, 0.0),
                observed_usd=observed,
                threshold_usd=max(float(alert.threshold_usd), 0.0),
                cumulative_usd=self._last_observed_by_ticket[ticket],
                fired_at=alert.fired_at,
            )
            self._events.append(event)
            payload = event.to_payload()
            for q in list(self._subscribers.get(ticket, set())):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    self._subscribers.get(ticket, set()).discard(q)
            return event

    async def snapshot(self, ticket: str | None, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        since = now - _ROLLING_WINDOW
        async with self._lock:
            events = [
                ev for ev in self._events
                if (ticket is None or ev.ticket == ticket) and ev.fired_at >= since
            ]
            tickets = sorted({ev.ticket for ev in events} | ({ticket} if ticket else set()))
            rows = []
            for key in tickets:
                ticket_events = [ev for ev in events if ev.ticket == key]
                rolling = sum(ev.delta_usd for ev in ticket_events)
                rows.append({
                    "ticket": key,
                    "rolling_24h_usd": round(rolling, 6),
                    "cumulative_usd": round(self._last_observed_by_ticket.get(key, 0.0), 6),
                    "alert_count_24h": len(ticket_events),
                    "last_alert_at": (
                        max((ev.fired_at for ev in ticket_events), default=None).isoformat()
                        if ticket_events else None
                    ),
                })
            rows.sort(key=lambda row: (-row["rolling_24h_usd"], row["ticket"]))
            return {
                "ticket": ticket,
                "window_hours": 24,
                "generated_at": now.isoformat(),
                "rows": rows,
                "total_rolling_24h_usd": round(sum(row["rolling_24h_usd"] for row in rows), 6),
                "total_cumulative_usd": round(sum(row["cumulative_usd"] for row in rows), 6),
                "events": [ev.to_payload() for ev in events[-25:]],
            }

    async def subscribe(self, ticket: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.setdefault(ticket, set()).add(q)
        return q

    async def unsubscribe(self, ticket: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(ticket)
            if not subs:
                return
            subs.discard(q)
            if not subs:
                self._subscribers.pop(ticket, None)

    async def reset_for_tests(self) -> None:
        async with self._lock:
            self._events.clear()
            self._last_observed_by_ticket.clear()
            self._subscribers.clear()


tracker = SdkCostTracker()


async def sdk_cost_alert_sink(alert: BudgetAlert) -> None:
    """CostGuard ``alert_sink`` that feeds the OP-820 live tracker."""
    await tracker.record_alert(alert)


def _normalise_ticket(ticket: str | None) -> str | None:
    value = (ticket or "").strip().upper()
    if not value:
        return None
    if not _TICKET_RE.match(value):
        raise HTTPException(status_code=400, detail="ticket must look like OP-XX")
    return value


def _ticket_from_alert(alert: BudgetAlert) -> str | None:
    for candidate in (alert.scope.key, str(alert.scope)):
        match = re.search(r"\bOP-[A-Z0-9]+\b", candidate, re.IGNORECASE)
        if match:
            return match.group(0).upper()
    return None


@router.get("", response_model=None)
async def get_sdk_cost(
    request: Request,
    ticket: str | None = Query(None),
    stream: bool = Query(False),
    _user: auth.User = Depends(auth.require_admin),
):
    ticket_key = _normalise_ticket(ticket)
    wants_stream = stream or "text/event-stream" in request.headers.get("accept", "")
    if not wants_stream:
        return JSONResponse(await tracker.snapshot(ticket_key))
    if ticket_key is None:
        raise HTTPException(status_code=400, detail="ticket is required for stream")

    queue = await tracker.subscribe(ticket_key)

    async def generator():
        try:
            yield {
                "event": SDK_COST_EVENT,
                "data": json.dumps(await tracker.snapshot(ticket_key)),
            }
            while True:
                if await request.is_disconnected():
                    break
                payload = await queue.get()
                yield {
                    "event": SDK_COST_EVENT,
                    "data": json.dumps(payload),
                }
        finally:
            await tracker.unsubscribe(ticket_key, queue)

    return EventSourceResponse(generator())


async def _reset_for_tests() -> None:
    await tracker.reset_for_tests()

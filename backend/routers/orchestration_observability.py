"""O9 (#272) — orchestration observability HTTP surface.

Exposes the dashboard / Grafana data plane:

  * ``GET /orchestration/snapshot`` — single roll-up the
    ``orchestration-panel.tsx`` polls every 10 s. Includes queue depth,
    held locks, merger vote rates, awaiting-human-+2 list, and worker
    pool capacity.
  * ``GET /orchestration/awaiting-human`` — just the awaiting-human list.
    Useful for ops / Slack bots that want a thin payload.
  * ``POST /orchestration/queue-tick`` — manually fire an
    ``orchestration.queue.tick`` SSE event. Mostly for tests + the
    occasional debug nudge from operators; the orchestrator's
    housekeeping loop emits this on its own cadence.

These endpoints sit *inside* the authenticated surface (no separate
prefix from the rest of the API) because they leak in-flight task ids;
the bare ``/metrics`` exporter from ``observability.py`` stays
unauthenticated for Prometheus.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from backend import orchestration_observability as obs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["orchestration-observability"])


@router.get("/orchestration/snapshot")
async def orchestration_snapshot() -> dict:
    """Single roll-up for the orchestration panel.

    Returns:
      * ``queue.by_priority`` / ``queue.by_state`` / ``queue.total``
      * ``locks.by_task`` / ``locks.total_paths`` / ``locks.total_tasks``
      * ``merger.plus_two_total`` / ``merger.abstain_total`` /
        ``merger.security_refusal_total`` plus rate fields
      * ``workers.active`` / ``workers.inflight`` / ``workers.capacity``
      * ``awaiting_human_plus_two`` — list of pending changes
      * ``awaiting_human_warn_hours`` — soft alert threshold
    """
    return obs.snapshot_orchestration()


@router.get("/orchestration/awaiting-human")
async def orchestration_awaiting_human() -> dict:
    """Lightweight list-only view for Slack / CLI bots."""
    return {
        "items": [e.to_dict() for e in obs.list_awaiting_human()],
        "warn_hours": obs.AWAITING_HUMAN_WARN_HOURS,
    }


@router.post("/orchestration/queue-tick")
async def orchestration_queue_tick() -> dict:
    """Force a queue tick + push the SSE event.

    Returns the snapshot that was emitted so the caller doesn't have to
    chase it via SSE if all they wanted was a one-off probe.
    """
    obs.emit_queue_tick()
    return {"ok": True, "snapshot": obs.snapshot_orchestration()}

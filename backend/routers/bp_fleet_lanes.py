"""WP.10 — BP dispatch board lanes router.

Surfaces the BP Fleet UI Lanes (Active / Scheduled / Ambient / History)
to the dashboard tile/page. Implementation details (lane semantics,
inspired-by-Warp license note) live in :mod:`backend.bp_fleet_lanes`.

Endpoints
---------
* ``GET  /bp/fleet/lanes``                — full 4-lane snapshot for the dispatch board.
* ``GET  /bp/fleet/lanes/counts``         — per-lane counts only (header chip tile).
* ``GET  /bp/fleet/agents/{agent_id}``    — detail-panel payload for one agent.
* ``POST /bp/fleet/agents/{agent_id}/revoke`` — operator-initiated revoke.

The router reads the in-memory agent mirror owned by
:mod:`backend.routers.agents` (the canonical source for live agent state
in the current data model). Pulling the mirror lazily inside each
request avoids the import-time cycle that would result from importing
``_agents`` at module scope.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from backend import bp_fleet_lanes as _lanes
from backend.models import Agent, AgentStatus


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bp/fleet", tags=["bp", "fleet"])


def _current_agents() -> list[Agent]:
    """Pull a snapshot of agents from the in-memory mirror.

    Imported lazily so the BP fleet router can be unit-tested without
    the full agents-router import chain (character cards, talent tree,
    party store...). Tests monkeypatch this seam to inject fixtures.
    """
    from backend.routers import agents as _agents_router  # noqa: PLC0415
    return list(_agents_router._agents.values())


def _lane_payload(agent: Agent) -> dict[str, Any]:
    """Compact per-agent row shape for the lane list view.

    The lane card UI only needs id/name/status/progress/sub_type — the
    full detail envelope (with sub_tasks, workspace, etc.) is served by
    the per-agent endpoint when the operator opens the side panel.
    """
    return {
        "id": agent.id,
        "name": agent.name,
        "type": agent.type.value if hasattr(agent.type, "value") else str(agent.type),
        "sub_type": agent.sub_type,
        "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
        "ai_model": agent.ai_model,
        "progress": {
            "current": agent.progress.current,
            "total": agent.progress.total,
        },
    }


@router.get("/lanes")
async def get_lanes() -> dict[str, Any]:
    """Return the four lanes with compact per-agent rows."""
    agents = _current_agents()
    binned = _lanes.classify(agents)
    return {
        "lanes": {
            key: [_lane_payload(a) for a in binned[key]]
            for key in _lanes.LANE_KEYS
        },
        "counts": {key: len(binned[key]) for key in _lanes.LANE_KEYS},
    }


@router.get("/lanes/counts")
async def get_lane_counts() -> dict[str, int]:
    """Return only the per-lane counts — cheap header / tile poll."""
    return _lanes.lane_counts(_current_agents())


@router.get("/agents/{agent_id}")
async def get_agent_detail(agent_id: str) -> dict[str, Any]:
    """Detail-panel payload (timeline-ready envelope) for one agent."""
    for agent in _current_agents():
        if agent.id == agent_id:
            return _lanes.detail_payload(agent)
    raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")


@router.post("/agents/{agent_id}/revoke")
async def revoke_agent(agent_id: str) -> dict[str, Any]:
    """Operator-initiated revoke for an agent in Active or Ambient lane.

    Implementation today: flip the in-memory mirror's status to
    ``error`` with a revoke note. The agents router owns persistence —
    if/when ``_persist`` is wired in here we'll round-trip through the
    DB, but the current contract (memory-first, see
    ``agents._persist``) is honoured by mutating the in-memory record.
    """
    from backend.routers import agents as _agents_router  # noqa: PLC0415

    agent = _agents_router._agents.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")

    lane = _lanes.lane_for(agent)
    if lane not in (_lanes.LANE_ACTIVE, _lanes.LANE_AMBIENT):
        raise HTTPException(
            status_code=409,
            detail=f"agent not revocable from lane '{lane}'",
        )

    revoked = agent.model_copy(update={"status": AgentStatus.error})
    _agents_router._agents[agent_id] = revoked
    logger.info("bp.fleet.revoke agent=%s prev_lane=%s", agent_id, lane)
    return {
        "id": agent_id,
        "prev_lane": lane,
        "new_lane": _lanes.lane_for(revoked),
        "status": revoked.status.value,
    }

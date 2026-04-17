"""R2 (#308) — Semantic Entropy Monitor REST endpoints.

Surfaces per-agent cognitive-health snapshots for the Agent Matrix
Wall Cognitive Health card and the "Highest Entropy Agent" badge in
the Ops Summary panel.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend import semantic_entropy as se

router = APIRouter(prefix="/entropy", tags=["entropy"])


@router.get("/agents")
async def list_entropy() -> dict:
    """Return semantic-entropy snapshots for every tracked agent.

    Also exposes the current highest-entropy agent so the ops summary
    can render without re-scanning. Empty response is ``{"agents": [],
    "highest": null}`` — the UI treats an empty list as "monitor
    hasn't yet collected a measurement".
    """
    return {
        "agents": se.snapshot_all(),
        "highest": se.highest_entropy_agent(),
    }


@router.get("/agents/{agent_id}")
async def get_entropy(agent_id: str) -> dict:
    snap = se.snapshot_agent(agent_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Agent has no entropy snapshot yet")
    return snap

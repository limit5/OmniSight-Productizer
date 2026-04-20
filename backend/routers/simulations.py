"""Simulation results REST API."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from backend import db
from backend.db_pool import get_conn
from backend.models import SimulationRequest
from backend.routers import _pagination as _pg

# Phase-3 P6 (2026-04-20): prefix renamed /system/simulations →
# /runtime/simulations — see backend/routers/system.py for rationale.
router = APIRouter(prefix="/runtime/simulations", tags=["simulations"])


@router.get("")
async def list_simulations(
    task_id: str = "",
    agent_id: str = "",
    status: str = "",
    limit: int = _pg.Limit(default=50, max_cap=200),
    conn: asyncpg.Connection = Depends(get_conn),
):
    """List recent simulation runs with optional filters."""
    return await db.list_simulations(
        conn, task_id=task_id, agent_id=agent_id, status=status, limit=limit,
    )


@router.get("/{sim_id}")
async def get_simulation(
    sim_id: str,
    conn: asyncpg.Connection = Depends(get_conn),
):
    """Get a specific simulation result with full report."""
    sim = await db.get_simulation(conn, sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return sim


@router.post("")
async def trigger_simulation(req: SimulationRequest):
    """Manually trigger a simulation run (from UI or external API)."""
    from pathlib import Path

    from backend.agents.tools import run_simulation, set_active_workspace

    # Set workspace to project root so simulate.sh can find source files
    project_root = Path(__file__).resolve().parent.parent.parent
    set_active_workspace(project_root)
    try:
        result = await run_simulation.ainvoke({
            "track": req.track.value,
            "module": req.module,
            "input_data": req.input_data or "",
            "mock": req.mock,
            "platform": req.platform,
        })
    finally:
        set_active_workspace(None)
    return {"result": result}

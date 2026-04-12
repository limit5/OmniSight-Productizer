"""Simulation results REST API."""

from fastapi import APIRouter, HTTPException

from backend import db
from backend.models import SimulationRequest

router = APIRouter(prefix="/system/simulations", tags=["simulations"])


@router.get("")
async def list_simulations(
    task_id: str = "",
    agent_id: str = "",
    status: str = "",
    limit: int = 50,
):
    """List recent simulation runs with optional filters."""
    return await db.list_simulations(
        task_id=task_id, agent_id=agent_id, status=status, limit=limit
    )


@router.get("/{sim_id}")
async def get_simulation(sim_id: str):
    """Get a specific simulation result with full report."""
    sim = await db.get_simulation(sim_id)
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

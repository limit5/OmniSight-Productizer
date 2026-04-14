"""Phase 56 — workflow run inspection + resume API.

GET    /workflow/runs                  list runs (filter by status)
GET    /workflow/runs/{id}             replay (run + steps + in_flight)
GET    /workflow/in-flight             shortcut for status=running
POST   /workflow/runs/{id}/resume      mark + return; caller-side
                                        re-invocation re-uses cached steps
POST   /workflow/runs/{id}/finish      manually mark completed/failed
                                        (admin / debug)

The actual "resume" semantic lives in `backend.workflow.step` — when
the caller re-runs the original code path, each `@step(run, key)`
checks for a cached output before re-invoking. This router just gives
operators visibility + a way to flag a run as needing attention.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend import workflow as wf

router = APIRouter(prefix="/workflow", tags=["workflow"])


@router.get("/runs")
async def list_runs(status: str | None = None, limit: int = 50) -> dict:
    runs = await wf.list_runs(status=status, limit=limit)
    return {
        "runs": [
            {
                "id": r.id, "kind": r.kind, "status": r.status,
                "started_at": r.started_at, "completed_at": r.completed_at,
                "last_step_id": r.last_step_id, "metadata": r.metadata,
            }
            for r in runs
        ],
        "count": len(runs),
    }


@router.get("/in-flight")
async def list_in_flight() -> dict:
    """Convenience for the dashboard 'Resume in-flight runs?' banner."""
    runs = await wf.list_in_flight_on_startup()
    return {"runs": [
        {"id": r.id, "kind": r.kind, "started_at": r.started_at,
         "last_step_id": r.last_step_id, "metadata": r.metadata}
        for r in runs
    ], "count": len(runs)}


@router.get("/runs/{run_id}")
async def replay_run(run_id: str) -> dict:
    payload = await wf.replay(run_id)
    if not payload:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    return payload


@router.post("/runs/{run_id}/finish")
async def finish_run(run_id: str, status: str = "completed") -> dict:
    if status not in {"completed", "failed", "halted"}:
        raise HTTPException(status_code=422, detail="status must be completed|failed|halted")
    run = await wf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    await wf.finish(run_id, status=status)  # type: ignore[arg-type]
    return {"id": run_id, "status": status}

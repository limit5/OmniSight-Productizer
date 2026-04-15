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

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend import workflow as wf
from backend.routers import _pagination as _pg

router = APIRouter(prefix="/workflow", tags=["workflow"])


def _parse_if_match(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match header required")
    try:
        return int(if_match.strip('" '))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="If-Match must be an integer version")


@router.get("/runs")
async def list_runs(status: str | None = None, limit: int = _pg.Limit(default=50, max_cap=200)) -> dict:
    runs = await wf.list_runs(status=status, limit=limit)
    return {
        "runs": [
            {
                "id": r.id, "kind": r.kind, "status": r.status,
                "started_at": r.started_at, "completed_at": r.completed_at,
                "last_step_id": r.last_step_id, "metadata": r.metadata,
                "version": r.version,
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
async def finish_run(run_id: str, status: str = "completed",
                     if_match: str | None = Header(None, alias="If-Match")) -> dict:
    if status not in {"completed", "failed", "halted"}:
        raise HTTPException(status_code=422, detail="status must be completed|failed|halted")
    run = await wf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    expected = _parse_if_match(if_match) if if_match is not None else None
    try:
        await wf.finish(run_id, status=status, expected_version=expected)  # type: ignore[arg-type]
    except wf.VersionConflict:
        raise HTTPException(status_code=409, detail="version conflict — resource modified elsewhere")
    return {"id": run_id, "status": status}


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str,
                    if_match: str | None = Header(None, alias="If-Match")) -> dict:
    expected = _parse_if_match(if_match)
    run = await wf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    if run.status not in {"failed", "halted"}:
        raise HTTPException(status_code=400, detail="only failed/halted runs can be retried")
    try:
        updated = await wf.retry_run(run_id, expected)
    except wf.VersionConflict:
        raise HTTPException(status_code=409, detail="version conflict — resource modified elsewhere")
    return {"id": updated.id, "status": updated.status, "version": updated.version}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str,
                     if_match: str | None = Header(None, alias="If-Match")) -> dict:
    expected = _parse_if_match(if_match)
    run = await wf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    if run.status != "running":
        raise HTTPException(status_code=400, detail="only running runs can be cancelled")
    try:
        new_ver = await wf.cancel_run(run_id, expected)
    except wf.VersionConflict:
        raise HTTPException(status_code=409, detail="version conflict — resource modified elsewhere")
    return {"id": run_id, "status": "halted", "version": new_ver}


class _MetadataUpdate(BaseModel):
    metadata: dict


@router.patch("/runs/{run_id}")
async def update_run(run_id: str, body: _MetadataUpdate,
                     if_match: str | None = Header(None, alias="If-Match")) -> dict:
    expected = _parse_if_match(if_match)
    run = await wf.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"workflow run {run_id} not found")
    try:
        new_ver = await wf.update_run_metadata(run_id, expected, body.metadata)
    except wf.VersionConflict:
        raise HTTPException(status_code=409, detail="version conflict — resource modified elsewhere")
    return {"id": run_id, "version": new_ver}

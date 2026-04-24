"""Phase 61 — Project final-report endpoints.

POST /projects/{id}/report        trigger generation, return JSON
GET  /projects/{id}/report        last-built JSON (or build on demand)
GET  /projects/{id}/report.html   HTML render
GET  /projects/{id}/report.pdf    WeasyPrint render (falls back to .html
                                  if WeasyPrint isn't installed)
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from backend import optimistic_lock as _ol
from backend import project_report as pr
from backend import project_runs
from backend.routers import _pagination as _pg

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


# In-memory cache of last build per project_id (small & cheap).
_LAST: dict[str, pr.FinalReport] = {}


@router.get("/{project_id}/runs")
async def list_project_runs(
    project_id: str,
    limit: int = _pg.Limit(default=50, max_cap=200),
) -> dict:
    """B7 (#207): parent project_runs with materialised child workflow_runs.

    Q.7 #301: each item now carries ``version`` so the frontend can
    echo it back in ``If-Match`` when PATCHing the run's label /
    metadata.
    """
    items = await project_runs.list_by_project_with_children(project_id, limit)
    return {"project_runs": items, "count": len(items)}


class _ProjectRunPatch(BaseModel):
    """Q.7 #301 body for ``PATCH /projects/runs/{run_id}``.

    ``label`` is the operator-editable display name for a project run
    (rendered in the RunHistory collapsed parent). Both fields are
    optional — an empty body still bumps the version so cross-device
    echo still lands.
    """
    label: str | None = None


@router.patch("/runs/{project_run_id}")
async def patch_project_run(
    project_run_id: str,
    body: _ProjectRunPatch,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict:
    """Update a project run's label (operator rename flow).

    Q.7 #301 — requires ``If-Match: <version>`` header. Two operators
    renaming the same project run on different devices produce exactly
    one winner (post-bump version echoed) and one 409 (shaped body
    the frontend ``use409Conflict`` hook consumes).

    The ``PATCH /projects/{id}`` slot in the TODO mapped onto
    ``project_runs`` here because that is the only DB-backed
    "projects" entity today — the ``projects`` top-level table
    is a future item (see TODO Y4-era work). The endpoint shape
    (``PATCH /projects/runs/{id}``) leaves room for a future
    ``PATCH /projects/{project_id}`` without colliding.
    """
    expected_version = _ol.parse_if_match(if_match)
    from backend.db_pool import get_pool
    updates: dict[str, object] = {}
    if body.label is not None:
        updates["label"] = body.label

    async with get_pool().acquire() as conn:
        try:
            new_version = await _ol.bump_version_pg(
                conn,
                "project_runs",
                pk_col="id",
                pk_value=project_run_id,
                expected_version=expected_version,
                updates=updates,
            )
        except _ol.VersionConflict as conflict:
            if conflict.current_version is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"project_run {project_run_id} not found",
                )
            _ol.raise_conflict(
                conflict.current_version,
                conflict.your_version,
                resource="project_run",
            )

        row = await conn.fetchrow(
            "SELECT id, project_id, label, created_at, version "
            "FROM project_runs WHERE id = $1",
            project_run_id,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"project_run {project_run_id} not found",
        )
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "version": new_version,
    }


@router.post("/{project_id}/report")
async def build_report(project_id: str) -> dict:
    rep = await pr.build_report(project_id)
    _LAST[project_id] = rep
    return {"etag": pr.report_etag(rep), "report": rep.to_dict()}


@router.get("/{project_id}/report")
async def get_report(project_id: str) -> dict:
    rep = _LAST.get(project_id)
    if rep is None:
        rep = await pr.build_report(project_id)
        _LAST[project_id] = rep
    return {"etag": pr.report_etag(rep), "report": rep.to_dict()}


@router.get("/{project_id}/report.html", response_class=HTMLResponse)
async def get_report_html(project_id: str) -> HTMLResponse:
    rep = _LAST.get(project_id) or await pr.build_report(project_id)
    _LAST[project_id] = rep
    return HTMLResponse(content=pr.render_html(rep))


@router.get("/{project_id}/report.pdf")
async def get_report_pdf(project_id: str):
    rep = _LAST.get(project_id) or await pr.build_report(project_id)
    _LAST[project_id] = rep
    out = Path(tempfile.gettempdir()) / f"omnisight-report-{project_id}-{pr.report_etag(rep)}.pdf"
    ok, path = pr.render_pdf(rep, out)
    if ok:
        return FileResponse(path, media_type="application/pdf",
                            filename=f"{project_id}-final-report.pdf")
    # WeasyPrint missing or failed — return HTML fallback with a note
    return FileResponse(path, media_type="text/html",
                        filename=f"{project_id}-final-report.html",
                        headers={"X-Render-Fallback": "html"})

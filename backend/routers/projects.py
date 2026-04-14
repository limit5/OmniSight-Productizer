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

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from backend import project_report as pr

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


# In-memory cache of last build per project_id (small & cheap).
_LAST: dict[str, pr.FinalReport] = {}


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

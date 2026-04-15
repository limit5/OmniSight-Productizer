"""B3/REPORT-01 — Project report endpoints.

Endpoints:
  POST /report/generate      — build a project report from a workflow run
  GET  /report/{report_id}   — retrieve a cached report (markdown)
  GET  /report/{report_id}/pdf — download PDF version
  POST /report/share          — create a signed read-only URL
  GET  /report/share/{report_id} — access a shared report via signed URL
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend import report_generator as _rg

router = APIRouter(tags=["report"])

_report_cache: dict[str, tuple[_rg.ReportData, str]] = {}


class GenerateRequest(BaseModel):
    run_id: str
    title: str = "OmniSight Project Report"
    parsed_spec: dict[str, Any] | None = None


class ShareRequest(BaseModel):
    report_id: str
    base_url: str = ""
    expires_in: int = 86400


class ShareResponse(BaseModel):
    url: str
    expires_in: int


@router.post("/report/generate")
async def generate(req: GenerateRequest) -> dict[str, Any]:
    report = await _rg.generate_project_report(
        req.run_id,
        title=req.title,
        parsed_spec_dict=req.parsed_spec,
    )
    md = _rg.render_markdown(report)
    _report_cache[report.report_id] = (report, md)
    return {
        "report_id": report.report_id,
        "title": report.title,
        "generated_at": report.generated_at,
        "markdown": md,
    }


@router.get("/report/{report_id}")
async def get_report(report_id: str) -> dict[str, Any]:
    entry = _report_cache.get(report_id)
    if not entry:
        raise HTTPException(404, "Report not found")
    report, md = entry
    return {
        "report_id": report.report_id,
        "title": report.title,
        "generated_at": report.generated_at,
        "markdown": md,
    }


@router.get("/report/{report_id}/pdf")
async def get_pdf(report_id: str):
    from fastapi.responses import Response

    entry = _report_cache.get(report_id)
    if not entry:
        raise HTTPException(404, "Report not found")
    _, md = entry
    try:
        pdf_bytes = _rg.render_pdf(md)
    except ImportError as exc:
        raise HTTPException(501, str(exc))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report_id}.pdf"'},
    )


@router.post("/report/share")
async def share(req: ShareRequest) -> ShareResponse:
    if req.report_id not in _report_cache:
        raise HTTPException(404, "Report not found")
    url = _rg.generate_signed_url(
        req.base_url or "",
        req.report_id,
        expires_in=req.expires_in,
    )
    return ShareResponse(url=url, expires_in=req.expires_in)


@router.get("/report/share/{report_id}")
async def shared_report(
    report_id: str,
    expires: int = Query(...),
    sig: str = Query(...),
) -> dict[str, Any]:
    if not _rg.verify_signed_url(report_id, expires, sig):
        raise HTTPException(403, "Invalid or expired share link")
    entry = _report_cache.get(report_id)
    if not entry:
        raise HTTPException(404, "Report not found or expired")
    report, md = entry
    return {
        "report_id": report.report_id,
        "title": report.title,
        "generated_at": report.generated_at,
        "markdown": md,
    }

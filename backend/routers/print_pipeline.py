"""C20 — L4-CORE-20 Print pipeline endpoints (#241).

REST endpoints for IPP/CUPS backend, PDL interpreters, color management,
and print queue/spooler integration.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import print_pipeline as pp

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/printing", tags=["printing"])


# ── Request models ───────────────────────────────────────────────────

class IPPJobRequest(BaseModel):
    printer_uri: str = Field(..., description="Printer URI (e.g. ipp://printer.local/ipp/print)")
    document_format: str = Field(default="application/pdf", description="MIME type of document")
    attributes: dict[str, Any] = Field(default_factory=dict, description="IPP job attributes")
    document_size: int = Field(default=0, description="Document size in bytes")


class PCLGenerateRequest(BaseModel):
    page_size: str = Field(default="a4", description="Page size (a4, letter, a3, legal, b5)")
    resolution_dpi: int = Field(default=300, description="Resolution in DPI")
    copies: int = Field(default=1, description="Number of copies")
    duplex: str = Field(default="simplex", description="Duplex mode (simplex, duplex_long, duplex_short)")
    pages: int = Field(default=1, description="Number of pages")


class PSGenerateRequest(BaseModel):
    page_size: str = Field(default="a4", description="Page size")
    resolution_dpi: int = Field(default=300, description="Resolution in DPI")
    level: str = Field(default="level2", description="PostScript level (level1, level2, level3)")
    pages: int = Field(default=1, description="Number of pages")
    duplex: str = Field(default="simplex", description="Duplex mode")


class RasterRenderRequest(BaseModel):
    device: str = Field(default="pwgraster", description="Ghostscript output device")
    dpi: int = Field(default=300, description="Render resolution")
    page_size: str = Field(default="a4", description="Page size")
    color_bits: int = Field(default=24, description="Bits per pixel (8=grey, 24=RGB)")


class ProfileSelectRequest(BaseModel):
    paper_id: str = Field(..., description="Paper profile ID")
    ink_id: str = Field(..., description="Ink set ID")


class ICCGenerateRequest(BaseModel):
    paper_id: str = Field(..., description="Paper profile ID")
    ink_id: str = Field(..., description="Ink set ID")


class EnqueueRequest(BaseModel):
    document_name: str = Field(..., description="Document name")
    printer_uri: str = Field(..., description="Printer URI")
    priority: int = Field(default=50, description="Job priority (1-100)")
    size_bytes: int = Field(default=0, description="Document size in bytes")
    pages: int = Field(default=1, description="Page count")


class GateValidateRequest(BaseModel):
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs")
    required_domains: list[str] | None = Field(default=None, description="Required domains")


class CertGenerateRequest(BaseModel):
    domain: str = Field(default="all", description="Domain (ipp_cups, pdl_interpreters, color_management, print_queue, all)")


# ── IPP / CUPS endpoints ────────────────────────────────────────────

@router.get("/ipp/operations")
async def get_ipp_operations(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(o) for o in pp.list_ipp_operations()]


@router.get("/ipp/operations/{op_id}")
async def get_ipp_operation(
    op_id: str,
    _user: dict = Depends(_require),
) -> dict:
    op = pp.get_ipp_operation(op_id)
    if op is None:
        raise HTTPException(404, f"IPP operation not found: {op_id}")
    return asdict(op)


@router.get("/ipp/attributes")
async def get_ipp_attributes(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(a) for a in pp.list_ipp_attributes()]


@router.get("/cups/backends")
async def get_cups_backends(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(b) for b in pp.list_cups_backends()]


@router.get("/cups/backends/{backend_id}")
async def get_cups_backend(
    backend_id: str,
    _user: dict = Depends(_require),
) -> dict:
    b = pp.get_cups_backend(backend_id)
    if b is None:
        raise HTTPException(404, f"CUPS backend not found: {backend_id}")
    return asdict(b)


@router.get("/ipp/job-states")
async def get_ipp_job_states(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in pp.list_ipp_job_states()]


@router.post("/ipp/jobs")
async def submit_ipp_job(
    req: IPPJobRequest,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.submit_ipp_job(
            printer_uri=req.printer_uri,
            document_format=req.document_format,
            attributes=req.attributes,
        )
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/ipp/jobs")
async def list_ipp_jobs(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(j) for j in pp.list_ipp_jobs()]


@router.get("/ipp/jobs/{job_id}")
async def get_ipp_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    job = pp.get_ipp_job(job_id)
    if job is None:
        raise HTTPException(404, f"IPP job not found: {job_id}")
    return asdict(job)


@router.post("/ipp/jobs/{job_id}/cancel")
async def cancel_ipp_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.cancel_ipp_job(job_id)
        if job is None:
            raise HTTPException(404, f"IPP job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ipp/jobs/{job_id}/hold")
async def hold_ipp_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.hold_ipp_job(job_id)
        if job is None:
            raise HTTPException(404, f"IPP job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/ipp/jobs/{job_id}/release")
async def release_ipp_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.release_ipp_job(job_id)
        if job is None:
            raise HTTPException(404, f"IPP job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── PDL interpreter endpoints ───────────────────────────────────────

@router.get("/pdl/languages")
async def get_pdl_languages(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(l) for l in pp.list_pdl_languages()]


@router.get("/pdl/languages/{lang_id}")
async def get_pdl_language(
    lang_id: str,
    _user: dict = Depends(_require),
) -> dict:
    lang = pp.get_pdl_language(lang_id)
    if lang is None:
        raise HTTPException(404, f"PDL language not found: {lang_id}")
    return asdict(lang)


@router.get("/pdl/pcl/commands")
async def get_pcl_commands(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(c) for c in pp.list_pcl_commands()]


@router.get("/pdl/ps/operators")
async def get_ps_operators(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(o) for o in pp.list_ps_operators()]


@router.get("/pdl/ghostscript/devices")
async def get_ghostscript_devices(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(d) for d in pp.list_ghostscript_devices()]


@router.get("/pdl/ghostscript/devices/{device_id}")
async def get_ghostscript_device(
    device_id: str,
    _user: dict = Depends(_require),
) -> dict:
    d = pp.get_ghostscript_device(device_id)
    if d is None:
        raise HTTPException(404, f"Ghostscript device not found: {device_id}")
    return asdict(d)


@router.get("/pdl/raster-formats")
async def get_raster_formats(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(f) for f in pp.list_raster_formats()]


@router.post("/pdl/pcl/generate")
async def generate_pcl(
    req: PCLGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = pp.generate_pcl(
        page_size=req.page_size,
        resolution_dpi=req.resolution_dpi,
        copies=req.copies,
        duplex=req.duplex,
        pages=req.pages,
    )
    return {
        "page_count": result.page_count,
        "resolution_dpi": result.resolution_dpi,
        "page_size": result.page_size,
        "duplex": result.duplex,
        "data_size": len(result.data),
        "checksum": result.checksum,
    }


@router.post("/pdl/ps/generate")
async def generate_postscript(
    req: PSGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = pp.generate_postscript(
        page_size=req.page_size,
        resolution_dpi=req.resolution_dpi,
        level=req.level,
        pages=req.pages,
        duplex=req.duplex,
    )
    return {
        "page_count": result.page_count,
        "dsc_compliant": result.dsc_compliant,
        "level": result.level,
        "bounding_box": result.bounding_box,
        "data_size": len(result.data),
        "checksum": result.checksum,
    }


@router.post("/pdl/render")
async def render_pdf_to_raster(
    req: RasterRenderRequest,
    _user: dict = Depends(_require),
) -> dict:
    try:
        result = pp.render_pdf_to_raster(
            device=req.device,
            dpi=req.dpi,
            page_size=req.page_size,
            color_bits=req.color_bits,
        )
        return {
            "width": result.width,
            "height": result.height,
            "dpi": result.dpi,
            "color_space": result.color_space,
            "bits_per_pixel": result.bits_per_pixel,
            "page_count": result.page_count,
            "device": result.device,
            "data_size": len(result.data),
            "checksum": result.checksum,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── Color management endpoints ──────────────────────────────────────

@router.get("/color/papers")
async def get_paper_profiles(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in pp.list_paper_profiles()]


@router.get("/color/papers/{paper_id}")
async def get_paper_profile(
    paper_id: str,
    _user: dict = Depends(_require),
) -> dict:
    p = pp.get_paper_profile(paper_id)
    if p is None:
        raise HTTPException(404, f"Paper profile not found: {paper_id}")
    return asdict(p)


@router.get("/color/inks")
async def get_ink_sets(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(i) for i in pp.list_ink_sets()]


@router.get("/color/inks/{ink_id}")
async def get_ink_set(
    ink_id: str,
    _user: dict = Depends(_require),
) -> dict:
    i = pp.get_ink_set(ink_id)
    if i is None:
        raise HTTPException(404, f"Ink set not found: {ink_id}")
    return asdict(i)


@router.get("/color/rendering-intents")
async def get_rendering_intents(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(i) for i in pp.list_print_rendering_intents()]


@router.get("/color/spaces")
async def get_color_spaces(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in pp.list_color_spaces()]


@router.post("/color/select")
async def select_profile(
    req: ProfileSelectRequest,
    _user: dict = Depends(_require),
) -> dict:
    try:
        sel = pp.select_print_profile(req.paper_id, req.ink_id)
        return asdict(sel)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/color/icc/generate")
async def generate_icc(
    req: ICCGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    try:
        data = pp.generate_print_icc_binary(req.paper_id, req.ink_id)
        return {
            "paper_id": req.paper_id,
            "ink_id": req.ink_id,
            "size_bytes": len(data),
            "checksum": __import__("hashlib").sha256(data).hexdigest(),
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── Print queue endpoints ───────────────────────────────────────────

@router.get("/queue/policies")
async def get_queue_policies(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(p) for p in pp.list_queue_policies()]


@router.get("/queue/priorities")
async def get_priority_levels(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(l) for l in pp.list_priority_levels()]


@router.get("/queue/config")
async def get_spooler_config(
    _user: dict = Depends(_require),
) -> dict:
    return asdict(pp.get_spooler_config())


@router.get("/queue/lifecycle")
async def get_lifecycle_states(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in pp.list_job_lifecycle_states()]


@router.post("/queue/jobs")
async def enqueue_job(
    req: EnqueueRequest,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.enqueue_print_job(
            document_name=req.document_name,
            printer_uri=req.printer_uri,
            priority=req.priority,
            size_bytes=req.size_bytes,
            pages=req.pages,
        )
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/queue/jobs")
async def list_queue_jobs(
    policy: str = "fifo",
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(j) for j in pp.list_queue_jobs(policy)]


@router.get("/queue/jobs/{job_id}")
async def get_queue_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    job = pp.get_queue_job(job_id)
    if job is None:
        raise HTTPException(404, f"Queue job not found: {job_id}")
    return asdict(job)


@router.post("/queue/jobs/{job_id}/hold")
async def hold_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.hold_queue_job(job_id)
        if job is None:
            raise HTTPException(404, f"Queue job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/queue/jobs/{job_id}/release")
async def release_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.release_queue_job(job_id)
        if job is None:
            raise HTTPException(404, f"Queue job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/queue/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        job = pp.cancel_queue_job(job_id)
        if job is None:
            raise HTTPException(404, f"Queue job not found: {job_id}")
        return asdict(job)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/queue/jobs/{job_id}/complete")
async def complete_job(
    job_id: str,
    _user: dict = Depends(_require),
) -> dict:
    job = pp.advance_queue_job_to_completion(job_id)
    if job is None:
        raise HTTPException(404, f"Queue job not found: {job_id}")
    return asdict(job)


# ── Test recipes, SoCs, artifacts, certs ────────────────────────────

@router.get("/test-recipes")
async def get_test_recipes(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(r) for r in pp.list_test_recipes()]


@router.get("/test-recipes/{recipe_id}")
async def get_test_recipe(
    recipe_id: str,
    _user: dict = Depends(_require),
) -> dict:
    r = pp.get_test_recipe(recipe_id)
    if r is None:
        raise HTTPException(404, f"Test recipe not found: {recipe_id}")
    return asdict(r)


@router.post("/test-recipes/{recipe_id}/run")
async def run_test_recipe(
    recipe_id: str,
    _user: dict = Depends(_require),
) -> dict:
    try:
        result = pp.run_test_recipe(recipe_id)
        return asdict(result)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/socs")
async def get_compatible_socs(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(s) for s in pp.list_compatible_socs()]


@router.get("/artifacts")
async def get_artifact_definitions(
    _user: dict = Depends(_require),
) -> list[dict]:
    return [asdict(a) for a in pp.list_artifact_definitions()]


@router.post("/validate")
async def validate_gate(
    req: GateValidateRequest,
    _user: dict = Depends(_require),
) -> dict:
    result = pp.validate_print_gate(req.artifacts, req.required_domains)
    return asdict(result)


@router.get("/certs")
async def get_certs(
    _user: dict = Depends(_require),
) -> list[dict]:
    return pp.get_print_certs()


@router.post("/certs/generate")
async def generate_certs(
    req: CertGenerateRequest,
    _user: dict = Depends(_require),
) -> dict:
    return pp.generate_cert_artifacts(req.domain)

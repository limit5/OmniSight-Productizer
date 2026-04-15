"""D1 — SKILL-UVC: UVC gadget management endpoints (#218).

REST endpoints for UVC gadget lifecycle, stream control, still image
capture, extension unit access, and compliance checking.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import uvc_gadget as uvc

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/uvc-gadget", tags=["uvc-gadget"])

_manager: uvc.UVCGadgetManager | None = None


def _get_manager() -> uvc.UVCGadgetManager:
    global _manager
    if _manager is None:
        _manager = uvc.UVCGadgetManager()
    return _manager


# ── Request models ───────────────────────────────────────────────────


class CreateGadgetRequest(BaseModel):
    gadget_name: str = Field(default="g_uvc", description="ConfigFS gadget name")
    vendor_id: int = Field(default=0x1D6B, description="USB Vendor ID")
    product_id: int = Field(default=0x0104, description="USB Product ID")
    manufacturer: str = Field(default="OmniSight", description="Manufacturer string")
    product: str = Field(default="UVC Camera", description="Product string")
    serial: str = Field(default="000000000001", description="Serial number")
    max_power: int = Field(default=500, description="Max power (mA)")
    formats: list[str] = Field(
        default=["h264", "mjpeg", "yuy2"],
        description="Stream formats to advertise",
    )


class BindUdcRequest(BaseModel):
    udc_name: str = Field(default="", description="UDC name (auto-detect if empty)")


class StartStreamRequest(BaseModel):
    format: str = Field(default="h264", description="Stream format (h264, mjpeg, yuy2)")
    width: int = Field(default=1920, description="Frame width")
    height: int = Field(default=1080, description="Frame height")
    fps: int = Field(default=30, description="Frame rate")


class XUSetRequest(BaseModel):
    selector: int = Field(..., description="XU control selector")
    value: int = Field(..., description="Value to set")


# ── Query endpoints ──────────────────────────────────────────────────


@router.get("/formats", dependencies=[Depends(_require)])
async def get_formats() -> list[dict[str, Any]]:
    return uvc.list_stream_formats()


@router.get("/resolutions", dependencies=[Depends(_require)])
async def get_resolutions(format_id: str | None = None) -> list[dict[str, Any]]:
    return uvc.list_resolutions(format_id)


@router.get("/xu-controls", dependencies=[Depends(_require)])
async def get_xu_controls() -> list[dict[str, Any]]:
    return uvc.list_xu_controls()


# ── Gadget lifecycle ─────────────────────────────────────────────────


@router.post("/create", dependencies=[Depends(_require)])
async def create_gadget(req: CreateGadgetRequest) -> dict[str, Any]:
    formats = []
    for f in req.formats:
        try:
            formats.append(uvc.StreamFormat(f))
        except ValueError:
            raise HTTPException(400, f"Unknown format: {f}")

    config = uvc.GadgetConfig(
        gadget_name=req.gadget_name,
        vendor_id=req.vendor_id,
        product_id=req.product_id,
        manufacturer=req.manufacturer,
        product=req.product,
        serial=req.serial,
        max_power=req.max_power,
        formats=formats,
    )
    global _manager
    _manager = uvc.UVCGadgetManager(config)
    if not _manager.create_gadget():
        raise HTTPException(500, "Failed to create gadget")
    return _manager.get_status()


@router.post("/bind", dependencies=[Depends(_require)])
async def bind_udc(req: BindUdcRequest) -> dict[str, Any]:
    mgr = _get_manager()
    if not mgr.bind_udc(req.udc_name):
        raise HTTPException(500, "Failed to bind UDC")
    return mgr.get_status()


@router.post("/unbind", dependencies=[Depends(_require)])
async def unbind_udc() -> dict[str, Any]:
    mgr = _get_manager()
    if not mgr.unbind_udc():
        raise HTTPException(500, "Failed to unbind UDC")
    return mgr.get_status()


@router.post("/destroy", dependencies=[Depends(_require)])
async def destroy_gadget() -> dict[str, Any]:
    mgr = _get_manager()
    if not mgr.destroy_gadget():
        raise HTTPException(500, "Failed to destroy gadget")
    return mgr.get_status()


@router.get("/status", dependencies=[Depends(_require)])
async def get_status() -> dict[str, Any]:
    return _get_manager().get_status()


# ── Stream control ───────────────────────────────────────────────────


@router.post("/stream/start", dependencies=[Depends(_require)])
async def start_stream(req: StartStreamRequest) -> dict[str, Any]:
    mgr = _get_manager()
    try:
        fmt = uvc.StreamFormat(req.format)
    except ValueError:
        raise HTTPException(400, f"Unknown format: {req.format}")
    if not mgr.start_stream(fmt, req.width, req.height, req.fps):
        raise HTTPException(500, f"Cannot start stream (state: {mgr.state.value})")
    return mgr.get_status()


@router.post("/stream/stop", dependencies=[Depends(_require)])
async def stop_stream() -> dict[str, Any]:
    mgr = _get_manager()
    if not mgr.stop_stream():
        raise HTTPException(500, "Cannot stop stream")
    return mgr.get_status()


# ── Still image ──────────────────────────────────────────────────────


@router.post("/still/capture", dependencies=[Depends(_require)])
async def capture_still() -> dict[str, Any]:
    mgr = _get_manager()
    capture = mgr.capture_still()
    if not capture.path:
        raise HTTPException(500, "Cannot capture still image")
    return asdict(capture)


# ── Extension unit ───────────────────────────────────────────────────


@router.get("/xu/{selector}", dependencies=[Depends(_require)])
async def xu_get(selector: int) -> dict[str, Any]:
    mgr = _get_manager()
    try:
        value = mgr.xu_get(selector)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"selector": selector, "value": value}


@router.post("/xu", dependencies=[Depends(_require)])
async def xu_set(req: XUSetRequest) -> dict[str, Any]:
    mgr = _get_manager()
    try:
        mgr.xu_set(req.selector, req.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"selector": req.selector, "value": req.value, "ok": True}


# ── Compliance ───────────────────────────────────────────────────────


@router.get("/compliance", dependencies=[Depends(_require)])
async def run_compliance() -> dict[str, Any]:
    mgr = _get_manager()
    report = uvc.run_compliance_check(mgr)
    return {
        "gadget_name": report.gadget_name,
        "timestamp": report.timestamp,
        "all_passed": report.all_passed,
        "pass_count": report.pass_count,
        "fail_count": report.fail_count,
        "results": [asdict(r) for r in report.results],
    }


# ── Descriptor tree ──────────────────────────────────────────────────


@router.get("/descriptors", dependencies=[Depends(_require)])
async def get_descriptors() -> dict[str, Any]:
    mgr = _get_manager()
    tree = mgr.descriptor_tree
    if tree is None:
        return {"tree": None, "validation_errors": ["Gadget not created yet"]}
    errors = uvc.validate_descriptors(tree)
    return {
        "tree": {
            "camera_terminal": asdict(tree.camera_terminal),
            "processing_unit": asdict(tree.processing_unit),
            "output_terminal": asdict(tree.output_terminal),
            "extension_unit": {
                "unit_id": tree.extension_unit.unit_id,
                "num_controls": tree.extension_unit.num_controls,
                "controls": [asdict(c) for c in tree.extension_unit.controls],
            },
            "formats": [
                {
                    "format_id": f.format_id.value,
                    "bits_per_pixel": f.bits_per_pixel,
                    "frames": [asdict(fr) for fr in f.frames],
                }
                for f in tree.formats
            ],
            "still_image": asdict(tree.still_image),
        },
        "validation_errors": errors,
    }

"""C22 — L4-CORE-22 Barcode/scanning SDK abstraction endpoints (#243).

REST endpoints for barcode scanner management, symbology queries,
decode operations, frame sample validation, and test recipes.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import barcode_scanner as bs

_require = _au.require_operator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/barcode", tags=["barcode"])


# ── Request models ───────────────────────────────────────────────────

class ScanRequest(BaseModel):
    vendor_id: str = Field(..., description="Vendor adapter ID (zebra_snapi, honeywell, datalogic, newland)")
    frame_base64: str = Field(..., description="Base64-encoded frame data")
    symbology_filter: list[str] | None = Field(default=None, description="Optional symbology filter list")
    decode_mode: str = Field(default="api", description="Decode mode (hid_wedge, spp, api)")
    prefix: str = Field(default="", description="Output prefix (HID/SPP modes)")
    suffix: str = Field(default="", description="Output suffix (HID/SPP modes)")


class ValidateSampleRequest(BaseModel):
    sample_id: str = Field(..., description="Frame sample ID")
    vendor_id: str = Field(default="zebra_snapi", description="Vendor adapter ID")


class ValidateSymbologyRequest(BaseModel):
    symbology: str = Field(..., description="Symbology ID")
    data: str = Field(..., description="Data to validate")


class RunRecipeRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID")


# ── Vendor endpoints ─────────────────────────────────────────────────

@router.get("/vendors", dependencies=[Depends(_require)])
async def get_vendors() -> list[dict[str, Any]]:
    vendors = bs.list_vendors()
    return [asdict(v) for v in vendors]


@router.get("/vendors/{vendor_id}/capabilities", dependencies=[Depends(_require)])
async def get_vendor_capabilities(vendor_id: str) -> dict[str, Any]:
    try:
        scanner = bs.create_scanner(vendor_id)
        return scanner.get_capabilities()
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown vendor: {vendor_id}")


# ── Symbology endpoints ─────────────────────────────────────────────

@router.get("/symbologies", dependencies=[Depends(_require)])
async def get_symbologies(category: str | None = None) -> list[dict[str, Any]]:
    return bs.list_symbologies(category=category)


@router.post("/symbologies/validate", dependencies=[Depends(_require)])
async def validate_symbology(req: ValidateSymbologyRequest) -> dict[str, Any]:
    valid, message = bs.validate_symbology_data(req.symbology, req.data)
    return {"symbology": req.symbology, "data": req.data, "valid": valid, "message": message}


# ── Decode mode endpoints ────────────────────────────────────────────

@router.get("/decode-modes", dependencies=[Depends(_require)])
async def get_decode_modes() -> list[dict[str, Any]]:
    return bs.list_decode_modes()


# ── Scan / decode endpoints ──────────────────────────────────────────

@router.post("/scan", dependencies=[Depends(_require)])
async def scan(req: ScanRequest) -> dict[str, Any]:
    try:
        frame_data = base64.b64decode(req.frame_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 frame data")

    try:
        config = bs.ScannerConfig(
            vendor_id=req.vendor_id,
            decode_mode=req.decode_mode,
            enabled_symbologies=req.symbology_filter or [],
            prefix=req.prefix,
            suffix=req.suffix,
        )
        scanner = bs.create_scanner(req.vendor_id, config)
        scanner.connect()
        scanner.configure(config)
        result = scanner.scan(frame_data)
        scanner.disconnect()
        return asdict(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Frame sample endpoints ───────────────────────────────────────────

@router.get("/frame-samples", dependencies=[Depends(_require)])
async def get_frame_samples() -> list[dict[str, Any]]:
    return bs.list_frame_samples()


@router.get("/frame-samples/{sample_id}", dependencies=[Depends(_require)])
async def get_frame_sample(sample_id: str) -> dict[str, Any]:
    try:
        frame, meta = bs.generate_frame_sample(sample_id)
        meta["frame_base64"] = base64.b64encode(frame).decode("ascii")
        return meta
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Unknown sample: {sample_id}")


@router.post("/frame-samples/validate", dependencies=[Depends(_require)])
async def validate_sample(req: ValidateSampleRequest) -> dict[str, Any]:
    try:
        return bs.validate_frame_sample(req.sample_id, req.vendor_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Test recipe endpoints ────────────────────────────────────────────

@router.get("/test-recipes", dependencies=[Depends(_require)])
async def get_test_recipes() -> list[dict[str, Any]]:
    return bs.list_test_recipes()


@router.post("/test-recipes/run", dependencies=[Depends(_require)])
async def run_recipe(req: RunRecipeRequest) -> dict[str, Any]:
    result = bs.run_test_recipe(req.recipe_id)
    return asdict(result)


# ── Artifact & gate endpoints ────────────────────────────────────────

@router.get("/artifacts", dependencies=[Depends(_require)])
async def get_artifacts() -> list[dict[str, Any]]:
    return bs.list_artifacts()


@router.post("/gate/validate", dependencies=[Depends(_require)])
async def validate_gate() -> dict[str, Any]:
    return bs.validate_gate()

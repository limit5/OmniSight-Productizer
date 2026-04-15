"""C10 — L4-CORE-10 Radio certification pre-compliance endpoints (#224).

REST endpoints for radio certification standard lookup, test recipe queries,
emissions test execution, SAR upload, checklist validation, and artifact
generation.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import radio_compliance as rc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/radio", tags=["radio"])


class EmissionsTestRequest(BaseModel):
    region_id: str = Field(..., description="Region key (fcc, ce_red, ncc_lpd, srrc_srd)")
    recipe_id: str = Field(..., description="Test recipe ID")
    device_target: str = Field(..., description="Device under test identifier")
    timeout_s: int = Field(default=600, description="Timeout in seconds")


class SARUploadRequest(BaseModel):
    region_id: str = Field(..., description="Region key")
    file_path: str = Field(..., description="Path to SAR result file")
    peak_sar_w_kg: float = Field(default=0.0, description="Peak SAR value (0 = auto-extract)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChecklistRequest(BaseModel):
    target_regions: list[str] = Field(..., description="List of region IDs to validate")
    radio: dict[str, Any] = Field(default_factory=dict, description="Radio spec details")
    provided_artifacts: list[str] = Field(default_factory=list, description="Already-provided artifact IDs")


class ArtifactGenRequest(BaseModel):
    region_id: str = Field(..., description="Region key")
    provided_artifacts: list[str] = Field(default_factory=list)


@router.get("/regions")
async def list_regions(_user=Depends(_au.require_operator)) -> dict:
    regions = rc.list_regions()
    items = []
    for r in regions:
        items.append({
            "region_id": r.region_id,
            "name": r.name,
            "authority": r.authority,
            "region": r.region,
            "description": r.description,
            "recipe_count": len(r.test_recipes),
            "required_artifacts": r.required_artifacts,
        })
    return {"items": items, "count": len(items)}


@router.get("/regions/{region_id}")
async def get_region(region_id: str, _user=Depends(_au.require_operator)) -> dict:
    reg = rc.get_region(region_id)
    if reg is None:
        raise HTTPException(status_code=404, detail=f"Radio region {region_id!r} not found")
    return {
        "region_id": reg.region_id,
        "name": reg.name,
        "authority": reg.authority,
        "region": reg.region,
        "description": reg.description,
        "test_recipes": [r.to_dict() for r in reg.test_recipes],
        "required_artifacts": reg.required_artifacts,
    }


@router.get("/regions/{region_id}/recipes")
async def get_recipes(region_id: str, _user=Depends(_au.require_operator)) -> dict:
    recipes = rc.get_test_recipes(region_id)
    if not recipes:
        raise HTTPException(status_code=404, detail=f"No recipes for region {region_id!r}")
    return {
        "items": [r.to_dict() for r in recipes],
        "count": len(recipes),
    }


@router.get("/artifacts")
async def list_artifacts(_user=Depends(_au.require_operator)) -> dict:
    arts = rc.list_artifact_definitions()
    items = [
        {
            "artifact_id": a.artifact_id,
            "name": a.name,
            "description": a.description,
            "file_pattern": a.file_pattern,
        }
        for a in arts
    ]
    return {"items": items, "count": len(items)}


@router.post("/test/emissions")
async def run_emissions_test(
    req: EmissionsTestRequest,
    _user=Depends(_au.require_admin),
) -> dict:
    result = rc.run_emissions_test(
        req.region_id, req.recipe_id, req.device_target,
        timeout_s=req.timeout_s,
    )
    await rc.log_radio_test_result(result)
    return result.to_dict()


@router.post("/test/sar")
async def upload_sar(
    req: SARUploadRequest,
    _user=Depends(_au.require_admin),
) -> dict:
    result = rc.upload_sar_result(
        req.region_id, req.file_path,
        peak_sar_w_kg=req.peak_sar_w_kg,
        metadata=req.metadata,
    )
    await rc.log_radio_test_result(result)
    return result.to_dict()


@router.post("/checklist")
async def validate_checklist(
    req: ChecklistRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    spec = {
        "target_regions": req.target_regions,
        "radio": req.radio,
        "provided_artifacts": req.provided_artifacts,
    }
    checklists = rc.validate_radio_checklist(spec)
    return {
        "checklists": [c.to_dict() for c in checklists],
        "count": len(checklists),
        "all_complete": all(c.complete for c in checklists),
    }


@router.post("/artifacts/generate")
async def generate_artifacts(
    req: ArtifactGenRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    spec = {"provided_artifacts": req.provided_artifacts}
    artifacts = rc.generate_cert_artifacts(req.region_id, spec=spec)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Region {req.region_id!r} not found")
    return {
        "items": [a.to_dict() for a in artifacts],
        "count": len(artifacts),
        "region": req.region_id,
    }

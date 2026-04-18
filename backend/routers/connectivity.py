"""C13 — L4-CORE-13 Connectivity sub-skill library endpoints (#227).

REST endpoints for connectivity protocol lookup, test recipe queries,
connectivity test execution, sub-skill registry, composition resolution,
checklist validation, and artifact generation.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import connectivity as conn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connectivity", tags=["connectivity"])


class ConnTestRequest(BaseModel):
    protocol_id: str = Field(..., description="Protocol key (ble, wifi, fiveg, ethernet, can, modbus, opcua)")
    recipe_id: str = Field(..., description="Test recipe ID")
    target_device: str = Field(..., description="Target device identifier")
    timeout_s: int = Field(default=600, description="Timeout in seconds")


class ChecklistRequest(BaseModel):
    target_protocols: list[str] = Field(..., description="List of protocol IDs to validate")
    provided_artifacts: list[str] = Field(default_factory=list, description="Already-provided artifact IDs")


class ArtifactGenRequest(BaseModel):
    protocol_id: str = Field(..., description="Protocol key")
    provided_artifacts: list[str] = Field(default_factory=list)


class CompositionRequest(BaseModel):
    product_type: str = Field(..., description="Product type (e.g. 'Industrial gateway', 'IoT gateway')")


class SocCompatRequest(BaseModel):
    soc_id: str = Field(..., description="SoC identifier")
    protocol_ids: list[str] = Field(default_factory=list, description="Protocol IDs to check (empty = all)")


# -- Protocol endpoints --

@router.get("/protocols")
async def list_protocols(_user=Depends(_au.require_operator)) -> dict:
    protocols = conn.list_protocols()
    items = []
    for p in protocols:
        items.append({
            "protocol_id": p.protocol_id,
            "name": p.name,
            "standard": p.standard,
            "authority": p.authority,
            "description": p.description,
            "transport": p.transport,
            "layer": p.layer,
            "feature_count": len(p.features),
            "recipe_count": len(p.test_recipes),
            "required_artifacts": p.required_artifacts,
            "compatible_socs": p.compatible_socs,
        })
    return {"items": items, "count": len(items)}


@router.get("/protocols/{protocol_id}")
async def get_protocol(protocol_id: str, _user=Depends(_au.require_operator)) -> dict:
    proto = conn.get_protocol(protocol_id)
    if proto is None:
        raise HTTPException(status_code=404, detail=f"Protocol {protocol_id!r} not found")
    return proto.to_dict()


@router.get("/protocols/{protocol_id}/recipes")
async def get_recipes(protocol_id: str, _user=Depends(_au.require_operator)) -> dict:
    recipes = conn.get_test_recipes(protocol_id)
    if not recipes:
        raise HTTPException(status_code=404, detail=f"No recipes for protocol {protocol_id!r}")
    return {
        "items": [r.to_dict() for r in recipes],
        "count": len(recipes),
    }


@router.get("/protocols/{protocol_id}/features")
async def get_features(protocol_id: str, _user=Depends(_au.require_operator)) -> dict:
    features = conn.get_protocol_features(protocol_id)
    if not features:
        raise HTTPException(status_code=404, detail=f"Protocol {protocol_id!r} not found")
    return {"protocol_id": protocol_id, "features": features, "count": len(features)}


# -- Artifact endpoints --

@router.get("/artifacts")
async def list_artifacts(_user=Depends(_au.require_operator)) -> dict:
    arts = conn.list_artifact_definitions()
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


@router.post("/artifacts/generate")
async def generate_artifacts(
    req: ArtifactGenRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    spec = {"provided_artifacts": req.provided_artifacts}
    artifacts = conn.generate_cert_artifacts(req.protocol_id, spec=spec)
    if not artifacts:
        raise HTTPException(status_code=404, detail=f"Protocol {req.protocol_id!r} not found")
    return {
        "items": [a.to_dict() for a in artifacts],
        "count": len(artifacts),
        "protocol": req.protocol_id,
    }


# -- Test endpoints --

@router.post("/test")
async def run_connectivity_test(
    req: ConnTestRequest,
    _user=Depends(_au.require_admin),
) -> dict:
    result = conn.run_connectivity_test(
        req.protocol_id, req.recipe_id, req.target_device,
        timeout_s=req.timeout_s,
    )
    await conn.log_connectivity_test_result(result)
    return result.to_dict()


# -- Checklist --

@router.post("/checklist")
async def validate_checklist(
    req: ChecklistRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    spec = {
        "target_protocols": req.target_protocols,
        "provided_artifacts": req.provided_artifacts,
    }
    checklists = conn.validate_connectivity_checklist(spec)
    return {
        "checklists": [c.to_dict() for c in checklists],
        "count": len(checklists),
        "all_complete": all(c.complete for c in checklists),
    }


# -- Sub-skill registry --

@router.get("/sub-skills")
async def list_sub_skills(_user=Depends(_au.require_operator)) -> dict:
    skills = conn.list_sub_skills()
    return {
        "items": [s.to_dict() for s in skills],
        "count": len(skills),
    }


@router.get("/sub-skills/{sub_skill_id}")
async def get_sub_skill(sub_skill_id: str, _user=Depends(_au.require_operator)) -> dict:
    ss = conn.get_sub_skill(sub_skill_id)
    if ss is None:
        raise HTTPException(status_code=404, detail=f"Sub-skill {sub_skill_id!r} not found")
    return ss.to_dict()


# -- Composition --

@router.get("/composition/rules")
async def list_composition_rules(_user=Depends(_au.require_operator)) -> dict:
    rules = conn.list_composition_rules()
    return {
        "items": [r.to_dict() for r in rules],
        "count": len(rules),
    }


@router.post("/composition/resolve")
async def resolve_composition(
    req: CompositionRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    result = conn.resolve_composition(req.product_type)
    return result.to_dict()


# -- SoC compatibility --

@router.post("/soc-compat")
async def check_soc_compatibility(
    req: SocCompatRequest,
    _user=Depends(_au.require_operator),
) -> dict:
    result = conn.check_soc_compatibility(
        req.soc_id,
        protocol_ids=req.protocol_ids or None,
    )
    return {
        "soc_id": req.soc_id,
        "compatibility": result,
        "supported_count": sum(1 for v in result.values() if v),
        "total_checked": len(result),
    }

"""C16 — L4-CORE-16 OTA framework endpoints (#230).

REST endpoints for A/B slot management, delta update operations, rollback
evaluation, firmware signature verification, update manifest management,
phased rollout evaluation, and OTA test execution.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import ota_framework as ota

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ota", tags=["ota"])


class SlotSwitchRequest(BaseModel):
    scheme_id: str = Field(..., description="A/B slot scheme ID")
    target_slot: str = Field(default="B", description="Target slot to activate (A or B)")


class DeltaGenerateRequest(BaseModel):
    engine_id: str = Field(..., description="Delta engine ID (bsdiff, zchunk, rauc)")
    old_image_path: str = Field(..., description="Path to old firmware image")
    new_image_path: str = Field(..., description="Path to new firmware image")
    patch_output_path: str = Field(default="", description="Output path for delta patch")


class DeltaApplyRequest(BaseModel):
    engine_id: str = Field(..., description="Delta engine ID")
    old_image_path: str = Field(..., description="Path to old firmware image")
    patch_path: str = Field(..., description="Path to delta patch file")
    output_path: str = Field(default="", description="Output path for patched image")


class FirmwareSignRequest(BaseModel):
    scheme_id: str = Field(..., description="Signature scheme ID (ed25519_direct, x509_chain, mcuboot_ecdsa)")
    image_path: str = Field(..., description="Path to firmware image")
    key_path: str = Field(default="", description="Path to signing key")


class FirmwareVerifyRequest(BaseModel):
    scheme_id: str = Field(..., description="Signature scheme ID")
    image_path: str = Field(..., description="Path to firmware image")
    signature_path: str = Field(default="", description="Path to signature file")
    public_key_path: str = Field(default="", description="Path to public key")
    tampered: bool = Field(default=False, description="Simulate tampered image (for testing)")


class RollbackEvalRequest(BaseModel):
    policy_id: str = Field(..., description="Rollback policy ID")
    boot_count: int = Field(default=0, description="Current boot count")
    watchdog_fired: bool = Field(default=False, description="Whether watchdog timer fired")
    health_ok: bool = Field(default=True, description="Whether health check passed")


class ManifestCreateRequest(BaseModel):
    firmware_version: str = Field(..., description="Target firmware version")
    images: list[dict[str, Any]] = Field(..., description="List of image descriptors")
    signature_scheme: str = Field(default="ed25519_direct", description="Signature scheme ID")
    rollout_strategy: str = Field(default="immediate", description="Rollout strategy ID")
    min_firmware_version: str = Field(default="", description="Minimum eligible firmware version")
    release_notes: str = Field(default="", description="Release notes (markdown)")


class ManifestValidateRequest(BaseModel):
    manifest_data: dict[str, Any] = Field(..., description="Manifest JSON to validate")


class RolloutEvalRequest(BaseModel):
    strategy_id: str = Field(..., description="Rollout strategy ID")
    phase_id: str = Field(..., description="Phase ID to evaluate")
    fleet_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Fleet metrics: crash_rate_pct, rollback_rate_pct, success_rate_pct",
    )


class OTATestRequest(BaseModel):
    recipe_id: str = Field(..., description="OTA test recipe ID")
    target_device: str = Field(default="sim_device", description="Target device identifier")
    work_dir: str = Field(default="/tmp/ota_test", description="Working directory for test artifacts")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  A/B Slot Scheme endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/ab-schemes")
async def list_ab_schemes():
    return [s.to_dict() for s in ota.list_ab_slot_schemes()]


@router.get("/ab-schemes/{scheme_id}")
async def get_ab_scheme(scheme_id: str):
    scheme = ota.get_ab_slot_scheme(scheme_id)
    if scheme is None:
        raise HTTPException(404, f"A/B slot scheme not found: {scheme_id}")
    return scheme.to_dict()


@router.post("/ab-schemes/switch")
async def switch_slot(req: SlotSwitchRequest):
    result = ota.switch_ab_slot(req.scheme_id, req.target_slot)
    return result.to_dict()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Delta Engine endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/delta-engines")
async def list_engines():
    return [e.to_dict() for e in ota.list_delta_engines()]


@router.get("/delta-engines/{engine_id}")
async def get_engine(engine_id: str):
    engine = ota.get_delta_engine(engine_id)
    if engine is None:
        raise HTTPException(404, f"Delta engine not found: {engine_id}")
    return engine.to_dict()


@router.post("/delta/generate")
async def delta_generate(req: DeltaGenerateRequest):
    result = ota.generate_delta(
        req.engine_id, req.old_image_path, req.new_image_path, req.patch_output_path
    )
    return result.to_dict()


@router.post("/delta/apply")
async def delta_apply(req: DeltaApplyRequest):
    result = ota.apply_delta(
        req.engine_id, req.old_image_path, req.patch_path, req.output_path
    )
    return result.to_dict()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rollback Policy endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/rollback-policies")
async def list_policies():
    return [p.to_dict() for p in ota.list_rollback_policies()]


@router.get("/rollback-policies/{policy_id}")
async def get_policy(policy_id: str):
    policy = ota.get_rollback_policy(policy_id)
    if policy is None:
        raise HTTPException(404, f"Rollback policy not found: {policy_id}")
    return policy.to_dict()


@router.post("/rollback/evaluate")
async def eval_rollback(req: RollbackEvalRequest):
    result = ota.evaluate_rollback(
        req.policy_id, req.boot_count, req.watchdog_fired, req.health_ok
    )
    return result.to_dict()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Signature Scheme endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/signature-schemes")
async def list_sig_schemes():
    return [s.to_dict() for s in ota.list_signature_schemes()]


@router.get("/signature-schemes/{scheme_id}")
async def get_sig_scheme(scheme_id: str):
    scheme = ota.get_signature_scheme(scheme_id)
    if scheme is None:
        raise HTTPException(404, f"Signature scheme not found: {scheme_id}")
    return scheme.to_dict()


@router.post("/firmware/sign")
async def sign_fw(req: FirmwareSignRequest):
    result = ota.sign_firmware(req.scheme_id, req.image_path, req.key_path)
    return result.to_dict()


@router.post("/firmware/verify")
async def verify_fw(req: FirmwareVerifyRequest):
    result = ota.verify_firmware_signature(
        req.scheme_id, req.image_path, req.signature_path,
        req.public_key_path, req.tampered,
    )
    return result.to_dict()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Server / Manifest endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/rollout-strategies")
async def list_strategies():
    return [s.to_dict() for s in ota.list_rollout_strategies()]


@router.get("/rollout-strategies/{strategy_id}")
async def get_strategy(strategy_id: str):
    strategy = ota.get_rollout_strategy(strategy_id)
    if strategy is None:
        raise HTTPException(404, f"Rollout strategy not found: {strategy_id}")
    return strategy.to_dict()


@router.post("/manifest/create")
async def create_manifest(req: ManifestCreateRequest):
    manifest = ota.create_update_manifest(
        req.firmware_version, req.images, req.signature_scheme,
        req.rollout_strategy, req.min_firmware_version, req.release_notes,
    )
    return manifest.to_dict()


@router.post("/manifest/validate")
async def validate_manifest(req: ManifestValidateRequest):
    result = ota.validate_manifest(req.manifest_data)
    return result.to_dict()


@router.post("/rollout/evaluate")
async def eval_rollout(req: RolloutEvalRequest):
    result = ota.evaluate_rollout_phase(
        req.strategy_id, req.phase_id, req.fleet_metrics
    )
    return result.to_dict()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test & Artifact endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/test/recipes")
async def list_test_recipes():
    return [r.to_dict() for r in ota.list_ota_test_recipes()]


@router.get("/test/recipes/{recipe_id}")
async def get_test_recipe(recipe_id: str):
    recipe = ota.get_ota_test_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, f"OTA test recipe not found: {recipe_id}")
    return recipe.to_dict()


@router.post("/test/run")
async def run_test(req: OTATestRequest):
    result = ota.run_ota_test(req.recipe_id, req.target_device, req.work_dir)
    return result.to_dict()


@router.get("/artifacts")
async def list_artifacts():
    return ota.list_artifact_definitions()


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str):
    defn = ota.get_artifact_definition(artifact_id)
    if defn is None:
        raise HTTPException(404, f"OTA artifact not found: {artifact_id}")
    return defn


@router.post("/artifacts/generate")
async def generate_artifacts(scheme_id: str = ""):
    certs = ota.generate_cert_artifacts(scheme_id)
    return [c.to_dict() for c in certs]


@router.get("/certs")
async def get_certs():
    return ota.get_ota_framework_certs()


@router.post("/soc-compat")
async def check_soc_compat(soc_id: str):
    return ota.check_soc_ota_support(soc_id)

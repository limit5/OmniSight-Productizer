"""C18 — L4-CORE-18 Payment / PCI compliance framework endpoints (#239).

REST endpoints for PCI-DSS control mapping, PCI-PTS modules, EMV test
stubs, P2PE key injection, HSM integration, and certification artifacts.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import payment_compliance as pc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payment", tags=["payment"])


# ── Request models ───────────────────────────────────────────────────

class PCIDSSGateRequest(BaseModel):
    level: str = Field(..., description="PCI-DSS level (L1, L2, L3, L4)")
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs produced")
    dag_id: str = Field(default="", description="DAG ID to validate")


class PCIPTSGateRequest(BaseModel):
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs produced")


class EMVTestRequest(BaseModel):
    level: str = Field(..., description="EMV level (L1, L2, L3)")
    test_category: str | None = Field(default=None, description="Specific test category (optional)")


class EMVGateRequest(BaseModel):
    level: str = Field(..., description="EMV level (L1, L2, L3)")
    artifacts: list[str] = Field(default_factory=list, description="List of artifact IDs produced")


class HSMSessionRequest(BaseModel):
    vendor: str = Field(..., description="HSM vendor (thales, utimaco, safenet)")


class HSMKeyGenRequest(BaseModel):
    session_id: str = Field(..., description="Active HSM session ID")
    key_type: str = Field(..., description="Key type (zmk, zpk, bdk, etc.)")
    algorithm: str = Field(..., description="Algorithm (AES-256, RSA-2048, etc.)")


class HSMEncryptRequest(BaseModel):
    session_id: str = Field(..., description="Active HSM session ID")
    plaintext: str = Field(..., description="Data to encrypt")
    key_id: str = Field(..., description="Key ID to use")


class HSMDecryptRequest(BaseModel):
    session_id: str = Field(..., description="Active HSM session ID")
    ciphertext: str = Field(..., description="Data to decrypt")
    key_id: str = Field(..., description="Key ID to use")


class P2PEKeyInjectionRequest(BaseModel):
    hsm_vendor: str = Field(..., description="HSM vendor for key generation")
    device_id: str = Field(..., description="Target device ID")
    injection_method: str = Field(default="kif_ceremony", description="Key injection method")


class CertArtifactRequest(BaseModel):
    standard: str = Field(..., description="Standard (pci_dss, emv, pci_pts)")
    level: str = Field(default="", description="Level within standard")
    existing_artifacts: list[str] = Field(default_factory=list, description="Already-produced artifacts")


class TestRecipeRunRequest(BaseModel):
    recipe_id: str = Field(..., description="Test recipe ID to run")


class RegisterCertRequest(BaseModel):
    standard: str = Field(..., description="Certification standard")
    status: str = Field(default="Pending", description="Certification status")
    cert_id: str = Field(default="", description="Certificate ID")
    details: dict[str, Any] = Field(default_factory=dict)


# ── Response models ──────────────────────────────────────────────────

class PCIDSSLevelResponse(BaseModel):
    level_id: str
    name: str
    description: str
    validation_type: str
    required_artifacts: list[str]
    required_dag_tasks: list[str]


class PCIDSSLevelsResponse(BaseModel):
    items: list[PCIDSSLevelResponse]
    count: int


class PCIDSSRequirementResponse(BaseModel):
    req_id: str
    title: str
    description: str
    artifacts: list[str]
    tasks: list[str]


class PCIDSSRequirementsResponse(BaseModel):
    items: list[PCIDSSRequirementResponse]
    count: int


class GateFindingResponse(BaseModel):
    category: str
    item: str
    message: str


class PaymentGateResponse(BaseModel):
    standard: str
    level: str
    verdict: str
    timestamp: float
    passed: bool
    total_issues: int
    missing_artifacts: list[str]
    missing_tasks: list[str]
    findings: list[GateFindingResponse]
    metadata: dict[str, Any]


class PCIPTSRuleResponse(BaseModel):
    rule_id: str
    title: str
    description: str
    severity: str
    required_artifacts: list[str]


class PCIPTSModuleResponse(BaseModel):
    module_id: str
    name: str
    description: str
    rules: list[PCIPTSRuleResponse]


class PCIPTSModulesResponse(BaseModel):
    items: list[PCIPTSModuleResponse]
    count: int


class EMVLevelResponse(BaseModel):
    level_id: str
    name: str
    description: str
    test_categories: list[str]
    required_artifacts: list[str]
    required_dag_tasks: list[str]


class EMVLevelsResponse(BaseModel):
    items: list[EMVLevelResponse]
    count: int


class EMVTestResultResponse(BaseModel):
    level: str
    test_category: str
    status: str
    timestamp: float
    test_cases: list[dict[str, Any]]
    summary: str


class EMVTestResultsResponse(BaseModel):
    results: list[EMVTestResultResponse]
    count: int


class P2PEDomainResponse(BaseModel):
    domain_id: str
    name: str
    description: str
    controls: list[dict[str, Any]]


class P2PEDomainsResponse(BaseModel):
    items: list[P2PEDomainResponse]
    count: int


class KeyInjectionResponse(BaseModel):
    device_id: str
    hsm_vendor: str
    status: str
    timestamp: float
    key_serial_number: str
    ipek_check_value: str
    steps_completed: list[str]
    error_message: str


class HSMVendorSummaryResponse(BaseModel):
    vendor_id: str
    name: str
    type: str
    fips_level: str
    pci_pts_certified: bool
    protocols: list[str]
    key_types: list[str]
    supported_algorithms: list[str]


class HSMVendorResponse(HSMVendorSummaryResponse):
    commands: dict[str, str]


class HSMVendorsResponse(BaseModel):
    items: list[HSMVendorSummaryResponse]
    count: int


class HSMSessionResponse(BaseModel):
    session_id: str
    vendor: str
    status: str
    created_at: float
    capabilities: list[str]
    metadata: dict[str, Any]


class HSMSessionsResponse(BaseModel):
    items: list[HSMSessionResponse]
    count: int


class HSMCloseResponse(BaseModel):
    status: str
    session_id: str


class HSMKeyGenResponse(BaseModel):
    status: str
    key_id: str
    key_type: str
    algorithm: str
    check_value: str
    hsm_vendor: str
    command_used: str


class HSMEncryptResponse(BaseModel):
    status: str
    ciphertext: str
    key_id: str
    command_used: str


class HSMDecryptResponse(BaseModel):
    status: str
    plaintext: str
    key_id: str
    command_used: str


class PaymentTestRecipeResponse(BaseModel):
    recipe_id: str
    name: str
    description: str
    domain: str
    steps: list[str]


class PaymentTestRecipesResponse(BaseModel):
    items: list[PaymentTestRecipeResponse]
    count: int


class PaymentTestRecipeRunResponse(BaseModel):
    status: str
    recipe_id: str
    recipe_name: str
    domain: str
    total_steps: int
    passed_steps: int
    step_results: list[dict[str, Any]]
    timestamp: float


class CertArtifactItemResponse(BaseModel):
    artifact_id: str
    name: str
    status: str
    file_pattern: str


class CertGapAnalysisResponse(BaseModel):
    artifact_id: str
    name: str
    action: str
    priority: str


class CertArtifactBundleResponse(BaseModel):
    standard: str
    level: str
    status: str
    timestamp: float
    artifacts: list[CertArtifactItemResponse]
    gap_analysis: list[CertGapAnalysisResponse]


class PaymentCertResponse(BaseModel):
    standard: str
    status: str
    cert_id: str
    details: dict[str, Any]


class PaymentCertsResponse(BaseModel):
    items: list[PaymentCertResponse]
    count: int


class RegisterCertResponse(BaseModel):
    status: str
    standard: str


class ArtifactDefinitionResponse(BaseModel):
    artifact_id: str
    name: str
    description: str
    file_pattern: str


class ArtifactDefinitionsResponse(BaseModel):
    items: list[ArtifactDefinitionResponse]
    count: int


class CompatibleSocResponse(BaseModel):
    soc_id: str
    name: str
    payment_capabilities: list[str]
    notes: str


class CompatibleSocsResponse(BaseModel):
    items: list[CompatibleSocResponse]
    count: int


# ── PCI-DSS endpoints ───────────────────────────────────────────────

@router.get("/pci-dss/levels", response_model=PCIDSSLevelsResponse)
async def list_pci_dss_levels(_user=Depends(_au.require_operator)) -> dict:
    levels = pc.list_pci_dss_levels()
    return {
        "items": [
            {
                "level_id": lv.level_id,
                "name": lv.name,
                "description": lv.description,
                "validation_type": lv.validation_type,
                "required_artifacts": lv.required_artifacts,
                "required_dag_tasks": lv.required_dag_tasks,
            }
            for lv in levels
        ],
        "count": len(levels),
    }


@router.get("/pci-dss/levels/{level_id}", response_model=PCIDSSLevelResponse)
async def get_pci_dss_level(level_id: str, _user=Depends(_au.require_operator)) -> dict:
    lv = pc.get_pci_dss_level(level_id)
    if lv is None:
        raise HTTPException(status_code=404, detail=f"PCI-DSS level {level_id!r} not found")
    return {
        "level_id": lv.level_id,
        "name": lv.name,
        "description": lv.description,
        "validation_type": lv.validation_type,
        "required_artifacts": lv.required_artifacts,
        "required_dag_tasks": lv.required_dag_tasks,
    }


@router.get("/pci-dss/requirements", response_model=PCIDSSRequirementsResponse)
async def list_pci_dss_requirements(_user=Depends(_au.require_operator)) -> dict:
    reqs = pc.list_pci_dss_requirements()
    return {
        "items": [
            {
                "req_id": r.req_id,
                "title": r.title,
                "description": r.description,
                "artifacts": r.artifacts,
                "tasks": r.tasks,
            }
            for r in reqs
        ],
        "count": len(reqs),
    }


@router.get("/pci-dss/requirements/{req_id}", response_model=PCIDSSRequirementResponse)
async def get_pci_dss_requirement(req_id: str, _user=Depends(_au.require_operator)) -> dict:
    req = pc.get_pci_dss_requirement(req_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"PCI-DSS requirement {req_id!r} not found")
    return {
        "req_id": req.req_id,
        "title": req.title,
        "description": req.description,
        "artifacts": req.artifacts,
        "tasks": req.tasks,
    }


@router.post("/pci-dss/validate", response_model=PaymentGateResponse)
async def validate_pci_dss(body: PCIDSSGateRequest, _user=Depends(_au.require_operator)) -> dict:
    from backend.dag_schema import DAG, Task
    dag = DAG(dag_id=body.dag_id or "inline", tasks=[
        Task(task_id="placeholder", description="placeholder", required_tier="t1",
             toolchain="cmake", expected_output="out"),
    ])
    result = pc.validate_pci_dss_gate(dag, body.level, body.artifacts)
    return result.to_dict()


# ── PCI-PTS endpoints ───────────────────────────────────────────────

@router.get("/pci-pts/modules", response_model=PCIPTSModulesResponse)
async def list_pci_pts_modules(_user=Depends(_au.require_operator)) -> dict:
    modules = pc.list_pci_pts_modules()
    return {
        "items": [
            {
                "module_id": m.module_id,
                "name": m.name,
                "description": m.description,
                "rules": [
                    {
                        "rule_id": r.rule_id,
                        "title": r.title,
                        "description": r.description,
                        "severity": r.severity,
                        "required_artifacts": r.required_artifacts,
                    }
                    for r in m.rules
                ],
            }
            for m in modules
        ],
        "count": len(modules),
    }


@router.get("/pci-pts/modules/{module_id}", response_model=PCIPTSModuleResponse)
async def get_pci_pts_module(module_id: str, _user=Depends(_au.require_operator)) -> dict:
    mod = pc.get_pci_pts_module(module_id)
    if mod is None:
        raise HTTPException(status_code=404, detail=f"PCI-PTS module {module_id!r} not found")
    return {
        "module_id": mod.module_id,
        "name": mod.name,
        "description": mod.description,
        "rules": [
            {
                "rule_id": r.rule_id,
                "title": r.title,
                "description": r.description,
                "severity": r.severity,
                "required_artifacts": r.required_artifacts,
            }
            for r in mod.rules
        ],
    }


@router.post("/pci-pts/validate", response_model=PaymentGateResponse)
async def validate_pci_pts(body: PCIPTSGateRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.validate_pci_pts_gate(body.artifacts)
    return result.to_dict()


# ── EMV endpoints ────────────────────────────────────────────────────

@router.get("/emv/levels", response_model=EMVLevelsResponse)
async def list_emv_levels(_user=Depends(_au.require_operator)) -> dict:
    levels = pc.list_emv_levels()
    return {
        "items": [
            {
                "level_id": lv.level_id,
                "name": lv.name,
                "description": lv.description,
                "test_categories": lv.test_categories,
                "required_artifacts": lv.required_artifacts,
                "required_dag_tasks": lv.required_dag_tasks,
            }
            for lv in levels
        ],
        "count": len(levels),
    }


@router.get("/emv/levels/{level_id}", response_model=EMVLevelResponse)
async def get_emv_level(level_id: str, _user=Depends(_au.require_operator)) -> dict:
    lv = pc.get_emv_level(level_id)
    if lv is None:
        raise HTTPException(status_code=404, detail=f"EMV level {level_id!r} not found")
    return {
        "level_id": lv.level_id,
        "name": lv.name,
        "description": lv.description,
        "test_categories": lv.test_categories,
        "required_artifacts": lv.required_artifacts,
        "required_dag_tasks": lv.required_dag_tasks,
    }


@router.post("/emv/test", response_model=EMVTestResultsResponse)
async def run_emv_test(body: EMVTestRequest, _user=Depends(_au.require_operator)) -> dict:
    results = pc.run_emv_test_stub(body.level, body.test_category)
    return {
        "results": [r.to_dict() for r in results],
        "count": len(results),
    }


@router.post("/emv/validate", response_model=PaymentGateResponse)
async def validate_emv(body: EMVGateRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.validate_emv_gate(body.level, body.artifacts)
    return result.to_dict()


# ── P2PE endpoints ───────────────────────────────────────────────────

@router.get("/p2pe/domains", response_model=P2PEDomainsResponse)
async def list_p2pe_domains(_user=Depends(_au.require_operator)) -> dict:
    domains = pc.list_p2pe_domains()
    return {
        "items": [
            {
                "domain_id": d.domain_id,
                "name": d.name,
                "description": d.description,
                "controls": d.controls,
            }
            for d in domains
        ],
        "count": len(domains),
    }


@router.post("/p2pe/key-injection", response_model=KeyInjectionResponse)
async def run_key_injection(body: P2PEKeyInjectionRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.run_p2pe_key_injection(body.hsm_vendor, body.device_id, body.injection_method)
    return result.to_dict()


# ── HSM endpoints ────────────────────────────────────────────────────

@router.get("/hsm/vendors", response_model=HSMVendorsResponse)
async def list_hsm_vendors(_user=Depends(_au.require_operator)) -> dict:
    vendors = pc.list_hsm_vendors()
    return {
        "items": [
            {
                "vendor_id": v.vendor_id,
                "name": v.name,
                "type": v.hsm_type,
                "fips_level": v.fips_level,
                "pci_pts_certified": v.pci_pts_certified,
                "protocols": v.protocols,
                "key_types": v.key_types,
                "supported_algorithms": v.supported_algorithms,
            }
            for v in vendors
        ],
        "count": len(vendors),
    }


@router.get("/hsm/vendors/{vendor_id}", response_model=HSMVendorResponse)
async def get_hsm_vendor(vendor_id: str, _user=Depends(_au.require_operator)) -> dict:
    v = pc.get_hsm_vendor(vendor_id)
    if v is None:
        raise HTTPException(status_code=404, detail=f"HSM vendor {vendor_id!r} not found")
    return {
        "vendor_id": v.vendor_id,
        "name": v.name,
        "type": v.hsm_type,
        "fips_level": v.fips_level,
        "pci_pts_certified": v.pci_pts_certified,
        "protocols": v.protocols,
        "key_types": v.key_types,
        "supported_algorithms": v.supported_algorithms,
        "commands": v.commands,
    }


@router.post("/hsm/sessions", response_model=HSMSessionResponse)
async def create_hsm_session(body: HSMSessionRequest, _user=Depends(_au.require_operator)) -> dict:
    session = pc.create_hsm_session(body.vendor)
    if session.status == pc.HSMSessionStatus.error:
        raise HTTPException(status_code=400, detail=session.metadata.get("error", "HSM session error"))
    return session.to_dict()


@router.get("/hsm/sessions", response_model=HSMSessionsResponse)
async def list_hsm_sessions(_user=Depends(_au.require_operator)) -> dict:
    sessions = pc.list_active_hsm_sessions()
    return {
        "items": [s.to_dict() for s in sessions],
        "count": len(sessions),
    }


@router.delete("/hsm/sessions/{session_id}", response_model=HSMCloseResponse)
async def close_hsm_session(session_id: str, _user=Depends(_au.require_operator)) -> dict:
    closed = pc.close_hsm_session(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")
    return {"status": "closed", "session_id": session_id}


@router.post("/hsm/generate-key", response_model=HSMKeyGenResponse)
async def hsm_generate_key(body: HSMKeyGenRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.hsm_generate_key(body.session_id, body.key_type, body.algorithm)
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("error", "Key generation failed"))
    return result


@router.post("/hsm/encrypt", response_model=HSMEncryptResponse)
async def hsm_encrypt(body: HSMEncryptRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.hsm_encrypt(body.session_id, body.plaintext, body.key_id)
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("error", "Encryption failed"))
    return result


@router.post("/hsm/decrypt", response_model=HSMDecryptResponse)
async def hsm_decrypt(body: HSMDecryptRequest, _user=Depends(_au.require_operator)) -> dict:
    result = pc.hsm_decrypt(body.session_id, body.ciphertext, body.key_id)
    if result.get("status") == "failed":
        raise HTTPException(status_code=400, detail=result.get("error", "Decryption failed"))
    return result


# ── Test recipe endpoints ────────────────────────────────────────────

@router.get("/test-recipes", response_model=PaymentTestRecipesResponse)
async def list_test_recipes(_user=Depends(_au.require_operator)) -> dict:
    recipes = pc.list_test_recipes()
    return {
        "items": [
            {
                "recipe_id": r.recipe_id,
                "name": r.name,
                "description": r.description,
                "domain": r.domain,
                "steps": r.steps,
            }
            for r in recipes
        ],
        "count": len(recipes),
    }


@router.post("/test-recipes/{recipe_id}/run", response_model=PaymentTestRecipeRunResponse)
async def run_test_recipe(recipe_id: str, _user=Depends(_au.require_operator)) -> dict:
    result = pc.run_test_recipe(recipe_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=404, detail=result.get("message", "Recipe not found"))
    return result


# ── Cert artifact endpoints ──────────────────────────────────────────

@router.post("/certs/generate", response_model=CertArtifactBundleResponse)
async def generate_cert_artifacts(body: CertArtifactRequest, _user=Depends(_au.require_operator)) -> dict:
    bundle = pc.generate_cert_artifacts(body.standard, body.level, body.existing_artifacts)
    if bundle.status == pc.CertArtifactStatus.error:
        raise HTTPException(status_code=400, detail=f"Cannot generate artifacts for {body.standard}/{body.level}")
    return bundle.to_dict()


@router.get("/certs", response_model=PaymentCertsResponse)
async def list_payment_certs(_user=Depends(_au.require_operator)) -> dict:
    certs = pc.get_payment_certs()
    return {"items": certs, "count": len(certs)}


@router.post("/certs/register", response_model=RegisterCertResponse)
async def register_payment_cert(body: RegisterCertRequest, _user=Depends(_au.require_operator)) -> dict:
    pc.register_payment_cert(body.standard, body.status, body.cert_id, body.details)
    return {"status": "registered", "standard": body.standard}


# ── Artifact definitions ─────────────────────────────────────────────

@router.get("/artifacts", response_model=ArtifactDefinitionsResponse)
async def list_artifact_definitions(_user=Depends(_au.require_operator)) -> dict:
    defs = pc.list_artifact_definitions()
    return {
        "items": [
            {
                "artifact_id": d.artifact_id,
                "name": d.name,
                "description": d.description,
                "file_pattern": d.file_pattern,
            }
            for d in defs
        ],
        "count": len(defs),
    }


@router.get("/artifacts/{artifact_id}", response_model=ArtifactDefinitionResponse)
async def get_artifact_definition(artifact_id: str, _user=Depends(_au.require_operator)) -> dict:
    d = pc.get_artifact_definition(artifact_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_id!r} not found")
    return {
        "artifact_id": d.artifact_id,
        "name": d.name,
        "description": d.description,
        "file_pattern": d.file_pattern,
    }


# ── SoC compatibility ────────────────────────────────────────────────

@router.get("/socs", response_model=CompatibleSocsResponse)
async def list_compatible_socs(_user=Depends(_au.require_operator)) -> dict:
    socs = pc.list_compatible_socs()
    return {"items": socs, "count": len(socs)}


@router.get("/socs/{soc_id}", response_model=CompatibleSocResponse)
async def get_compatible_soc(soc_id: str, _user=Depends(_au.require_operator)) -> dict:
    soc = pc.get_compatible_soc(soc_id)
    if soc is None:
        raise HTTPException(status_code=404, detail=f"SoC {soc_id!r} not found")
    return soc

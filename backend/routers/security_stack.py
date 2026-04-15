"""C15 — L4-CORE-15 Security stack endpoints (#229).

REST endpoints for secure boot chain verification, TEE binding queries,
remote attestation, SBOM signing, threat model evaluation, and security
test execution.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import security_stack as sec

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/security", tags=["security"])


class BootChainVerifyRequest(BaseModel):
    chain_id: str = Field(..., description="Secure boot chain ID")
    stage_results: list[dict[str, str]] = Field(
        default_factory=list,
        description="List of {stage_id, status} for each verified stage",
    )


class TEESessionRequest(BaseModel):
    tee_id: str = Field(..., description="TEE binding ID (optee, trustzone_m, sgx)")
    ta_uuid: str = Field(
        default="00000000-0000-0000-0000-000000000001",
        description="Trusted Application UUID",
    )
    command_id: int = Field(default=0, description="TA command ID")
    params: dict[str, Any] = Field(default_factory=dict, description="Command parameters")


class AttestationQuoteRequest(BaseModel):
    provider_id: str = Field(..., description="Attestation provider ID")
    nonce: str = Field(default="", description="Challenge nonce for freshness")
    pcr_indices: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 4, 7],
        description="PCR indices to include in quote",
    )


class AttestationVerifyRequest(BaseModel):
    provider_id: str = Field(..., description="Attestation provider ID")
    nonce: str = Field(default="", description="Challenge nonce")
    pcr_indices: list[int] = Field(default_factory=lambda: [0, 1, 2, 4, 7])
    expected_pcr_values: dict[str, str] = Field(
        default_factory=dict,
        description="Expected PCR values {pcr_index_str: hash_hex}",
    )


class SBOMSignRequest(BaseModel):
    tool_id: str = Field(default="cosign", description="SBOM signing tool")
    sbom_path: str = Field(..., description="Path to SBOM file")
    mode: str = Field(default="key_pair", description="Signing mode (keyless, key_pair, kms)")
    key_path: str = Field(default="", description="Path to signing key (for key_pair mode)")


class ThreatCoverageRequest(BaseModel):
    class_id: str = Field(..., description="Product class ID for threat model")
    provided_mitigations: list[str] = Field(
        default_factory=list,
        description="List of implemented mitigations",
    )


class SecurityTestRequest(BaseModel):
    recipe_id: str = Field(..., description="Security test recipe ID")
    target_device: str = Field(..., description="Target device identifier")
    timeout_s: int = Field(default=600, description="Timeout in seconds")


class SocSecurityRequest(BaseModel):
    soc_id: str = Field(..., description="SoC identifier")
    features: list[str] = Field(default_factory=list, description="Features to check")


class ArtifactGenRequest(BaseModel):
    security_domain: str = Field(..., description="Security domain")
    provided_artifacts: list[str] = Field(default_factory=list)


# -- Secure boot chain endpoints --

@router.get("/boot-chains")
async def list_boot_chains() -> dict[str, Any]:
    chains = sec.list_boot_chains()
    return {
        "count": len(chains),
        "chains": [c.to_dict() for c in chains],
    }


@router.get("/boot-chains/{chain_id}")
async def get_boot_chain(chain_id: str) -> dict[str, Any]:
    chain = sec.get_boot_chain(chain_id)
    if chain is None:
        raise HTTPException(status_code=404, detail=f"Boot chain {chain_id!r} not found")
    return chain.to_dict()


@router.post("/boot-chains/verify")
async def verify_boot_chain(req: BootChainVerifyRequest) -> dict[str, Any]:
    result = sec.verify_boot_chain(req.chain_id, req.stage_results)
    return result.to_dict()


# -- TEE binding endpoints --

@router.get("/tee/bindings")
async def list_tee_bindings() -> dict[str, Any]:
    bindings = sec.list_tee_bindings()
    return {
        "count": len(bindings),
        "bindings": [b.to_dict() for b in bindings],
    }


@router.get("/tee/bindings/{tee_id}")
async def get_tee_binding(tee_id: str) -> dict[str, Any]:
    binding = sec.get_tee_binding(tee_id)
    if binding is None:
        raise HTTPException(status_code=404, detail=f"TEE binding {tee_id!r} not found")
    return binding.to_dict()


@router.post("/tee/session")
async def simulate_tee_session(req: TEESessionRequest) -> dict[str, Any]:
    return sec.simulate_tee_session(
        req.tee_id,
        ta_uuid=req.ta_uuid,
        command_id=req.command_id,
        params=req.params,
    )


# -- Attestation endpoints --

@router.get("/attestation/providers")
async def list_attestation_providers() -> dict[str, Any]:
    providers = sec.list_attestation_providers()
    return {
        "count": len(providers),
        "providers": [p.to_dict() for p in providers],
    }


@router.get("/attestation/providers/{provider_id}")
async def get_attestation_provider(provider_id: str) -> dict[str, Any]:
    provider = sec.get_attestation_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=404, detail=f"Attestation provider {provider_id!r} not found"
        )
    return provider.to_dict()


@router.post("/attestation/quote")
async def generate_attestation_quote(req: AttestationQuoteRequest) -> dict[str, Any]:
    quote = sec.generate_attestation_quote(
        req.provider_id, nonce=req.nonce, pcr_indices=req.pcr_indices,
    )
    return quote.to_dict()


@router.post("/attestation/verify")
async def verify_attestation(req: AttestationVerifyRequest) -> dict[str, Any]:
    quote = sec.generate_attestation_quote(
        req.provider_id, nonce=req.nonce, pcr_indices=req.pcr_indices,
    )
    expected = {int(k): v for k, v in req.expected_pcr_values.items()} if req.expected_pcr_values else None
    return sec.verify_attestation_quote(quote, expected)


# -- SBOM signing endpoints --

@router.get("/sbom/signers")
async def list_sbom_signers() -> dict[str, Any]:
    signers = sec.list_sbom_signers()
    return {
        "count": len(signers),
        "signers": [s.to_dict() for s in signers],
    }


@router.get("/sbom/signers/{tool_id}")
async def get_sbom_signer(tool_id: str) -> dict[str, Any]:
    signer = sec.get_sbom_signer(tool_id)
    if signer is None:
        raise HTTPException(status_code=404, detail=f"SBOM signer {tool_id!r} not found")
    return signer.to_dict()


@router.post("/sbom/sign")
async def sign_sbom(req: SBOMSignRequest) -> dict[str, Any]:
    result = sec.sign_sbom(req.tool_id, req.sbom_path, mode=req.mode, key_path=req.key_path)
    return result.to_dict()


# -- Threat model endpoints --

@router.get("/threat-models")
async def list_threat_models() -> dict[str, Any]:
    models = sec.list_threat_models()
    return {
        "count": len(models),
        "models": [m.to_dict() for m in models],
    }


@router.get("/threat-models/{class_id}")
async def get_threat_model(class_id: str) -> dict[str, Any]:
    model = sec.get_threat_model(class_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Threat model {class_id!r} not found")
    return model.to_dict()


@router.post("/threat-models/coverage")
async def evaluate_threat_coverage(req: ThreatCoverageRequest) -> dict[str, Any]:
    result = sec.evaluate_threat_coverage(req.class_id, req.provided_mitigations)
    return result.to_dict()


# -- Security test endpoints --

@router.get("/test/recipes")
async def list_security_test_recipes() -> dict[str, Any]:
    recipes = sec.list_security_test_recipes()
    return {
        "count": len(recipes),
        "recipes": [r.to_dict() for r in recipes],
    }


@router.get("/test/recipes/{recipe_id}")
async def get_security_test_recipe(recipe_id: str) -> dict[str, Any]:
    recipe = sec.get_security_test_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"Test recipe {recipe_id!r} not found")
    return recipe.to_dict()


@router.get("/test/recipes/domain/{domain}")
async def get_recipes_by_domain(domain: str) -> dict[str, Any]:
    recipes = sec.get_recipes_by_domain(domain)
    return {
        "domain": domain,
        "count": len(recipes),
        "recipes": [r.to_dict() for r in recipes],
    }


@router.post("/test/run")
async def run_security_test(req: SecurityTestRequest) -> dict[str, Any]:
    result = sec.run_security_test(
        req.recipe_id, req.target_device, timeout_s=req.timeout_s,
    )
    return result.to_dict()


# -- SoC compatibility --

@router.post("/soc-compat")
async def check_soc_security_support(req: SocSecurityRequest) -> dict[str, Any]:
    return sec.check_soc_security_support(req.soc_id, req.features)


# -- Artifact endpoints --

@router.get("/artifacts")
async def list_artifact_definitions() -> dict[str, Any]:
    defs = sec.list_artifact_definitions()
    return {
        "count": len(defs),
        "artifacts": defs,
    }


@router.post("/artifacts/generate")
async def generate_cert_artifacts(req: ArtifactGenRequest) -> dict[str, Any]:
    artifacts = sec.generate_cert_artifacts(
        req.security_domain,
        spec={"provided_artifacts": req.provided_artifacts},
    )
    return {
        "count": len(artifacts),
        "artifacts": [a.to_dict() for a in artifacts],
    }

"""C15 — L4-CORE-15 Security stack (#229).

Secure boot chain verification, TEE binding (OP-TEE / TrustZone / SGX),
remote attestation (TPM / SE / fTPM), SBOM signing (sigstore/cosign),
key management, and threat model per product class.

Public API:
    chains       = list_boot_chains()
    chain        = get_boot_chain(chain_id)
    tees         = list_tee_bindings()
    tee          = get_tee_binding(tee_id)
    providers    = list_attestation_providers()
    provider     = get_attestation_provider(provider_id)
    signers      = list_sbom_signers()
    signer       = get_sbom_signer(tool_id)
    models       = list_threat_models()
    model        = get_threat_model(class_id)
    recipes      = list_security_test_recipes()
    result       = run_security_test(recipe_id, target, work_dir)
    verified     = verify_boot_chain(chain_id, stage_results)
    attestation  = generate_attestation_quote(provider_id, nonce, pcr_list)
    sbom_result  = sign_sbom(tool_id, sbom_path, mode)
    coverage     = evaluate_threat_coverage(class_id, mitigations)
    certs        = get_security_stack_certs()
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SECURITY_STACK_PATH = _PROJECT_ROOT / "configs" / "security_stack.yaml"


# -- Enums --

class SecurityDomain(str, Enum):
    secure_boot = "secure_boot"
    tee = "tee"
    attestation = "attestation"
    sbom = "sbom"
    key_management = "key_management"
    threat_model = "threat_model"


class BootStageStatus(str, Enum):
    verified = "verified"
    failed = "failed"
    skipped = "skipped"
    pending = "pending"


class TEESessionState(str, Enum):
    initialized = "initialized"
    opened = "opened"
    active = "active"
    closed = "closed"
    error = "error"


class AttestationStatus(str, Enum):
    trusted = "trusted"
    untrusted = "untrusted"
    pending = "pending"
    error = "error"


class SBOMFormat(str, Enum):
    spdx = "spdx"
    cyclonedx = "cyclonedx"


class SigningMode(str, Enum):
    keyless = "keyless"
    key_pair = "key_pair"
    kms = "kms"


class ThreatCategory(str, Enum):
    spoofing = "spoofing"
    tampering = "tampering"
    repudiation = "repudiation"
    information_disclosure = "information_disclosure"
    denial_of_service = "denial_of_service"
    elevation_of_privilege = "elevation_of_privilege"


class SecurityTestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


# -- Data models --

@dataclass
class BootStage:
    stage_id: str
    name: str
    description: str = ""
    verification: str = ""
    signing_algo: str = ""
    rollback_protection: bool = False
    immutable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "name": self.name,
            "description": self.description,
            "verification": self.verification,
            "signing_algo": self.signing_algo,
            "rollback_protection": self.rollback_protection,
            "immutable": self.immutable,
        }


@dataclass
class SecureBootChainDef:
    chain_id: str
    name: str
    description: str = ""
    stages: list[BootStage] = field(default_factory=list)
    compatible_socs: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "name": self.name,
            "description": self.description,
            "stages": [s.to_dict() for s in self.stages],
            "compatible_socs": self.compatible_socs,
            "required_tools": self.required_tools,
        }


@dataclass
class TEEFunction:
    name: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


@dataclass
class TEEBindingDef:
    tee_id: str
    name: str
    description: str = ""
    spec: str = ""
    features: list[str] = field(default_factory=list)
    api_functions: list[TEEFunction] = field(default_factory=list)
    compatible_socs: list[str] = field(default_factory=list)
    build_system: str = "cmake"
    ta_signing: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tee_id": self.tee_id,
            "name": self.name,
            "description": self.description,
            "spec": self.spec,
            "features": self.features,
            "api_functions": [f.to_dict() for f in self.api_functions],
            "compatible_socs": self.compatible_socs,
            "build_system": self.build_system,
            "ta_signing": self.ta_signing,
        }


@dataclass
class AttestationOperation:
    name: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


@dataclass
class PCRAssignment:
    pcr_index: int
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"pcr_index": self.pcr_index, "description": self.description}


@dataclass
class AttestationProviderDef:
    provider_id: str
    name: str
    description: str = ""
    spec: str = ""
    features: list[str] = field(default_factory=list)
    operations: list[AttestationOperation] = field(default_factory=list)
    pcr_banks: list[str] = field(default_factory=list)
    pcr_assignments: list[PCRAssignment] = field(default_factory=list)
    compatible_platforms: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider_id": self.provider_id,
            "name": self.name,
            "description": self.description,
            "spec": self.spec,
            "features": self.features,
            "operations": [o.to_dict() for o in self.operations],
            "compatible_platforms": self.compatible_platforms,
            "required_tools": self.required_tools,
        }
        if self.pcr_banks:
            d["pcr_banks"] = self.pcr_banks
        if self.pcr_assignments:
            d["pcr_assignments"] = [p.to_dict() for p in self.pcr_assignments]
        return d


@dataclass
class SBOMSigningMode:
    mode_id: str
    name: str
    description: str = ""
    requires_key: bool = False
    requires_oidc: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode_id": self.mode_id,
            "name": self.name,
            "description": self.description,
            "requires_key": self.requires_key,
            "requires_oidc": self.requires_oidc,
        }


@dataclass
class SBOMSignerDef:
    tool_id: str
    name: str
    description: str = ""
    version: str = ""
    signing_modes: list[SBOMSigningMode] = field(default_factory=list)
    sbom_formats: list[str] = field(default_factory=list)
    commands: list[dict[str, str]] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "signing_modes": [m.to_dict() for m in self.signing_modes],
            "sbom_formats": self.sbom_formats,
            "commands": self.commands,
            "required_tools": self.required_tools,
        }


@dataclass
class ThreatEntry:
    category: str
    threats: list[str] = field(default_factory=list)
    mitigations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "threats": self.threats,
            "mitigations": self.mitigations,
        }


@dataclass
class ThreatModelDef:
    class_id: str
    name: str
    stride_categories: list[ThreatEntry] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "name": self.name,
            "stride_categories": [c.to_dict() for c in self.stride_categories],
            "required_artifacts": self.required_artifacts,
        }


@dataclass
class SecurityTestRecipe:
    recipe_id: str
    name: str
    category: str
    description: str = ""
    security_domain: str = ""
    tools: list[str] = field(default_factory=list)
    timeout_s: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "security_domain": self.security_domain,
            "tools": self.tools,
            "timeout_s": self.timeout_s,
        }


@dataclass
class SecurityTestResult:
    recipe_id: str
    security_domain: str
    status: SecurityTestStatus
    target_device: str = ""
    timestamp: float = field(default_factory=time.time)
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == SecurityTestStatus.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "security_domain": self.security_domain,
            "status": self.status.value,
            "target_device": self.target_device,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class BootChainVerifyResult:
    chain_id: str
    overall_status: BootStageStatus
    stage_results: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "overall_status": self.overall_status.value,
            "stage_results": self.stage_results,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class AttestationQuote:
    provider_id: str
    status: AttestationStatus
    nonce: str = ""
    pcr_values: dict[int, str] = field(default_factory=dict)
    quote_data: str = ""
    signature: str = ""
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status.value,
            "nonce": self.nonce,
            "pcr_values": {str(k): v for k, v in self.pcr_values.items()},
            "quote_data": self.quote_data,
            "signature": self.signature,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class SBOMSignResult:
    tool_id: str
    mode: str
    sbom_path: str = ""
    signature_path: str = ""
    success: bool = False
    transparency_log_entry: str = ""
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "mode": self.mode,
            "sbom_path": self.sbom_path,
            "signature_path": self.signature_path,
            "success": self.success,
            "transparency_log_entry": self.transparency_log_entry,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class ThreatCoverageResult:
    class_id: str
    total_threats: int = 0
    mitigated_threats: int = 0
    unmitigated_threats: int = 0
    coverage_pct: float = 0.0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "total_threats": self.total_threats,
            "mitigated_threats": self.mitigated_threats,
            "unmitigated_threats": self.unmitigated_threats,
            "coverage_pct": self.coverage_pct,
            "gaps": self.gaps,
            "timestamp": self.timestamp,
        }


@dataclass
class SecurityCertArtifact:
    artifact_id: str
    name: str
    security_domain: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "security_domain": self.security_domain,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


# -- Config loading (cached) --

_SEC_CACHE: dict | None = None


def _load_security_config() -> dict:
    global _SEC_CACHE
    if _SEC_CACHE is None:
        try:
            _SEC_CACHE = yaml.safe_load(
                _SECURITY_STACK_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "security_stack.yaml load failed: %s — using empty config", exc
            )
            _SEC_CACHE = {
                "secure_boot_chains": {},
                "tee_bindings": {},
                "attestation_providers": {},
                "sbom_signing": {},
                "threat_models": {},
                "test_recipes": [],
                "artifact_definitions": {},
            }
    return _SEC_CACHE


def reload_security_config_for_tests() -> None:
    global _SEC_CACHE
    _SEC_CACHE = None


# -- Secure boot chain queries --

def _parse_boot_stage(data: dict) -> BootStage:
    return BootStage(
        stage_id=data.get("stage_id", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        verification=data.get("verification", ""),
        signing_algo=data.get("signing_algo", ""),
        rollback_protection=data.get("rollback_protection", False),
        immutable=data.get("immutable", False),
    )


def _parse_boot_chain(chain_id: str, data: dict) -> SecureBootChainDef:
    stages = [_parse_boot_stage(s) for s in data.get("stages", [])]
    return SecureBootChainDef(
        chain_id=chain_id,
        name=data.get("name", chain_id),
        description=data.get("description", ""),
        stages=stages,
        compatible_socs=data.get("compatible_socs", []),
        required_tools=data.get("required_tools", []),
    )


def list_boot_chains() -> list[SecureBootChainDef]:
    raw = _load_security_config().get("secure_boot_chains", {})
    return [_parse_boot_chain(k, v) for k, v in raw.items()]


def get_boot_chain(chain_id: str) -> SecureBootChainDef | None:
    raw = _load_security_config().get("secure_boot_chains", {})
    if chain_id not in raw:
        return None
    return _parse_boot_chain(chain_id, raw[chain_id])


# -- TEE binding queries --

def _parse_tee_function(data: dict) -> TEEFunction:
    return TEEFunction(
        name=data.get("name", ""),
        description=data.get("description", ""),
    )


def _parse_tee_binding(tee_id: str, data: dict) -> TEEBindingDef:
    funcs = [_parse_tee_function(f) for f in data.get("api_functions", [])]
    return TEEBindingDef(
        tee_id=tee_id,
        name=data.get("name", tee_id),
        description=data.get("description", ""),
        spec=data.get("spec", ""),
        features=data.get("features", []),
        api_functions=funcs,
        compatible_socs=data.get("compatible_socs", []),
        build_system=data.get("build_system", "cmake"),
        ta_signing=data.get("ta_signing", ""),
    )


def list_tee_bindings() -> list[TEEBindingDef]:
    raw = _load_security_config().get("tee_bindings", {})
    return [_parse_tee_binding(k, v) for k, v in raw.items()]


def get_tee_binding(tee_id: str) -> TEEBindingDef | None:
    raw = _load_security_config().get("tee_bindings", {})
    if tee_id not in raw:
        return None
    return _parse_tee_binding(tee_id, raw[tee_id])


# -- Attestation provider queries --

def _parse_attestation_op(data: dict) -> AttestationOperation:
    return AttestationOperation(
        name=data.get("name", ""),
        description=data.get("description", ""),
    )


def _parse_pcr_assignment(data: dict) -> PCRAssignment:
    return PCRAssignment(
        pcr_index=data.get("pcr_index", 0),
        description=data.get("description", ""),
    )


def _parse_attestation_provider(provider_id: str, data: dict) -> AttestationProviderDef:
    ops = [_parse_attestation_op(o) for o in data.get("operations", [])]
    pcrs = [_parse_pcr_assignment(p) for p in data.get("pcr_assignments", [])]
    return AttestationProviderDef(
        provider_id=provider_id,
        name=data.get("name", provider_id),
        description=data.get("description", ""),
        spec=data.get("spec", ""),
        features=data.get("features", []),
        operations=ops,
        pcr_banks=data.get("pcr_banks", []),
        pcr_assignments=pcrs,
        compatible_platforms=data.get("compatible_platforms", []),
        required_tools=data.get("required_tools", []),
    )


def list_attestation_providers() -> list[AttestationProviderDef]:
    raw = _load_security_config().get("attestation_providers", {})
    return [_parse_attestation_provider(k, v) for k, v in raw.items()]


def get_attestation_provider(provider_id: str) -> AttestationProviderDef | None:
    raw = _load_security_config().get("attestation_providers", {})
    if provider_id not in raw:
        return None
    return _parse_attestation_provider(provider_id, raw[provider_id])


# -- SBOM signer queries --

def _parse_signing_mode(data: dict) -> SBOMSigningMode:
    return SBOMSigningMode(
        mode_id=data.get("mode_id", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        requires_key=data.get("requires_key", False),
        requires_oidc=data.get("requires_oidc", False),
    )


def _parse_sbom_signer(tool_id: str, data: dict) -> SBOMSignerDef:
    modes = [_parse_signing_mode(m) for m in data.get("signing_modes", [])]
    return SBOMSignerDef(
        tool_id=tool_id,
        name=data.get("name", tool_id),
        description=data.get("description", ""),
        version=data.get("version", ""),
        signing_modes=modes,
        sbom_formats=data.get("sbom_formats", []),
        commands=data.get("commands", []),
        features=data.get("features", []),
        required_tools=data.get("required_tools", []),
    )


def list_sbom_signers() -> list[SBOMSignerDef]:
    raw = _load_security_config().get("sbom_signing", {})
    return [_parse_sbom_signer(k, v) for k, v in raw.items()]


def get_sbom_signer(tool_id: str) -> SBOMSignerDef | None:
    raw = _load_security_config().get("sbom_signing", {})
    if tool_id not in raw:
        return None
    return _parse_sbom_signer(tool_id, raw[tool_id])


# -- Threat model queries --

def _parse_threat_entry(data: dict) -> ThreatEntry:
    return ThreatEntry(
        category=data.get("category", ""),
        threats=data.get("threats", []),
        mitigations=data.get("mitigations", []),
    )


def _parse_threat_model(class_id: str, data: dict) -> ThreatModelDef:
    cats = [_parse_threat_entry(c) for c in data.get("stride_categories", [])]
    return ThreatModelDef(
        class_id=class_id,
        name=data.get("name", class_id),
        stride_categories=cats,
        required_artifacts=data.get("required_artifacts", []),
    )


def list_threat_models() -> list[ThreatModelDef]:
    raw = _load_security_config().get("threat_models", {})
    return [_parse_threat_model(k, v) for k, v in raw.items()]


def get_threat_model(class_id: str) -> ThreatModelDef | None:
    raw = _load_security_config().get("threat_models", {})
    if class_id not in raw:
        return None
    return _parse_threat_model(class_id, raw[class_id])


# -- Test recipe queries --

def _parse_security_test_recipe(data: dict) -> SecurityTestRecipe:
    return SecurityTestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        security_domain=data.get("security_domain", ""),
        tools=data.get("tools", []),
        timeout_s=data.get("timeout_s", 60),
    )


def list_security_test_recipes() -> list[SecurityTestRecipe]:
    raw = _load_security_config().get("test_recipes", [])
    return [_parse_security_test_recipe(r) for r in raw]


def get_security_test_recipe(recipe_id: str) -> SecurityTestRecipe | None:
    for r in list_security_test_recipes():
        if r.recipe_id == recipe_id:
            return r
    return None


def get_recipes_by_domain(domain: str) -> list[SecurityTestRecipe]:
    return [r for r in list_security_test_recipes() if r.security_domain == domain]


# -- Artifact definitions --

def get_artifact_definition(artifact_id: str) -> dict[str, Any] | None:
    raw = _load_security_config().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return {
        "artifact_id": artifact_id,
        "name": d.get("name", artifact_id),
        "description": d.get("description", ""),
        "file_pattern": d.get("file_pattern", ""),
    }


def list_artifact_definitions() -> list[dict[str, Any]]:
    raw = _load_security_config().get("artifact_definitions", {})
    return [
        {
            "artifact_id": k,
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "file_pattern": v.get("file_pattern", ""),
        }
        for k, v in raw.items()
    ]


# -- Secure boot chain verification --

def verify_boot_chain(
    chain_id: str,
    stage_results: list[dict[str, str]] | None = None,
) -> BootChainVerifyResult:
    chain = get_boot_chain(chain_id)
    if chain is None:
        return BootChainVerifyResult(
            chain_id=chain_id,
            overall_status=BootStageStatus.failed,
            message=f"Unknown boot chain: {chain_id!r}. "
                    f"Available: {[c.chain_id for c in list_boot_chains()]}",
        )

    stage_results = stage_results or []
    result_map = {r["stage_id"]: r.get("status", "pending") for r in stage_results}

    verified_stages: list[dict[str, Any]] = []
    all_verified = True

    for stage in chain.stages:
        status_str = result_map.get(stage.stage_id, "pending")
        try:
            status = BootStageStatus(status_str)
        except ValueError:
            status = BootStageStatus.pending

        if status != BootStageStatus.verified:
            all_verified = False

        verified_stages.append({
            "stage_id": stage.stage_id,
            "name": stage.name,
            "status": status.value,
            "signing_algo": stage.signing_algo,
            "rollback_protection": stage.rollback_protection,
        })

    overall = BootStageStatus.verified if all_verified else BootStageStatus.failed
    failed_count = sum(1 for s in verified_stages if s["status"] == "failed")
    pending_count = sum(1 for s in verified_stages if s["status"] == "pending")

    if pending_count > 0 and failed_count == 0:
        overall = BootStageStatus.pending

    msg_parts = []
    if failed_count:
        msg_parts.append(f"{failed_count} stage(s) failed")
    if pending_count:
        msg_parts.append(f"{pending_count} stage(s) pending")
    verified_count = sum(1 for s in verified_stages if s["status"] == "verified")
    msg_parts.append(f"{verified_count}/{len(chain.stages)} verified")

    return BootChainVerifyResult(
        chain_id=chain_id,
        overall_status=overall,
        stage_results=verified_stages,
        message="; ".join(msg_parts),
    )


# -- TEE session simulation --

def simulate_tee_session(
    tee_id: str,
    ta_uuid: str = "00000000-0000-0000-0000-000000000001",
    command_id: int = 0,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tee = get_tee_binding(tee_id)
    if tee is None:
        return {
            "tee_id": tee_id,
            "state": TEESessionState.error.value,
            "message": f"Unknown TEE binding: {tee_id!r}",
        }

    steps = []
    steps.append({
        "step": "TEEC_InitializeContext",
        "status": "ok",
        "description": "TEE context initialized",
    })
    steps.append({
        "step": "TEEC_OpenSession",
        "status": "ok",
        "ta_uuid": ta_uuid,
        "description": f"Session opened with TA {ta_uuid}",
    })
    steps.append({
        "step": "TEEC_InvokeCommand",
        "status": "ok",
        "command_id": command_id,
        "description": f"Command {command_id} invoked",
    })
    steps.append({
        "step": "TEEC_CloseSession",
        "status": "ok",
        "description": "Session closed",
    })
    steps.append({
        "step": "TEEC_FinalizeContext",
        "status": "ok",
        "description": "Context finalized",
    })

    return {
        "tee_id": tee_id,
        "tee_name": tee.name,
        "ta_uuid": ta_uuid,
        "state": TEESessionState.closed.value,
        "steps": steps,
        "message": "TEE session lifecycle completed successfully (simulated)",
    }


# -- Remote attestation --

def generate_attestation_quote(
    provider_id: str,
    nonce: str = "",
    pcr_indices: list[int] | None = None,
) -> AttestationQuote:
    provider = get_attestation_provider(provider_id)
    if provider is None:
        return AttestationQuote(
            provider_id=provider_id,
            status=AttestationStatus.error,
            message=f"Unknown attestation provider: {provider_id!r}. "
                    f"Available: {[p.provider_id for p in list_attestation_providers()]}",
        )

    pcr_indices = pcr_indices or [0, 1, 2, 4, 7]

    pcr_values: dict[int, str] = {}
    for idx in pcr_indices:
        measurement = f"{provider_id}:pcr{idx}:{nonce}".encode()
        pcr_values[idx] = hashlib.sha256(measurement).hexdigest()

    quote_payload = json.dumps({
        "provider": provider_id,
        "nonce": nonce,
        "pcr_values": {str(k): v for k, v in pcr_values.items()},
        "timestamp": time.time(),
    }, sort_keys=True)

    quote_hash = hashlib.sha256(quote_payload.encode()).hexdigest()

    return AttestationQuote(
        provider_id=provider_id,
        status=AttestationStatus.trusted,
        nonce=nonce,
        pcr_values=pcr_values,
        quote_data=quote_hash,
        signature=f"sim-sig:{quote_hash[:32]}",
        message=f"Attestation quote generated (simulated) — {len(pcr_indices)} PCRs measured",
    )


def verify_attestation_quote(
    quote: AttestationQuote,
    expected_pcr_values: dict[int, str] | None = None,
) -> dict[str, Any]:
    if quote.status != AttestationStatus.trusted:
        return {
            "verified": False,
            "reason": f"Quote status is {quote.status.value}, not trusted",
        }

    if expected_pcr_values:
        mismatches = []
        for idx, expected in expected_pcr_values.items():
            actual = quote.pcr_values.get(idx, "")
            if actual != expected:
                mismatches.append({
                    "pcr_index": idx,
                    "expected": expected,
                    "actual": actual,
                })
        if mismatches:
            return {
                "verified": False,
                "reason": "PCR value mismatch",
                "mismatches": mismatches,
            }

    return {
        "verified": True,
        "provider_id": quote.provider_id,
        "pcr_count": len(quote.pcr_values),
        "message": "Attestation quote verified successfully",
    }


# -- SBOM signing --

def sign_sbom(
    tool_id: str,
    sbom_path: str,
    mode: str = "key_pair",
    key_path: str = "",
) -> SBOMSignResult:
    signer = get_sbom_signer(tool_id)
    if signer is None:
        return SBOMSignResult(
            tool_id=tool_id,
            mode=mode,
            sbom_path=sbom_path,
            message=f"Unknown SBOM signer: {tool_id!r}. "
                    f"Available: {[s.tool_id for s in list_sbom_signers()]}",
        )

    valid_modes = [m.mode_id for m in signer.signing_modes]
    if mode not in valid_modes:
        return SBOMSignResult(
            tool_id=tool_id,
            mode=mode,
            sbom_path=sbom_path,
            message=f"Invalid signing mode: {mode!r}. Valid: {valid_modes}",
        )

    mode_def = next(m for m in signer.signing_modes if m.mode_id == mode)
    if mode_def.requires_key and not key_path:
        return SBOMSignResult(
            tool_id=tool_id,
            mode=mode,
            sbom_path=sbom_path,
            message=f"Signing mode {mode!r} requires a key_path",
        )

    binary = shutil.which(tool_id)
    if binary:
        return _exec_sbom_sign(binary, sbom_path, mode, key_path)

    sig_path = f"{sbom_path}.sig"
    content_hash = hashlib.sha256(sbom_path.encode()).hexdigest()

    return SBOMSignResult(
        tool_id=tool_id,
        mode=mode,
        sbom_path=sbom_path,
        signature_path=sig_path,
        success=True,
        transparency_log_entry=f"rekor:{content_hash[:16]}" if mode == "keyless" else "",
        message=f"Stub: SBOM signed with {tool_id} ({mode} mode). "
                f"Signature: {sig_path}",
    )


def _exec_sbom_sign(
    binary: str,
    sbom_path: str,
    mode: str,
    key_path: str,
) -> SBOMSignResult:
    cmd = [binary, "sign-blob", sbom_path]
    if mode == "key_pair" and key_path:
        cmd += ["--key", key_path]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        success = proc.returncode == 0
        return SBOMSignResult(
            tool_id=Path(binary).stem,
            mode=mode,
            sbom_path=sbom_path,
            signature_path=f"{sbom_path}.sig",
            success=success,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return SBOMSignResult(
            tool_id=Path(binary).stem,
            mode=mode,
            sbom_path=sbom_path,
            message="Timeout after 120s",
        )
    except FileNotFoundError:
        return SBOMSignResult(
            tool_id=Path(binary).stem,
            mode=mode,
            sbom_path=sbom_path,
            message=f"Binary not found: {binary}",
        )


# -- Threat model coverage evaluation --

def evaluate_threat_coverage(
    class_id: str,
    provided_mitigations: list[str] | None = None,
) -> ThreatCoverageResult:
    model = get_threat_model(class_id)
    if model is None:
        return ThreatCoverageResult(
            class_id=class_id,
            message_extra=f"Unknown threat model class: {class_id!r}",
        ) if False else ThreatCoverageResult(class_id=class_id)

    provided = set(provided_mitigations or [])
    total = 0
    mitigated = 0
    gaps: list[dict[str, Any]] = []

    for cat in model.stride_categories:
        for threat in cat.threats:
            total += 1
            has_defined_mitigation = len(cat.mitigations) > 0
            has_provided_mitigation = any(
                m.lower() in threat.lower() or threat.lower() in m.lower()
                for m in provided
            ) if provided else False

            if has_defined_mitigation or has_provided_mitigation:
                mitigated += 1
            else:
                gaps.append({
                    "category": cat.category,
                    "threat": threat,
                    "status": "unmitigated",
                })

    coverage_pct = (mitigated / total * 100.0) if total else 0.0

    return ThreatCoverageResult(
        class_id=class_id,
        total_threats=total,
        mitigated_threats=mitigated,
        unmitigated_threats=total - mitigated,
        coverage_pct=round(coverage_pct, 1),
        gaps=gaps,
    )


# -- SoC compatibility check --

def check_soc_security_support(
    soc_id: str,
    features: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "soc_id": soc_id,
        "secure_boot_chains": [],
        "tee_bindings": [],
        "attestation_providers": [],
    }

    soc_lower = soc_id.lower()

    for chain in list_boot_chains():
        if any(soc_lower in s.lower() for s in chain.compatible_socs):
            result["secure_boot_chains"].append(chain.chain_id)

    for tee in list_tee_bindings():
        if any(soc_lower in s.lower() for s in tee.compatible_socs):
            result["tee_bindings"].append(tee.tee_id)

    for prov in list_attestation_providers():
        if any(soc_lower in s.lower() or s.lower() == "any"
               for s in prov.compatible_platforms):
            result["attestation_providers"].append(prov.provider_id)

    result["has_secure_boot"] = len(result["secure_boot_chains"]) > 0
    result["has_tee"] = len(result["tee_bindings"]) > 0
    result["has_attestation"] = len(result["attestation_providers"]) > 0

    return result


# -- Security test stub runner --

def run_security_test(
    recipe_id: str,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> SecurityTestResult:
    recipe = get_security_test_recipe(recipe_id)
    if recipe is None:
        return SecurityTestResult(
            recipe_id=recipe_id,
            security_domain="unknown",
            status=SecurityTestStatus.error,
            target_device=target_device,
            message=f"Unknown recipe: {recipe_id!r}. "
                    f"Available: {[r.recipe_id for r in list_security_test_recipes()]}",
        )

    binary = kwargs.pop("binary", "")
    if binary and shutil.which(binary):
        return _exec_security_binary(
            binary, recipe, target_device,
            work_dir=work_dir, timeout_s=timeout_s, **kwargs,
        )

    return SecurityTestResult(
        recipe_id=recipe_id,
        security_domain=recipe.security_domain,
        status=SecurityTestStatus.pending,
        target_device=target_device,
        measurements={
            "category": recipe.category,
            "tools": recipe.tools,
            "security_domain": recipe.security_domain,
        },
        message=f"Stub: {recipe.name} — awaiting hardware execution. "
                f"Tools needed: {recipe.tools}.",
    )


def _exec_security_binary(
    binary: str,
    recipe: SecurityTestRecipe,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> SecurityTestResult:
    cmd = [
        binary,
        "--domain", recipe.security_domain,
        "--recipe", recipe.recipe_id,
        "--device", target_device,
    ]
    output_file = kwargs.get("output_file", "")
    if output_file:
        cmd += ["--output", output_file]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s, cwd=work_dir,
        )
        passed = proc.returncode == 0
        return SecurityTestResult(
            recipe_id=recipe.recipe_id,
            security_domain=recipe.security_domain,
            status=SecurityTestStatus.passed if passed else SecurityTestStatus.failed,
            target_device=target_device,
            raw_log_path=output_file,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return SecurityTestResult(
            recipe_id=recipe.recipe_id,
            security_domain=recipe.security_domain,
            status=SecurityTestStatus.error,
            target_device=target_device,
            message=f"Timeout after {timeout_s}s",
        )
    except FileNotFoundError:
        return SecurityTestResult(
            recipe_id=recipe.recipe_id,
            security_domain=recipe.security_domain,
            status=SecurityTestStatus.error,
            target_device=target_device,
            message=f"Binary not found: {binary}",
        )


# -- Doc suite generator integration --

_ACTIVE_SEC_CERTS: list[dict[str, Any]] = []


def register_security_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_SEC_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_security_stack_certs() -> list[dict[str, Any]]:
    return list(_ACTIVE_SEC_CERTS)


def clear_security_certs() -> None:
    _ACTIVE_SEC_CERTS.clear()


# -- Cert artifact generator --

def generate_cert_artifacts(
    security_domain: str,
    spec: dict[str, Any] | None = None,
    test_results: list[SecurityTestResult] | None = None,
) -> list[SecurityCertArtifact]:
    spec = spec or {}
    test_results = test_results or []
    provided = set(spec.get("provided_artifacts", []))

    art_defs = list_artifact_definitions()
    artifacts: list[SecurityCertArtifact] = []

    for ad in art_defs:
        aid = ad["artifact_id"]
        status = "provided" if aid in provided else "pending"
        artifacts.append(SecurityCertArtifact(
            artifact_id=aid,
            name=ad["name"],
            security_domain=security_domain,
            status=status,
            description=ad.get("description", ""),
        ))

    return artifacts

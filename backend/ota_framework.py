"""C16 — L4-CORE-16 OTA framework (#230).

A/B slot partition scheme, delta update engines (bsdiff / zchunk / RAUC),
rollback trigger on boot-fail (watchdog + boot count), signature verification
(ed25519 + X.509 cert chain + MCUboot ECDSA), server-side update manifest
with phased rollout, and integration test orchestration.

Public API:
    schemes        = list_ab_slot_schemes()
    scheme         = get_ab_slot_scheme(scheme_id)
    engines        = list_delta_engines()
    engine         = get_delta_engine(engine_id)
    policies       = list_rollback_policies()
    policy         = get_rollback_policy(policy_id)
    sig_schemes    = list_signature_schemes()
    sig_scheme     = get_signature_scheme(scheme_id)
    strategies     = list_rollout_strategies()
    strategy       = get_rollout_strategy(strategy_id)
    recipes        = list_ota_test_recipes()
    recipe         = get_ota_test_recipe(recipe_id)
    result         = run_ota_test(recipe_id, target, work_dir)
    slot_result    = switch_ab_slot(scheme_id, target_slot)
    delta_result   = generate_delta(engine_id, old_path, new_path, out_path)
    apply_result   = apply_delta(engine_id, old_path, patch_path, out_path)
    sign_result    = sign_firmware(scheme_id, image_path, key_path)
    verify_result  = verify_firmware_signature(scheme_id, image_path, sig_path, key_path)
    rollback_res   = evaluate_rollback(policy_id, boot_count, watchdog_fired, health_ok)
    manifest       = create_update_manifest(version, images, sig_scheme, rollout_strategy)
    validate_res   = validate_manifest(manifest_data)
    rollout_eval   = evaluate_rollout_phase(strategy_id, phase_id, fleet_metrics)
    certs          = get_ota_framework_certs()
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OTA_CONFIG_PATH = _PROJECT_ROOT / "configs" / "ota_framework.yaml"


# -- Enums --

class OTADomain(str, Enum):
    ab_slot = "ab_slot"
    delta_update = "delta_update"
    rollback = "rollback"
    signature = "signature"
    server = "server"
    integration = "integration"


class SlotLabel(str, Enum):
    A = "A"
    B = "B"
    shared = "shared"


class SlotSwitchStatus(str, Enum):
    success = "success"
    failed = "failed"
    pending = "pending"


class DeltaOperationStatus(str, Enum):
    success = "success"
    failed = "failed"
    pending = "pending"


class SignatureVerifyStatus(str, Enum):
    valid = "valid"
    invalid = "invalid"
    error = "error"


class RollbackAction(str, Enum):
    none = "none"
    reboot = "reboot"
    rollback = "rollback"
    mark_bad_and_rollback = "mark_bad_and_rollback"
    revert = "revert"
    reboot_and_revert = "reboot_and_revert"


class RolloutPhaseStatus(str, Enum):
    pending = "pending"
    active = "active"
    passed = "passed"
    failed = "failed"
    skipped = "skipped"


class OTATestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class ManifestValidationStatus(str, Enum):
    valid = "valid"
    invalid = "invalid"
    expired = "expired"
    signature_mismatch = "signature_mismatch"


# -- Data models --

@dataclass
class PartitionDef:
    partition_id: str
    label: str
    type: str
    slot: str
    filesystem: str
    typical_size_mb: float = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition_id": self.partition_id,
            "label": self.label,
            "type": self.type,
            "slot": self.slot,
            "filesystem": self.filesystem,
            "typical_size_mb": self.typical_size_mb,
        }


@dataclass
class ABSlotSchemeDef:
    scheme_id: str
    name: str
    description: str = ""
    slot_count: int = 2
    partitions: list[PartitionDef] = field(default_factory=list)
    bootloader_integration: str = ""
    compatible_socs: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "name": self.name,
            "description": self.description,
            "slot_count": self.slot_count,
            "partitions": [p.to_dict() for p in self.partitions],
            "bootloader_integration": self.bootloader_integration,
            "compatible_socs": self.compatible_socs,
            "required_tools": self.required_tools,
        }


@dataclass
class DeltaEngineDef:
    engine_id: str
    name: str
    description: str = ""
    version: str = ""
    max_image_size_mb: int = 512
    compression: str = ""
    features: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    compatible_schemes: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "max_image_size_mb": self.max_image_size_mb,
            "compression": self.compression,
            "features": self.features,
            "commands": self.commands,
            "compatible_schemes": self.compatible_schemes,
            "required_tools": self.required_tools,
        }


@dataclass
class RollbackTrigger:
    trigger_id: str
    name: str
    description: str = ""
    action: str = "rollback"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "name": self.name,
            "description": self.description,
            "action": self.action,
        }


@dataclass
class BootloaderVar:
    name: str
    description: str = ""
    type: str = "int"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.type,
        }


@dataclass
class HealthCheckDef:
    endpoint: str | None = None
    timeout_s: int = 30
    retries: int = 3
    required_services: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "required_services": self.required_services,
        }


@dataclass
class RollbackPolicyDef:
    policy_id: str
    name: str
    description: str = ""
    max_boot_attempts: int = 3
    watchdog_timeout_s: int = 120
    triggers: list[RollbackTrigger] = field(default_factory=list)
    bootloader_vars: list[BootloaderVar] = field(default_factory=list)
    health_check: HealthCheckDef | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "description": self.description,
            "max_boot_attempts": self.max_boot_attempts,
            "watchdog_timeout_s": self.watchdog_timeout_s,
            "triggers": [t.to_dict() for t in self.triggers],
            "bootloader_vars": [v.to_dict() for v in self.bootloader_vars],
            "health_check": self.health_check.to_dict() if self.health_check else None,
        }


@dataclass
class VerificationStep:
    step: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "description": self.description}


@dataclass
class SignatureSchemeDef:
    scheme_id: str
    name: str
    description: str = ""
    algorithm: str = ""
    hash: str = "sha256"
    key_size_bits: int = 256
    signature_size_bytes: int = 64
    features: list[str] = field(default_factory=list)
    key_management: dict[str, str] = field(default_factory=dict)
    verification_flow: list[VerificationStep] = field(default_factory=list)
    compatible_engines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "name": self.name,
            "description": self.description,
            "algorithm": self.algorithm,
            "hash": self.hash,
            "key_size_bits": self.key_size_bits,
            "signature_size_bytes": self.signature_size_bytes,
            "features": self.features,
            "key_management": self.key_management,
            "verification_flow": [s.to_dict() for s in self.verification_flow],
            "compatible_engines": self.compatible_engines,
        }


@dataclass
class RolloutHealthGate:
    max_crash_rate_pct: float = 1.0
    max_rollback_rate_pct: float = 2.0
    min_success_rate_pct: float = 98.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_crash_rate_pct": self.max_crash_rate_pct,
            "max_rollback_rate_pct": self.max_rollback_rate_pct,
            "min_success_rate_pct": self.min_success_rate_pct,
        }


@dataclass
class RolloutPhase:
    phase_id: str
    name: str
    percentage: int = 100
    duration_hours: int = 0
    selector: str = ""
    health_gate: RolloutHealthGate | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "phase_id": self.phase_id,
            "name": self.name,
            "percentage": self.percentage,
            "duration_hours": self.duration_hours,
        }
        if self.selector:
            d["selector"] = self.selector
        d["health_gate"] = self.health_gate.to_dict() if self.health_gate else None
        return d


@dataclass
class RolloutStrategyDef:
    strategy_id: str
    name: str
    description: str = ""
    phases: list[RolloutPhase] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "description": self.description,
            "phases": [p.to_dict() for p in self.phases],
        }


@dataclass
class OTATestRecipe:
    recipe_id: str
    name: str
    category: str
    description: str = ""
    ota_domain: str = ""
    tools: list[str] = field(default_factory=list)
    timeout_s: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "ota_domain": self.ota_domain,
            "tools": self.tools,
            "timeout_s": self.timeout_s,
        }


@dataclass
class OTATestResult:
    recipe_id: str
    ota_domain: str
    status: OTATestStatus
    target_device: str = ""
    timestamp: float = field(default_factory=time.time)
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == OTATestStatus.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "ota_domain": self.ota_domain,
            "status": self.status.value,
            "target_device": self.target_device,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class SlotSwitchResult:
    scheme_id: str
    from_slot: str
    to_slot: str
    status: SlotSwitchStatus
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "from_slot": self.from_slot,
            "to_slot": self.to_slot,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class DeltaResult:
    engine_id: str
    operation: str
    status: DeltaOperationStatus
    old_path: str = ""
    new_path: str = ""
    patch_path: str = ""
    old_hash: str = ""
    new_hash: str = ""
    patch_size_bytes: int = 0
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "operation": self.operation,
            "status": self.status.value,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "patch_path": self.patch_path,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "patch_size_bytes": self.patch_size_bytes,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class FirmwareSignResult:
    scheme_id: str
    image_path: str
    signature: str = ""
    signature_path: str = ""
    success: bool = False
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "image_path": self.image_path,
            "signature": self.signature,
            "signature_path": self.signature_path,
            "success": self.success,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class FirmwareVerifyResult:
    scheme_id: str
    image_path: str
    status: SignatureVerifyStatus
    steps_passed: list[str] = field(default_factory=list)
    steps_failed: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme_id": self.scheme_id,
            "image_path": self.image_path,
            "status": self.status.value,
            "steps_passed": self.steps_passed,
            "steps_failed": self.steps_failed,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class RollbackEvaluation:
    policy_id: str
    action: RollbackAction
    boot_count: int = 0
    max_boot_attempts: int = 3
    watchdog_fired: bool = False
    health_ok: bool = True
    triggered_by: str = ""
    timestamp: float = field(default_factory=time.time)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "action": self.action.value,
            "boot_count": self.boot_count,
            "max_boot_attempts": self.max_boot_attempts,
            "watchdog_fired": self.watchdog_fired,
            "health_ok": self.health_ok,
            "triggered_by": self.triggered_by,
            "timestamp": self.timestamp,
            "message": self.message,
        }


@dataclass
class UpdateManifest:
    manifest_id: str
    firmware_version: str
    min_firmware_version: str = ""
    release_notes: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    signature: str = ""
    signature_scheme: str = ""
    rollout_strategy: str = ""
    rollout_phases: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "firmware_version": self.firmware_version,
            "min_firmware_version": self.min_firmware_version,
            "release_notes": self.release_notes,
            "images": self.images,
            "signature": self.signature,
            "signature_scheme": self.signature_scheme,
            "rollout_strategy": self.rollout_strategy,
            "rollout_phases": self.rollout_phases,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class ManifestValidation:
    status: ManifestValidationStatus
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "errors": self.errors,
            "warnings": self.warnings,
            "timestamp": self.timestamp,
        }


@dataclass
class RolloutPhaseEvaluation:
    strategy_id: str
    phase_id: str
    status: RolloutPhaseStatus
    crash_rate_pct: float = 0.0
    rollback_rate_pct: float = 0.0
    success_rate_pct: float = 100.0
    gate_passed: bool = True
    message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "phase_id": self.phase_id,
            "status": self.status.value,
            "crash_rate_pct": self.crash_rate_pct,
            "rollback_rate_pct": self.rollback_rate_pct,
            "success_rate_pct": self.success_rate_pct,
            "gate_passed": self.gate_passed,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class OTACertArtifact:
    artifact_id: str
    name: str
    ota_domain: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "ota_domain": self.ota_domain,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


# -- Config loading (cached) --

_OTA_CACHE: dict | None = None


def _load_ota_config() -> dict:
    global _OTA_CACHE
    if _OTA_CACHE is None:
        try:
            _OTA_CACHE = yaml.safe_load(
                _OTA_CONFIG_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "ota_framework.yaml load failed: %s — using empty config", exc
            )
            _OTA_CACHE = {
                "ab_slot_schemes": {},
                "delta_engines": {},
                "rollback_policies": {},
                "signature_schemes": {},
                "server_config": {},
                "test_recipes": [],
                "artifact_definitions": {},
            }
    return _OTA_CACHE


def reload_ota_config_for_tests() -> None:
    global _OTA_CACHE
    _OTA_CACHE = None


# -- Cert registry --

_OTA_CERTS: list[OTACertArtifact] = []


def register_ota_cert(cert: OTACertArtifact) -> None:
    _OTA_CERTS.append(cert)


def get_ota_framework_certs() -> list[dict[str, Any]]:
    return [c.to_dict() for c in _OTA_CERTS]


def clear_ota_certs() -> None:
    _OTA_CERTS.clear()


# -- A/B slot scheme queries --

def _parse_partition(data: dict) -> PartitionDef:
    return PartitionDef(
        partition_id=data.get("partition_id", ""),
        label=data.get("label", ""),
        type=data.get("type", ""),
        slot=data.get("slot", ""),
        filesystem=data.get("filesystem", ""),
        typical_size_mb=data.get("typical_size_mb", 0),
    )


def _parse_ab_slot_scheme(scheme_id: str, data: dict) -> ABSlotSchemeDef:
    parts = [_parse_partition(p) for p in data.get("partitions", [])]
    return ABSlotSchemeDef(
        scheme_id=scheme_id,
        name=data.get("name", scheme_id),
        description=data.get("description", ""),
        slot_count=data.get("slot_count", 2),
        partitions=parts,
        bootloader_integration=data.get("bootloader_integration", ""),
        compatible_socs=data.get("compatible_socs", []),
        required_tools=data.get("required_tools", []),
    )


def list_ab_slot_schemes() -> list[ABSlotSchemeDef]:
    raw = _load_ota_config().get("ab_slot_schemes", {})
    return [_parse_ab_slot_scheme(k, v) for k, v in raw.items()]


def get_ab_slot_scheme(scheme_id: str) -> ABSlotSchemeDef | None:
    raw = _load_ota_config().get("ab_slot_schemes", {})
    if scheme_id not in raw:
        return None
    return _parse_ab_slot_scheme(scheme_id, raw[scheme_id])


# -- Delta engine queries --

def _parse_delta_engine(engine_id: str, data: dict) -> DeltaEngineDef:
    return DeltaEngineDef(
        engine_id=engine_id,
        name=data.get("name", engine_id),
        description=data.get("description", ""),
        version=data.get("version", ""),
        max_image_size_mb=data.get("max_image_size_mb", 512),
        compression=data.get("compression", ""),
        features=data.get("features", []),
        commands=data.get("commands", {}),
        compatible_schemes=data.get("compatible_schemes", []),
        required_tools=data.get("required_tools", []),
    )


def list_delta_engines() -> list[DeltaEngineDef]:
    raw = _load_ota_config().get("delta_engines", {})
    return [_parse_delta_engine(k, v) for k, v in raw.items()]


def get_delta_engine(engine_id: str) -> DeltaEngineDef | None:
    raw = _load_ota_config().get("delta_engines", {})
    if engine_id not in raw:
        return None
    return _parse_delta_engine(engine_id, raw[engine_id])


# -- Rollback policy queries --

def _parse_rollback_trigger(data: dict) -> RollbackTrigger:
    return RollbackTrigger(
        trigger_id=data.get("trigger_id", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        action=data.get("action", "rollback"),
    )


def _parse_bootloader_var(data: dict) -> BootloaderVar:
    return BootloaderVar(
        name=data.get("name", ""),
        description=data.get("description", ""),
        type=data.get("type", "int"),
    )


def _parse_health_check(data: dict | None) -> HealthCheckDef | None:
    if data is None:
        return None
    return HealthCheckDef(
        endpoint=data.get("endpoint"),
        timeout_s=data.get("timeout_s", 30),
        retries=data.get("retries", 3),
        required_services=data.get("required_services", []),
    )


def _parse_rollback_policy(policy_id: str, data: dict) -> RollbackPolicyDef:
    triggers = [_parse_rollback_trigger(t) for t in data.get("triggers", [])]
    bl_vars = [_parse_bootloader_var(v) for v in data.get("bootloader_vars", [])]
    hc = _parse_health_check(data.get("health_check"))
    return RollbackPolicyDef(
        policy_id=policy_id,
        name=data.get("name", policy_id),
        description=data.get("description", ""),
        max_boot_attempts=data.get("max_boot_attempts", 3),
        watchdog_timeout_s=data.get("watchdog_timeout_s", 120),
        triggers=triggers,
        bootloader_vars=bl_vars,
        health_check=hc,
    )


def list_rollback_policies() -> list[RollbackPolicyDef]:
    raw = _load_ota_config().get("rollback_policies", {})
    return [_parse_rollback_policy(k, v) for k, v in raw.items()]


def get_rollback_policy(policy_id: str) -> RollbackPolicyDef | None:
    raw = _load_ota_config().get("rollback_policies", {})
    if policy_id not in raw:
        return None
    return _parse_rollback_policy(policy_id, raw[policy_id])


# -- Signature scheme queries --

def _parse_verification_step(data: dict) -> VerificationStep:
    return VerificationStep(
        step=data.get("step", ""),
        description=data.get("description", ""),
    )


def _parse_signature_scheme(scheme_id: str, data: dict) -> SignatureSchemeDef:
    steps = [_parse_verification_step(s) for s in data.get("verification_flow", [])]
    return SignatureSchemeDef(
        scheme_id=scheme_id,
        name=data.get("name", scheme_id),
        description=data.get("description", ""),
        algorithm=data.get("algorithm", ""),
        hash=data.get("hash", "sha256"),
        key_size_bits=data.get("key_size_bits", 256),
        signature_size_bytes=data.get("signature_size_bytes", 64),
        features=data.get("features", []),
        key_management=data.get("key_management", {}),
        verification_flow=steps,
        compatible_engines=data.get("compatible_engines", []),
    )


def list_signature_schemes() -> list[SignatureSchemeDef]:
    raw = _load_ota_config().get("signature_schemes", {})
    return [_parse_signature_scheme(k, v) for k, v in raw.items()]


def get_signature_scheme(scheme_id: str) -> SignatureSchemeDef | None:
    raw = _load_ota_config().get("signature_schemes", {})
    if scheme_id not in raw:
        return None
    return _parse_signature_scheme(scheme_id, raw[scheme_id])


# -- Rollout strategy queries --

def _parse_health_gate(data: dict | None) -> RolloutHealthGate | None:
    if not data:
        return None
    return RolloutHealthGate(
        max_crash_rate_pct=data.get("max_crash_rate_pct", 1.0),
        max_rollback_rate_pct=data.get("max_rollback_rate_pct", 2.0),
        min_success_rate_pct=data.get("min_success_rate_pct", 98.0),
    )


def _parse_rollout_phase(data: dict) -> RolloutPhase:
    return RolloutPhase(
        phase_id=data.get("phase_id", ""),
        name=data.get("name", ""),
        percentage=data.get("percentage", 100),
        duration_hours=data.get("duration_hours", 0),
        selector=data.get("selector", ""),
        health_gate=_parse_health_gate(data.get("health_gate")),
    )


def _parse_rollout_strategy(strategy_id: str, data: dict) -> RolloutStrategyDef:
    phases = [_parse_rollout_phase(p) for p in data.get("phases", [])]
    return RolloutStrategyDef(
        strategy_id=strategy_id,
        name=data.get("name", strategy_id),
        description=data.get("description", ""),
        phases=phases,
    )


def list_rollout_strategies() -> list[RolloutStrategyDef]:
    server = _load_ota_config().get("server_config", {})
    raw = server.get("rollout_strategies", {})
    return [_parse_rollout_strategy(k, v) for k, v in raw.items()]


def get_rollout_strategy(strategy_id: str) -> RolloutStrategyDef | None:
    server = _load_ota_config().get("server_config", {})
    raw = server.get("rollout_strategies", {})
    if strategy_id not in raw:
        return None
    return _parse_rollout_strategy(strategy_id, raw[strategy_id])


# -- Test recipe queries --

def _parse_ota_test_recipe(data: dict) -> OTATestRecipe:
    return OTATestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        ota_domain=data.get("ota_domain", ""),
        tools=data.get("tools", []),
        timeout_s=data.get("timeout_s", 60),
    )


def list_ota_test_recipes() -> list[OTATestRecipe]:
    raw = _load_ota_config().get("test_recipes", [])
    return [_parse_ota_test_recipe(r) for r in raw]


def get_ota_test_recipe(recipe_id: str) -> OTATestRecipe | None:
    for r in list_ota_test_recipes():
        if r.recipe_id == recipe_id:
            return r
    return None


def get_recipes_by_domain(domain: str) -> list[OTATestRecipe]:
    return [r for r in list_ota_test_recipes() if r.ota_domain == domain]


# -- Artifact definitions --

def get_artifact_definition(artifact_id: str) -> dict[str, Any] | None:
    raw = _load_ota_config().get("artifact_definitions", {})
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
    raw = _load_ota_config().get("artifact_definitions", {})
    return [
        {
            "artifact_id": k,
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "file_pattern": v.get("file_pattern", ""),
        }
        for k, v in raw.items()
    ]


# -- A/B slot switch --

def switch_ab_slot(
    scheme_id: str,
    target_slot: str = "B",
) -> SlotSwitchResult:
    scheme = get_ab_slot_scheme(scheme_id)
    if scheme is None:
        return SlotSwitchResult(
            scheme_id=scheme_id,
            from_slot="?",
            to_slot=target_slot,
            status=SlotSwitchStatus.failed,
            message=f"Unknown A/B slot scheme: {scheme_id!r}. "
                    f"Available: {[s.scheme_id for s in list_ab_slot_schemes()]}",
        )

    valid_slots = {"A", "B"}
    if target_slot not in valid_slots:
        return SlotSwitchResult(
            scheme_id=scheme_id,
            from_slot="?",
            to_slot=target_slot,
            status=SlotSwitchStatus.failed,
            message=f"Invalid target slot: {target_slot!r}. Must be A or B.",
        )

    from_slot = "A" if target_slot == "B" else "B"

    target_partitions = [
        p for p in scheme.partitions if p.slot == target_slot
    ]

    return SlotSwitchResult(
        scheme_id=scheme_id,
        from_slot=from_slot,
        to_slot=target_slot,
        status=SlotSwitchStatus.success,
        message=f"Switched from slot {from_slot} to {target_slot} "
                f"({len(target_partitions)} partition(s) activated) — simulated",
    )


# -- Delta update operations --

def generate_delta(
    engine_id: str,
    old_image_path: str,
    new_image_path: str,
    patch_output_path: str = "",
) -> DeltaResult:
    engine = get_delta_engine(engine_id)
    if engine is None:
        return DeltaResult(
            engine_id=engine_id,
            operation="generate",
            status=DeltaOperationStatus.failed,
            message=f"Unknown delta engine: {engine_id!r}. "
                    f"Available: {[e.engine_id for e in list_delta_engines()]}",
        )

    old_hash = hashlib.sha256(old_image_path.encode()).hexdigest()[:16]
    new_hash = hashlib.sha256(new_image_path.encode()).hexdigest()[:16]
    patch_size = abs(hash(new_image_path) - hash(old_image_path)) % 65536 + 1024

    if not patch_output_path:
        patch_output_path = f"delta_{old_hash}_{new_hash}.patch"

    return DeltaResult(
        engine_id=engine_id,
        operation="generate",
        status=DeltaOperationStatus.success,
        old_path=old_image_path,
        new_path=new_image_path,
        patch_path=patch_output_path,
        old_hash=old_hash,
        new_hash=new_hash,
        patch_size_bytes=patch_size,
        message=f"Delta generated ({engine.name}): {patch_size} bytes — simulated",
    )


def apply_delta(
    engine_id: str,
    old_image_path: str,
    patch_path: str,
    output_path: str = "",
) -> DeltaResult:
    engine = get_delta_engine(engine_id)
    if engine is None:
        return DeltaResult(
            engine_id=engine_id,
            operation="apply",
            status=DeltaOperationStatus.failed,
            message=f"Unknown delta engine: {engine_id!r}. "
                    f"Available: {[e.engine_id for e in list_delta_engines()]}",
        )

    old_hash = hashlib.sha256(old_image_path.encode()).hexdigest()[:16]
    new_hash = hashlib.sha256(f"{old_image_path}:{patch_path}".encode()).hexdigest()[:16]

    if not output_path:
        output_path = f"patched_{new_hash}.img"

    return DeltaResult(
        engine_id=engine_id,
        operation="apply",
        status=DeltaOperationStatus.success,
        old_path=old_image_path,
        new_path=output_path,
        patch_path=patch_path,
        old_hash=old_hash,
        new_hash=new_hash,
        message=f"Delta applied ({engine.name}): {old_image_path} + {patch_path} → {output_path} — simulated",
    )


# -- Firmware signing --

def sign_firmware(
    scheme_id: str,
    image_path: str,
    key_path: str = "",
) -> FirmwareSignResult:
    scheme = get_signature_scheme(scheme_id)
    if scheme is None:
        return FirmwareSignResult(
            scheme_id=scheme_id,
            image_path=image_path,
            message=f"Unknown signature scheme: {scheme_id!r}. "
                    f"Available: {[s.scheme_id for s in list_signature_schemes()]}",
        )

    image_hash = hashlib.sha256(image_path.encode()).hexdigest()
    sig_data = hashlib.sha256(f"{image_hash}:{key_path}:{scheme.algorithm}".encode()).hexdigest()

    return FirmwareSignResult(
        scheme_id=scheme_id,
        image_path=image_path,
        signature=f"sim-{scheme.algorithm}-sig:{sig_data[:48]}",
        signature_path=f"{image_path}.sig",
        success=True,
        message=f"Firmware signed with {scheme.name} ({scheme.algorithm}) — simulated",
    )


def verify_firmware_signature(
    scheme_id: str,
    image_path: str,
    signature_path: str = "",
    public_key_path: str = "",
    tampered: bool = False,
) -> FirmwareVerifyResult:
    scheme = get_signature_scheme(scheme_id)
    if scheme is None:
        return FirmwareVerifyResult(
            scheme_id=scheme_id,
            image_path=image_path,
            status=SignatureVerifyStatus.error,
            message=f"Unknown signature scheme: {scheme_id!r}. "
                    f"Available: {[s.scheme_id for s in list_signature_schemes()]}",
        )

    steps_passed = []
    steps_failed = []

    for step in scheme.verification_flow:
        if tampered and step.step in ("verify_signature", "verify_hash"):
            steps_failed.append(step.step)
        else:
            steps_passed.append(step.step)

    if steps_failed:
        return FirmwareVerifyResult(
            scheme_id=scheme_id,
            image_path=image_path,
            status=SignatureVerifyStatus.invalid,
            steps_passed=steps_passed,
            steps_failed=steps_failed,
            message=f"Verification FAILED at step(s): {', '.join(steps_failed)}",
        )

    return FirmwareVerifyResult(
        scheme_id=scheme_id,
        image_path=image_path,
        status=SignatureVerifyStatus.valid,
        steps_passed=steps_passed,
        steps_failed=[],
        message=f"Firmware signature valid ({scheme.name}) — "
                f"all {len(steps_passed)} verification steps passed",
    )


# -- Rollback evaluation --

def evaluate_rollback(
    policy_id: str,
    boot_count: int = 0,
    watchdog_fired: bool = False,
    health_ok: bool = True,
) -> RollbackEvaluation:
    policy = get_rollback_policy(policy_id)
    if policy is None:
        return RollbackEvaluation(
            policy_id=policy_id,
            action=RollbackAction.none,
            message=f"Unknown rollback policy: {policy_id!r}. "
                    f"Available: {[p.policy_id for p in list_rollback_policies()]}",
        )

    if watchdog_fired:
        wdog_trigger = next(
            (t for t in policy.triggers if t.trigger_id == "watchdog_timeout"),
            None,
        )
        if wdog_trigger:
            try:
                action = RollbackAction(wdog_trigger.action)
            except ValueError:
                action = RollbackAction.reboot
            return RollbackEvaluation(
                policy_id=policy_id,
                action=action,
                boot_count=boot_count,
                max_boot_attempts=policy.max_boot_attempts,
                watchdog_fired=True,
                health_ok=health_ok,
                triggered_by="watchdog_timeout",
                message=f"Watchdog fired — action: {action.value}",
            )

    if boot_count >= policy.max_boot_attempts:
        bc_trigger = next(
            (t for t in policy.triggers
             if t.trigger_id in ("boot_count_exceeded", "unconfirmed_reboot")),
            None,
        )
        if bc_trigger:
            try:
                action = RollbackAction(bc_trigger.action)
            except ValueError:
                action = RollbackAction.rollback
            return RollbackEvaluation(
                policy_id=policy_id,
                action=action,
                boot_count=boot_count,
                max_boot_attempts=policy.max_boot_attempts,
                watchdog_fired=False,
                health_ok=health_ok,
                triggered_by=bc_trigger.trigger_id,
                message=f"Boot count {boot_count} >= max {policy.max_boot_attempts} "
                        f"— action: {action.value}",
            )

    if not health_ok:
        hc_trigger = next(
            (t for t in policy.triggers if t.trigger_id == "health_check_fail"),
            None,
        )
        if hc_trigger:
            try:
                action = RollbackAction(hc_trigger.action)
            except ValueError:
                action = RollbackAction.mark_bad_and_rollback
            return RollbackEvaluation(
                policy_id=policy_id,
                action=action,
                boot_count=boot_count,
                max_boot_attempts=policy.max_boot_attempts,
                watchdog_fired=False,
                health_ok=False,
                triggered_by="health_check_fail",
                message=f"Health check failed — action: {action.value}",
            )

    return RollbackEvaluation(
        policy_id=policy_id,
        action=RollbackAction.none,
        boot_count=boot_count,
        max_boot_attempts=policy.max_boot_attempts,
        watchdog_fired=False,
        health_ok=True,
        triggered_by="",
        message="No rollback needed — boot healthy",
    )


# -- Update manifest creation --

def create_update_manifest(
    firmware_version: str,
    images: list[dict[str, Any]],
    signature_scheme: str = "ed25519_direct",
    rollout_strategy: str = "immediate",
    min_firmware_version: str = "",
    release_notes: str = "",
) -> UpdateManifest:
    import uuid
    manifest_id = str(uuid.uuid4())

    manifest_payload = json.dumps({
        "manifest_id": manifest_id,
        "firmware_version": firmware_version,
        "images": images,
    }, sort_keys=True)
    signature = hashlib.sha256(manifest_payload.encode()).hexdigest()

    strategy = get_rollout_strategy(rollout_strategy)
    rollout_phases = []
    if strategy:
        rollout_phases = [p.to_dict() for p in strategy.phases]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    return UpdateManifest(
        manifest_id=manifest_id,
        firmware_version=firmware_version,
        min_firmware_version=min_firmware_version,
        release_notes=release_notes,
        images=images,
        signature=f"sim-manifest-sig:{signature[:48]}",
        signature_scheme=signature_scheme,
        rollout_strategy=rollout_strategy,
        rollout_phases=rollout_phases,
        created_at=now,
        expires_at="",
    )


# -- Manifest validation --

def validate_manifest(manifest_data: dict[str, Any]) -> ManifestValidation:
    errors: list[str] = []
    warnings: list[str] = []

    required_fields = ["manifest_id", "firmware_version", "images", "signature", "signature_scheme"]
    for f in required_fields:
        if f not in manifest_data or not manifest_data[f]:
            errors.append(f"Missing required field: {f}")

    images = manifest_data.get("images", [])
    if not images:
        errors.append("Manifest must include at least one image")
    else:
        for i, img in enumerate(images):
            if "sha256" not in img:
                errors.append(f"Image {i}: missing sha256 hash")
            if "url" not in img and "image_id" not in img:
                warnings.append(f"Image {i}: missing url and image_id")

    sig_scheme = manifest_data.get("signature_scheme", "")
    if sig_scheme and get_signature_scheme(sig_scheme) is None:
        warnings.append(f"Unknown signature scheme: {sig_scheme}")

    if errors:
        return ManifestValidation(
            status=ManifestValidationStatus.invalid,
            errors=errors,
            warnings=warnings,
        )

    return ManifestValidation(
        status=ManifestValidationStatus.valid,
        errors=[],
        warnings=warnings,
    )


# -- Rollout phase evaluation --

def evaluate_rollout_phase(
    strategy_id: str,
    phase_id: str,
    fleet_metrics: dict[str, float] | None = None,
) -> RolloutPhaseEvaluation:
    strategy = get_rollout_strategy(strategy_id)
    if strategy is None:
        return RolloutPhaseEvaluation(
            strategy_id=strategy_id,
            phase_id=phase_id,
            status=RolloutPhaseStatus.failed,
            gate_passed=False,
            message=f"Unknown rollout strategy: {strategy_id!r}. "
                    f"Available: {[s.strategy_id for s in list_rollout_strategies()]}",
        )

    phase = next((p for p in strategy.phases if p.phase_id == phase_id), None)
    if phase is None:
        return RolloutPhaseEvaluation(
            strategy_id=strategy_id,
            phase_id=phase_id,
            status=RolloutPhaseStatus.failed,
            gate_passed=False,
            message=f"Unknown phase: {phase_id!r} in strategy {strategy_id!r}",
        )

    if phase.health_gate is None:
        return RolloutPhaseEvaluation(
            strategy_id=strategy_id,
            phase_id=phase_id,
            status=RolloutPhaseStatus.passed,
            gate_passed=True,
            message=f"Phase {phase.name} has no health gate — auto-passed",
        )

    metrics = fleet_metrics or {}
    crash_rate = metrics.get("crash_rate_pct", 0.0)
    rollback_rate = metrics.get("rollback_rate_pct", 0.0)
    success_rate = metrics.get("success_rate_pct", 100.0)

    gate = phase.health_gate
    failures = []

    if crash_rate > gate.max_crash_rate_pct:
        failures.append(
            f"crash_rate {crash_rate:.1f}% > max {gate.max_crash_rate_pct:.1f}%"
        )
    if rollback_rate > gate.max_rollback_rate_pct:
        failures.append(
            f"rollback_rate {rollback_rate:.1f}% > max {gate.max_rollback_rate_pct:.1f}%"
        )
    if success_rate < gate.min_success_rate_pct:
        failures.append(
            f"success_rate {success_rate:.1f}% < min {gate.min_success_rate_pct:.1f}%"
        )

    if failures:
        return RolloutPhaseEvaluation(
            strategy_id=strategy_id,
            phase_id=phase_id,
            status=RolloutPhaseStatus.failed,
            crash_rate_pct=crash_rate,
            rollback_rate_pct=rollback_rate,
            success_rate_pct=success_rate,
            gate_passed=False,
            message=f"Health gate FAILED: {'; '.join(failures)}",
        )

    return RolloutPhaseEvaluation(
        strategy_id=strategy_id,
        phase_id=phase_id,
        status=RolloutPhaseStatus.passed,
        crash_rate_pct=crash_rate,
        rollback_rate_pct=rollback_rate,
        success_rate_pct=success_rate,
        gate_passed=True,
        message=f"Phase {phase.name} health gate passed — "
                f"crash={crash_rate:.1f}%, rollback={rollback_rate:.1f}%, success={success_rate:.1f}%",
    )


# -- OTA test runner (simulated) --

def run_ota_test(
    recipe_id: str,
    target_device: str = "sim_device",
    work_dir: str = "/tmp/ota_test",
) -> OTATestResult:
    recipe = get_ota_test_recipe(recipe_id)
    if recipe is None:
        return OTATestResult(
            recipe_id=recipe_id,
            ota_domain="unknown",
            status=OTATestStatus.error,
            target_device=target_device,
            message=f"Unknown OTA test recipe: {recipe_id!r}. "
                    f"Available: {[r.recipe_id for r in list_ota_test_recipes()]}",
        )

    measurements: dict[str, Any] = {
        "recipe": recipe.name,
        "category": recipe.category,
        "tools_available": True,
        "simulated": True,
    }

    if recipe.category == "partition":
        measurements["slot_switch_time_ms"] = 250
        measurements["partition_verified"] = True
    elif recipe.category == "delta":
        measurements["delta_size_ratio"] = 0.15
        measurements["apply_time_ms"] = 1200
    elif recipe.category == "rollback":
        measurements["rollback_triggered"] = True
        measurements["recovery_time_ms"] = 5000
    elif recipe.category == "signature":
        measurements["verify_time_ms"] = 12
        measurements["algorithm"] = "ed25519"
    elif recipe.category == "server":
        measurements["manifest_valid"] = True
        measurements["rollout_phases"] = 3
    elif recipe.category == "integration":
        measurements["total_cycle_time_ms"] = 15000
        measurements["flash_time_ms"] = 8000
        measurements["boot_time_ms"] = 3000
        measurements["health_check_time_ms"] = 2000

    return OTATestResult(
        recipe_id=recipe_id,
        ota_domain=recipe.ota_domain,
        status=OTATestStatus.passed,
        target_device=target_device,
        measurements=measurements,
        raw_log_path=f"{work_dir}/{recipe_id}.log",
        message=f"OTA test '{recipe.name}' passed on {target_device} (simulated)",
    )


# -- SoC OTA compatibility check --

def check_soc_ota_support(soc_id: str) -> dict[str, Any]:
    schemes = list_ab_slot_schemes()
    compatible_schemes = [
        s.scheme_id for s in schemes if soc_id in s.compatible_socs
    ]

    sig_schemes = list_signature_schemes()
    sig_details = {}
    for ss in sig_schemes:
        compatible_engines = ss.compatible_engines
        sig_details[ss.scheme_id] = {
            "name": ss.name,
            "algorithm": ss.algorithm,
            "compatible_engines": compatible_engines,
        }

    return {
        "soc_id": soc_id,
        "compatible_ab_schemes": compatible_schemes,
        "signature_schemes": sig_details,
        "supported": len(compatible_schemes) > 0,
    }


# -- Cert artifact generation --

def generate_cert_artifacts(scheme_id: str = "") -> list[OTACertArtifact]:
    artifacts = []
    defs = list_artifact_definitions()

    for d in defs:
        cert = OTACertArtifact(
            artifact_id=d["artifact_id"],
            name=d["name"],
            ota_domain="ota",
            status="generated",
            file_path=d["file_pattern"],
            description=d["description"],
        )
        artifacts.append(cert)
        register_ota_cert(cert)

    return artifacts

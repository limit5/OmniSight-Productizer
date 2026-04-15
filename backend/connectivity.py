"""C13 — L4-CORE-13 Connectivity sub-skill library (#227).

Sub-skill library for connectivity protocols:
  BLE        — GATT + pairing + OTA DFU profile
  WiFi       — STA/AP + provisioning + enterprise auth
  5G         — modem AT / QMI + dual-SIM
  Ethernet   — basic + VLAN + PoE detection
  CAN        — SocketCAN + diagnostics (UDS/OBD-II)
  Modbus     — RTU/TCP master/slave
  OPC-UA     — server/client for industrial automation

Provides:
  - Protocol definition lookup from connectivity_standards.yaml
  - Per-protocol test recipe management
  - Connectivity test stub runners
  - Sub-skill registry with composition rules
  - Checklist validation (spec → required tests + artifacts)
  - get_connectivity_certs() for doc_suite_generator integration

Public API:
    protocols = list_protocols()
    proto     = get_protocol("ble")
    recipes   = get_test_recipes("wifi")
    result    = run_connectivity_test(protocol, recipe_id, target, work_dir)
    check     = validate_connectivity_checklist(spec)
    compose   = resolve_composition(product_type)
    certs     = get_connectivity_certs()
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONNECTIVITY_STANDARDS_PATH = _PROJECT_ROOT / "configs" / "connectivity_standards.yaml"


# -- Enums --

class ConnectivityProtocol(str, Enum):
    ble = "ble"
    wifi = "wifi"
    fiveg = "fiveg"
    ethernet = "ethernet"
    can = "can"
    modbus = "modbus"
    opcua = "opcua"


class TestCategory(str, Enum):
    functional = "functional"
    security = "security"
    performance = "performance"
    provisioning = "provisioning"
    monitoring = "monitoring"
    resilience = "resilience"
    diagnostics = "diagnostics"
    ota = "ota"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


class TransportType(str, Enum):
    wireless = "wireless"
    wired = "wired"
    mixed = "mixed"


class ProtocolLayer(str, Enum):
    link = "link"
    network = "network"
    application = "application"


# -- Data models --

@dataclass
class ConnTestRecipe:
    recipe_id: str
    name: str
    category: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tools": self.tools,
            "reference": self.reference,
        }


@dataclass
class ConnArtifactDef:
    artifact_id: str
    name: str
    description: str = ""
    file_pattern: str = ""


@dataclass
class ProtocolDef:
    protocol_id: str
    name: str
    standard: str
    authority: str
    description: str = ""
    transport: str = "wireless"
    layer: str = "link"
    features: list[str] = field(default_factory=list)
    test_recipes: list[ConnTestRecipe] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    compatible_socs: list[str] = field(default_factory=list)

    def get_recipe(self, recipe_id: str) -> ConnTestRecipe | None:
        for r in self.test_recipes:
            if r.recipe_id == recipe_id:
                return r
        return None

    @property
    def recipe_ids(self) -> list[str]:
        return [r.recipe_id for r in self.test_recipes]

    def recipes_by_category(self, category: str) -> list[ConnTestRecipe]:
        return [r for r in self.test_recipes if r.category == category]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "name": self.name,
            "standard": self.standard,
            "authority": self.authority,
            "description": self.description,
            "transport": self.transport,
            "layer": self.layer,
            "features": self.features,
            "test_recipes": [r.to_dict() for r in self.test_recipes],
            "required_artifacts": self.required_artifacts,
            "compatible_socs": self.compatible_socs,
        }


@dataclass
class ConnTestResult:
    recipe_id: str
    protocol: str
    status: TestStatus
    target_device: str = ""
    timestamp: float = field(default_factory=time.time)
    measurements: dict[str, Any] = field(default_factory=dict)
    raw_log_path: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == TestStatus.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "protocol": self.protocol,
            "status": self.status.value,
            "target_device": self.target_device,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class ChecklistItem:
    item_id: str
    description: str
    category: str
    status: TestStatus = TestStatus.pending
    details: str = ""


@dataclass
class ConnChecklist:
    protocol: str
    protocol_name: str
    items: list[ChecklistItem] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def passed_count(self) -> int:
        return sum(1 for i in self.items if i.status == TestStatus.passed)

    @property
    def pending_count(self) -> int:
        return sum(1 for i in self.items if i.status == TestStatus.pending)

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.status == TestStatus.failed)

    @property
    def complete(self) -> bool:
        return all(
            i.status in (TestStatus.passed, TestStatus.skipped) for i in self.items
        ) and len(self.items) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_name": self.protocol_name,
            "total": self.total,
            "passed": self.passed_count,
            "pending": self.pending_count,
            "failed": self.failed_count,
            "complete": self.complete,
            "timestamp": self.timestamp,
            "items": [
                {
                    "item_id": i.item_id,
                    "description": i.description,
                    "category": i.category,
                    "status": i.status.value,
                    "details": i.details,
                }
                for i in self.items
            ],
        }


@dataclass
class ConnCertArtifact:
    artifact_id: str
    name: str
    protocol: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "protocol": self.protocol,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


@dataclass
class SubSkillDef:
    sub_skill_id: str
    skill_id: str
    protocols: list[str] = field(default_factory=list)
    typical_products: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_skill_id": self.sub_skill_id,
            "skill_id": self.skill_id,
            "protocols": self.protocols,
            "typical_products": self.typical_products,
        }


@dataclass
class CompositionRule:
    name: str
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "optional": self.optional,
        }


@dataclass
class CompositionResult:
    product_type: str
    matched_rule: str | None = None
    required_sub_skills: list[str] = field(default_factory=list)
    optional_sub_skills: list[str] = field(default_factory=list)
    all_protocols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_type": self.product_type,
            "matched_rule": self.matched_rule,
            "required_sub_skills": self.required_sub_skills,
            "optional_sub_skills": self.optional_sub_skills,
            "all_protocols": self.all_protocols,
        }


# -- Config loading (cached) --

_CONN_CACHE: dict | None = None


def _load_connectivity_standards() -> dict:
    global _CONN_CACHE
    if _CONN_CACHE is None:
        try:
            _CONN_CACHE = yaml.safe_load(
                _CONNECTIVITY_STANDARDS_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "connectivity_standards.yaml load failed: %s — using empty config", exc
            )
            _CONN_CACHE = {
                "protocols": {},
                "sub_skill_registry": {},
                "artifact_definitions": {},
            }
    return _CONN_CACHE


def reload_connectivity_standards_for_tests() -> None:
    global _CONN_CACHE
    _CONN_CACHE = None


def _parse_recipe(data: dict) -> ConnTestRecipe:
    return ConnTestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        tools=data.get("tools", []),
        reference=data.get("reference", ""),
    )


def _parse_protocol(protocol_id: str, data: dict) -> ProtocolDef:
    recipes = [_parse_recipe(r) for r in data.get("test_recipes", [])]
    return ProtocolDef(
        protocol_id=protocol_id,
        name=data.get("name", protocol_id),
        standard=data.get("standard", ""),
        authority=data.get("authority", ""),
        description=data.get("description", ""),
        transport=data.get("transport", "wireless"),
        layer=data.get("layer", "link"),
        features=data.get("features", []),
        test_recipes=recipes,
        required_artifacts=data.get("required_artifacts", []),
        compatible_socs=data.get("compatible_socs", []),
    )


# -- Protocol queries --

def get_protocol(protocol_id: str) -> ProtocolDef | None:
    raw = _load_connectivity_standards().get("protocols", {})
    if protocol_id not in raw:
        return None
    return _parse_protocol(protocol_id, raw[protocol_id])


def list_protocols() -> list[ProtocolDef]:
    raw = _load_connectivity_standards().get("protocols", {})
    return [_parse_protocol(k, v) for k, v in raw.items()]


def get_test_recipes(protocol_id: str) -> list[ConnTestRecipe]:
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.test_recipes


def get_protocol_features(protocol_id: str) -> list[str]:
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.features


def get_compatible_socs(protocol_id: str) -> list[str]:
    proto = get_protocol(protocol_id)
    if proto is None:
        return []
    return proto.compatible_socs


# -- Artifact definitions --

def get_artifact_definition(artifact_id: str) -> ConnArtifactDef | None:
    raw = _load_connectivity_standards().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return ConnArtifactDef(
        artifact_id=artifact_id,
        name=d.get("name", artifact_id),
        description=d.get("description", ""),
        file_pattern=d.get("file_pattern", ""),
    )


def list_artifact_definitions() -> list[ConnArtifactDef]:
    raw = _load_connectivity_standards().get("artifact_definitions", {})
    return [
        ConnArtifactDef(
            artifact_id=k,
            name=v.get("name", k),
            description=v.get("description", ""),
            file_pattern=v.get("file_pattern", ""),
        )
        for k, v in raw.items()
    ]


# -- Test stub runners --

def run_connectivity_test(
    protocol_id: str,
    recipe_id: str,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> ConnTestResult:
    """Stub runner for connectivity tests.

    In production this dispatches to the appropriate tool (bluetoothctl,
    wpa_supplicant, mmcli, etc.). Currently returns a pending/stub result.
    """
    proto = get_protocol(protocol_id)
    if proto is None:
        return ConnTestResult(
            recipe_id=recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Unknown protocol: {protocol_id!r}. "
                    f"Available: {[p.protocol_id for p in list_protocols()]}",
        )

    recipe = proto.get_recipe(recipe_id)
    if recipe is None:
        return ConnTestResult(
            recipe_id=recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Unknown recipe: {recipe_id!r}. Available: {proto.recipe_ids}",
        )

    binary = kwargs.pop("binary", "")
    if binary and shutil.which(binary):
        return _exec_connectivity_binary(
            binary, protocol_id, recipe, target_device,
            work_dir=work_dir, timeout_s=timeout_s, **kwargs,
        )

    return ConnTestResult(
        recipe_id=recipe_id,
        protocol=protocol_id,
        status=TestStatus.pending,
        target_device=target_device,
        measurements={
            "category": recipe.category,
            "tools": recipe.tools,
            "features": proto.features,
        },
        message=f"Stub: {recipe.name} — awaiting hardware execution. "
                f"Tools needed: {recipe.tools}. Ref: {recipe.reference}",
    )


def _exec_connectivity_binary(
    binary: str,
    protocol_id: str,
    recipe: ConnTestRecipe,
    target_device: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> ConnTestResult:
    cmd = [
        binary,
        "--protocol", protocol_id,
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
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.passed if passed else TestStatus.failed,
            target_device=target_device,
            raw_log_path=output_file,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Timeout after {timeout_s}s",
        )
    except FileNotFoundError:
        return ConnTestResult(
            recipe_id=recipe.recipe_id,
            protocol=protocol_id,
            status=TestStatus.error,
            target_device=target_device,
            message=f"Binary not found: {binary}",
        )


# -- Sub-skill registry --

def list_sub_skills() -> list[SubSkillDef]:
    raw = _load_connectivity_standards().get("sub_skill_registry", {})
    available = raw.get("available_sub_skills", [])
    return [
        SubSkillDef(
            sub_skill_id=s["id"],
            skill_id=s.get("skill_id", ""),
            protocols=s.get("protocols", []),
            typical_products=s.get("typical_products", []),
        )
        for s in available
    ]


def get_sub_skill(sub_skill_id: str) -> SubSkillDef | None:
    for s in list_sub_skills():
        if s.sub_skill_id == sub_skill_id:
            return s
    return None


def list_composition_rules() -> list[CompositionRule]:
    raw = _load_connectivity_standards().get("sub_skill_registry", {})
    rules = raw.get("composition_rules", [])
    return [
        CompositionRule(
            name=r["name"],
            required=r.get("required", []),
            optional=r.get("optional", []),
        )
        for r in rules
    ]


def resolve_composition(product_type: str) -> CompositionResult:
    """Resolve which sub-skills a product type needs.

    Matches product_type against composition rules by name (case-insensitive,
    underscores/hyphens/spaces normalized). Returns required + optional sub-skills.
    """
    normalized = product_type.lower().replace("-", " ").replace("_", " ")
    rules = list_composition_rules()

    for rule in rules:
        rule_normalized = rule.name.lower().replace("-", " ").replace("_", " ")
        if rule_normalized == normalized:
            all_protos: list[str] = list(rule.required) + list(rule.optional)
            return CompositionResult(
                product_type=product_type,
                matched_rule=rule.name,
                required_sub_skills=list(rule.required),
                optional_sub_skills=list(rule.optional),
                all_protocols=all_protos,
            )

    sub_skills = list_sub_skills()
    for ss in sub_skills:
        if product_type.lower() in [p.lower() for p in ss.typical_products]:
            return CompositionResult(
                product_type=product_type,
                matched_rule=None,
                required_sub_skills=ss.protocols,
                optional_sub_skills=[],
                all_protocols=ss.protocols,
            )

    return CompositionResult(
        product_type=product_type,
        matched_rule=None,
        required_sub_skills=[],
        optional_sub_skills=[],
        all_protocols=[],
    )


# -- Cert artifact generator --

def generate_cert_artifacts(
    protocol_id: str,
    spec: dict[str, Any] | None = None,
    test_results: list[ConnTestResult] | None = None,
) -> list[ConnCertArtifact]:
    proto = get_protocol(protocol_id)
    if proto is None:
        return []

    spec = spec or {}
    test_results = test_results or []
    provided_artifacts = set(spec.get("provided_artifacts", []))

    result_map: dict[str, ConnTestResult] = {
        r.recipe_id: r for r in test_results
    }

    art_defs = {a.artifact_id: a for a in list_artifact_definitions()}
    artifacts: list[ConnCertArtifact] = []

    for art_id in proto.required_artifacts:
        art_def = art_defs.get(art_id)
        name = art_def.name if art_def else art_id
        desc = art_def.description if art_def else ""

        if art_id in provided_artifacts:
            status = "provided"
        else:
            status = "pending"

        artifacts.append(ConnCertArtifact(
            artifact_id=art_id,
            name=name,
            protocol=protocol_id,
            status=status,
            description=desc,
        ))

    return artifacts


# -- Checklist validation --

def validate_connectivity_checklist(
    spec: dict[str, Any],
    test_results: list[ConnTestResult] | None = None,
) -> list[ConnChecklist]:
    target_protocols = spec.get("target_protocols", [])
    if not target_protocols:
        return []

    test_results = test_results or []
    result_map: dict[str, ConnTestResult] = {
        r.recipe_id: r for r in test_results
    }

    checklists: list[ConnChecklist] = []

    for protocol_id in target_protocols:
        proto = get_protocol(protocol_id)
        if proto is None:
            continue

        items: list[ChecklistItem] = []

        for recipe in proto.test_recipes:
            existing = result_map.get(recipe.recipe_id)
            if existing:
                if existing.status == TestStatus.passed:
                    status = TestStatus.passed
                elif existing.status == TestStatus.failed:
                    status = TestStatus.failed
                else:
                    status = TestStatus.pending
            else:
                status = TestStatus.pending

            items.append(ChecklistItem(
                item_id=recipe.recipe_id,
                description=recipe.name,
                category=recipe.category,
                status=status,
                details=recipe.reference,
            ))

        art_defs = {a.artifact_id: a for a in list_artifact_definitions()}
        provided = set(spec.get("provided_artifacts", []))
        for art_id in proto.required_artifacts:
            art_def = art_defs.get(art_id)
            items.append(ChecklistItem(
                item_id=f"artifact:{art_id}",
                description=f"Artifact: {art_def.name if art_def else art_id}",
                category="artifact",
                status=TestStatus.passed if art_id in provided else TestStatus.pending,
            ))

        checklists.append(ConnChecklist(
            protocol=protocol_id,
            protocol_name=proto.name,
            items=items,
        ))

    return checklists


# -- SoC compatibility check --

def check_soc_compatibility(
    soc_id: str,
    protocol_ids: list[str] | None = None,
) -> dict[str, bool]:
    """Check which protocols support a given SoC.

    Empty compatible_socs means universal support.
    """
    protocol_ids = protocol_ids or [p.value for p in ConnectivityProtocol]
    result: dict[str, bool] = {}
    for pid in protocol_ids:
        proto = get_protocol(pid)
        if proto is None:
            result[pid] = False
            continue
        if not proto.compatible_socs:
            result[pid] = True
        else:
            result[pid] = soc_id.lower() in [s.lower() for s in proto.compatible_socs]
    return result


# -- Doc suite generator integration --

_ACTIVE_CONN_CERTS: list[dict[str, Any]] = []


def register_connectivity_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_CONN_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_connectivity_certs() -> list[dict[str, Any]]:
    """Return registered connectivity certs for doc_suite_generator."""
    return list(_ACTIVE_CONN_CERTS)


def clear_connectivity_certs() -> None:
    _ACTIVE_CONN_CERTS.clear()


# -- Audit log integration --

async def log_connectivity_test_result(result: ConnTestResult) -> Optional[int]:
    try:
        from backend import audit
        entity_id = f"{result.protocol}:{result.recipe_id}"
        return await audit.log(
            action="connectivity_test",
            entity_kind="connectivity_test_result",
            entity_id=entity_id,
            before=None,
            after=result.to_dict(),
            actor="connectivity",
        )
    except Exception as exc:
        logger.warning("Failed to log connectivity test result to audit: %s", exc)
        return None


def log_connectivity_test_result_sync(result: ConnTestResult) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_connectivity_test_result_sync skipped (no running loop)")
        return
    loop.create_task(log_connectivity_test_result(result))

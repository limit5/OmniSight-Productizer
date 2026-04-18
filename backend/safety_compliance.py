"""C9 — L4-CORE-09 Safety & compliance framework (#223).

Rule library for functional-safety standards:
  ISO 26262 (ASIL A-D)  — automotive
  IEC 60601 (SW-A/B/C)  — medical devices
  DO-178C   (DAL A-E)   — airborne systems
  IEC 61508 (SIL 1-4)   — E/E/PE systems

Each rule is a DAG validator that checks required artifacts and
required task types. A DAG that claims compliance with a given
standard+level must include all mandated artifacts and task types
or the gate rejects it.

Public API:
    result = check_compliance(dag, standard, level, artifacts)
    result = validate_safety_gate(dag, standard, level, artifacts)
    certs  = get_safety_certs()  # for doc_suite_generator integration
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STANDARDS_PATH = _PROJECT_ROOT / "configs" / "safety_standards.yaml"


# ── Enums ──────────────────────────────────────────────────────────────

class SafetyStandard(str, Enum):
    iso26262 = "iso26262"
    iec60601 = "iec60601"
    do178 = "do178"
    iec61508 = "iec61508"


class GateVerdict(str, Enum):
    passed = "passed"
    failed = "failed"
    error = "error"


# ── Data models ────────────────────────────────────────────────────────

@dataclass
class ArtifactDefinition:
    artifact_id: str
    name: str
    description: str = ""
    file_pattern: str = ""


@dataclass
class SafetyLevel:
    level_id: str
    name: str
    description: str = ""
    required_artifacts: list[str] = field(default_factory=list)
    required_dag_tasks: list[str] = field(default_factory=list)
    review_required: bool = False


@dataclass
class SafetyStandardDef:
    standard_id: str
    name: str
    domain: str
    levels: list[SafetyLevel] = field(default_factory=list)

    def get_level(self, level_id: str) -> SafetyLevel | None:
        for lv in self.levels:
            if lv.level_id == level_id:
                return lv
        return None

    @property
    def level_ids(self) -> list[str]:
        return [lv.level_id for lv in self.levels]


@dataclass
class GateFinding:
    category: str
    item: str
    message: str


@dataclass
class SafetyGateResult:
    standard: str
    level: str
    verdict: GateVerdict
    timestamp: float = field(default_factory=time.time)
    missing_artifacts: list[str] = field(default_factory=list)
    missing_tasks: list[str] = field(default_factory=list)
    findings: list[GateFinding] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.passed

    @property
    def total_issues(self) -> int:
        return len(self.missing_artifacts) + len(self.missing_tasks) + len(self.findings)

    def summary(self) -> str:
        if self.passed:
            return f"Safety gate PASSED for {self.standard} {self.level}"
        parts = []
        if self.missing_artifacts:
            parts.append(f"{len(self.missing_artifacts)} missing artifact(s)")
        if self.missing_tasks:
            parts.append(f"{len(self.missing_tasks)} missing task type(s)")
        if self.findings:
            parts.append(f"{len(self.findings)} additional finding(s)")
        return (
            f"Safety gate FAILED for {self.standard} {self.level}: "
            + ", ".join(parts)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "standard": self.standard,
            "level": self.level,
            "verdict": self.verdict.value,
            "timestamp": self.timestamp,
            "passed": self.passed,
            "total_issues": self.total_issues,
            "missing_artifacts": self.missing_artifacts,
            "missing_tasks": self.missing_tasks,
            "findings": [
                {"category": f.category, "item": f.item, "message": f.message}
                for f in self.findings
            ],
            "metadata": self.metadata,
        }


# ── Config loading (cached) ───────────────────────────────────────────

_STANDARDS_CACHE: dict | None = None


def _load_standards() -> dict:
    global _STANDARDS_CACHE
    if _STANDARDS_CACHE is None:
        try:
            _STANDARDS_CACHE = yaml.safe_load(
                _STANDARDS_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "safety_standards.yaml load failed: %s — using empty rules", exc
            )
            _STANDARDS_CACHE = {"standards": {}, "artifact_definitions": {}}
    return _STANDARDS_CACHE


def reload_standards_for_tests() -> None:
    global _STANDARDS_CACHE
    _STANDARDS_CACHE = None


def _parse_standard(std_id: str, data: dict) -> SafetyStandardDef:
    levels = []
    for lv_data in data.get("levels", []):
        levels.append(SafetyLevel(
            level_id=lv_data["id"],
            name=lv_data.get("name", lv_data["id"]),
            description=lv_data.get("description", ""),
            required_artifacts=lv_data.get("required_artifacts", []),
            required_dag_tasks=lv_data.get("required_dag_tasks", []),
            review_required=lv_data.get("review_required", False),
        ))
    return SafetyStandardDef(
        standard_id=std_id,
        name=data.get("name", std_id),
        domain=data.get("domain", ""),
        levels=levels,
    )


def get_standard(standard: str) -> SafetyStandardDef | None:
    raw = _load_standards().get("standards", {})
    if standard not in raw:
        return None
    return _parse_standard(standard, raw[standard])


def list_standards() -> list[SafetyStandardDef]:
    raw = _load_standards().get("standards", {})
    return [_parse_standard(k, v) for k, v in raw.items()]


def get_artifact_definition(artifact_id: str) -> ArtifactDefinition | None:
    raw = _load_standards().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return ArtifactDefinition(
        artifact_id=artifact_id,
        name=d.get("name", artifact_id),
        description=d.get("description", ""),
        file_pattern=d.get("file_pattern", ""),
    )


def list_artifact_definitions() -> list[ArtifactDefinition]:
    raw = _load_standards().get("artifact_definitions", {})
    return [
        ArtifactDefinition(
            artifact_id=k,
            name=v.get("name", k),
            description=v.get("description", ""),
            file_pattern=v.get("file_pattern", ""),
        )
        for k, v in raw.items()
    ]


# ── DAG safety validator ──────────────────────────────────────────────

def _normalize_level(standard: str, level: str) -> str:
    """Accept common shorthand and normalise to the YAML id form."""
    mapping: dict[str, dict[str, str]] = {
        "iso26262": {
            "a": "ASIL_A", "b": "ASIL_B", "c": "ASIL_C", "d": "ASIL_D",
            "asil_a": "ASIL_A", "asil_b": "ASIL_B",
            "asil_c": "ASIL_C", "asil_d": "ASIL_D",
            "asil-a": "ASIL_A", "asil-b": "ASIL_B",
            "asil-c": "ASIL_C", "asil-d": "ASIL_D",
        },
        "iec60601": {
            "a": "SW_A", "b": "SW_B", "c": "SW_C",
            "sw_a": "SW_A", "sw_b": "SW_B", "sw_c": "SW_C",
            "sw-a": "SW_A", "sw-b": "SW_B", "sw-c": "SW_C",
        },
        "do178": {
            "a": "DAL_A", "b": "DAL_B", "c": "DAL_C",
            "d": "DAL_D", "e": "DAL_E",
            "dal_a": "DAL_A", "dal_b": "DAL_B", "dal_c": "DAL_C",
            "dal_d": "DAL_D", "dal_e": "DAL_E",
            "dal-a": "DAL_A", "dal-b": "DAL_B", "dal-c": "DAL_C",
            "dal-d": "DAL_D", "dal-e": "DAL_E",
        },
        "iec61508": {
            "1": "SIL_1", "2": "SIL_2", "3": "SIL_3", "4": "SIL_4",
            "sil_1": "SIL_1", "sil_2": "SIL_2",
            "sil_3": "SIL_3", "sil_4": "SIL_4",
            "sil-1": "SIL_1", "sil-2": "SIL_2",
            "sil-3": "SIL_3", "sil-4": "SIL_4",
        },
    }
    std_map = mapping.get(standard, {})
    return std_map.get(level.lower(), level)


def _extract_task_types(dag: Any) -> set[str]:
    """Extract task type keywords from DAG task descriptions and IDs."""
    types: set[str] = set()
    for task in dag.tasks:
        tid_lower = task.task_id.lower()
        desc_lower = task.description.lower()
        combined = tid_lower + " " + desc_lower
        keyword_map = {
            "static_analysis": ["static_analysis", "static-analysis", "lint", "sast"],
            "unit_test": ["unit_test", "unit-test", "unittest"],
            "integration_test": ["integration_test", "integration-test", "integ_test"],
            "code_review": ["code_review", "code-review", "peer_review"],
            "coverage_analysis": ["coverage_analysis", "coverage-analysis", "code_coverage"],
            "runtime_verification": ["runtime_verification", "runtime-verification", "runtime_check"],
            "formal_verification": ["formal_verification", "formal-verification", "formal_proof"],
            "fault_injection_test": ["fault_injection", "fault-injection", "fuzz_test", "chaos_test"],
            "regression_test": ["regression_test", "regression-test"],
            "penetration_test": ["penetration_test", "penetration-test", "pentest", "security_test"],
            "mc_dc_coverage": ["mc_dc", "mcdc", "mc-dc", "modified_condition"],
        }
        for task_type, keywords in keyword_map.items():
            if any(kw in combined for kw in keywords):
                types.add(task_type)
    return types


def validate_safety_gate(
    dag: Any,
    standard: str,
    level: str,
    artifacts: list[str] | None = None,
) -> SafetyGateResult:
    """Validate a DAG against a safety standard + level.

    Args:
        dag: A DAG object (from dag_schema) with .tasks attribute.
        standard: Standard key (e.g. "iso26262").
        level: Level key or shorthand (e.g. "B", "ASIL_B").
        artifacts: List of artifact IDs that have been produced.

    Returns:
        SafetyGateResult with verdict, missing artifacts/tasks, findings.
    """
    if artifacts is None:
        artifacts = []

    std_def = get_standard(standard)
    if std_def is None:
        return SafetyGateResult(
            standard=standard,
            level=level,
            verdict=GateVerdict.error,
            findings=[GateFinding(
                category="config",
                item=standard,
                message=f"Unknown safety standard: {standard!r}. "
                        f"Available: {[s.standard_id for s in list_standards()]}",
            )],
        )

    normalised_level = _normalize_level(standard, level)
    level_def = std_def.get_level(normalised_level)
    if level_def is None:
        return SafetyGateResult(
            standard=standard,
            level=level,
            verdict=GateVerdict.error,
            findings=[GateFinding(
                category="config",
                item=level,
                message=f"Unknown level {level!r} for {standard}. "
                        f"Available: {std_def.level_ids}",
            )],
        )

    artifact_set = set(artifacts)
    missing_artifacts = [
        a for a in level_def.required_artifacts if a not in artifact_set
    ]

    dag_task_types = _extract_task_types(dag)
    missing_tasks = [
        t for t in level_def.required_dag_tasks if t not in dag_task_types
    ]

    findings: list[GateFinding] = []

    if level_def.review_required:
        has_review = "code_review" in dag_task_types
        if not has_review:
            findings.append(GateFinding(
                category="process",
                item="review_required",
                message=f"{std_def.name} {level_def.name} requires mandatory "
                        f"code review — no review task found in DAG",
            ))

    if not dag.tasks:
        findings.append(GateFinding(
            category="structure",
            item="empty_dag",
            message="DAG has no tasks — cannot satisfy any safety requirements",
        ))

    has_issues = bool(missing_artifacts or missing_tasks or findings)
    verdict = GateVerdict.failed if has_issues else GateVerdict.passed

    return SafetyGateResult(
        standard=standard,
        level=normalised_level,
        verdict=verdict,
        missing_artifacts=missing_artifacts,
        missing_tasks=missing_tasks,
        findings=findings,
        metadata={
            "standard_name": std_def.name,
            "level_name": level_def.name,
            "domain": std_def.domain,
            "total_required_artifacts": len(level_def.required_artifacts),
            "total_required_tasks": len(level_def.required_dag_tasks),
            "provided_artifacts": sorted(artifacts),
            "detected_task_types": sorted(dag_task_types),
        },
    )


def check_compliance(
    dag: Any,
    standard: str,
    level: str,
    artifacts: list[str] | None = None,
) -> SafetyGateResult:
    """Alias for validate_safety_gate (CLI-friendly name)."""
    return validate_safety_gate(dag, standard, level, artifacts)


# ── Multi-standard check ──────────────────────────────────────────────

def check_all_standards(
    dag: Any,
    requirements: list[dict[str, str]],
    artifacts: list[str] | None = None,
) -> list[SafetyGateResult]:
    """Check a DAG against multiple standards at once.

    Args:
        dag: DAG object.
        requirements: List of {"standard": ..., "level": ...} dicts.
        artifacts: Shared artifact list.

    Returns:
        List of SafetyGateResult, one per requirement.
    """
    return [
        validate_safety_gate(dag, r["standard"], r["level"], artifacts)
        for r in requirements
    ]


# ── Doc suite generator integration ──────────────────────────────────

_ACTIVE_CERTS: list[dict[str, Any]] = []


def register_safety_cert(
    standard: str,
    level: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_CERTS.append({
        "standard": f"{standard} {level}",
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_safety_certs() -> list[dict[str, Any]]:
    """Return registered safety certs for doc_suite_generator."""
    return list(_ACTIVE_CERTS)


def clear_safety_certs() -> None:
    _ACTIVE_CERTS.clear()


# ── Audit log integration ────────────────────────────────────────────

async def log_safety_gate_result(result: SafetyGateResult) -> Optional[int]:
    try:
        from backend import audit
        return await audit.log(
            action="safety_gate_check",
            entity_kind="safety_gate_result",
            entity_id=f"{result.standard}:{result.level}",
            before=None,
            after=result.to_dict(),
            actor="safety_compliance",
        )
    except Exception as exc:
        logger.warning("Failed to log safety gate result to audit: %s", exc)
        return None


def log_safety_gate_result_sync(result: SafetyGateResult) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_safety_gate_result_sync skipped (no running loop)")
        return
    loop.create_task(log_safety_gate_result(result))

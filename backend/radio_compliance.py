"""C10 — L4-CORE-10 Radio certification pre-compliance (#224).

Test recipe library for radio certification standards:
  FCC Part 15  — United States
  CE RED       — European Union
  NCC LPD      — Taiwan
  SRRC SRD     — China

Provides:
  - Per-region test recipe lookup
  - Conducted + radiated emissions stub runners
  - SAR test hook (operator-uploads SAR result file)
  - Per-region cert artifact generator
  - Checklist validation (spec → required tests + artifacts)
  - get_radio_certs() for doc_suite_generator integration

Public API:
    recipes = get_test_recipes("fcc")
    result  = run_emissions_test(region, recipe_id, device, work_dir)
    sar     = upload_sar_result(region, file_path, metadata)
    arts    = generate_cert_artifacts(region, spec, results)
    check   = validate_radio_checklist(spec)
    certs   = get_radio_certs()
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
_RADIO_STANDARDS_PATH = _PROJECT_ROOT / "configs" / "radio_standards.yaml"


# -- Enums --

class RadioRegion(str, Enum):
    fcc = "fcc"
    ce_red = "ce_red"
    ncc_lpd = "ncc_lpd"
    srrc_srd = "srrc_srd"


class EmissionsCategory(str, Enum):
    conducted = "conducted"
    radiated = "radiated"
    sar = "sar"
    receiver = "receiver"


class TestStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    pending = "pending"
    skipped = "skipped"
    error = "error"


# -- Data models --

@dataclass
class TestRecipe:
    recipe_id: str
    name: str
    category: str
    description: str = ""
    frequency_range_mhz: list[float] = field(default_factory=list)
    reference: str = ""
    equipment: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "frequency_range_mhz": self.frequency_range_mhz,
            "reference": self.reference,
            "equipment": self.equipment,
            "limits": self.limits,
        }


@dataclass
class RadioArtifactDef:
    artifact_id: str
    name: str
    description: str = ""
    file_pattern: str = ""


@dataclass
class RadioRegionDef:
    region_id: str
    name: str
    authority: str
    region: str
    description: str = ""
    test_recipes: list[TestRecipe] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)

    def get_recipe(self, recipe_id: str) -> TestRecipe | None:
        for r in self.test_recipes:
            if r.recipe_id == recipe_id:
                return r
        return None

    @property
    def recipe_ids(self) -> list[str]:
        return [r.recipe_id for r in self.test_recipes]

    def recipes_by_category(self, category: str) -> list[TestRecipe]:
        return [r for r in self.test_recipes if r.category == category]


@dataclass
class EmissionsTestResult:
    recipe_id: str
    region: str
    status: TestStatus
    device_under_test: str = ""
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
            "region": self.region,
            "status": self.status.value,
            "device_under_test": self.device_under_test,
            "timestamp": self.timestamp,
            "measurements": self.measurements,
            "raw_log_path": self.raw_log_path,
            "message": self.message,
        }


@dataclass
class SARResult:
    region: str
    status: TestStatus
    file_path: str = ""
    timestamp: float = field(default_factory=time.time)
    peak_sar_w_kg: float = 0.0
    limit_w_kg: float = 0.0
    averaging_mass_g: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.status == TestStatus.passed

    @property
    def within_limit(self) -> bool:
        if self.limit_w_kg <= 0:
            return False
        return self.peak_sar_w_kg <= self.limit_w_kg

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "status": self.status.value,
            "file_path": self.file_path,
            "timestamp": self.timestamp,
            "peak_sar_w_kg": self.peak_sar_w_kg,
            "limit_w_kg": self.limit_w_kg,
            "averaging_mass_g": self.averaging_mass_g,
            "within_limit": self.within_limit,
            "metadata": self.metadata,
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
class RadioChecklist:
    region: str
    region_name: str
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
        return all(i.status in (TestStatus.passed, TestStatus.skipped) for i in self.items) and len(self.items) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "region_name": self.region_name,
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
class CertArtifact:
    artifact_id: str
    name: str
    region: str
    status: str = "pending"
    file_path: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "region": self.region,
            "status": self.status,
            "file_path": self.file_path,
            "description": self.description,
        }


# -- Config loading (cached) --

_RADIO_CACHE: dict | None = None


def _load_radio_standards() -> dict:
    global _RADIO_CACHE
    if _RADIO_CACHE is None:
        try:
            _RADIO_CACHE = yaml.safe_load(
                _RADIO_STANDARDS_PATH.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning(
                "radio_standards.yaml load failed: %s — using empty config", exc
            )
            _RADIO_CACHE = {"regions": {}, "artifact_definitions": {}}
    return _RADIO_CACHE


def reload_radio_standards_for_tests() -> None:
    global _RADIO_CACHE
    _RADIO_CACHE = None


def _parse_recipe(data: dict) -> TestRecipe:
    limits: dict[str, Any] = {}
    for k in ("limits_db_uv", "limits_db_uv_m", "limit_w_kg", "averaging_mass_g"):
        if k in data:
            limits[k] = data[k]
    return TestRecipe(
        recipe_id=data["id"],
        name=data.get("name", data["id"]),
        category=data.get("category", ""),
        description=data.get("description", ""),
        frequency_range_mhz=data.get("frequency_range_mhz", []),
        reference=data.get("reference", ""),
        equipment=data.get("equipment", []),
        limits=limits,
    )


def _parse_region(region_id: str, data: dict) -> RadioRegionDef:
    recipes = [_parse_recipe(r) for r in data.get("test_recipes", [])]
    return RadioRegionDef(
        region_id=region_id,
        name=data.get("name", region_id),
        authority=data.get("authority", ""),
        region=data.get("region", ""),
        description=data.get("description", ""),
        test_recipes=recipes,
        required_artifacts=data.get("required_artifacts", []),
    )


def get_region(region_id: str) -> RadioRegionDef | None:
    raw = _load_radio_standards().get("regions", {})
    if region_id not in raw:
        return None
    return _parse_region(region_id, raw[region_id])


def list_regions() -> list[RadioRegionDef]:
    raw = _load_radio_standards().get("regions", {})
    return [_parse_region(k, v) for k, v in raw.items()]


def get_test_recipes(region_id: str) -> list[TestRecipe]:
    reg = get_region(region_id)
    if reg is None:
        return []
    return reg.test_recipes


def get_artifact_definition(artifact_id: str) -> RadioArtifactDef | None:
    raw = _load_radio_standards().get("artifact_definitions", {})
    if artifact_id not in raw:
        return None
    d = raw[artifact_id]
    return RadioArtifactDef(
        artifact_id=artifact_id,
        name=d.get("name", artifact_id),
        description=d.get("description", ""),
        file_pattern=d.get("file_pattern", ""),
    )


def list_artifact_definitions() -> list[RadioArtifactDef]:
    raw = _load_radio_standards().get("artifact_definitions", {})
    return [
        RadioArtifactDef(
            artifact_id=k,
            name=v.get("name", k),
            description=v.get("description", ""),
            file_pattern=v.get("file_pattern", ""),
        )
        for k, v in raw.items()
    ]


# -- Emissions stub runners --

def run_emissions_test(
    region_id: str,
    recipe_id: str,
    device_target: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> EmissionsTestResult:
    """Stub runner for conducted/radiated emissions tests.

    In production this shells out to the lab equipment control software.
    Currently returns a pending/stub result for pre-compliance planning.
    """
    reg = get_region(region_id)
    if reg is None:
        return EmissionsTestResult(
            recipe_id=recipe_id,
            region=region_id,
            status=TestStatus.error,
            device_under_test=device_target,
            message=f"Unknown region: {region_id!r}. Available: {[r.region_id for r in list_regions()]}",
        )

    recipe = reg.get_recipe(recipe_id)
    if recipe is None:
        return EmissionsTestResult(
            recipe_id=recipe_id,
            region=region_id,
            status=TestStatus.error,
            device_under_test=device_target,
            message=f"Unknown recipe: {recipe_id!r}. Available: {reg.recipe_ids}",
        )

    if recipe.category == "sar":
        return EmissionsTestResult(
            recipe_id=recipe_id,
            region=region_id,
            status=TestStatus.error,
            device_under_test=device_target,
            message="SAR tests require operator upload — use upload_sar_result() instead",
        )

    binary = kwargs.pop("binary", "")
    if binary and shutil.which(binary):
        return _exec_emissions_binary(
            binary, region_id, recipe, device_target,
            work_dir=work_dir, timeout_s=timeout_s, **kwargs,
        )

    return EmissionsTestResult(
        recipe_id=recipe_id,
        region=region_id,
        status=TestStatus.pending,
        device_under_test=device_target,
        measurements={
            "category": recipe.category,
            "frequency_range_mhz": recipe.frequency_range_mhz,
            "limits": recipe.limits,
        },
        message=f"Stub: {recipe.name} — awaiting lab execution. "
                f"Equipment needed: {recipe.equipment}. Ref: {recipe.reference}",
    )


def _exec_emissions_binary(
    binary: str,
    region_id: str,
    recipe: TestRecipe,
    device_target: str,
    *,
    work_dir: str | None = None,
    timeout_s: int = 600,
    **kwargs: Any,
) -> EmissionsTestResult:
    cmd = [
        binary,
        "--region", region_id,
        "--recipe", recipe.recipe_id,
        "--device", device_target,
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
        return EmissionsTestResult(
            recipe_id=recipe.recipe_id,
            region=region_id,
            status=TestStatus.passed if passed else TestStatus.failed,
            device_under_test=device_target,
            raw_log_path=output_file,
            message=proc.stdout[:500] if proc.stdout else proc.stderr[:500],
        )
    except subprocess.TimeoutExpired:
        return EmissionsTestResult(
            recipe_id=recipe.recipe_id,
            region=region_id,
            status=TestStatus.error,
            device_under_test=device_target,
            message=f"Timeout after {timeout_s}s",
        )
    except FileNotFoundError:
        return EmissionsTestResult(
            recipe_id=recipe.recipe_id,
            region=region_id,
            status=TestStatus.error,
            device_under_test=device_target,
            message=f"Binary not found: {binary}",
        )


# -- SAR test hook --

def upload_sar_result(
    region_id: str,
    file_path: str,
    *,
    peak_sar_w_kg: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> SARResult:
    """Hook for operator to upload SAR test result file.

    The SAR test itself is performed by an accredited lab. This hook
    ingests the result file and validates against the region's limit.
    """
    reg = get_region(region_id)
    if reg is None:
        return SARResult(
            region=region_id,
            status=TestStatus.error,
            message=f"Unknown region: {region_id!r}",
        )

    sar_recipes = reg.recipes_by_category("sar")
    if not sar_recipes:
        return SARResult(
            region=region_id,
            status=TestStatus.error,
            message=f"No SAR recipe defined for region {region_id!r}",
        )

    sar_recipe = sar_recipes[0]
    limit = sar_recipe.limits.get("limit_w_kg", 0.0)
    avg_mass = sar_recipe.limits.get("averaging_mass_g", 0.0)

    fp = Path(file_path)
    if not fp.exists():
        return SARResult(
            region=region_id,
            status=TestStatus.error,
            file_path=file_path,
            message=f"SAR result file not found: {file_path}",
        )

    if peak_sar_w_kg <= 0:
        peak_sar_w_kg = _parse_sar_value_from_file(fp)

    if peak_sar_w_kg <= 0:
        return SARResult(
            region=region_id,
            status=TestStatus.pending,
            file_path=file_path,
            peak_sar_w_kg=0.0,
            limit_w_kg=limit,
            averaging_mass_g=avg_mass,
            metadata=metadata or {},
            message="SAR file uploaded but peak value could not be extracted — operator review needed",
        )

    within = peak_sar_w_kg <= limit if limit > 0 else False
    return SARResult(
        region=region_id,
        status=TestStatus.passed if within else TestStatus.failed,
        file_path=file_path,
        peak_sar_w_kg=peak_sar_w_kg,
        limit_w_kg=limit,
        averaging_mass_g=avg_mass,
        metadata=metadata or {},
        message=f"SAR {peak_sar_w_kg:.3f} W/kg vs limit {limit:.1f} W/kg — "
                + ("PASS" if within else "FAIL"),
    )


def _parse_sar_value_from_file(fp: Path) -> float:
    """Best-effort extraction of peak SAR from common report formats."""
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0.0

    if fp.suffix == ".json":
        try:
            data = json.loads(content)
            for key in ("peak_sar", "peak_sar_w_kg", "sar_value", "sar"):
                if key in data:
                    return float(data[key])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    import re
    patterns = [
        r"peak[\s_]*sar[\s:=]*(\d+\.?\d*)\s*[Ww]/[Kk][Gg]",
        r"sar[\s_]*value[\s:=]*(\d+\.?\d*)",
        r"(\d+\.?\d*)\s*[Ww]/[Kk][Gg]",
    ]
    for pat in patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue

    return 0.0


# -- Per-region cert artifact generator --

def generate_cert_artifacts(
    region_id: str,
    spec: dict[str, Any] | None = None,
    test_results: list[EmissionsTestResult] | None = None,
    sar_result: SARResult | None = None,
) -> list[CertArtifact]:
    """Generate the list of required certification artifacts for a region.

    Returns a checklist of artifacts with their status based on what
    has been provided vs what the region requires.
    """
    reg = get_region(region_id)
    if reg is None:
        return []

    spec = spec or {}
    test_results = test_results or []
    provided_artifacts = set(spec.get("provided_artifacts", []))

    result_map: dict[str, EmissionsTestResult] = {
        r.recipe_id: r for r in test_results
    }

    art_defs = {a.artifact_id: a for a in list_artifact_definitions()}
    artifacts: list[CertArtifact] = []

    for art_id in reg.required_artifacts:
        art_def = art_defs.get(art_id)
        name = art_def.name if art_def else art_id
        desc = art_def.description if art_def else ""

        if art_id in provided_artifacts:
            status = "provided"
        elif art_id == "test_report" and test_results:
            all_done = all(
                r.status in (TestStatus.passed, TestStatus.failed)
                for r in test_results
            )
            status = "complete" if all_done else "in_progress"
        else:
            status = "pending"

        artifacts.append(CertArtifact(
            artifact_id=art_id,
            name=name,
            region=region_id,
            status=status,
            description=desc,
        ))

    if sar_result and sar_result.file_path:
        sar_status = "provided" if sar_result.passed else "failed"
        artifacts.append(CertArtifact(
            artifact_id="sar_report",
            name="SAR Test Report",
            region=region_id,
            status=sar_status,
            file_path=sar_result.file_path,
            description="Specific Absorption Rate test report",
        ))

    return artifacts


# -- Checklist validation --

def validate_radio_checklist(
    spec: dict[str, Any],
    test_results: list[EmissionsTestResult] | None = None,
    sar_results: dict[str, SARResult] | None = None,
) -> list[RadioChecklist]:
    """Validate a radio spec and generate per-region checklists.

    Args:
        spec: Must contain 'target_regions' (list of region IDs) and
              optionally 'radio' dict with frequency/power/technology info.
        test_results: Previously run emissions test results.
        sar_results: Dict of region_id → SARResult.

    Returns:
        List of RadioChecklist, one per target region.
    """
    target_regions = spec.get("target_regions", [])
    if not target_regions:
        return []

    test_results = test_results or []
    sar_results = sar_results or {}

    result_map: dict[str, EmissionsTestResult] = {
        r.recipe_id: r for r in test_results
    }

    checklists: list[RadioChecklist] = []

    for region_id in target_regions:
        reg = get_region(region_id)
        if reg is None:
            continue

        items: list[ChecklistItem] = []

        for recipe in reg.test_recipes:
            existing = result_map.get(recipe.recipe_id)
            if existing:
                if existing.status == TestStatus.passed:
                    status = TestStatus.passed
                elif existing.status == TestStatus.failed:
                    status = TestStatus.failed
                else:
                    status = TestStatus.pending
            elif recipe.category == "sar":
                sar = sar_results.get(region_id)
                if sar and sar.passed:
                    status = TestStatus.passed
                elif sar and sar.status == TestStatus.failed:
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
        for art_id in reg.required_artifacts:
            art_def = art_defs.get(art_id)
            items.append(ChecklistItem(
                item_id=f"artifact:{art_id}",
                description=f"Artifact: {art_def.name if art_def else art_id}",
                category="artifact",
                status=TestStatus.passed if art_id in provided else TestStatus.pending,
            ))

        checklists.append(RadioChecklist(
            region=region_id,
            region_name=reg.name,
            items=items,
        ))

    return checklists


# -- Doc suite generator integration --

_ACTIVE_RADIO_CERTS: list[dict[str, Any]] = []


def register_radio_cert(
    standard: str,
    status: str = "Pending",
    cert_id: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    _ACTIVE_RADIO_CERTS.append({
        "standard": standard,
        "status": status,
        "cert_id": cert_id,
        "details": details or {},
    })


def get_radio_certs() -> list[dict[str, Any]]:
    """Return registered radio certs for doc_suite_generator."""
    return list(_ACTIVE_RADIO_CERTS)


def clear_radio_certs() -> None:
    _ACTIVE_RADIO_CERTS.clear()


# -- Audit log integration --

async def log_radio_test_result(result: EmissionsTestResult | SARResult) -> Optional[int]:
    try:
        from backend import audit
        entity_id = (
            f"{result.region}:{result.recipe_id}"
            if isinstance(result, EmissionsTestResult)
            else f"{result.region}:sar"
        )
        return await audit.log(
            action="radio_test",
            entity_kind="radio_test_result",
            entity_id=entity_id,
            before=None,
            after=result.to_dict(),
            actor="radio_compliance",
        )
    except Exception as exc:
        logger.warning("Failed to log radio test result to audit: %s", exc)
        return None


def log_radio_test_result_sync(result: EmissionsTestResult | SARResult) -> None:
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("log_radio_test_result_sync skipped (no running loop)")
        return
    loop.create_task(log_radio_test_result(result))

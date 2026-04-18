"""C5 — L4-CORE-05 Skill pack registry (#214).

Central registry for discovering, installing, validating, and enumerating
skill packs stored under ``configs/skills/<name>/``.

Convention
----------
Each skill pack lives in its own directory::

    configs/skills/<name>/
        skill.yaml          — manifest (required for formal packs)
        tasks.yaml          — DAG task templates
        scaffolds/          — code scaffold templates
        tests/              — integration test definitions
        hil/                — hardware-in-the-loop recipes
        docs/               — doc templates (datasheet, user manual, …)
        SKILL.md            — legacy human-readable description (optional)

Lifecycle hooks
---------------
* **install**  — run after copying a pack into the registry
* **validate** — check that the pack is internally consistent
* **enumerate** — list capabilities the pack provides
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from backend.skill_manifest import (
    REQUIRED_ARTIFACT_KINDS,
    SkillManifest,
)

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "configs" / "skills"
_INTERNAL_PREFIXES = ("_",)


@dataclass
class ValidationIssue:
    level: str  # "error" | "warning"
    message: str


@dataclass
class ValidationResult:
    skill_name: str
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]


@dataclass
class SkillInfo:
    name: str
    path: Path
    has_manifest: bool
    manifest: Optional[SkillManifest] = None
    has_tasks_yaml: bool = False
    has_skill_md: bool = False
    artifact_kinds: set[str] = field(default_factory=set)


def _is_skill_dir(p: Path) -> bool:
    if not p.is_dir():
        return False
    if p.name.startswith(_INTERNAL_PREFIXES):
        return False
    return True


def get_skills_dir() -> Path:
    return _SKILLS_DIR


def list_skills(skills_dir: Optional[Path] = None) -> list[SkillInfo]:
    """Enumerate all installed skill packs."""
    base = skills_dir or _SKILLS_DIR
    if not base.exists():
        return []

    results: list[SkillInfo] = []
    for child in sorted(base.iterdir()):
        if not _is_skill_dir(child):
            continue
        info = _inspect_skill(child)
        results.append(info)
    return results


def _inspect_skill(skill_dir: Path) -> SkillInfo:
    """Build a SkillInfo from the contents of a skill directory."""
    name = skill_dir.name
    manifest_path = skill_dir / "skill.yaml"
    has_manifest = manifest_path.exists()
    manifest = None
    artifact_kinds: set[str] = set()

    if has_manifest:
        try:
            manifest = load_manifest(manifest_path)
            artifact_kinds = manifest.artifact_kinds_present()
        except Exception as exc:
            logger.warning("failed to parse manifest for %s: %s", name, exc)

    if not artifact_kinds:
        artifact_kinds = _detect_artifact_kinds(skill_dir)

    return SkillInfo(
        name=name,
        path=skill_dir,
        has_manifest=has_manifest,
        manifest=manifest,
        has_tasks_yaml=(skill_dir / "tasks.yaml").exists(),
        has_skill_md=(skill_dir / "SKILL.md").exists(),
        artifact_kinds=artifact_kinds,
    )


def _detect_artifact_kinds(skill_dir: Path) -> set[str]:
    """Heuristic detection of artifact kinds when no manifest exists."""
    kinds: set[str] = set()
    if (skill_dir / "tasks.yaml").exists():
        kinds.add("tasks")
    if (skill_dir / "scaffolds").is_dir():
        kinds.add("scaffolds")
    if (skill_dir / "tests").is_dir():
        kinds.add("tests")
    if (skill_dir / "hil").is_dir():
        kinds.add("hil")
    if (skill_dir / "docs").is_dir():
        kinds.add("docs")
    return kinds


def load_manifest(path: Path) -> SkillManifest:
    """Parse and validate a skill.yaml file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"skill.yaml must be a YAML mapping, got {type(raw).__name__}")
    return SkillManifest(**raw)


def get_skill(name: str, skills_dir: Optional[Path] = None) -> Optional[SkillInfo]:
    """Look up a single skill by name."""
    base = skills_dir or _SKILLS_DIR
    skill_path = base / name
    if not _is_skill_dir(skill_path):
        return None
    return _inspect_skill(skill_path)


def validate_skill(name: str, skills_dir: Optional[Path] = None) -> ValidationResult:
    """Run full validation on an installed skill pack.

    Checks:
      1. Directory exists and is not internal
      2. skill.yaml present and parseable
      3. schema_version matches
      4. All 5 required artifact kinds declared
      5. Declared artifact paths exist on disk
      6. Dependency skills exist in registry
      7. Lifecycle hook validate command (if set) succeeds
    """
    base = skills_dir or _SKILLS_DIR
    skill_path = base / name
    issues: list[ValidationIssue] = []

    if not skill_path.is_dir():
        return ValidationResult(
            skill_name=name,
            ok=False,
            issues=[ValidationIssue("error", f"skill directory not found: {skill_path}")],
        )

    manifest_path = skill_path / "skill.yaml"
    if not manifest_path.exists():
        issues.append(ValidationIssue("error", "skill.yaml manifest not found"))
        detected = _detect_artifact_kinds(skill_path)
        missing = REQUIRED_ARTIFACT_KINDS - detected
        if missing:
            issues.append(ValidationIssue(
                "warning",
                f"missing artifact kinds (heuristic): {sorted(missing)}",
            ))
        return ValidationResult(skill_name=name, ok=False, issues=issues)

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        issues.append(ValidationIssue("error", f"skill.yaml parse error: {exc}"))
        return ValidationResult(skill_name=name, ok=False, issues=issues)

    if manifest.name != name:
        issues.append(ValidationIssue(
            "error",
            f"manifest name {manifest.name!r} does not match directory name {name!r}",
        ))

    missing_kinds = manifest.missing_artifact_kinds()
    if missing_kinds:
        issues.append(ValidationIssue(
            "error",
            f"missing required artifact kinds: {sorted(missing_kinds)}",
        ))

    for art in manifest.artifacts:
        art_path = skill_path / art.path
        if not art_path.exists():
            issues.append(ValidationIssue(
                "error",
                f"declared artifact not found: {art.path} (kind={art.kind})",
            ))

    for dep_skill in manifest.depends_on_skills:
        dep_path = base / dep_skill
        if not dep_path.is_dir():
            issues.append(ValidationIssue(
                "warning",
                f"dependency skill {dep_skill!r} not found in registry",
            ))

    if manifest.hooks.validate_cmd:
        try:
            result = subprocess.run(
                manifest.hooks.validate_cmd,
                shell=True,
                capture_output=True,
                timeout=30,
                cwd=str(skill_path),
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                issues.append(ValidationIssue(
                    "error",
                    f"validate hook failed (rc={result.returncode}): {stderr[:200]}",
                ))
        except subprocess.TimeoutExpired:
            issues.append(ValidationIssue("error", "validate hook timed out (30s)"))
        except Exception as exc:
            issues.append(ValidationIssue("error", f"validate hook error: {exc}"))

    ok = not any(i.level == "error" for i in issues)
    return ValidationResult(skill_name=name, ok=ok, issues=issues)


def install_skill(
    source: Path,
    name: Optional[str] = None,
    skills_dir: Optional[Path] = None,
    *,
    overwrite: bool = False,
) -> SkillInfo:
    """Install a skill pack from a source directory.

    Copies the source directory into the registry, validates the manifest,
    and runs the install hook if defined.

    Parameters
    ----------
    source : Path
        Directory containing the skill pack to install.
    name : str, optional
        Override name. Defaults to source directory name.
    skills_dir : Path, optional
        Override registry root.
    overwrite : bool
        If True, replace existing skill with same name.

    Returns
    -------
    SkillInfo
        Info about the newly installed skill.

    Raises
    ------
    FileExistsError
        If a skill with the same name already exists and overwrite=False.
    ValueError
        If the source directory is not a valid skill pack.
    """
    base = skills_dir or _SKILLS_DIR
    skill_name = name or source.name

    if not source.is_dir():
        raise ValueError(f"source must be a directory: {source}")

    dest = base / skill_name
    if dest.exists():
        if not overwrite:
            raise FileExistsError(f"skill {skill_name!r} already exists at {dest}")
        shutil.rmtree(dest)

    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(source), str(dest))

    manifest_path = dest / "skill.yaml"
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
        if manifest.hooks.install:
            try:
                subprocess.run(
                    manifest.hooks.install,
                    shell=True,
                    capture_output=True,
                    timeout=60,
                    cwd=str(dest),
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "install hook for %s failed (rc=%d): %s",
                    skill_name, exc.returncode,
                    exc.stderr.decode("utf-8", errors="replace")[:200],
                )
            except subprocess.TimeoutExpired:
                logger.warning("install hook for %s timed out", skill_name)

    return _inspect_skill(dest)


def enumerate_skill(name: str, skills_dir: Optional[Path] = None) -> dict:
    """Run the enumerate hook and return structured capabilities.

    If no enumerate hook is defined, returns artifact-based summary.
    """
    base = skills_dir or _SKILLS_DIR
    skill_path = base / name
    info = _inspect_skill(skill_path)

    result: dict = {
        "name": name,
        "has_manifest": info.has_manifest,
        "artifact_kinds": sorted(info.artifact_kinds),
        "has_tasks_yaml": info.has_tasks_yaml,
    }

    if info.manifest:
        result["version"] = info.manifest.version
        result["description"] = info.manifest.description
        result["compatible_socs"] = info.manifest.compatible_socs
        result["depends_on_skills"] = info.manifest.depends_on_skills
        result["depends_on_core"] = info.manifest.depends_on_core
        result["keywords"] = info.manifest.keywords

        if info.manifest.hooks.enumerate_cmd:
            try:
                proc = subprocess.run(
                    info.manifest.hooks.enumerate_cmd,
                    shell=True,
                    capture_output=True,
                    timeout=30,
                    cwd=str(skill_path),
                )
                if proc.returncode == 0:
                    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
                    try:
                        result["capabilities"] = yaml.safe_load(stdout)
                    except Exception:
                        result["capabilities_raw"] = stdout
            except Exception as exc:
                result["enumerate_error"] = str(exc)

    return result

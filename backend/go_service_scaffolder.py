"""X6 #302 — SKILL-GO-SERVICE project scaffolder.

Renders a Go 1.22+ microservice from the templates shipped in
``configs/skills/skill-go-service/scaffolds/``. Second software-vertical
skill pack — re-exercises the X0-X4 framework on a non-Python language
toolchain (go modules / go test / goreleaser) after X5 SKILL-FASTAPI
proved the framework on Python.

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. Matches X5's
  ``fastapi_scaffolder`` convention so framework regressions in the
  Jinja path surface on both skills at once.
* **Framework knob** — ``gin`` or ``fiber``. Templates carry the
  conditional; rendered code only sees the chosen engine so go vet
  / golangci-lint does not see dead imports.
* **Module path** — ``go.mod`` needs a module path string. Defaults
  to ``github.com/example/<slug>`` when not supplied; explicit override
  wins. Slug rules match ``_derive_package_name`` in
  ``fastapi_scaffolder`` but keep hyphens (go modules use hyphenated
  path segments).
* **Database knob** — ``postgres`` | ``sqlite`` | ``none``. The
  scaffold ships an in-memory ``MemoryStore`` so the rendered project
  compiles + tests green without a real DB dependency; real driver
  wiring is a TODO the operator fills in.
* **Idempotent** — on re-render we overwrite scaffold files in place.
  Files OUTSIDE the scaffold surface (e.g. operator-added
  ``internal/domain/``) are never touched.
* **Dry-run build** — ``dry_run_build()`` constructs the X3
  ``DockerImageAdapter`` + ``HelmChartAdapter`` + ``GoreleaserAdapter``
  against the rendered tree and runs each ``_validate_source`` path.
  Confirms the scaffold ships every file the adapters demand without
  actually running ``docker build`` / ``helm package`` /
  ``goreleaser release`` on CI.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``dry_run_build()``   — X3 adapter smoke.
``pilot_report()``    — one-shot X0-X4 validation report.
``validate_pack()``   — skill registry self-check.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import jinja2

from backend import platform_profile as _platform
from backend.build_adapters import (
    BuildSource,
    DockerImageAdapter,
    GoreleaserAdapter,
    HelmChartAdapter,
)
from backend.skill_registry import get_skill, validate_skill
from backend.software_compliance import run_all as run_compliance_all

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-go-service"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_FRAMEWORK_CHOICES = ("gin", "fiber")
_DATABASE_CHOICES = ("postgres", "sqlite", "none")
_DEPLOY_CHOICES = ("docker", "helm", "both")

_TEMPLATE_SUFFIX = ".j2"

# Module-path slug — go modules allow lowercase letters, digits,
# hyphens, and dots. Leading digits are legal (unlike Python), so we
# don't prefix like _derive_package_name does.
_MODULE_SLUG_RE = re.compile(r"[^a-z0-9.\-]+")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    module_path: Optional[str] = None     # defaults to github.com/example/<slug>
    framework: str = "gin"                # gin | fiber
    database: str = "postgres"            # postgres | sqlite | none
    deploy: str = "both"                  # docker | helm | both
    compliance: bool = True
    platform_profile: str = "linux-x86_64-native"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.framework not in _FRAMEWORK_CHOICES:
            raise ValueError(
                f"framework must be one of {_FRAMEWORK_CHOICES}, got {self.framework!r}"
            )
        if self.database not in _DATABASE_CHOICES:
            raise ValueError(
                f"database must be one of {_DATABASE_CHOICES}, got {self.database!r}"
            )
        if self.deploy not in _DEPLOY_CHOICES:
            raise ValueError(
                f"deploy must be one of {_DEPLOY_CHOICES}, got {self.deploy!r}"
            )

    def resolved_module_path(self) -> str:
        if self.module_path:
            return self.module_path
        return f"github.com/example/{_slugify_module(self.project_name)}"

    def builds_docker(self) -> bool:
        return self.deploy in ("docker", "both")

    def builds_helm(self) -> bool:
        return self.deploy in ("helm", "both")


@dataclass
class RenderOutcome:
    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    module_path: str = ""
    profile_binding: str = ""

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "module_path": self.module_path,
            "profile_binding": self.profile_binding,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _slugify_module(project_name: str) -> str:
    """Slugify a project name for the default module path tail.

    Go module paths permit lowercase alphanumerics, hyphens, and dots;
    leading digits are legal. An empty slug falls back to ``service``.
    """
    slug = _MODULE_SLUG_RE.sub("-", project_name.lower()).strip("-.")
    if not slug:
        return "service"
    return slug


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rendered_rel: str, opts: ScaffoldOptions) -> bool:
    # Deploy-gated files.
    if rendered_rel in ("Dockerfile", "docker-compose.yml") and not opts.builds_docker():
        return True
    if rendered_rel.startswith("deploy/helm/") and not opts.builds_helm():
        return True
    # Compliance-gated files.
    if rendered_rel == "spdx.allowlist.json" and not opts.compliance:
        return True
    return False


def _build_jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_SCAFFOLDS_DIR)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )


def _render_context(opts: ScaffoldOptions) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "project_name": opts.project_name,
        "module_path": opts.resolved_module_path(),
        "framework": opts.framework,
        "database": opts.database,
        "deploy": opts.deploy,
        "compliance": opts.compliance,
    }
    # Resolve X0 profile so the Dockerfile tag / helm image stays
    # aligned with the platform the skill targets. On failure we fall
    # through to defaults — same pattern as fastapi_scaffolder.
    try:
        raw = _platform.load_raw_profile(opts.platform_profile)
        ctx["platform_profile"] = opts.platform_profile
        ctx["platform_packaging"] = raw.get("packaging", "")
        ctx["platform_runtime"] = raw.get("software_runtime", "")
    except Exception:  # noqa: BLE001
        ctx["platform_profile"] = opts.platform_profile
        ctx["platform_packaging"] = ""
        ctx["platform_runtime"] = ""
    return ctx


def _write_file(dest: Path, content: bytes | str) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        dest.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))
    dest.write_bytes(content)
    return len(content)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-GO-SERVICE scaffold into ``out_dir``.

    Parameters
    ----------
    out_dir : Path
        Destination project root. Created if missing.
    options : ScaffoldOptions
        Knob values — ``project_name`` is required; everything else
        has a safe default.
    overwrite : bool
        When True (default), existing files inside the scaffold surface
        are overwritten. Files outside the scaffold surface are never
        touched.
    """
    options.validate()
    out_dir = Path(out_dir)
    if not _SCAFFOLDS_DIR.is_dir():
        raise FileNotFoundError(f"scaffolds directory missing: {_SCAFFOLDS_DIR}")

    out_dir.mkdir(parents=True, exist_ok=True)

    env = _build_jinja_env()
    ctx = _render_context(options)

    outcome = RenderOutcome(
        out_dir=out_dir,
        module_path=ctx["module_path"],
        profile_binding=ctx["platform_profile"],
    )

    for src in _iter_scaffold_files(_SCAFFOLDS_DIR):
        rel = src.relative_to(_SCAFFOLDS_DIR).as_posix()
        if rel.endswith(_TEMPLATE_SUFFIX):
            rendered_rel = rel[: -len(_TEMPLATE_SUFFIX)]
            is_template = True
        else:
            rendered_rel = rel
            is_template = False

        if _should_skip(rendered_rel, options):
            continue

        dest = out_dir / rendered_rel
        if dest.exists() and not overwrite:
            outcome.warnings.append(f"skipped existing: {rendered_rel}")
            continue

        if is_template:
            template = env.get_template(rel)
            rendered = template.render(**ctx)
            outcome.bytes_written += _write_file(dest, rendered)
        else:
            outcome.bytes_written += _write_file(dest, src.read_bytes())
        outcome.files_written.append(dest)

    # check_cov.sh must be executable — the Makefile runs it directly.
    cov_script = out_dir / "scripts" / "check_cov.sh"
    if cov_script.exists():
        cov_script.chmod(0o755)

    logger.info(
        "SKILL-GO-SERVICE rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_build(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the X3 build adapters against the rendered project
    without executing ``docker build`` / ``helm package`` /
    ``goreleaser release`` — we want to prove the scaffold ships every
    file each adapter demands and that the ``_validate_source`` paths
    accept the tree.

    Return dict shape::

        {"docker":     {"adapter": "DockerImageAdapter",
                        "artifact_valid": True,
                        "image_uri": "<name>:0.1.0"},
         "helm":       {"adapter": "HelmChartAdapter",
                        "artifact_valid": True,
                        "chart_dir": "<out_dir>/deploy/helm"},
         "goreleaser": {"adapter": "GoreleaserAdapter",
                        "artifact_valid": True,
                        "config": "<out_dir>/.goreleaser.yaml"}}
    """
    results: dict[str, Any] = {}
    options.validate()

    if options.builds_docker():
        adapter = DockerImageAdapter(
            name=options.project_name,
            version="0.1.0",
        )
        source = BuildSource(path=out_dir)
        try:
            source.validate()
            adapter._validate_source(source)  # noqa: SLF001 — adapter contract
            artifact_ok = True
            artifact_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001
            artifact_ok = False
            artifact_error = str(exc)
        results["docker"] = {
            "adapter": type(adapter).__name__,
            "image_uri": adapter.resolve_image_uri(),
            "artifact_valid": artifact_ok,
            "artifact_error": artifact_error,
        }

    if options.builds_helm():
        helm_adapter = HelmChartAdapter(
            name=options.project_name,
            version="0.1.0",
        )
        chart_dir = out_dir / "deploy" / "helm"
        source = BuildSource(path=out_dir, manifest=chart_dir)
        try:
            source.validate()
            helm_adapter._validate_source(source)  # noqa: SLF001
            chart_ok = True
            chart_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001
            chart_ok = False
            chart_error = str(exc)
        results["helm"] = {
            "adapter": type(helm_adapter).__name__,
            "chart_dir": str(chart_dir),
            "artifact_valid": chart_ok,
            "artifact_error": chart_error,
        }

    # goreleaser is always shipped — it's the release path for every
    # Go service regardless of the deploy knob (which only gates
    # docker/helm).
    gor_adapter = GoreleaserAdapter(
        name=options.project_name,
        version="0.1.0",
    )
    gor_source = BuildSource(path=out_dir)
    try:
        gor_source.validate()
        gor_adapter._validate_source(gor_source)  # noqa: SLF001
        gor_ok = True
        gor_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001
        gor_ok = False
        gor_error = str(exc)
    results["goreleaser"] = {
        "adapter": type(gor_adapter).__name__,
        "config": str(out_dir / ".goreleaser.yaml"),
        "artifact_valid": gor_ok,
        "artifact_error": gor_error,
    }

    results["module_path"] = options.resolved_module_path()
    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot X0-X4 gate report for the rendered project.

    Layers:
      * X0 — platform profile id the scaffold binds to.
      * X1 — ``scripts/check_cov.sh`` pins ``COVERAGE_THRESHOLD=70`` by
        default (surfaced as ``coverage_floor``).
      * X3 — ``dry_run_build`` result.
      * X4 — ``software_compliance.run_all`` bundle. Scans Go deps via
        ``go-licenses`` when available; otherwise the gates degrade
        to ``skipped`` and the bundle still reports back.
    """
    build = dry_run_build(out_dir, options)

    compliance = run_compliance_all(
        out_dir,
        component_name=options.project_name,
        component_version="0.1.0",
    )

    coverage_floor: Optional[float] = None
    script = out_dir / "scripts" / "check_cov.sh"
    if script.is_file():
        text = script.read_text(encoding="utf-8")
        m = re.search(r'THRESHOLD="\$\{COVERAGE_THRESHOLD:-(\d+(?:\.\d+)?)\}"', text)
        if m:
            coverage_floor = float(m.group(1))

    return {
        "skill": "skill-go-service",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "module_path": options.resolved_module_path(),
            "framework": options.framework,
            "database": options.database,
            "deploy": options.deploy,
            "compliance": options.compliance,
        },
        "x0_profile": options.platform_profile,
        "x1_coverage_floor": coverage_floor,
        "x3_build": build,
        "x4_compliance": compliance.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-go-service pack is complete."""
    info = get_skill("skill-go-service")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-go-service dir missing"]}

    result = validate_skill("skill-go-service")
    return {
        "installed": True,
        "ok": result.ok,
        "skill_name": result.skill_name,
        "issues": [{"level": i.level, "message": i.message} for i in result.issues],
        "artifact_kinds": sorted(info.artifact_kinds),
        "has_manifest": info.has_manifest,
        "has_tasks_yaml": info.has_tasks_yaml,
    }


__all__ = [
    "ScaffoldOptions",
    "RenderOutcome",
    "render_project",
    "dry_run_build",
    "pilot_report",
    "validate_pack",
    "_slugify_module",
]

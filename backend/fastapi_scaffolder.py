"""X5 #301 — SKILL-FASTAPI project scaffolder.

Renders a FastAPI 0.110+ project from the templates shipped in
``configs/skills/skill-fastapi/scaffolds/``. First software-vertical
skill pack and the pilot that exercises the X0-X4 framework end-to-end
— same pattern D1 SKILL-UVC applied to C5, D29 SKILL-HMI-WEBUI applied
to C26, and W6 SKILL-NEXTJS applied to W0-W5.

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. Non-``.j2`` modules (``db.py``,
  ``models.py``, ``schemas.py``, the Alembic template, test files)
  don't interpolate project-specific values, so keeping them out of
  the templating path avoids accidental corruption when the scaffold
  contains example ``{{ ... }}`` tokens.
* **Package rename** — the scaffold lives under ``src/app/``; at render
  time we rewrite every path (and Jinja context) so the rendered tree
  has ``src/<package_name>/``. ``package_name`` defaults to the
  slugified ``project_name``; explicit override wins.
* **Idempotent** — on re-render we overwrite scaffold files in place.
  Operator edits OUTSIDE the scaffold surface (e.g. ``src/<pkg>/domain/``)
  are never touched.
* **Framework binding** — ``render_project`` resolves the target
  software platform profile via ``backend.platform_profile.load_raw_profile``
  so the rendered Dockerfile & values.yaml read from the X0 profile,
  not a duplicated constant.
* **Dry-run build** — ``dry_run_build()`` constructs the X3
  ``DockerImageAdapter`` + ``HelmChartAdapter`` against the rendered
  tree and runs their ``_validate_source`` path. This catches
  "scaffold rendered but Dockerfile missing" class regressions without
  needing a real ``docker build`` on CI.

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
    HelmChartAdapter,
)
from backend.skill_registry import get_skill, validate_skill
from backend.software_compliance import run_all as run_compliance_all

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-fastapi"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"
_SCAFFOLD_PACKAGE_DIR = "src/app"

_DATABASE_CHOICES = ("postgres", "sqlite")
_AUTH_CHOICES = ("jwt", "oauth2", "none")
_DEPLOY_CHOICES = ("docker", "helm", "both")

_TEMPLATE_SUFFIX = ".j2"

# Scaffold paths gated on knobs — matched on the RENAMED relative path
# (i.e. after ``src/app/`` has been rewritten to ``src/<pkg>/``). Auth
# and deploy knobs map to file prefixes because the scaffolder never
# needs fine-grained per-file auth variants (the auth module itself
# branches at the template level).
_AUTH_ONLY_PATH_PREFIXES: tuple[tuple[str, str], ...] = (
    # (path prefix relative to rendered project root, required auth value)
    ("core/security.py", "jwt-or-oauth2"),
)

_DEPLOY_ONLY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Dockerfile", "docker"),
    ("docker-compose.yml", "docker"),
    ("deploy/helm/", "helm"),
)

# Package name slugification — keep pythonic: lowercase letters,
# digits, underscore; always start with a letter.
_PKG_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PKG_LEADING_DIGIT_RE = re.compile(r"^[0-9]")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    package_name: Optional[str] = None  # defaults to slug(project_name)
    database: str = "postgres"          # postgres | sqlite
    auth: str = "jwt"                   # jwt | oauth2 | none
    deploy: str = "both"                # docker | helm | both
    compliance: bool = True
    platform_profile: str = "linux-x86_64-native"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.database not in _DATABASE_CHOICES:
            raise ValueError(
                f"database must be one of {_DATABASE_CHOICES}, got {self.database!r}"
            )
        if self.auth not in _AUTH_CHOICES:
            raise ValueError(
                f"auth must be one of {_AUTH_CHOICES}, got {self.auth!r}"
            )
        if self.deploy not in _DEPLOY_CHOICES:
            raise ValueError(
                f"deploy must be one of {_DEPLOY_CHOICES}, got {self.deploy!r}"
            )

    def resolved_package_name(self) -> str:
        if self.package_name:
            return self.package_name
        return _derive_package_name(self.project_name)

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
    package_name: str = ""
    profile_binding: str = ""

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "package_name": self.package_name,
            "profile_binding": self.profile_binding,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _derive_package_name(project_name: str) -> str:
    """Slugify a project name into a Python-identifier-safe package name.

    Rules:
      * Lowercase.
      * Non-alphanumeric chars collapse to a single underscore.
      * Leading digit gets an ``app_`` prefix.
      * Empty result falls back to ``app``.
    """
    slug = _PKG_SLUG_RE.sub("_", project_name.lower()).strip("_")
    if not slug:
        return "app"
    if _PKG_LEADING_DIGIT_RE.match(slug):
        slug = f"app_{slug}"
    return slug


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _rewrite_package_path(rel: str, package_name: str) -> str:
    """Translate a scaffold-relative path into the rendered-project path.

    Replaces the fixed ``src/app/`` prefix with ``src/<package_name>/``
    so the generated tree uses the project's package name. Paths
    outside ``src/app/`` are returned unchanged.
    """
    prefix = _SCAFFOLD_PACKAGE_DIR + "/"
    if rel.startswith(prefix):
        return f"src/{package_name}/" + rel[len(prefix):]
    if rel == _SCAFFOLD_PACKAGE_DIR:
        return f"src/{package_name}"
    return rel


def _should_skip(rendered_rel: str, opts: ScaffoldOptions) -> bool:
    # Auth-gated files: core/security.py only when auth != none.
    pkg_security = f"src/{opts.resolved_package_name()}/core/security.py"
    if rendered_rel == pkg_security and opts.auth == "none":
        return True
    # Deploy-gated files
    if rendered_rel in ("Dockerfile", "docker-compose.yml") and not opts.builds_docker():
        return True
    if rendered_rel.startswith("deploy/helm/") and not opts.builds_helm():
        return True
    # Compliance-gated files
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
        "package_name": opts.resolved_package_name(),
        "database": opts.database,
        "auth": opts.auth,
        "deploy": opts.deploy,
        "compliance": opts.compliance,
    }
    # Resolve X0 profile so the Dockerfile tag / helm image stays
    # aligned with the platform the skill targets.
    try:
        raw = _platform.load_raw_profile(opts.platform_profile)
        ctx["platform_profile"] = opts.platform_profile
        ctx["platform_packaging"] = raw.get("packaging", "")
        ctx["platform_runtime"] = raw.get("software_runtime", "")
    except Exception:  # noqa: BLE001 — fall through to defaults
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
    """Render the SKILL-FASTAPI scaffold into ``out_dir``.

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
        package_name=ctx["package_name"],
        profile_binding=ctx["platform_profile"],
    )

    for src in _iter_scaffold_files(_SCAFFOLDS_DIR):
        rel = src.relative_to(_SCAFFOLDS_DIR).as_posix()
        # Compute the rendered-project relative path (post-rename + strip .j2).
        if rel.endswith(_TEMPLATE_SUFFIX):
            rendered_rel_raw = rel[: -len(_TEMPLATE_SUFFIX)]
            is_template = True
        else:
            rendered_rel_raw = rel
            is_template = False
        rendered_rel = _rewrite_package_path(rendered_rel_raw, ctx["package_name"])

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

    logger.info(
        "SKILL-FASTAPI rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_build(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the X3 DockerImageAdapter / HelmChartAdapter classes
    against the rendered project without executing ``docker build`` or
    ``helm package`` — we want to prove the scaffold ships every file
    the adapters demand (Dockerfile / Chart.yaml) and that their
    ``_validate_source`` path accepts the tree.

    Return dict shape::

        {"docker": {"adapter": "DockerImageAdapter",
                    "artifact_valid": True,
                    "image_uri": "<name>:0.1.0"},
         "helm":   {"adapter": "HelmChartAdapter",
                    "artifact_valid": True,
                    "chart_dir": "<out_dir>/deploy/helm"}}
    """
    results: dict[str, Any] = {}
    pkg = options.resolved_package_name()

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

    # Sanity hook for the OpenAPI governance script — N3 integration.
    dump_script = out_dir / "scripts" / "dump_openapi.py"
    results["openapi_dump"] = {
        "script": str(dump_script),
        "present": dump_script.is_file(),
        "mentions_check_flag": (
            dump_script.is_file() and "--check" in dump_script.read_text(encoding="utf-8")
        ),
    }

    # package_name ends up informing downstream X1/X2/X4 gates — expose
    # it so callers don't have to re-derive it.
    results["package_name"] = pkg
    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot X0-X4 gate report for the rendered project.

    Layers:
      * X0 — platform profile id the scaffold binds to.
      * X1 — pyproject.toml pins ``--cov-fail-under=80`` (surfaced as
        the ``coverage_floor`` field).
      * X3 — ``dry_run_build`` result.
      * X4 — ``software_compliance.run_all`` bundle. Scans Python deps
        via pip-licenses / pip audit when available; otherwise the
        gates degrade to ``skipped`` and the bundle still reports back.
    """
    build = dry_run_build(out_dir, options)

    # ecosystem is auto-detected from pyproject.toml — passing it
    # explicitly would hard-wire "python" before the file is even
    # resolvable, defeating the X4 detect_ecosystem contract.
    compliance = run_compliance_all(
        out_dir,
        component_name=options.project_name,
        component_version="0.1.0",
    )

    # X1 surface: expose the pinned coverage floor the rendered
    # pyproject.toml ships with so the caller can confirm the
    # framework threshold survived the render.
    pyproject = out_dir / "pyproject.toml"
    coverage_floor: Optional[int] = None
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        m = re.search(r"--cov-fail-under=(\d+)", text)
        if m:
            coverage_floor = int(m.group(1))

    return {
        "skill": "skill-fastapi",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "package_name": options.resolved_package_name(),
            "database": options.database,
            "auth": options.auth,
            "deploy": options.deploy,
            "compliance": options.compliance,
        },
        "x0_profile": options.platform_profile,
        "x1_coverage_floor": coverage_floor,
        "x3_build": build,
        "x4_compliance": compliance.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-fastapi pack is complete.

    Returns a dict with the skill registry validation result. Used by
    ``test_skill_fastapi.py`` as a living spec — a missing artifact or
    broken manifest trips the test immediately.
    """
    info = get_skill("skill-fastapi")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-fastapi dir missing"]}

    result = validate_skill("skill-fastapi")
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
    "_derive_package_name",
]

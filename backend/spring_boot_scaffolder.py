"""X9 #305 — SKILL-SPRING-BOOT project scaffolder.

Renders a Spring Boot 3.2+ service on JVM 21 LTS from the templates
shipped in ``configs/skills/skill-spring-boot/scaffolds/``. Fifth and
final priority-X software-vertical skill pack — re-exercises the
X0-X4 framework on the JVM after X5 FastAPI (Python), X6 Go, X7
Rust, X8 Tauri (dual-language Rust + TS).

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. Same contract as
  ``go_service_scaffolder`` / ``rust_cli_scaffolder`` / ``tauri_scaffolder``.
* **Package-path rewrite** — the scaffold ships Java sources under
  ``src/main/java/__pkg__/…`` and ``src/test/java/__pkg__/…``; at
  render time we rewrite every ``__pkg__`` segment to the resolved
  Java package path (e.g. ``com/example/my_service``). The in-file
  Jinja context exposes ``base_package`` as the dotted form so
  ``package com.example.my_service;`` lines stay correct.
* **Build-tool knob** — ``maven`` (default, ships ``pom.xml``) or
  ``gradle`` (ships ``build.gradle.kts`` + ``settings.gradle.kts`` +
  ``gradle/wrapper/gradle-wrapper.properties`` + stub ``gradlew`` /
  ``gradlew.bat``). The knob gates files via ``_should_skip`` — the
  scaffold never ships both build files at once.
* **Database knob** — ``postgres`` | ``h2`` | ``none``. When
  ``none``, JPA / Flyway dependencies drop out of the build file,
  the ``Item`` entity + ``ItemRepository`` are skipped, and
  ``ItemService`` uses an in-memory ``ConcurrentHashMap``.
* **Deploy knob** — ``docker`` | ``helm`` | ``both``; mirrors X6/X7.
* **Idempotent** — re-render overwrites files inside the scaffold
  surface; files outside (e.g. operator-added ``src/main/java/<pkg>/domain/extra/``)
  are never touched.
* **Dry-run build** — ``dry_run_build()`` walks the X3 adapters the
  scaffold ships: ``DockerImageAdapter`` (when deploy uses docker),
  ``HelmChartAdapter`` (when deploy uses helm), and the new X3
  ``MavenAdapter`` / ``GradleAdapter`` (always — release path is
  orthogonal to deploy).

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
    GradleAdapter,
    HelmChartAdapter,
    MavenAdapter,
)
from backend.skill_registry import get_skill, validate_skill
from backend.software_compliance import run_all as run_compliance_all

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-spring-boot"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_BUILD_TOOL_CHOICES = ("maven", "gradle")
_DATABASE_CHOICES = ("postgres", "h2", "none")
_DEPLOY_CHOICES = ("docker", "helm", "both")

_TEMPLATE_SUFFIX = ".j2"
_PACKAGE_PLACEHOLDER = "__pkg__"

# Artifact-id slug — Maven coordinates allow lowercase letters,
# digits, and hyphens (dots are legal but conventionally group-id
# only). Leading digits are legal.
_ARTIFACT_SLUG_RE = re.compile(r"[^a-z0-9\-]+")

# Reverse-DNS group-id validator. Matches ``com.example``,
# ``io.acme.platform``, rejects ``Com.Example`` (uppercase) and
# ``com.example.``.
_GROUP_ID_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$")

# Java package-segment rule: lowercase letters, digits (not leading),
# underscores — hyphens convert to underscore.
_PKG_SEGMENT_RE = re.compile(r"[^a-z0-9_]+")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    group_id: str = "com.example"
    artifact_id: Optional[str] = None     # defaults to slug(project_name)
    build_tool: str = "maven"             # maven | gradle
    database: str = "postgres"            # postgres | h2 | none
    deploy: str = "both"                  # docker | helm | both
    compliance: bool = True
    platform_profile: str = "linux-x86_64-native"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.build_tool not in _BUILD_TOOL_CHOICES:
            raise ValueError(
                f"build_tool must be one of {_BUILD_TOOL_CHOICES}, got {self.build_tool!r}"
            )
        if self.database not in _DATABASE_CHOICES:
            raise ValueError(
                f"database must be one of {_DATABASE_CHOICES}, got {self.database!r}"
            )
        if self.deploy not in _DEPLOY_CHOICES:
            raise ValueError(
                f"deploy must be one of {_DEPLOY_CHOICES}, got {self.deploy!r}"
            )
        if not _GROUP_ID_RE.match(self.group_id):
            raise ValueError(
                f"group_id must be reverse-DNS (e.g. com.example), got {self.group_id!r}"
            )

    def resolved_artifact_id(self) -> str:
        if self.artifact_id:
            return self.artifact_id
        return _slugify_artifact(self.project_name)

    def resolved_base_package(self) -> str:
        """Return the dotted Java package (e.g. ``com.example.my_service``).

        Built from ``group_id`` + the slugified project name with
        hyphens replaced by underscores (Java identifiers cannot
        contain hyphens). The artifact-id is *not* used verbatim —
        ``my-service`` as an artifact id yields ``my_service`` as a
        package-segment.
        """
        segment = _slugify_package_segment(self.resolved_artifact_id())
        return f"{self.group_id}.{segment}"

    def resolved_package_path(self) -> str:
        """Return the package path for file-system layout (e.g.
        ``com/example/my_service``)."""
        return self.resolved_base_package().replace(".", "/")

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
    artifact_id: str = ""
    base_package: str = ""
    profile_binding: str = ""

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "artifact_id": self.artifact_id,
            "base_package": self.base_package,
            "profile_binding": self.profile_binding,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Slug helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _slugify_artifact(project_name: str) -> str:
    """Slugify a project name for the default artifact_id.

    Lowercases, folds non-``[a-z0-9-]`` to ``-``, collapses runs, and
    strips leading/trailing separators. Empty result falls back to
    ``service`` (same rule as X6 ``_slugify_module``).
    """
    slug = _ARTIFACT_SLUG_RE.sub("-", project_name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "service"


def _slugify_package_segment(artifact_id: str) -> str:
    """Convert an artifact-id into a legal Java package segment.

    Hyphens become underscores; anything else non-``[a-z0-9_]``
    collapses to ``_``; leading digits are prefixed with ``pkg_``
    because Java identifiers may not start with a digit.
    """
    segment = artifact_id.lower().replace("-", "_")
    segment = _PKG_SEGMENT_RE.sub("_", segment)
    segment = re.sub(r"_+", "_", segment).strip("_")
    if not segment:
        return "service"
    if segment[0].isdigit():
        segment = "pkg_" + segment
    return segment


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _rewrite_package_path(rel: str, pkg_path: str) -> str:
    """Rewrite ``__pkg__`` segments in a scaffold-relative path to
    the resolved package path. Handles both
    ``src/main/java/__pkg__/…`` and ``src/test/java/__pkg__/…``.
    """
    if _PACKAGE_PLACEHOLDER not in rel:
        return rel
    return rel.replace(_PACKAGE_PLACEHOLDER, pkg_path)


def _should_skip(rendered_rel: str, opts: ScaffoldOptions) -> bool:
    # Build-tool gate: ship pom.xml only for Maven, the Gradle quartet
    # only for Gradle.
    maven_only = {"pom.xml"}
    gradle_only = {
        "build.gradle.kts",
        "settings.gradle.kts",
        "gradle/wrapper/gradle-wrapper.properties",
        "gradlew",
        "gradlew.bat",
    }
    if opts.build_tool == "maven" and rendered_rel in gradle_only:
        return True
    if opts.build_tool == "gradle" and rendered_rel in maven_only:
        return True

    # Deploy-gated files.
    if rendered_rel in ("Dockerfile", "docker-compose.yml") and not opts.builds_docker():
        return True
    if rendered_rel.startswith("deploy/helm/") and not opts.builds_helm():
        return True

    # Compliance-gated files.
    if rendered_rel == "spdx.allowlist.json" and not opts.compliance:
        return True

    # Database-gated files: when database=none, JPA entity + repository
    # + their test + the Flyway migration folder are skipped; the
    # ItemService template already handles the in-memory variant.
    if opts.database == "none":
        db_gated = {
            "src/main/resources/db/migration/V1__create_items_table.sql",
        }
        if rendered_rel in db_gated:
            return True
        # Any file under the generic domain package — Item.java,
        # ItemRepository.java, ItemRepositoryTest.java — is skipped.
        # We match on tail segments because the package path rewrite
        # has already happened by the time we get here.
        pkg_tail_skips = {
            "domain/Item.java",
            "domain/ItemRepository.java",
            "domain/ItemRepositoryTest.java",
        }
        if any(rendered_rel.endswith(suffix) for suffix in pkg_tail_skips):
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
        "group_id": opts.group_id,
        "artifact_id": opts.resolved_artifact_id(),
        "base_package": opts.resolved_base_package(),
        "build_tool": opts.build_tool,
        "database": opts.database,
        "deploy": opts.deploy,
        "compliance": opts.compliance,
    }
    # Resolve X0 profile so the Dockerfile runtime tag / values.yaml
    # stay aligned with the platform the skill targets. Fail-soft —
    # profile load failures make the context carry empty strings and
    # the render proceeds (same rule as fastapi / go / rust / tauri).
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
    """Render the SKILL-SPRING-BOOT scaffold into ``out_dir``.

    Parameters
    ----------
    out_dir : Path
        Destination project root. Created if missing.
    options : ScaffoldOptions
        Knob values — ``project_name`` is required; everything else
        has a safe default.
    overwrite : bool
        When True (default), existing files inside the scaffold
        surface are overwritten. Files outside the scaffold surface
        are never touched.
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
        artifact_id=ctx["artifact_id"],
        base_package=ctx["base_package"],
        profile_binding=ctx["platform_profile"],
    )

    pkg_path = options.resolved_package_path()

    for src in _iter_scaffold_files(_SCAFFOLDS_DIR):
        rel = src.relative_to(_SCAFFOLDS_DIR).as_posix()
        if rel.endswith(_TEMPLATE_SUFFIX):
            rendered_rel_raw = rel[: -len(_TEMPLATE_SUFFIX)]
            is_template = True
        else:
            rendered_rel_raw = rel
            is_template = False

        rendered_rel = _rewrite_package_path(rendered_rel_raw, pkg_path)

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

    # `gradlew` must be executable — the Dockerfile builder stage
    # and Makefile both assume it is.
    gradlew = out_dir / "gradlew"
    if gradlew.exists():
        gradlew.chmod(0o755)

    logger.info(
        "SKILL-SPRING-BOOT rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_build(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the X3 build adapters against the rendered project
    without executing ``docker build`` / ``helm package`` / ``mvn
    package`` / ``./gradlew bootJar`` — we just validate that the
    scaffold ships every file each adapter demands.

    Shape::

        {"docker":  {"adapter": "DockerImageAdapter",
                     "artifact_valid": True,
                     "image_uri": "<name>:0.1.0"},
         "helm":    {"adapter": "HelmChartAdapter",
                     "artifact_valid": True,
                     "chart_dir": "<out_dir>/deploy/helm"},
         "maven":   {"adapter": "MavenAdapter",
                     "artifact_valid": True,
                     "pom": "<out_dir>/pom.xml"},       # build_tool=maven only
         "gradle":  {"adapter": "GradleAdapter",
                     "artifact_valid": True,
                     "build_file": "<out_dir>/build.gradle.kts"}}  # build_tool=gradle only
    """
    results: dict[str, Any] = {}
    options.validate()

    if options.builds_docker():
        adapter = DockerImageAdapter(
            name=options.resolved_artifact_id(),
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
            name=options.resolved_artifact_id(),
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

    if options.build_tool == "maven":
        mvn_adapter = MavenAdapter(
            name=options.resolved_artifact_id(),
            version="0.1.0",
        )
        mvn_source = BuildSource(path=out_dir)
        try:
            mvn_source.validate()
            mvn_adapter._validate_source(mvn_source)  # noqa: SLF001
            mvn_ok = True
            mvn_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001
            mvn_ok = False
            mvn_error = str(exc)
        results["maven"] = {
            "adapter": type(mvn_adapter).__name__,
            "pom": str(out_dir / "pom.xml"),
            "artifact_valid": mvn_ok,
            "artifact_error": mvn_error,
        }
    else:
        gradle_adapter = GradleAdapter(
            name=options.resolved_artifact_id(),
            version="0.1.0",
        )
        gradle_source = BuildSource(path=out_dir)
        try:
            gradle_source.validate()
            gradle_adapter._validate_source(gradle_source)  # noqa: SLF001
            gradle_ok = True
            gradle_error: Optional[str] = None
        except Exception as exc:  # noqa: BLE001
            gradle_ok = False
            gradle_error = str(exc)
        results["gradle"] = {
            "adapter": type(gradle_adapter).__name__,
            "build_file": str(out_dir / "build.gradle.kts"),
            "artifact_valid": gradle_ok,
            "artifact_error": gradle_error,
        }

    results["artifact_id"] = options.resolved_artifact_id()
    results["base_package"] = options.resolved_base_package()
    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot X0-X4 gate report for the rendered project.

    Layers:
      * X0 — platform profile id the scaffold binds to.
      * X1 — JaCoCo ``check`` goal at LINE=0.70 (Maven) or
        ``jacocoTestCoverageVerification`` at 0.70 (Gradle); parsed
        out of the rendered build file so regressions in the anchor
        constant surface here.
      * X3 — ``dry_run_build`` result.
      * X4 — ``software_compliance.run_all`` bundle. Detects the
        maven ecosystem via pom.xml / build.gradle.kts and runs the
        new ``_scan_maven`` adapter; when ``mvn`` is absent the gate
        falls back to the pom.xml / build.gradle walker and reports
        rows as ``UNKNOWN``.
    """
    build = dry_run_build(out_dir, options)

    compliance = run_compliance_all(
        out_dir,
        component_name=options.resolved_artifact_id(),
        component_version="0.1.0",
    )

    coverage_floor: Optional[float] = None
    if options.build_tool == "maven":
        pom = out_dir / "pom.xml"
        if pom.is_file():
            text = pom.read_text(encoding="utf-8")
            m = re.search(r"<minimum>(\d+(?:\.\d+)?)</minimum>", text)
            if m:
                # JaCoCo coverage is fractional 0.70 → surface as 70.0%.
                coverage_floor = float(m.group(1)) * 100.0
    else:
        build_file = out_dir / "build.gradle.kts"
        if build_file.is_file():
            text = build_file.read_text(encoding="utf-8")
            m = re.search(r'minimum\s*=\s*"(\d+(?:\.\d+)?)"\.toBigDecimal', text)
            if m:
                coverage_floor = float(m.group(1)) * 100.0

    return {
        "skill": "skill-spring-boot",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "group_id": options.group_id,
            "artifact_id": options.resolved_artifact_id(),
            "base_package": options.resolved_base_package(),
            "build_tool": options.build_tool,
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
    """Self-check that the installed skill-spring-boot pack is complete."""
    info = get_skill("skill-spring-boot")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-spring-boot dir missing"]}

    result = validate_skill("skill-spring-boot")
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
    "_slugify_artifact",
    "_slugify_package_segment",
]

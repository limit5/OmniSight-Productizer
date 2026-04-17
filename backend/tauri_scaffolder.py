"""X8 #304 — SKILL-DESKTOP-TAURI project scaffolder.

Renders a Tauri 2.x desktop project from the templates shipped in
``configs/skills/skill-desktop-tauri/scaffolds/``. Fourth software-
vertical skill pack — the first **dual-language** consumer of the
X0-X4 framework: Rust backend (``src-tauri/``) + TypeScript frontend
(``src/``) bundled into platform-native installers (msi / dmg / deb /
AppImage / rpm) for Windows, macOS, and Linux, with
``tauri-plugin-updater`` wired for auto-update.

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. Matches the X5/X6/X7
  convention so a regression in the Jinja path surfaces on every
  skill at once.
* **Frontend knob** — ``react`` (default) or ``vue``. Templates carry
  variant-specific files; the unused variant's files are dropped via
  ``_should_skip``. The ``package.json``, ``vite.config.ts``, and
  ``index.html`` Jinja conditionals swap deps + entry-point pointers.
* **Updater knob** — ``updater=False`` removes the
  ``tauri-plugin-updater`` dep, the ``[bundle.updater]`` block in
  ``tauri.conf.json``, and the related capability grants.
* **Compliance knob** — ``compliance=False`` skips the SPDX allowlist
  and ``deny.toml``.
* **Idempotent** — re-render overwrites scaffold files in place;
  files OUTSIDE the scaffold surface (e.g. operator-added
  ``src-tauri/src/operators/``) are never touched.
* **Identifier validation** — ``identifier`` must be reverse-DNS
  shaped (``[a-z][a-z0-9-]*(\\.[a-z][a-z0-9-]*)+``); macOS code-sign
  rejects anything else, so we fail fast at render time rather than
  at the operator's first ``tauri build`` attempt.
* **Two-ecosystem compliance** — ``pilot_report`` runs the X4 bundle
  against both the project root (npm) and ``src-tauri/`` (cargo) and
  merges the verdicts. A release fails if either ecosystem's gates
  fail.
* **Dry-run build** — ``dry_run_build()`` constructs the X3
  ``CargoDistAdapter`` against the rendered ``src-tauri/`` directory
  and runs its ``_validate_source`` path. Confirms the scaffold
  ships the expected ``Cargo.toml`` without invoking the real
  ``cargo dist build`` in CI.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``dry_run_build()``   — X3 adapter smoke (against src-tauri/).
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

from backend import platform as _platform
from backend.build_adapters import BuildSource, CargoDistAdapter
from backend.skill_registry import get_skill, validate_skill
from backend.software_compliance import run_all as run_compliance_all
from backend.software_compliance.licenses import detect_ecosystem

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-desktop-tauri"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_FRONTEND_CHOICES = ("react", "vue")

_TEMPLATE_SUFFIX = ".j2"

# Cargo crate name — [a-z0-9_], underscores only (no hyphens) per
# Cargo identifier rules. bin_name is looser and allows hyphens
# (Cargo's `[[bin]] name` lane).
_CRATE_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_BIN_SLUG_RE = re.compile(r"[^a-z0-9._\-]+")
_NPM_SLUG_RE = re.compile(r"[^a-z0-9\-]+")

# Reverse-DNS bundle identifier — required by macOS code-sign and the
# desktop-tauri role's mandatory PR-self-audit checklist.
_REVERSE_DNS_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    app_name: Optional[str] = None            # display name; defaults to humanised project_name
    bin_name: Optional[str] = None            # binary name; defaults to slugified project_name
    crate_name: Optional[str] = None          # cargo crate name; defaults to _underscorify(bin_name)
    identifier: Optional[str] = None          # reverse-DNS bundle id; defaults to com.example.<crate>
    frontend: str = "react"                   # react | vue
    updater: bool = True
    compliance: bool = True
    platform_profile: str = "linux-x86_64-native"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.frontend not in _FRONTEND_CHOICES:
            raise ValueError(
                f"frontend must be one of {_FRONTEND_CHOICES}, got {self.frontend!r}"
            )
        ident = self.resolved_identifier()
        if not _REVERSE_DNS_RE.match(ident):
            raise ValueError(
                f"identifier {ident!r} must be reverse-DNS shaped "
                "(e.g. 'com.example.myapp'); macOS code-sign rejects other forms"
            )

    def resolved_bin_name(self) -> str:
        if self.bin_name:
            return _slugify_bin(self.bin_name)
        return _slugify_bin(self.project_name)

    def resolved_crate_name(self) -> str:
        if self.crate_name:
            return _slugify_crate(self.crate_name)
        return _slugify_crate(self.resolved_bin_name())

    def resolved_app_name(self) -> str:
        if self.app_name:
            return self.app_name
        return _humanise(self.project_name)

    def resolved_slug_name(self) -> str:
        # npm package name — kebab, lowercase. Permissive: any allowed
        # bin_name slug is also a valid npm name once the dots become
        # hyphens (npm rejects `.` in package names).
        return _slugify_npm(self.resolved_bin_name())

    def resolved_identifier(self) -> str:
        if self.identifier:
            return self.identifier.lower()
        # Default — `com.example.<slug>`. Operators almost always
        # override this on first render; the default is good enough
        # to pass the reverse-DNS regex without forcing the knob.
        # We deliberately use slug_name (kebab) over crate_name
        # (underscore) — reverse-DNS labels reject `_`.
        return f"com.example.{self.resolved_slug_name()}"


@dataclass
class RenderOutcome:
    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    bin_name: str = ""
    crate_name: str = ""
    app_name: str = ""
    identifier: str = ""
    frontend: str = ""
    profile_binding: str = ""

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "bin_name": self.bin_name,
            "crate_name": self.crate_name,
            "app_name": self.app_name,
            "identifier": self.identifier,
            "frontend": self.frontend,
            "profile_binding": self.profile_binding,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _slugify_bin(project_name: str) -> str:
    """Slugify a project name for the [[bin]] target name.

    Cargo's bin target allows lowercase alphanumerics, hyphens,
    underscores, and dots. Empty slug falls back to ``app``.
    """
    slug = _BIN_SLUG_RE.sub("-", project_name.lower()).strip("-.")
    if not slug:
        return "app"
    return slug


def _slugify_crate(name: str) -> str:
    """Slugify a name for the Cargo package name.

    Cargo crate names are identifiers: lowercase alphanumerics +
    underscores. Hyphens become underscores; punctuation is dropped.
    """
    slug = _CRATE_SLUG_RE.sub("_", name.lower()).strip("_")
    if not slug:
        return "app"
    return slug


def _slugify_npm(name: str) -> str:
    """Slugify for an npm package name (kebab, no dots).

    npm rejects dots in package names and is stricter than Cargo bin
    rules — collapse dots to hyphens, then strip.
    """
    slug = _NPM_SLUG_RE.sub("-", name.lower()).strip("-")
    if not slug:
        return "app"
    return slug


def _humanise(project_name: str) -> str:
    """Humanise a slugified name for display ("my-app" → "My App")."""
    parts = re.split(r"[-_.]+", project_name.strip())
    return " ".join(p.capitalize() for p in parts if p)


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rendered_rel: str, opts: ScaffoldOptions) -> bool:
    # Compliance-gated files.
    if rendered_rel == "spdx.allowlist.json" and not opts.compliance:
        return True
    if rendered_rel == "src-tauri/deny.toml" and not opts.compliance:
        return True

    # Frontend variant gating — ship only the chosen variant's files.
    react_files = {
        "src/main.tsx",
        "src/App.tsx",
        "src/__tests__/App.test.tsx",
    }
    vue_files = {
        "src/main.ts",
        "src/App.vue",
        "src/__tests__/App.test.ts",
    }
    if opts.frontend == "react" and rendered_rel in vue_files:
        return True
    if opts.frontend == "vue" and rendered_rel in react_files:
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
    bin_name = opts.resolved_bin_name()
    ctx: dict[str, Any] = {
        "project_name": opts.project_name,
        "app_name": opts.resolved_app_name(),
        "bin_name": bin_name,
        "crate_name": opts.resolved_crate_name(),
        "slug_name": opts.resolved_slug_name(),
        "identifier": opts.resolved_identifier(),
        "frontend": opts.frontend,
        "updater": opts.updater,
        "compliance": opts.compliance,
    }
    # Resolve X0 profile — same pattern as rust_cli_scaffolder /
    # go_service_scaffolder.
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
    """Render the SKILL-DESKTOP-TAURI scaffold into ``out_dir``."""
    options.validate()
    out_dir = Path(out_dir)
    if not _SCAFFOLDS_DIR.is_dir():
        raise FileNotFoundError(f"scaffolds directory missing: {_SCAFFOLDS_DIR}")

    out_dir.mkdir(parents=True, exist_ok=True)

    env = _build_jinja_env()
    ctx = _render_context(options)

    outcome = RenderOutcome(
        out_dir=out_dir,
        bin_name=ctx["bin_name"],
        crate_name=ctx["crate_name"],
        app_name=ctx["app_name"],
        identifier=ctx["identifier"],
        frontend=ctx["frontend"],
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
        else:
            rendered = src.read_bytes()

        outcome.bytes_written += _write_file(dest, rendered)
        outcome.files_written.append(dest)

    # check_cov.sh must be executable — the Makefile runs it directly.
    cov_script = out_dir / "scripts" / "check_cov.sh"
    if cov_script.exists():
        cov_script.chmod(0o755)

    logger.info(
        "SKILL-DESKTOP-TAURI rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_build(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the X3 ``CargoDistAdapter`` against the rendered
    project's ``src-tauri/`` directory without invoking the real
    ``cargo dist build``. We want to prove the scaffold ships every
    file the adapter demands and that ``_validate_source`` accepts
    the tree. The real release path is ``tauri build`` per-platform
    via tauri-action; cargo-dist is the offline config-shape gate.

    Return dict shape::

        {"cargo-dist": {"adapter": "CargoDistAdapter",
                        "artifact_valid": True,
                        "config": "<out_dir>/src-tauri/Cargo.toml"},
         "bin_name":  "<bin_name>",
         "crate_name": "<crate_name>",
         "src_tauri_dir": "<out_dir>/src-tauri"}
    """
    options.validate()
    src_tauri = Path(out_dir) / "src-tauri"
    results: dict[str, Any] = {}

    adapter = CargoDistAdapter(
        name=options.resolved_crate_name(),
        version="0.1.0",
    )
    source = BuildSource(path=src_tauri)
    try:
        source.validate()
        adapter._validate_source(source)  # noqa: SLF001 — adapter contract
        artifact_ok = True
        artifact_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001
        artifact_ok = False
        artifact_error = str(exc)

    results["cargo-dist"] = {
        "adapter": type(adapter).__name__,
        "config": str(src_tauri / "Cargo.toml"),
        "dist_workspace": str(src_tauri / "dist-workspace.toml"),
        "artifact_valid": artifact_ok,
        "artifact_error": artifact_error,
    }
    results["bin_name"] = options.resolved_bin_name()
    results["crate_name"] = options.resolved_crate_name()
    results["src_tauri_dir"] = str(src_tauri)
    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot X0-X4 gate report for the rendered project.

    Layers:
      * X0 — platform profile id the scaffold binds to.
      * X1 — ``scripts/check_cov.sh`` pins ``COVERAGE_THRESHOLD=75``
        for the Rust track; ``vite.config.ts`` pins 80 for the Node
        track.
      * X3 — ``dry_run_build`` result.
      * X4 — ``software_compliance.run_all`` bundle, run twice: once
        at the project root (npm) and once under ``src-tauri/``
        (cargo). Both verdicts are returned so a release can gate on
        either.
    """
    out_dir = Path(out_dir)
    src_tauri = out_dir / "src-tauri"
    build = dry_run_build(out_dir, options)

    npm_compliance = run_compliance_all(
        out_dir,
        component_name=options.resolved_slug_name(),
        component_version="0.1.0",
    )
    cargo_compliance = run_compliance_all(
        src_tauri,
        component_name=options.resolved_crate_name(),
        component_version="0.1.0",
    )

    coverage_floor_rust: Optional[float] = None
    rust_script = out_dir / "scripts" / "check_cov.sh"
    if rust_script.is_file():
        text = rust_script.read_text(encoding="utf-8")
        m = re.search(
            r'THRESHOLD="\$\{COVERAGE_THRESHOLD:-(\d+(?:\.\d+)?)\}"', text,
        )
        if m:
            coverage_floor_rust = float(m.group(1))

    coverage_floor_frontend: Optional[float] = None
    vite_cfg = out_dir / "vite.config.ts"
    if vite_cfg.is_file():
        text = vite_cfg.read_text(encoding="utf-8")
        # The vite config sets `lines: 80`, etc. We pull the first
        # one (lines) as the canonical floor.
        m = re.search(r"lines:\s*(\d+)", text)
        if m:
            coverage_floor_frontend = float(m.group(1))

    return {
        "skill": "skill-desktop-tauri",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "app_name": options.resolved_app_name(),
            "bin_name": options.resolved_bin_name(),
            "crate_name": options.resolved_crate_name(),
            "slug_name": options.resolved_slug_name(),
            "identifier": options.resolved_identifier(),
            "frontend": options.frontend,
            "updater": options.updater,
            "compliance": options.compliance,
        },
        "x0_profile": options.platform_profile,
        "x1_coverage_floor_rust": coverage_floor_rust,
        "x1_coverage_floor_frontend": coverage_floor_frontend,
        "x3_build": build,
        "x4_compliance": {
            "npm": npm_compliance.to_dict(),
            "cargo": cargo_compliance.to_dict(),
        },
        "x4_ecosystems_detected": {
            "root": detect_ecosystem(out_dir),
            "src_tauri": detect_ecosystem(src_tauri),
        },
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-desktop-tauri pack is complete."""
    info = get_skill("skill-desktop-tauri")
    if info is None:
        return {
            "installed": False,
            "ok": False,
            "issues": ["skill-desktop-tauri dir missing"],
        }

    result = validate_skill("skill-desktop-tauri")
    return {
        "installed": True,
        "ok": result.ok,
        "skill_name": result.skill_name,
        "issues": [
            {"level": i.level, "message": i.message} for i in result.issues
        ],
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
    "_slugify_bin",
    "_slugify_crate",
    "_slugify_npm",
    "_humanise",
]

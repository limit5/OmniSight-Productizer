"""X7 #303 — SKILL-RUST-CLI project scaffolder.

Renders a Rust 2021 edition CLI binary from the templates shipped in
``configs/skills/skill-rust-cli/scaffolds/``. Third software-vertical
skill pack — re-exercises the X0-X4 framework on a Rust toolchain
(cargo + rustc + cargo-dist) with a different deliverable shape than
X5 FastAPI / X6 Go-service: a single-file native binary instead of a
long-running HTTP server.

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. Matches the X5/X6
  convention so framework regressions in the Jinja path surface on
  every skill at once.
* **Runtime knob** — ``tokio`` (default) or ``sync``. Templates carry
  the conditional; when sync, ``tokio`` is dropped from Cargo.toml
  and each subcommand uses its ``_sync`` entry point.
* **Completions knob** — ``completions=False`` omits the
  ``completions`` subcommand scaffold and drops ``clap_complete`` /
  the completion file from the tree; the cli.rs variant and mod
  declarations move in lockstep.
* **Bin-name** — ``bin_name`` is the produced binary name; defaults
  to the ``project_name`` slug. ``crate_name`` is the Cargo crate
  name (underscores, not hyphens — Rust identifier rules).
* **Idempotent** — on re-render we overwrite scaffold files in place.
  Files OUTSIDE the scaffold surface (e.g. operator-added
  ``src/commands/custom/``) are never touched.
* **Dry-run build** — ``dry_run_build()`` constructs the X3
  ``CargoDistAdapter`` against the rendered tree and runs its
  ``_validate_source`` path. Confirms the scaffold ships Cargo.toml
  without invoking the real ``cargo dist build`` in CI.

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

from backend import platform as _platform
from backend.build_adapters import BuildSource, CargoDistAdapter
from backend.skill_registry import get_skill, validate_skill
from backend.software_compliance import run_all as run_compliance_all

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-rust-cli"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_RUNTIME_CHOICES = ("tokio", "sync")

_TEMPLATE_SUFFIX = ".j2"

# Rust crate name — [a-z0-9_], underscores only (no hyphens) per
# Cargo identifier rules. bin_name is looser and allows hyphens
# (Cargo's `[[bin]] name` lane).
_CRATE_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_BIN_SLUG_RE = re.compile(r"[^a-z0-9._\-]+")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    bin_name: Optional[str] = None            # defaults to slugified project_name
    crate_name: Optional[str] = None          # defaults to _underscorify(bin_name)
    runtime: str = "tokio"                    # tokio | sync
    completions: bool = True
    compliance: bool = True
    platform_profile: str = "linux-x86_64-native"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.runtime not in _RUNTIME_CHOICES:
            raise ValueError(
                f"runtime must be one of {_RUNTIME_CHOICES}, got {self.runtime!r}"
            )

    def resolved_bin_name(self) -> str:
        if self.bin_name:
            return _slugify_bin(self.bin_name)
        return _slugify_bin(self.project_name)

    def resolved_crate_name(self) -> str:
        if self.crate_name:
            return _slugify_crate(self.crate_name)
        return _slugify_crate(self.resolved_bin_name())


@dataclass
class RenderOutcome:
    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    bin_name: str = ""
    crate_name: str = ""
    profile_binding: str = ""

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "bin_name": self.bin_name,
            "crate_name": self.crate_name,
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
    underscores. Hyphens are not legal in `[[bin]].path` references
    via `use` statements, so we swap them for underscores and drop
    other punctuation. Leading digit is still legal — Cargo warns
    but doesn't reject.
    """
    slug = _CRATE_SLUG_RE.sub("_", name.lower()).strip("_")
    if not slug:
        return "app"
    return slug


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rendered_rel: str, opts: ScaffoldOptions) -> bool:
    # Compliance-gated files.
    if rendered_rel in ("spdx.allowlist.json", "deny.toml") and not opts.compliance:
        return True
    # Completions-gated: when --completions off, drop the subcommand
    # file entirely. The cli.rs template itself still compiles because
    # its `Completions` variant is inline; we swap the template to the
    # "no completions" shape via rendering context below.
    if (
        rendered_rel.endswith("commands/completions.rs")
        and not opts.completions
    ):
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
        "bin_name": bin_name,
        "crate_name": opts.resolved_crate_name(),
        # env-var-safe upper-case prefix (hyphens → underscores) —
        # Cargo bin names allow hyphens but env var identifiers do
        # not, so we pre-compute this rather than leaning on a
        # Jinja filter chain in every template.
        "env_prefix": bin_name.upper().replace("-", "_"),
        "runtime": opts.runtime,
        "completions": opts.completions,
        "compliance": opts.compliance,
    }
    # Resolve X0 profile — same pattern as go_service_scaffolder.
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


def _patch_cli_for_no_completions(text: str) -> str:
    """Remove the `Completions(...)` variant + handler when
    completions=off. Keeps cli.rs compilable without the
    clap_complete dep.
    """
    # Drop the `/// ...\n    Completions(...),` variant (3 lines).
    text = re.sub(
        r"\n    /// Emit shell completions for the given shell\.\n"
        r"    Completions\(crate::commands::completions::Args\),\n",
        "\n",
        text,
    )
    return text


def _patch_main_for_no_completions(text: str) -> str:
    """Drop the `Commands::Completions(...)` dispatch arm."""
    text = re.sub(
        r"\s*Commands::Completions\(args\) => commands::completions::run\(args\),",
        "",
        text,
    )
    return text


def _patch_commands_mod_for_no_completions(text: str) -> str:
    """Drop `pub mod completions;` from commands/mod.rs."""
    return text.replace("pub mod completions;\n", "")


def _patch_cargo_for_no_completions(text: str) -> str:
    """Drop `clap_complete` from [dependencies]."""
    return re.sub(r'\nclap_complete = "[^"]+"', "", text)


def _patch_cargo_for_sync(text: str) -> str:
    """Drop the tokio block when runtime=sync. The Jinja template
    already guards it with ``{% if runtime == "tokio" %}`` so this
    is a no-op for the default flow; kept as a guard rail for
    direct byte-level rewriters that skip Jinja in tests.
    """
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-RUST-CLI scaffold into ``out_dir``."""
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

        # When completions=off, strip references from the cli/main/commands
        # module trees. We do it post-render so the Jinja templates stay
        # simple (they don't need per-file {% if %} around every import).
        if not options.completions and isinstance(rendered, str):
            if rendered_rel == "src/cli.rs":
                rendered = _patch_cli_for_no_completions(rendered)
            elif rendered_rel == "src/main.rs":
                rendered = _patch_main_for_no_completions(rendered)
            elif rendered_rel == "src/commands/mod.rs":
                rendered = _patch_commands_mod_for_no_completions(rendered)
            elif rendered_rel == "Cargo.toml":
                rendered = _patch_cargo_for_no_completions(rendered)

        outcome.bytes_written += _write_file(dest, rendered)
        outcome.files_written.append(dest)

    # check_cov.sh must be executable — the Makefile runs it directly.
    cov_script = out_dir / "scripts" / "check_cov.sh"
    if cov_script.exists():
        cov_script.chmod(0o755)

    logger.info(
        "SKILL-RUST-CLI rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_build(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the X3 ``CargoDistAdapter`` against the rendered
    project without invoking the real ``cargo dist build``. We want
    to prove the scaffold ships every file the adapter demands and
    that the ``_validate_source`` path accepts the tree.

    Return dict shape::

        {"cargo-dist": {"adapter": "CargoDistAdapter",
                        "artifact_valid": True,
                        "config": "<out_dir>/Cargo.toml"},
         "bin_name":  "<bin_name>",
         "crate_name": "<crate_name>"}
    """
    options.validate()
    results: dict[str, Any] = {}

    adapter = CargoDistAdapter(
        name=options.resolved_crate_name(),
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

    results["cargo-dist"] = {
        "adapter": type(adapter).__name__,
        "config": str(out_dir / "Cargo.toml"),
        "dist_workspace": str(out_dir / "dist-workspace.toml"),
        "artifact_valid": artifact_ok,
        "artifact_error": artifact_error,
    }
    results["bin_name"] = options.resolved_bin_name()
    results["crate_name"] = options.resolved_crate_name()
    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot X0-X4 gate report for the rendered project.

    Layers:
      * X0 — platform profile id the scaffold binds to.
      * X1 — ``scripts/check_cov.sh`` pins ``COVERAGE_THRESHOLD=75``
        by default (surfaced as ``coverage_floor``).
      * X3 — ``dry_run_build`` result.
      * X4 — ``software_compliance.run_all`` bundle. Scans cargo
        deps via ``cargo-license`` when available; otherwise the
        gates degrade to ``skipped`` and the bundle still reports
        back.
    """
    build = dry_run_build(out_dir, options)

    compliance = run_compliance_all(
        out_dir,
        component_name=options.resolved_crate_name(),
        component_version="0.1.0",
    )

    coverage_floor: Optional[float] = None
    script = out_dir / "scripts" / "check_cov.sh"
    if script.is_file():
        text = script.read_text(encoding="utf-8")
        m = re.search(
            r'THRESHOLD="\$\{COVERAGE_THRESHOLD:-(\d+(?:\.\d+)?)\}"', text,
        )
        if m:
            coverage_floor = float(m.group(1))

    return {
        "skill": "skill-rust-cli",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "bin_name": options.resolved_bin_name(),
            "crate_name": options.resolved_crate_name(),
            "runtime": options.runtime,
            "completions": options.completions,
            "compliance": options.compliance,
        },
        "x0_profile": options.platform_profile,
        "x1_coverage_floor": coverage_floor,
        "x3_build": build,
        "x4_compliance": compliance.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-rust-cli pack is complete."""
    info = get_skill("skill-rust-cli")
    if info is None:
        return {
            "installed": False,
            "ok": False,
            "issues": ["skill-rust-cli dir missing"],
        }

    result = validate_skill("skill-rust-cli")
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
]

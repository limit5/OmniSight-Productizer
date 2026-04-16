"""W6 #280 — SKILL-NEXTJS project scaffolder.

Renders a Next.js 16 App Router project from the templates shipped in
``configs/skills/skill_nextjs/scaffolds/``. First web-vertical skill
pack and the pilot that exercises the W0-W5 framework end-to-end
(same pattern D1 SKILL-UVC applied to C5, and D29 SKILL-HMI-WEBUI
applied to C26).

Design
------
* **Template resolution** — ``.j2`` files are Jinja-rendered;
  everything else is copied byte-for-byte. This keeps static assets
  (CSS, JSON fixtures, configs that should not interpolate) out of
  the templating path, while ``package.json.j2`` / ``next.config.mjs.j2``
  can branch on knobs like ``auth`` / ``trpc`` / ``target``.
* **Idempotent** — on re-render we overwrite scaffold files. The
  operator is expected to edit OUTSIDE the scaffold surface (e.g.
  ``app/dashboard/`` is NOT in the scaffold, so it survives).
* **Framework binding** — each render resolves the target web
  profile from ``backend.platform.get_platform_config`` so the
  ``bundle_size_budget`` / ``memory_limit_mb`` read straight from
  the W1 profile, not a copy.
* **Dry-run deploy** — ``dry_run_deploy()`` calls the W4 adapter's
  constructor path + a fake BuildArtifact validation to prove the
  generated project hands off cleanly, without hitting the network.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``dry_run_deploy()``  — W4 adapter smoke.
``pilot_report()``    — one-shot W0-W5 validation report.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import jinja2

from backend import platform as _platform
from backend.deploy.base import BuildArtifact
from backend.skill_registry import get_skill, validate_skill
from backend.web_compliance import run_all as run_compliance_all
from backend.web_simulator import parse_budget

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-nextjs"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_AUTH_CHOICES = ("nextauth", "clerk", "none")
_TARGET_CHOICES = ("vercel", "cloudflare", "both")

_TEMPLATE_SUFFIX = ".j2"

# Files that only make sense for one auth / trpc / target mode. The
# scaffolder skips the irrelevant ones to keep the rendered tree clean.
_AUTH_ONLY_FILES: dict[str, str] = {
    "auth/nextauth.config.ts":       "nextauth",
    "auth/middleware.nextauth.ts":   "nextauth",
    "app/api/auth/[...nextauth]/route.ts": "nextauth",
    "auth/clerk.middleware.ts":      "clerk",
    "auth/clerk.example.tsx":        "clerk",
}

_TRPC_ONLY_FILES: frozenset[str] = frozenset({
    "server/trpc.ts",
    "server/trpc.client.tsx",
    "app/api/trpc/[trpc]/route.ts",
})

_TARGET_ONLY_FILES: dict[str, str] = {
    "vercel.json.j2":   "vercel",
    "wrangler.toml.j2": "cloudflare",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    auth: str = "nextauth"         # nextauth | clerk | none
    trpc: bool = False
    target: str = "both"           # vercel | cloudflare | both
    compliance: bool = True
    backend_url: str = "http://localhost:8000"

    def validate(self) -> None:
        if not self.project_name or not self.project_name.strip():
            raise ValueError("project_name must be non-empty")
        if self.auth not in _AUTH_CHOICES:
            raise ValueError(f"auth must be one of {_AUTH_CHOICES}, got {self.auth!r}")
        if self.target not in _TARGET_CHOICES:
            raise ValueError(f"target must be one of {_TARGET_CHOICES}, got {self.target!r}")

    def resolved_profiles(self) -> list[str]:
        """Which W1 web profile IDs this scaffold binds to."""
        if self.target == "vercel":
            return ["web-vercel"]
        if self.target == "cloudflare":
            return ["web-edge-cloudflare"]
        return ["web-vercel", "web-edge-cloudflare"]


@dataclass
class RenderOutcome:
    out_dir: Path
    files_written: list[Path] = field(default_factory=list)
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)
    profile_bindings: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "out_dir": str(self.out_dir),
            "files_written": [str(p) for p in self.files_written],
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
            "profile_bindings": dict(self.profile_bindings),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _iter_scaffold_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def _should_skip(rel_path: str, opts: ScaffoldOptions) -> bool:
    # Auth-gated files
    for marker, required in _AUTH_ONLY_FILES.items():
        if rel_path == marker and opts.auth != required:
            return True
    # tRPC-gated files
    if rel_path in _TRPC_ONLY_FILES and not opts.trpc:
        return True
    # Target-gated build configs
    for marker, required in _TARGET_ONLY_FILES.items():
        if rel_path == marker and opts.target not in (required, "both"):
            return True
    # Compliance-gated files
    compliance_paths = (
        "docs/privacy/retention.md.j2",
        "docs/privacy/dpa.md.j2",
        "components/consent/CookieBanner.tsx",
        "app/privacy/erasure/route.ts",
        "spdx.allowlist.json",
    )
    if not opts.compliance and rel_path in compliance_paths:
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
        "auth": opts.auth,
        "trpc": opts.trpc,
        "target": opts.target,
        "compliance": opts.compliance,
        "backend_url": opts.backend_url,
    }
    # Resolve W1 profile budgets so the generated vercel.json /
    # wrangler.toml know their ceilings without duplicating values.
    memory_limit = None
    bundle_budget = None
    for profile_id in opts.resolved_profiles():
        try:
            raw = _platform.load_raw_profile(profile_id)
        except Exception:  # noqa: BLE001 — fall through to defaults
            continue
        memory_limit = memory_limit or raw.get("memory_limit_mb")
        bundle_budget = bundle_budget or raw.get("bundle_size_budget")
    ctx["memory_limit_mb"] = memory_limit or 1024
    ctx["bundle_size_budget"] = bundle_budget or "50MiB"
    ctx["bundle_budget_bytes"] = parse_budget(bundle_budget or "50MiB", fallback=50 * 1024 * 1024)
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
    """Render the SKILL-NEXTJS scaffold into ``out_dir``.

    Parameters
    ----------
    out_dir : Path
        Destination project root. Created if missing.
    options : ScaffoldOptions
        Knob values — ``project_name`` is required; everything else
        has a safe default.
    overwrite : bool
        When ``True`` (default), existing files inside the scaffold
        surface are overwritten. Files OUTSIDE the scaffold surface
        are never touched.
    """
    options.validate()
    out_dir = Path(out_dir)
    if not _SCAFFOLDS_DIR.is_dir():
        raise FileNotFoundError(f"scaffolds directory missing: {_SCAFFOLDS_DIR}")

    out_dir.mkdir(parents=True, exist_ok=True)

    env = _build_jinja_env()
    ctx = _render_context(options)

    outcome = RenderOutcome(out_dir=out_dir)
    for profile_id in options.resolved_profiles():
        outcome.profile_bindings[profile_id] = ctx["bundle_budget_bytes"]

    for src in _iter_scaffold_files(_SCAFFOLDS_DIR):
        rel = src.relative_to(_SCAFFOLDS_DIR).as_posix()
        if _should_skip(rel, options):
            continue

        if rel.endswith(_TEMPLATE_SUFFIX):
            out_rel = rel[: -len(_TEMPLATE_SUFFIX)]
            dest = out_dir / out_rel
            if dest.exists() and not overwrite:
                outcome.warnings.append(f"skipped existing: {out_rel}")
                continue
            template = env.get_template(rel)
            rendered = template.render(**ctx)
            outcome.bytes_written += _write_file(dest, rendered)
        else:
            dest = out_dir / rel
            if dest.exists() and not overwrite:
                outcome.warnings.append(f"skipped existing: {rel}")
                continue
            outcome.bytes_written += _write_file(dest, src.read_bytes())
        outcome.files_written.append(dest)

    logger.info(
        "SKILL-NEXTJS rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_deploy(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the W4 deploy adapter classes against the rendered
    project without hitting the network.

    For each target profile we:
      1. Construct the adapter via ``from_plaintext_token`` with a
         throw-away token (adapters never log it — ``token_fp()`` is
         the only exposure).
      2. Build a ``BuildArtifact`` pointed at ``out_dir`` and call
         ``validate()``. This catches mis-shaped artifacts before a
         real deploy would fail mid-upload.

    Return dict shape::

        {"vercel": {"adapter": "VercelAdapter", "artifact_valid": True},
         "cloudflare": {"adapter": "CloudflarePagesAdapter", ...}}
    """
    from backend.deploy.vercel import VercelAdapter
    from backend.deploy.cloudflare_pages import CloudflarePagesAdapter

    results: dict[str, Any] = {}
    art = BuildArtifact(path=out_dir, framework="next")
    try:
        art.validate()
        artifact_ok = True
        artifact_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001
        artifact_ok = False
        artifact_error = str(exc)

    targets = options.resolved_profiles()

    if "web-vercel" in targets:
        adapter = VercelAdapter.from_plaintext_token(
            token="test-token-vercel-placeholder",
            project_name=options.project_name,
        )
        results["vercel"] = {
            "adapter": type(adapter).__name__,
            "provider": adapter.provider,
            "project_name": adapter.project_name,
            "token_fingerprint": adapter.token_fp(),
            "artifact_valid": artifact_ok,
            "artifact_error": artifact_error,
        }

    if "web-edge-cloudflare" in targets:
        adapter = CloudflarePagesAdapter.from_plaintext_token(
            token="test-token-cf-placeholder",
            project_name=options.project_name,
            account_id="00000000000000000000000000000000",
        )
        results["cloudflare"] = {
            "adapter": type(adapter).__name__,
            "provider": adapter.provider,
            "project_name": adapter.project_name,
            "token_fingerprint": adapter.token_fp(),
            "artifact_valid": artifact_ok,
            "artifact_error": artifact_error,
        }

    return results


def pilot_report(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """One-shot W0-W5 gate report for the rendered project.

    Runs the W5 compliance bundle (WCAG / GDPR / SPDX) against the
    rendered directory and layers the W0/W1/W4 adapter bindings on
    top so the caller has a single view of pilot health.
    """
    bundle = run_compliance_all(out_dir)

    return {
        "skill": "skill-nextjs",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "auth": options.auth,
            "trpc": options.trpc,
            "target": options.target,
            "compliance": options.compliance,
        },
        "w0_w1_profiles": options.resolved_profiles(),
        "w4_deploy": dry_run_deploy(out_dir, options),
        "w5_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill_nextjs pack is complete.

    Returns a dict with the skill registry validation result. Used by
    ``test_skill_nextjs.py`` as a living spec — a missing artifact or
    broken manifest trips the test immediately.
    """
    info = get_skill("skill-nextjs")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-nextjs dir missing"]}

    result = validate_skill("skill-nextjs")
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
    "dry_run_deploy",
    "pilot_report",
    "validate_pack",
]

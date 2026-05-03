"""W7 #281 — SKILL-NUXT project scaffolder.

Renders a Nuxt 4 project from the templates shipped in
``configs/skills/skill-nuxt/scaffolds/``. Second web-vertical skill
pack — re-validates the W0-W5 framework on a non-React stack after
W6 SKILL-NEXTJS (#280) proved it on React.

Design
------
The scaffolder deliberately mirrors ``backend.nextjs_scaffolder``:
same ``ScaffoldOptions`` / ``RenderOutcome`` shapes, same Jinja2
environment, same ``dry_run_deploy`` / ``pilot_report`` entry
points. Where the two diverge is the set of knobs — Nuxt has:

* ``pinia``        — on/off Pinia store bundle (Vue state mgmt).
* ``target``       — one of ``node`` / ``vercel`` / ``cloudflare`` /
                     ``bun`` / ``all``. Maps 1:1 to a Nitro preset
                     (``node-server`` / ``vercel`` / ``cloudflare-pages``
                     / ``bun``) and, for adapter dispatch, to the
                     W1 profile the rendered tree binds to.

Why the same API shape matters: if SKILL-NUXT introduced a brand-new
render contract, we'd have two sibling packs with two ways to
render a web project, and the "framework" claim would be in name
only. Keeping the contract identical is precisely what lets us say
the W0-W5 layers survived a second consumer.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``dry_run_deploy()``  — W4 adapter smoke (Vercel / Cloudflare /
                        DockerNginx selected from the target).
``pilot_report()``    — one-shot W0-W5 validation report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import jinja2

from backend import platform as _platform
from backend.deploy.base import BuildArtifact
from backend.skill_registry import get_skill, validate_skill
from backend.web.vite_config_injection import (
    OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH,
    OMNISIGHT_VITE_PLUGIN_PACKAGE,
    OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
    ViteConfigInjectionResult,
    render_omnisight_plugin_bootstrap_module,
)
from backend.web_compliance import run_all as run_compliance_all
from backend.web_simulator import parse_budget

logger = logging.getLogger(__name__)

_SKILL_DIR = (
    Path(__file__).resolve().parent.parent
    / "configs" / "skills" / "skill-nuxt"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_AUTH_CHOICES = ("sidebase", "clerk", "none")
_TARGET_CHOICES = ("node", "vercel", "cloudflare", "bun", "all")

_TEMPLATE_SUFFIX = ".j2"

# Nitro preset pinned into nuxt.config.ts when NITRO_PRESET is unset.
# All-targets renders default to node-server (lowest-common-denominator);
# a single-target render defaults to that target's preset.
_TARGET_NITRO_PRESET: dict[str, str] = {
    "node":       "node-server",
    "vercel":     "vercel",
    "cloudflare": "cloudflare-pages",
    "bun":        "bun",
    "all":        "node-server",
}

# Map a ``target`` selection to the W1 platform profile IDs the
# rendered tree binds to. Bun reuses web-ssr-node because they share
# the same bundle-size / memory envelope from a profile perspective.
_TARGET_PROFILES: dict[str, list[str]] = {
    "node":       ["web-ssr-node"],
    "vercel":     ["web-vercel"],
    "cloudflare": ["web-edge-cloudflare"],
    "bun":        ["web-ssr-node"],
    "all":        ["web-ssr-node", "web-vercel", "web-edge-cloudflare"],
}

# Files that only make sense for a given auth mode.
_AUTH_ONLY_FILES: dict[str, str] = {
    "auth/nuxt-auth.config.ts":    "sidebase",
    "middleware/auth.global.ts":   "sidebase",
    "auth/clerk.example.vue":      "clerk",
}

# Files that only make sense when Pinia is wired.
_PINIA_ONLY_FILES: frozenset[str] = frozenset({
    "stores/counter.ts",
    "tests/unit/counter.test.ts",
    "tests/unit/setup.ts",
})

# Target-gated build configs: path → {targets that want this file}
_TARGET_ONLY_FILES: dict[str, frozenset[str]] = {
    "vercel.json.j2":   frozenset({"vercel", "all"}),
    "wrangler.toml.j2": frozenset({"cloudflare", "all"}),
    "Dockerfile.j2":    frozenset({"node", "bun", "all"}),
    "bunfig.toml":      frozenset({"bun", "all"}),
}

# Compliance-gated files (skipped when compliance=False).
_COMPLIANCE_PATHS: tuple[str, ...] = (
    "docs/privacy/retention.md.j2",
    "docs/privacy/dpa.md.j2",
    "components/consent/CookieBanner.vue",
    "server/api/privacy/erasure.post.ts",
    "spdx.allowlist.json",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions:
    project_name: str
    auth: str = "sidebase"         # sidebase | clerk | none
    pinia: bool = True
    target: str = "all"            # node | vercel | cloudflare | bun | all
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
        return list(_TARGET_PROFILES[self.target])

    def default_nitro_preset(self) -> str:
        """Scaffold-time default for `nuxt.config.ts` `nitro.preset`."""
        return _TARGET_NITRO_PRESET[self.target]


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
    # Pinia-gated files
    if rel_path in _PINIA_ONLY_FILES and not opts.pinia:
        return True
    # Target-gated build configs
    for marker, wanted in _TARGET_ONLY_FILES.items():
        if rel_path == marker and opts.target not in wanted:
            return True
    # Compliance-gated files
    if not opts.compliance and rel_path in _COMPLIANCE_PATHS:
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
        "pinia": opts.pinia,
        "target": opts.target,
        "compliance": opts.compliance,
        "backend_url": opts.backend_url,
        "default_nitro_preset": opts.default_nitro_preset(),
    }

    # Resolve the W1 profile budgets. For "all" targets we want the
    # tightest budget (the Cloudflare 1 MiB ceiling) to feed the W2
    # bundle gate, while the Vercel memory limit needs to be carried
    # independently because it only applies to the serverless profile.
    bundle_budget: Optional[str] = None
    tightest_bytes: Optional[int] = None
    vercel_memory_limit: Optional[int] = None
    node_memory_limit: Optional[int] = None

    for profile_id in opts.resolved_profiles():
        try:
            raw = _platform.load_raw_profile(profile_id)
        except Exception:  # noqa: BLE001 — fall through to defaults
            continue

        b = raw.get("bundle_size_budget")
        if b:
            parsed = parse_budget(b, fallback=5 * 1024 * 1024)
            if tightest_bytes is None or parsed < tightest_bytes:
                tightest_bytes = parsed
                bundle_budget = b

        if profile_id == "web-vercel":
            vercel_memory_limit = raw.get("memory_limit_mb")
        elif profile_id == "web-ssr-node":
            node_memory_limit = raw.get("memory_limit_mb")

    ctx["bundle_size_budget"] = bundle_budget or "5MiB"
    ctx["bundle_budget_bytes"] = tightest_bytes or 5 * 1024 * 1024
    ctx["vercel_memory_limit_mb"] = vercel_memory_limit or 1024
    ctx["node_memory_limit_mb"] = node_memory_limit or 512
    return ctx


def _write_file(dest: Path, content: bytes | str) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        dest.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))
    dest.write_bytes(content)
    return len(content)


def _write_omnisight_vite_plugin_bootstrap(
    out_dir: Path, *, overwrite: bool,
) -> tuple[Optional[ViteConfigInjectionResult], Optional[Path]]:
    """W15.5 — write the omnisight-vite-plugin bootstrap module into
    ``<out_dir>/scripts/omnisight-vite-plugin.mjs``.

    Returns a ``(result, dest_path)`` tuple where ``result`` describes
    what landed (``None`` when skipped because the file existed and
    ``overwrite=False``).  The bootstrap is sourced from
    :func:`backend.web.vite_config_injection.render_omnisight_plugin_bootstrap_module`
    so the W6/W7/W8 scaffolders all write byte-identical content.
    """
    bootstrap_dest = out_dir / OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH
    if bootstrap_dest.exists() and not overwrite:
        return None, bootstrap_dest
    bootstrap_text = render_omnisight_plugin_bootstrap_module()
    written = _write_file(bootstrap_dest, bootstrap_text)
    return (
        ViteConfigInjectionResult(
            bootstrap_relative_path=OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH,
            bootstrap_bytes=written,
            package_name=OMNISIGHT_VITE_PLUGIN_PACKAGE,
            package_version=OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
        ),
        bootstrap_dest,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-NUXT scaffold into ``out_dir``.

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

    # W15.5 — write the omnisight-vite-plugin bootstrap module so the
    # rendered nuxt.config.ts's `./scripts/omnisight-vite-plugin.mjs`
    # import resolves.  Idempotent: re-rendering with overwrite=True
    # rewrites the file from the central template so a future bump
    # propagates on the next render; overwrite=False preserves any
    # operator edits that happened to land at the same relative path.
    bootstrap_result, bootstrap_dest = _write_omnisight_vite_plugin_bootstrap(
        out_dir, overwrite=overwrite,
    )
    if bootstrap_result is not None and bootstrap_dest is not None:
        outcome.bytes_written += bootstrap_result.bootstrap_bytes
        outcome.files_written.append(bootstrap_dest)
    elif bootstrap_dest is not None:
        outcome.warnings.append(
            f"skipped existing: {OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH}"
        )

    logger.info(
        "SKILL-NUXT rendered %d files (%d bytes) into %s",
        len(outcome.files_written), outcome.bytes_written, out_dir,
    )
    return outcome


def dry_run_deploy(
    out_dir: Path,
    options: ScaffoldOptions,
) -> dict[str, Any]:
    """Exercise the W4 deploy adapter classes against the rendered
    project without hitting the network.

    For each target the scaffold requested:
      * ``vercel``     → ``VercelAdapter``          (web-vercel profile)
      * ``cloudflare`` → ``CloudflarePagesAdapter`` (web-edge-cloudflare)
      * ``node``/``bun`` → ``DockerNginxAdapter``   (container path
                          — the W4 adapter family's on-disk outlier
                          that doesn't need a remote API)

    Return dict shape::

        {"vercel":     {"adapter": "VercelAdapter",         ...},
         "cloudflare": {"adapter": "CloudflarePagesAdapter", ...},
         "docker":     {"adapter": "DockerNginxAdapter",     ...}}
    """
    from backend.deploy.vercel import VercelAdapter
    from backend.deploy.cloudflare_pages import CloudflarePagesAdapter
    from backend.deploy.docker_nginx import DockerNginxAdapter

    results: dict[str, Any] = {}
    art = BuildArtifact(path=out_dir, framework="nuxt")
    try:
        art.validate()
        artifact_ok = True
        artifact_error: Optional[str] = None
    except Exception as exc:  # noqa: BLE001
        artifact_ok = False
        artifact_error = str(exc)

    targets = options.target

    if targets in ("vercel", "all"):
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

    if targets in ("cloudflare", "all"):
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

    if targets in ("node", "bun", "all"):
        adapter = DockerNginxAdapter.from_plaintext_token(
            token="",
            project_name=options.project_name,
        )
        results["docker"] = {
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
    top so the caller has a single view of cross-stack health.
    """
    bundle = run_compliance_all(out_dir)

    return {
        "skill": "skill-nuxt",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "auth": options.auth,
            "pinia": options.pinia,
            "target": options.target,
            "compliance": options.compliance,
        },
        "w0_w1_profiles": options.resolved_profiles(),
        "nitro_preset_default": options.default_nitro_preset(),
        "w4_deploy": dry_run_deploy(out_dir, options),
        "w5_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-nuxt pack is complete."""
    info = get_skill("skill-nuxt")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-nuxt dir missing"]}

    result = validate_skill("skill-nuxt")
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

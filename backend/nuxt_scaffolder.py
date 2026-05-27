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
* ``drizzle``      — on/off Drizzle ORM data layer for FS.7.2.
* ``postmark``     — on/off Postmark contact-email route for FS.7.2.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from backend import platform_profile as _platform
from backend.deploy.base import BuildArtifact
from backend.scaffolder_base import RenderOutcome, ScaffolderBase
from backend.scaffolder_base import ScaffoldOptions as _ScaffoldOptionsBase
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

_DRIZZLE_ONLY_FILES: frozenset[str] = frozenset({
    "drizzle/schema.ts",
    "server/db.ts",
})

_POSTMARK_ONLY_FILES: frozenset[str] = frozenset({
    "server/email.ts",
    "server/api/contact.post.ts.j2",
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
class ScaffoldOptions(_ScaffoldOptionsBase):
    """SKILL-NUXT knobs — extends the shared base with Nuxt fields.

    ``project_name`` (+ its non-empty check) is inherited from
    :class:`backend.scaffolder_base.ScaffoldOptions`; :meth:`validate`
    calls ``super().validate()`` then layers the auth / target enum
    rules on top.
    """

    auth: str = "sidebase"         # sidebase | clerk | none
    pinia: bool = True
    drizzle: bool = False
    postmark: bool = False
    target: str = "all"            # node | vercel | cloudflare | bun | all
    compliance: bool = True
    backend_url: str = "http://localhost:8000"

    def validate(self) -> None:
        super().validate()
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _NuxtScaffolder(ScaffolderBase):
    """SKILL-NUXT scaffolder — supplies the gating + context hooks.

    The render loop, Jinja env, and byte-level writes live in
    :class:`backend.scaffolder_base.ScaffolderBase`; this subclass only
    declares what is Nuxt-specific. :meth:`render_project` is overridden
    to append the W15.5 vite-plugin bootstrap write after the shared
    loop completes (the base loop has no post-render hook).
    """

    def should_skip(self, rel_path: str, options: ScaffoldOptions) -> bool:
        # Auth-gated files
        for marker, required in _AUTH_ONLY_FILES.items():
            if rel_path == marker and options.auth != required:
                return True
        # Pinia-gated files
        if rel_path in _PINIA_ONLY_FILES and not options.pinia:
            return True
        # Drizzle-gated files
        if rel_path in _DRIZZLE_ONLY_FILES and not options.drizzle:
            return True
        # Postmark-gated files
        if rel_path in _POSTMARK_ONLY_FILES and not options.postmark:
            return True
        # Target-gated build configs
        for marker, wanted in _TARGET_ONLY_FILES.items():
            if rel_path == marker and options.target not in wanted:
                return True
        # Compliance-gated files
        if not options.compliance and rel_path in _COMPLIANCE_PATHS:
            return True
        return False

    def build_context(self, options: ScaffoldOptions) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "project_name": options.project_name,
            "auth": options.auth,
            "pinia": options.pinia,
            "drizzle": options.drizzle,
            "postmark": options.postmark,
            "target": options.target,
            "compliance": options.compliance,
            "backend_url": options.backend_url,
            "default_nitro_preset": options.default_nitro_preset(),
        }

        # Resolve the W1 profile budgets. For "all" targets we want the
        # tightest budget (the Cloudflare 1 MiB ceiling) to feed the W2
        # bundle gate, while the Vercel memory limit needs to be carried
        # independently because it only applies to the serverless profile.
        bundle_budget: Optional[str] = None
        tightest_bytes: Optional[int] = None
        vercel_memory_limit: Optional[int] = None
        node_memory_limit: Optional[int] = None

        for profile_id in options.resolved_profiles():
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

    def make_outcome(self, out_dir: Path, context: dict[str, Any]) -> RenderOutcome:
        outcome = RenderOutcome(out_dir=out_dir)
        outcome.profile_binding = {
            profile_id: context["bundle_budget_bytes"]
            for profile_id in _profiles_for_context(context)
        }
        return outcome

    def render_project(
        self,
        out_dir: Path,
        options: ScaffoldOptions,
        *,
        overwrite: bool = True,
    ) -> RenderOutcome:
        outcome = super().render_project(out_dir, options, overwrite=overwrite)

        # W15.5 — write the omnisight-vite-plugin bootstrap module so the
        # rendered nuxt.config.ts's `./scripts/omnisight-vite-plugin.mjs`
        # import resolves.  Idempotent: re-rendering with overwrite=True
        # rewrites the file from the central template so a future bump
        # propagates on the next render; overwrite=False preserves any
        # operator edits that happened to land at the same relative path.
        bootstrap_result, bootstrap_dest = self._write_omnisight_vite_plugin_bootstrap(
            Path(out_dir), overwrite=overwrite,
        )
        if bootstrap_result is not None and bootstrap_dest is not None:
            outcome.bytes_written += bootstrap_result.bootstrap_bytes
            outcome.files_written.append(bootstrap_dest)
        elif bootstrap_dest is not None:
            outcome.warnings.append(
                f"skipped existing: {OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH}"
            )
        return outcome

    def _write_omnisight_vite_plugin_bootstrap(
        self, out_dir: Path, *, overwrite: bool,
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
        written = self._write_file(bootstrap_dest, bootstrap_text)
        return (
            ViteConfigInjectionResult(
                bootstrap_relative_path=OMNISIGHT_VITE_PLUGIN_BOOTSTRAP_RELATIVE_PATH,
                bootstrap_bytes=written,
                package_name=OMNISIGHT_VITE_PLUGIN_PACKAGE,
                package_version=OMNISIGHT_VITE_PLUGIN_PACKAGE_VERSION,
            ),
            bootstrap_dest,
        )


def _profiles_for_context(context: dict[str, Any]) -> list[str]:
    """Profiles bound by a render context — mirrors
    :meth:`ScaffoldOptions.resolved_profiles` off the resolved ``target``."""
    target = context.get("target", "all")
    return list(_TARGET_PROFILES[target])


#: Module-level singleton — the scaffold dir is fixed per skill pack.
_SCAFFOLDER = _NuxtScaffolder(_SCAFFOLDS_DIR, skill_label="SKILL-NUXT")


def _render_context(opts: ScaffoldOptions) -> dict[str, Any]:
    """SKILL-NUXT Jinja render context (delegates to the scaffolder)."""
    return _SCAFFOLDER.build_context(opts)


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

    Thin façade over :data:`_SCAFFOLDER`; the render machinery lives in
    :class:`backend.scaffolder_base.ScaffolderBase`.

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
    return _SCAFFOLDER.render_project(out_dir, options, overwrite=overwrite)


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
            "drizzle": options.drizzle,
            "postmark": options.postmark,
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

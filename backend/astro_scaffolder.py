"""W8 #282 — SKILL-ASTRO project scaffolder.

Renders an Astro 5 project from the templates shipped in
``configs/skills/skill-astro/scaffolds/``. Third web-vertical skill
pack — extends the W0-W5 framework onto the content-heavy vertical
(SSG-by-default, optional SSR, Islands architecture, MDX,
headless CMS) after W6 SKILL-NEXTJS (#280) proved it on React and
W7 SKILL-NUXT (#281) proved it on Vue/Nitro.

Design
------
The scaffolder deliberately mirrors ``backend.nextjs_scaffolder`` and
``backend.nuxt_scaffolder``: same ``ScaffoldOptions`` / ``RenderOutcome``
shapes, same Jinja2 environment, same ``dry_run_deploy`` /
``pilot_report`` entry points. Where this one diverges is the set of
knobs — Astro has:

* ``islands``   — hydration framework for interactive islands
                  (``react`` / ``vue`` / ``svelte`` / ``none``).
* ``cms``       — headless CMS source adapter (``sanity`` /
                  ``contentful`` / ``none``). ``none`` ships a
                  local-MDX-only tree (the Astro default).
* ``target``    — build target (``static`` / ``node`` / ``vercel`` /
                  ``cloudflare`` / ``all``). Maps 1:1 to the Astro
                  output mode + adapter pair and, for adapter
                  dispatch, to the W1 profile the rendered tree
                  binds to.

Keeping the API shape identical across the three web scaffolders is
exactly what lets the W0-W5 layers claim "framework" rather than
"pilot plus two copies". The per-skill knobs vary; the envelope does
not.

Shared base (OP-1791, W3 1C)
----------------------------
The render loop, Jinja env, and byte-level writes live in
:class:`backend.scaffolder_base.ScaffolderBase`; this module only
declares what is Astro-specific (knob gating, render context, profile
binding) plus the W15.5 vite-plugin bootstrap write that runs after the
loop. ``ScaffoldOptions`` subclasses the shared base; ``RenderOutcome``
is re-exported from the base unchanged. Mirrors the OP-1784 ``android``
and OP-1788/1789/1790 (ios/nextjs/fastapi) migrations exactly.

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
    / "configs" / "skills" / "skill-astro"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_ISLAND_CHOICES = ("react", "vue", "svelte", "none")
_CMS_CHOICES = ("sanity", "contentful", "none")
_TARGET_CHOICES = ("static", "node", "vercel", "cloudflare", "all")

# Default `ASTRO_TARGET` pinned into astro.config.mjs. A single-target
# render defaults to that target's value; `all` defaults to `static`
# (lowest-common-denominator — every Astro project can build static).
_TARGET_DEFAULT: dict[str, str] = {
    "static":     "static",
    "node":       "node",
    "vercel":     "vercel",
    "cloudflare": "cloudflare",
    "all":        "static",
}

# Map a `target` selection to the W1 platform profile IDs the rendered
# tree binds to.
_TARGET_PROFILES: dict[str, list[str]] = {
    "static":     ["web-static"],
    "node":       ["web-ssr-node"],
    "vercel":     ["web-vercel"],
    "cloudflare": ["web-edge-cloudflare"],
    "all":        ["web-static", "web-ssr-node", "web-vercel", "web-edge-cloudflare"],
}

# File extension the MDX seed uses for the island component import.
_ISLAND_EXT: dict[str, str] = {
    "react":  ".jsx",
    "vue":    ".vue",
    "svelte": ".svelte",
    "none":   "",
}

# Files gated by the `islands` knob — only one of Counter.{jsx,vue,svelte}
# ships at a time; `none` ships none of them.
_ISLANDS_ONLY_FILES: dict[str, str] = {
    "src/components/Counter.jsx":    "react",
    "src/components/Counter.vue":    "vue",
    "src/components/Counter.svelte": "svelte",
}

# Files gated by the `cms` knob.
_CMS_ONLY_FILES: dict[str, str] = {
    "src/lib/cms/sanity.ts":                   "sanity",
    "src/lib/cms/contentful.ts":               "contentful",
    "src/pages/api/webhooks/sanity.ts":        "sanity",
    "src/pages/api/webhooks/contentful.ts":    "contentful",
}

# CMS unit test only makes sense when a CMS is actually wired.
_CMS_TESTS_FILES: frozenset[str] = frozenset({
    "tests/unit/cms.test.ts",
})

# Target-gated build configs: path → {targets that want this file}
_TARGET_ONLY_FILES: dict[str, frozenset[str]] = {
    "vercel.json.j2":   frozenset({"vercel", "all"}),
    "wrangler.toml.j2": frozenset({"cloudflare", "all"}),
    "Dockerfile.j2":    frozenset({"static", "node", "all"}),
}

# Compliance-gated files (skipped when compliance=False).
_COMPLIANCE_PATHS: tuple[str, ...] = (
    "docs/privacy/retention.md.j2",
    "docs/privacy/dpa.md.j2",
    "src/components/ConsentBanner.astro",
    "src/lib/consent.ts",
    "src/pages/api/privacy/erasure.ts",
    "spdx.allowlist.json",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions(_ScaffoldOptionsBase):
    """SKILL-ASTRO knobs — extends the shared base with content fields.

    ``project_name`` (+ its non-empty check) is inherited from
    :class:`backend.scaffolder_base.ScaffoldOptions`; :meth:`validate`
    calls ``super().validate()`` then layers the islands / cms / target
    enum rules on top.
    """

    islands: str = "react"          # react | vue | svelte | none
    cms: str = "none"               # sanity | contentful | none
    target: str = "static"          # static | node | vercel | cloudflare | all
    compliance: bool = True
    # `auth` is not used by Astro (the content vertical rarely needs
    # app-level auth) but is kept on the dataclass for API parity
    # with SKILL-NEXTJS / SKILL-NUXT so the upstream orchestrator can
    # pass one ScaffoldOptions through all three.
    auth: str = "none"
    backend_url: str = "http://localhost:8000"

    def validate(self) -> None:
        super().validate()
        if self.islands not in _ISLAND_CHOICES:
            raise ValueError(f"islands must be one of {_ISLAND_CHOICES}, got {self.islands!r}")
        if self.cms not in _CMS_CHOICES:
            raise ValueError(f"cms must be one of {_CMS_CHOICES}, got {self.cms!r}")
        if self.target not in _TARGET_CHOICES:
            raise ValueError(f"target must be one of {_TARGET_CHOICES}, got {self.target!r}")

    def resolved_profiles(self) -> list[str]:
        """Which W1 web profile IDs this scaffold binds to."""
        return list(_TARGET_PROFILES[self.target])

    def default_target(self) -> str:
        """Scaffold-time default for `astro.config.mjs` `ASTRO_TARGET`."""
        return _TARGET_DEFAULT[self.target]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _AstroScaffolder(ScaffolderBase):
    """SKILL-ASTRO scaffolder — supplies the gating + context hooks.

    The render loop, Jinja env, and byte-level writes live in
    :class:`backend.scaffolder_base.ScaffolderBase`; this subclass only
    declares what is Astro-specific. :meth:`render_project` is
    overridden to append the W15.5 vite-plugin bootstrap write after the
    shared loop completes (the base loop has no post-render hook).
    """

    def should_skip(self, rel_path: str, options: ScaffoldOptions) -> bool:
        # Islands-gated files — only the matching framework's component
        # ships; `islands=none` ships none of them.
        for marker, required in _ISLANDS_ONLY_FILES.items():
            if rel_path == marker and options.islands != required:
                return True
        # CMS-gated adapter + webhook files
        for marker, required in _CMS_ONLY_FILES.items():
            if rel_path == marker and options.cms != required:
                return True
        # CMS unit test only when a CMS is wired
        if rel_path in _CMS_TESTS_FILES and options.cms == "none":
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
            "islands": options.islands,
            "island_ext": _ISLAND_EXT[options.islands],
            "cms": options.cms,
            "target": options.target,
            "compliance": options.compliance,
            "auth": options.auth,
            "backend_url": options.backend_url,
            "default_target": options.default_target(),
        }

        # Resolve the W1 profile budgets. For "all" targets we want the
        # TIGHTEST bundle budget (Cloudflare 1 MiB wins over web-static's
        # 500 KiB? No — 500 KiB is tighter. So web-static wins for the
        # budget context, and web-edge-cloudflare wins when static is not
        # in the mix). The tightest-wins rule carries over from the W7
        # scaffolder — the intent is that the W2 bundle gate fires on the
        # most restrictive target the render supports.
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

        ctx["bundle_size_budget"] = bundle_budget or "500KiB"
        ctx["bundle_budget_bytes"] = tightest_bytes or 500 * 1024
        ctx["vercel_memory_limit_mb"] = vercel_memory_limit or 1024
        ctx["node_memory_limit_mb"] = node_memory_limit or 512
        return ctx

    def make_outcome(self, out_dir: Path, context: dict[str, Any]) -> RenderOutcome:
        outcome = RenderOutcome(out_dir=out_dir)
        # One entry per bound W1 profile → its resolved bundle budget.
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
        # rendered astro.config.mjs's `./scripts/omnisight-vite-plugin.mjs`
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
    return list(_TARGET_PROFILES[context.get("target", "static")])


#: Module-level singleton — the scaffold dir is fixed per skill pack.
_SCAFFOLDER = _AstroScaffolder(_SCAFFOLDS_DIR, skill_label="SKILL-ASTRO")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _render_context(opts: ScaffoldOptions) -> dict[str, Any]:
    """SKILL-ASTRO Jinja render context (delegates to the scaffolder)."""
    return _SCAFFOLDER.build_context(opts)


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-ASTRO scaffold into ``out_dir``.

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
      * ``node``       → ``DockerNginxAdapter``     (container path)
      * ``static``     → ``DockerNginxAdapter``     (serve dist/ via nginx;
                         the W4 family's static-host reference)

    Return dict shape mirrors ``nuxt_scaffolder.dry_run_deploy`` so the
    upstream orchestrator can aggregate outcomes across skills.
    """
    from backend.deploy.vercel import VercelAdapter
    from backend.deploy.cloudflare_pages import CloudflarePagesAdapter
    from backend.deploy.docker_nginx import DockerNginxAdapter

    results: dict[str, Any] = {}
    art = BuildArtifact(path=out_dir, framework="astro")
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

    if targets in ("static", "node", "all"):
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
    top so the caller has a single view of cross-stack health. Shape
    mirrors ``nuxt_scaffolder.pilot_report`` / ``nextjs_scaffolder.pilot_report``
    — that is the "framework survived" invariant.
    """
    bundle = run_compliance_all(out_dir)

    return {
        "skill": "skill-astro",
        "out_dir": str(out_dir),
        "options": {
            "project_name": options.project_name,
            "islands": options.islands,
            "cms": options.cms,
            "target": options.target,
            "compliance": options.compliance,
        },
        "w0_w1_profiles": options.resolved_profiles(),
        "astro_target_default": options.default_target(),
        "w4_deploy": dry_run_deploy(out_dir, options),
        "w5_compliance": bundle.to_dict(),
    }


def validate_pack() -> dict[str, Any]:
    """Self-check that the installed skill-astro pack is complete."""
    info = get_skill("skill-astro")
    if info is None:
        return {"installed": False, "ok": False, "issues": ["skill-astro dir missing"]}

    result = validate_skill("skill-astro")
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
    "_render_context",
    "_SCAFFOLDS_DIR",
    "_SKILL_DIR",
]

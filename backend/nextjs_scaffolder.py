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
  profile from ``backend.platform_profile.get_platform_config`` so the
  ``bundle_size_budget`` / ``memory_limit_mb`` read straight from
  the W1 profile, not a copy.
* **Dry-run deploy** — ``dry_run_deploy()`` calls the W4 adapter's
  constructor path + a fake BuildArtifact validation to prove the
  generated project hands off cleanly, without hitting the network.
* **Example app** — optional example surfaces are scaffold-only files
  so the default skeleton stays unchanged while FS.7.4 can render a
  complete todo app inside the same full-stack bundle.

Shared base (OP-1789, W3 1C)
----------------------------
The render loop, Jinja env, and byte-level writes live in
:class:`backend.scaffolder_base.ScaffolderBase`; this module only
declares what is Next.js-specific (knob gating, render context, profile
binding) plus the W15.5 vite-plugin bootstrap write that runs after the
loop. ``ScaffoldOptions`` subclasses the shared base; ``RenderOutcome``
is re-exported from the base unchanged. Mirrors the OP-1784 ``android``
migration exactly.

Public API
----------
``ScaffoldOptions``   — knobs that parameterise the render.
``RenderOutcome``     — files written, size totals, warnings.
``render_project()``  — main entry point.
``dry_run_deploy()``  — W4 adapter smoke.
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
    / "configs" / "skills" / "skill-nextjs"
)
_SCAFFOLDS_DIR = _SKILL_DIR / "scaffolds"

_AUTH_CHOICES = ("nextauth", "clerk", "none")
_TARGET_CHOICES = ("vercel", "cloudflare", "both")
_EXAMPLE_APP_CHOICES = ("none", "todo")

# Files that only make sense for one auth / trpc / prisma / resend /
# target mode. The scaffolder skips the irrelevant ones to keep the
# rendered tree clean.
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

_PRISMA_ONLY_FILES: frozenset[str] = frozenset({
    "prisma/schema.prisma.j2",
    "server/db.ts",
})

_RESEND_ONLY_FILES: frozenset[str] = frozenset({
    "server/email.ts",
    "app/api/contact/route.ts.j2",
})

_TARGET_ONLY_FILES: dict[str, str] = {
    "vercel.json.j2":   "vercel",
    "wrangler.toml.j2": "cloudflare",
}

_EXAMPLE_ONLY_FILES: dict[str, str] = {
    "app/todos/page.tsx":              "todo",
    "components/TodoApp.tsx":          "todo",
    "tests/unit/todo-app.test.tsx":    "todo",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScaffoldOptions(_ScaffoldOptionsBase):
    """SKILL-NEXTJS knobs — extends the shared base with web fields.

    ``project_name`` (+ its non-empty check) is inherited from
    :class:`backend.scaffolder_base.ScaffoldOptions`; :meth:`validate`
    calls ``super().validate()`` then layers the auth / target /
    example-app enum rules on top.
    """

    auth: str = "nextauth"         # nextauth | clerk | none
    trpc: bool = False
    prisma: bool = False
    resend: bool = False
    target: str = "both"           # vercel | cloudflare | both
    compliance: bool = True
    backend_url: str = "http://localhost:8000"
    example_app: str = "none"      # none | todo

    def validate(self) -> None:
        super().validate()
        if self.auth not in _AUTH_CHOICES:
            raise ValueError(f"auth must be one of {_AUTH_CHOICES}, got {self.auth!r}")
        if self.target not in _TARGET_CHOICES:
            raise ValueError(f"target must be one of {_TARGET_CHOICES}, got {self.target!r}")
        if self.example_app not in _EXAMPLE_APP_CHOICES:
            raise ValueError(
                f"example_app must be one of {_EXAMPLE_APP_CHOICES}, got {self.example_app!r}"
            )

    def resolved_profiles(self) -> list[str]:
        """Which W1 web profile IDs this scaffold binds to."""
        if self.target == "vercel":
            return ["web-vercel"]
        if self.target == "cloudflare":
            return ["web-edge-cloudflare"]
        return ["web-vercel", "web-edge-cloudflare"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffolder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _NextjsScaffolder(ScaffolderBase):
    """SKILL-NEXTJS scaffolder — supplies the gating + context hooks.

    The render loop, Jinja env, and byte-level writes live in
    :class:`backend.scaffolder_base.ScaffolderBase`; this subclass only
    declares what is Next.js-specific. :meth:`render_project` is
    overridden to append the W15.5 vite-plugin bootstrap write after the
    shared loop completes (the base loop has no post-render hook).
    """

    def should_skip(self, rel_path: str, options: ScaffoldOptions) -> bool:
        # Auth-gated files
        for marker, required in _AUTH_ONLY_FILES.items():
            if rel_path == marker and options.auth != required:
                return True
        # tRPC-gated files
        if rel_path in _TRPC_ONLY_FILES and not options.trpc:
            return True
        # Prisma-gated files
        if rel_path in _PRISMA_ONLY_FILES and not options.prisma:
            return True
        # Resend-gated files
        if rel_path in _RESEND_ONLY_FILES and not options.resend:
            return True
        # Target-gated build configs
        for marker, required in _TARGET_ONLY_FILES.items():
            if rel_path == marker and options.target not in (required, "both"):
                return True
        # Example-app-gated files
        for marker, required in _EXAMPLE_ONLY_FILES.items():
            if rel_path == marker and options.example_app != required:
                return True
        # Compliance-gated files
        compliance_paths = (
            "docs/privacy/retention.md.j2",
            "docs/privacy/dpa.md.j2",
            "components/consent/CookieBanner.tsx",
            "app/privacy/erasure/route.ts",
            "spdx.allowlist.json",
        )
        if not options.compliance and rel_path in compliance_paths:
            return True
        return False

    def build_context(self, options: ScaffoldOptions) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "project_name": options.project_name,
            "auth": options.auth,
            "trpc": options.trpc,
            "prisma": options.prisma,
            "resend": options.resend,
            "target": options.target,
            "compliance": options.compliance,
            "backend_url": options.backend_url,
            "example_app": options.example_app,
        }
        # Resolve W1 profile budgets so the generated vercel.json /
        # wrangler.toml know their ceilings without duplicating values.
        memory_limit = None
        bundle_budget = None
        for profile_id in options.resolved_profiles():
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
        # rendered vitest.config.ts's `./scripts/omnisight-vite-plugin.mjs`
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
    target = context.get("target", "both")
    if target == "vercel":
        return ["web-vercel"]
    if target == "cloudflare":
        return ["web-edge-cloudflare"]
    return ["web-vercel", "web-edge-cloudflare"]


#: Module-level singleton — the scaffold dir is fixed per skill pack.
_SCAFFOLDER = _NextjsScaffolder(_SCAFFOLDS_DIR, skill_label="SKILL-NEXTJS")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _render_context(opts: ScaffoldOptions) -> dict[str, Any]:
    """SKILL-NEXTJS Jinja render context (delegates to the scaffolder)."""
    return _SCAFFOLDER.build_context(opts)


def render_project(
    out_dir: Path,
    options: ScaffoldOptions,
    *,
    overwrite: bool = True,
) -> RenderOutcome:
    """Render the SKILL-NEXTJS scaffold into ``out_dir``.

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
            "prisma": options.prisma,
            "resend": options.resend,
            "target": options.target,
            "compliance": options.compliance,
            "example_app": options.example_app,
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
    "RenderOutcome",
    "ScaffoldOptions",
    "render_project",
    "dry_run_deploy",
    "pilot_report",
    "validate_pack",
    "_render_context",
    "_SCAFFOLDS_DIR",
    "_SKILL_DIR",
    "_PRISMA_ONLY_FILES",
    "_RESEND_ONLY_FILES",
    "_TRPC_ONLY_FILES",
]

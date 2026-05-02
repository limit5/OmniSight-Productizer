"""W6 #280 — SKILL-NEXTJS pilot skill contract tests.

SKILL-NEXTJS is the first web-vertical skill pack and the pilot that
validates the W0-W5 framework end-to-end. These tests lock the
framework invariants the same way D1 SKILL-UVC locked C5 and D29
SKILL-HMI-WEBUI locked C26:

* **W0** — ``target_kind=web`` dispatch works for the profiles the
  scaffold binds to.
* **W1** — resolved ``bundle_size_budget`` / ``memory_limit_mb`` are
  the *real* profile values, not duplicated constants.
* **W3** — rendered project matches ``frontend-react`` role
  anti-patterns (Server Components for data, Client Components for
  interaction, no ``useEffect`` for fetching).
* **W4** — ``VercelAdapter`` and ``CloudflarePagesAdapter`` both
  construct against the rendered artifact without hitting the
  network.
* **W5** — compliance bundle passes (or skips cleanly) against the
  rendered project; GDPR retention / DPA / erasure handler shipped.

The Turbopack ``root`` pin is explicitly checked — we hit this panic
in OmniSight's own ``next.config.mjs`` and the scaffold has to ship
the fix so every generated project inherits it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.deploy.base import BuildArtifact
from backend.nextjs_scaffolder import (
    ScaffoldOptions,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
    _PRISMA_ONLY_FILES,
    _RESEND_ONLY_FILES,
    _TRPC_ONLY_FILES,
    _render_context,
    dry_run_deploy,
    pilot_report,
    render_project,
    validate_pack,
)
from backend.platform import load_raw_profile
from backend.skill_registry import get_skill, list_skills, validate_skill


@pytest.fixture
def project_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "pilot-app"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="pilot-app",
        auth="nextauth",
        trpc=False,
        prisma=False,
        resend=False,
        target="both",
        compliance=True,
        example_app="none",
    )
    kwargs.update(overrides)
    return ScaffoldOptions(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack registry invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert "skill-nextjs" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-nextjs")
        assert result.ok, (
            f"skill-nextjs validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-nextjs")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-nextjs")
        assert info is not None
        assert info.manifest is not None
        # CORE-05 is the skill pack framework itself; CORE-21 the
        # enterprise_web reference. Both must stay pinned.
        assert "CORE-05" in info.manifest.depends_on_core
        assert "enterprise_web" in info.manifest.depends_on_skills

    def test_manifest_keywords_include_pilot_marker(self):
        info = get_skill("skill-nextjs")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        # "w6" marks the pilot milestone; "pilot" + "turbopack" make the
        # pack findable by operators debugging the workspace-root panic.
        assert {"pilot", "w6", "turbopack", "nextjs", "fs-7-1"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-nextjs"

    def test_skill_dir_resolution(self):
        assert _SKILL_DIR.is_dir()
        assert (_SKILL_DIR / "skill.yaml").is_file()
        assert (_SKILL_DIR / "tasks.yaml").is_file()
        assert _SCAFFOLDS_DIR.is_dir()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Scaffold render (unit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestScaffoldRender:
    def test_render_writes_core_files(self, project_dir):
        outcome = render_project(project_dir, _default_opts())
        must_exist = [
            "package.json",
            "next.config.mjs",
            "tsconfig.json",
            ".gitignore",
            "app/layout.tsx",
            "app/page.tsx",
            "app/globals.css",
            "app/actions.ts",
            "app/api/health/route.ts",
            "app/api/v1/[...slug]/route.ts",
            "components/Counter.tsx",
            "playwright.config.ts",
            "vitest.config.ts",
            "e2e/smoke.spec.ts",
            "tests/unit/counter.test.tsx",
        ]
        for rel in must_exist:
            assert (project_dir / rel).is_file(), f"missing: {rel}"
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_turbopack_root_is_pinned(self, project_dir):
        """Regression test — OmniSight itself hit the workspace-root
        panic on Next 16 pre-releases. The scaffold MUST ship the fix."""
        render_project(project_dir, _default_opts())
        config = (project_dir / "next.config.mjs").read_text()
        assert "turbopack:" in config
        assert "root: __dirname" in config
        # And the __dirname line that powers the pin must also exist.
        assert "fileURLToPath" in config

    def test_package_json_branches_on_auth(self, project_dir):
        render_project(project_dir, _default_opts(auth="nextauth"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "next-auth" in pkg["dependencies"]
        assert "@clerk/nextjs" not in pkg["dependencies"]

    def test_package_json_branches_on_clerk(self, project_dir):
        render_project(project_dir, _default_opts(auth="clerk"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@clerk/nextjs" in pkg["dependencies"]
        assert "next-auth" not in pkg["dependencies"]

    def test_package_json_branches_on_trpc(self, project_dir):
        render_project(project_dir, _default_opts(trpc=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@trpc/server" in pkg["dependencies"]
        assert (project_dir / "server" / "trpc.ts").is_file()
        assert (project_dir / "app" / "api" / "trpc" / "[trpc]" / "route.ts").is_file()

    def test_package_json_branches_on_prisma(self, project_dir):
        render_project(project_dir, _default_opts(prisma=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@prisma/client" in pkg["dependencies"]
        assert "prisma" in pkg["devDependencies"]
        assert "db:generate" in pkg["scripts"]
        assert (project_dir / "prisma" / "schema.prisma").is_file()
        assert (project_dir / "server" / "db.ts").is_file()

    def test_package_json_branches_on_resend(self, project_dir):
        render_project(project_dir, _default_opts(resend=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "resend" in pkg["dependencies"]
        assert (project_dir / "server" / "email.ts").is_file()
        assert (project_dir / "app" / "api" / "contact" / "route.ts").is_file()

    def test_fs71_fullstack_bundle(self, project_dir):
        opts = _default_opts(auth="nextauth", trpc=True, prisma=True, resend=True)
        render_project(project_dir, opts)
        pkg = json.loads((project_dir / "package.json").read_text())
        for dep in ("next-auth", "@trpc/server", "@prisma/client", "resend"):
            assert dep in pkg["dependencies"]
        assert (project_dir / "auth" / "nextauth.config.ts").is_file()
        assert (project_dir / "server" / "trpc.ts").is_file()
        assert (project_dir / "server" / "db.ts").is_file()
        assert (project_dir / "server" / "email.ts").is_file()
        contact = (project_dir / "app" / "api" / "contact" / "route.ts").read_text()
        assert "sendContactEmail" in contact
        assert "db.message.create" in contact

    def test_fs74_todo_example_app_bundle(self, project_dir):
        opts = _default_opts(
            auth="nextauth",
            trpc=True,
            prisma=True,
            resend=True,
            target="both",
            compliance=True,
            example_app="todo",
        )
        render_project(project_dir, opts)

        assert (project_dir / "app" / "todos" / "page.tsx").is_file()
        assert (project_dir / "components" / "TodoApp.tsx").is_file()
        assert (project_dir / "tests" / "unit" / "todo-app.test.tsx").is_file()

        page = (project_dir / "app" / "todos" / "page.tsx").read_text()
        todo_app = (project_dir / "components" / "TodoApp.tsx").read_text()
        todo_test = (project_dir / "tests" / "unit" / "todo-app.test.tsx").read_text()
        assert 'role="main"' in page
        assert "TodoApp" in page
        assert "useState<Todo[]>" in todo_app
        assert "module-global cache" in todo_app
        assert "adds, toggles, and deletes a task" in todo_test

        report = pilot_report(project_dir, opts)
        assert report["options"]["example_app"] == "todo"
        assert report["w5_compliance"]["failed_count"] == 0

    def test_trpc_off_skips_trpc_files(self, project_dir):
        render_project(project_dir, _default_opts(trpc=False))
        for rel in _TRPC_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"

    def test_prisma_off_skips_prisma_files(self, project_dir):
        render_project(project_dir, _default_opts(prisma=False))
        for rel in _PRISMA_ONLY_FILES:
            rendered_rel = rel.removesuffix(".j2")
            assert not (project_dir / rendered_rel).exists(), f"{rendered_rel} leaked through"

    def test_resend_off_skips_resend_files(self, project_dir):
        render_project(project_dir, _default_opts(resend=False))
        for rel in _RESEND_ONLY_FILES:
            rendered_rel = rel.removesuffix(".j2")
            assert not (project_dir / rendered_rel).exists(), f"{rendered_rel} leaked through"

    def test_example_app_none_skips_example_files(self, project_dir):
        render_project(project_dir, _default_opts(example_app="none"))
        assert not (project_dir / "app" / "todos" / "page.tsx").exists()
        assert not (project_dir / "components" / "TodoApp.tsx").exists()
        assert not (project_dir / "tests" / "unit" / "todo-app.test.tsx").exists()

    def test_auth_nextauth_skips_clerk_files(self, project_dir):
        render_project(project_dir, _default_opts(auth="nextauth"))
        assert not (project_dir / "auth" / "clerk.middleware.ts").exists()
        assert not (project_dir / "auth" / "clerk.example.tsx").exists()
        assert (project_dir / "auth" / "nextauth.config.ts").is_file()

    def test_auth_clerk_skips_nextauth_files(self, project_dir):
        render_project(project_dir, _default_opts(auth="clerk"))
        assert not (project_dir / "auth" / "nextauth.config.ts").exists()
        assert not (project_dir / "auth" / "middleware.nextauth.ts").exists()
        assert (project_dir / "auth" / "clerk.middleware.ts").is_file()

    def test_target_vercel_only_skips_wrangler(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        assert (project_dir / "vercel.json").is_file()
        assert not (project_dir / "wrangler.toml").exists()

    def test_target_cloudflare_only_skips_vercel_json(self, project_dir):
        render_project(project_dir, _default_opts(target="cloudflare"))
        assert (project_dir / "wrangler.toml").is_file()
        assert not (project_dir / "vercel.json").exists()

    def test_target_both_ships_both_configs(self, project_dir):
        render_project(project_dir, _default_opts(target="both"))
        assert (project_dir / "vercel.json").is_file()
        assert (project_dir / "wrangler.toml").is_file()

    def test_compliance_off_skips_privacy_docs(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        assert not (project_dir / "docs" / "privacy" / "retention.md").exists()
        assert not (project_dir / "components" / "consent" / "CookieBanner.tsx").exists()
        assert not (project_dir / "spdx.allowlist.json").exists()

    def test_compliance_on_ships_all_three_gate_inputs(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        # GDPR: retention + DPA + erasure handler
        assert (project_dir / "docs" / "privacy" / "retention.md").is_file()
        assert (project_dir / "docs" / "privacy" / "dpa.md").is_file()
        assert (project_dir / "app" / "privacy" / "erasure" / "route.ts").is_file()
        # SPDX: allowlist
        assert (project_dir / "spdx.allowlist.json").is_file()
        # WCAG: focus styles + landmarks in the scaffold itself
        page = (project_dir / "app" / "page.tsx").read_text()
        assert 'role="main"' in page or 'id="main"' in page

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_invalid_auth_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", auth="saml-maybe-someday").validate()

    def test_invalid_target_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", target="aws-amplify").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()

    def test_invalid_example_app_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", example_app="forum").validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W0 / W1 framework bindings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW0W1Bindings:
    def test_resolved_profiles_for_vercel(self):
        opts = _default_opts(target="vercel")
        assert opts.resolved_profiles() == ["web-vercel"]

    def test_resolved_profiles_for_cloudflare(self):
        opts = _default_opts(target="cloudflare")
        assert opts.resolved_profiles() == ["web-edge-cloudflare"]

    def test_resolved_profiles_for_both(self):
        opts = _default_opts(target="both")
        assert opts.resolved_profiles() == ["web-vercel", "web-edge-cloudflare"]

    def test_profile_loads_via_platform_module(self):
        """W0 dispatch test — the profile the scaffold binds to must
        be loadable through the central backend.platform loader."""
        for profile_id in ("web-vercel", "web-edge-cloudflare"):
            data = load_raw_profile(profile_id)
            assert data.get("target_kind") == "web"

    def test_render_context_reads_profile_budget(self):
        ctx = _render_context(_default_opts(target="vercel"))
        # web-vercel.yaml declares 50MiB; we expect that value to
        # surface in ctx, not a hard-coded scaffolder default.
        assert ctx["bundle_budget_bytes"] == 50 * 1024 * 1024

    def test_render_context_reads_cloudflare_budget(self):
        ctx = _render_context(_default_opts(target="cloudflare"))
        # web-edge-cloudflare declares 1MiB.
        assert ctx["bundle_budget_bytes"] == 1 * 1024 * 1024

    def test_vercel_json_memory_limit_from_profile(self, project_dir):
        """W1 profile memory_limit_mb must propagate, not be duplicated."""
        render_project(project_dir, _default_opts(target="vercel"))
        cfg = json.loads((project_dir / "vercel.json").read_text())
        # web-vercel.yaml ships 1024; if the scaffold hard-codes a
        # different number we want a regression signal.
        assert cfg["functions"]["app/api/v1/[...slug]/route.ts"]["memory"] == 1024


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W3 role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW3RoleAlignment:
    """Anti-patterns listed in configs/roles/web/frontend-react.skill.md
    must not appear in the scaffolded code."""

    def test_server_component_no_use_client(self, project_dir):
        render_project(project_dir, _default_opts())
        page = (project_dir / "app" / "page.tsx").read_text()
        # Home page is a Server Component — must NOT opt into client
        assert '"use client"' not in page and "'use client'" not in page

    def test_client_component_marked(self, project_dir):
        render_project(project_dir, _default_opts())
        counter = (project_dir / "components" / "Counter.tsx").read_text()
        assert '"use client"' in counter

    def test_no_use_effect_for_fetching(self, project_dir):
        render_project(project_dir, _default_opts())
        # Across all generated .tsx files, no data fetch inside useEffect.
        for tsx in project_dir.rglob("*.tsx"):
            text = tsx.read_text()
            # crude but load-bearing — if either pattern slips in we
            # want the regression signal.
            assert "useEffect(() => { fetch" not in text
            assert "useEffect(() => {\n    fetch" not in text

    def test_a11y_role_landmark_present(self, project_dir):
        render_project(project_dir, _default_opts())
        page = (project_dir / "app" / "page.tsx").read_text()
        assert 'role="main"' in page


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W4 deploy adapter smoke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW4DeployAdapters:
    def test_dry_run_constructs_both_adapters(self, project_dir):
        render_project(project_dir, _default_opts(target="both"))
        result = dry_run_deploy(project_dir, _default_opts(target="both"))
        assert set(result.keys()) == {"vercel", "cloudflare"}
        assert result["vercel"]["provider"] == "vercel"
        assert result["cloudflare"]["provider"] == "cloudflare-pages"
        assert result["vercel"]["artifact_valid"] is True
        assert result["cloudflare"]["artifact_valid"] is True

    def test_build_artifact_validates_against_rendered_project(self, project_dir):
        render_project(project_dir, _default_opts())
        art = BuildArtifact(path=project_dir, framework="next")
        art.validate()  # should not raise

    def test_token_fingerprint_masks_token(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        result = dry_run_deploy(project_dir, _default_opts(target="vercel"))
        fp = result["vercel"]["token_fingerprint"]
        assert fp != "test-token-vercel-placeholder"
        assert "test-token-vercel-placeholder" not in fp

    def test_dry_run_vercel_only(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        result = dry_run_deploy(project_dir, _default_opts(target="vercel"))
        assert "vercel" in result
        assert "cloudflare" not in result

    def test_dry_run_cloudflare_only(self, project_dir):
        render_project(project_dir, _default_opts(target="cloudflare"))
        result = dry_run_deploy(project_dir, _default_opts(target="cloudflare"))
        assert "cloudflare" in result
        assert "vercel" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W5 compliance wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW5Compliance:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-nextjs"
        assert report["options"]["project_name"] == "pilot-app"
        assert set(report["w4_deploy"]) == {"vercel", "cloudflare"}
        # W5 bundle structure comes from ComplianceBundle.to_dict()
        assert "gates" in report["w5_compliance"]
        gate_ids = {g["gate_id"] for g in report["w5_compliance"]["gates"]}
        assert gate_ids == {"wcag", "gdpr", "spdx"}

    def test_gdpr_retention_doc_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        doc = (project_dir / "docs" / "privacy" / "retention.md").read_text()
        assert "Retention" in doc or "retention" in doc
        assert "pilot-app" in doc

    def test_gdpr_erasure_handler_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        handler = project_dir / "app" / "privacy" / "erasure" / "route.ts"
        assert handler.is_file()
        text = handler.read_text()
        assert "POST" in text
        assert "/api/v1/privacy/erasure" in text

    def test_spdx_allowlist_ships_approved_licenses(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        allow = json.loads((project_dir / "spdx.allowlist.json").read_text())
        assert "MIT" in allow["allow"]
        assert "Apache-2.0" in allow["allow"]
        assert "GPL-3.0" in allow["deny"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pilot — W0-W5 end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPilotValidation:
    """The integrating gate — same bar D1 set for C5 and D29 for C26."""

    def test_full_pilot_flow(self, project_dir):
        opts = _default_opts(auth="nextauth", trpc=True, target="both", compliance=True)
        outcome = render_project(project_dir, opts)
        assert outcome.bytes_written > 0

        report = pilot_report(project_dir, opts)

        # W0/W1: profiles resolved
        assert set(report["w0_w1_profiles"]) == {"web-vercel", "web-edge-cloudflare"}

        # W4: both adapters construct cleanly
        for tgt in ("vercel", "cloudflare"):
            assert report["w4_deploy"][tgt]["artifact_valid"] is True

        # W5: compliance bundle ran without erroring
        assert report["w5_compliance"]["total_gates"] == 3
        # In sandbox, WCAG and SPDX may be "skipped" (no axe / no
        # node_modules); GDPR should pass because the scaffold ships
        # all required posture docs. Bundle.passed accepts skipped.
        assert report["w5_compliance"]["failed_count"] == 0

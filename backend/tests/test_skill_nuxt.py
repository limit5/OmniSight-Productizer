"""W7 #281 — SKILL-NUXT cross-stack skill contract tests.

SKILL-NUXT is the second web-vertical skill pack and the
confirmation that the W0-W5 framework is not React-specific. These
tests lock the framework invariants the same way W6 #280 did for
SKILL-NEXTJS — every gate that exists for both packs is exercised
here with the Nuxt scaffold outputs, so a regression in any W layer
fires on both suites.

* **W0** — ``target_kind=web`` dispatch works for the profiles the
  scaffold binds to (``web-ssr-node`` / ``web-vercel`` /
  ``web-edge-cloudflare``).
* **W1** — resolved ``bundle_size_budget`` / ``memory_limit_mb`` are
  the *real* profile values. When the render targets "all", we keep
  the *tightest* bundle budget (the Cloudflare 1 MiB ceiling).
* **W3** — rendered project matches ``frontend-vue`` role
  anti-patterns (Composition API + ``<script setup>``, no raw
  ``fetch()`` inside setup, Pinia actions not mutations).
* **W4** — VercelAdapter / CloudflarePagesAdapter / DockerNginxAdapter
  all construct against the rendered artifact without hitting the
  network.
* **W5** — compliance bundle runs (or skips cleanly) against the
  rendered project; GDPR retention / DPA / erasure handler shipped.

The Nitro preset pin is explicitly checked — operators can flip
between node-server / vercel / cloudflare-pages / bun via one env
var because the scaffold never hard-codes the preset.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.deploy.base import BuildArtifact
from backend.nuxt_scaffolder import (
    ScaffoldOptions,
    _COMPLIANCE_PATHS,
    _DRIZZLE_ONLY_FILES,
    _PINIA_ONLY_FILES,
    _POSTMARK_ONLY_FILES,
    _SCAFFOLDS_DIR,
    _SKILL_DIR,
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
        yield Path(tmp) / "nuxt-app"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="nuxt-app",
        auth="sidebase",
        pinia=True,
        drizzle=False,
        postmark=False,
        target="all",
        compliance=True,
    )
    kwargs.update(overrides)
    return ScaffoldOptions(**kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill pack registry invariants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSkillPackRegistry:
    def test_pack_discoverable(self):
        names = {s.name for s in list_skills()}
        assert "skill-nuxt" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-nuxt")
        assert result.ok, (
            f"skill-nuxt validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-nuxt")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-nuxt")
        assert info is not None
        assert info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert "enterprise_web" in info.manifest.depends_on_skills

    def test_manifest_keywords_include_cross_stack_marker(self):
        info = get_skill("skill-nuxt")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        # Pilot-aware keywords so operators can find the pack by
        # the shape of the problem ("nitro preset", "pinia state",
        # "vue 3 skill pack").
        assert {"nuxt", "nitro", "pinia", "drizzle", "postmark", "fs-7-2", "w7"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-nuxt"

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
            "nuxt.config.ts",
            "tsconfig.json",
            ".gitignore",
            "app.vue",
            "layouts/default.vue",
            "pages/index.vue",
            "pages/about.vue",
            "components/Counter.vue",
            "composables/useBackend.ts",
            "stores/counter.ts",
            "server/middleware/security-headers.ts",
            "server/api/health.get.ts",
            "server/api/v1/[...slug].ts",
            "playwright.config.ts",
            "vitest.config.ts",
            "e2e/smoke.spec.ts",
            "tests/unit/counter.test.ts",
        ]
        for rel in must_exist:
            assert (project_dir / rel).is_file(), f"missing: {rel}"
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_nitro_preset_pinned(self, project_dir):
        """The whole W7 target story rests on one env var pivot —
        the scaffold MUST NOT hard-code a preset without the
        `process.env.NITRO_PRESET ||` fallback wiring."""
        render_project(project_dir, _default_opts(target="all"))
        cfg = (project_dir / "nuxt.config.ts").read_text()
        assert "nitro:" in cfg
        assert "preset:" in cfg
        assert "process.env.NITRO_PRESET" in cfg
        # Default for target=all is node-server (lowest common denominator).
        assert '"node-server"' in cfg or "'node-server'" in cfg

    def test_sc63_security_headers_middleware_auto_added(self, project_dir):
        render_project(project_dir, _default_opts())
        middleware = (project_dir / "server" / "middleware" / "security-headers.ts").read_text()
        assert "SC.6.3 security headers" in middleware
        assert '"Content-Security-Policy"' in middleware
        assert "script-src 'self'" in middleware
        assert "'unsafe-eval'" not in middleware
        assert '"Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload"' in middleware
        assert '"X-Frame-Options": "DENY"' in middleware
        assert '"Referrer-Policy": "strict-origin"' in middleware
        assert '"Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()"' in middleware
        assert '"Cross-Origin-Resource-Policy": "same-origin"' in middleware
        assert '"Cross-Origin-Embedder-Policy": "require-corp"' in middleware
        assert '"Cross-Origin-Opener-Policy": "same-origin"' in middleware

    def test_nitro_preset_single_target_defaults(self, project_dir):
        """Single-target renders pin the default preset to that target."""
        render_project(project_dir, _default_opts(target="cloudflare"))
        cfg = (project_dir / "nuxt.config.ts").read_text()
        assert "cloudflare-pages" in cfg

        render_project(project_dir, _default_opts(target="bun"))
        cfg = (project_dir / "nuxt.config.ts").read_text()
        assert '"bun"' in cfg or "'bun'" in cfg

    def test_package_json_branches_on_auth(self, project_dir):
        render_project(project_dir, _default_opts(auth="sidebase"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@sidebase/nuxt-auth" in pkg["dependencies"]
        assert "@clerk/nuxt" not in pkg["dependencies"]

    def test_package_json_branches_on_clerk(self, project_dir):
        render_project(project_dir, _default_opts(auth="clerk"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@clerk/nuxt" in pkg["dependencies"]
        assert "@sidebase/nuxt-auth" not in pkg["dependencies"]

    def test_package_json_branches_on_pinia(self, project_dir):
        render_project(project_dir, _default_opts(pinia=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "pinia" in pkg["dependencies"]
        assert "@pinia/nuxt" in pkg["dependencies"]
        assert (project_dir / "stores" / "counter.ts").is_file()

    def test_package_json_branches_on_drizzle(self, project_dir):
        render_project(project_dir, _default_opts(drizzle=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "drizzle-orm" in pkg["dependencies"]
        assert "postgres" in pkg["dependencies"]
        assert "drizzle-kit" in pkg["devDependencies"]
        assert "db:generate" in pkg["scripts"]
        assert (project_dir / "drizzle" / "schema.ts").is_file()
        assert (project_dir / "server" / "db.ts").is_file()

    def test_package_json_branches_on_postmark(self, project_dir):
        render_project(project_dir, _default_opts(postmark=True))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "postmark" in pkg["dependencies"]
        assert (project_dir / "server" / "email.ts").is_file()
        assert (project_dir / "server" / "api" / "contact.post.ts").is_file()

    def test_fs72_fullstack_bundle(self, project_dir):
        opts = _default_opts(auth="sidebase", pinia=True, drizzle=True, postmark=True)
        render_project(project_dir, opts)
        pkg = json.loads((project_dir / "package.json").read_text())
        for dep in ("@sidebase/nuxt-auth", "drizzle-orm", "postgres", "postmark"):
            assert dep in pkg["dependencies"]
        assert (project_dir / "auth" / "nuxt-auth.config.ts").is_file()
        assert (project_dir / "middleware" / "auth.global.ts").is_file()
        assert (project_dir / "drizzle" / "schema.ts").is_file()
        assert (project_dir / "server" / "db.ts").is_file()
        assert (project_dir / "server" / "email.ts").is_file()
        contact = (project_dir / "server" / "api" / "contact.post.ts").read_text()
        assert "sendContactEmail" in contact
        assert "getDb().insert(messages)" in contact

    def test_pinia_off_skips_store_files(self, project_dir):
        render_project(project_dir, _default_opts(pinia=False))
        for rel in _PINIA_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "pinia" not in pkg["dependencies"]
        assert "@pinia/nuxt" not in pkg["dependencies"]

    def test_drizzle_off_skips_drizzle_files(self, project_dir):
        render_project(project_dir, _default_opts(drizzle=False))
        for rel in _DRIZZLE_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"

    def test_postmark_off_skips_postmark_files(self, project_dir):
        render_project(project_dir, _default_opts(postmark=False))
        for rel in _POSTMARK_ONLY_FILES:
            rendered_rel = rel.removesuffix(".j2")
            assert not (project_dir / rendered_rel).exists(), f"{rendered_rel} leaked through"

    def test_auth_sidebase_skips_clerk_files(self, project_dir):
        render_project(project_dir, _default_opts(auth="sidebase"))
        assert not (project_dir / "auth" / "clerk.example.vue").exists()
        assert (project_dir / "auth" / "nuxt-auth.config.ts").is_file()
        assert (project_dir / "middleware" / "auth.global.ts").is_file()

    def test_auth_clerk_skips_sidebase_files(self, project_dir):
        render_project(project_dir, _default_opts(auth="clerk"))
        assert not (project_dir / "auth" / "nuxt-auth.config.ts").exists()
        assert not (project_dir / "middleware" / "auth.global.ts").exists()
        assert (project_dir / "auth" / "clerk.example.vue").is_file()

    def test_target_vercel_only_skips_wrangler_and_docker(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        assert (project_dir / "vercel.json").is_file()
        assert not (project_dir / "wrangler.toml").exists()
        assert not (project_dir / "Dockerfile").exists()
        assert not (project_dir / "bunfig.toml").exists()

    def test_target_cloudflare_only_skips_other_targets(self, project_dir):
        render_project(project_dir, _default_opts(target="cloudflare"))
        assert (project_dir / "wrangler.toml").is_file()
        assert not (project_dir / "vercel.json").exists()
        assert not (project_dir / "Dockerfile").exists()

    def test_target_node_ships_dockerfile(self, project_dir):
        render_project(project_dir, _default_opts(target="node"))
        assert (project_dir / "Dockerfile").is_file()
        assert not (project_dir / "vercel.json").exists()
        assert not (project_dir / "wrangler.toml").exists()
        assert not (project_dir / "bunfig.toml").exists()

    def test_target_bun_ships_bunfig_and_dockerfile(self, project_dir):
        render_project(project_dir, _default_opts(target="bun"))
        assert (project_dir / "bunfig.toml").is_file()
        assert (project_dir / "Dockerfile").is_file()

    def test_target_all_ships_every_build_config(self, project_dir):
        render_project(project_dir, _default_opts(target="all"))
        assert (project_dir / "vercel.json").is_file()
        assert (project_dir / "wrangler.toml").is_file()
        assert (project_dir / "Dockerfile").is_file()
        assert (project_dir / "bunfig.toml").is_file()

    def test_compliance_off_skips_privacy_files(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        for rel in _COMPLIANCE_PATHS:
            # .j2 suffix stripped in rendered tree
            cleaned = rel[:-3] if rel.endswith(".j2") else rel
            assert not (project_dir / cleaned).exists(), f"{cleaned} leaked through"

    def test_compliance_on_ships_all_three_gate_inputs(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        # GDPR: retention + DPA + erasure handler
        assert (project_dir / "docs" / "privacy" / "retention.md").is_file()
        assert (project_dir / "docs" / "privacy" / "dpa.md").is_file()
        assert (project_dir / "server" / "api" / "privacy" / "erasure.post.ts").is_file()
        # SPDX: allowlist
        assert (project_dir / "spdx.allowlist.json").is_file()
        # WCAG: landmarks present in default layout
        layout = (project_dir / "layouts" / "default.vue").read_text()
        assert 'role="main"' in layout or 'id="main"' in layout

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_invalid_auth_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", auth="oauth-maybe-someday").validate()

    def test_invalid_target_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", target="deno").validate()

    def test_empty_project_name_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="   ").validate()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W0 / W1 framework bindings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW0W1Bindings:
    def test_resolved_profiles_for_node(self):
        assert _default_opts(target="node").resolved_profiles() == ["web-ssr-node"]

    def test_resolved_profiles_for_vercel(self):
        assert _default_opts(target="vercel").resolved_profiles() == ["web-vercel"]

    def test_resolved_profiles_for_cloudflare(self):
        assert _default_opts(target="cloudflare").resolved_profiles() == ["web-edge-cloudflare"]

    def test_resolved_profiles_for_bun(self):
        # Bun shares web-ssr-node — same server-bundle envelope.
        assert _default_opts(target="bun").resolved_profiles() == ["web-ssr-node"]

    def test_resolved_profiles_for_all(self):
        assert _default_opts(target="all").resolved_profiles() == [
            "web-ssr-node", "web-vercel", "web-edge-cloudflare"
        ]

    def test_profile_loads_via_platform_module(self):
        """W0 dispatch test — the profile the scaffold binds to must
        be loadable through the central backend.platform loader."""
        for profile_id in ("web-ssr-node", "web-vercel", "web-edge-cloudflare"):
            data = load_raw_profile(profile_id)
            assert data.get("target_kind") == "web"

    def test_render_context_reads_node_budget(self):
        ctx = _render_context(_default_opts(target="node"))
        # web-ssr-node declares 5 MiB server bundle.
        assert ctx["bundle_budget_bytes"] == 5 * 1024 * 1024

    def test_render_context_reads_vercel_budget(self):
        ctx = _render_context(_default_opts(target="vercel"))
        # web-vercel declares 50 MiB.
        assert ctx["bundle_budget_bytes"] == 50 * 1024 * 1024

    def test_render_context_reads_cloudflare_budget(self):
        ctx = _render_context(_default_opts(target="cloudflare"))
        # web-edge-cloudflare declares 1 MiB.
        assert ctx["bundle_budget_bytes"] == 1 * 1024 * 1024

    def test_render_context_all_keeps_tightest_budget(self):
        """When multiple profiles apply, the scaffolder must surface
        the tightest budget so the W2 bundle gate fires on the most
        restrictive target. Cloudflare's 1 MiB wins."""
        ctx = _render_context(_default_opts(target="all"))
        assert ctx["bundle_budget_bytes"] == 1 * 1024 * 1024

    def test_vercel_json_memory_limit_from_profile(self, project_dir):
        """W1 profile memory_limit_mb must propagate, not be duplicated."""
        render_project(project_dir, _default_opts(target="vercel"))
        cfg = json.loads((project_dir / "vercel.json").read_text())
        fn_key = ".vercel/output/functions/__nitro.func/index.mjs"
        assert cfg["functions"][fn_key]["memory"] == 1024


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W3 frontend-vue role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW3RoleAlignment:
    """Anti-patterns listed in configs/roles/web/frontend-vue.skill.md
    must not appear in the scaffolded code."""

    def test_script_setup_used_everywhere(self, project_dir):
        render_project(project_dir, _default_opts())
        for vue in project_dir.rglob("*.vue"):
            text = vue.read_text()
            # Either the file has a <script setup> block, or no
            # <script> at all (app.vue / pure template files).
            if "<script" in text:
                assert "<script setup" in text, f"{vue} uses non-setup <script>"

    def test_no_raw_fetch_in_component_setup(self, project_dir):
        render_project(project_dir, _default_opts())
        # frontend-vue role bans `fetch(` inside `<script setup>`.
        # useFetch / $fetch wrappers are OK because they're hydration-aware.
        for vue in project_dir.rglob("*.vue"):
            text = vue.read_text()
            if "<script setup" not in text:
                continue
            setup_block = text.split("<script setup", 1)[1].split("</script>", 1)[0]
            # crude but load-bearing — raw fetch(url...) inside setup fails.
            lines_with_fetch = [
                ln for ln in setup_block.splitlines()
                if "fetch(" in ln and "useFetch" not in ln and "$fetch" not in ln
            ]
            assert not lines_with_fetch, (
                f"{vue.name} contains raw fetch() in <script setup>: {lines_with_fetch}"
            )

    def test_pages_layout_landmark_present(self, project_dir):
        render_project(project_dir, _default_opts())
        layout = (project_dir / "layouts" / "default.vue").read_text()
        assert 'role="main"' in layout
        assert 'role="banner"' in layout

    def test_pinia_uses_actions_not_direct_state_mutation(self, project_dir):
        """Pinia best practice — mutation via actions, not state
        reassignment at call sites. The generated store exposes an
        `increment()` action; tests reference it rather than `count++`."""
        render_project(project_dir, _default_opts(pinia=True))
        counter_test = (project_dir / "tests" / "unit" / "counter.test.ts").read_text()
        assert "store.increment()" in counter_test


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W4 deploy adapter smoke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW4DeployAdapters:
    def test_dry_run_constructs_all_three_adapters_for_all(self, project_dir):
        render_project(project_dir, _default_opts(target="all"))
        result = dry_run_deploy(project_dir, _default_opts(target="all"))
        assert set(result.keys()) == {"vercel", "cloudflare", "docker"}
        assert result["vercel"]["provider"] == "vercel"
        assert result["cloudflare"]["provider"] == "cloudflare-pages"
        assert result["docker"]["provider"] == "docker-nginx"
        for tgt in ("vercel", "cloudflare", "docker"):
            assert result[tgt]["artifact_valid"] is True

    def test_build_artifact_validates_against_rendered_project(self, project_dir):
        render_project(project_dir, _default_opts())
        art = BuildArtifact(path=project_dir, framework="nuxt")
        art.validate()  # should not raise

    def test_token_fingerprint_masks_token(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        result = dry_run_deploy(project_dir, _default_opts(target="vercel"))
        fp = result["vercel"]["token_fingerprint"]
        assert fp != "test-token-vercel-placeholder"
        assert "test-token-vercel-placeholder" not in fp

    def test_dry_run_node_only_uses_docker_adapter(self, project_dir):
        render_project(project_dir, _default_opts(target="node"))
        result = dry_run_deploy(project_dir, _default_opts(target="node"))
        assert "docker" in result
        assert "vercel" not in result
        assert "cloudflare" not in result

    def test_dry_run_bun_only_uses_docker_adapter(self, project_dir):
        """Bun reuses the same container-path adapter as Node (both are
        long-running server runtimes — W4's docker-nginx adapter is the
        lowest-common-denominator target family)."""
        render_project(project_dir, _default_opts(target="bun"))
        result = dry_run_deploy(project_dir, _default_opts(target="bun"))
        assert "docker" in result
        assert result["docker"]["provider"] == "docker-nginx"

    def test_dry_run_cloudflare_only(self, project_dir):
        render_project(project_dir, _default_opts(target="cloudflare"))
        result = dry_run_deploy(project_dir, _default_opts(target="cloudflare"))
        assert "cloudflare" in result
        assert "vercel" not in result
        assert "docker" not in result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W5 compliance wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW5Compliance:
    def test_pilot_report_shape(self, project_dir):
        render_project(project_dir, _default_opts())
        report = pilot_report(project_dir, _default_opts())
        assert report["skill"] == "skill-nuxt"
        assert report["options"]["project_name"] == "nuxt-app"
        assert set(report["w4_deploy"]) == {"vercel", "cloudflare", "docker"}
        assert "gates" in report["w5_compliance"]
        gate_ids = {g["gate_id"] for g in report["w5_compliance"]["gates"]}
        assert gate_ids == {"wcag", "gdpr", "spdx"}

    def test_gdpr_retention_doc_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        doc = (project_dir / "docs" / "privacy" / "retention.md").read_text()
        assert "Retention" in doc or "retention" in doc
        assert "nuxt-app" in doc

    def test_gdpr_erasure_handler_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        handler = project_dir / "server" / "api" / "privacy" / "erasure.post.ts"
        assert handler.is_file()
        text = handler.read_text()
        assert "erasure" in text
        assert "/api/v1/privacy/erasure" in text

    def test_spdx_allowlist_ships_approved_licenses(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        allow = json.loads((project_dir / "spdx.allowlist.json").read_text())
        assert "MIT" in allow["allow"]
        assert "Apache-2.0" in allow["allow"]
        assert "GPL-3.0" in allow["deny"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-stack — W0-W5 end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCrossStackValidation:
    """The integrating gate — same bar W6 set for SKILL-NEXTJS.

    If SKILL-NUXT renders through the same ScaffoldOptions /
    render_project / dry_run_deploy / pilot_report API as
    SKILL-NEXTJS with no framework-level changes required, we can
    claim W0-W5 is a framework (n=2), not a pilot-plus-copy (n=1).
    """

    def test_full_cross_stack_flow(self, project_dir):
        opts = _default_opts(auth="sidebase", pinia=True, target="all", compliance=True)
        outcome = render_project(project_dir, opts)
        assert outcome.bytes_written > 0

        report = pilot_report(project_dir, opts)

        # W0/W1: profiles resolved
        assert set(report["w0_w1_profiles"]) == {
            "web-ssr-node", "web-vercel", "web-edge-cloudflare"
        }

        # W4: all three adapters construct cleanly
        for tgt in ("vercel", "cloudflare", "docker"):
            assert report["w4_deploy"][tgt]["artifact_valid"] is True

        # W5: compliance bundle ran without erroring
        assert report["w5_compliance"]["total_gates"] == 3
        # Same semantics as W6: in sandbox, WCAG/SPDX may be skipped;
        # GDPR should pass because the scaffold ships the posture docs.
        assert report["w5_compliance"]["failed_count"] == 0

    def test_api_shape_matches_sibling_skill(self):
        """SKILL-NEXTJS and SKILL-NUXT must expose the same public
        attributes on ScaffoldOptions + RenderOutcome so the upstream
        orchestrator can treat them interchangeably. This is the hard
        version of "the framework survived a second consumer"."""
        from backend import nextjs_scaffolder, nuxt_scaffolder
        next_fields = {f.name for f in nextjs_scaffolder.ScaffoldOptions.__dataclass_fields__.values()}
        nuxt_fields = {f.name for f in nuxt_scaffolder.ScaffoldOptions.__dataclass_fields__.values()}

        # Shared contract — both must ship these knobs.
        shared = {"project_name", "auth", "target", "compliance", "backend_url"}
        assert shared.issubset(next_fields)
        assert shared.issubset(nuxt_fields)

        # Entry-point symmetry
        for name in ("render_project", "dry_run_deploy", "pilot_report", "validate_pack"):
            assert hasattr(nextjs_scaffolder, name)
            assert hasattr(nuxt_scaffolder, name)

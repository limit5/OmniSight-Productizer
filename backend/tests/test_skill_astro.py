"""W8 #282 — SKILL-ASTRO content-vertical skill contract tests.

SKILL-ASTRO is the third web-vertical skill pack and the confirmation
that the W0-W5 framework is not just cross-stack (n=2 after W7) but
also cross-shape — it covers the content-heavy / SSG-first vertical
that neither whole-app JS skill (SKILL-NEXTJS, SKILL-NUXT) addressed.

* **W0** — ``target_kind=web`` dispatch works for the profiles the
  scaffold binds to (``web-static`` / ``web-ssr-node`` / ``web-vercel`` /
  ``web-edge-cloudflare``).
* **W1** — resolved ``bundle_size_budget`` / ``memory_limit_mb`` are
  the *real* profile values. When the render targets "all", we keep
  the *tightest* bundle budget (``web-static``'s 500 KiB).
* **W3** — rendered project matches ``frontend-{react,vue,svelte}``
  role anti-patterns (client: directives prefer visible over load,
  semantic landmarks, no raw fetch in MDX frontmatter).
* **W4** — VercelAdapter / CloudflarePagesAdapter / DockerNginxAdapter
  all construct against the rendered artifact without hitting the
  network.
* **W5** — compliance bundle runs (or skips cleanly) against the
  rendered project; GDPR retention / DPA / erasure handler shipped.
* **Content vertical** — content collections with Zod schema, MDX
  seed entry, RSS feed, sitemap integration, CMS source adapter and
  webhook route when `cms != none`.

The ASTRO_TARGET pivot is explicitly checked — operators can flip
between static / node / vercel / cloudflare via one env var because
the scaffold never hard-codes the target.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from backend.deploy.base import BuildArtifact
from backend.astro_scaffolder import (
    ScaffoldOptions,
    _CMS_ONLY_FILES,
    _CMS_TESTS_FILES,
    _COMPLIANCE_PATHS,
    _ISLANDS_ONLY_FILES,
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
        yield Path(tmp) / "astro-site"


def _default_opts(**overrides) -> ScaffoldOptions:
    kwargs = dict(
        project_name="astro-site",
        islands="react",
        cms="none",
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
        assert "skill-astro" in names

    def test_pack_validates_clean(self):
        result = validate_skill("skill-astro")
        assert result.ok, (
            f"skill-astro validation failed: "
            f"{[(i.level, i.message) for i in result.issues]}"
        )

    def test_all_five_artifact_kinds_declared(self):
        info = get_skill("skill-astro")
        assert info is not None
        assert info.artifact_kinds == {"tasks", "scaffolds", "tests", "hil", "docs"}

    def test_manifest_declares_core_dependencies(self):
        info = get_skill("skill-astro")
        assert info is not None
        assert info.manifest is not None
        assert "CORE-05" in info.manifest.depends_on_core
        assert "enterprise_web" in info.manifest.depends_on_skills

    def test_manifest_keywords_include_content_vertical_marker(self):
        info = get_skill("skill-astro")
        assert info and info.manifest
        kws = set(info.manifest.keywords)
        assert {"astro", "islands", "mdx", "w8"}.issubset(kws)

    def test_validate_pack_helper(self):
        result = validate_pack()
        assert result["installed"] is True
        assert result["ok"] is True
        assert result["skill_name"] == "skill-astro"

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
            "astro.config.mjs",
            "tsconfig.json",
            ".gitignore",
            ".env.example",
            "src/env.d.ts",
            "src/content/config.ts",
            "src/content/blog/hello-world.mdx",
            "src/layouts/BaseLayout.astro",
            "src/pages/index.astro",
            "src/pages/about.astro",
            "src/pages/blog/[...slug].astro",
            "src/pages/rss.xml.ts",
            "playwright.config.ts",
            "vitest.config.ts",
            "e2e/smoke.spec.ts",
            "tests/unit/setup.ts",
        ]
        for rel in must_exist:
            assert (project_dir / rel).is_file(), f"missing: {rel}"
        assert outcome.bytes_written > 0
        assert outcome.warnings == []

    def test_astro_target_pinned(self, project_dir):
        """The whole W8 target story rests on one env var pivot —
        the scaffold MUST NOT hard-code a target without the
        `process.env.ASTRO_TARGET ||` fallback wiring."""
        render_project(project_dir, _default_opts(target="all"))
        cfg = (project_dir / "astro.config.mjs").read_text()
        assert "process.env.ASTRO_TARGET" in cfg
        # Default for target=all is static (lowest common denominator).
        assert '"static"' in cfg

    def test_astro_target_single_default(self, project_dir):
        """Single-target renders pin the default target in the config."""
        render_project(project_dir, _default_opts(target="cloudflare"))
        cfg = (project_dir / "astro.config.mjs").read_text()
        assert '"cloudflare"' in cfg
        # cloudflare-only render imports the cloudflare adapter but not
        # the node/vercel ones.
        assert 'from "@astrojs/cloudflare"' in cfg
        assert 'from "@astrojs/vercel"' not in cfg
        assert 'from "@astrojs/node"' not in cfg

    def test_astro_target_static_has_no_adapter_import(self, project_dir):
        render_project(project_dir, _default_opts(target="static"))
        cfg = (project_dir / "astro.config.mjs").read_text()
        assert 'from "@astrojs/node"' not in cfg
        assert 'from "@astrojs/vercel"' not in cfg
        assert 'from "@astrojs/cloudflare"' not in cfg

    def test_package_json_branches_on_islands_react(self, project_dir):
        render_project(project_dir, _default_opts(islands="react"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@astrojs/react" in pkg["dependencies"]
        assert "react" in pkg["dependencies"]
        assert "@astrojs/vue" not in pkg["dependencies"]
        assert "@astrojs/svelte" not in pkg["dependencies"]

    def test_package_json_branches_on_islands_vue(self, project_dir):
        render_project(project_dir, _default_opts(islands="vue"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@astrojs/vue" in pkg["dependencies"]
        assert "vue" in pkg["dependencies"]
        assert "@astrojs/react" not in pkg["dependencies"]

    def test_package_json_branches_on_islands_svelte(self, project_dir):
        render_project(project_dir, _default_opts(islands="svelte"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@astrojs/svelte" in pkg["dependencies"]
        assert "svelte" in pkg["dependencies"]
        assert "@astrojs/react" not in pkg["dependencies"]

    def test_package_json_islands_none_ships_no_framework_dep(self, project_dir):
        render_project(project_dir, _default_opts(islands="none"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@astrojs/react" not in pkg["dependencies"]
        assert "@astrojs/vue" not in pkg["dependencies"]
        assert "@astrojs/svelte" not in pkg["dependencies"]

    def test_islands_only_ships_matching_component(self, project_dir):
        render_project(project_dir, _default_opts(islands="react"))
        assert (project_dir / "src/components/Counter.jsx").is_file()
        assert not (project_dir / "src/components/Counter.vue").exists()
        assert not (project_dir / "src/components/Counter.svelte").exists()

    def test_islands_none_skips_every_counter(self, project_dir):
        render_project(project_dir, _default_opts(islands="none"))
        for rel in _ISLANDS_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"

    def test_package_json_branches_on_cms_sanity(self, project_dir):
        render_project(project_dir, _default_opts(cms="sanity"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@sanity/client" in pkg["dependencies"]
        assert "contentful" not in pkg["dependencies"]
        assert (project_dir / "src/lib/cms/sanity.ts").is_file()
        assert (project_dir / "src/pages/api/webhooks/sanity.ts").is_file()

    def test_package_json_branches_on_cms_contentful(self, project_dir):
        render_project(project_dir, _default_opts(cms="contentful"))
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "contentful" in pkg["dependencies"]
        assert "@sanity/client" not in pkg["dependencies"]
        assert (project_dir / "src/lib/cms/contentful.ts").is_file()
        assert (project_dir / "src/pages/api/webhooks/contentful.ts").is_file()

    def test_cms_none_skips_cms_files(self, project_dir):
        render_project(project_dir, _default_opts(cms="none"))
        for rel in _CMS_ONLY_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"
        # cms=none also skips the cms unit test
        for rel in _CMS_TESTS_FILES:
            assert not (project_dir / rel).exists(), f"{rel} leaked through"
        pkg = json.loads((project_dir / "package.json").read_text())
        assert "@sanity/client" not in pkg["dependencies"]
        assert "contentful" not in pkg["dependencies"]

    def test_cms_sanity_skips_contentful_files(self, project_dir):
        render_project(project_dir, _default_opts(cms="sanity"))
        assert not (project_dir / "src/lib/cms/contentful.ts").exists()
        assert not (project_dir / "src/pages/api/webhooks/contentful.ts").exists()

    def test_target_vercel_only_skips_wrangler_and_docker(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        assert (project_dir / "vercel.json").is_file()
        assert not (project_dir / "wrangler.toml").exists()
        assert not (project_dir / "Dockerfile").exists()

    def test_target_cloudflare_only_skips_other_targets(self, project_dir):
        render_project(project_dir, _default_opts(target="cloudflare"))
        assert (project_dir / "wrangler.toml").is_file()
        assert not (project_dir / "vercel.json").exists()
        assert not (project_dir / "Dockerfile").exists()

    def test_target_static_ships_dockerfile(self, project_dir):
        render_project(project_dir, _default_opts(target="static"))
        # Static target includes Dockerfile (nginx) for the "serve dist/"
        # path — the W4 family's static-site reference.
        assert (project_dir / "Dockerfile").is_file()
        df = (project_dir / "Dockerfile").read_text()
        assert "nginx" in df

    def test_target_node_ships_dockerfile_with_node_runtime(self, project_dir):
        render_project(project_dir, _default_opts(target="node"))
        assert (project_dir / "Dockerfile").is_file()
        df = (project_dir / "Dockerfile").read_text()
        # Node target Dockerfile runs the @astrojs/node standalone server,
        # not nginx.
        assert "node" in df.lower()
        assert "entry.mjs" in df
        assert not (project_dir / "vercel.json").exists()
        assert not (project_dir / "wrangler.toml").exists()

    def test_target_all_ships_every_build_config(self, project_dir):
        render_project(project_dir, _default_opts(target="all"))
        assert (project_dir / "vercel.json").is_file()
        assert (project_dir / "wrangler.toml").is_file()
        assert (project_dir / "Dockerfile").is_file()

    def test_compliance_off_skips_privacy_files(self, project_dir):
        render_project(project_dir, _default_opts(compliance=False))
        for rel in _COMPLIANCE_PATHS:
            cleaned = rel[:-3] if rel.endswith(".j2") else rel
            assert not (project_dir / cleaned).exists(), f"{cleaned} leaked through"

    def test_compliance_on_ships_all_three_gate_inputs(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        # GDPR: retention + DPA + erasure handler
        assert (project_dir / "docs/privacy/retention.md").is_file()
        assert (project_dir / "docs/privacy/dpa.md").is_file()
        assert (project_dir / "src/pages/api/privacy/erasure.ts").is_file()
        # SPDX: allowlist
        assert (project_dir / "spdx.allowlist.json").is_file()
        # WCAG: landmarks present in the base layout
        layout = (project_dir / "src/layouts/BaseLayout.astro").read_text()
        assert 'role="main"' in layout or 'id="main"' in layout

    def test_idempotent_rerender(self, project_dir):
        render_project(project_dir, _default_opts())
        first = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        render_project(project_dir, _default_opts())
        second = sorted(p.name for p in project_dir.rglob("*") if p.is_file())
        assert first == second

    def test_invalid_islands_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", islands="solid").validate()

    def test_invalid_cms_rejected(self):
        with pytest.raises(ValueError):
            ScaffoldOptions(project_name="x", cms="strapi").validate()

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
    def test_resolved_profiles_for_static(self):
        assert _default_opts(target="static").resolved_profiles() == ["web-static"]

    def test_resolved_profiles_for_node(self):
        assert _default_opts(target="node").resolved_profiles() == ["web-ssr-node"]

    def test_resolved_profiles_for_vercel(self):
        assert _default_opts(target="vercel").resolved_profiles() == ["web-vercel"]

    def test_resolved_profiles_for_cloudflare(self):
        assert _default_opts(target="cloudflare").resolved_profiles() == ["web-edge-cloudflare"]

    def test_resolved_profiles_for_all(self):
        assert _default_opts(target="all").resolved_profiles() == [
            "web-static", "web-ssr-node", "web-vercel", "web-edge-cloudflare"
        ]

    def test_profile_loads_via_platform_module(self):
        """W0 dispatch test — every profile the scaffold binds to must
        be loadable through the central backend.platform loader."""
        for profile_id in (
            "web-static", "web-ssr-node", "web-vercel", "web-edge-cloudflare",
        ):
            data = load_raw_profile(profile_id)
            assert data.get("target_kind") == "web"

    def test_render_context_reads_static_budget(self):
        ctx = _render_context(_default_opts(target="static"))
        # web-static declares 500 KiB critical-path budget.
        assert ctx["bundle_budget_bytes"] == 500 * 1024

    def test_render_context_reads_node_budget(self):
        ctx = _render_context(_default_opts(target="node"))
        assert ctx["bundle_budget_bytes"] == 5 * 1024 * 1024

    def test_render_context_reads_vercel_budget(self):
        ctx = _render_context(_default_opts(target="vercel"))
        assert ctx["bundle_budget_bytes"] == 50 * 1024 * 1024

    def test_render_context_reads_cloudflare_budget(self):
        ctx = _render_context(_default_opts(target="cloudflare"))
        assert ctx["bundle_budget_bytes"] == 1 * 1024 * 1024

    def test_render_context_all_keeps_tightest_budget(self):
        """When multiple profiles apply, the scaffolder must surface
        the tightest budget. `web-static`'s 500 KiB wins for target=all."""
        ctx = _render_context(_default_opts(target="all"))
        assert ctx["bundle_budget_bytes"] == 500 * 1024

    def test_vercel_json_memory_limit_from_profile(self, project_dir):
        """W1 profile memory_limit_mb must propagate, not be duplicated."""
        render_project(project_dir, _default_opts(target="vercel"))
        cfg = json.loads((project_dir / "vercel.json").read_text())
        fn_key = ".vercel/output/functions/**/*.func/*.mjs"
        assert cfg["functions"][fn_key]["memory"] == 1024


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  W3 role alignment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestW3RoleAlignment:
    """Anti-patterns listed in configs/roles/web/frontend-*.skill.md +
    a11y / perf roles must not appear in the scaffolded code."""

    def test_layout_ships_landmarks(self, project_dir):
        render_project(project_dir, _default_opts())
        layout = (project_dir / "src/layouts/BaseLayout.astro").read_text()
        assert 'role="banner"' in layout
        assert 'role="main"' in layout
        assert 'role="contentinfo"' in layout

    def test_island_defaults_to_client_visible(self, project_dir):
        """perf role: hydrate with `client:visible` (intersection observer)
        rather than `client:load` (eager) for non-LCP components."""
        render_project(project_dir, _default_opts(islands="react"))
        mdx = (project_dir / "src/content/blog/hello-world.mdx").read_text()
        assert "client:visible" in mdx
        assert "client:load" not in mdx

    def test_mdx_does_not_fetch_in_frontmatter(self, project_dir):
        """SKILL.md anti-pattern: MDX frontmatter must not call fetch()
        — MDX runs at build time in SSG mode; fetch belongs in the
        surrounding .astro page."""
        render_project(project_dir, _default_opts())
        for mdx in project_dir.rglob("*.mdx"):
            text = mdx.read_text()
            # strip frontmatter block (between first two '---' lines)
            lines = text.splitlines()
            if lines and lines[0].strip() == "---":
                end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
                if end is not None:
                    front = "\n".join(lines[1:end])
                    assert "fetch(" not in front, f"{mdx.name} fetch() in frontmatter"

    def test_rss_and_sitemap_integrations_wired(self, project_dir):
        """SEO role: structured content (RSS + sitemap) must be wired."""
        render_project(project_dir, _default_opts())
        cfg = (project_dir / "astro.config.mjs").read_text()
        assert "@astrojs/sitemap" in cfg
        assert "sitemap()" in cfg
        # RSS endpoint ships as a page, not a config integration.
        assert (project_dir / "src/pages/rss.xml.ts").is_file()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Content vertical specifics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContentVertical:
    def test_content_collection_config_ships_zod_schema(self, project_dir):
        render_project(project_dir, _default_opts())
        cfg = (project_dir / "src/content/config.ts").read_text()
        assert "defineCollection" in cfg
        assert "z.string()" in cfg
        assert "pubDate" in cfg
        assert "collections = { blog }" in cfg

    def test_seed_mdx_frontmatter_matches_schema(self, project_dir):
        render_project(project_dir, _default_opts())
        mdx = (project_dir / "src/content/blog/hello-world.mdx").read_text()
        # Must have all required schema fields (title/description/pubDate)
        assert 'title:' in mdx
        assert 'description:' in mdx
        assert 'pubDate:' in mdx

    def test_dynamic_blog_route_renders_collection(self, project_dir):
        render_project(project_dir, _default_opts())
        route = (project_dir / "src/pages/blog/[...slug].astro").read_text()
        assert "getStaticPaths" in route
        assert 'getCollection("blog"' in route

    def test_rss_endpoint_uses_astrojs_rss(self, project_dir):
        render_project(project_dir, _default_opts())
        rss = (project_dir / "src/pages/rss.xml.ts").read_text()
        assert "@astrojs/rss" in rss


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CMS source adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCmsAdapters:
    def test_sanity_adapter_exposes_fetch_and_verify(self, project_dir):
        render_project(project_dir, _default_opts(cms="sanity"))
        mod = (project_dir / "src/lib/cms/sanity.ts").read_text()
        assert "fetchEntries" in mod
        assert "verifyWebhook" in mod
        assert "@sanity/client" in mod
        assert "timingSafeEquals" in mod  # webhook sig compare is timing-safe

    def test_contentful_adapter_exposes_fetch_and_verify(self, project_dir):
        render_project(project_dir, _default_opts(cms="contentful"))
        mod = (project_dir / "src/lib/cms/contentful.ts").read_text()
        assert "fetchEntries" in mod
        assert "verifyWebhook" in mod
        assert 'from "contentful"' in mod

    def test_sanity_webhook_rejects_invalid_signature(self, project_dir):
        render_project(project_dir, _default_opts(cms="sanity"))
        hook = (project_dir / "src/pages/api/webhooks/sanity.ts").read_text()
        assert "verifyWebhook" in hook
        assert 'status: 401' in hook
        assert "sanity-webhook-signature" in hook

    def test_contentful_webhook_rejects_invalid_signature(self, project_dir):
        render_project(project_dir, _default_opts(cms="contentful"))
        hook = (project_dir / "src/pages/api/webhooks/contentful.ts").read_text()
        assert "verifyWebhook" in hook
        assert 'status: 401' in hook
        assert "x-contentful-webhook-signature" in hook


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
        art = BuildArtifact(path=project_dir, framework="astro")
        art.validate()  # should not raise

    def test_token_fingerprint_masks_token(self, project_dir):
        render_project(project_dir, _default_opts(target="vercel"))
        result = dry_run_deploy(project_dir, _default_opts(target="vercel"))
        fp = result["vercel"]["token_fingerprint"]
        assert fp != "test-token-vercel-placeholder"
        assert "test-token-vercel-placeholder" not in fp

    def test_dry_run_static_only_uses_docker_adapter(self, project_dir):
        """Static target dry-runs DockerNginxAdapter — the W4 family's
        "serve from nginx" reference for static-host deploys."""
        render_project(project_dir, _default_opts(target="static"))
        result = dry_run_deploy(project_dir, _default_opts(target="static"))
        assert "docker" in result
        assert "vercel" not in result
        assert "cloudflare" not in result

    def test_dry_run_node_only_uses_docker_adapter(self, project_dir):
        render_project(project_dir, _default_opts(target="node"))
        result = dry_run_deploy(project_dir, _default_opts(target="node"))
        assert "docker" in result
        assert "vercel" not in result
        assert "cloudflare" not in result

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
        assert report["skill"] == "skill-astro"
        assert report["options"]["project_name"] == "astro-site"
        assert set(report["w4_deploy"]) == {"vercel", "cloudflare", "docker"}
        assert "gates" in report["w5_compliance"]
        gate_ids = {g["gate_id"] for g in report["w5_compliance"]["gates"]}
        assert gate_ids == {"wcag", "gdpr", "spdx"}

    def test_gdpr_retention_doc_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        doc = (project_dir / "docs/privacy/retention.md").read_text()
        assert "Retention" in doc or "retention" in doc
        assert "astro-site" in doc

    def test_gdpr_erasure_handler_shipped(self, project_dir):
        render_project(project_dir, _default_opts(compliance=True))
        handler = project_dir / "src/pages/api/privacy/erasure.ts"
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
#  Cross-stack / Content-vertical — W0-W5 end-to-end
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContentVerticalValidation:
    """The integrating gate — same bar W7 set for SKILL-NUXT.

    If SKILL-ASTRO renders through the same ScaffoldOptions /
    render_project / dry_run_deploy / pilot_report API as
    SKILL-NEXTJS and SKILL-NUXT with no framework-level changes
    required, we can claim W0-W5 survived a *third* consumer — and
    this consumer is shaped differently (content-first SSG vs
    whole-app SSR), so n=3 is meaningfully stronger than n=2.
    """

    def test_full_content_vertical_flow(self, project_dir):
        opts = _default_opts(islands="react", cms="sanity", target="all", compliance=True)
        outcome = render_project(project_dir, opts)
        assert outcome.bytes_written > 0

        report = pilot_report(project_dir, opts)

        # W0/W1: profiles resolved
        assert set(report["w0_w1_profiles"]) == {
            "web-static", "web-ssr-node", "web-vercel", "web-edge-cloudflare",
        }

        # W4: all three adapters construct cleanly
        for tgt in ("vercel", "cloudflare", "docker"):
            assert report["w4_deploy"][tgt]["artifact_valid"] is True

        # W5: compliance bundle ran without erroring
        assert report["w5_compliance"]["total_gates"] == 3
        assert report["w5_compliance"]["failed_count"] == 0

    def test_api_shape_matches_sibling_skills(self):
        """SKILL-NEXTJS, SKILL-NUXT, and SKILL-ASTRO must expose the
        same public attributes on ScaffoldOptions + symmetric entry
        points so the upstream orchestrator can treat them
        interchangeably. This is the hard version of "the framework
        survived a third consumer" — n=3 across shapes, not stacks."""
        from backend import nextjs_scaffolder, nuxt_scaffolder, astro_scaffolder

        next_fields = {f.name for f in nextjs_scaffolder.ScaffoldOptions.__dataclass_fields__.values()}
        nuxt_fields = {f.name for f in nuxt_scaffolder.ScaffoldOptions.__dataclass_fields__.values()}
        astro_fields = {f.name for f in astro_scaffolder.ScaffoldOptions.__dataclass_fields__.values()}

        # Shared contract — all three must ship these knobs.
        shared = {"project_name", "auth", "target", "compliance", "backend_url"}
        assert shared.issubset(next_fields)
        assert shared.issubset(nuxt_fields)
        assert shared.issubset(astro_fields)

        # Entry-point symmetry
        for name in ("render_project", "dry_run_deploy", "pilot_report", "validate_pack"):
            assert hasattr(nextjs_scaffolder, name)
            assert hasattr(nuxt_scaffolder, name)
            assert hasattr(astro_scaffolder, name)

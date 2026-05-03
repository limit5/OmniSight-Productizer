"""W11.9 #XXX — Contract tests for ``backend.web.framework_adapter``.

Pins:

    * Public surface (constants, dataclass shape, error hierarchy,
      Protocol shape, package re-exports).
    * :func:`make_framework_adapter` returns the right adapter for each
      of the three frameworks and rejects anything outside
      :data:`SUPPORTED_FRAMEWORKS`.
    * Each adapter (Next / Nuxt / Astro) emits the same canonical file
      set (count, exact relative paths, traceability HTML at the pinned
      relative path) and bakes the W11.7 manifest correctly.
    * The W11.7 traceability comment round-trips through
      :func:`parse_html_traceability_comment` for the static
      ``public/clone-traceability.html`` of every framework.
    * The Astro layout has the W11.7 comment baked into ``<head>``
      directly (Astro is server-rendered HTML, no JSX detour).
    * The Next.js / Nuxt layouts carry the W11.7 manifest_hash + clone_id
      as ``<meta>`` tags so post-build verification doesn't have to
      parse a comment from inside a JSX/Vue template.
    * :func:`write_rendered_project` round-trips files to disk, refuses
      path traversal, refuses to overwrite when ``overwrite=False``.
    * :func:`assert_no_copied_bytes` is the entry-gate of every
      adapter — a tampered ``TransformedSpec`` carrying ``data:`` URIs
      raises before any file is rendered.
    * Manifest is optional: rendering without one yields a project
      without traceability surfaces (no comment baked, no clone_id meta,
      ``traceability_html_relative_path`` is ``None``).
    * Package re-exports: 23 W11.9 symbols + the post-W11.9 total
      drift-guard pin (146 → 169).

All tests run in-process — no Node / Next / Nuxt / Astro toolchain
needed; the adapter is a pure text emitter.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

import backend.web as web_pkg
from backend.web.clone_manifest import (
    CloneManifest,
    build_clone_manifest,
    parse_html_traceability_comment,
)
from backend.web.content_classifier import RiskClassification, RiskScore
from backend.web.framework_adapter import (
    ASTRO_FRAMEWORK_NAME,
    AstroFrameworkAdapter,
    FrameworkAdapter,
    FrameworkAdapterError,
    GENERATOR_META,
    MAX_RENDERED_IMAGES,
    MAX_RENDERED_NAV_ITEMS,
    MAX_RENDERED_SECTIONS,
    NEXT_FRAMEWORK_NAME,
    NUXT_FRAMEWORK_NAME,
    NextFrameworkAdapter,
    NuxtFrameworkAdapter,
    RenderedFile,
    RenderedProject,
    RenderedProjectWriteError,
    SUPPORTED_FRAMEWORKS,
    TRACEABILITY_HTML_FILENAME,
    TRACEABILITY_HTML_RELATIVE_PATH,
    UnknownFrameworkError,
    make_framework_adapter,
    project_to_audit_payload,
    render_clone_project,
    write_rendered_project,
)
from backend.web.output_transformer import BytesLeakError, TransformedSpec
from backend.web.refusal_signals import RefusalDecision
from backend.web.site_cloner import SiteClonerError


# ─── Fixtures ─────────────────────────────────────────────────────────


def _make_transformed(**overrides: Any) -> TransformedSpec:
    """Return a richly-populated TransformedSpec with overridable fields."""
    base: dict[str, Any] = dict(
        source_url="https://example.com",
        fetched_at="2026-04-29T10:00:00Z",
        backend="playwright",
        title="Welcome to Acme",
        meta={
            "description": "Acme is the very best.",
            "og:url": "https://example.com",  # should be filtered out
            "canonical": "https://example.com",  # should be filtered out
        },
        hero={"heading": "Welcome", "tagline": "Subtitle", "cta_label": "Sign Up"},
        nav=({"label": "Home"}, {"label": "Pricing"}, {"label": "Docs"}),
        sections=(
            {"heading": "Features", "summary": "Lots of features."},
            {"heading": "Pricing", "summary": "Affordable."},
        ),
        footer={"text": "© Acme 2026"},
        images=(
            {
                "url": "https://placehold.co/800x600?text=Hero",
                "alt": "hero placeholder",
                "kind": "placeholder",
                "source_url": "https://example.com/hero.png",
            },
            {
                "url": "https://placehold.co/200x100?text=Logo",
                "alt": "logo placeholder",
                "kind": "placeholder",
                "source_url": "https://example.com/logo.png",
            },
        ),
        colors=("#111827", "#f97316", "#10b981"),
        fonts=("Inter, sans-serif", "Roboto Mono, monospace"),
        spacing={
            "max_width": "1200px",
            "padding": ["1rem", "2rem"],
        },
        warnings=(),
        signals_used=("llm", "image_placeholder"),
        model="claude-haiku-4-5",
        transformations=("bytes_strip", "text_rewrite", "image_placeholder"),
    )
    base.update(overrides)
    return TransformedSpec(**base)


def _make_manifest(transformed: TransformedSpec) -> CloneManifest:
    classification = RiskClassification(
        risk_level="low",
        scores=(RiskScore(category="clean", level="low", reason="ok"),),
        model="claude-haiku-4-5",
        signals_used=("heuristic", "llm"),
        prefilter_only=False,
    )
    refusal = RefusalDecision(
        allowed=True, signals_checked=("robots",), reasons=(), details={},
    )
    return build_clone_manifest(
        source_url=transformed.source_url,
        fetched_at=transformed.fetched_at,
        backend=transformed.backend,
        classification=classification,
        transformed=transformed,
        tenant_id="tenant-1",
        actor="alice@example.com",
        refusal_decision=refusal,
    )


# ─── 1. Public surface ────────────────────────────────────────────────


class TestPublicSurface:

    def test_supported_frameworks_pin(self):
        assert SUPPORTED_FRAMEWORKS == frozenset({"next", "nuxt", "astro"})

    def test_framework_name_constants_pin(self):
        assert NEXT_FRAMEWORK_NAME == "next"
        assert NUXT_FRAMEWORK_NAME == "nuxt"
        assert ASTRO_FRAMEWORK_NAME == "astro"

    def test_traceability_html_path_pin(self):
        assert TRACEABILITY_HTML_RELATIVE_PATH == "public/clone-traceability.html"
        assert TRACEABILITY_HTML_FILENAME == "clone-traceability.html"

    def test_max_rendered_caps(self):
        assert MAX_RENDERED_SECTIONS == 50
        assert MAX_RENDERED_NAV_ITEMS == 50
        assert MAX_RENDERED_IMAGES == 50

    def test_generator_meta_attribution(self):
        assert "OmniSight" in GENERATOR_META
        assert "open-lovable" in GENERATOR_META
        assert "MIT" in GENERATOR_META

    def test_error_hierarchy_chains_to_site_cloner(self):
        # Every adapter error must inherit SiteClonerError so the
        # router's ``except SiteClonerError`` keeps catching uniformly.
        assert issubclass(FrameworkAdapterError, SiteClonerError)
        assert issubclass(UnknownFrameworkError, FrameworkAdapterError)
        assert issubclass(RenderedProjectWriteError, FrameworkAdapterError)

    def test_rendered_file_frozen(self):
        rf = RenderedFile(relative_path="x.txt", content="abc")
        with pytest.raises(FrozenInstanceError):
            rf.relative_path = "y.txt"  # type: ignore[misc]

    def test_rendered_project_frozen(self):
        rp = RenderedProject(
            framework="next",
            adapter_name="NextFrameworkAdapter",
            files=(),
            traceability_html_relative_path=None,
            manifest_clone_id=None,
            manifest_hash=None,
        )
        with pytest.raises(FrozenInstanceError):
            rp.framework = "nuxt"  # type: ignore[misc]

    def test_framework_adapter_protocol_runtime_checkable(self):
        assert isinstance(NextFrameworkAdapter(), FrameworkAdapter)
        assert isinstance(NuxtFrameworkAdapter(), FrameworkAdapter)
        assert isinstance(AstroFrameworkAdapter(), FrameworkAdapter)

    def test_framework_adapter_protocol_rejects_random_object(self):
        # Anything that doesn't have the right shape is not a
        # FrameworkAdapter; runtime_checkable enforces presence of
        # framework / name / render attrs.
        class NotAdapter:
            pass
        assert not isinstance(NotAdapter(), FrameworkAdapter)


# ─── 2. Factory ───────────────────────────────────────────────────────


class TestMakeFrameworkAdapter:

    @pytest.mark.parametrize("framework,cls", [
        ("next", NextFrameworkAdapter),
        ("nuxt", NuxtFrameworkAdapter),
        ("astro", AstroFrameworkAdapter),
    ])
    def test_returns_concrete_adapter(self, framework, cls):
        adapter = make_framework_adapter(framework)
        assert isinstance(adapter, cls)
        assert adapter.framework == framework

    def test_case_insensitive(self):
        assert isinstance(make_framework_adapter("Next"), NextFrameworkAdapter)
        assert isinstance(make_framework_adapter("ASTRO"), AstroFrameworkAdapter)

    def test_strips_whitespace(self):
        assert isinstance(make_framework_adapter("  nuxt  "), NuxtFrameworkAdapter)

    def test_unknown_framework_raises(self):
        with pytest.raises(UnknownFrameworkError):
            make_framework_adapter("svelte")

    def test_empty_framework_raises(self):
        with pytest.raises(UnknownFrameworkError):
            make_framework_adapter("")

    def test_non_string_framework_raises(self):
        with pytest.raises(UnknownFrameworkError):
            make_framework_adapter(123)  # type: ignore[arg-type]


# ─── 3. Render — common contract per adapter ──────────────────────────


@pytest.fixture
def transformed():
    return _make_transformed()


@pytest.fixture
def manifest(transformed):
    return _make_manifest(transformed)


class TestRenderCommonContract:

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_returns_rendered_project(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        assert isinstance(proj, RenderedProject)
        assert proj.framework == framework

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_emits_nine_files(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        # Every adapter ships the same artefact count: package.json,
        # framework config, tsconfig, layout/page (×2), css, traceability
        # html, gitignore, README.md = 9.
        assert len(proj.files) == 9

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_includes_traceability_html(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        relpaths = [f.relative_path for f in proj.files]
        assert TRACEABILITY_HTML_RELATIVE_PATH in relpaths
        assert proj.traceability_html_relative_path == TRACEABILITY_HTML_RELATIVE_PATH

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_includes_package_json(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        pkg = next(f for f in proj.files if f.relative_path == "package.json")
        # Every package.json must declare a private project (we don't
        # want anyone npm-publishing a clone) and a "scripts" section
        # the operator can run with `npm run dev`.
        assert '"private": true' in pkg.content
        assert '"dev"' in pkg.content

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_traceability_html_contains_w117_comment(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        trace = next(f for f in proj.files if f.relative_path == TRACEABILITY_HTML_RELATIVE_PATH)
        parsed = parse_html_traceability_comment(trace.content)
        assert parsed is not None, "traceability html must carry a W11.7 comment"
        assert parsed["clone_id"] == manifest.clone_id
        assert parsed["manifest_hash"] == manifest.manifest_hash
        assert parsed["source_url"] == "https://example.com"

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_manifest_clone_id_and_hash_pinned_on_project(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        assert proj.manifest_clone_id == manifest.clone_id
        assert proj.manifest_hash == manifest.manifest_hash

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_files_are_rendered_file_instances(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        assert all(isinstance(f, RenderedFile) for f in proj.files)
        # Tuple, not list — frozen dataclass guarantees immutability.
        assert isinstance(proj.files, tuple)

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_paths_are_relative_and_safe(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        for f in proj.files:
            assert not f.relative_path.startswith("/"), f"absolute path: {f.relative_path}"
            assert "\\" not in f.relative_path, f"backslash in path: {f.relative_path}"
            assert ".." not in f.relative_path.split("/"), f"traversal in path: {f.relative_path}"

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_drops_source_identity_meta(self, framework, transformed, manifest):
        # ``og:url`` and ``canonical`` carry source identity and MUST
        # NOT survive into the rendered project even if they slip past
        # the L3 transformer.
        proj = render_clone_project(transformed, framework, manifest=manifest)
        for f in proj.files:
            # The source URL itself is in the manifest comment + meta
            # (that's by design — it's the *traceability* surface), but
            # the og:url meta tag must not leak into the layout.
            assert 'name="og:url"' not in f.content, (
                f"og:url leaked into {f.relative_path}"
            )
            assert 'name="canonical"' not in f.content, (
                f"canonical leaked into {f.relative_path}"
            )

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_carries_safe_meta_through(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        # ``description`` survives — it's on the L3-allowed-keys list.
        all_content = "\n".join(f.content for f in proj.files)
        assert "Acme is the very best." in all_content

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_files_are_unique_relative_paths(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        relpaths = [f.relative_path for f in proj.files]
        assert len(relpaths) == len(set(relpaths)), (
            f"duplicate relative paths in {framework}: {relpaths}"
        )

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_emits_design_tokens_css(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        # Every adapter emits the design-token CSS inline somewhere.
        all_content = "\n".join(f.content for f in proj.files)
        assert "--omnisight-color-1" in all_content
        assert "#111827" in all_content  # passthrough colour token


# ─── 4. Render — without manifest ─────────────────────────────────────


class TestRenderWithoutManifest:

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_render_without_manifest_succeeds(self, framework, transformed):
        proj = render_clone_project(transformed, framework, manifest=None)
        assert isinstance(proj, RenderedProject)
        # The traceability surface is opted out without a manifest.
        assert proj.traceability_html_relative_path is None
        assert proj.manifest_clone_id is None
        assert proj.manifest_hash is None

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_traceability_html_carries_no_w117_comment_without_manifest(
        self, framework, transformed,
    ):
        proj = render_clone_project(transformed, framework, manifest=None)
        # The static traceability HTML file is still emitted (so the
        # static asset path is consistent across builds), but it does
        # NOT carry a manifest comment.
        trace = next(f for f in proj.files if f.relative_path == TRACEABILITY_HTML_RELATIVE_PATH)
        parsed = parse_html_traceability_comment(trace.content)
        assert parsed is None

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_no_clone_meta_tags_without_manifest(
        self, framework, transformed,
    ):
        proj = render_clone_project(transformed, framework, manifest=None)
        all_content = "\n".join(f.content for f in proj.files)
        assert "omnisight-manifest-hash" not in all_content


# ─── 5. Render — input validation ─────────────────────────────────────


class TestRenderInputValidation:

    def test_rejects_non_transformed_spec(self, manifest):
        with pytest.raises(FrameworkAdapterError):
            render_clone_project({"title": "x"}, "next", manifest=manifest)  # type: ignore[arg-type]

    def test_rejects_non_manifest(self, transformed):
        with pytest.raises(FrameworkAdapterError):
            render_clone_project(transformed, "next", manifest={"clone_id": "x"})  # type: ignore[arg-type]

    def test_rejects_unknown_framework(self, transformed, manifest):
        with pytest.raises(UnknownFrameworkError):
            render_clone_project(transformed, "svelte", manifest=manifest)

    def test_explicit_adapter_overrides_framework_arg(self, transformed, manifest):
        # When ``adapter`` is supplied, ``framework`` is ignored —
        # useful for tests / future Vue / Svelte rows that plug their
        # own adapter in.
        adapter = NextFrameworkAdapter()
        proj = render_clone_project(transformed, "asdf", manifest=manifest, adapter=adapter)
        assert proj.framework == "next"

    def test_rejects_bad_adapter_object(self, transformed, manifest):
        class NotAdapter:
            pass
        with pytest.raises(FrameworkAdapterError):
            render_clone_project(transformed, "next", manifest=manifest, adapter=NotAdapter())  # type: ignore[arg-type]


# ─── 6. assert_no_copied_bytes invariant ──────────────────────────────


class TestNoCopiedBytesInvariant:

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_data_uri_in_image_url_raises_before_render(self, framework, manifest):
        bad = _make_transformed(images=({
            "url": "data:image/png;base64,AAAA",
            "alt": "leaked",
            "kind": "placeholder",
            "source_url": "https://example.com/x.png",
        },))
        # Manifest builder may already reject this on its own — so
        # build a fresh manifest from the *good* spec first:
        good_manifest = _make_manifest(_make_transformed())
        with pytest.raises(BytesLeakError):
            render_clone_project(bad, framework, manifest=good_manifest)

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_clean_spec_passes_invariant(self, framework, transformed, manifest):
        # No exception means the gate passed.
        proj = render_clone_project(transformed, framework, manifest=manifest)
        assert isinstance(proj, RenderedProject)


# ─── 7. Astro-specific: comment baked into Layout.astro ───────────────


class TestAstroLayoutBakesComment:

    def test_layout_carries_w117_comment_in_head(self, transformed, manifest):
        proj = render_clone_project(transformed, "astro", manifest=manifest)
        layout = next(
            f for f in proj.files if f.relative_path == "src/layouts/Layout.astro"
        )
        # Astro ships server-rendered HTML so the W11.7 comment can
        # live in the layout's <head> directly.
        parsed = parse_html_traceability_comment(layout.content)
        assert parsed is not None
        assert parsed["clone_id"] == manifest.clone_id
        assert parsed["manifest_hash"] == manifest.manifest_hash

    def test_layout_does_not_carry_comment_without_manifest(self, transformed):
        proj = render_clone_project(transformed, "astro", manifest=None)
        layout = next(
            f for f in proj.files if f.relative_path == "src/layouts/Layout.astro"
        )
        parsed = parse_html_traceability_comment(layout.content)
        assert parsed is None


# ─── 8. Next/Nuxt manifest meta surface ───────────────────────────────


class TestNextManifestMetaSurface:

    def test_layout_metadata_carries_manifest_hash(self, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        layout = next(f for f in proj.files if f.relative_path == "app/layout.tsx")
        assert manifest.manifest_hash in layout.content
        assert "omnisight-manifest-hash" in layout.content
        assert "omnisight-clone-id" in layout.content
        # Pinned identifier carried explicitly so DMCA / audit tooling
        # can grep the built page without parsing JSX.
        assert manifest.clone_id in layout.content


class TestNuxtManifestMetaSurface:

    def test_nuxt_config_meta_carries_manifest_hash(self, transformed, manifest):
        proj = render_clone_project(transformed, "nuxt", manifest=manifest)
        config = next(f for f in proj.files if f.relative_path == "nuxt.config.ts")
        assert manifest.manifest_hash in config.content
        assert "omnisight-manifest-hash" in config.content
        assert "omnisight-clone-id" in config.content


# ─── 9. write_rendered_project ────────────────────────────────────────


class TestWriteRenderedProject:

    def test_writes_all_files_round_trip(self, tmp_path, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        paths = write_rendered_project(proj, project_root=tmp_path)
        assert len(paths) == len(proj.files)
        for f, p in zip(proj.files, paths):
            assert p.exists()
            assert p.read_text(encoding="utf-8") == f.content
            # Path must be relative-to project_root (no escape).
            p.relative_to(tmp_path.resolve())

    def test_creates_parent_directories_on_demand(self, tmp_path, transformed, manifest):
        proj = render_clone_project(transformed, "nuxt", manifest=manifest)
        write_rendered_project(proj, project_root=tmp_path / "fresh-subdir")
        assert (tmp_path / "fresh-subdir" / "pages" / "index.vue").exists()

    def test_overwrite_default_true(self, tmp_path, transformed, manifest):
        proj = render_clone_project(transformed, "astro", manifest=manifest)
        write_rendered_project(proj, project_root=tmp_path)
        # Second call with same project — no error by default.
        write_rendered_project(proj, project_root=tmp_path)

    def test_overwrite_false_refuses_existing(self, tmp_path, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        write_rendered_project(proj, project_root=tmp_path)
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project(proj, project_root=tmp_path, overwrite=False)

    def test_rejects_non_project(self, tmp_path):
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project({"files": []}, project_root=tmp_path)  # type: ignore[arg-type]

    def test_rejects_path_traversal(self, tmp_path):
        bad = RenderedProject(
            framework="next",
            adapter_name="X",
            files=(RenderedFile(relative_path="../escape.txt", content="evil"),),
            traceability_html_relative_path=None,
            manifest_clone_id=None,
            manifest_hash=None,
        )
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project(bad, project_root=tmp_path)

    def test_rejects_absolute_path(self, tmp_path):
        bad = RenderedProject(
            framework="next",
            adapter_name="X",
            files=(RenderedFile(relative_path="/etc/passwd", content="evil"),),
            traceability_html_relative_path=None,
            manifest_clone_id=None,
            manifest_hash=None,
        )
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project(bad, project_root=tmp_path)

    def test_rejects_backslash_path(self, tmp_path):
        bad = RenderedProject(
            framework="next",
            adapter_name="X",
            files=(RenderedFile(relative_path="dir\\file.txt", content="evil"),),
            traceability_html_relative_path=None,
            manifest_clone_id=None,
            manifest_hash=None,
        )
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project(bad, project_root=tmp_path)

    def test_rejects_empty_segments(self, tmp_path):
        bad = RenderedProject(
            framework="next",
            adapter_name="X",
            files=(RenderedFile(relative_path="a//b.txt", content="x"),),
            traceability_html_relative_path=None,
            manifest_clone_id=None,
            manifest_hash=None,
        )
        with pytest.raises(RenderedProjectWriteError):
            write_rendered_project(bad, project_root=tmp_path)

    def test_string_project_root_accepted(self, tmp_path, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        # str path also accepted (Path | str signature).
        paths = write_rendered_project(proj, project_root=str(tmp_path))
        assert len(paths) == len(proj.files)


# ─── 10. project_to_audit_payload ─────────────────────────────────────


class TestProjectToAuditPayload:

    def test_payload_top_level_keys(self, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        payload = project_to_audit_payload(proj)
        assert set(payload.keys()) == {
            "framework",
            "adapter",
            "files",
            "traceability_html_path",
            "manifest_clone_id",
            "manifest_hash",
        }

    def test_payload_files_are_relative_paths_only(self, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        payload = project_to_audit_payload(proj)
        # File contents are *not* in the payload — only relative paths.
        # Audit row stays compact.
        assert all(isinstance(p, str) for p in payload["files"])
        assert "package.json" in payload["files"]
        assert TRACEABILITY_HTML_RELATIVE_PATH in payload["files"]
        # No content blob on any of these.
        assert all("{" not in p for p in payload["files"][:3] if p != "package.json")

    def test_payload_has_no_manifest_when_none(self, transformed):
        proj = render_clone_project(transformed, "next", manifest=None)
        payload = project_to_audit_payload(proj)
        assert payload["manifest_clone_id"] is None
        assert payload["manifest_hash"] is None
        assert payload["traceability_html_path"] is None

    def test_payload_carries_manifest_hash(self, transformed, manifest):
        proj = render_clone_project(transformed, "next", manifest=manifest)
        payload = project_to_audit_payload(proj)
        assert payload["manifest_hash"] == manifest.manifest_hash
        assert payload["manifest_clone_id"] == manifest.clone_id

    def test_payload_rejects_non_project(self):
        with pytest.raises(FrameworkAdapterError):
            project_to_audit_payload({"framework": "next"})  # type: ignore[arg-type]


# ─── 11. List caps ────────────────────────────────────────────────────


class TestListCaps:

    def test_huge_section_count_capped_at_max(self, manifest):
        # Build a TransformedSpec with 200 sections — we expect ≤ 50 to
        # be rendered. (Mirrors L3 transformer's MAX_REWRITTEN_LIST_ITEMS).
        many_sections = tuple(
            {"heading": f"H{i}", "summary": f"S{i}"} for i in range(200)
        )
        ts = _make_transformed(sections=many_sections)
        # Re-build manifest from the same spec so its hash reflects the
        # capped output (the manifest builder does its own caps too).
        m = _make_manifest(ts)
        proj = render_clone_project(ts, "next", manifest=m)
        page = next(f for f in proj.files if f.relative_path == "app/page.tsx")
        # Count <h2> emitted in the page; each section emits exactly
        # one <h2>. Cap is MAX_RENDERED_SECTIONS.
        assert page.content.count("<h2>") <= MAX_RENDERED_SECTIONS

    def test_huge_nav_count_capped(self, manifest):
        many_nav = tuple({"label": f"L{i}"} for i in range(200))
        ts = _make_transformed(nav=many_nav)
        m = _make_manifest(ts)
        proj = render_clone_project(ts, "nuxt", manifest=m)
        page = next(f for f in proj.files if f.relative_path == "pages/index.vue")
        # Each nav label emits exactly one <li>, so the rendered page
        # has ≤ MAX_RENDERED_NAV_ITEMS <li> tags.
        assert page.content.count("<li>") <= MAX_RENDERED_NAV_ITEMS


# ─── 12. Adapter shape consistency across frameworks ─────────────────


class TestAdapterShapeConsistencyAcrossFrameworks:

    def test_all_three_share_traceability_path(self, transformed, manifest):
        # Every adapter ships the static traceability HTML at the same
        # relative path so post-build verification tooling has one URL
        # to crawl regardless of framework.
        next_proj = render_clone_project(transformed, "next", manifest=manifest)
        nuxt_proj = render_clone_project(transformed, "nuxt", manifest=manifest)
        astro_proj = render_clone_project(transformed, "astro", manifest=manifest)
        assert next_proj.traceability_html_relative_path == TRACEABILITY_HTML_RELATIVE_PATH
        assert nuxt_proj.traceability_html_relative_path == TRACEABILITY_HTML_RELATIVE_PATH
        assert astro_proj.traceability_html_relative_path == TRACEABILITY_HTML_RELATIVE_PATH

    def test_traceability_html_byte_identical_across_frameworks(self, transformed, manifest):
        # The static traceability scaffold is built from the
        # TransformedSpec + manifest only — it must NOT vary by
        # framework, so DMCA / audit tooling can rely on it being the
        # same artefact regardless of the rendered framework.
        next_proj = render_clone_project(transformed, "next", manifest=manifest)
        nuxt_proj = render_clone_project(transformed, "nuxt", manifest=manifest)
        astro_proj = render_clone_project(transformed, "astro", manifest=manifest)

        def _trace(p):
            return next(
                f.content for f in p.files if f.relative_path == TRACEABILITY_HTML_RELATIVE_PATH
            )

        assert _trace(next_proj) == _trace(nuxt_proj) == _trace(astro_proj)


# ─── 13. Package re-exports ───────────────────────────────────────────


W11_9_SYMBOLS = (
    "ASTRO_FRAMEWORK_NAME",
    "AstroFrameworkAdapter",
    "FrameworkAdapter",
    "FrameworkAdapterError",
    "GENERATOR_META",
    "MAX_RENDERED_IMAGES",
    "MAX_RENDERED_NAV_ITEMS",
    "MAX_RENDERED_SECTIONS",
    "NEXT_FRAMEWORK_NAME",
    "NUXT_FRAMEWORK_NAME",
    "NextFrameworkAdapter",
    "NuxtFrameworkAdapter",
    "RenderedFile",
    "RenderedProject",
    "RenderedProjectWriteError",
    "SUPPORTED_FRAMEWORKS",
    "TRACEABILITY_HTML_FILENAME",
    "TRACEABILITY_HTML_RELATIVE_PATH",
    "UnknownFrameworkError",
    "make_framework_adapter",
    "project_to_audit_payload",
    "render_clone_project",
    "write_rendered_project",
)


@pytest.mark.parametrize("symbol", W11_9_SYMBOLS)
def test_w11_9_symbol_re_exported_via_package(symbol):
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert hasattr(web_pkg, symbol), f"{symbol} not attribute of backend.web"


def test_total_re_export_count_pinned_at_192():
    # W11.8 left __all__ at 146 symbols; W11.9 adds 23 framework_adapter
    # symbols → 169; W11.10 adds 12 clone_spec_context symbols → 181;
    # W11.12 adds 11 clone_audit symbols → 192;
    # W13.2 adds 7 screenshot-breakpoint symbols → 199;
    # W13.3 adds 18 screenshot-writer symbols → 217;
    # W13.4 adds 16 screenshot-ghost-overlay symbols → 233;
    # W15.2 adds 11 vite_error_relay symbols → 244;
    # W15.3 adds 8 vite_error_prompt symbols → 252;
    # W15.4 adds 10 vite_retry_budget symbols → 262.
    # If this fails with a different count, audit whether you consciously
    # added / removed a public symbol and update the pin alongside the
    # current row's TODO entry.
    assert len(web_pkg.__all__) == 262


# ─── 14. Whole-spec invariants ────────────────────────────────────────


class TestWholeSpecInvariants:

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_html_escape_in_traceability_scaffold(self, framework, manifest):
        # If the L3 rewrite output contained an HTML special char
        # (e.g. ``<script>``), the adapter MUST escape it before
        # putting it into the static ``public/clone-traceability.html``
        # scaffold — that file is rendered HTML the adapter emits
        # directly (no framework runtime in between to escape). JS
        # string literals inside JSX / Vue / Astro frontmatter
        # expression contexts get escaped at framework render-time
        # by the framework itself, so this invariant only fences
        # the literal-HTML surface we own directly.
        ts = _make_transformed(
            title="Hello <script>alert(1)</script>",
            hero={"heading": "<img src=x>", "tagline": "&quot;", "cta_label": "OK"},
        )
        m = _make_manifest(ts)
        proj = render_clone_project(ts, framework, manifest=m)
        trace = next(
            f for f in proj.files if f.relative_path == TRACEABILITY_HTML_RELATIVE_PATH
        )
        assert "<script>alert(1)</script>" not in trace.content
        # Belt-and-braces: an unescaped raw <img src=x> shouldn't
        # survive either.
        assert "<img src=x>" not in trace.content
        # The escaped form must be present (proves we ran html.escape
        # over the title rather than just stripping the input).
        assert "&lt;script&gt;" in trace.content

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_files_are_text(self, framework, transformed, manifest):
        # Every emitted file must be a text string (not bytes / blob).
        proj = render_clone_project(transformed, framework, manifest=manifest)
        for f in proj.files:
            assert isinstance(f.content, str)

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_attribution_in_readme(self, framework, transformed, manifest):
        proj = render_clone_project(transformed, framework, manifest=manifest)
        readme = next(f for f in proj.files if f.relative_path == "README.md")
        assert "open-lovable" in readme.content
        assert "MIT" in readme.content

    @pytest.mark.parametrize("framework", ["next", "nuxt", "astro"])
    def test_gitignore_excludes_omnisight_dir(self, framework, transformed, manifest):
        # ``.omnisight/`` carries the W11.7 manifest JSON; we don't
        # want it in the operator's git history (the audit row /
        # external manifest store is the canonical record).
        proj = render_clone_project(transformed, framework, manifest=manifest)
        gi = next(f for f in proj.files if f.relative_path == ".gitignore")
        assert ".omnisight/" in gi.content

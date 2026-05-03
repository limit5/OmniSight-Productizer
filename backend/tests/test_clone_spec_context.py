"""W11.10 #XXX — Contract tests for ``backend.web.clone_spec_context``.

Pins:

    * Public surface (constants, error hierarchy, package re-exports).
    * :func:`build_clone_spec_context` produces a deterministic markdown
      block with all five W11 invariants pinned and the manifest
      fingerprint surfaced (or ``absent`` when no manifest is supplied).
    * Defensive :func:`assert_no_copied_bytes` re-run on the input —
      ``data:`` URI / forbidden field raise :class:`CloneSpecContextError`.
    * Per-category caps (nav / sections / images / colours / fonts) and
      the ``MAX_CLONE_SPEC_CONTEXT_CHARS`` global cap.
    * :func:`backend.prompt_loader.build_system_prompt` accepts
      ``clone_spec_context`` and threads the block into the assembled
      prompt with the ``# Clone Spec Context (W11)`` header.
    * :class:`backend.agents.state.GraphState` carries the new field
      with empty-string default and is wired through
      ``_specialist_node_factory``.
    * Package re-exports: 12 W11.10 symbols + the running total
      drift-guard pin (169 → 181 after W11.10 → 192 after W11.12).
"""

from __future__ import annotations

from typing import Any

import pytest

import backend.web as web_pkg
from backend.web.clone_manifest import CloneManifest, build_clone_manifest
from backend.web.clone_spec_context import (
    CLONE_SPEC_CONTEXT_HEADER,
    CloneSpecContextError,
    MAX_CLONE_SPEC_CONTEXT_CHARS,
    MAX_CONTEXT_COLOR_ITEMS,
    MAX_CONTEXT_FONT_ITEMS,
    MAX_CONTEXT_IMAGE_ITEMS,
    MAX_CONTEXT_NAV_ITEMS,
    MAX_CONTEXT_SECTION_ITEMS,
    MAX_CONTEXT_SECTION_SUMMARY_CHARS,
    TRUNCATION_MARKER,
    W11_INVARIANTS_BLOCK,
    build_clone_spec_context,
)
from backend.web.content_classifier import RiskClassification, RiskScore
from backend.web.output_transformer import TransformedSpec
from backend.web.refusal_signals import RefusalDecision
from backend.web.site_cloner import SiteClonerError


# ─── Fixtures ─────────────────────────────────────────────────────────


def _make_transformed(**overrides: Any) -> TransformedSpec:
    base: dict[str, Any] = dict(
        source_url="https://acme.example/landing",
        fetched_at="2026-04-29T10:00:00Z",
        backend="playwright",
        title="Welcome to Our Brand",
        meta={"description": "Generic tagline."},
        hero={
            "heading": "Build with confidence",
            "tagline": "Tools that scale.",
            "cta_label": "Sign up",
        },
        nav=({"label": "Home"}, {"label": "Pricing"}, {"label": "Docs"}),
        sections=(
            {"heading": "Features", "summary": "Lots of features."},
            {"heading": "Pricing", "summary": "Affordable."},
        ),
        footer={"text": "Our Brand 2026"},
        images=(
            {
                "url": "https://placehold.co/800x600?text=Hero",
                "alt": "hero placeholder",
                "kind": "placeholder",
                "source_url": "https://acme.example/hero.png",
            },
        ),
        colors=("#111827", "#f97316", "#10b981"),
        fonts=("Inter, sans-serif", "Roboto Mono, monospace"),
        spacing={"max_width": "1200px", "gap": "8px"},
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

    def test_header_pinned(self):
        assert CLONE_SPEC_CONTEXT_HEADER == "# Clone Spec Context (W11)"

    def test_max_chars_default(self):
        assert MAX_CLONE_SPEC_CONTEXT_CHARS == 4_000

    def test_per_category_caps_sane(self):
        assert MAX_CONTEXT_NAV_ITEMS == 12
        assert MAX_CONTEXT_SECTION_ITEMS == 6
        assert MAX_CONTEXT_IMAGE_ITEMS == 6
        assert MAX_CONTEXT_COLOR_ITEMS == 12
        assert MAX_CONTEXT_FONT_ITEMS == 8
        assert MAX_CONTEXT_SECTION_SUMMARY_CHARS == 240

    def test_invariants_block_pins_no_copy_bytes(self):
        assert "Never copy bytes" in W11_INVARIANTS_BLOCK
        assert "Image placeholders only" in W11_INVARIANTS_BLOCK
        assert "Attribution travels" in W11_INVARIANTS_BLOCK
        assert "Echo the manifest fingerprint" in W11_INVARIANTS_BLOCK

    def test_invariants_block_lists_five_rules(self):
        # Five numbered rules — operators read this block to verify the
        # full W11 invariants survive into the agent prompt.
        for n in ("1.", "2.", "3.", "4.", "5."):
            assert n in W11_INVARIANTS_BLOCK, f"rule {n} missing from W11 invariants"

    def test_truncation_marker_distinct_from_per_category_marker(self):
        assert TRUNCATION_MARKER != ""
        assert "[clone-spec context truncated]" in TRUNCATION_MARKER
        # per-category cap markers say "more …" — must not collide
        assert "more" not in TRUNCATION_MARKER

    def test_error_hierarchy_chains_to_site_cloner_error(self):
        assert issubclass(CloneSpecContextError, SiteClonerError)


# ─── 2. build_clone_spec_context — happy path ────────────────────────


class TestBuildCloneSpecContextHappyPath:

    def test_returns_string_starting_with_header(self):
        block = build_clone_spec_context(_make_transformed())
        assert block.startswith(CLONE_SPEC_CONTEXT_HEADER)

    def test_identity_block_uses_absent_when_no_manifest(self):
        block = build_clone_spec_context(_make_transformed())
        assert "clone_id: `absent`" in block
        assert "manifest_hash: `absent`" in block

    def test_identity_block_uses_manifest_when_supplied(self):
        spec = _make_transformed()
        manifest = _make_manifest(spec)
        block = build_clone_spec_context(spec, manifest=manifest)
        assert f"clone_id: `{manifest.clone_id}`" in block
        assert f"manifest_hash: `{manifest.manifest_hash}`" in block
        assert "sha256:" in block

    def test_identity_includes_attribution(self):
        block = build_clone_spec_context(_make_transformed())
        assert "open-lovable" in block
        assert "MIT" in block
        assert "LICENSES/open-lovable-mit.txt" in block

    def test_identity_includes_source_url_and_backend(self):
        block = build_clone_spec_context(_make_transformed())
        assert "https://acme.example/landing" in block
        assert "playwright" in block

    def test_identity_includes_rewrite_model_and_transformations(self):
        block = build_clone_spec_context(_make_transformed())
        assert "claude-haiku-4-5" in block
        assert "bytes_strip" in block
        assert "text_rewrite" in block
        assert "image_placeholder" in block

    def test_invariants_block_present_verbatim(self):
        block = build_clone_spec_context(_make_transformed())
        assert W11_INVARIANTS_BLOCK in block

    def test_outline_renders_title_hero_nav_sections_footer(self):
        block = build_clone_spec_context(_make_transformed())
        assert "Welcome to Our Brand" in block
        assert "Build with confidence" in block
        assert "Tools that scale." in block
        assert "Sign up" in block
        # nav labels
        assert "- Home" in block
        assert "- Pricing" in block
        assert "- Docs" in block
        # section headings + summaries
        assert "**Features**" in block
        assert "Lots of features." in block
        # footer
        assert "Our Brand 2026" in block

    def test_design_tokens_listed(self):
        block = build_clone_spec_context(_make_transformed())
        # colours
        assert "#111827" in block
        assert "#f97316" in block
        # fonts
        assert "Inter, sans-serif" in block
        # spacing — keys + values
        assert "max_width" in block
        assert "1200px" in block
        assert "gap" in block

    def test_image_block_lists_placeholders_and_omits_source_url(self):
        block = build_clone_spec_context(_make_transformed())
        assert "https://placehold.co/800x600" in block
        # source_url is provenance — must NOT appear in agent-visible block
        assert "https://acme.example/hero.png" not in block

    def test_returns_block_under_max_chars_default(self):
        block = build_clone_spec_context(_make_transformed())
        assert len(block) <= MAX_CLONE_SPEC_CONTEXT_CHARS


# ─── 3. Empty / sparse spec degrades gracefully ──────────────────────


class TestEmptySpecDegradesGracefully:

    def test_empty_hero_renders_empty_marker(self):
        spec = _make_transformed(hero=None)
        block = build_clone_spec_context(spec)
        assert "- hero:\n  (empty)" in block

    def test_empty_nav_renders_empty_marker(self):
        spec = _make_transformed(nav=())
        block = build_clone_spec_context(spec)
        assert "- nav:\n  (empty)" in block

    def test_empty_sections_renders_empty_marker(self):
        spec = _make_transformed(sections=())
        block = build_clone_spec_context(spec)
        assert "- sections:\n  (empty)" in block

    def test_empty_footer_renders_empty_marker(self):
        spec = _make_transformed(footer=None)
        block = build_clone_spec_context(spec)
        assert "- footer: (empty)" in block

    def test_empty_images_renders_empty_marker(self):
        spec = _make_transformed(images=())
        block = build_clone_spec_context(spec)
        assert "(empty)" in block

    def test_empty_design_tokens_render_empty_markers(self):
        spec = _make_transformed(colors=(), fonts=(), spacing={})
        block = build_clone_spec_context(spec)
        assert "- colors: (empty)" in block
        assert "- fonts: (empty)" in block
        assert "- spacing:\n  (empty)" in block

    def test_minimal_spec_still_buildable(self):
        spec = TransformedSpec(
            source_url="https://example.com",
            fetched_at="2026-04-29T00:00:00Z",
            backend="playwright",
            title="",
            meta={},
            hero=None,
            nav=(),
            sections=(),
            footer=None,
            images=(),
            colors=(),
            fonts=(),
            spacing={},
            warnings=(),
            signals_used=("llm",),
            model="m",
            transformations=("bytes_strip",),
        )
        block = build_clone_spec_context(spec)
        assert block.startswith(CLONE_SPEC_CONTEXT_HEADER)
        assert "https://example.com" in block


# ─── 4. Per-category caps ────────────────────────────────────────────


class TestPerCategoryCaps:

    def test_nav_caps_at_max_context_nav_items(self):
        many = tuple({"label": f"item-{i}"} for i in range(MAX_CONTEXT_NAV_ITEMS + 8))
        spec = _make_transformed(nav=many)
        block = build_clone_spec_context(spec)
        assert "more nav items" in block
        # First N labels appear, the (N+1)-th does not
        assert f"- item-{MAX_CONTEXT_NAV_ITEMS - 1}" in block
        assert f"- item-{MAX_CONTEXT_NAV_ITEMS}" not in block

    def test_sections_cap_at_max(self):
        many = tuple(
            {"heading": f"sect-{i}", "summary": "x"}
            for i in range(MAX_CONTEXT_SECTION_ITEMS + 4)
        )
        spec = _make_transformed(sections=many)
        block = build_clone_spec_context(spec)
        assert "more sections" in block
        assert f"**sect-{MAX_CONTEXT_SECTION_ITEMS - 1}**" in block
        assert f"**sect-{MAX_CONTEXT_SECTION_ITEMS}**" not in block

    def test_images_cap_at_max(self):
        many = tuple(
            {
                "url": f"https://placehold.co/img-{i}",
                "alt": f"a{i}",
                "kind": "placeholder",
                "source_url": f"src{i}",
            }
            for i in range(MAX_CONTEXT_IMAGE_ITEMS + 3)
        )
        spec = _make_transformed(images=many)
        block = build_clone_spec_context(spec)
        assert "more images" in block

    def test_colors_cap_at_max(self):
        many = tuple(f"#{i:06x}" for i in range(MAX_CONTEXT_COLOR_ITEMS + 5))
        spec = _make_transformed(colors=many)
        block = build_clone_spec_context(spec)
        # last enumerated colour visible, first dropped colour invisible
        assert f"#{MAX_CONTEXT_COLOR_ITEMS - 1:06x}" in block
        assert "more]" in block.split("colors:")[1]

    def test_fonts_cap_at_max(self):
        many = tuple(f"Font-{i}" for i in range(MAX_CONTEXT_FONT_ITEMS + 4))
        spec = _make_transformed(fonts=many)
        block = build_clone_spec_context(spec)
        assert f"Font-{MAX_CONTEXT_FONT_ITEMS - 1}" in block
        assert "more]" in block.split("fonts:")[1].split("\n")[0]

    def test_section_summary_cap(self):
        long_summary = "x" * (MAX_CONTEXT_SECTION_SUMMARY_CHARS + 50)
        spec = _make_transformed(
            sections=({"heading": "long", "summary": long_summary},),
        )
        block = build_clone_spec_context(spec)
        # The summary is truncated to the cap with an ellipsis appended
        assert long_summary not in block
        assert "…" in block


# ─── 5. Whole-block max-chars cap ────────────────────────────────────


class TestWholeBlockCap:

    def test_truncation_appends_marker(self):
        # Force overflow: many sections of long-but-capped summaries.
        many = tuple(
            {"heading": f"h{i}", "summary": "y" * MAX_CONTEXT_SECTION_SUMMARY_CHARS}
            for i in range(MAX_CONTEXT_SECTION_ITEMS)
        )
        many_nav = tuple({"label": f"l{i}"} for i in range(MAX_CONTEXT_NAV_ITEMS))
        spec = _make_transformed(
            title="z" * 500,
            sections=many,
            nav=many_nav,
            colors=tuple(f"#{i:06x}" for i in range(MAX_CONTEXT_COLOR_ITEMS)),
        )
        block = build_clone_spec_context(spec)
        # Block stays under the cap (post-trim).
        assert len(block) <= MAX_CLONE_SPEC_CONTEXT_CHARS
        # If we overflowed, the marker appears; if we did not, header is intact.
        if "[clone-spec context truncated]" in block:
            assert block.endswith(TRUNCATION_MARKER)
        # Header always survives — operators must always know which clone the block referred to.
        assert block.startswith(CLONE_SPEC_CONTEXT_HEADER)


# ─── 6. Bytes-leak invariant (defense in depth) ──────────────────────


class TestBytesLeakInvariant:

    def test_data_uri_in_image_url_raises(self):
        spec = _make_transformed(
            images=(
                {
                    "url": "data:image/png;base64,deadbeef",
                    "alt": "leak",
                    "kind": "placeholder",
                    "source_url": "https://acme.example/h.png",
                },
            ),
        )
        with pytest.raises(CloneSpecContextError) as exc:
            build_clone_spec_context(spec)
        assert "no-copied-bytes" in str(exc.value)

    def test_base64_prefix_in_url_raises(self):
        # ``assert_no_copied_bytes`` matches the ``base64,`` *prefix* (the
        # only realistic shape — a stripped data URI). A URL that merely
        # contains ``;base64,`` somewhere in the path is fine.
        spec = _make_transformed(
            images=(
                {
                    "url": "base64,deadbeef",
                    "alt": "leak",
                    "kind": "placeholder",
                    "source_url": "src",
                },
            ),
        )
        with pytest.raises(CloneSpecContextError):
            build_clone_spec_context(spec)

    def test_forbidden_field_raises(self):
        spec = _make_transformed(
            images=(
                {
                    "url": "https://example.com/img.png",
                    "alt": "x",
                    "kind": "placeholder",
                    "source_url": "src",
                    "bytes": "deadbeef",
                },
            ),
        )
        with pytest.raises(CloneSpecContextError):
            build_clone_spec_context(spec)


# ─── 7. Input validation ─────────────────────────────────────────────


class TestInputValidation:

    def test_non_transformed_spec_raises(self):
        with pytest.raises(CloneSpecContextError):
            build_clone_spec_context({"title": "x"})  # type: ignore[arg-type]

    def test_non_manifest_raises(self):
        with pytest.raises(CloneSpecContextError):
            build_clone_spec_context(
                _make_transformed(),
                manifest={"clone_id": "x"},  # type: ignore[arg-type]
            )

    def test_none_manifest_accepted(self):
        block = build_clone_spec_context(_make_transformed(), manifest=None)
        assert block.startswith(CLONE_SPEC_CONTEXT_HEADER)


# ─── 8. build_system_prompt integration ──────────────────────────────


class TestBuildSystemPromptIntegration:

    def test_clone_spec_context_appended_to_assembled_prompt(self):
        from backend.prompt_loader import build_system_prompt

        block = build_clone_spec_context(_make_transformed())
        prompt = build_system_prompt(
            model_name="claude-sonnet-4",
            agent_type="general",
            sub_type="",
            clone_spec_context=block,
        )
        assert CLONE_SPEC_CONTEXT_HEADER in prompt
        assert "Never copy bytes" in prompt

    def test_clone_spec_context_empty_is_no_op(self):
        from backend.prompt_loader import build_system_prompt

        prompt = build_system_prompt(
            model_name="claude-sonnet-4",
            agent_type="general",
            sub_type="",
            clone_spec_context="",
        )
        assert CLONE_SPEC_CONTEXT_HEADER not in prompt

    def test_clone_spec_context_truncated_when_oversized(self):
        from backend.prompt_loader import build_system_prompt

        # Force oversize with an arbitrary string longer than the cap.
        oversized = "x" * (MAX_CLONE_SPEC_CONTEXT_CHARS + 1000)
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            clone_spec_context=oversized,
        )
        assert "[clone-spec context truncated]" in prompt

    def test_clone_spec_context_appears_after_task_skill(self):
        from backend.prompt_loader import build_system_prompt

        block = build_clone_spec_context(_make_transformed())
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            task_skill_context="dummy task skill body",
            clone_spec_context=block,
        )
        # Clone block must appear after task skill so the W11 invariants
        # are the last thing the model sees before the handoff context.
        task_idx = prompt.index("# Task Skill")
        clone_idx = prompt.index(CLONE_SPEC_CONTEXT_HEADER)
        assert clone_idx > task_idx

    def test_clone_spec_context_appears_before_handoff(self):
        from backend.prompt_loader import build_system_prompt

        block = build_clone_spec_context(_make_transformed())
        prompt = build_system_prompt(
            model_name="",
            agent_type="general",
            sub_type="",
            handoff_context="prior agent context",
            clone_spec_context=block,
        )
        clone_idx = prompt.index(CLONE_SPEC_CONTEXT_HEADER)
        handoff_idx = prompt.index("# Previous Task Handoff")
        assert clone_idx < handoff_idx


# ─── 9. GraphState wiring ────────────────────────────────────────────


class TestGraphStateWiring:

    def test_clone_spec_context_field_default_empty(self):
        from backend.agents.state import GraphState

        state = GraphState(user_command="x")
        assert state.clone_spec_context == ""

    def test_clone_spec_context_field_round_trips(self):
        from backend.agents.state import GraphState

        block = build_clone_spec_context(_make_transformed())
        state = GraphState(user_command="scaffold", clone_spec_context=block)
        assert state.clone_spec_context == block
        assert state.clone_spec_context.startswith(CLONE_SPEC_CONTEXT_HEADER)


# ─── 10. Specialist node threads clone_spec_context ──────────────────


class TestSpecialistNodeThreadsCloneSpecContext:

    def test_specialist_node_passes_clone_spec_context_to_build_system_prompt(
        self, monkeypatch
    ):
        # Capture the kwargs passed into build_system_prompt by the
        # specialist node so we can assert clone_spec_context flows through.
        captured: dict[str, Any] = {}

        from backend.agents import nodes

        def _fake_build_system_prompt(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "fake prompt"

        # A fake LLM whose .invoke returns an object with a benign
        # .content + no tool_calls — enough for the node to short-circuit
        # past the LLM branch into "answer = resp.content".
        class _Resp:
            content = "hello"
            tool_calls: list[Any] = []

        class _FakeLLM:
            def invoke(self, _msgs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(nodes, "build_system_prompt", _fake_build_system_prompt)
        monkeypatch.setattr(nodes, "_get_llm", lambda **kwargs: _FakeLLM())
        monkeypatch.setattr(
            nodes,
            "_resolve_skill_loading_mode",
            lambda _x: "eager",
        )

        from backend.agents.state import GraphState

        block = build_clone_spec_context(_make_transformed())
        state = GraphState(
            user_command="scaffold a Next.js project",
            routed_to="general",
            clone_spec_context=block,
        )

        node = nodes._specialist_node_factory("general")

        import asyncio

        asyncio.run(node(state))

        assert "clone_spec_context" in captured
        assert captured["clone_spec_context"] == block


# ─── 11. Package re-exports ──────────────────────────────────────────


W11_10_SYMBOLS = [
    "CLONE_SPEC_CONTEXT_HEADER",
    "CloneSpecContextError",
    "MAX_CLONE_SPEC_CONTEXT_CHARS",
    "MAX_CONTEXT_COLOR_ITEMS",
    "MAX_CONTEXT_FONT_ITEMS",
    "MAX_CONTEXT_IMAGE_ITEMS",
    "MAX_CONTEXT_NAV_ITEMS",
    "MAX_CONTEXT_SECTION_ITEMS",
    "MAX_CONTEXT_SECTION_SUMMARY_CHARS",
    "TRUNCATION_MARKER",
    "W11_INVARIANTS_BLOCK",
    "build_clone_spec_context",
]


@pytest.mark.parametrize("symbol", W11_10_SYMBOLS)
def test_w11_10_symbol_re_exported_via_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__, f"{symbol} missing from backend.web.__all__"
    assert hasattr(web_pkg, symbol), f"{symbol} not attribute of backend.web"


def test_total_re_export_count_pinned_at_192() -> None:
    # W11.9 left __all__ at 169 symbols; W11.10 adds 12 clone_spec_context
    # symbols → 181; W11.12 adds 11 clone_audit symbols → 192;
    # W13.2 adds 7 screenshot-breakpoint symbols → 199; W13.3 adds 18
    # screenshot-writer symbols → 217; W13.4 adds 16 screenshot-ghost-
    # overlay symbols → 233; W15.2 adds 11 vite_error_relay symbols
    # → 244; W15.3 adds 8 vite_error_prompt symbols → 252; W15.4 adds
    # 10 vite_retry_budget symbols → 262; W15.5 adds 13
    # vite_config_injection symbols → 275; W15.6 adds 13 vite_self_fix
    # symbols → 288. If this fails with a different count, audit
    # whether you consciously added / removed a public symbol and
    # update the pin alongside the row's TODO entry.
    assert len(web_pkg.__all__) == 330


# ─── 12. Whole-spec invariants ───────────────────────────────────────


class TestWholeSpecInvariants:

    def test_block_carries_attribution_when_no_manifest(self):
        block = build_clone_spec_context(_make_transformed())
        assert "open-lovable" in block
        assert "MIT" in block

    def test_block_carries_attribution_when_manifest_present(self):
        spec = _make_transformed()
        manifest = _make_manifest(spec)
        block = build_clone_spec_context(spec, manifest=manifest)
        assert "open-lovable" in block
        assert "MIT" in block

    def test_no_data_uri_in_output(self):
        block = build_clone_spec_context(_make_transformed())
        assert "data:" not in block
        assert "base64," not in block

    def test_block_does_not_leak_image_source_url(self):
        spec = _make_transformed(
            images=(
                {
                    "url": "https://placehold.co/800x600",
                    "alt": "hero",
                    "kind": "placeholder",
                    "source_url": "https://acme.example/SECRET-internal-asset.png",
                },
            ),
        )
        block = build_clone_spec_context(spec)
        # Source URL must never reach the agent — provenance only.
        assert "SECRET-internal-asset" not in block

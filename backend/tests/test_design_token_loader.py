"""V1 #3 (issue #317) — design-token loader contract tests.

Pins ``backend/design_token_loader.py`` against:

  * structural invariants of :class:`DesignToken` / :class:`DesignTokens`;
  * CSS parsing correctness across all four scopes
    (``:root`` / ``.dark`` / ``@theme`` / ``html``) plus the ``@keyframes``
    /  ``@layer`` / ``@media`` / nested-brace noise that surrounds them;
  * correct classification of every token kind (colour / font / radius
    / spacing / shadow / other) by name AND by value shape;
  * graceful degradation when project root / CSS / tailwind.config are
    missing, empty or unreadable — the agent MUST get back an empty
    but well-formed :class:`DesignTokens`, never a traceback;
  * JSON-serialisability at the tool boundary
    (:meth:`DesignTokens.to_dict`);
  * determinism of :meth:`DesignTokens.to_agent_context` — same input
    must produce byte-identical output (Anthropic prompt-cache key
    stability depends on it);
  * live-parity: running ``load_design_tokens`` on the checked-in
    OmniSight project extracts the palette / fonts / radii that the UI
    Designer skill (``configs/roles/ui-designer.md``) references
    verbatim in its prompt.

If a future commit renames a token (e.g. drops ``--neural-blue``), adds
a scope we don't understand, or changes the markdown shape of the
agent-context block, these tests fail noisily — the agent prompt then
gets a code-review before drift lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import design_token_loader as dtl
from backend.design_token_loader import (
    DesignToken,
    DesignTokens,
    KINDS,
    LOADER_SCHEMA_VERSION,
    SCOPES,
    load_design_tokens,
    render_agent_context_block,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Module-level invariants ──────────────────────────────────────────


class TestModuleInvariants:
    def test_kinds_is_fixed_tuple(self):
        assert isinstance(KINDS, tuple)
        assert set(KINDS) == {"color", "font", "radius", "spacing", "shadow", "other"}

    def test_scopes_is_fixed_tuple(self):
        assert isinstance(SCOPES, tuple)
        assert set(SCOPES) == {"root", "dark", "theme", "html", "tailwind-config"}

    def test_schema_version_is_semver(self):
        parts = LOADER_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_public_surface_exports(self):
        for name in (
            "KINDS",
            "LOADER_SCHEMA_VERSION",
            "SCOPES",
            "DesignToken",
            "DesignTokens",
            "load_design_tokens",
            "render_agent_context_block",
        ):
            assert name in dtl.__all__, f"{name} must be in __all__"


# ── DesignToken dataclass ────────────────────────────────────────────


class TestDesignToken:
    def test_minimal_valid_construction(self):
        t = DesignToken(name="primary", value="#38bdf8", kind="color", scope="root")
        assert t.name == "primary"
        assert t.value == "#38bdf8"
        assert t.kind == "color"
        assert t.scope == "root"
        assert t.source == ""
        assert t.css_name == "--primary"

    def test_frozen_rejects_mutation(self):
        t = DesignToken(name="a", value="1", kind="other", scope="root")
        with pytest.raises((AttributeError, Exception)):
            t.name = "b"  # type: ignore[misc]

    @pytest.mark.parametrize("bad_kind", ["colour", "", "COLOR", None])
    def test_unknown_kind_rejected(self, bad_kind):
        with pytest.raises((ValueError, TypeError)):
            DesignToken(name="x", value="1", kind=bad_kind, scope="root")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_scope", ["body", "", "ROOT", None])
    def test_unknown_scope_rejected(self, bad_scope):
        with pytest.raises((ValueError, TypeError)):
            DesignToken(name="x", value="1", kind="other", scope=bad_scope)  # type: ignore[arg-type]

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            DesignToken(name="", value="1", kind="other", scope="root")


# ── DesignTokens container ───────────────────────────────────────────


class TestDesignTokensEmpty:
    def test_empty_default(self):
        d = DesignTokens()
        assert d.all_tokens == ()
        assert d.sources == ()
        assert d.has_dark is False
        assert d.is_dark_only is False
        assert dict(d.palette) == {}
        assert dict(d.palette_dark) == {}
        assert dict(d.fonts) == {}
        assert dict(d.radii) == {}
        assert dict(d.spacing) == {}
        assert dict(d.shadows) == {}
        assert dict(d.brand) == {}
        assert d.utility_classes() == ()
        assert d.token_names() == ()

    def test_empty_agent_context_is_well_formed(self):
        d = DesignTokens()
        out = d.to_agent_context()
        assert out.startswith(f"# Design tokens (v{LOADER_SCHEMA_VERSION})")
        assert "No design tokens extracted" in out
        assert out.endswith("\n")

    def test_empty_to_dict_is_json_safe(self):
        d = DesignTokens()
        dump = json.dumps(d.to_dict())
        parsed = json.loads(dump)
        assert parsed["tokens"] == []
        assert parsed["schema_version"] == LOADER_SCHEMA_VERSION


class TestFilterValidation:
    def _build(self):
        return DesignTokens(
            all_tokens=(
                DesignToken("primary", "#111", "color", "root"),
                DesignToken("font-sans", "Inter", "font", "theme"),
            )
        )

    def test_filter_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            self._build().filter_tokens(kind="colour")

    def test_filter_rejects_unknown_scope(self):
        with pytest.raises(ValueError):
            self._build().filter_tokens(scope="body")

    def test_filter_kind_and_scope(self):
        d = self._build()
        assert len(d.filter_tokens(kind="color")) == 1
        assert len(d.filter_tokens(scope="theme")) == 1
        assert len(d.filter_tokens(kind="font", scope="theme")) == 1
        assert len(d.filter_tokens(kind="font", scope="root")) == 0


# ── CSS parser coverage ──────────────────────────────────────────────


_TAILWIND_V4_CSS = """\
@import 'tailwindcss';
@custom-variant dark (&:is(.dark *));

html {
  color-scheme: dark;
  background: #010409;
}

:root {
  --background: #010409;
  --foreground: #e2e8f0;
  --primary: #38bdf8;
  --primary-foreground: #010409;
  --destructive: oklch(0.577 0.245 27.325);
  --border: rgba(56, 189, 248, 0.35);
  --radius: 0.5rem;
  --neural-blue: #38bdf8;
  --chart-1: #38bdf8;
}

.dark {
  --background: #000000;
  --foreground: #ffffff;
}

@theme inline {
  --font-sans: Inter, sans-serif;
  --font-mono: Fira, monospace;
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --color-primary: var(--primary);
  --radius-sm: calc(var(--radius) - 4px);
  --radius-lg: var(--radius);
}

@keyframes pulse {
  0% { opacity: 1; }
  50% { opacity: 0.5; }
}

@layer base {
  * { @apply border-border; }
}

.custom-animation {
  animation: pulse 2s;
}
"""


class TestCSSParser:
    @pytest.fixture
    def css_project(self, tmp_path: Path) -> Path:
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(_TAILWIND_V4_CSS)
        return tmp_path

    def test_finds_the_globals_file(self, css_project):
        d = load_design_tokens(css_project)
        assert d.sources == ("app/globals.css",)

    def test_detects_dark_only_theme(self, css_project):
        d = load_design_tokens(css_project)
        assert d.is_dark_only is True

    def test_has_dark_tokens_flag(self, css_project):
        d = load_design_tokens(css_project)
        assert d.has_dark is True

    def test_root_palette_extracted(self, css_project):
        d = load_design_tokens(css_project)
        assert d.palette["background"] == "#010409"
        assert d.palette["primary"] == "#38bdf8"
        assert d.palette["destructive"].startswith("oklch(")
        assert d.palette["border"].startswith("rgba(")

    def test_dark_palette_extracted(self, css_project):
        d = load_design_tokens(css_project)
        assert d.palette_dark["background"] == "#000000"
        assert d.palette_dark["foreground"] == "#ffffff"
        # dark overrides only live under `.dark` — not leaked to :root palette
        assert d.palette["background"] == "#010409"

    def test_fonts_extracted(self, css_project):
        d = load_design_tokens(css_project)
        assert d.fonts["font-sans"] == "Inter, sans-serif"
        assert d.fonts["font-mono"] == "Fira, monospace"

    def test_radii_extracted(self, css_project):
        d = load_design_tokens(css_project)
        assert d.radii["radius"] == "0.5rem"
        assert d.radii["radius-sm"].startswith("calc(")

    def test_brand_excludes_shadcn_semantics(self, css_project):
        d = load_design_tokens(css_project)
        assert "neural-blue" in d.brand
        # shadcn-official tokens must NOT leak into brand
        assert "background" not in d.brand
        assert "primary" not in d.brand
        assert "chart-1" not in d.brand

    def test_utility_classes_generated_from_theme_only(self, css_project):
        d = load_design_tokens(css_project)
        u = set(d.utility_classes())
        # from --color-* we get bg/text/border triples
        assert {"bg-background", "text-background", "border-background"} <= u
        assert {"bg-primary", "text-primary", "border-primary"} <= u
        # from --radius-*
        assert "rounded-sm" in u
        assert "rounded-lg" in u
        # from --font-*
        assert "font-sans" in u
        assert "font-mono" in u
        # :root-only tokens MUST NOT become utilities (they're not in @theme)
        assert "bg-neural-blue" not in u

    def test_keyframes_and_layer_noise_ignored(self, css_project):
        d = load_design_tokens(css_project)
        names = {t.name for t in d.all_tokens}
        # 0% / 50% keyframe selectors must not produce tokens
        assert "0%" not in names
        assert "50%" not in names

    def test_every_token_has_valid_kind_and_scope(self, css_project):
        d = load_design_tokens(css_project)
        for t in d.all_tokens:
            assert t.kind in KINDS
            assert t.scope in SCOPES


class TestKindClassification:
    """Pin the name-prefix + value-shape classifier so a future edit
    to the regex/prefix set can't silently reclassify tokens.
    """

    @pytest.mark.parametrize(
        "name, value, expected_kind",
        [
            # Name-prefix route
            ("primary", "#fff", "color"),
            ("primary-foreground", "#000", "color"),
            ("background", "oklch(0.1 0 0)", "color"),
            ("foreground", "rgb(0,0,0)", "color"),
            ("chart-1", "#abc", "color"),
            ("sidebar-border", "rgba(0,0,0,0.1)", "color"),
            ("color-primary", "var(--primary)", "color"),
            ("neural-blue", "#38bdf8", "color"),
            ("hardware-orange-dim", "rgba(249,115,22,0.3)", "color"),
            ("font-sans", "Inter", "font"),
            ("font-mono", "Fira", "font"),
            ("radius", "0.5rem", "radius"),
            ("radius-sm", "calc(var(--radius) - 4px)", "radius"),
            ("spacing", "0.25rem", "spacing"),
            ("spacing-4", "1rem", "spacing"),
            ("shadow", "0 1px 3px rgba(0,0,0,.1)", "shadow"),
            ("shadow-md", "0 4px 6px rgba(0,0,0,.1)", "shadow"),
            # Value-shape fallback (name NOT in prefix set)
            ("weird-color", "#abcdef", "color"),
            ("weird-color-rgb", "rgb(1,2,3)", "color"),
            ("weird-color-oklch", "oklch(0.5 0 0)", "color"),
            # Truly "other"
            ("ease", "cubic-bezier(0.4, 0, 0.2, 1)", "other"),
            ("duration", "200ms", "other"),
        ],
    )
    def test_classify_cases(self, name, value, expected_kind):
        t = DesignToken(
            name=name,
            value=value,
            kind=dtl._classify_kind(name, value),
            scope="root",
        )
        assert t.kind == expected_kind, (name, value)


class TestMalformedCSSGracefulFallback:
    """No matter how ugly the input, we never crash the agent prompt."""

    def _load(self, tmp_path: Path, css: str) -> DesignTokens:
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(css)
        return load_design_tokens(tmp_path)

    def test_empty_file(self, tmp_path):
        d = self._load(tmp_path, "")
        assert d.sources == ("app/globals.css",)
        assert d.all_tokens == ()

    def test_comments_only(self, tmp_path):
        d = self._load(tmp_path, "/* just a comment */\n/* another */\n")
        assert d.all_tokens == ()

    def test_unclosed_brace(self, tmp_path):
        # Intentionally missing closing brace on :root {
        d = self._load(tmp_path, ":root { --primary: #fff; ")
        # Parser is tolerant — it may or may not pick up the token,
        # but MUST NOT raise.
        assert isinstance(d, DesignTokens)

    def test_custom_variant_statement_not_mistaken_for_selector(self, tmp_path):
        css = (
            "@custom-variant dark (&:is(.dark *));\n"
            ":root { --primary: #aaa; }\n"
        )
        d = self._load(tmp_path, css)
        assert d.palette.get("primary") == "#aaa"

    def test_nested_media_does_not_break_scope_detection(self, tmp_path):
        css = (
            ":root { --a: 1rem; }\n"
            "@media (prefers-reduced-motion: reduce) { * { animation: none; } }\n"
            "@theme inline { --color-primary: var(--primary); }\n"
        )
        d = self._load(tmp_path, css)
        assert d.filter_tokens(scope="root")
        assert d.filter_tokens(scope="theme")

    def test_non_utf8_file_survives(self, tmp_path):
        (tmp_path / "app").mkdir()
        # Write bytes that aren't valid UTF-8.
        (tmp_path / "app" / "globals.css").write_bytes(b":root { --x: \xff\xfe; }")
        d = load_design_tokens(tmp_path)
        # The read fails → file is silently skipped, empty tokens.
        assert isinstance(d, DesignTokens)


# ── load_design_tokens() entry-point ─────────────────────────────────


class TestLoadDesignTokensEntrypoint:
    def test_none_project_root(self):
        d = load_design_tokens(None)
        assert d.all_tokens == ()
        assert d.sources == ()

    def test_missing_directory(self, tmp_path):
        d = load_design_tokens(tmp_path / "does-not-exist")
        assert d.all_tokens == ()

    def test_project_without_globals(self, tmp_path):
        (tmp_path / "not-a-css").write_text("hi")
        d = load_design_tokens(tmp_path)
        assert d.all_tokens == ()

    def test_string_path_accepted(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(":root { --primary: #fff; }")
        d = load_design_tokens(str(tmp_path))
        assert d.palette["primary"] == "#fff"

    def test_components_json_pointer_wins(self, tmp_path):
        (tmp_path / "custom").mkdir()
        (tmp_path / "custom" / "tw.css").write_text(":root { --primary: #123; }")
        (tmp_path / "components.json").write_text(
            json.dumps({"tailwind": {"css": "custom/tw.css"}})
        )
        d = load_design_tokens(tmp_path)
        assert d.palette["primary"] == "#123"
        assert "custom/tw.css" in d.sources[0]

    def test_styles_fallback_when_no_app_dir(self, tmp_path):
        (tmp_path / "styles").mkdir()
        (tmp_path / "styles" / "globals.css").write_text(":root { --primary: #abc; }")
        d = load_design_tokens(tmp_path)
        assert d.sources == ("styles/globals.css",)
        assert d.palette["primary"] == "#abc"


# ── Tailwind v3 config parser ────────────────────────────────────────


_TAILWIND_V3_CONFIG = """\
import type { Config } from "tailwindcss";
// generated config
export default {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: "hsl(var(--primary))",
        "brand-red": "#ef4444",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif"],
        mono: ["'Fira Code'", "monospace"],
      },
      borderRadius: {
        DEFAULT: "0.5rem",
        lg: "1rem",
      },
      spacing: {
        gutter: "1.5rem",
      },
    },
  },
} satisfies Config;
"""


class TestTailwindV3Config:
    def test_parses_colors_fonts_radii_spacing(self, tmp_path):
        (tmp_path / "tailwind.config.ts").write_text(_TAILWIND_V3_CONFIG)
        d = load_design_tokens(tmp_path)
        names = {t.name: t for t in d.all_tokens if t.scope == "tailwind-config"}
        assert "primary" in names and names["primary"].kind == "color"
        assert "brand-red" in names and names["brand-red"].value == "#ef4444"
        assert "sans" in names and names["sans"].kind == "font"
        assert "DEFAULT" in names and names["DEFAULT"].kind == "radius"
        assert "gutter" in names and names["gutter"].kind == "spacing"

    def test_array_font_stack_joined(self, tmp_path):
        (tmp_path / "tailwind.config.ts").write_text(_TAILWIND_V3_CONFIG)
        d = load_design_tokens(tmp_path)
        sans = next(
            t for t in d.all_tokens if t.scope == "tailwind-config" and t.name == "sans"
        )
        assert "Inter" in sans.value
        assert "ui-sans-serif" in sans.value


# ── Serialisation + agent-context determinism ────────────────────────


class TestAgentContextDeterminism:
    def _project_with_tokens(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(_TAILWIND_V4_CSS)
        return tmp_path

    def test_agent_context_is_byte_identical_across_calls(self, tmp_path):
        root = self._project_with_tokens(tmp_path)
        a = render_agent_context_block(root)
        b = render_agent_context_block(root)
        assert a == b, "render_agent_context_block must be deterministic"

    def test_agent_context_starts_with_version_header(self, tmp_path):
        root = self._project_with_tokens(tmp_path)
        out = render_agent_context_block(root)
        assert out.startswith(f"# Design tokens (v{LOADER_SCHEMA_VERSION})")
        assert out.endswith("\n")

    def test_agent_context_contains_core_sections(self, tmp_path):
        root = self._project_with_tokens(tmp_path)
        out = render_agent_context_block(root)
        assert "## Palette (base)" in out
        assert "## Palette (dark overrides)" in out
        assert "## Fonts" in out
        assert "## Radii" in out
        assert "## Generation rules" in out
        assert "Tailwind utility classes" in out

    def test_agent_context_warns_dark_only(self, tmp_path):
        root = self._project_with_tokens(tmp_path)
        out = render_agent_context_block(root)
        assert "dark-only" in out
        assert "Do NOT assume a light theme" in out

    def test_palette_entries_sorted_alphabetically(self, tmp_path):
        root = self._project_with_tokens(tmp_path)
        out = render_agent_context_block(root)
        palette_block = out.split("## Palette (base)")[1].split("## ")[0]
        entries = [
            line.split("`")[1]
            for line in palette_block.splitlines()
            if line.startswith("- `--")
        ]
        assert entries == sorted(entries), "palette entries must be sorted"


class TestJSONSerialisation:
    def test_to_dict_roundtrips_through_json(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(_TAILWIND_V4_CSS)
        d = load_design_tokens(tmp_path)
        dump = json.dumps(d.to_dict())
        parsed = json.loads(dump)
        assert parsed["schema_version"] == LOADER_SCHEMA_VERSION
        assert parsed["is_dark_only"] is True
        assert parsed["has_dark"] is True
        assert isinstance(parsed["tokens"], list)
        assert isinstance(parsed["palette"], dict)
        assert isinstance(parsed["utility_classes"], list)
        # No stray dataclass instance leaked.
        for t in parsed["tokens"]:
            assert set(t.keys()) >= {"name", "value", "kind", "scope"}

    def test_views_are_read_only(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "globals.css").write_text(_TAILWIND_V4_CSS)
        d = load_design_tokens(tmp_path)
        with pytest.raises(TypeError):
            d.palette["primary"] = "mutated"  # type: ignore[index]


# ── Live parity with the OmniSight project globals.css ───────────────


class TestLiveProjectParity:
    """The UI Designer skill cites specific tokens by name; if the
    project-level globals.css drops them, those skill lines become
    a lie. Fail loud here rather than ship a broken agent prompt.
    """

    @pytest.fixture(scope="class")
    def live(self) -> DesignTokens:
        return load_design_tokens(PROJECT_ROOT)

    def test_has_sources(self, live):
        assert live.sources, "live project must expose a globals.css"

    def test_project_is_dark_only(self, live):
        assert live.is_dark_only, (
            "ui-designer.md states the project is dark-only; "
            "globals.css must keep `html { color-scheme: dark }`"
        )

    @pytest.mark.parametrize(
        "token_name",
        [
            "background",
            "foreground",
            "primary",
            "primary-foreground",
            "destructive",
            "destructive-foreground",
            "border",
            "card",
            "muted-foreground",
            "ring",
        ],
    )
    def test_shadcn_semantic_palette_present(self, live, token_name):
        assert token_name in live.palette, (
            f"shadcn semantic token --{token_name} missing from "
            f"app/globals.css; UI Designer skill cites it."
        )

    @pytest.mark.parametrize(
        "token_name",
        [
            "neural-blue",
            "hardware-orange",
            "artifact-purple",
            "validation-emerald",
            "critical-red",
        ],
    )
    def test_fui_brand_palette_present(self, live, token_name):
        assert token_name in live.brand, (
            f"FUI brand token --{token_name} missing; the UI Designer "
            f"skill uses it for the 5-agent colour coding."
        )

    def test_chart_palette_present(self, live):
        for i in range(1, 6):
            assert f"chart-{i}" in live.palette

    def test_fonts_present(self, live):
        assert "font-sans" in live.fonts
        assert "font-mono" in live.fonts

    def test_radius_scale_present(self, live):
        r = live.radii
        assert "radius" in r
        assert "radius-sm" in r
        assert "radius-lg" in r

    def test_utility_classes_cover_core_semantics(self, live):
        u = set(live.utility_classes())
        assert "bg-background" in u
        assert "text-foreground" in u
        assert "bg-primary" in u
        assert "text-primary-foreground" in u
        assert "border-border" in u
        assert "rounded-lg" in u

    def test_agent_context_block_non_empty(self, live):
        block = live.to_agent_context()
        assert "## Palette (base)" in block
        assert "## Brand colours" in block

"""V1 #6 (issue #317) — figma_to_ui pipeline contract tests.

Pins ``backend/figma_to_ui.py`` against:

  * structural invariants of :class:`FigmaToken`,
    :class:`FigmaDesignContext`, :class:`FigmaExtraction`,
    :class:`FigmaGenerationResult` (frozen, validated, JSON-safe);
  * node-id / file-key normalisation (canonicalises both the
    ``123-456`` url form and the ``123:456`` API form);
  * :func:`from_mcp_response` tolerance for raw dict, stringified
    JSON, and MCP ``{"content":[{type:"text", text:"…"}]}`` envelopes,
    including screenshot base64 / data-URL unwrap;
  * extraction heuristics — colours / spacing / radii / shadows /
    typography / component hierarchy / imports — on a synthetic Figma
    reference code payload and a minimal absolute-positioned slab
    (the kind Figma emits by default);
  * deterministic prompt construction (byte-identical across calls);
  * the full :func:`generate_ui_from_figma` pipeline with injected
    fake invokers: successful generation, LLM-unavailable fallback,
    TSX-missing fallback, auto-fix round, and graceful bubbling of
    MCP-parse warnings;
  * the agent-callable :func:`run_figma_to_ui` entry point with both
    ``mcp_response=`` and ``context=`` modes;
  * sibling module integration — the prompt really references the
    live shadcn registry + design-token block, and the auto-fix
    round really rewrites raw ``<button>`` into ``<Button>``.

If sibling modules rename a public export, one of the cross-module
tests will fail noisily — that's intentional; the agent tool surface
is a contract, not an implementation detail.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import figma_to_ui as ftu
from backend.figma_to_ui import (
    DEFAULT_FIGMA_MODEL,
    DEFAULT_FIGMA_PROVIDER,
    FIGMA_SCHEMA_VERSION,
    TOKEN_KINDS,
    FigmaDesignContext,
    FigmaExtraction,
    FigmaGenerationResult,
    FigmaToken,
    build_figma_generation_prompt,
    build_multimodal_message,
    canonical_figma_source,
    extract_from_context,
    from_mcp_response,
    generate_ui_from_figma,
    normalize_file_key,
    normalize_node_id,
    run_figma_to_ui,
    validate_figma_context,
)
from backend.component_consistency_linter import LintReport
from backend.vision_to_ui import VisionImage, validate_image


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Shared fixtures ──────────────────────────────────────────────────


# Smallest legal PNG (1×1 transparent pixel).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_1X1_B64 = base64.b64encode(_PNG_1X1).decode("ascii")


# A realistic, if stylised, Figma MCP reference-code payload.
_FIGMA_REFERENCE_CODE = """\
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function HeroSection() {
  return (
    <div
      className="flex flex-col gap-6 bg-[#0b1220] p-[32px] rounded-[16px]"
      style={{ boxShadow: "0 4px 24px rgba(0, 0, 0, 0.35)" }}
    >
      <Card className="bg-card text-card-foreground rounded-lg">
        <CardHeader>
          <CardTitle className="text-2xl font-semibold">
            Pricing
          </CardTitle>
        </CardHeader>
        <CardContent className="gap-4 p-6">
          <p style={{ color: "#94a3b8", fontSize: "14px" }}>
            Pick a plan that scales with your team.
          </p>
          <Button variant="default" className="bg-primary text-primary-foreground rounded-md">
            Upgrade
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
"""


_FIGMA_MCP_RESPONSE = {
    "code": _FIGMA_REFERENCE_CODE,
    "screenshot": _PNG_1X1_B64,
    "screenshot_mime": "image/png",
    "variables": {
        "color/primary": "#38bdf8",
        "color/background": {"r": 0.043, "g": 0.070, "b": 0.118},  # ~#0b1220
        "radius/md": "8px",
        "spacing/4": "16px",
        "font/sans": "Inter, sans-serif",
    },
    "metadata": {
        "frame_name": "Hero / Pricing",
        "width": 1440,
        "height": 900,
    },
    "asset_urls": {
        "hero_bg.png": "https://example.com/a.png",
    },
}


_CLEAN_TSX_RESPONSE = (
    "Sure — here's the rebuilt component:\n\n"
    "```tsx\n"
    "import { Button } from \"@/components/ui/button\";\n"
    "\n"
    "export default function HeroSection() {\n"
    "  return (\n"
    "    <div className=\"flex flex-col gap-6 bg-background text-foreground p-6\">\n"
    "      <Button variant=\"default\">Upgrade</Button>\n"
    "    </div>\n"
    "  );\n"
    "}\n"
    "```\n"
)


# ── Module invariants ────────────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_is_semver(self):
        parts = FIGMA_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_default_model_is_opus_4_7(self):
        assert DEFAULT_FIGMA_MODEL.startswith("claude-opus-4-7")
        assert DEFAULT_FIGMA_PROVIDER == "anthropic"

    def test_token_kinds_fixed(self):
        assert TOKEN_KINDS == (
            "color", "spacing", "radius", "font", "shadow", "other",
        )

    @pytest.mark.parametrize("name", [
        "FIGMA_SCHEMA_VERSION",
        "DEFAULT_FIGMA_MODEL",
        "DEFAULT_FIGMA_PROVIDER",
        "TOKEN_KINDS",
        "FigmaToken",
        "FigmaDesignContext",
        "FigmaExtraction",
        "FigmaGenerationResult",
        "normalize_node_id",
        "normalize_file_key",
        "canonical_figma_source",
        "from_mcp_response",
        "validate_figma_context",
        "extract_from_context",
        "build_figma_generation_prompt",
        "build_multimodal_message",
        "generate_ui_from_figma",
        "run_figma_to_ui",
    ])
    def test_public_surface_exports(self, name):
        assert name in ftu.__all__, f"{name} must be in __all__"


# ── Node-id / file-key normalisation ────────────────────────────────


class TestNormaliseNodeId:
    @pytest.mark.parametrize("raw,expected", [
        ("1:2", "1:2"),
        ("1-2", "1:2"),
        ("123:456", "123:456"),
        ("123-456", "123:456"),
        ("-5:6", "-5:6"),
        ("-5-6", "-5:6"),  # leading '-' preserved, split on second '-'
        ("  1:2  ", "1:2"),
    ])
    def test_accepts_canonical_forms(self, raw, expected):
        assert normalize_node_id(raw) == expected

    @pytest.mark.parametrize("raw", [
        "",
        "   ",
        "1",
        "1:",
        ":2",
        "abc",
        "1/2",
        "1:2:3",
    ])
    def test_rejects_invalid_forms(self, raw):
        with pytest.raises(ValueError):
            normalize_node_id(raw)

    def test_rejects_none(self):
        with pytest.raises(ValueError):
            normalize_node_id(None)  # type: ignore[arg-type]


class TestNormaliseFileKey:
    def test_accepts_opaque_token(self):
        assert normalize_file_key("ABC123") == "ABC123"
        assert normalize_file_key("  zxY_987  ") == "zxY_987"

    @pytest.mark.parametrize("raw", [
        "",
        "   ",
        "ABC/DEF",  # looks like a URL path
        "with space",
    ])
    def test_rejects_malformed(self, raw):
        with pytest.raises(ValueError):
            normalize_file_key(raw)


class TestCanonicalSource:
    def test_emits_url_shape_with_dash(self):
        assert (
            canonical_figma_source("XYZ", "1:2")
            == "figma.com/design/XYZ?node-id=1-2"
        )


# ── FigmaToken ───────────────────────────────────────────────────────


class TestFigmaToken:
    def test_frozen(self):
        t = FigmaToken(name="primary", value="#38bdf8", kind="color")
        with pytest.raises(Exception):
            t.kind = "font"  # type: ignore[misc]

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            FigmaToken(name="primary", value="#abc", kind="not-a-kind")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            FigmaToken(name="", value="#abc", kind="color")

    def test_rejects_none_value(self):
        with pytest.raises(ValueError):
            FigmaToken(name="x", value=None, kind="color")  # type: ignore[arg-type]


# ── FigmaDesignContext ──────────────────────────────────────────────


class TestFigmaDesignContext:
    def test_validate_happy_path(self):
        ctx = validate_figma_context(
            file_key="ABC",
            node_id="1:2",
            code="<div/>",
            variables={"color/primary": "#abc"},
            metadata={"frame": "hero"},
            asset_urls={"a.png": "https://x/a.png"},
        )
        assert ctx.file_key == "ABC"
        assert ctx.node_id == "1:2"
        assert ctx.source == "figma.com/design/ABC?node-id=1-2"
        assert ctx.code == "<div/>"
        assert not ctx.has_screenshot

    def test_rejects_empty_file_key(self):
        with pytest.raises(ValueError):
            validate_figma_context(file_key="", node_id="1:2")

    def test_rejects_invalid_node_id(self):
        with pytest.raises(ValueError):
            validate_figma_context(file_key="ABC", node_id="xyz")

    def test_inner_mappings_are_readonly(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2",
            variables={"x": 1},
        )
        # Direct mutation forbidden by MappingProxyType.
        with pytest.raises(TypeError):
            ctx.variables["x"] = 2  # type: ignore[index]

    def test_normalises_node_id_dash_form(self):
        ctx = validate_figma_context(file_key="ABC", node_id="123-456")
        assert ctx.node_id == "123:456"

    def test_to_dict_is_json_safe(self):
        img = validate_image(_PNG_1X1, "image/png")
        ctx = FigmaDesignContext(
            file_key="ABC", node_id="1:2",
            code="<div/>",
            screenshot=img,
            source="figma.com/design/ABC?node-id=1-2",
        )
        d = ctx.to_dict()
        json.dumps(d)  # must not raise
        assert d["has_screenshot"] is True
        assert d["screenshot_mime"] == "image/png"
        assert d["screenshot_bytes"] == len(_PNG_1X1)
        assert d["schema_version"] == FIGMA_SCHEMA_VERSION

    def test_rejects_wrong_screenshot_type(self):
        with pytest.raises(TypeError):
            FigmaDesignContext(
                file_key="ABC", node_id="1:2", code="",
                screenshot="not an image",  # type: ignore[arg-type]
            )


# ── from_mcp_response ────────────────────────────────────────────────


class TestFromMcpResponse:
    def test_happy_path_dict(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1-2",
        )
        assert ctx.file_key == "ABC"
        assert ctx.node_id == "1:2"
        assert "HeroSection" in ctx.code
        assert ctx.has_screenshot  # base64 decoded into a VisionImage
        assert "color/primary" in ctx.variables
        assert ctx.asset_urls == {"hero_bg.png": "https://example.com/a.png"}

    def test_string_json_payload(self):
        ctx = from_mcp_response(
            json.dumps(_FIGMA_MCP_RESPONSE),
            file_key="ABC", node_id="1:2",
        )
        assert "HeroSection" in ctx.code

    def test_content_wrapper_envelope(self):
        envelope = {
            "content": [
                {"type": "text", "text": json.dumps(_FIGMA_MCP_RESPONSE)},
            ],
        }
        ctx = from_mcp_response(envelope, file_key="ABC", node_id="1:2")
        assert "HeroSection" in ctx.code

    def test_none_response_yields_warnings(self):
        ctx = from_mcp_response(None, file_key="ABC", node_id="1:2")
        assert ctx.code == ""
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "mcp_response_missing" in warnings
        assert "figma_context_empty" in warnings

    def test_non_json_string_yields_warning(self):
        ctx = from_mcp_response(
            "lolwut no json here", file_key="ABC", node_id="1:2",
        )
        assert ctx.code == ""
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "mcp_response_not_json" in warnings

    def test_non_object_payload_yields_warning(self):
        ctx = from_mcp_response(
            [1, 2, 3], file_key="ABC", node_id="1:2",
        )
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "mcp_response_not_object" in warnings

    def test_data_url_screenshot(self):
        data_url = f"data:image/png;base64,{_PNG_1X1_B64}"
        response = {"code": "<div/>", "screenshot": data_url}
        ctx = from_mcp_response(
            response, file_key="ABC", node_id="1:2",
        )
        assert ctx.has_screenshot
        assert ctx.screenshot and ctx.screenshot.mime_type == "image/png"

    def test_bytes_screenshot(self):
        response = {"code": "<div/>", "screenshot": _PNG_1X1}
        ctx = from_mcp_response(
            response, file_key="ABC", node_id="1:2",
        )
        assert ctx.has_screenshot

    def test_bad_base64_screenshot_warns_but_returns(self):
        response = {"code": "<div/>", "screenshot": "@@not b64@@"}
        ctx = from_mcp_response(
            response, file_key="ABC", node_id="1:2",
        )
        assert not ctx.has_screenshot
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "screenshot_invalid" in warnings

    def test_canonical_source_attached(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        assert ctx.source == "figma.com/design/ABC?node-id=1-2"

    def test_normalises_node_id_forms(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1-2",
        )
        assert ctx.node_id == "1:2"

    def test_variables_are_mapping_proxy(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        with pytest.raises(TypeError):
            ctx.variables["color/primary"] = "#fff"  # type: ignore[index]

    def test_empty_payload_marked_empty(self):
        ctx = from_mcp_response(
            {"code": "", "screenshot": ""}, file_key="ABC", node_id="1:2",
        )
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "figma_context_empty" in warnings


# ── extract_from_context ─────────────────────────────────────────────


class TestExtractFromContext:
    def _ctx(self, code: str = _FIGMA_REFERENCE_CODE) -> FigmaDesignContext:
        return validate_figma_context(
            file_key="ABC", node_id="1:2",
            code=code,
            variables={
                "color/primary": "#38bdf8",
                "radius/md": "8px",
                "font/sans": "Inter, sans-serif",
            },
        )

    def test_detects_hex_colours(self):
        extraction = extract_from_context(self._ctx())
        assert "#0b1220" in extraction.color_values
        assert "#94a3b8" in extraction.color_values

    def test_detects_rgba(self):
        extraction = extract_from_context(self._ctx())
        assert any("rgba(" in v for v in extraction.color_values)

    def test_detects_spacing_px(self):
        extraction = extract_from_context(self._ctx())
        # The 32px and 14px inline values should show up.
        assert "32px" in extraction.spacing_values
        assert "14px" in extraction.spacing_values

    def test_detects_scale_spacing_utilities(self):
        extraction = extract_from_context(self._ctx())
        # gap-6, p-6, gap-4 are in the reference code — normalised
        # into `scale:gap-6`, `scale:p-6`, `scale:gap-4`.
        joined = "|".join(extraction.spacing_values)
        assert "scale:gap-6" in joined
        assert "scale:p-6" in joined

    def test_detects_radii(self):
        extraction = extract_from_context(self._ctx())
        assert any("rounded-lg" in r for r in extraction.radii)
        assert any("rounded-md" in r for r in extraction.radii)
        # The arbitrary rounded-[16px] should make it in too.
        assert any("rounded-[16px]" in r for r in extraction.radii)

    def test_detects_shadows(self):
        extraction = extract_from_context(self._ctx())
        # The inline boxShadow should land in shadows.
        joined = "|".join(extraction.shadows)
        assert "rgba(0, 0, 0, 0.35)" in joined or "rgba(0,0,0,0.35)" in joined

    def test_detects_typography(self):
        extraction = extract_from_context(self._ctx())
        assert "text-2xl" in extraction.typography
        assert "font-semibold" in extraction.typography
        # fontSize: "14px" lands as size:14px via the CSS regex.
        # (fontSize is JSX inline — not `font-size:`, so we only catch
        # the Tailwind utilities here; still, the `size:` prefix proves
        # the CSS regex works when it fires.)

    def test_detects_components_and_imports(self):
        extraction = extract_from_context(self._ctx())
        assert "Button" in extraction.component_hierarchy
        assert "Card" in extraction.component_hierarchy
        assert "@/components/ui/button" in extraction.imported_components
        assert "@/components/ui/card" in extraction.imported_components

    def test_design_tokens_include_variable_and_observed(self):
        extraction = extract_from_context(self._ctx())
        kinds = {t.kind for t in extraction.design_tokens}
        names = {t.name for t in extraction.design_tokens}
        assert "color" in kinds
        assert "color/primary" in names  # from the MCP variables map
        # Observed synthetic tokens prefixed with "observed-".
        assert any(n.startswith("observed-color-") for n in names)

    def test_empty_code_returns_empty_flagged(self):
        ctx = validate_figma_context(file_key="ABC", node_id="1:2", code="")
        extraction = extract_from_context(ctx)
        assert extraction.parse_succeeded is False
        assert "empty_code" in extraction.warnings

    def test_extraction_is_deterministic(self):
        a = extract_from_context(self._ctx()).to_dict()
        b = extract_from_context(self._ctx()).to_dict()
        assert a == b

    def test_extraction_to_dict_json_safe(self):
        extraction = extract_from_context(self._ctx())
        json.dumps(extraction.to_dict())

    def test_absolute_position_code_still_parseable(self):
        abs_code = """
        <div style={{position:"absolute", top:"24px", left:"32px", width:"1440px"}}>
          <span style={{color:"#ffffff", fontSize:"18px"}}>Slab</span>
        </div>
        """
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code=abs_code,
        )
        extraction = extract_from_context(ctx)
        assert "#ffffff" in extraction.color_values
        assert "24px" in extraction.spacing_values
        assert "1440px" in extraction.spacing_values
        # We don't gate on the tag (`div` is lowercase so no hierarchy);
        # what matters is the pipeline doesn't crash on the shape.

    def test_variable_rgb_dict_normalises_to_hex(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
            variables={"color/primary": {"r": 0.22, "g": 0.74, "b": 0.97}},
        )
        extraction = extract_from_context(ctx)
        primary = next(
            t for t in extraction.design_tokens
            if t.name == "color/primary"
        )
        # r*255 ≈ 56, g*255 ≈ 189, b*255 ≈ 247 → #38bdf7
        assert primary.value.startswith("#")
        assert primary.kind == "color"

    def test_css_var_references_surfaced(self):
        code = 'export default () => <div style={{ color: "var(--primary)" }} />'
        ctx = validate_figma_context(file_key="ABC", node_id="1:2", code=code)
        extraction = extract_from_context(ctx)
        assert "var(--primary)" in extraction.color_values


# ── Prompt construction ──────────────────────────────────────────────


class TestGenerationPromptDeterminism:
    def _ctx(self) -> FigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_same_inputs_byte_identical(self):
        ctx = self._ctx()
        a = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="pricing page",
        )
        b = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="pricing page",
        )
        assert a == b

    def test_brief_changes_prompt(self):
        ctx = self._ctx()
        a = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="a",
        )
        b = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="b",
        )
        assert a != b

    def test_prompt_contains_rules_and_figma_header(self):
        prompt = build_figma_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma MCP" in prompt
        assert "Generation rules" in prompt
        assert "```tsx" in prompt
        assert "dark-only" in prompt.lower()
        assert "shadcn" in prompt.lower()

    def test_prompt_contains_figma_source_and_extraction(self):
        prompt = build_figma_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma source" in prompt
        assert "Figma extraction" in prompt
        assert "file_key" in prompt
        assert "node_id" in prompt

    def test_prompt_injects_registry_and_tokens(self):
        prompt = build_figma_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "button" in prompt.lower()  # from registry block
        assert "primary" in prompt.lower()  # from tokens block

    def test_prompt_contains_reference_code_fence(self):
        prompt = build_figma_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma reference code" in prompt
        assert "HeroSection" in prompt

    def test_prompt_reference_code_is_truncated(self):
        huge = "<div>" + ("// " + "a" * 100 + "\n") * 200 + "</div>"
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code=huge,
        )
        prompt = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "truncated" in prompt

    def test_prompt_handles_missing_code(self):
        ctx = validate_figma_context(file_key="ABC", node_id="1:2", code="")
        prompt = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "no reference code" in prompt.lower()

    def test_prompt_shows_screenshot_flag(self):
        ctx = self._ctx()
        prompt = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "attached (multimodal)" in prompt

    def test_prompt_without_screenshot(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        prompt = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "not attached" in prompt

    def test_empty_brief_is_rendered(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        prompt = build_figma_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "(none)" in prompt


# ── Multimodal message ───────────────────────────────────────────────


class TestMultimodalMessage:
    def test_without_screenshot_returns_text_message(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        msg = build_multimodal_message(ctx, "hello")
        assert msg.content == "hello"

    def test_with_screenshot_returns_text_image_list(self):
        img = validate_image(_PNG_1X1, "image/png")
        ctx = FigmaDesignContext(
            file_key="ABC", node_id="1:2", code="<div/>",
            screenshot=img,
            source="figma.com/design/ABC?node-id=1-2",
        )
        msg = build_multimodal_message(ctx, "prompt")
        assert isinstance(msg.content, list)
        assert msg.content[0] == {"type": "text", "text": "prompt"}
        assert msg.content[1]["type"] == "image"
        assert msg.content[1]["source"]["media_type"] == "image/png"


# ── FigmaGenerationResult ────────────────────────────────────────────


class TestFigmaGenerationResult:
    def test_is_clean_requires_non_empty_tsx(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        r = FigmaGenerationResult(
            context=ctx,
            extraction=FigmaExtraction(),
            tsx_code="",
            lint_report=LintReport(),
        )
        assert not r.is_clean
        r2 = FigmaGenerationResult(
            context=ctx,
            extraction=FigmaExtraction(),
            tsx_code="<Button>ok</Button>\n",
            lint_report=LintReport(),
        )
        assert r2.is_clean

    def test_to_dict_json_safe(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        r = FigmaGenerationResult(
            context=ctx,
            extraction=FigmaExtraction(),
            tsx_code="<Button>ok</Button>",
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=("llm_unavailable",),
            model="claude-opus-4-7",
            provider="anthropic",
        )
        d = r.to_dict()
        json.dumps(d)
        assert d["warnings"] == ["llm_unavailable"]
        assert d["schema_version"] == FIGMA_SCHEMA_VERSION
        assert d["context"]["file_key"] == "ABC"


# ── Pipeline: generate_ui_from_figma ────────────────────────────────


class FakeInvoker:
    """Deterministic chat-invoker double for pipeline tests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list] = []

    def __call__(self, messages):
        self.calls.append(messages)
        if not self._responses:
            return ""
        return self._responses.pop(0)


class TestGenerateUiFromFigma:
    def _ctx(self) -> FigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_happy_path_clean_tsx(self):
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            brief="pricing page",
            invoker=inv,
        )
        assert isinstance(result, FigmaGenerationResult)
        assert "<Button" in result.tsx_code
        assert result.lint_report.is_clean
        assert not result.auto_fix_applied
        assert "llm_unavailable" not in result.warnings
        assert result.is_clean
        assert len(inv.calls) == 1

    def test_llm_unavailable_returns_empty_tsx(self):
        inv = FakeInvoker([])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert result.tsx_code == ""
        assert "llm_unavailable" in result.warnings
        assert not result.is_clean

    def test_tsx_missing_fallback_preserves_raw(self):
        inv = FakeInvoker([
            "Sorry, I can't parse this Figma node — please re-export.",
        ])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "tsx_missing" in result.warnings
        assert "Sorry" in result.tsx_code

    def test_auto_fix_rewrites_raw_button(self):
        dirty = (
            "```tsx\n"
            "export default function X({ handleSave }: { handleSave: () => void }) {\n"
            "  return <button onClick={handleSave}>Save</button>;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([dirty])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=True,
        )
        assert result.auto_fix_applied
        assert "<Button onClick={handleSave}>Save</Button>" in result.tsx_code
        assert "<button " not in result.tsx_code
        assert "</button>" not in result.tsx_code
        assert "@/components/ui/button" in result.tsx_code
        assert result.pre_fix_lint_report is not None
        assert not result.pre_fix_lint_report.is_clean
        assert result.lint_report.is_clean

    def test_auto_fix_disabled_leaves_violation(self):
        dirty = (
            "```tsx\n"
            "export default function X() {\n"
            "  return <button>Save</button>;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([dirty])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=False,
        )
        assert not result.auto_fix_applied
        assert "<button" in result.tsx_code
        assert not result.lint_report.is_clean

    def test_preprovided_extraction_is_used(self):
        ctx = self._ctx()
        extraction = extract_from_context(ctx)
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        result = generate_ui_from_figma(
            ctx,
            project_root=PROJECT_ROOT,
            invoker=inv,
            extraction=extraction,
        )
        assert result.extraction is extraction

    def test_bubbles_up_mcp_parse_warnings(self):
        # A malformed MCP response: non-JSON string.
        ctx = from_mcp_response(
            "not json", file_key="ABC", node_id="1:2",
        )
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        result = generate_ui_from_figma(
            ctx,
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "mcp_response_not_json" in result.warnings
        assert "figma_context_empty" in result.warnings

    def test_context_without_screenshot_still_works(self):
        ctx = validate_figma_context(
            file_key="ABC", node_id="1:2",
            code="<Card><CardContent>Hi</CardContent></Card>",
        )
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        result = generate_ui_from_figma(
            ctx, project_root=PROJECT_ROOT, invoker=inv,
        )
        assert result.is_clean
        # The request must be a text-only message.
        msg = inv.calls[0][0]
        assert msg.content == msg.content  # smoke — it's a string
        assert isinstance(msg.content, str)

    def test_rejects_non_context_argument(self):
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        with pytest.raises(TypeError):
            generate_ui_from_figma(
                "not a context",  # type: ignore[arg-type]
                project_root=PROJECT_ROOT,
                invoker=inv,
            )

    def test_model_and_provider_forwarded(self):
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            provider="anthropic",
            model="claude-opus-4-7",
        )
        assert result.provider == "anthropic"
        assert result.model == "claude-opus-4-7"


# ── run_figma_to_ui ──────────────────────────────────────────────────


class TestRunFigmaToUi:
    def test_mcp_response_path_returns_json_safe_dict(self):
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        out = run_figma_to_ui(
            file_key="ABC",
            node_id="1:2",
            mcp_response=_FIGMA_MCP_RESPONSE,
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert isinstance(out, dict)
        json.dumps(out)
        assert out["schema_version"] == FIGMA_SCHEMA_VERSION
        assert "context" in out and "extraction" in out
        assert "tsx_code" in out and "lint_report" in out
        assert out["is_clean"] is True

    def test_context_path(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        inv = FakeInvoker([_CLEAN_TSX_RESPONSE])
        out = run_figma_to_ui(
            file_key="ABC",
            node_id="1:2",
            context=ctx,
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert out["context"]["file_key"] == "ABC"

    def test_requires_exactly_one_of(self):
        with pytest.raises(ValueError):
            run_figma_to_ui(
                file_key="ABC", node_id="1:2",
                project_root=PROJECT_ROOT,
            )
        with pytest.raises(ValueError):
            ctx = from_mcp_response(
                _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
            )
            run_figma_to_ui(
                file_key="ABC", node_id="1:2",
                mcp_response=_FIGMA_MCP_RESPONSE,
                context=ctx,
                project_root=PROJECT_ROOT,
            )

    def test_context_key_mismatch_raises(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        with pytest.raises(ValueError):
            run_figma_to_ui(
                file_key="DIFFERENT", node_id="1:2",
                context=ctx, project_root=PROJECT_ROOT,
            )
        with pytest.raises(ValueError):
            run_figma_to_ui(
                file_key="ABC", node_id="9:9",
                context=ctx, project_root=PROJECT_ROOT,
            )

    def test_surfaces_llm_unavailable(self):
        inv = FakeInvoker([])
        out = run_figma_to_ui(
            file_key="ABC",
            node_id="1:2",
            mcp_response=_FIGMA_MCP_RESPONSE,
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "llm_unavailable" in out["warnings"]
        assert out["tsx_code"] == ""


# ── Default invoker wiring (no network) ──────────────────────────────


class TestDefaultInvokerWiring:
    def test_default_invoker_bound_to_provider_model(self):
        from backend import figma_to_ui as module

        seen: dict = {}

        def _fake_invoke_chat(messages, *, provider=None, model=None, llm=None):
            seen["provider"] = provider
            seen["model"] = model
            return _CLEAN_TSX_RESPONSE

        with patch("backend.llm_adapter.invoke_chat", _fake_invoke_chat):
            out = module.generate_ui_from_figma(
                from_mcp_response(
                    _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
                ),
                project_root=PROJECT_ROOT,
                provider="anthropic",
                model="claude-opus-4-7",
            )
        assert out.is_clean
        assert seen["provider"] == "anthropic"
        assert seen["model"] == "claude-opus-4-7"

    def test_default_invoker_swallows_network_errors(self):
        def _boom(messages, *, provider=None, model=None, llm=None):
            raise RuntimeError("network down")

        with patch("backend.llm_adapter.invoke_chat", _boom):
            result = generate_ui_from_figma(
                from_mcp_response(
                    _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
                ),
                project_root=PROJECT_ROOT,
            )
        assert "llm_unavailable" in result.warnings
        assert result.tsx_code == ""


# ── Cross-module integration ─────────────────────────────────────────


class TestSiblingIntegration:
    def _ctx(self) -> FigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_prompt_references_installed_registry_and_tokens(self):
        prompt = build_figma_generation_prompt(
            context=self._ctx(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "button" in prompt.lower()
        assert "card" in prompt.lower()
        assert "primary" in prompt.lower()
        assert "background" in prompt.lower()

    def test_pipeline_auto_fix_maps_input_to_shadcn(self):
        dirty = (
            "```tsx\n"
            "export default function X() {\n"
            "  return <input type=\"email\" />;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([dirty])
        result = generate_ui_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=True,
        )
        assert "<Input" in result.tsx_code
        assert "@/components/ui/input" in result.tsx_code

    def test_extraction_tokens_round_trip_through_to_dict(self):
        extraction = extract_from_context(self._ctx())
        d = extraction.to_dict()
        json.dumps(d)
        # At minimum the MCP-provided variables come through.
        names = {t["name"] for t in d["design_tokens"]}
        assert "color/primary" in names

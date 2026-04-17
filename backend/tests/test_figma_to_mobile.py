"""V5 #3 (issue #321) — figma_to_mobile pipeline contract tests.

Pins ``backend/figma_to_mobile.py`` against:

  * structural invariants of :class:`MobileFigmaToken`,
    :class:`MobileFigmaDesignContext`, :class:`MobileFigmaExtraction`,
    :class:`MobileCodeOutputs`, :class:`MobileFigmaGenerationResult`
    (frozen, validated, JSON-safe);
  * node-id / file-key normalisation (canonicalises both the
    ``123-456`` url form and the ``123:456`` API form);
  * :func:`from_mcp_response` tolerance for raw dict, stringified
    JSON, and MCP ``{"content":[{type:"text", text:"…"}]}`` envelopes,
    including screenshot base64 / data-URL unwrap;
  * extraction heuristics — colours / spacing / radii / shadows /
    typography / component hierarchy / imports — on a synthetic Figma
    reference code payload;
  * deterministic prompt construction (byte-identical across calls);
  * the three-platform response parser —
    :func:`extract_mobile_code_from_response` — for fenced
    ``swift`` / ``kotlin`` / ``dart`` + accepted aliases, plus graceful
    handling of partial / missing / empty responses;
  * the full :func:`generate_mobile_from_figma` pipeline with injected
    fake invokers: successful generation, LLM-unavailable fallback,
    partial-platform fallback, graceful bubbling of MCP-parse
    warnings;
  * the agent-callable :func:`run_figma_to_mobile` entry point with
    both ``mcp_response=`` and ``context=`` modes;
  * sibling module integration — the prompt really references the
    live mobile component registry + design-token block, and
    ``TARGET_PLATFORMS`` stays in sync with
    :mod:`backend.mobile_component_registry`.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import figma_to_mobile as ftm
from backend.figma_to_mobile import (
    DEFAULT_FIGMA_MOBILE_MODEL,
    DEFAULT_FIGMA_MOBILE_PROVIDER,
    FIGMA_MOBILE_SCHEMA_VERSION,
    PLATFORM_LANGS,
    TARGET_PLATFORMS,
    TOKEN_KINDS,
    MobileCodeOutputs,
    MobileFigmaDesignContext,
    MobileFigmaExtraction,
    MobileFigmaGenerationResult,
    MobileFigmaToken,
    build_mobile_generation_prompt,
    build_multimodal_message,
    canonical_figma_source,
    extract_from_context,
    extract_mobile_code_from_response,
    from_mcp_response,
    generate_mobile_from_figma,
    normalize_file_key,
    normalize_node_id,
    run_figma_to_mobile,
    validate_figma_mobile_context,
)
from backend.mobile_component_registry import (
    PLATFORMS as REGISTRY_PLATFORMS,
)
from backend.vision_to_ui import VisionImage, validate_image


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Shared fixtures ──────────────────────────────────────────────────


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_1X1_B64 = base64.b64encode(_PNG_1X1).decode("ascii")


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
        "color/background": {"r": 0.043, "g": 0.070, "b": 0.118},
        "radius/md": "8px",
        "spacing/4": "16px",
        "font/sans": "Inter, sans-serif",
    },
    "metadata": {
        "frame_name": "Hero / Pricing",
        "width": 375,
        "height": 812,
    },
    "asset_urls": {
        "hero_bg.png": "https://example.com/a.png",
    },
}


_CLEAN_THREE_PLATFORM_RESPONSE = (
    "Here's the three-platform rebuild:\n\n"
    "```swift\n"
    "// Platform: SwiftUI\n"
    "import SwiftUI\n"
    "\n"
    "struct HeroSectionView: View {\n"
    "  var body: some View {\n"
    "    VStack(alignment: .leading, spacing: AppSpacing.md) {\n"
    "      Text(\"Pricing\").font(.title2).bold()\n"
    "      Text(\"Pick a plan that scales with your team.\")\n"
    "        .font(.subheadline)\n"
    "        .foregroundStyle(.secondary)\n"
    "      Button(\"Upgrade\") { onUpgrade() }\n"
    "        .buttonStyle(.borderedProminent)\n"
    "        .accessibilityLabel(\"Upgrade plan\")\n"
    "    }\n"
    "  }\n"
    "}\n"
    "```\n"
    "\n"
    "```kotlin\n"
    "// Platform: Jetpack Compose\n"
    "@Composable\n"
    "fun HeroSection(onUpgrade: () -> Unit) {\n"
    "  Card(\n"
    "    colors = CardDefaults.cardColors(\n"
    "      containerColor = MaterialTheme.colorScheme.surfaceContainer,\n"
    "    ),\n"
    "  ) {\n"
    "    Column(\n"
    "      modifier = Modifier.padding(AppSpacing.md),\n"
    "      verticalArrangement = Arrangement.spacedBy(AppSpacing.sm),\n"
    "    ) {\n"
    "      Text(\"Pricing\", style = MaterialTheme.typography.headlineSmall)\n"
    "      Text(\n"
    "        \"Pick a plan that scales with your team.\",\n"
    "        style = MaterialTheme.typography.bodyMedium,\n"
    "      )\n"
    "      FilledTonalButton(onClick = onUpgrade) { Text(\"Upgrade\") }\n"
    "    }\n"
    "  }\n"
    "}\n"
    "```\n"
    "\n"
    "```dart\n"
    "// Platform: Flutter\n"
    "class HeroSection extends StatelessWidget {\n"
    "  const HeroSection({super.key, required this.onUpgrade});\n"
    "  final VoidCallback onUpgrade;\n"
    "\n"
    "  @override\n"
    "  Widget build(BuildContext context) {\n"
    "    final theme = Theme.of(context);\n"
    "    return Card(\n"
    "      child: Padding(\n"
    "        padding: const EdgeInsets.all(16),\n"
    "        child: Column(\n"
    "          crossAxisAlignment: CrossAxisAlignment.start,\n"
    "          children: [\n"
    "            Text('Pricing', style: theme.textTheme.headlineSmall),\n"
    "            Text(\n"
    "              'Pick a plan that scales with your team.',\n"
    "              style: theme.textTheme.bodyMedium,\n"
    "            ),\n"
    "            FilledButton(onPressed: onUpgrade, child: const Text('Upgrade')),\n"
    "          ],\n"
    "        ),\n"
    "      ),\n"
    "    );\n"
    "  }\n"
    "}\n"
    "```\n"
)


# ── Module invariants ────────────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_is_semver(self):
        parts = FIGMA_MOBILE_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_default_model_is_opus_4_7(self):
        assert DEFAULT_FIGMA_MOBILE_MODEL.startswith("claude-opus-4-7")
        assert DEFAULT_FIGMA_MOBILE_PROVIDER == "anthropic"

    def test_target_platforms_match_registry(self):
        assert TARGET_PLATFORMS == REGISTRY_PLATFORMS

    def test_platform_langs_cover_all_platforms(self):
        for plat in TARGET_PLATFORMS:
            assert plat in PLATFORM_LANGS
        assert PLATFORM_LANGS["swiftui"] == "swift"
        assert PLATFORM_LANGS["compose"] == "kotlin"
        assert PLATFORM_LANGS["flutter"] == "dart"

    def test_token_kinds_fixed(self):
        assert TOKEN_KINDS == (
            "color", "spacing", "radius", "font", "shadow", "other",
        )

    @pytest.mark.parametrize("name", [
        "FIGMA_MOBILE_SCHEMA_VERSION",
        "DEFAULT_FIGMA_MOBILE_MODEL",
        "DEFAULT_FIGMA_MOBILE_PROVIDER",
        "TARGET_PLATFORMS",
        "PLATFORM_LANGS",
        "TOKEN_KINDS",
        "MobileFigmaToken",
        "MobileFigmaDesignContext",
        "MobileFigmaExtraction",
        "MobileCodeOutputs",
        "MobileFigmaGenerationResult",
        "normalize_node_id",
        "normalize_file_key",
        "canonical_figma_source",
        "from_mcp_response",
        "validate_figma_mobile_context",
        "extract_from_context",
        "build_mobile_generation_prompt",
        "build_multimodal_message",
        "extract_mobile_code_from_response",
        "generate_mobile_from_figma",
        "run_figma_to_mobile",
    ])
    def test_public_surface_exports(self, name):
        assert name in ftm.__all__, f"{name} must be in __all__"


# ── Node-id / file-key normalisation ────────────────────────────────


class TestNormaliseNodeId:
    @pytest.mark.parametrize("raw,expected", [
        ("1:2", "1:2"),
        ("1-2", "1:2"),
        ("123:456", "123:456"),
        ("123-456", "123:456"),
        ("-5:6", "-5:6"),
        ("-5-6", "-5:6"),
        ("  1:2  ", "1:2"),
    ])
    def test_accepts_canonical_forms(self, raw, expected):
        assert normalize_node_id(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", "   ", "1", "1:", ":2", "abc", "1/2", "1:2:3",
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

    @pytest.mark.parametrize("raw", ["", "   ", "ABC/DEF", "with space"])
    def test_rejects_malformed(self, raw):
        with pytest.raises(ValueError):
            normalize_file_key(raw)


class TestCanonicalSource:
    def test_emits_url_shape_with_dash(self):
        assert (
            canonical_figma_source("XYZ", "1:2")
            == "figma.com/design/XYZ?node-id=1-2"
        )


# ── MobileFigmaToken ─────────────────────────────────────────────────


class TestMobileFigmaToken:
    def test_frozen(self):
        t = MobileFigmaToken(name="primary", value="#38bdf8", kind="color")
        with pytest.raises(Exception):
            t.kind = "font"  # type: ignore[misc]

    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            MobileFigmaToken(name="primary", value="#abc", kind="not-a-kind")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError):
            MobileFigmaToken(name="", value="#abc", kind="color")

    def test_rejects_none_value(self):
        with pytest.raises(ValueError):
            MobileFigmaToken(name="x", value=None, kind="color")  # type: ignore[arg-type]


# ── MobileFigmaDesignContext ─────────────────────────────────────────


class TestMobileFigmaDesignContext:
    def test_validate_happy_path(self):
        ctx = validate_figma_mobile_context(
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
            validate_figma_mobile_context(file_key="", node_id="1:2")

    def test_rejects_invalid_node_id(self):
        with pytest.raises(ValueError):
            validate_figma_mobile_context(file_key="ABC", node_id="xyz")

    def test_inner_mappings_are_readonly(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", variables={"x": 1},
        )
        with pytest.raises(TypeError):
            ctx.variables["x"] = 2  # type: ignore[index]

    def test_normalises_node_id_dash_form(self):
        ctx = validate_figma_mobile_context(file_key="ABC", node_id="123-456")
        assert ctx.node_id == "123:456"

    def test_to_dict_is_json_safe(self):
        img = validate_image(_PNG_1X1, "image/png")
        ctx = MobileFigmaDesignContext(
            file_key="ABC", node_id="1:2",
            code="<div/>",
            screenshot=img,
            source="figma.com/design/ABC?node-id=1-2",
        )
        d = ctx.to_dict()
        json.dumps(d)
        assert d["has_screenshot"] is True
        assert d["screenshot_mime"] == "image/png"
        assert d["screenshot_bytes"] == len(_PNG_1X1)
        assert d["schema_version"] == FIGMA_MOBILE_SCHEMA_VERSION

    def test_rejects_wrong_screenshot_type(self):
        with pytest.raises(TypeError):
            MobileFigmaDesignContext(
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
        assert ctx.has_screenshot
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
        ctx = from_mcp_response([1, 2, 3], file_key="ABC", node_id="1:2")
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "mcp_response_not_object" in warnings

    def test_data_url_screenshot(self):
        data_url = f"data:image/png;base64,{_PNG_1X1_B64}"
        response = {"code": "<div/>", "screenshot": data_url}
        ctx = from_mcp_response(response, file_key="ABC", node_id="1:2")
        assert ctx.has_screenshot
        assert ctx.screenshot and ctx.screenshot.mime_type == "image/png"

    def test_bytes_screenshot(self):
        response = {"code": "<div/>", "screenshot": _PNG_1X1}
        ctx = from_mcp_response(response, file_key="ABC", node_id="1:2")
        assert ctx.has_screenshot

    def test_bad_base64_screenshot_warns_but_returns(self):
        response = {"code": "<div/>", "screenshot": "@@not b64@@"}
        ctx = from_mcp_response(response, file_key="ABC", node_id="1:2")
        assert not ctx.has_screenshot
        warnings = ctx.metadata.get("_parse_warnings") or ()
        assert "screenshot_invalid" in warnings

    def test_canonical_source_attached(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        assert ctx.source == "figma.com/design/ABC?node-id=1-2"

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
    def _ctx(self, code: str = _FIGMA_REFERENCE_CODE) -> MobileFigmaDesignContext:
        return validate_figma_mobile_context(
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
        assert "32px" in extraction.spacing_values
        assert "14px" in extraction.spacing_values

    def test_detects_scale_spacing_utilities(self):
        extraction = extract_from_context(self._ctx())
        joined = "|".join(extraction.spacing_values)
        assert "scale:gap-6" in joined
        assert "scale:p-6" in joined

    def test_detects_radii(self):
        extraction = extract_from_context(self._ctx())
        assert any("rounded-lg" in r for r in extraction.radii)
        assert any("rounded-md" in r for r in extraction.radii)
        assert any("rounded-[16px]" in r for r in extraction.radii)

    def test_detects_shadows(self):
        extraction = extract_from_context(self._ctx())
        joined = "|".join(extraction.shadows)
        assert "rgba(0, 0, 0, 0.35)" in joined or "rgba(0,0,0,0.35)" in joined

    def test_detects_typography(self):
        extraction = extract_from_context(self._ctx())
        assert "text-2xl" in extraction.typography
        assert "font-semibold" in extraction.typography

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
        assert "color/primary" in names
        assert any(n.startswith("observed-color-") for n in names)

    def test_empty_code_returns_empty_flagged(self):
        ctx = validate_figma_mobile_context(file_key="ABC", node_id="1:2", code="")
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

    def test_variable_rgb_dict_normalises_to_hex(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
            variables={"color/primary": {"r": 0.22, "g": 0.74, "b": 0.97}},
        )
        extraction = extract_from_context(ctx)
        primary = next(
            t for t in extraction.design_tokens
            if t.name == "color/primary"
        )
        assert primary.value.startswith("#")
        assert primary.kind == "color"

    def test_css_var_references_surfaced(self):
        code = 'export default () => <div style={{ color: "var(--primary)" }} />'
        ctx = validate_figma_mobile_context(file_key="ABC", node_id="1:2", code=code)
        extraction = extract_from_context(ctx)
        assert "var(--primary)" in extraction.color_values


# ── Prompt construction ──────────────────────────────────────────────


class TestGenerationPromptDeterminism:
    def _ctx(self) -> MobileFigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_same_inputs_byte_identical(self):
        ctx = self._ctx()
        a = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="pricing card",
        )
        b = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="pricing card",
        )
        assert a == b

    def test_brief_changes_prompt(self):
        ctx = self._ctx()
        a = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="a",
        )
        b = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief="b",
        )
        assert a != b

    def test_prompt_contains_rules_and_figma_header(self):
        prompt = build_mobile_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma MCP" in prompt
        assert "Generation rules" in prompt
        assert "```swift" in prompt
        assert "```kotlin" in prompt
        assert "```dart" in prompt
        assert "SwiftUI" in prompt
        assert "Compose" in prompt
        assert "Flutter" in prompt

    def test_prompt_contains_figma_source_and_extraction(self):
        prompt = build_mobile_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma source" in prompt
        assert "Figma extraction" in prompt
        assert "file_key" in prompt
        assert "node_id" in prompt

    def test_prompt_injects_registry_and_tokens(self):
        prompt = build_mobile_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        # Mobile component registry block references "Mobile component registry".
        assert "Mobile component registry" in prompt
        # At least one SwiftUI component should be listed.
        assert "NavigationStack" in prompt
        # Compose + Flutter primitives should appear too.
        assert "Scaffold" in prompt

    def test_prompt_contains_reference_code_fence(self):
        prompt = build_mobile_generation_prompt(
            context=self._ctx(), project_root=PROJECT_ROOT, brief=None,
        )
        assert "Figma reference code" in prompt
        assert "HeroSection" in prompt

    def test_prompt_reference_code_is_truncated(self):
        huge = "<div>" + ("// " + "a" * 100 + "\n") * 200 + "</div>"
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code=huge,
        )
        prompt = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "truncated" in prompt

    def test_prompt_handles_missing_code(self):
        ctx = validate_figma_mobile_context(file_key="ABC", node_id="1:2", code="")
        prompt = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "no reference code" in prompt.lower()

    def test_prompt_shows_screenshot_flag(self):
        ctx = self._ctx()
        prompt = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "attached (multimodal)" in prompt

    def test_prompt_without_screenshot(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        prompt = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "not attached" in prompt

    def test_empty_brief_is_rendered(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        prompt = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        assert "(none)" in prompt

    def test_platforms_narrowing(self):
        ctx = self._ctx()
        prompt_all = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
        )
        prompt_one = build_mobile_generation_prompt(
            context=ctx, project_root=PROJECT_ROOT, brief=None,
            platforms=("swiftui",),
        )
        # Narrowed prompt should NOT mention Compose / Flutter in the
        # `Target platforms` section (it still may mention them in the
        # rules header, which lists all three by design — so we check
        # the renderer listing, not the rules).
        assert "Target platforms" in prompt_one
        assert prompt_all != prompt_one
        # The narrowed prompt's registry block must not include the
        # "Flutter 3.22+" header since that platform was filtered out.
        assert "Flutter 3.22+" not in prompt_one
        assert "Flutter 3.22+" in prompt_all

    def test_platforms_rejects_unknown(self):
        ctx = self._ctx()
        with pytest.raises(ValueError):
            build_mobile_generation_prompt(
                context=ctx, project_root=PROJECT_ROOT, brief=None,
                platforms=("android",),
            )

    def test_platforms_rejects_empty(self):
        ctx = self._ctx()
        with pytest.raises(ValueError):
            build_mobile_generation_prompt(
                context=ctx, project_root=PROJECT_ROOT, brief=None,
                platforms=(),
            )


# ── Multimodal message ───────────────────────────────────────────────


class TestMultimodalMessage:
    def test_without_screenshot_returns_text_message(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        msg = build_multimodal_message(ctx, "hello")
        assert msg.content == "hello"

    def test_with_screenshot_returns_text_image_list(self):
        img = validate_image(_PNG_1X1, "image/png")
        ctx = MobileFigmaDesignContext(
            file_key="ABC", node_id="1:2", code="<div/>",
            screenshot=img,
            source="figma.com/design/ABC?node-id=1-2",
        )
        msg = build_multimodal_message(ctx, "prompt")
        assert isinstance(msg.content, list)
        assert msg.content[0] == {"type": "text", "text": "prompt"}
        assert msg.content[1]["type"] == "image"
        assert msg.content[1]["source"]["media_type"] == "image/png"


# ── Response parsing ─────────────────────────────────────────────────


class TestExtractMobileCodeFromResponse:
    def test_clean_three_platform_response(self):
        outputs = extract_mobile_code_from_response(_CLEAN_THREE_PLATFORM_RESPONSE)
        assert isinstance(outputs, MobileCodeOutputs)
        assert "HeroSectionView" in outputs.swift
        assert "@Composable" in outputs.kotlin
        assert "StatelessWidget" in outputs.dart
        assert outputs.is_complete
        assert outputs.missing_platforms() == ()

    def test_empty_response(self):
        outputs = extract_mobile_code_from_response("")
        assert outputs == MobileCodeOutputs()
        assert not outputs.is_complete
        assert set(outputs.missing_platforms()) == set(TARGET_PLATFORMS)

    def test_only_swift_present(self):
        text = "```swift\nimport SwiftUI\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "import SwiftUI" in outputs.swift
        assert outputs.kotlin == ""
        assert outputs.dart == ""
        assert not outputs.is_complete
        assert set(outputs.missing_platforms()) == {"compose", "flutter"}

    def test_swiftui_alias_accepted(self):
        text = "```swiftui\nstruct V: View { var body: some View { Text(\"hi\") } }\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "struct V" in outputs.swift

    def test_compose_aliases(self):
        for alias in ("kotlin", "kt", "compose"):
            text = f"```{alias}\n@Composable\nfun X() {{}}\n```\n"
            outputs = extract_mobile_code_from_response(text)
            assert "@Composable" in outputs.kotlin, f"alias {alias} not recognised"

    def test_flutter_alias(self):
        text = "```flutter\nclass X extends StatelessWidget {}\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "StatelessWidget" in outputs.dart

    def test_first_block_wins_on_duplicate(self):
        text = (
            "```swift\n// first\n```\n"
            "```swift\n// second\n```\n"
        )
        outputs = extract_mobile_code_from_response(text)
        assert "// first" in outputs.swift
        assert "// second" not in outputs.swift

    def test_unknown_fence_ignored(self):
        text = (
            "```swift\nimport SwiftUI\n```\n"
            "```text\nPlain notes\n```\n"
            "```kotlin\n@Composable\nfun X() {}\n```\n"
            "```dart\nclass X extends StatelessWidget {}\n```\n"
        )
        outputs = extract_mobile_code_from_response(text)
        assert outputs.is_complete
        assert "Plain notes" not in outputs.swift
        assert "Plain notes" not in outputs.kotlin
        assert "Plain notes" not in outputs.dart

    def test_tilde_fences_accepted(self):
        text = (
            "~~~swift\nimport SwiftUI\n~~~\n"
            "~~~kotlin\n@Composable\nfun X() {}\n~~~\n"
            "~~~dart\nclass X extends StatelessWidget {}\n~~~\n"
        )
        outputs = extract_mobile_code_from_response(text)
        assert outputs.is_complete

    def test_mobile_code_outputs_is_frozen(self):
        outputs = MobileCodeOutputs(swift="a", kotlin="b", dart="c")
        with pytest.raises(Exception):
            outputs.swift = "x"  # type: ignore[misc]

    def test_mobile_code_outputs_to_dict(self):
        outputs = MobileCodeOutputs(swift="a", kotlin="b", dart="")
        d = outputs.to_dict()
        json.dumps(d)
        assert d["swift"] == "a"
        assert d["kotlin"] == "b"
        assert d["dart"] == ""
        assert d["is_complete"] is False
        assert d["missing_platforms"] == ["flutter"]


# ── MobileFigmaGenerationResult ──────────────────────────────────────


class TestMobileFigmaGenerationResult:
    def test_is_complete_requires_all_three(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        r = MobileFigmaGenerationResult(
            context=ctx, extraction=MobileFigmaExtraction(),
        )
        assert not r.is_complete

        complete = MobileFigmaGenerationResult(
            context=ctx,
            extraction=MobileFigmaExtraction(),
            outputs=MobileCodeOutputs(swift="s", kotlin="k", dart="d"),
        )
        assert complete.is_complete

    def test_to_dict_json_safe(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2", code="<div/>",
        )
        r = MobileFigmaGenerationResult(
            context=ctx,
            extraction=MobileFigmaExtraction(),
            outputs=MobileCodeOutputs(swift="a", kotlin="b", dart="c"),
            raw_response="ok",
            warnings=("llm_unavailable",),
            model="claude-opus-4-7",
            provider="anthropic",
        )
        d = r.to_dict()
        json.dumps(d)
        assert d["warnings"] == ["llm_unavailable"]
        assert d["schema_version"] == FIGMA_MOBILE_SCHEMA_VERSION
        assert d["context"]["file_key"] == "ABC"
        assert d["outputs"]["is_complete"] is True


# ── Pipeline: generate_mobile_from_figma ────────────────────────────


class FakeInvoker:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list] = []

    def __call__(self, messages):
        self.calls.append(messages)
        if not self._responses:
            return ""
        return self._responses.pop(0)


class TestGenerateMobileFromFigma:
    def _ctx(self) -> MobileFigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_happy_path_three_platforms(self):
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            brief="pricing card",
            invoker=inv,
        )
        assert isinstance(result, MobileFigmaGenerationResult)
        assert result.is_complete
        assert "HeroSectionView" in result.outputs.swift
        assert "@Composable" in result.outputs.kotlin
        assert "StatelessWidget" in result.outputs.dart
        assert "llm_unavailable" not in result.warnings
        assert len(inv.calls) == 1

    def test_llm_unavailable_returns_empty(self):
        inv = FakeInvoker([])
        result = generate_mobile_from_figma(
            self._ctx(), project_root=PROJECT_ROOT, invoker=inv,
        )
        assert not result.is_complete
        assert result.outputs.swift == ""
        assert result.outputs.kotlin == ""
        assert result.outputs.dart == ""
        assert "llm_unavailable" in result.warnings

    def test_partial_response_emits_missing_warnings(self):
        partial = (
            "```swift\nimport SwiftUI\nstruct V: View { var body: some View { Text(\"x\") } }\n```\n"
        )
        inv = FakeInvoker([partial])
        result = generate_mobile_from_figma(
            self._ctx(), project_root=PROJECT_ROOT, invoker=inv,
        )
        assert "import SwiftUI" in result.outputs.swift
        assert not result.is_complete
        assert "compose_missing" in result.warnings
        assert "flutter_missing" in result.warnings
        assert "swiftui_missing" not in result.warnings

    def test_preprovided_extraction_is_used(self):
        ctx = self._ctx()
        extraction = extract_from_context(ctx)
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_figma(
            ctx,
            project_root=PROJECT_ROOT,
            invoker=inv,
            extraction=extraction,
        )
        assert result.extraction is extraction

    def test_bubbles_up_mcp_parse_warnings(self):
        ctx = from_mcp_response(
            "not json", file_key="ABC", node_id="1:2",
        )
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_figma(
            ctx, project_root=PROJECT_ROOT, invoker=inv,
        )
        assert "mcp_response_not_json" in result.warnings
        assert "figma_context_empty" in result.warnings

    def test_context_without_screenshot_still_works(self):
        ctx = validate_figma_mobile_context(
            file_key="ABC", node_id="1:2",
            code="<Card><CardContent>Hi</CardContent></Card>",
        )
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_figma(
            ctx, project_root=PROJECT_ROOT, invoker=inv,
        )
        assert result.is_complete
        msg = inv.calls[0][0]
        assert isinstance(msg.content, str)

    def test_rejects_non_context_argument(self):
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        with pytest.raises(TypeError):
            generate_mobile_from_figma(
                "not a context",  # type: ignore[arg-type]
                project_root=PROJECT_ROOT, invoker=inv,
            )

    def test_model_and_provider_forwarded(self):
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            provider="anthropic",
            model="claude-opus-4-7",
        )
        assert result.provider == "anthropic"
        assert result.model == "claude-opus-4-7"

    def test_platforms_narrow_skips_missing_warnings(self):
        # When caller only asked for swiftui, a compose-missing / flutter-missing
        # warning must NOT be emitted.
        swift_only = (
            "```swift\nimport SwiftUI\nstruct V: View { var body: some View { Text(\"x\") } }\n```\n"
        )
        inv = FakeInvoker([swift_only])
        result = generate_mobile_from_figma(
            self._ctx(),
            project_root=PROJECT_ROOT,
            invoker=inv,
            platforms=("swiftui",),
        )
        assert "compose_missing" not in result.warnings
        assert "flutter_missing" not in result.warnings
        assert "swiftui_missing" not in result.warnings


# ── run_figma_to_mobile ──────────────────────────────────────────────


class TestRunFigmaToMobile:
    def test_mcp_response_path_returns_json_safe_dict(self):
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        out = run_figma_to_mobile(
            file_key="ABC",
            node_id="1:2",
            mcp_response=_FIGMA_MCP_RESPONSE,
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert isinstance(out, dict)
        json.dumps(out)
        assert out["schema_version"] == FIGMA_MOBILE_SCHEMA_VERSION
        assert "context" in out
        assert "extraction" in out
        assert "outputs" in out
        assert out["is_complete"] is True

    def test_context_path(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        out = run_figma_to_mobile(
            file_key="ABC", node_id="1:2",
            context=ctx, project_root=PROJECT_ROOT, invoker=inv,
        )
        assert out["context"]["file_key"] == "ABC"

    def test_requires_exactly_one_of(self):
        with pytest.raises(ValueError):
            run_figma_to_mobile(
                file_key="ABC", node_id="1:2",
                project_root=PROJECT_ROOT,
            )
        with pytest.raises(ValueError):
            ctx = from_mcp_response(
                _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
            )
            run_figma_to_mobile(
                file_key="ABC", node_id="1:2",
                mcp_response=_FIGMA_MCP_RESPONSE,
                context=ctx, project_root=PROJECT_ROOT,
            )

    def test_context_key_mismatch_raises(self):
        ctx = from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )
        with pytest.raises(ValueError):
            run_figma_to_mobile(
                file_key="DIFFERENT", node_id="1:2",
                context=ctx, project_root=PROJECT_ROOT,
            )
        with pytest.raises(ValueError):
            run_figma_to_mobile(
                file_key="ABC", node_id="9:9",
                context=ctx, project_root=PROJECT_ROOT,
            )

    def test_surfaces_llm_unavailable(self):
        inv = FakeInvoker([])
        out = run_figma_to_mobile(
            file_key="ABC", node_id="1:2",
            mcp_response=_FIGMA_MCP_RESPONSE,
            project_root=PROJECT_ROOT, invoker=inv,
        )
        assert "llm_unavailable" in out["warnings"]
        assert out["outputs"]["swift"] == ""
        assert out["outputs"]["kotlin"] == ""
        assert out["outputs"]["dart"] == ""


# ── Default invoker wiring (no network) ──────────────────────────────


class TestDefaultInvokerWiring:
    def test_default_invoker_bound_to_provider_model(self):
        seen: dict = {}

        def _fake_invoke_chat(messages, *, provider=None, model=None, llm=None):
            seen["provider"] = provider
            seen["model"] = model
            return _CLEAN_THREE_PLATFORM_RESPONSE

        with patch("backend.llm_adapter.invoke_chat", _fake_invoke_chat):
            out = generate_mobile_from_figma(
                from_mcp_response(
                    _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
                ),
                project_root=PROJECT_ROOT,
                provider="anthropic",
                model="claude-opus-4-7",
            )
        assert out.is_complete
        assert seen["provider"] == "anthropic"
        assert seen["model"] == "claude-opus-4-7"

    def test_default_invoker_swallows_network_errors(self):
        def _boom(messages, *, provider=None, model=None, llm=None):
            raise RuntimeError("network down")

        with patch("backend.llm_adapter.invoke_chat", _boom):
            result = generate_mobile_from_figma(
                from_mcp_response(
                    _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
                ),
                project_root=PROJECT_ROOT,
            )
        assert "llm_unavailable" in result.warnings
        assert not result.is_complete


# ── Cross-module integration ─────────────────────────────────────────


class TestSiblingIntegration:
    def _ctx(self) -> MobileFigmaDesignContext:
        return from_mcp_response(
            _FIGMA_MCP_RESPONSE, file_key="ABC", node_id="1:2",
        )

    def test_prompt_references_mobile_registry_and_tokens(self):
        prompt = build_mobile_generation_prompt(
            context=self._ctx(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        # Mobile registry block content.
        assert "SwiftUI (iOS 16+)" in prompt
        assert "Jetpack Compose" in prompt
        assert "Flutter" in prompt
        # Representative entries from each platform.
        assert "NavigationStack" in prompt
        assert "Scaffold" in prompt

    def test_extraction_tokens_round_trip_through_to_dict(self):
        extraction = extract_from_context(self._ctx())
        d = extraction.to_dict()
        json.dumps(d)
        names = {t["name"] for t in d["design_tokens"]}
        assert "color/primary" in names

    def test_target_platforms_identical_to_registry(self):
        # V5 #3 must NOT drift from mobile_component_registry on the
        # three-platform contract.  If someone adds a fourth platform
        # to the registry, this test will fail noisily.
        assert TARGET_PLATFORMS == REGISTRY_PLATFORMS

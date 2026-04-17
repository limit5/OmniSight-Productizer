"""V5 #4 (issue #321) — vision_to_mobile pipeline contract tests.

Pins ``backend/vision_to_mobile.py`` against:

  * structural invariants of :class:`MobileVisionAnalysis` and
    :class:`MobileVisionGenerationResult` (frozen, JSON-safe);
  * sibling alignment — :data:`TARGET_PLATFORMS` stays in sync with
    :mod:`backend.mobile_component_registry`, and :data:`PLATFORM_LANGS`
    re-exports the :mod:`backend.figma_to_mobile` mapping;
  * deterministic prompt construction (byte-identical across calls) for
    both the analysis stage and the generation stage;
  * tolerant response parsing — fenced JSON, bare JSON, prose fallback —
    and graceful degradation when the model returns nothing or garbage;
  * three-platform code extraction via the re-exported
    :func:`backend.figma_to_mobile.extract_mobile_code_from_response`
    contract;
  * the full :func:`generate_mobile_from_vision` pipeline with an
    injected fake chat invoker (no network): successful generation,
    LLM-unavailable on either round, partial-platform fallback,
    pre-provided analysis short-circuit, ``platforms=`` narrowing;
  * the agent-callable :func:`run_vision_to_mobile` entry point;
  * default-invoker wiring (mocks ``backend.llm_adapter.invoke_chat`` —
    no network);
  * cross-module integration — the prompt really references the live
    mobile registry + design-token block, and re-exported primitives
    (``VisionImage`` / ``MobileCodeOutputs``) are the same objects as
    the upstream modules so callers stay polymorphic.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from backend import vision_to_mobile as vtm
from backend.vision_to_mobile import (
    DEFAULT_VISION_MOBILE_MODEL,
    DEFAULT_VISION_MOBILE_PROVIDER,
    MAX_IMAGE_BYTES,
    PLATFORM_LANGS,
    SUPPORTED_MIME_TYPES,
    TARGET_PLATFORMS,
    VISION_MOBILE_SCHEMA_VERSION,
    MobileCodeOutputs,
    MobileVisionAnalysis,
    MobileVisionGenerationResult,
    VisionImage,
    analyze_mobile_screenshot,
    build_mobile_generation_prompt_from_vision,
    build_multimodal_message,
    build_vision_mobile_analysis_prompt,
    extract_mobile_code_from_response,
    generate_mobile_from_vision,
    parse_mobile_vision_analysis,
    run_vision_to_mobile,
    validate_image,
)
from backend.figma_to_mobile import (
    MobileCodeOutputs as FigmaMobileCodeOutputs,
    PLATFORM_LANGS as FIGMA_PLATFORM_LANGS,
    extract_mobile_code_from_response as figma_extract,
)
from backend.mobile_component_registry import (
    PLATFORMS as REGISTRY_PLATFORMS,
)
from backend.vision_to_ui import (
    MAX_IMAGE_BYTES as VTU_MAX_BYTES,
    SUPPORTED_MIME_TYPES as VTU_MIMES,
    VisionImage as UpstreamVisionImage,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Shared fixtures ──────────────────────────────────────────────────


# Smallest legal PNG (1×1 transparent pixel) — hard-coded so the suite
# stays hermetic without a PIL dep.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


_VALID_JSON_RESPONSE = json.dumps({
    "layout_summary": (
        "Phone (compact) — top app bar with back chevron and title, hero card "
        "with avatar + name, scrollable list of settings rows, bottom nav with "
        "3 tabs."
    ),
    "color_observations": [
        "dark surface background",
        "blue accent on selected tab",
    ],
    "detected_components": [
        "back button",
        "list of settings rows with chevrons",
        "bottom navigation bar with 3 tabs",
        "primary CTA button",
    ],
    "suggested_swiftui": [
        "NavigationStack", "List", "TabView", "Button",
    ],
    "suggested_compose": [
        "Scaffold", "TopAppBar", "NavigationBar", "FilledButton",
    ],
    "suggested_flutter": [
        "Scaffold", "AppBar", "NavigationBar", "FilledButton",
    ],
    "accessibility_notes": [
        "back button is icon-only — needs accessibilityLabel / contentDescription",
        "ensure touch target >= 44pt / 48dp on small chevrons",
    ],
})


_CLEAN_THREE_PLATFORM_RESPONSE = (
    "Here's the rebuilt surface:\n\n"
    "```swift\n"
    "// Platform: SwiftUI\n"
    "import SwiftUI\n"
    "\n"
    "struct SettingsView: View {\n"
    "  var body: some View {\n"
    "    NavigationStack {\n"
    "      List {\n"
    "        Section { Text(\"Profile\") }\n"
    "      }\n"
    "      .navigationTitle(\"Settings\")\n"
    "    }\n"
    "  }\n"
    "}\n"
    "```\n"
    "\n"
    "```kotlin\n"
    "// Platform: Jetpack Compose\n"
    "@Composable\n"
    "fun SettingsScreen() {\n"
    "  Scaffold(\n"
    "    topBar = { TopAppBar(title = { Text(\"Settings\") }) },\n"
    "  ) { padding ->\n"
    "    Column(modifier = Modifier.padding(padding)) {\n"
    "      Text(\"Profile\", style = MaterialTheme.typography.titleMedium)\n"
    "    }\n"
    "  }\n"
    "}\n"
    "```\n"
    "\n"
    "```dart\n"
    "// Platform: Flutter\n"
    "class SettingsScreen extends StatelessWidget {\n"
    "  const SettingsScreen({super.key});\n"
    "\n"
    "  @override\n"
    "  Widget build(BuildContext context) {\n"
    "    return Scaffold(\n"
    "      appBar: AppBar(title: const Text('Settings')),\n"
    "      body: ListView(\n"
    "        children: const [ListTile(title: Text('Profile'))],\n"
    "      ),\n"
    "    );\n"
    "  }\n"
    "}\n"
    "```\n"
)


# ── Module invariants ────────────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_is_semver(self):
        parts = VISION_MOBILE_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_default_model_is_opus_4_7(self):
        assert DEFAULT_VISION_MOBILE_MODEL.startswith("claude-opus-4-7")
        assert DEFAULT_VISION_MOBILE_PROVIDER == "anthropic"

    def test_target_platforms_match_registry(self):
        # V5 #4 must NOT drift from mobile_component_registry on the
        # three-platform contract.
        assert TARGET_PLATFORMS == REGISTRY_PLATFORMS

    def test_platform_langs_match_figma_to_mobile(self):
        # Re-export — same object identity, same content.
        assert PLATFORM_LANGS is FIGMA_PLATFORM_LANGS
        for plat in TARGET_PLATFORMS:
            assert plat in PLATFORM_LANGS
        assert PLATFORM_LANGS["swiftui"] == "swift"
        assert PLATFORM_LANGS["compose"] == "kotlin"
        assert PLATFORM_LANGS["flutter"] == "dart"

    def test_max_image_bytes_inherits_vision_to_ui(self):
        assert MAX_IMAGE_BYTES == VTU_MAX_BYTES == 5 * 1024 * 1024

    def test_supported_mime_types_inherits_vision_to_ui(self):
        assert SUPPORTED_MIME_TYPES == VTU_MIMES == frozenset({
            "image/png", "image/jpeg", "image/gif", "image/webp",
        })

    def test_vision_image_is_upstream_class(self):
        # Re-export — callers can use VisionImage from either module
        # interchangeably (e.g. validate once, hand to either pipeline).
        assert VisionImage is UpstreamVisionImage

    def test_mobile_code_outputs_is_upstream_class(self):
        # Re-export — same dataclass as figma_to_mobile so a downstream
        # consumer (e.g. the mobile-ui-designer skill) can swap inputs
        # without changing its result-handling code.
        assert MobileCodeOutputs is FigmaMobileCodeOutputs

    def test_extract_helper_is_upstream_function(self):
        assert extract_mobile_code_from_response is figma_extract

    @pytest.mark.parametrize("name", [
        "VISION_MOBILE_SCHEMA_VERSION",
        "DEFAULT_VISION_MOBILE_MODEL",
        "DEFAULT_VISION_MOBILE_PROVIDER",
        "MAX_IMAGE_BYTES",
        "SUPPORTED_MIME_TYPES",
        "TARGET_PLATFORMS",
        "PLATFORM_LANGS",
        "VisionImage",
        "MobileCodeOutputs",
        "MobileVisionAnalysis",
        "MobileVisionGenerationResult",
        "validate_image",
        "build_multimodal_message",
        "build_vision_mobile_analysis_prompt",
        "build_mobile_generation_prompt_from_vision",
        "parse_mobile_vision_analysis",
        "extract_mobile_code_from_response",
        "analyze_mobile_screenshot",
        "generate_mobile_from_vision",
        "run_vision_to_mobile",
    ])
    def test_public_surface_exports(self, name):
        assert name in vtm.__all__, f"{name} must be in __all__"


# ── MobileVisionAnalysis ─────────────────────────────────────────────


class TestMobileVisionAnalysis:
    def test_frozen(self):
        a = MobileVisionAnalysis(layout_summary="x")
        with pytest.raises(Exception):
            a.layout_summary = "y"  # type: ignore[misc]

    def test_extras_is_mapping_proxy(self):
        a = MobileVisionAnalysis(extras={"k": 1})
        with pytest.raises(TypeError):
            a.extras["k"] = 2  # type: ignore[index]

    def test_to_dict_json_safe(self):
        a = MobileVisionAnalysis(
            layout_summary="l",
            color_observations=("c",),
            detected_components=("d",),
            suggested_swiftui=("NavigationStack",),
            suggested_compose=("Scaffold",),
            suggested_flutter=("Scaffold",),
            accessibility_notes=("n",),
            raw_text="raw",
            parse_succeeded=True,
            extras={"foo": 1},
        )
        d = a.to_dict()
        s = json.dumps(d)
        rt = json.loads(s)
        assert rt["layout_summary"] == "l"
        assert rt["parse_succeeded"] is True
        assert rt["suggested_swiftui"] == ["NavigationStack"]
        assert rt["suggested_compose"] == ["Scaffold"]
        assert rt["suggested_flutter"] == ["Scaffold"]
        assert rt["extras"] == {"foo": 1}
        assert rt["schema_version"] == VISION_MOBILE_SCHEMA_VERSION

    def test_has_any_suggestions(self):
        empty = MobileVisionAnalysis()
        assert not empty.has_any_suggestions
        with_one = MobileVisionAnalysis(suggested_compose=("Scaffold",))
        assert with_one.has_any_suggestions

    def test_suggestions_for_known_platform(self):
        a = MobileVisionAnalysis(
            suggested_swiftui=("NavigationStack",),
            suggested_compose=("Scaffold",),
            suggested_flutter=("Scaffold",),
        )
        assert a.suggestions_for("swiftui") == ("NavigationStack",)
        assert a.suggestions_for("compose") == ("Scaffold",)
        assert a.suggestions_for("flutter") == ("Scaffold",)

    def test_suggestions_for_unknown_platform_raises(self):
        with pytest.raises(ValueError):
            MobileVisionAnalysis().suggestions_for("rust")


# ── MobileVisionGenerationResult ─────────────────────────────────────


class TestMobileVisionGenerationResult:
    def test_frozen(self):
        r = MobileVisionGenerationResult(analysis=MobileVisionAnalysis())
        with pytest.raises(Exception):
            r.warnings = ("x",)  # type: ignore[misc]

    def test_is_complete_requires_all_three(self):
        r = MobileVisionGenerationResult(
            analysis=MobileVisionAnalysis(),
            outputs=MobileCodeOutputs(swift="a", kotlin="b", dart=""),
        )
        assert not r.is_complete

        complete = MobileVisionGenerationResult(
            analysis=MobileVisionAnalysis(),
            outputs=MobileCodeOutputs(swift="s", kotlin="k", dart="d"),
        )
        assert complete.is_complete

    def test_to_dict_json_safe(self):
        r = MobileVisionGenerationResult(
            analysis=MobileVisionAnalysis(layout_summary="x", parse_succeeded=True),
            outputs=MobileCodeOutputs(swift="a", kotlin="b", dart="c"),
            raw_response="ok",
            warnings=("llm_unavailable",),
            model="claude-opus-4-7",
            provider="anthropic",
        )
        d = r.to_dict()
        s = json.dumps(d)
        rt = json.loads(s)
        assert rt["warnings"] == ["llm_unavailable"]
        assert rt["schema_version"] == VISION_MOBILE_SCHEMA_VERSION
        assert rt["analysis"]["layout_summary"] == "x"
        assert rt["outputs"]["is_complete"] is True
        assert rt["is_complete"] is True


# ── Multimodal message ───────────────────────────────────────────────


class TestMultimodalMessage:
    def test_content_is_text_then_image(self):
        img = validate_image(_PNG_1X1, "image/png")
        msg = build_multimodal_message(img, "describe the screen")
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert msg.content[0] == {"type": "text", "text": "describe the screen"}
        image_block = msg.content[1]
        assert image_block["type"] == "image"
        assert image_block["source"]["type"] == "base64"
        assert image_block["source"]["media_type"] == "image/png"
        assert base64.b64decode(image_block["source"]["data"]) == _PNG_1X1


# ── Analysis prompt determinism ──────────────────────────────────────


class TestAnalysisPromptDeterminism:
    def test_same_hint_same_prompt(self):
        a = build_vision_mobile_analysis_prompt("focus on the FAB")
        b = build_vision_mobile_analysis_prompt("focus on the FAB")
        assert a == b

    def test_hint_changes_prompt(self):
        a = build_vision_mobile_analysis_prompt(None)
        b = build_vision_mobile_analysis_prompt("different context")
        assert a != b
        assert "different context" in b

    def test_empty_hint_is_equivalent_to_none(self):
        a = build_vision_mobile_analysis_prompt(None)
        b = build_vision_mobile_analysis_prompt("")
        c = build_vision_mobile_analysis_prompt("   \n  ")
        assert a == b == c

    def test_prompt_mentions_three_platforms_and_json_keys(self):
        prompt = build_vision_mobile_analysis_prompt(None)
        assert "SwiftUI" in prompt
        assert "Compose" in prompt
        assert "Flutter" in prompt
        for key in (
            '"layout_summary"',
            '"color_observations"',
            '"detected_components"',
            '"suggested_swiftui"',
            '"suggested_compose"',
            '"suggested_flutter"',
            '"accessibility_notes"',
        ):
            assert key in prompt

    def test_prompt_warns_against_deprecated_apis(self):
        prompt = build_vision_mobile_analysis_prompt(None)
        # The prompt must steer the model away from deprecated forms the
        # mobile_component_registry rejects.
        assert "NavigationView" in prompt
        assert "BottomNavigation" in prompt or "BottomNavigationBar" in prompt


# ── Generation prompt determinism ────────────────────────────────────


def _sample_analysis() -> MobileVisionAnalysis:
    return MobileVisionAnalysis(
        layout_summary="header + 3-row settings list + bottom nav",
        color_observations=("dark surface", "cyan accent"),
        detected_components=("settings row", "bottom nav"),
        suggested_swiftui=("NavigationStack", "List", "TabView"),
        suggested_compose=("Scaffold", "NavigationBar", "ListItem"),
        suggested_flutter=("Scaffold", "NavigationBar", "ListView"),
        accessibility_notes=("icon-only back button needs label",),
        parse_succeeded=True,
    )


class TestGenerationPromptDeterminism:
    def test_same_inputs_byte_identical(self):
        a = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT,
            brief="settings screen",
        )
        b = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT,
            brief="settings screen",
        )
        assert a == b

    def test_brief_changes_prompt(self):
        a = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief="a",
        )
        b = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief="b",
        )
        assert a != b

    def test_prompt_contains_rules_block(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        assert "Generation rules" in prompt
        assert "```swift" in prompt
        assert "```kotlin" in prompt
        assert "```dart" in prompt

    def test_prompt_contains_analysis_block(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        assert "Vision analysis" in prompt
        assert "header + 3-row settings list" in prompt
        assert "Suggested SwiftUI views" in prompt
        assert "NavigationStack" in prompt

    def test_prompt_injects_registry_and_tokens(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        assert "Mobile component registry" in prompt
        # Representative entry from the live registry per platform.
        assert "NavigationStack" in prompt
        assert "Scaffold" in prompt

    def test_empty_analysis_still_renders(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        assert "Vision analysis" in prompt
        assert "(not extracted)" in prompt
        assert "(none noted)" in prompt

    def test_caller_brief_renders_when_present(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief="login screen with phone OTP",
        )
        assert "login screen with phone OTP" in prompt

    def test_caller_brief_renders_none_when_absent(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "## Caller brief" in prompt
        assert "(none)" in prompt

    def test_target_platforms_listed_explicitly(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        assert "Target platforms" in prompt
        for plat in TARGET_PLATFORMS:
            assert plat in prompt

    def test_platforms_narrowing(self):
        prompt_all = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief=None,
        )
        prompt_one = build_mobile_generation_prompt_from_vision(
            analysis=_sample_analysis(),
            project_root=PROJECT_ROOT, brief=None,
            platforms=("swiftui",),
        )
        assert prompt_all != prompt_one
        # The narrowed prompt's mobile registry block must NOT include
        # the Flutter header since that platform was filtered out.
        assert "Flutter 3.22+" not in prompt_one
        assert "Flutter 3.22+" in prompt_all

    def test_platforms_rejects_unknown(self):
        with pytest.raises(ValueError):
            build_mobile_generation_prompt_from_vision(
                analysis=_sample_analysis(),
                project_root=PROJECT_ROOT, brief=None,
                platforms=("android",),
            )

    def test_platforms_rejects_empty(self):
        with pytest.raises(ValueError):
            build_mobile_generation_prompt_from_vision(
                analysis=_sample_analysis(),
                project_root=PROJECT_ROOT, brief=None,
                platforms=(),
            )


# ── parse_mobile_vision_analysis() ───────────────────────────────────


class TestParseMobileVisionAnalysis:
    def test_parses_bare_json(self):
        analysis = parse_mobile_vision_analysis(_VALID_JSON_RESPONSE)
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Phone (compact)")
        assert "NavigationStack" in analysis.suggested_swiftui
        assert "Scaffold" in analysis.suggested_compose
        assert "FilledButton" in analysis.suggested_flutter

    def test_parses_fenced_json(self):
        text = (
            "Sure, here's the analysis:\n```json\n"
            + _VALID_JSON_RESPONSE
            + "\n```\nhope this helps"
        )
        analysis = parse_mobile_vision_analysis(text)
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Phone (compact)")

    def test_parses_json_embedded_in_prose(self):
        text = (
            "My analysis:\n\n"
            + _VALID_JSON_RESPONSE
            + "\n\nLet me know!"
        )
        analysis = parse_mobile_vision_analysis(text)
        assert analysis.parse_succeeded
        assert "settings rows with chevrons" in " ".join(
            analysis.detected_components
        )

    def test_salvages_prose_keys(self):
        text = (
            "Layout: hero card then list of rows then bottom tab bar\n"
            "Colors: dark base, cyan accent\n"
            "Components: hero card, list row, bottom tab\n"
            "SwiftUI: NavigationStack, List, TabView\n"
            "Compose: Scaffold, NavigationBar\n"
            "Flutter: Scaffold, NavigationBar\n"
            "A11y: icon-only back button needs accessibilityLabel\n"
        )
        analysis = parse_mobile_vision_analysis(text)
        # Salvage does NOT declare success.
        assert not analysis.parse_succeeded
        assert "hero card" in analysis.layout_summary
        # The salvaged values come back lowercase / single-line — we just
        # check that the comma-separated values were split correctly.
        assert any("NavigationStack" in s for s in analysis.suggested_swiftui)
        assert any("Scaffold" in s for s in analysis.suggested_compose)
        assert any("Scaffold" in s for s in analysis.suggested_flutter)

    def test_empty_input_returns_empty_analysis(self):
        analysis = parse_mobile_vision_analysis("")
        assert analysis.layout_summary == ""
        assert analysis.parse_succeeded is False
        assert analysis.raw_text == ""

    def test_garbage_input_returns_empty_analysis(self):
        analysis = parse_mobile_vision_analysis("lolwut {{not json}}")
        assert not analysis.parse_succeeded
        assert analysis.raw_text == "lolwut {{not json}}"

    def test_accepts_comma_string_lists(self):
        resp = json.dumps({
            "layout_summary": "x",
            "color_observations": "cyan\n- dark base",
            "detected_components": ["a", "b"],
            "suggested_swiftui": [],
            "suggested_compose": [],
            "suggested_flutter": [],
            "accessibility_notes": "carousel needs pause",
        })
        analysis = parse_mobile_vision_analysis(resp)
        assert analysis.parse_succeeded
        assert analysis.color_observations == ("cyan", "dark base")
        assert analysis.accessibility_notes == ("carousel needs pause",)

    def test_key_aliases_tolerated(self):
        # Models often emit shortened keys ("ios" / "android" / "flutter").
        resp = json.dumps({
            "layout": "alias test",
            "colours": ["cyan"],
            "widgets": ["button"],
            "ios": ["NavigationStack"],
            "android": ["Scaffold"],
            "flutter": ["Scaffold"],
            "a11y": ["icon-only needs label"],
        })
        analysis = parse_mobile_vision_analysis(resp)
        assert analysis.parse_succeeded
        assert analysis.layout_summary == "alias test"
        assert analysis.color_observations == ("cyan",)
        assert analysis.suggested_swiftui == ("NavigationStack",)
        assert analysis.suggested_compose == ("Scaffold",)
        assert analysis.suggested_flutter == ("Scaffold",)

    def test_ignores_extra_keys_by_default(self):
        resp = json.dumps({
            "layout_summary": "x",
            "random_extra": "ignored",
        })
        analysis = parse_mobile_vision_analysis(resp)
        assert analysis.extras == {}

    def test_captures_requested_extras(self):
        resp = json.dumps({
            "layout_summary": "x",
            "density": "high",
        })
        analysis = parse_mobile_vision_analysis(
            resp, raw_extras_keys=["density"],
        )
        assert dict(analysis.extras) == {"density": "high"}


# ── extract_mobile_code_from_response() (re-exported) ────────────────


class TestExtractMobileCode:
    def test_clean_three_platform_response(self):
        outputs = extract_mobile_code_from_response(_CLEAN_THREE_PLATFORM_RESPONSE)
        assert "SettingsView" in outputs.swift
        assert "@Composable" in outputs.kotlin
        assert "StatelessWidget" in outputs.dart
        assert outputs.is_complete

    def test_only_swift_present(self):
        text = "```swift\nimport SwiftUI\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "import SwiftUI" in outputs.swift
        assert outputs.kotlin == ""
        assert outputs.dart == ""
        assert set(outputs.missing_platforms()) == {"compose", "flutter"}

    def test_swiftui_alias_accepted(self):
        text = "```swiftui\nstruct V: View { var body: some View { Text(\"hi\") } }\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "struct V" in outputs.swift

    def test_compose_aliases(self):
        for alias in ("kotlin", "kt", "compose"):
            text = f"```{alias}\n@Composable\nfun X() {{}}\n```\n"
            outputs = extract_mobile_code_from_response(text)
            assert "@Composable" in outputs.kotlin, f"alias {alias} failed"

    def test_flutter_alias(self):
        text = "```flutter\nclass X extends StatelessWidget {}\n```\n"
        outputs = extract_mobile_code_from_response(text)
        assert "StatelessWidget" in outputs.dart


# ── Pipeline: analyze_mobile_screenshot ──────────────────────────────


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


class TestAnalyzeMobileScreenshot:
    def test_happy_path(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        analysis = analyze_mobile_screenshot(
            _PNG_1X1, "image/png", invoker=inv,
        )
        assert analysis.parse_succeeded
        assert "NavigationStack" in analysis.suggested_swiftui
        assert len(inv.calls) == 1
        # Message is a HumanMessage with [text, image] content.
        msgs = inv.calls[0]
        assert len(msgs) == 1
        content = msgs[0].content
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image"

    def test_accepts_prevalidated_vision_image(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        img = validate_image(_PNG_1X1, "image/png")
        analysis = analyze_mobile_screenshot(img, invoker=inv)
        assert analysis.parse_succeeded

    def test_llm_unavailable_returns_empty_analysis(self):
        inv = FakeInvoker([])  # always returns ""
        analysis = analyze_mobile_screenshot(
            _PNG_1X1, "image/png", invoker=inv,
        )
        assert not analysis.parse_succeeded
        assert analysis.raw_text == ""

    def test_invalid_image_raises(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        with pytest.raises(ValueError):
            analyze_mobile_screenshot(b"", "image/png", invoker=inv)

    def test_hint_included_in_prompt(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        analyze_mobile_screenshot(
            _PNG_1X1, "image/png",
            hint="focus on the bottom nav", invoker=inv,
        )
        prompt = inv.calls[0][0].content[0]["text"]
        assert "focus on the bottom nav" in prompt


# ── Pipeline: generate_mobile_from_vision ────────────────────────────


class TestGenerateMobileFromVision:
    def test_happy_path_three_platforms(self):
        inv = FakeInvoker([
            _VALID_JSON_RESPONSE,
            _CLEAN_THREE_PLATFORM_RESPONSE,
        ])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            brief="settings screen",
            invoker=inv,
        )
        assert isinstance(result, MobileVisionGenerationResult)
        assert result.analysis.parse_succeeded
        assert result.is_complete
        assert "SettingsView" in result.outputs.swift
        assert "@Composable" in result.outputs.kotlin
        assert "StatelessWidget" in result.outputs.dart
        assert "llm_unavailable" not in result.warnings
        assert len(inv.calls) == 2  # analysis + generation

    def test_llm_unavailable_on_analysis(self):
        inv = FakeInvoker([])  # empty right away
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert not result.is_complete
        assert "llm_unavailable" in result.warnings
        assert result.outputs.swift == ""
        # Single call: analysis returned empty → we short-circuit.
        assert len(inv.calls) == 1

    def test_llm_unavailable_on_generation(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, ""])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert not result.is_complete
        assert "llm_unavailable" in result.warnings
        # Analysis was preserved.
        assert result.analysis.parse_succeeded

    def test_partial_response_emits_missing_warnings(self):
        partial = (
            "```swift\n"
            "import SwiftUI\n"
            "struct V: View { var body: some View { Text(\"x\") } }\n"
            "```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, partial])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "import SwiftUI" in result.outputs.swift
        assert not result.is_complete
        assert "compose_missing" in result.warnings
        assert "flutter_missing" in result.warnings
        assert "swiftui_missing" not in result.warnings

    def test_preprovided_analysis_skips_first_call(self):
        analysis = parse_mobile_vision_analysis(_VALID_JSON_RESPONSE)
        inv = FakeInvoker([_CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            analysis=analysis,
        )
        assert len(inv.calls) == 1
        assert result.analysis is analysis
        assert result.is_complete

    def test_invalid_image_raises_before_llm(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, _CLEAN_THREE_PLATFORM_RESPONSE])
        with pytest.raises(ValueError):
            generate_mobile_from_vision(
                b"", "image/png",
                project_root=PROJECT_ROOT,
                invoker=inv,
            )
        assert inv.calls == []

    def test_result_carries_model_and_provider(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, _CLEAN_THREE_PLATFORM_RESPONSE])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            model="claude-opus-4-7",
            provider="anthropic",
        )
        assert result.model == "claude-opus-4-7"
        assert result.provider == "anthropic"

    def test_platforms_narrow_skips_missing_warnings(self):
        # When caller only asked for swiftui, compose-missing /
        # flutter-missing must NOT be emitted.
        swift_only = (
            "```swift\n"
            "import SwiftUI\n"
            "struct V: View { var body: some View { Text(\"x\") } }\n"
            "```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, swift_only])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            platforms=("swiftui",),
        )
        assert "compose_missing" not in result.warnings
        assert "flutter_missing" not in result.warnings
        assert "swiftui_missing" not in result.warnings
        assert "import SwiftUI" in result.outputs.swift

    def test_analysis_parse_failed_propagates_warning(self):
        # First response is unparseable prose → analysis salvages but
        # parse_succeeded=False; pipeline should still attempt generation
        # and emit "analysis_parse_failed".
        inv = FakeInvoker([
            "I can't quite see the screen but it looks like a phone.",
            _CLEAN_THREE_PLATFORM_RESPONSE,
        ])
        result = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "analysis_parse_failed" in result.warnings
        assert result.is_complete  # generation succeeded anyway


# ── run_vision_to_mobile() agent entry point ─────────────────────────


class TestRunVisionToMobile:
    def test_returns_json_safe_dict(self):
        inv = FakeInvoker([
            _VALID_JSON_RESPONSE,
            _CLEAN_THREE_PLATFORM_RESPONSE,
        ])
        out = run_vision_to_mobile(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert isinstance(out, dict)
        json.dumps(out)  # must serialise without a custom encoder
        assert out["schema_version"] == VISION_MOBILE_SCHEMA_VERSION
        assert "analysis" in out
        assert "outputs" in out
        assert "warnings" in out
        assert out["is_complete"] is True

    def test_surfaces_llm_unavailable(self):
        inv = FakeInvoker([])
        out = run_vision_to_mobile(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "llm_unavailable" in out["warnings"]
        assert out["outputs"]["swift"] == ""
        assert out["outputs"]["kotlin"] == ""
        assert out["outputs"]["dart"] == ""
        assert out["is_complete"] is False

    def test_platforms_narrow_returned_in_dict(self):
        swift_only = (
            "```swift\nimport SwiftUI\nstruct V: View { var body: some View { Text(\"x\") } }\n```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, swift_only])
        out = run_vision_to_mobile(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            platforms=("swiftui",),
        )
        assert "import SwiftUI" in out["outputs"]["swift"]
        assert "compose_missing" not in out["warnings"]
        assert "flutter_missing" not in out["warnings"]


# ── Default invoker wiring (no network — mocks invoke_chat) ──────────


class TestDefaultInvokerWiring:
    def test_default_invoker_calls_invoke_chat_with_requested_model(self):
        seen: dict = {}

        def _fake_invoke_chat(messages, *, provider=None, model=None, llm=None):
            seen["provider"] = provider
            seen["model"] = model
            return _VALID_JSON_RESPONSE

        with patch("backend.llm_adapter.invoke_chat", _fake_invoke_chat):
            analysis = analyze_mobile_screenshot(
                _PNG_1X1, "image/png",
                provider="anthropic", model="claude-opus-4-7",
            )
        assert analysis.parse_succeeded
        assert seen["provider"] == "anthropic"
        assert seen["model"] == "claude-opus-4-7"

    def test_default_invoker_swallows_network_errors(self):
        def _boom(messages, *, provider=None, model=None, llm=None):
            raise RuntimeError("network down")

        with patch("backend.llm_adapter.invoke_chat", _boom):
            result = generate_mobile_from_vision(
                _PNG_1X1, "image/png",
                project_root=PROJECT_ROOT,
            )
        assert "llm_unavailable" in result.warnings
        assert not result.is_complete


# ── Cross-module integration (sibling modules still in play) ─────────


class TestSiblingIntegration:
    def test_generation_prompt_references_live_mobile_registry(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        # Headers the registry's render_agent_context_block emits.
        assert "Mobile component registry" in prompt
        assert "SwiftUI (iOS 16+)" in prompt
        assert "Jetpack Compose" in prompt
        assert "Flutter" in prompt
        # Representative entries from each platform.
        assert "NavigationStack" in prompt
        assert "Scaffold" in prompt

    def test_generation_prompt_references_design_tokens(self):
        prompt = build_mobile_generation_prompt_from_vision(
            analysis=MobileVisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        # The live design-token loader produces a "Design tokens" heading.
        # We don't pin the exact token names (those live in globals.css and
        # are pinned by sibling test_design_token_loader).
        assert "Design tokens" in prompt or "design token" in prompt.lower()

    def test_target_platforms_identical_to_registry(self):
        # V5 #4 must NOT drift from the registry's three-platform contract.
        # If someone adds a fourth platform, this test fails noisily.
        assert TARGET_PLATFORMS == REGISTRY_PLATFORMS

    def test_outputs_dataclass_is_shared_with_figma_to_mobile(self):
        # A downstream caller can take a MobileCodeOutputs from either the
        # screenshot pipeline or the Figma pipeline and pass it to the
        # same handler — no isinstance() checks required.
        outputs_from_vision = generate_mobile_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=FakeInvoker([
                _VALID_JSON_RESPONSE,
                _CLEAN_THREE_PLATFORM_RESPONSE,
            ]),
        ).outputs
        assert isinstance(outputs_from_vision, FigmaMobileCodeOutputs)

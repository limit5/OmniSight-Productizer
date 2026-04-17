"""V1 #5 (issue #317) — vision_to_ui pipeline contract tests.

Pins ``backend/vision_to_ui.py`` against:

  * structural invariants of :class:`VisionImage`,
    :class:`VisionAnalysis`, :class:`VisionGenerationResult`
    (frozen, validated, JSON-safe);
  * image validation (supported mime types, size cap, mime/payload
    agreement);
  * deterministic prompt construction (byte-identical across calls)
    for both analysis and generation stages;
  * multimodal message assembly (correct base64 image block shape);
  * tolerant response parsing — fenced JSON, bare JSON, prose
    fallback — and graceful degradation when the model returns
    nothing or garbage;
  * TSX extraction across the common fence language variants;
  * the full :func:`generate_ui_from_vision` pipeline with an
    injected fake chat invoker (no network): successful generation,
    LLM-unavailable fallback, TSX-missing fallback, auto-fix round,
    and integration with the component-consistency linter.

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

from backend import vision_to_ui as vtu
from backend.vision_to_ui import (
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_PROVIDER,
    MAX_IMAGE_BYTES,
    SUPPORTED_MIME_TYPES,
    VISION_SCHEMA_VERSION,
    VisionAnalysis,
    VisionGenerationResult,
    VisionImage,
    analyze_screenshot,
    build_multimodal_message,
    build_ui_generation_prompt,
    build_vision_analysis_prompt,
    extract_tsx_from_response,
    generate_ui_from_vision,
    parse_vision_analysis,
    run_vision_to_ui,
    validate_image,
)

from backend.component_consistency_linter import LintReport


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Fixtures ─────────────────────────────────────────────────────────


# Smallest legal PNG (1×1 transparent pixel, generated once and
# hard-coded here to keep the test suite hermetic — no PIL dep).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # minimally JPEG-ish prefix
_GIF_89A = b"GIF89a" + b"\x00" * 20
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20


# ── Module invariants ────────────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_is_semver(self):
        parts = VISION_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_default_vision_model_is_opus_4_7(self):
        assert DEFAULT_VISION_MODEL.startswith("claude-opus-4-7")
        assert DEFAULT_VISION_PROVIDER == "anthropic"

    def test_supported_mime_types_is_frozen(self):
        assert isinstance(SUPPORTED_MIME_TYPES, frozenset)
        assert SUPPORTED_MIME_TYPES == frozenset({
            "image/png", "image/jpeg", "image/gif", "image/webp",
        })

    def test_max_image_bytes_is_5_mib(self):
        assert MAX_IMAGE_BYTES == 5 * 1024 * 1024

    def test_public_surface_exports(self):
        for name in (
            "VISION_SCHEMA_VERSION",
            "DEFAULT_VISION_MODEL",
            "MAX_IMAGE_BYTES",
            "SUPPORTED_MIME_TYPES",
            "VisionImage",
            "VisionAnalysis",
            "VisionGenerationResult",
            "validate_image",
            "build_multimodal_message",
            "build_vision_analysis_prompt",
            "build_ui_generation_prompt",
            "parse_vision_analysis",
            "extract_tsx_from_response",
            "analyze_screenshot",
            "generate_ui_from_vision",
            "run_vision_to_ui",
        ):
            assert name in vtu.__all__, f"{name} must be in __all__"


# ── VisionImage invariants ───────────────────────────────────────────


class TestVisionImage:
    def test_frozen(self):
        img = VisionImage(data=_PNG_1X1, mime_type="image/png")
        with pytest.raises(Exception):
            img.data = b""  # type: ignore[misc]

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            VisionImage(data=b"", mime_type="image/png")

    def test_rejects_unknown_mime(self):
        with pytest.raises(ValueError):
            VisionImage(data=_PNG_1X1, mime_type="image/svg+xml")

    def test_rejects_non_bytes(self):
        with pytest.raises(TypeError):
            VisionImage(data="not bytes", mime_type="image/png")  # type: ignore[arg-type]

    def test_rejects_oversize(self):
        with pytest.raises(ValueError):
            VisionImage(data=b"\x89PNG" + b"\x00" * MAX_IMAGE_BYTES,
                        mime_type="image/png")

    def test_size_and_b64_accessors(self):
        img = VisionImage(data=_PNG_1X1, mime_type="image/png")
        assert img.size_bytes == len(_PNG_1X1)
        decoded = base64.b64decode(img.to_base64())
        assert decoded == _PNG_1X1


# ── validate_image() ─────────────────────────────────────────────────


class TestValidateImage:
    def test_accepts_legal_png(self):
        img = validate_image(_PNG_1X1, "image/png", source="a.png")
        assert img.mime_type == "image/png"
        assert img.source == "a.png"

    def test_normalises_jpg_to_jpeg(self):
        img = validate_image(_JPEG_MAGIC, "image/jpg")
        assert img.mime_type == "image/jpeg"

    def test_rejects_non_bytes(self):
        with pytest.raises(TypeError):
            validate_image("hello", "image/png")  # type: ignore[arg-type]

    def test_rejects_unknown_mime(self):
        with pytest.raises(ValueError):
            validate_image(_PNG_1X1, "image/bmp")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_image(b"", "image/png")

    def test_rejects_oversize(self):
        with pytest.raises(ValueError):
            validate_image(b"\x89PNG" + b"\x00" * MAX_IMAGE_BYTES,
                           "image/png")

    def test_rejects_mime_payload_mismatch(self):
        # JPEG payload declared as PNG → sniff catches it.
        with pytest.raises(ValueError):
            validate_image(_JPEG_MAGIC, "image/png")

    @pytest.mark.parametrize("payload,mime", [
        (_PNG_1X1, "image/png"),
        (_JPEG_MAGIC, "image/jpeg"),
        (_GIF_89A, "image/gif"),
        (_WEBP, "image/webp"),
    ])
    def test_all_supported_formats_accepted(self, payload, mime):
        img = validate_image(payload, mime)
        assert img.mime_type == mime

    def test_unknown_payload_falls_back_to_declared(self):
        # A payload we don't sniff (e.g. synthetic) is accepted as
        # declared — we can't prove it's wrong.
        synthetic = b"\x00\x01\x02\x03\x04\x05" * 4
        img = validate_image(synthetic, "image/png")
        assert img.mime_type == "image/png"


# ── build_multimodal_message() ───────────────────────────────────────


class TestMultimodalMessage:
    def test_content_is_text_then_image(self):
        img = validate_image(_PNG_1X1, "image/png")
        msg = build_multimodal_message(img, "describe this")
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert msg.content[0] == {"type": "text", "text": "describe this"}
        image_block = msg.content[1]
        assert image_block["type"] == "image"
        assert image_block["source"]["type"] == "base64"
        assert image_block["source"]["media_type"] == "image/png"
        assert base64.b64decode(image_block["source"]["data"]) == _PNG_1X1

    def test_roundtrip_base64(self):
        img = validate_image(_JPEG_MAGIC, "image/jpeg")
        msg = build_multimodal_message(img, "x")
        raw = base64.b64decode(msg.content[1]["source"]["data"])
        assert raw == _JPEG_MAGIC


# ── Prompt determinism ───────────────────────────────────────────────


class TestAnalysisPromptDeterminism:
    def test_same_hint_same_prompt(self):
        a = build_vision_analysis_prompt("keep focus on header")
        b = build_vision_analysis_prompt("keep focus on header")
        assert a == b

    def test_hint_changes_prompt(self):
        a = build_vision_analysis_prompt(None)
        b = build_vision_analysis_prompt("different context")
        assert a != b

    def test_prompt_mentions_json_shape(self):
        prompt = build_vision_analysis_prompt(None)
        for key in (
            '"layout_summary"',
            '"color_observations"',
            '"detected_components"',
            '"suggested_primitives"',
            '"accessibility_notes"',
        ):
            assert key in prompt

    def test_empty_hint_is_equivalent_to_none(self):
        a = build_vision_analysis_prompt(None)
        b = build_vision_analysis_prompt("")
        c = build_vision_analysis_prompt("   \n  ")
        assert a == b == c


class TestGenerationPromptDeterminism:
    def _analysis(self) -> VisionAnalysis:
        return VisionAnalysis(
            layout_summary="header + 3-col card grid + footer",
            color_observations=("dark background", "cyan CTA"),
            detected_components=("primary button", "card"),
            suggested_primitives=("button", "card"),
            accessibility_notes=("icon-only close button needs aria-label",),
            parse_succeeded=True,
        )

    def test_same_inputs_byte_identical(self):
        analysis = self._analysis()
        a = build_ui_generation_prompt(
            analysis=analysis,
            project_root=PROJECT_ROOT,
            brief="pricing page, 3 plans",
        )
        b = build_ui_generation_prompt(
            analysis=analysis,
            project_root=PROJECT_ROOT,
            brief="pricing page, 3 plans",
        )
        assert a == b

    def test_brief_changes_prompt(self):
        analysis = self._analysis()
        a = build_ui_generation_prompt(
            analysis=analysis, project_root=PROJECT_ROOT, brief="a",
        )
        b = build_ui_generation_prompt(
            analysis=analysis, project_root=PROJECT_ROOT, brief="b",
        )
        assert a != b

    def test_prompt_contains_rules_block(self):
        prompt = build_ui_generation_prompt(
            analysis=self._analysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "Generation rules" in prompt
        assert "dark-only" in prompt.lower()
        assert "```tsx" in prompt
        assert "shadcn" in prompt.lower()

    def test_prompt_contains_analysis_block(self):
        prompt = build_ui_generation_prompt(
            analysis=self._analysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "Vision analysis" in prompt
        assert "header + 3-col card grid + footer" in prompt
        assert "cyan CTA" in prompt
        assert "aria-label" in prompt

    def test_prompt_injects_registry_and_tokens(self):
        prompt = build_ui_generation_prompt(
            analysis=self._analysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        # Registry block header + at least one component we know ships
        # in this repo's components/ui/.
        assert "button" in prompt.lower()
        # Design tokens block header + at least one shadcn semantic
        # palette token (these are pinned by the sibling test suite
        # as present in app/globals.css).
        assert "primary" in prompt.lower()

    def test_empty_analysis_still_renders(self):
        prompt = build_ui_generation_prompt(
            analysis=VisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "Vision analysis" in prompt
        assert "(not extracted)" in prompt
        assert "(none noted)" in prompt


# ── parse_vision_analysis() ──────────────────────────────────────────


_VALID_JSON_RESPONSE = json.dumps({
    "layout_summary": "Header + sidebar + main + footer",
    "color_observations": ["dark slate background", "cyan accent"],
    "detected_components": ["CTA button", "sidebar nav", "data table"],
    "suggested_primitives": ["button", "sidebar", "table"],
    "accessibility_notes": ["icon-only button — needs aria-label"],
})


class TestParseVisionAnalysis:
    def test_parses_bare_json(self):
        analysis = parse_vision_analysis(_VALID_JSON_RESPONSE)
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Header +")
        assert analysis.color_observations == (
            "dark slate background", "cyan accent",
        )
        assert "button" in analysis.suggested_primitives

    def test_parses_fenced_json(self):
        text = "Sure!\n```json\n" + _VALID_JSON_RESPONSE + "\n```\nhope this helps"
        analysis = parse_vision_analysis(text)
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Header +")

    def test_parses_json_embedded_in_prose(self):
        text = (
            "My analysis:\n\n"
            + _VALID_JSON_RESPONSE
            + "\n\nLet me know if that works!"
        )
        analysis = parse_vision_analysis(text)
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Header +")

    def test_salvages_prose_keys(self):
        text = (
            "Layout: hero banner then 2-col card grid\n"
            "Colors: dark base, amber highlights\n"
            "Components: hero CTA, testimonial carousel\n"
            "Shadcn: button, carousel, card\n"
            "A11y: pause button required on carousel\n"
        )
        analysis = parse_vision_analysis(text)
        # Salvage does NOT declare success — downstream can see it's
        # untrusted and ask for a retry.
        assert not analysis.parse_succeeded
        assert "hero banner" in analysis.layout_summary

    def test_empty_input_returns_empty_analysis(self):
        analysis = parse_vision_analysis("")
        assert analysis.layout_summary == ""
        assert analysis.parse_succeeded is False
        assert analysis.raw_text == ""

    def test_garbage_input_returns_empty_analysis(self):
        analysis = parse_vision_analysis("lolwut {{not json}}")
        assert not analysis.parse_succeeded
        assert analysis.raw_text == "lolwut {{not json}}"

    def test_accepts_comma_string_lists(self):
        resp = json.dumps({
            "layout_summary": "x",
            "color_observations": "cyan\n- dark base",
            "detected_components": ["a", "b"],
            "suggested_primitives": [],
            "accessibility_notes": "carousel needs pause",
        })
        analysis = parse_vision_analysis(resp)
        assert analysis.parse_succeeded
        assert analysis.color_observations == ("cyan", "dark base")
        assert analysis.accessibility_notes == ("carousel needs pause",)

    def test_key_aliases_tolerated(self):
        resp = json.dumps({
            "layout": "alias test",
            "colours": ["cyan"],
            "widgets": ["button"],
            "primitives": ["button"],
            "a11y": ["notes"],
        })
        analysis = parse_vision_analysis(resp)
        assert analysis.parse_succeeded
        assert analysis.layout_summary == "alias test"
        assert analysis.color_observations == ("cyan",)

    def test_ignores_extra_keys_by_default(self):
        resp = json.dumps({
            "layout_summary": "x",
            "random_extra": "ignored",
        })
        analysis = parse_vision_analysis(resp)
        assert analysis.extras == {}

    def test_captures_requested_extras(self):
        resp = json.dumps({
            "layout_summary": "x",
            "density": "high",
        })
        analysis = parse_vision_analysis(
            resp, raw_extras_keys=["density"],
        )
        assert analysis.extras == {"density": "high"}


# ── extract_tsx_from_response() ──────────────────────────────────────


class TestExtractTsx:
    def test_tsx_fence(self):
        text = "Here:\n```tsx\nexport default function X() { return <div/>; }\n```\n"
        tsx = extract_tsx_from_response(text)
        assert "export default function X()" in tsx
        assert tsx.endswith("\n")

    @pytest.mark.parametrize("lang", ["tsx", "jsx", "ts", "typescript", "javascript"])
    def test_accepts_all_lang_variants(self, lang):
        text = f"```{lang}\n<div/>\n```"
        tsx = extract_tsx_from_response(text)
        assert "<div/>" in tsx

    def test_fallback_to_langless_fence_with_jsx(self):
        text = "```\n<div>hi</div>\n```"
        tsx = extract_tsx_from_response(text)
        assert "<div>hi</div>" in tsx

    def test_last_resort_slice(self):
        text = "Here it is: <div>hi</div> bye"
        tsx = extract_tsx_from_response(text)
        assert "<div>hi</div>" in tsx

    def test_empty_input_returns_empty(self):
        assert extract_tsx_from_response("") == ""
        assert extract_tsx_from_response("just prose, no tags") == ""

    def test_picks_tsx_over_json(self):
        text = (
            "```json\n{\"ok\":true}\n```\n"
            "```tsx\nfunction X(){return <p/>}\n```"
        )
        tsx = extract_tsx_from_response(text)
        assert "function X()" in tsx
        assert "ok" not in tsx


# ── VisionAnalysis / VisionGenerationResult structure ────────────────


class TestDataclasses:
    def test_analysis_frozen(self):
        a = VisionAnalysis(layout_summary="x")
        with pytest.raises(Exception):
            a.layout_summary = "y"  # type: ignore[misc]

    def test_analysis_to_dict_json_safe(self):
        a = VisionAnalysis(
            layout_summary="l",
            color_observations=("c",),
            detected_components=("d",),
            suggested_primitives=("p",),
            accessibility_notes=("n",),
            raw_text="raw",
            parse_succeeded=True,
            extras={"foo": 1},
        )
        d = a.to_dict()
        s = json.dumps(d)  # must not raise
        round = json.loads(s)
        assert round["layout_summary"] == "l"
        assert round["parse_succeeded"] is True
        assert round["extras"] == {"foo": 1}
        assert round["schema_version"] == VISION_SCHEMA_VERSION

    def test_result_to_dict_json_safe(self):
        a = VisionAnalysis(layout_summary="x", parse_succeeded=True)
        r = VisionGenerationResult(
            analysis=a,
            tsx_code="<Button>ok</Button>",
            lint_report=LintReport(),
            pre_fix_lint_report=None,
            auto_fix_applied=False,
            warnings=("tsx_missing",),
            model="claude-opus-4-7",
            provider="anthropic",
        )
        d = r.to_dict()
        s = json.dumps(d)
        round = json.loads(s)
        assert round["tsx_code"] == "<Button>ok</Button>"
        assert round["warnings"] == ["tsx_missing"]
        assert round["lint_report"]["is_clean"] is True
        assert round["schema_version"] == VISION_SCHEMA_VERSION

    def test_result_is_clean_requires_non_empty_tsx(self):
        r = VisionGenerationResult(
            analysis=VisionAnalysis(),
            tsx_code="",
            lint_report=LintReport(),
        )
        assert not r.is_clean  # empty tsx is not "clean"

        r2 = VisionGenerationResult(
            analysis=VisionAnalysis(),
            tsx_code="<Button>ok</Button>\n",
            lint_report=LintReport(),
        )
        assert r2.is_clean


# ── Pipeline: analyze_screenshot ─────────────────────────────────────


class FakeInvoker:
    """Deterministic chat-invoker double for pipeline tests."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list] = []

    def __call__(self, messages: list) -> str:
        self.calls.append(messages)
        if not self._responses:
            return ""
        return self._responses.pop(0)


class TestAnalyzeScreenshot:
    def test_happy_path(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        analysis = analyze_screenshot(
            _PNG_1X1, "image/png", invoker=inv,
        )
        assert analysis.parse_succeeded
        assert analysis.layout_summary.startswith("Header +")
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
        analysis = analyze_screenshot(img, invoker=inv)
        assert analysis.parse_succeeded

    def test_llm_unavailable_returns_empty_analysis(self):
        inv = FakeInvoker([])  # always returns ""
        analysis = analyze_screenshot(
            _PNG_1X1, "image/png", invoker=inv,
        )
        assert not analysis.parse_succeeded
        assert analysis.raw_text == ""

    def test_invalid_image_raises(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        with pytest.raises(ValueError):
            analyze_screenshot(b"", "image/png", invoker=inv)

    def test_hint_included_in_prompt(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE])
        analyze_screenshot(
            _PNG_1X1, "image/png", hint="focus on the header", invoker=inv,
        )
        prompt = inv.calls[0][0].content[0]["text"]
        assert "focus on the header" in prompt


# ── Pipeline: generate_ui_from_vision ────────────────────────────────


class TestGenerateUiFromVision:
    def _clean_tsx_response(self) -> str:
        return (
            "Here's the rebuilt component:\n\n"
            "```tsx\n"
            "import { Button } from \"@/components/ui/button\";\n"
            "\n"
            "export default function Page() {\n"
            "  return (\n"
            "    <div className=\"flex flex-col gap-4 bg-background text-foreground p-6\">\n"
            "      <Button variant=\"default\">Click me</Button>\n"
            "    </div>\n"
            "  );\n"
            "}\n"
            "```\n"
        )

    def test_happy_path_clean_tsx(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, self._clean_tsx_response()])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            brief="pricing page, 3 plans",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert isinstance(result, VisionGenerationResult)
        assert result.analysis.parse_succeeded
        assert "<Button" in result.tsx_code
        assert result.lint_report.is_clean
        assert not result.auto_fix_applied
        assert result.warnings == ()
        assert result.is_clean
        assert len(inv.calls) == 2  # analysis + generation

    def test_llm_unavailable_fallback_on_analysis(self):
        inv = FakeInvoker([])  # empty right away
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert result.tsx_code == ""
        assert "llm_unavailable" in result.warnings
        assert not result.is_clean

    def test_llm_unavailable_fallback_on_generation(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, ""])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert result.tsx_code == ""
        assert "llm_unavailable" in result.warnings
        assert result.analysis.parse_succeeded

    def test_tsx_missing_fallback_preserves_raw(self):
        inv = FakeInvoker([
            _VALID_JSON_RESPONSE,
            "Sorry, I can't parse this image — please upload a clearer one.",
        ])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "tsx_missing" in result.warnings
        assert "Sorry" in result.tsx_code  # raw preserved for inspection

    def test_auto_fix_round_rewrites_raw_button(self):
        # Response contains a raw <button> — linter flags it, auto-fix
        # mechanical swap should rewrite to <Button>.
        dirty_response = (
            "```tsx\n"
            "export default function X({ handleSave }: { handleSave: () => void }) {\n"
            "  return <button onClick={handleSave}>Save</button>;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, dirty_response])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=True,
        )
        assert result.auto_fix_applied
        assert "<Button onClick={handleSave}>Save</Button>" in result.tsx_code
        # The opening raw <button> tag is gone.
        assert "<button " not in result.tsx_code
        assert "</button>" not in result.tsx_code
        assert "@/components/ui/button" in result.tsx_code
        assert result.pre_fix_lint_report is not None
        assert not result.pre_fix_lint_report.is_clean
        assert result.lint_report.is_clean

    def test_auto_fix_disabled_leaves_violation(self):
        dirty_response = (
            "```tsx\n"
            "export default function X() {\n"
            "  return <button>Save</button>;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, dirty_response])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=False,
        )
        assert not result.auto_fix_applied
        assert "<button" in result.tsx_code
        assert not result.lint_report.is_clean

    def test_preprovided_analysis_skips_first_call(self):
        analysis = parse_vision_analysis(_VALID_JSON_RESPONSE)
        inv = FakeInvoker([self._clean_tsx_response()])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            analysis=analysis,
        )
        assert len(inv.calls) == 1
        assert result.analysis is analysis
        assert result.is_clean

    def test_invalid_image_raises_before_llm(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, self._clean_tsx_response()])
        with pytest.raises(ValueError):
            generate_ui_from_vision(
                b"", "image/png",
                project_root=PROJECT_ROOT,
                invoker=inv,
            )
        assert inv.calls == []

    def test_result_carries_model_and_provider(self):
        inv = FakeInvoker([_VALID_JSON_RESPONSE, self._clean_tsx_response()])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            model="claude-opus-4-7",
            provider="anthropic",
        )
        assert result.model == "claude-opus-4-7"
        assert result.provider == "anthropic"


# ── run_vision_to_ui() agent entry point ─────────────────────────────


class TestRunVisionToUi:
    def test_returns_json_safe_dict(self):
        inv = FakeInvoker([
            _VALID_JSON_RESPONSE,
            "```tsx\nexport default () => <div>ok</div>;\n```",
        ])
        out = run_vision_to_ui(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert isinstance(out, dict)
        # Must be serialisable without a custom encoder.
        json.dumps(out)
        assert "analysis" in out
        assert "tsx_code" in out
        assert "lint_report" in out
        assert "warnings" in out
        assert out["schema_version"] == VISION_SCHEMA_VERSION

    def test_surfaces_llm_unavailable(self):
        inv = FakeInvoker([])
        out = run_vision_to_ui(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
        )
        assert "llm_unavailable" in out["warnings"]
        assert out["tsx_code"] == ""


# ── Default invoker wiring (no network — mocks invoke_chat) ──────────


class TestDefaultInvokerWiring:
    def test_default_invoker_calls_invoke_chat_with_requested_model(self):
        from backend import vision_to_ui as module

        seen: dict = {}

        def _fake_invoke_chat(messages, *, provider=None, model=None, llm=None):
            seen["messages"] = messages
            seen["provider"] = provider
            seen["model"] = model
            return _VALID_JSON_RESPONSE

        with patch.object(module, "analyze_screenshot", wraps=module.analyze_screenshot):
            with patch("backend.llm_adapter.invoke_chat", _fake_invoke_chat):
                analysis = module.analyze_screenshot(
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
            result = generate_ui_from_vision(
                _PNG_1X1, "image/png",
                project_root=PROJECT_ROOT,
            )
        # We don't crash — we report "llm_unavailable".
        assert "llm_unavailable" in result.warnings
        assert result.tsx_code == ""


# ── Cross-module integration (sibling modules still in play) ─────────


class TestSiblingIntegration:
    def test_generation_prompt_references_installed_registry(self):
        prompt = build_ui_generation_prompt(
            analysis=VisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        # These components ship in components/ui/ per the live
        # registry; if the registry renames them, sibling tests will
        # already fail — this is a smoke check at the prompt layer.
        assert "button" in prompt.lower()
        assert "card" in prompt.lower()

    def test_generation_prompt_references_design_tokens(self):
        prompt = build_ui_generation_prompt(
            analysis=VisionAnalysis(),
            project_root=PROJECT_ROOT,
            brief=None,
        )
        assert "primary" in prompt.lower()
        assert "background" in prompt.lower()

    def test_pipeline_uses_real_linter_on_real_registry(self):
        dirty = (
            "```tsx\n"
            "export default function X() {\n"
            "  return <input type=\"email\" />;\n"
            "}\n"
            "```\n"
        )
        inv = FakeInvoker([_VALID_JSON_RESPONSE, dirty])
        result = generate_ui_from_vision(
            _PNG_1X1, "image/png",
            project_root=PROJECT_ROOT,
            invoker=inv,
            auto_fix=True,
        )
        # Auto-fix should map <input> → <Input> with an import.
        assert "<Input" in result.tsx_code
        assert "@/components/ui/input" in result.tsx_code

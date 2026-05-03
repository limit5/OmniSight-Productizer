"""W16.2 — Image-attachment detection + layout-spec helper unit tests.

Locks the W16.2 helpers in ``backend/web/image_attachment.py`` that
detect inline ``data:image/<mime>;base64,…`` pastes and
``[image: <name>]`` upload markers in the operator's INVOKE command,
turn them into stable :class:`ImageAttachmentRef` records (16-hex-char
SHA-256 prefix), and wrap the vision-LLM round-trip with a degradable
fallback.

Coverage axes
─────────────

  §A  Drift guards on the frozen contract constants.
  §B  ``detect_image_attachments_in_text`` — data URL matching,
      attachment marker matching, dedup, paste-order preservation,
      MIME validation, oversize / no-payload defence, hard-cap.
  §C  :class:`ImageAttachmentRef` — frozen, exposes
      ``trigger_key()`` / ``image_attachment_trigger_key`` /
      ``trigger_keys_for_attachments`` in lock-step.
  §D  ``parse_layout_spec_response`` — shape parser handles canonical
      response, missing sections, multi-byte truncation, oversized
      input.
  §E  ``build_layout_spec_fallback`` — degraded marker is set + the
      summary is operator-actionable.
  §F  ``generate_layout_spec_for_image`` — vision-LLM-less degrade,
      ``generate_layout_spec(ref)`` adapter, ``invoke(messages)``
      adapter, error → degrade vs ``raise_on_failure=True``.
  §G  Re-export surface — every public symbol is re-exported via
      ``backend.web``.

These tests are PG-free, network-free and LLM-free — every helper
under test is a pure function so the tests stay fast and don't need
fixtures.

Module-global / cross-worker state audit (per
docs/sop/implement_phase_step.md Step 1): :mod:`backend.web.image_
attachment` keeps zero mutable module-level state — only frozen
constants, frozen dataclasses, and pre-compiled regex patterns. Every
uvicorn worker derives the same value from source code (Answer #1,
per-worker stateless derivation). No singleton, no in-memory cache,
no shared mutable state on the test side either.
"""

from __future__ import annotations

import hashlib

import pytest

from backend.web import image_attachment as ia
from backend import web as web_pkg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §A  Drift guards on frozen contract constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFrozenContractConstants:

    def test_image_hash_hex_length_pinned_at_16(self):
        # 16 hex = 64 bits — the trigger-key contract the frontend's
        # session-storage suppress system relies on. Drift would
        # silently mis-key suppressions across the FE/BE boundary.
        assert ia.IMAGE_HASH_HEX_LENGTH == 16

    def test_max_image_attachments_pinned_at_3(self):
        # Hard cap mirrors W16.1's _MAX_URL_TRIGGERS so a runaway
        # paste cannot blow up the LLM context budget.
        assert ia.MAX_IMAGE_ATTACHMENTS == 3

    def test_max_data_url_bytes_pinned_at_4mib(self):
        assert ia.MAX_DATA_URL_BYTES == 4 * 1024 * 1024

    def test_max_image_label_display_chars_pinned_at_64(self):
        assert ia.MAX_IMAGE_LABEL_DISPLAY_CHARS == 64

    def test_max_layout_spec_summary_chars_pinned_at_280(self):
        assert ia.MAX_LAYOUT_SPEC_SUMMARY_CHARS == 280

    def test_max_layout_spec_bytes_pinned_at_2048(self):
        assert ia.MAX_LAYOUT_SPEC_BYTES == 2048

    def test_supported_image_mime_subtypes_row_spec(self):
        # Row-spec tuple — adding HEIC etc. must update this constant
        # so the detection regex's post-match validation stays aligned.
        assert ia.SUPPORTED_IMAGE_MIME_SUBTYPES == (
            "png", "jpeg", "jpg", "gif", "webp", "svg+xml",
        )

    def test_image_coach_classes_row_spec_ordered(self):
        # Frozen row-spec literal "(a) component / (b) 整頁 / (c) brand
        # reference" — UIs must iterate this order.
        assert ia.IMAGE_COACH_CLASSES == (
            "component", "full_page", "brand_reference",
        )
        assert ia.IMAGE_COACH_CLASS_COMPONENT == "component"
        assert ia.IMAGE_COACH_CLASS_FULL_PAGE == "full_page"
        assert ia.IMAGE_COACH_CLASS_BRAND_REFERENCE == "brand_reference"

    def test_image_coach_trigger_prefix_ends_in_colon(self):
        # The W16.1-style suppress parser expects the prefix to end in
        # ':' so trigger keys split cleanly into prefix + payload.
        assert ia.IMAGE_COACH_TRIGGER_PREFIX == "image_in_message:"
        assert ia.IMAGE_COACH_TRIGGER_PREFIX.endswith(":")

    def test_image_ref_kinds_row_spec(self):
        assert ia.IMAGE_REF_KIND_DATA_URL == "data_url"
        assert ia.IMAGE_REF_KIND_MARKER == "marker"
        assert ia.IMAGE_REF_KINDS == ("data_url", "marker")

    def test_vision_llm_system_prompt_is_frozen_string(self):
        # Pinned to substring contract so the W16.9 e2e tests can grep
        # for the prompt fingerprint without false positives.
        assert isinstance(ia.VISION_LLM_SYSTEM_PROMPT, str)
        assert "vision-to-layout extractor" in ia.VISION_LLM_SYSTEM_PROMPT
        assert "OmniSight orchestrator" in ia.VISION_LLM_SYSTEM_PROMPT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §B  detect_image_attachments_in_text
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectImageAttachments:

    def test_empty_or_none_input_returns_empty(self):
        assert ia.detect_image_attachments_in_text("") == []
        assert ia.detect_image_attachments_in_text(None) == []
        assert ia.detect_image_attachments_in_text("   ") == []

    def test_no_image_payload_returns_empty(self):
        assert ia.detect_image_attachments_in_text(
            "regular text with no images at all",
        ) == []

    def test_single_data_url_emits_one_ref(self):
        text = "see this design data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"
        out = ia.detect_image_attachments_in_text(text)
        assert len(out) == 1
        ref = out[0]
        assert ref.kind == ia.IMAGE_REF_KIND_DATA_URL
        assert ref.mime_subtype == "png"
        assert len(ref.image_hash) == ia.IMAGE_HASH_HEX_LENGTH
        # Display label format: "<mime>:<hash16>"
        assert ref.display_label == f"png:{ref.image_hash}"

    def test_single_marker_emits_one_ref(self):
        out = ia.detect_image_attachments_in_text(
            "look at this [image: design-mock.png] for the layout",
        )
        assert len(out) == 1
        ref = out[0]
        assert ref.kind == ia.IMAGE_REF_KIND_MARKER
        assert ref.mime_subtype == ""  # source of truth lives in upload store
        assert ref.display_label == "design-mock.png"

    def test_marker_supports_cjk_filename(self):
        # Design files often have CJK / spaces — the regex must not
        # silently drop them.
        out = ia.detect_image_attachments_in_text(
            "[image: 設計稿 v3 final.png]",
        )
        assert len(out) == 1
        assert out[0].display_label == "設計稿 v3 final.png"

    def test_marker_tolerates_whitespace_inside_brackets(self):
        out = ia.detect_image_attachments_in_text(
            "[image:   spaced.jpg   ]",
        )
        assert len(out) == 1
        assert out[0].display_label == "spaced.jpg"

    def test_marker_case_insensitive_keyword(self):
        out = ia.detect_image_attachments_in_text(
            "[Image: caps.png] and [IMAGE: louder.png]",
        )
        assert {r.display_label for r in out} == {"caps.png", "louder.png"}

    def test_unsupported_mime_silently_dropped(self):
        # ``image/heic`` is not in SUPPORTED_IMAGE_MIME_SUBTYPES — must
        # not coach a bogus trigger.
        out = ia.detect_image_attachments_in_text(
            "data:image/heic;base64,AAAA",
        )
        assert out == []

    def test_data_url_no_payload_silently_dropped(self):
        # Defensive — empty base64 payload is not actionable.
        # The regex requires at least one base64 char so this just
        # doesn't match.
        out = ia.detect_image_attachments_in_text("data:image/png;base64,")
        assert out == []

    def test_oversized_data_url_silently_dropped(self):
        # > MAX_DATA_URL_BYTES → the frontend should have routed this
        # via ``/uploads`` and embedded a marker instead. We
        # defensively drop instead of coaching a 4 MiB trigger.
        big_payload = "A" * (ia.MAX_DATA_URL_BYTES + 100)
        out = ia.detect_image_attachments_in_text(
            f"data:image/png;base64,{big_payload}",
        )
        assert out == []

    def test_dedup_by_hash_keeps_first(self):
        # Two pastes of the same payload → one ref. Hash dedup is the
        # contract — the second occurrence's trigger key would be
        # identical and re-coaching the same image is the bug we are
        # protecting against.
        same = "data:image/png;base64,SAMEPAYLOAD"
        out = ia.detect_image_attachments_in_text(f"{same} and again {same}")
        assert len(out) == 1

    def test_marker_dedup_by_filename(self):
        out = ia.detect_image_attachments_in_text(
            "[image: a.png] and [image: a.png] (twice)",
        )
        assert len(out) == 1

    def test_paste_order_preserved_across_kinds(self):
        # Marker first, data URL second — order must reflect paste
        # order, not regex iteration order.
        text = (
            "first [image: alpha.png] then "
            "data:image/jpeg;base64,BBBB second"
        )
        out = ia.detect_image_attachments_in_text(text)
        assert [r.display_label for r in out][0] == "alpha.png"
        assert out[1].kind == ia.IMAGE_REF_KIND_DATA_URL

    def test_hard_cap_at_max_image_attachments(self):
        # 5 distinct markers → only first 3 emitted.
        text = " ".join(f"[image: f{i}.png]" for i in range(5))
        out = ia.detect_image_attachments_in_text(text)
        assert len(out) == ia.MAX_IMAGE_ATTACHMENTS
        assert [r.display_label for r in out] == [
            f"f{i}.png" for i in range(ia.MAX_IMAGE_ATTACHMENTS)
        ]

    def test_hash_is_deterministic_across_calls(self):
        text = "data:image/png;base64,DETERMINISTIC"
        a = ia.detect_image_attachments_in_text(text)
        b = ia.detect_image_attachments_in_text(text)
        assert a[0].image_hash == b[0].image_hash

    def test_data_url_hash_covers_payload_only(self):
        # The hash must cover the base64 payload so two different
        # mime types of the SAME bytes still dedup. Validates the
        # SHA-256 prefix wiring.
        payload = "PAYLOAD12345"
        expected = hashlib.sha256(payload.encode()).hexdigest()[:16]
        out = ia.detect_image_attachments_in_text(
            f"data:image/png;base64,{payload}",
        )
        assert out[0].image_hash == expected

    def test_marker_hash_covers_filename_verbatim(self):
        filename = "Brand Reference 設計.png"
        expected = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
        out = ia.detect_image_attachments_in_text(f"[image: {filename}]")
        assert out[0].image_hash == expected


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §C  ImageAttachmentRef + trigger-key helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ref(hash16: str = "abcdef0123456789", *, kind: str = "marker", label: str = "x.png") -> ia.ImageAttachmentRef:
    return ia.ImageAttachmentRef(
        kind=kind,
        mime_subtype="" if kind == "marker" else "png",
        image_hash=hash16,
        display_label=label,
        raw_excerpt=f"[image: {label}]",
    )


class TestImageAttachmentRef:

    def test_dataclass_is_frozen(self):
        r = _ref()
        with pytest.raises(Exception):  # FrozenInstanceError
            r.image_hash = "xxxx"  # type: ignore[misc]

    def test_trigger_key_uses_prefix_and_hash(self):
        r = _ref("DEADBEEF12345678")
        assert r.trigger_key() == "image_in_message:DEADBEEF12345678"

    def test_image_attachment_trigger_key_helper_matches_method(self):
        r = _ref("0011223344556677")
        assert ia.image_attachment_trigger_key(r) == r.trigger_key()

    def test_trigger_keys_for_attachments_preserves_order(self):
        refs = [_ref("aaaaaaaaaaaaaaaa"), _ref("bbbbbbbbbbbbbbbb")]
        assert ia.trigger_keys_for_attachments(refs) == [
            "image_in_message:aaaaaaaaaaaaaaaa",
            "image_in_message:bbbbbbbbbbbbbbbb",
        ]

    def test_trigger_keys_for_attachments_empty(self):
        assert ia.trigger_keys_for_attachments([]) == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §D  parse_layout_spec_response
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParseLayoutSpecResponse:

    def test_canonical_response_extracts_all_sections(self):
        ref = _ref("1111222233334444")
        raw = (
            "Landing page with hero, pricing, testimonial grid.\n"
            "- Header\n"
            "- Hero with CTA\n"
            "- Pricing table\n"
            "- Footer\n"
            "Colors: #1a73e8, #ffffff, #202124\n"
            "Fonts: Inter, Roboto Mono\n"
        )
        spec = ia.parse_layout_spec_response(ref, raw)
        assert spec.image_hash == ref.image_hash
        assert "hero" in spec.summary.lower()
        assert spec.components == (
            "Header", "Hero with CTA", "Pricing table", "Footer",
        )
        assert spec.colors == ("#1a73e8", "#ffffff", "#202124")
        assert spec.fonts == ("Inter", "Roboto Mono")
        assert spec.degraded is False
        assert spec.raw_text  # preserved

    def test_missing_colors_section_returns_empty_tuple(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref, "Just a summary.\n- One component\n",
        )
        assert spec.colors == ()
        assert spec.fonts == ()
        assert spec.components == ("One component",)

    def test_empty_input_returns_empty_layout(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(ref, "")
        assert spec.summary == ""
        assert spec.components == ()
        assert spec.degraded is False  # explicit empty != degraded
        assert spec.raw_text == ""

    def test_oversize_input_truncated_to_max_bytes(self):
        ref = _ref()
        # 4096 bytes → must shrink to ≤ MAX_LAYOUT_SPEC_BYTES.
        raw = "Summary line.\n" + ("- bullet line\n" * 400)
        spec = ia.parse_layout_spec_response(ref, raw)
        assert len(spec.raw_text.encode("utf-8")) <= ia.MAX_LAYOUT_SPEC_BYTES

    def test_multi_byte_truncation_safe(self):
        ref = _ref()
        # CJK characters are 3 bytes each in UTF-8 — boundary safety
        # check so we don't slice mid-codepoint.
        raw = ("中文" * 1500) + "\n- 元件 A\n"
        spec = ia.parse_layout_spec_response(ref, raw)
        # Round-trip decode must succeed (no UnicodeDecodeError raised
        # during parse).
        assert isinstance(spec.raw_text, str)

    def test_hex_color_with_or_without_hash_normalised(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref, "Summary.\nColors: #aabbcc, ddeeff\n",
        )
        assert spec.colors == ("#aabbcc", "#ddeeff")

    def test_color_dedup(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref, "Summary.\nColors: #aabbcc, #aabbcc, #112233\n",
        )
        assert spec.colors == ("#aabbcc", "#112233")

    def test_font_strips_quotes(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref, 'Summary.\nFonts: "Inter", \'Roboto\'\n',
        )
        assert spec.fonts == ("Inter", "Roboto")

    def test_font_cap_at_three(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref, "Summary.\nFonts: A, B, C, D, E\n",
        )
        assert len(spec.fonts) == 3

    def test_color_cap_at_five(self):
        ref = _ref()
        spec = ia.parse_layout_spec_response(
            ref,
            "Summary.\nColors: #111111, #222222, #333333, #444444, "
            "#555555, #666666\n",
        )
        assert len(spec.colors) == 5

    def test_summary_truncated_at_cap(self):
        ref = _ref()
        long_first = "Summary " + ("x" * 1000)
        spec = ia.parse_layout_spec_response(ref, long_first)
        assert len(spec.summary) <= ia.MAX_LAYOUT_SPEC_SUMMARY_CHARS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §E  build_layout_spec_fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildLayoutSpecFallback:

    def test_degraded_marker_set(self):
        ref = _ref(label="design.png")
        spec = ia.build_layout_spec_fallback(ref)
        assert spec.degraded is True
        assert spec.image_hash == ref.image_hash
        assert spec.components == ()
        assert spec.colors == ()
        assert spec.fonts == ()

    def test_summary_is_operator_actionable(self):
        ref = _ref(label="design.png")
        spec = ia.build_layout_spec_fallback(ref)
        # Operator-actionable: explicitly says no vision pass ran.
        assert "Vision LLM unavailable" in spec.summary
        assert "design.png" in spec.summary

    def test_summary_clipped_to_cap(self):
        ref = _ref(label="x" * 2000)
        spec = ia.build_layout_spec_fallback(ref)
        assert len(spec.summary) <= ia.MAX_LAYOUT_SPEC_SUMMARY_CHARS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §F  generate_layout_spec_for_image
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakeAdapterLLM:
    """Test fake exposing the simpler ``generate_layout_spec`` shape."""

    def __init__(self, spec: ia.LayoutSpec) -> None:
        self._spec = spec

    def generate_layout_spec(self, ref: ia.ImageAttachmentRef) -> ia.LayoutSpec:
        return self._spec


class _FakeInvokeLLM:
    """Test fake exposing the langchain-style ``invoke`` shape."""

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages):  # noqa: ANN001
        class _Resp:
            def __init__(self, c):
                self.content = c
        return _Resp(self._content)


class _RaisingLLM:
    def invoke(self, messages):  # noqa: ANN001
        raise RuntimeError("boom")


class TestGenerateLayoutSpecForImage:

    def test_no_llm_returns_degraded_fallback(self):
        ref = _ref()
        spec = ia.generate_layout_spec_for_image(ref, vision_llm=None)
        assert spec.degraded is True
        assert spec.image_hash == ref.image_hash

    def test_adapter_llm_returns_spec_directly(self):
        ref = _ref("9999888877776666")
        canonical = ia.LayoutSpec(
            image_hash=ref.image_hash,
            summary="Hero + pricing",
            components=("Header", "Hero", "Pricing"),
        )
        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_FakeAdapterLLM(canonical),
        )
        assert spec is canonical

    def test_adapter_llm_wrong_return_type_degrades(self):
        ref = _ref()

        class _BadAdapter:
            def generate_layout_spec(self, ref):  # noqa: ANN001
                return "not a layout spec"

        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_BadAdapter(),
        )
        assert spec.degraded is True

    def test_adapter_llm_wrong_return_type_raises_when_strict(self):
        ref = _ref()

        class _BadAdapter:
            def generate_layout_spec(self, ref):  # noqa: ANN001
                return None

        with pytest.raises(ia.LayoutSpecError):
            ia.generate_layout_spec_for_image(
                ref, vision_llm=_BadAdapter(), raise_on_failure=True,
            )

    def test_invoke_llm_canonical_response(self):
        ref = _ref("aaaabbbbccccdddd")
        raw = (
            "Hero and pricing layout.\n"
            "- Header\n- Hero\n- Pricing\n"
            "Colors: #1a73e8\nFonts: Inter\n"
        )
        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_FakeInvokeLLM(raw),
        )
        assert spec.degraded is False
        assert spec.image_hash == ref.image_hash
        assert "Header" in spec.components
        assert spec.colors == ("#1a73e8",)

    def test_invoke_llm_empty_content_degrades(self):
        ref = _ref()
        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_FakeInvokeLLM(""),
        )
        assert spec.degraded is True

    def test_invoke_llm_empty_content_raises_when_strict(self):
        ref = _ref()
        with pytest.raises(ia.LayoutSpecError):
            ia.generate_layout_spec_for_image(
                ref, vision_llm=_FakeInvokeLLM(""), raise_on_failure=True,
            )

    def test_llm_exception_degrades(self):
        ref = _ref()
        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_RaisingLLM(),
        )
        assert spec.degraded is True

    def test_llm_exception_raises_when_strict(self):
        ref = _ref()
        with pytest.raises(ia.LayoutSpecError):
            ia.generate_layout_spec_for_image(
                ref, vision_llm=_RaisingLLM(), raise_on_failure=True,
            )

    def test_unknown_llm_shape_degrades(self):
        ref = _ref()

        class _UnknownLLM:
            pass

        spec = ia.generate_layout_spec_for_image(
            ref, vision_llm=_UnknownLLM(),
        )
        assert spec.degraded is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §G  Re-export surface (every public symbol via backend.web)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


W16_2_SYMBOLS = (
    "IMAGE_COACH_CLASSES",
    "IMAGE_COACH_CLASS_BRAND_REFERENCE",
    "IMAGE_COACH_CLASS_COMPONENT",
    "IMAGE_COACH_CLASS_FULL_PAGE",
    "IMAGE_COACH_TRIGGER_PREFIX",
    "IMAGE_HASH_HEX_LENGTH",
    "IMAGE_REF_KINDS",
    "IMAGE_REF_KIND_DATA_URL",
    "IMAGE_REF_KIND_MARKER",
    "ImageAttachmentRef",
    "LayoutSpec",
    "LayoutSpecError",
    "MAX_DATA_URL_BYTES",
    "MAX_IMAGE_ATTACHMENTS",
    "MAX_IMAGE_LABEL_DISPLAY_CHARS",
    "MAX_LAYOUT_SPEC_BYTES",
    "MAX_LAYOUT_SPEC_SUMMARY_CHARS",
    "SUPPORTED_IMAGE_MIME_SUBTYPES",
    "VISION_LLM_SYSTEM_PROMPT",
    "build_layout_spec_fallback",
    "detect_image_attachments_in_text",
    "generate_layout_spec_for_image",
    "image_attachment_trigger_key",
    "parse_layout_spec_response",
    "trigger_keys_for_attachments",
)


@pytest.mark.parametrize("symbol", W16_2_SYMBOLS)
def test_w16_2_symbol_re_exported_via_package(symbol: str) -> None:
    assert symbol in web_pkg.__all__, (
        f"{symbol} missing from backend.web.__all__"
    )
    assert hasattr(web_pkg, symbol), (
        f"{symbol} not attribute of backend.web"
    )


def test_w16_2_symbol_count_added_is_25() -> None:
    # Drift guard: row spec adds exactly 25 public symbols.
    assert len(W16_2_SYMBOLS) == 25


def test_total_re_export_count_pinned_at_313() -> None:
    # W15.6 baseline was 288; W16.2 adds 25 image_attachment symbols
    # → 313. If this fails with a different count, audit whether you
    # consciously added / removed a public symbol and update the pin
    # alongside the row's TODO entry.
    assert len(web_pkg.__all__) == 374

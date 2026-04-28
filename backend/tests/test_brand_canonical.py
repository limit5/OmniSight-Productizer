"""W12.3 — :mod:`backend.brand_canonical` contract tests.

Pins the shared canonicalisation primitives that were lifted out of
:mod:`backend.brand_consistency_validator` so that
:mod:`backend.brand_extractor` can import them publicly instead of
crossing the validator's privacy boundary.

Coverage focuses on the contracts the **shared** layer must uphold —
identity / determinism / canonical output — and on the **alias
contract** that guarantees the validator's re-exports are the *same*
objects (not copies).  Detailed parser semantics remain pinned by the
existing validator + extractor test suites; this file's job is the
seam between them.
"""

from __future__ import annotations

import pytest

from backend import brand_canonical as bc
from backend import brand_consistency_validator as bcv
from backend import brand_extractor as be


# ── Public surface invariants ────────────────────────────────────────


class TestPublicSurface:
    def test_exports_alphabetised(self):
        assert bc.__all__ == sorted(bc.__all__)

    def test_expected_names_present(self):
        for name in (
            "GENERIC_FONT_KEYWORDS",
            "extract_font_families",
            "extract_hex_colors",
            "extract_hsl_colors",
            "extract_rgb_colors",
            "extract_tailwind_palette_classes",
            "hsl_to_hex",
            "iter_css_vars",
            "normalize_font_name",
            "normalize_hex",
            "rgb_to_hex",
            "split_font_stack",
        ):
            assert name in bc.__all__, name

    def test_no_dunder_leak(self):
        # Re-exports must be the *names* of helpers, not internals like
        # the compiled regexes — those stay module-private.
        for name in bc.__all__:
            assert not name.startswith("_"), name


# ── Re-export identity (validator side) ──────────────────────────────


class TestValidatorReexportsAreIdentical:
    """The validator must re-export the *same* objects, not copies."""

    @pytest.mark.parametrize("name", [
        "extract_font_families",
        "extract_hex_colors",
        "extract_hsl_colors",
        "extract_rgb_colors",
        "extract_tailwind_palette_classes",
        "hsl_to_hex",
        "normalize_font_name",
        "normalize_hex",
        "rgb_to_hex",
    ])
    def test_validator_reexports_canonical_object(self, name):
        assert getattr(bcv, name) is getattr(bc, name), (
            f"backend.brand_consistency_validator.{name} should be the "
            "same object re-exported from backend.brand_canonical, not a "
            "shadow copy."
        )

    def test_generic_font_keywords_reused_by_validator(self):
        # The validator's font_allowed() now reads the same frozenset.
        # We don't re-export the constant by name (it's an internal
        # helper) but the validator must use it: regress this by
        # smoking through font_allowed for a generic keyword.
        allowed = frozenset()  # empty allow-list
        for kw in ("sans-serif", "monospace", "system-ui"):
            assert bcv.font_allowed(kw, allowed) is True, kw


# ── Re-export identity (extractor side) ──────────────────────────────


class TestExtractorReusesCanonical:
    """The extractor must import its parser layer from brand_canonical
    rather than duplicating it.  W12.3's whole point.
    """

    def test_extractor_uses_canonical_helpers(self):
        # The internal references in brand_extractor — surfaced by
        # exercising end-to-end — must produce identical output to a
        # direct call into brand_canonical.
        text = '.a { color: #0066ff; font-family: "Inter", sans-serif; }'
        # Hex extraction: same canonical helper.
        assert bc.extract_hex_colors(text) == bcv.extract_hex_colors(text)
        # Font canonicalisation in the extractor must drop generic
        # keywords using the *same* set as the validator.
        spec = be.extract_brand_from_text(
            text, source_url="t", extracted_at="2026-04-29T00:00:00Z",
        )
        assert spec.fonts == ("inter",)  # sans-serif dropped via shared set

    def test_no_duplicate_generic_font_set_in_extractor(self):
        # The W12.2-era duplicate `_GENERIC_FONT_KEYWORDS` literal must
        # be gone — there must be exactly one source of truth.
        assert not hasattr(be, "_GENERIC_FONT_KEYWORDS"), (
            "brand_extractor must not redefine GENERIC_FONT_KEYWORDS; "
            "import it from backend.brand_canonical instead."
        )


# ── Canonicalisation behaviour ───────────────────────────────────────


class TestNormalizeHex:
    @pytest.mark.parametrize("raw, expected", [
        ("#abc", "#aabbcc"),
        ("#ABC", "#aabbcc"),
        ("#abcd", "#aabbcc"),     # alpha dropped
        ("#aabbccdd", "#aabbcc"),
        ("#FF00AA", "#ff00aa"),
        ("  #ff0000  ", "#ff0000"),
    ])
    def test_canonicalises(self, raw, expected):
        assert bc.normalize_hex(raw) == expected

    @pytest.mark.parametrize("raw", ["", "ff0000", "#gg", None, 42, "#12345"])
    def test_rejects_malformed(self, raw):
        assert bc.normalize_hex(raw) is None


class TestNormalizeFontName:
    @pytest.mark.parametrize("raw, expected", [
        ("Inter", "inter"),
        ("'Inter'", "inter"),
        ('"Helvetica Neue"', "helvetica neue"),
        ("  Inter  ", "inter"),
    ])
    def test_canonicalises(self, raw, expected):
        assert bc.normalize_font_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_rejects_blank(self, raw):
        assert bc.normalize_font_name(raw) is None


class TestRgbToHex:
    def test_red(self):
        assert bc.rgb_to_hex(255, 0, 0) == "#ff0000"

    def test_lower_clamp(self):
        assert bc.rgb_to_hex(-50, 0, 0) == "#000000"

    def test_upper_clamp(self):
        assert bc.rgb_to_hex(300, 300, 300) == "#ffffff"


class TestHslToHex:
    def test_hsl_red(self):
        assert bc.hsl_to_hex(0, 1.0, 0.5) == "#ff0000"

    def test_hsl_blue_approx(self):
        # hsl(210, 100%, 50%) ≈ #0080ff
        assert bc.hsl_to_hex(210, 1.0, 0.5) == "#0080ff"

    def test_hue_wraps(self):
        # 360 wraps back to 0
        assert bc.hsl_to_hex(360, 1.0, 0.5) == bc.hsl_to_hex(0, 1.0, 0.5)


class TestSplitFontStack:
    def test_basic(self):
        out = bc.split_font_stack("'Inter', sans-serif")
        assert out == ["'Inter'", "sans-serif"]

    def test_nested_var_paren_kept_intact(self):
        out = bc.split_font_stack("var(--font-sans, 'Inter'), sans-serif")
        assert out == ["var(--font-sans, 'Inter')", "sans-serif"]

    def test_quoted_comma_not_split(self):
        out = bc.split_font_stack('"Roboto, Bold", monospace')
        assert out == ['"Roboto, Bold"', "monospace"]

    def test_empty(self):
        assert bc.split_font_stack("") == []


# ── Extractors ───────────────────────────────────────────────────────


class TestExtractors:
    def test_extract_hex_colors(self):
        out = bc.extract_hex_colors(".a { color: #abc; } .b { color: #001122 }")
        assert [hx for hx, _ in out] == ["#abc", "#001122"]

    def test_extract_rgb_colors(self):
        out = bc.extract_rgb_colors(".a { color: rgb(0, 102, 255); }")
        assert [hx for hx, _ in out] == ["#0066ff"]

    def test_extract_hsl_colors(self):
        out = bc.extract_hsl_colors(".a { color: hsl(0, 100%, 50%); }")
        assert [hx for hx, _ in out] == ["#ff0000"]

    def test_extract_font_families_css(self):
        out = bc.extract_font_families(
            'h1 { font-family: "Inter", sans-serif; }'
        )
        assert [s for s, _ in out] == ['"Inter", sans-serif']

    def test_extract_font_families_jsx(self):
        out = bc.extract_font_families(
            "<div style={{ fontFamily: 'Inter, sans-serif' }} />"
        )
        assert [s for s, _ in out] == ["Inter, sans-serif"]

    def test_extract_tailwind_palette_classes(self):
        out = bc.extract_tailwind_palette_classes(
            'class="bg-slate-900 text-blue-600 hover:ring-red-500"'
        )
        names = [n for n, _ in out]
        assert "bg-slate-900" in names
        assert "text-blue-600" in names
        assert "ring-red-500" in names

    def test_iter_css_vars(self):
        # Only `var(--…)` references are surfaced — declarations
        # (`--primary: …`) are handled by the design-token loader.
        out = list(bc.iter_css_vars(
            ".a { color: var(--primary); background: var(--bg, #fff); }"
        ))
        names = [n for n, _ in out]
        assert names == ["primary", "bg"]

    def test_extractors_reject_non_string(self):
        # The B5 forward-mode validator's contract was "non-str → empty
        # tuple", not raise.  Preserve through the move.
        for fn in (
            bc.extract_hex_colors,
            bc.extract_rgb_colors,
            bc.extract_hsl_colors,
            bc.extract_font_families,
            bc.extract_tailwind_palette_classes,
        ):
            assert fn(None) == ()  # type: ignore[arg-type]
            assert fn(123) == ()  # type: ignore[arg-type]


# ── Generic font keywords contents ───────────────────────────────────


class TestGenericFontKeywords:
    def test_is_frozenset(self):
        assert isinstance(bc.GENERIC_FONT_KEYWORDS, frozenset)

    @pytest.mark.parametrize("kw", [
        "sans-serif", "serif", "monospace", "system-ui",
        "ui-sans-serif", "-apple-system",
    ])
    def test_contains_well_known(self, kw):
        assert kw in bc.GENERIC_FONT_KEYWORDS

    def test_does_not_contain_concrete_brand_fonts(self):
        for name in ("inter", "roboto", "helvetica", "arial"):
            assert name not in bc.GENERIC_FONT_KEYWORDS

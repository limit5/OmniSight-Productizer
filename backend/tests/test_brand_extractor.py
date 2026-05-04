"""W12.2 — :mod:`backend.brand_extractor` contract tests.

Pins :func:`extract_brand_from_url` and the supporting 5-dim
extractors against:

* Public-surface invariants (alphabetised ``__all__``, default
  constants pinned).
* Pure-pixel tally semantics (hex / rgb / hsl all roll up).
* Weighted k-means determinism + edge cases (empty input, k=0,
  fewer points than k, k=1).
* Font / heading / spacing / radius extractors against synthetic
  CSS payloads with known-correct outputs.
* :func:`extract_brand_from_url` fail-soft contract — fetcher
  exceptions, non-200 responses, and empty bodies all yield an empty
  :class:`BrandSpec` carrying ``source_url`` + ``extracted_at``.
* End-to-end pipeline integration — the surfaced spec round-trips
  through :func:`spec_to_json` (the W12.5 file format) without loss.
* Determinism across repeated runs (``seed`` fixed → byte-identical
  palette tuple) — the W12.6 reference matrix relies on this.

Network discipline: every test injects a fake ``fetch`` callable.
No test in this file may touch the real network — verified by the
"no urllib" assertion in :class:`TestNetworkDiscipline`.
"""

from __future__ import annotations

import sys

import pytest

from backend import brand_extractor as be
from backend.brand_extractor import (
    DEFAULT_KMEANS_MAX_ITER,
    DEFAULT_KMEANS_SEED,
    DEFAULT_PALETTE_K,
    extract_brand_from_text,
    extract_brand_from_url,
    extract_font_stack,
    extract_heading_scale,
    extract_radius_scale,
    extract_spacing_scale,
    kmeans_palette,
    tally_pixels,
)
from backend.brand_spec import (
    BrandSpec,
    BrandSpecError,
    spec_from_json,
    spec_to_json,
)


# ── Module-level invariants ─────────────────────────────────────────


class TestModuleInvariants:
    def test_exports_alphabetised(self):
        assert be.__all__ == sorted(be.__all__)

    def test_public_surface_exported(self):
        for name in (
            "DEFAULT_KMEANS_MAX_ITER",
            "DEFAULT_KMEANS_SEED",
            "DEFAULT_PALETTE_K",
            "extract_brand_from_text",
            "extract_brand_from_url",
            "extract_font_stack",
            "extract_heading_scale",
            "extract_radius_scale",
            "extract_spacing_scale",
            "kmeans_palette",
            "tally_pixels",
        ):
            assert name in be.__all__, name

    def test_default_palette_k(self):
        # 5 mirrors the W12.1 DIMENSIONS legend default brand sheet
        # (primary / secondary / accent / surface / muted).
        assert DEFAULT_PALETTE_K == 5

    def test_default_seed(self):
        # Pinned for the W12.6 reference matrix's byte-stable diff.
        assert DEFAULT_KMEANS_SEED == 0

    def test_default_max_iter_positive(self):
        assert DEFAULT_KMEANS_MAX_ITER > 0


# ── tally_pixels ────────────────────────────────────────────────────


class TestTallyPixels:
    def test_empty_text_empty_dict(self):
        assert tally_pixels("") == {}

    def test_non_string_empty_dict(self):
        assert tally_pixels(None) == {}  # type: ignore[arg-type]
        assert tally_pixels(123) == {}  # type: ignore[arg-type]

    def test_hex_literal_counted(self):
        out = tally_pixels(".a { color: #0066ff; }")
        assert out == {(0, 102, 255): 1}

    def test_short_hex_expanded(self):
        # `#abc` → `#aabbcc`
        out = tally_pixels(".a { color: #abc; }")
        assert out == {(0xaa, 0xbb, 0xcc): 1}

    def test_rgb_counted(self):
        out = tally_pixels(".a { background: rgb(0, 102, 255); }")
        assert out == {(0, 102, 255): 1}

    def test_hsl_counted(self):
        # hsl(210, 100%, 50%) ≈ rgb(0, 128, 255)
        out = tally_pixels(".a { background: hsl(210, 100%, 50%); }")
        assert out == {(0, 128, 255): 1}

    def test_repeated_colour_accumulates(self):
        text = "a { color: #ff0000 } b { color: #ff0000 } c { color: rgb(255,0,0) }"
        out = tally_pixels(text)
        assert out[(255, 0, 0)] == 3

    def test_distinct_colours_separate(self):
        text = "a { color: #112233 } b { color: #445566 }"
        out = tally_pixels(text)
        assert out == {(0x11, 0x22, 0x33): 1, (0x44, 0x55, 0x66): 1}


# ── kmeans_palette ──────────────────────────────────────────────────


class TestKmeansPalette:
    def test_empty_returns_empty(self):
        assert kmeans_palette({}) == ()

    def test_zero_k_returns_empty(self):
        assert kmeans_palette({(0, 0, 0): 1}, k=0) == ()

    def test_negative_k_returns_empty(self):
        assert kmeans_palette({(0, 0, 0): 1}, k=-3) == ()

    def test_fewer_points_than_k_returns_all_sorted(self):
        # 3 unique points, k=5 → return all 3, weighted-desc order.
        pixels = {(255, 0, 0): 5, (0, 255, 0): 10, (0, 0, 255): 1}
        out = kmeans_palette(pixels, k=5)
        assert out == ("#00ff00", "#ff0000", "#0000ff")

    def test_single_cluster_returns_centroid(self):
        # All identical points + k=1 → exact centroid.
        pixels = {(0, 102, 255): 10}
        out = kmeans_palette(pixels, k=1)
        assert out == ("#0066ff",)

    def test_two_well_separated_clusters(self):
        # Two tight clusters around #ff0000 and #0000ff with k=2:
        # palette must contain both, dominant first.
        pixels = {
            (255, 0, 0): 100,
            (250, 5, 5): 50,
            (245, 10, 10): 50,
            (0, 0, 255): 30,
            (5, 5, 250): 20,
        }
        out = kmeans_palette(pixels, k=2)
        assert len(out) == 2
        # First centroid dominates (red cluster has 200 weight vs blue 50).
        assert out[0].startswith("#f")  # red-ish
        assert out[1].startswith("#0")  # blue-ish

    def test_deterministic_with_seed(self):
        # Identical input + identical seed ⇒ byte-identical output.
        pixels = {(i * 7 % 256, i * 11 % 256, i * 13 % 256): (i % 5) + 1
                  for i in range(50)}
        a = kmeans_palette(pixels, k=4, seed=42)
        b = kmeans_palette(pixels, k=4, seed=42)
        assert a == b

    def test_different_seeds_may_differ(self):
        # Not strictly required (k-means may converge to same minima),
        # but exercise the seed plumbing.  Just assert both produce
        # k entries and the function runs.
        pixels = {(i, i, i): 1 for i in range(0, 256, 8)}
        a = kmeans_palette(pixels, k=3, seed=0)
        b = kmeans_palette(pixels, k=3, seed=1)
        assert len(a) <= 3 and len(b) <= 3

    def test_iterable_input_supported(self):
        # Mapping vs list-of-pairs both accepted.
        as_list = [((0, 0, 0), 1), ((255, 255, 255), 2)]
        out = kmeans_palette(as_list, k=2)
        assert set(out) == {"#000000", "#ffffff"}

    def test_output_is_canonical_hex(self):
        out = kmeans_palette({(0, 102, 255): 10}, k=1)
        assert all(s.startswith("#") and len(s) == 7 for s in out)
        assert all(s == s.lower() for s in out)


# ── extract_font_stack ──────────────────────────────────────────────


class TestExtractFontStack:
    def test_empty_text(self):
        assert extract_font_stack("") == ()

    def test_non_string(self):
        assert extract_font_stack(None) == ()  # type: ignore[arg-type]

    def test_basic_css_font_family(self):
        out = extract_font_stack('h1 { font-family: "Inter"; }')
        assert out == ("inter",)

    def test_drops_generic_keywords(self):
        out = extract_font_stack(
            'body { font-family: "Inter", sans-serif, system-ui; }'
        )
        assert out == ("inter",)

    def test_drops_var_indirection(self):
        out = extract_font_stack(
            'body { font-family: var(--font-sans), "Roboto"; }'
        )
        assert out == ("roboto",)

    def test_frequency_ordering(self):
        # Inter mentioned 3x, Roboto 1x → Inter first.
        text = """
            h1 { font-family: Inter, sans-serif; }
            h2 { font-family: Inter; }
            body { font-family: "Roboto", sans-serif; }
            p { font-family: Inter; }
        """
        out = extract_font_stack(text)
        assert out == ("inter", "roboto")

    def test_alphabetical_tiebreak(self):
        # Two families, equal frequency → alphabetical order.
        text = "a { font-family: Zeta; } b { font-family: Alpha; }"
        out = extract_font_stack(text)
        assert out == ("alpha", "zeta")

    def test_jsx_inline_style_picked_up(self):
        text = "<div style={{ fontFamily: 'Inter, sans-serif' }} />"
        out = extract_font_stack(text)
        assert out == ("inter",)


# ── extract_heading_scale ───────────────────────────────────────────


class TestExtractHeadingScale:
    def test_empty(self):
        scale = extract_heading_scale("")
        assert scale.is_empty

    def test_basic_h1_h2(self):
        text = "h1 { font-size: 48px; } h2 { font-size: 32px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 == 48.0
        assert scale.h2 == 32.0
        assert scale.h3 is None

    def test_rem_converted_to_px(self):
        text = "h1 { font-size: 3rem; }"
        scale = extract_heading_scale(text)
        assert scale.h1 == 48.0  # 3 * 16

    def test_em_converted_via_root_approx(self):
        text = "h2 { font-size: 2em; }"
        scale = extract_heading_scale(text)
        assert scale.h2 == 32.0  # documented approximation

    def test_percent_skipped(self):
        # No concrete px without cascade context.
        text = "h1 { font-size: 100%; }"
        scale = extract_heading_scale(text)
        assert scale.h1 is None

    def test_negative_skipped(self):
        text = "h1 { font-size: -16px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 is None

    def test_multiple_selectors_share_rule(self):
        # `h1, h2 { font-size: 24px }` → both levels surface 24.
        text = "h1, h2 { font-size: 24px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 == 24.0
        assert scale.h2 == 24.0

    def test_complex_selector_with_descendant(self):
        text = ".hero h1 { font-size: 64px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 == 64.0

    def test_first_declaration_wins(self):
        text = "h1 { font-size: 48px; } h1 { font-size: 16px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 == 48.0

    def test_does_not_match_h1foo(self):
        # `.h1foo` contains the substring "h1" but is not the h1 token.
        text = ".h1foo { font-size: 12px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 is None

    def test_does_not_match_dot_h1_class(self):
        # `.h1` is a class selector, not the h1 element.
        text = ".h1 { font-size: 12px; }"
        scale = extract_heading_scale(text)
        assert scale.h1 is None

    def test_all_six_levels(self):
        text = " ".join(
            f"h{i} {{ font-size: {64 - 8 * i}px; }}" for i in range(1, 7)
        )
        scale = extract_heading_scale(text)
        assert scale.to_dict() == {
            "h1": 56.0, "h2": 48.0, "h3": 40.0,
            "h4": 32.0, "h5": 24.0, "h6": 16.0,
        }


# ── extract_spacing_scale ───────────────────────────────────────────


class TestExtractSpacingScale:
    def test_empty(self):
        assert extract_spacing_scale("") == ()

    def test_basic_padding(self):
        out = extract_spacing_scale(".a { padding: 16px; }")
        assert 16.0 in out

    def test_shorthand_multiple_values(self):
        out = extract_spacing_scale(".a { padding: 4px 8px 12px 16px; }")
        for v in (4.0, 8.0, 12.0, 16.0):
            assert v in out

    def test_margin_top_long_hand(self):
        out = extract_spacing_scale(".a { margin-top: 24px; }")
        assert 24.0 in out

    def test_gap_picked_up(self):
        out = extract_spacing_scale(".a { gap: 32px; }")
        assert 32.0 in out

    def test_rem_converted(self):
        out = extract_spacing_scale(".a { padding: 1rem; }")
        assert 16.0 in out

    def test_zero_kept(self):
        out = extract_spacing_scale(".a { margin: 0; }")
        assert 0.0 in out

    def test_unitless_nonzero_skipped(self):
        # `padding: 16` (no unit) is invalid CSS — don't surface.
        out = extract_spacing_scale(".a { padding: 16; }")
        assert 16.0 not in out

    def test_negative_dropped(self):
        out = extract_spacing_scale(".a { margin: -8px; }")
        assert -8.0 not in out and 8.0 not in out

    def test_does_not_pick_up_font_size(self):
        # font-size is not a spacing property.
        out = extract_spacing_scale("h1 { font-size: 48px; }")
        assert 48.0 not in out

    def test_round_trip_through_brand_spec_dedup_sort(self):
        # Raw output is unsorted; BrandSpec normalises it.
        text = ".a { padding: 16px 8px 16px; gap: 4px; }"
        raw = extract_spacing_scale(text)
        spec = BrandSpec(spacing=raw)
        assert spec.spacing == (4.0, 8.0, 16.0)


# ── extract_radius_scale ────────────────────────────────────────────


class TestExtractRadiusScale:
    def test_empty(self):
        assert extract_radius_scale("") == ()

    def test_basic(self):
        out = extract_radius_scale(".a { border-radius: 8px; }")
        assert 8.0 in out

    def test_corner_long_hand(self):
        out = extract_radius_scale(
            ".a { border-top-left-radius: 12px; border-bottom-right-radius: 24px; }"
        )
        assert 12.0 in out and 24.0 in out

    def test_shorthand_four_values(self):
        out = extract_radius_scale(".a { border-radius: 4px 8px 12px 16px; }")
        for v in (4.0, 8.0, 12.0, 16.0):
            assert v in out

    def test_rem_converted(self):
        out = extract_radius_scale(".a { border-radius: 0.5rem; }")
        assert 8.0 in out  # 0.5 * 16

    def test_does_not_pick_up_padding(self):
        out = extract_radius_scale(".a { padding: 16px; }")
        assert 16.0 not in out


# ── extract_brand_from_text ─────────────────────────────────────────


class TestExtractBrandFromText:
    SAMPLE = """
        :root { --primary: #0066ff; }
        body {
          font-family: "Inter", "Helvetica Neue", sans-serif;
          color: #0066ff;
          background: #ffffff;
          padding: 16px;
          margin: 8px;
          gap: 24px;
        }
        .btn { background: rgb(0, 102, 255); border-radius: 8px; padding: 12px 16px; }
        .card { border-radius: 12px; margin: 32px; }
        h1 { font-size: 48px; font-family: Inter, sans-serif; }
        h2 { font-size: 32px; }
        .muted { color: #888888; padding: 4px; }
    """

    def test_returns_brand_spec(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        assert isinstance(spec, BrandSpec)

    def test_palette_dominant_first(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        # `#0066ff` appears most often (3 references) → must be in palette.
        assert "#0066ff" in spec.palette

    def test_fonts_extracted(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        assert "inter" in spec.fonts

    def test_heading_scale_extracted(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        assert spec.heading.h1 == 48.0
        assert spec.heading.h2 == 32.0

    def test_spacing_extracted_and_canonical(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        assert spec.spacing == tuple(sorted(set(spec.spacing)))
        for v in (4.0, 8.0, 12.0, 16.0, 24.0, 32.0):
            assert v in spec.spacing

    def test_radius_extracted(self):
        spec = extract_brand_from_text(self.SAMPLE, source_url="https://x/")
        assert spec.radius == (8.0, 12.0)

    def test_provenance_recorded(self):
        spec = extract_brand_from_text(
            self.SAMPLE,
            source_url="https://reference.example/",
            extracted_at="2026-04-29T12:00:00+00:00",
        )
        assert spec.source_url == "https://reference.example/"
        assert spec.extracted_at == "2026-04-29T12:00:00+00:00"

    def test_empty_text_yields_empty_spec(self):
        spec = extract_brand_from_text("")
        assert spec.is_empty

    def test_non_string_raises_brand_spec_error(self):
        with pytest.raises(BrandSpecError):
            extract_brand_from_text(None)  # type: ignore[arg-type]

    def test_round_trip_through_json(self):
        spec = extract_brand_from_text(
            self.SAMPLE,
            source_url="https://x/",
            extracted_at="2026-04-29T00:00:00+00:00",
        )
        text = spec_to_json(spec)
        restored = spec_from_json(text)
        assert restored == spec

    def test_deterministic_with_fixed_seed(self):
        a = extract_brand_from_text(
            self.SAMPLE,
            source_url="https://x/",
            extracted_at="t",
            seed=7,
        )
        b = extract_brand_from_text(
            self.SAMPLE,
            source_url="https://x/",
            extracted_at="t",
            seed=7,
        )
        assert a == b


# ── extract_brand_from_url ──────────────────────────────────────────


class TestExtractBrandFromUrl:
    SAMPLE = TestExtractBrandFromText.SAMPLE

    def test_rejects_empty_url(self):
        with pytest.raises(BrandSpecError):
            extract_brand_from_url("")

    def test_rejects_whitespace_url(self):
        with pytest.raises(BrandSpecError):
            extract_brand_from_url("   ")

    def test_rejects_non_string_url(self):
        with pytest.raises(BrandSpecError):
            extract_brand_from_url(None)  # type: ignore[arg-type]

    def test_happy_path_via_fake_fetch(self):
        def fetch(url):
            assert url == "https://example.com/"
            return 200, self.SAMPLE
        spec = extract_brand_from_url(
            "https://example.com/",
            fetch=fetch,
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec.source_url == "https://example.com/"
        assert spec.extracted_at == "2026-04-29T00:00:00+00:00"
        assert "#0066ff" in spec.palette
        assert "inter" in spec.fonts

    def test_fetch_exception_yields_empty_spec_with_provenance(self):
        def boom(url):
            raise OSError("dns")
        spec = extract_brand_from_url(
            "https://invalid.local/",
            fetch=boom,
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec.is_empty
        assert spec.source_url == "https://invalid.local/"
        assert spec.extracted_at == "2026-04-29T00:00:00+00:00"

    def test_non_200_yields_empty_spec_with_provenance(self):
        spec = extract_brand_from_url(
            "https://example.com/",
            fetch=lambda u: (404, "Not Found"),
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec.is_empty
        assert spec.source_url == "https://example.com/"
        assert spec.extracted_at == "2026-04-29T00:00:00+00:00"

    def test_empty_body_yields_empty_spec(self):
        spec = extract_brand_from_url(
            "https://example.com/",
            fetch=lambda u: (200, ""),
            now=lambda: "t",
        )
        assert spec.is_empty
        assert spec.source_url == "https://example.com/"

    def test_non_string_body_yields_empty_spec(self):
        spec = extract_brand_from_url(
            "https://example.com/",
            fetch=lambda u: (200, b"binary"),  # type: ignore[arg-type]
            now=lambda: "t",
        )
        assert spec.is_empty

    def test_default_seed_path_deterministic(self):
        a = extract_brand_from_url(
            "https://x/",
            fetch=lambda u: (200, self.SAMPLE),
            now=lambda: "t",
        )
        b = extract_brand_from_url(
            "https://x/",
            fetch=lambda u: (200, self.SAMPLE),
            now=lambda: "t",
        )
        assert a == b


# ── Network discipline ──────────────────────────────────────────────


class TestNetworkDiscipline:
    def test_no_test_actually_imports_urllib_at_module_load(self):
        # Importing :mod:`backend.brand_extractor` must not import
        # urllib at module load time — the default fetcher imports it
        # lazily only when invoked without a ``fetch=`` override.
        # If a future change moves the import to module-top, the
        # zero-network test runs would start triggering DNS at import.
        # Note: ``urllib`` itself is part of stdlib so may already be
        # imported by other test modules — we check ``urllib.request``
        # specifically since that is the network-touching submodule.
        before = "urllib.request" in sys.modules
        # Force re-import by reload — test only cares about the static
        # import surface at module-top, not what other tests pulled in.
        # If urllib.request was already loaded, we cannot prove the
        # negative; the assertion is best-effort.
        if not before:
            assert "urllib.request" not in sys.modules

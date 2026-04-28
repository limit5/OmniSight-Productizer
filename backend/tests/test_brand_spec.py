"""W12.1 — :class:`BrandSpec` dataclass contract tests.

Pins :mod:`backend.brand_spec` against:

* structural invariants of :class:`BrandSpec` / :class:`HeadingScale`
  (frozen, hashable, every field validated on construction);
* the 5-dim DIMENSIONS legend stays exactly
  ``("palette", "fonts", "heading", "spacing", "radius")``;
* canonicalisation of palette hexes, font names, spacing / radius
  scales (ordering + de-dup invariants);
* round-trip JSON safety via :func:`spec_to_json` /
  :func:`spec_from_json` and :meth:`BrandSpec.to_dict` /
  :meth:`BrandSpec.from_dict`;
* :class:`BrandSpecError` is raised on every input-shape violation —
  silent acceptance of a bad payload would let W12.5's
  ``.omnisight/brand.json`` writer ship malformed data downstream;
* ``schema_version`` round-trips so a future rev can refuse / migrate
  older payloads.

Sibling W12 rows (W12.2 extractor, W12.5 file writer, W12.6 reference
matrix) will add their own test files; this file is the type-only
contract.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from backend import brand_spec as bs
from backend.brand_spec import (
    BRAND_SPEC_SCHEMA_VERSION,
    DIMENSIONS,
    HEADING_LEVELS,
    BrandSpec,
    BrandSpecError,
    HeadingScale,
    canonicalise_font_name,
    canonicalise_hex,
    canonicalise_scale,
    spec_from_json,
    spec_to_json,
)


# ── Module-level invariants ─────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_semver(self):
        parts = BRAND_SPEC_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_dimensions_pinned(self):
        # If a future rev adds a 6th dimension this test must change in
        # the same commit — that forces a schema_version bump too.
        assert DIMENSIONS == ("palette", "fonts", "heading", "spacing", "radius")

    def test_heading_levels_pinned(self):
        assert HEADING_LEVELS == ("h1", "h2", "h3", "h4", "h5", "h6")

    def test_exports_alphabetised(self):
        assert bs.__all__ == sorted(bs.__all__)

    def test_public_surface_exported(self):
        for name in (
            "BRAND_SPEC_SCHEMA_VERSION",
            "DIMENSIONS",
            "HEADING_LEVELS",
            "BrandSpec",
            "BrandSpecError",
            "HeadingScale",
            "canonicalise_hex",
            "canonicalise_font_name",
            "canonicalise_scale",
            "spec_to_json",
            "spec_from_json",
        ):
            assert name in bs.__all__, name

    def test_brand_spec_error_subclasses_value_error(self):
        # Existing `except ValueError` chains must still catch us.
        assert issubclass(BrandSpecError, ValueError)


# ── canonicalise_hex ────────────────────────────────────────────────


class TestCanonicaliseHex:
    @pytest.mark.parametrize("raw,expected", [
        ("#aabbcc", "#aabbcc"),
        ("#AABBCC", "#aabbcc"),
        ("#abc", "#aabbcc"),
        ("#ABC", "#aabbcc"),
        ("#abcd", "#aabbcc"),       # alpha digit dropped
        ("#aabbccdd", "#aabbcc"),   # alpha pair dropped
        ("  #aabbcc  ", "#aabbcc"), # whitespace stripped
    ])
    def test_canonicalises(self, raw, expected):
        assert canonicalise_hex(raw) == expected

    @pytest.mark.parametrize("bad", [
        "aabbcc",      # missing leading '#'
        "#xyz",        # non-hex
        "#aabbccddee", # 10 digits
        "#",           # empty after '#'
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(BrandSpecError):
            canonicalise_hex(bad)

    def test_rejects_non_string(self):
        with pytest.raises(BrandSpecError):
            canonicalise_hex(0xaabbcc)


# ── canonicalise_font_name ──────────────────────────────────────────


class TestCanonicaliseFontName:
    @pytest.mark.parametrize("raw,expected", [
        ("Inter", "inter"),
        ("'Inter'", "inter"),
        ('"Inter"', "inter"),
        ("  Inter  ", "inter"),
        ("sans-serif", "sans-serif"),  # generic preserved
        ("Roboto Mono", "roboto mono"),
    ])
    def test_canonicalises(self, raw, expected):
        assert canonicalise_font_name(raw) == expected

    @pytest.mark.parametrize("bad", ["", "   ", "''", '""'])
    def test_rejects_blank(self, bad):
        with pytest.raises(BrandSpecError):
            canonicalise_font_name(bad)

    def test_rejects_non_string(self):
        with pytest.raises(BrandSpecError):
            canonicalise_font_name(42)


# ── canonicalise_scale ──────────────────────────────────────────────


class TestCanonicaliseScale:
    def test_sorts_ascending(self):
        assert canonicalise_scale([16, 4, 8, 24, 2]) == (2.0, 4.0, 8.0, 16.0, 24.0)

    def test_dedupes(self):
        assert canonicalise_scale([4, 8, 4, 8, 16]) == (4.0, 8.0, 16.0)

    def test_empty(self):
        assert canonicalise_scale([]) == ()

    def test_int_to_float(self):
        result = canonicalise_scale([1, 2, 3])
        assert all(isinstance(v, float) for v in result)

    def test_zero_allowed(self):
        # 0 is a legitimate spacing/radius value (eg. radius:0 squares).
        assert canonicalise_scale([0, 4, 8]) == (0.0, 4.0, 8.0)

    def test_rejects_negative(self):
        with pytest.raises(BrandSpecError):
            canonicalise_scale([-1.0, 4.0])

    def test_rejects_bool(self):
        # bool is an int subclass — must be rejected explicitly.
        with pytest.raises(BrandSpecError):
            canonicalise_scale([True, 4.0])

    def test_rejects_non_numeric(self):
        with pytest.raises(BrandSpecError):
            canonicalise_scale(["4px", 8.0])


# ── HeadingScale ────────────────────────────────────────────────────


class TestHeadingScale:
    def test_default_empty(self):
        h = HeadingScale()
        assert h.is_empty
        for level in HEADING_LEVELS:
            assert getattr(h, level) is None

    def test_partial_populated(self):
        h = HeadingScale(h1=48, h2=32, h3=24)
        assert h.h1 == 48.0
        assert h.h2 == 32.0
        assert h.h3 == 24.0
        assert h.h4 is None
        assert not h.is_empty

    def test_int_to_float(self):
        h = HeadingScale(h1=48)
        assert isinstance(h.h1, float)

    def test_is_frozen(self):
        h = HeadingScale(h1=48.0)
        with pytest.raises(FrozenInstanceError):
            h.h1 = 60.0  # type: ignore[misc]

    def test_rejects_negative(self):
        with pytest.raises(BrandSpecError):
            HeadingScale(h1=-1.0)

    def test_rejects_bool(self):
        with pytest.raises(BrandSpecError):
            HeadingScale(h1=True)  # type: ignore[arg-type]

    def test_rejects_non_numeric(self):
        with pytest.raises(BrandSpecError):
            HeadingScale(h1="48px")  # type: ignore[arg-type]

    def test_to_dict_round_trip(self):
        h = HeadingScale(h1=48, h2=32, h6=12)
        d = h.to_dict()
        assert d == {"h1": 48.0, "h2": 32.0, "h3": None,
                     "h4": None, "h5": None, "h6": 12.0}
        assert HeadingScale.from_dict(d) == h

    def test_from_dict_none(self):
        assert HeadingScale.from_dict(None) == HeadingScale()

    def test_from_dict_unknown_key_rejected(self):
        with pytest.raises(BrandSpecError):
            HeadingScale.from_dict({"h7": 8.0})

    def test_from_dict_non_mapping_rejected(self):
        with pytest.raises(BrandSpecError):
            HeadingScale.from_dict([("h1", 48)])  # type: ignore[arg-type]

    def test_zero_is_distinct_from_none(self):
        # h1=0 means "intentionally hidden"; h1=None means "no rule".
        h = HeadingScale(h1=0)
        assert h.h1 == 0.0
        assert not h.is_empty


# ── BrandSpec — construction ────────────────────────────────────────


class TestBrandSpecConstruction:
    def test_default_is_empty(self):
        spec = BrandSpec()
        assert spec.is_empty
        assert spec.palette == ()
        assert spec.fonts == ()
        assert spec.heading.is_empty
        assert spec.spacing == ()
        assert spec.radius == ()
        assert spec.source_url is None
        assert spec.extracted_at is None
        assert spec.schema_version == BRAND_SPEC_SCHEMA_VERSION

    def test_palette_canonicalised_and_dedup_preserves_order(self):
        spec = BrandSpec(palette=("#AABBCC", "#abc", "#ddeeff", "#DDEEFF"))
        # First two normalise to the same canonical form (#aabbcc) and
        # dedup keeps the first occurrence.  Order must be preserved
        # because k-means cluster dominance is meaningful.
        assert spec.palette == ("#aabbcc", "#ddeeff")

    def test_fonts_canonicalised_and_dedup_preserves_order(self):
        spec = BrandSpec(fonts=("Inter", "'Roboto'", "inter", "sans-serif"))
        assert spec.fonts == ("inter", "roboto", "sans-serif")

    def test_spacing_sorted_dedup(self):
        spec = BrandSpec(spacing=(16, 4, 8, 4, 24))
        assert spec.spacing == (4.0, 8.0, 16.0, 24.0)

    def test_radius_sorted_dedup(self):
        spec = BrandSpec(radius=(8, 0, 4, 8))
        assert spec.radius == (0.0, 4.0, 8.0)

    def test_heading_passed_through(self):
        h = HeadingScale(h1=48.0)
        spec = BrandSpec(heading=h)
        assert spec.heading is h

    def test_palette_list_input_converted_to_tuple(self):
        spec = BrandSpec(palette=["#aabbcc"])
        assert spec.palette == ("#aabbcc",)
        assert isinstance(spec.palette, tuple)

    def test_provenance_fields(self):
        spec = BrandSpec(
            source_url="https://example.com",
            extracted_at="2026-04-29T12:00:00Z",
        )
        assert spec.source_url == "https://example.com"
        assert spec.extracted_at == "2026-04-29T12:00:00Z"

    def test_schema_version_defaults(self):
        spec = BrandSpec()
        assert spec.schema_version == BRAND_SPEC_SCHEMA_VERSION

    def test_is_frozen(self):
        spec = BrandSpec(palette=("#aabbcc",))
        with pytest.raises(FrozenInstanceError):
            spec.palette = ()  # type: ignore[misc]

    def test_is_hashable(self):
        spec = BrandSpec(palette=("#aabbcc",), fonts=("inter",))
        # Frozen dataclasses with hashable field types are hashable.
        assert hash(spec) == hash(BrandSpec(palette=("#aabbcc",), fonts=("inter",)))


# ── BrandSpec — validation ──────────────────────────────────────────


class TestBrandSpecValidation:
    def test_palette_not_iterable_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(palette="#aabbcc")  # type: ignore[arg-type]

    def test_palette_bad_hex_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(palette=("not-a-hex",))

    def test_fonts_not_iterable_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(fonts="Inter")  # type: ignore[arg-type]

    def test_fonts_blank_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(fonts=("",))

    def test_heading_wrong_type_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(heading={"h1": 48})  # type: ignore[arg-type]

    def test_spacing_wrong_type_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(spacing="4 8 16")  # type: ignore[arg-type]

    def test_spacing_negative_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(spacing=(-1.0,))

    def test_radius_wrong_type_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(radius="0 4 8")  # type: ignore[arg-type]

    def test_radius_negative_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(radius=(-2.0,))

    def test_blank_source_url_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(source_url="   ")

    def test_blank_extracted_at_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(extracted_at="")

    def test_blank_schema_version_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec(schema_version="")


# ── BrandSpec — properties / mutation ───────────────────────────────


class TestBrandSpecProperties:
    def test_is_empty_ignores_provenance(self):
        spec = BrandSpec(source_url="https://x", extracted_at="2026-01-01T00:00:00Z")
        assert spec.is_empty

    def test_is_empty_false_with_palette(self):
        assert not BrandSpec(palette=("#aabbcc",)).is_empty

    def test_is_empty_false_with_heading(self):
        assert not BrandSpec(heading=HeadingScale(h1=48.0)).is_empty

    def test_is_empty_false_with_spacing(self):
        assert not BrandSpec(spacing=(4.0,)).is_empty

    def test_primary_color_when_palette_set(self):
        spec = BrandSpec(palette=("#aabbcc", "#ddeeff"))
        assert spec.primary_color == "#aabbcc"

    def test_primary_color_none_when_empty(self):
        assert BrandSpec().primary_color is None

    def test_primary_font_when_fonts_set(self):
        spec = BrandSpec(fonts=("Inter", "sans-serif"))
        assert spec.primary_font == "inter"

    def test_primary_font_none_when_empty(self):
        assert BrandSpec().primary_font is None

    def test_replace_with_returns_new_instance(self):
        original = BrandSpec(palette=("#aabbcc",))
        updated = original.replace_with(extracted_at="2026-04-29T00:00:00Z")
        assert updated is not original
        assert updated.palette == ("#aabbcc",)
        assert updated.extracted_at == "2026-04-29T00:00:00Z"
        assert original.extracted_at is None  # original unchanged

    def test_replace_with_re_runs_validation(self):
        # Re-running __post_init__ via dataclasses.replace must still
        # canonicalise — feeding a #ABC short hex through replace_with
        # should expand the same as construction does.
        spec = BrandSpec()
        updated = spec.replace_with(palette=("#abc",))
        assert updated.palette == ("#aabbcc",)


# ── BrandSpec — serialisation / round trip ──────────────────────────


class TestBrandSpecSerialisation:
    def _full_spec(self) -> BrandSpec:
        return BrandSpec(
            palette=("#0066ff", "#ffffff", "#111111"),
            fonts=("Inter", "sans-serif"),
            heading=HeadingScale(h1=48, h2=32, h3=24),
            spacing=(4, 8, 16, 24, 32),
            radius=(0, 4, 8, 16),
            source_url="https://example.com",
            extracted_at="2026-04-29T12:00:00Z",
        )

    def test_to_dict_shape(self):
        spec = self._full_spec()
        d = spec.to_dict()
        # All 8 top-level keys present (5 dims + 3 metadata).
        assert set(d) == {
            "schema_version", "source_url", "extracted_at",
            "palette", "fonts", "heading", "spacing", "radius",
        }

    def test_to_dict_tuples_become_lists(self):
        spec = self._full_spec()
        d = spec.to_dict()
        assert isinstance(d["palette"], list)
        assert isinstance(d["fonts"], list)
        assert isinstance(d["spacing"], list)
        assert isinstance(d["radius"], list)
        assert isinstance(d["heading"], dict)

    def test_to_dict_then_from_dict_round_trips(self):
        spec = self._full_spec()
        rebuilt = BrandSpec.from_dict(spec.to_dict())
        assert rebuilt == spec

    def test_empty_spec_round_trips(self):
        spec = BrandSpec()
        rebuilt = BrandSpec.from_dict(spec.to_dict())
        assert rebuilt == spec

    def test_json_round_trip_byte_deterministic(self):
        spec = self._full_spec()
        a = spec_to_json(spec)
        b = spec_to_json(spec)
        # Same input → byte-identical output (sort_keys=True).
        assert a == b

    def test_json_round_trip_reconstructs_equal_spec(self):
        spec = self._full_spec()
        rebuilt = spec_from_json(spec_to_json(spec))
        assert rebuilt == spec

    def test_json_compact_indent(self):
        spec = self._full_spec()
        compact = spec_to_json(spec, indent=None)
        # With indent=None there are no newlines in the output.
        assert "\n" not in compact
        # And it still round-trips.
        assert spec_from_json(compact) == spec

    def test_from_dict_ignores_unknown_top_level_key(self):
        # Forward-compat: an older reader running on a payload from a
        # future schema (with a 6th dim) must NOT crash.  It returns a
        # spec whose known fields are populated; the unknown key is
        # silently dropped.
        payload = self._full_spec().to_dict()
        payload["future_dim"] = ["something"]
        rebuilt = BrandSpec.from_dict(payload)
        assert rebuilt == self._full_spec()

    def test_from_dict_non_mapping_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict(["palette", "#aabbcc"])  # type: ignore[arg-type]

    def test_from_dict_palette_not_list_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict({"palette": "#aabbcc"})

    def test_from_dict_fonts_not_list_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict({"fonts": "Inter"})

    def test_from_dict_spacing_not_list_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict({"spacing": "4 8"})

    def test_from_dict_radius_not_list_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict({"radius": "0 4"})

    def test_from_dict_schema_version_non_string_rejected(self):
        with pytest.raises(BrandSpecError):
            BrandSpec.from_dict({"schema_version": 1.0})

    def test_from_dict_heading_payload(self):
        rebuilt = BrandSpec.from_dict({"heading": {"h1": 48, "h2": 32}})
        assert rebuilt.heading.h1 == 48.0
        assert rebuilt.heading.h2 == 32.0
        assert rebuilt.heading.h3 is None

    def test_from_dict_heading_explicit_none(self):
        rebuilt = BrandSpec.from_dict({"heading": None})
        assert rebuilt.heading == HeadingScale()

    def test_from_dict_heading_passthrough(self):
        h = HeadingScale(h1=48.0)
        rebuilt = BrandSpec.from_dict({"heading": h})
        assert rebuilt.heading == h

    def test_to_dict_canonical_json_compatible(self):
        # The dict must be pure JSON-types — a stdlib json.dumps of the
        # to_dict() output must succeed and re-parse to equal value.
        spec = self._full_spec()
        d = spec.to_dict()
        parsed = json.loads(json.dumps(d))
        # Compare via from_dict (handles list↔tuple convergence).
        assert BrandSpec.from_dict(parsed) == spec

    def test_schema_version_baked_into_payload(self):
        spec = BrandSpec()
        d = spec.to_dict()
        assert d["schema_version"] == BRAND_SPEC_SCHEMA_VERSION


# ── JSON helper edge cases ──────────────────────────────────────────


class TestJSONHelpers:
    def test_spec_to_json_rejects_non_brand_spec(self):
        with pytest.raises(BrandSpecError):
            spec_to_json({"palette": []})  # type: ignore[arg-type]

    def test_spec_from_json_rejects_non_string(self):
        with pytest.raises(BrandSpecError):
            spec_from_json(b"{}")  # type: ignore[arg-type]

    def test_spec_from_json_rejects_invalid_json(self):
        with pytest.raises(BrandSpecError):
            spec_from_json("{not json")

"""W12.6 — 8-reference-URL × 5-dimension brand-extraction reference matrix.

The W12 epic ships a *reverse-mode* brand-style pipeline:

* W12.1 — :class:`backend.brand_spec.BrandSpec` 5-dim type backbone.
* W12.2 — :func:`backend.brand_extractor.extract_brand_from_url` k-means
  / parser pipeline that turns a fetched HTML/CSS payload into a spec.
* W12.3 — :mod:`backend.brand_canonical` shared canonicalisation.
* W12.4 — :func:`backend.scaffold_reference.resolve_reference_url`
  argparse helper + resolver façade.
* W12.5 — :mod:`backend.brand_store` atomic ``.omnisight/brand.json``
  persistence layer.
* **W12.6 (this module)** — pin the contract via 8 representative
  reference fixtures × the 5 ``BrandSpec`` dimensions.

Why this matrix exists
----------------------

The earlier W12 rows verify each module in isolation; this file pins
**end-to-end behaviour** against a known palette of brand archetypes
(modern flat / classic Bootstrap / Material / minimalist / vibrant
multi-colour / serif editorial / dark-mode dashboard / no-brand
fail-soft).  Any regression in the extractor's regex set, the k-means
seeding, the canonicalisation primitives, or the resolver / store glue
surfaces here as a 1-line diff against the expected snapshot — exactly
the "drift guard" SOP §"Step 4" calls out.

The fixtures are *synthetic* HTML/CSS payloads, not live URLs — so
the matrix is fully air-gapped (no network, no flakiness, no
rate-limit risk) but still exercises every regex path the extractor
walks against real-world brand sheets.

Determinism contract
--------------------

* Identical fixture payload + identical seed ⇒ byte-identical
  :class:`BrandSpec`.  Extractor pins ``DEFAULT_KMEANS_SEED = 0``;
  this file relies on that pin and asserts cross-run stability.
* Identical fixture payload via the W12.4 resolver façade ⇒ identical
  spec to the direct extractor call.  Resolver must not mutate.
* Spec ↔ JSON round-trip via :func:`backend.brand_spec.spec_to_json`
  / :func:`spec_from_json` is loss-free (W12.5 file format guarantee).

Network discipline
------------------

Every test injects a fake ``fetch`` callable that pulls from
:data:`REFERENCE_FIXTURES`.  No test in this file may touch the real
network.  The :class:`TestNetworkDiscipline` class enforces this.

Module-global state audit (SOP §1)
----------------------------------

Test-only module — read-only fixture constants
(:data:`REFERENCE_FIXTURES` and the per-fixture expected snapshots)
plus pytest helper functions.  No mutable module state, no DB, no
shared in-memory cache.  Cross-worker irrelevant: tests run
single-process under pytest.

Compat-fingerprint grep (SOP §3)
--------------------------------

N/A — no DB code path.  ``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)|VALUES.*\\?[,)]"``
returns 0 hits in this file (the four matches inside this docstring
are the SOP §3 N/A note itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.brand_extractor import (
    DEFAULT_KMEANS_SEED,
    DEFAULT_PALETTE_K,
    extract_brand_from_text,
    extract_brand_from_url,
)
from backend.brand_spec import (
    BRAND_SPEC_SCHEMA_VERSION,
    DIMENSIONS,
    BrandSpec,
    HeadingScale,
    spec_from_json,
    spec_to_json,
)
from backend.brand_store import (
    read_brand_spec,
    read_brand_spec_if_exists,
    write_brand_spec,
)
from backend.scaffold_reference import resolve_reference_url


# ── Reference-fixture matrix ────────────────────────────────────────


@dataclass(frozen=True)
class ReferenceCase:
    """One row in the 8-URL × 5-dim reference matrix.

    The expected fields below are *exact* snapshots of what the W12.2
    extractor produces against the synthetic ``payload`` at
    ``DEFAULT_KMEANS_SEED`` — captured during W12.6 row landing on
    2026-04-29.  A drift in extraction surfaces as a contract failure
    here, not as silent behaviour change downstream.
    """

    name: str
    url: str
    payload: str
    expected_palette: tuple[str, ...]
    expected_fonts: tuple[str, ...]
    expected_heading: dict[str, float | None]
    expected_spacing: tuple[float, ...]
    expected_radius: tuple[float, ...]
    expected_is_empty: bool


# Eight reference brand archetypes — each chosen to cover a distinct
# extraction-pipeline corner the earlier W12 row tests already exercise
# in isolation, reassembled here as end-to-end snapshots.
#
#   1. tailwind-modern        — bright primary + neutrals, single brand font
#   2. bootstrap-classic      — multi-font fallback stack, 4-px radius scale
#   3. material-design        — RGB-literal colour, full h1..h6 ladder
#   4. minimal-monochrome     — < k unique colours (k-means short-circuit)
#   5. vibrant-colourful      — > k distinct hues (k-means real path)
#   6. serif-editorial        — Georgia / Times / Helvetica triple stack
#   7. dark-mode-dashboard    — dark palette + Geist + Inter fall-back
#   8. empty-no-brand         — nothing detectable → fail-soft empty spec
#
# All payloads use the same canonical formatting:
#   * `body` declares font-family / colour / background / padding / margin.
#   * Components declare their own padding / margin / gap / border-radius.
#   * Headings declare `font-size` in `px` (or `rem` where worth testing).
# This keeps the diff between fixtures reviewable and the expected
# snapshots predictable when re-capturing.


_FIXTURE_TAILWIND_MODERN = """
:root { --primary: #0066ff; }
body { font-family: "Inter", sans-serif; color: #0f172a; background: #ffffff; padding: 0; margin: 0; }
.btn { background: #0066ff; color: #ffffff; padding: 12px 24px; border-radius: 8px; }
.btn-secondary { background: #ffffff; color: #0066ff; padding: 12px 24px; border-radius: 8px; }
.card { background: #f8fafc; padding: 32px; border-radius: 12px; gap: 16px; margin: 16px; }
h1 { font-size: 48px; font-family: Inter, sans-serif; }
h2 { font-size: 32px; }
h3 { font-size: 24px; }
"""


_FIXTURE_BOOTSTRAP_CLASSIC = """
body { font-family: "Helvetica Neue", Arial, sans-serif; color: #212529; background: #f8f9fa; padding: 0; margin: 0; }
.btn-primary { background: #0d6efd; color: #ffffff; padding: 6px 12px; border-radius: 4px; }
.btn-secondary { background: #6c757d; color: #ffffff; padding: 6px 12px; border-radius: 4px; }
.alert { padding: 16px; margin: 16px; border-radius: 4px; }
.card { padding: 16px; border-radius: 4px; gap: 8px; }
h1 { font-size: 40px; }
h2 { font-size: 32px; }
h3 { font-size: 28px; }
"""


_FIXTURE_MATERIAL_DESIGN = """
body { font-family: Roboto, "Helvetica Neue", Arial, sans-serif; color: rgb(33, 33, 33); background: #ffffff; padding: 0; margin: 0; }
.btn { background: #6200ee; color: #ffffff; padding: 8px 16px; border-radius: 4px; }
.fab { background: #03dac6; padding: 16px; border-radius: 28px; }
.card { background: #ffffff; padding: 16px; margin: 8px; border-radius: 4px; gap: 16px; }
h1 { font-size: 96px; font-family: Roboto; }
h2 { font-size: 60px; }
h3 { font-size: 48px; }
h4 { font-size: 34px; }
h5 { font-size: 24px; }
h6 { font-size: 20px; }
"""


_FIXTURE_MINIMAL_MONOCHROME = """
body { font-family: "Inter", sans-serif; color: #000000; background: #ffffff; padding: 0; margin: 0; }
.divider { background: #cccccc; padding: 0; }
h1 { font-size: 64px; font-family: Inter; }
.section { padding: 48px; gap: 24px; }
.card { border-radius: 0; }
"""


_FIXTURE_VIBRANT_COLOURFUL = """
:root { --brand: #ff0080; }
body { font-family: "Poppins", sans-serif; color: #ffffff; background: #1a1a2e; padding: 0; margin: 0; }
.tag-red { background: #ff0080; color: #ffffff; padding: 4px 8px; border-radius: 999px; }
.tag-yellow { background: #ffd60a; color: #000000; padding: 4px 8px; border-radius: 999px; }
.tag-green { background: #06ffa5; padding: 4px 8px; border-radius: 999px; }
.tag-purple { background: #b026ff; padding: 4px 8px; border-radius: 999px; }
.tag-blue { background: #0080ff; padding: 4px 8px; border-radius: 999px; }
.section { padding: 24px; gap: 12px; border-radius: 16px; margin: 16px; }
h1 { font-size: 56px; font-family: Poppins; }
h2 { font-size: 32px; }
"""


_FIXTURE_SERIF_EDITORIAL = """
body { font-family: "Georgia", "Times New Roman", serif; color: #121212; background: #ffffff; padding: 0; margin: 0; }
.byline { font-family: "Helvetica Neue", sans-serif; color: #666666; }
.article { padding: 40px; margin: 32px; }
.pullquote { padding: 24px; border-radius: 0; margin: 32px; }
h1 { font-size: 56px; font-family: Georgia, serif; }
h2 { font-size: 36px; font-family: Georgia; }
h3 { font-size: 28px; }
"""


_FIXTURE_DARK_MODE_DASHBOARD = """
body { font-family: "Geist", "Inter", sans-serif; background: #000000; color: #ffffff; padding: 0; margin: 0; }
.nav { background: #0a0a0a; padding: 16px 24px; }
.card { background: #111111; padding: 24px; border-radius: 12px; gap: 16px; margin: 16px; }
.btn-primary { background: #ffffff; color: #000000; padding: 12px 24px; border-radius: 8px; }
.muted { color: #888888; }
h1 { font-size: 40px; font-family: Geist; }
h2 { font-size: 28px; }
h3 { font-size: 20px; }
"""


_FIXTURE_EMPTY_NO_BRAND = (
    "<html><body><p>Hello world. No brand styling here.</p></body></html>"
)


REFERENCE_FIXTURES: tuple[ReferenceCase, ...] = (
    ReferenceCase(
        name="tailwind-modern",
        url="https://tailwind-modern.example/",
        payload=_FIXTURE_TAILWIND_MODERN,
        expected_palette=("#0066ff", "#ffffff", "#0f172a", "#f8fafc"),
        expected_fonts=("inter",),
        expected_heading={
            "h1": 48.0, "h2": 32.0, "h3": 24.0,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 12.0, 16.0, 24.0, 32.0),
        expected_radius=(8.0, 12.0),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="bootstrap-classic",
        url="https://bootstrap-classic.example/",
        payload=_FIXTURE_BOOTSTRAP_CLASSIC,
        expected_palette=("#ffffff", "#0d6efd", "#212529", "#6c757d", "#f8f9fa"),
        expected_fonts=("arial", "helvetica neue"),
        expected_heading={
            "h1": 40.0, "h2": 32.0, "h3": 28.0,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 6.0, 8.0, 12.0, 16.0),
        expected_radius=(4.0,),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="material-design",
        url="https://material-design.example/",
        payload=_FIXTURE_MATERIAL_DESIGN,
        expected_palette=("#ffffff", "#03dac6", "#212121", "#6200ee"),
        expected_fonts=("roboto", "arial", "helvetica neue"),
        expected_heading={
            "h1": 96.0, "h2": 60.0, "h3": 48.0,
            "h4": 34.0, "h5": 24.0, "h6": 20.0,
        },
        expected_spacing=(0.0, 8.0, 16.0),
        expected_radius=(4.0, 28.0),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="minimal-monochrome",
        url="https://minimal-monochrome.example/",
        payload=_FIXTURE_MINIMAL_MONOCHROME,
        expected_palette=("#000000", "#cccccc", "#ffffff"),
        expected_fonts=("inter",),
        expected_heading={
            "h1": 64.0, "h2": None, "h3": None,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 24.0, 48.0),
        expected_radius=(0.0,),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="vibrant-colourful",
        url="https://vibrant-colourful.example/",
        payload=_FIXTURE_VIBRANT_COLOURFUL,
        expected_palette=("#e50daa", "#03c0d2", "#0d0d17", "#ffffff", "#ffd60a"),
        expected_fonts=("poppins",),
        expected_heading={
            "h1": 56.0, "h2": 32.0, "h3": None,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 4.0, 8.0, 12.0, 16.0, 24.0),
        expected_radius=(16.0, 999.0),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="serif-editorial",
        url="https://serif-editorial.example/",
        payload=_FIXTURE_SERIF_EDITORIAL,
        expected_palette=("#121212", "#666666", "#ffffff"),
        expected_fonts=("georgia", "helvetica neue", "times new roman"),
        expected_heading={
            "h1": 56.0, "h2": 36.0, "h3": 28.0,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 24.0, 32.0, 40.0),
        expected_radius=(0.0,),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="dark-mode-dashboard",
        url="https://dark-mode-dashboard.example/",
        payload=_FIXTURE_DARK_MODE_DASHBOARD,
        expected_palette=("#000000", "#ffffff", "#0a0a0a", "#111111", "#888888"),
        expected_fonts=("geist", "inter"),
        expected_heading={
            "h1": 40.0, "h2": 28.0, "h3": 20.0,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(0.0, 12.0, 16.0, 24.0),
        expected_radius=(8.0, 12.0),
        expected_is_empty=False,
    ),
    ReferenceCase(
        name="empty-no-brand",
        url="https://empty-no-brand.example/",
        payload=_FIXTURE_EMPTY_NO_BRAND,
        expected_palette=(),
        expected_fonts=(),
        expected_heading={
            "h1": None, "h2": None, "h3": None,
            "h4": None, "h5": None, "h6": None,
        },
        expected_spacing=(),
        expected_radius=(),
        expected_is_empty=True,
    ),
)


_FIXED_TS = "2026-04-29T00:00:00+00:00"


def _fake_fetch_for(case: ReferenceCase):
    """Return a deterministic ``fetch`` callable bound to ``case``."""

    def fetch(url: str) -> tuple[int, str]:
        # Sanity: every test that injects this fetcher must call it
        # with the matching URL — guards against fixture/expected
        # mismatch in the matrix loop.
        assert url == case.url, f"fixture URL mismatch: {url!r} vs {case.url!r}"
        return 200, case.payload

    return fetch


def _fake_now() -> str:
    return _FIXED_TS


def _ids(prefix: str) -> list[str]:
    return [f"{prefix}::{c.name}" for c in REFERENCE_FIXTURES]


# ── Matrix invariants ───────────────────────────────────────────────


class TestMatrixShape:
    """The matrix itself must satisfy a few invariants the row title
    promises: 8 fixtures × 5 dimensions, each one named distinctly."""

    def test_fixture_count_is_eight(self):
        # Row title: "8 reference URL × 5 維度".  Eight is the contract.
        assert len(REFERENCE_FIXTURES) == 8

    def test_dimensions_match_brand_spec_legend(self):
        # Five dimensions = the W12.1 ``DIMENSIONS`` legend.  Drift guard.
        assert DIMENSIONS == ("palette", "fonts", "heading", "spacing", "radius")
        assert len(DIMENSIONS) == 5

    def test_fixture_names_unique(self):
        names = [c.name for c in REFERENCE_FIXTURES]
        assert len(names) == len(set(names))

    def test_fixture_urls_unique(self):
        urls = [c.url for c in REFERENCE_FIXTURES]
        assert len(urls) == len(set(urls))

    def test_every_url_uses_https(self):
        for c in REFERENCE_FIXTURES:
            assert c.url.startswith("https://"), c.url

    def test_every_url_under_example_tld(self):
        # Reserved ".example" TLD per RFC 2606 — guarantees no test
        # URL ever resolves to a real host even if a future version of
        # this matrix accidentally drops the fake-fetch injection.
        for c in REFERENCE_FIXTURES:
            assert c.url.endswith(".example/"), c.url

    def test_every_payload_is_non_empty_string(self):
        for c in REFERENCE_FIXTURES:
            assert isinstance(c.payload, str)
            assert len(c.payload) > 0, c.name

    def test_at_least_one_empty_no_brand_case(self):
        # The fail-soft contract is meaningful only if the matrix
        # exercises it explicitly.
        empties = [c for c in REFERENCE_FIXTURES if c.expected_is_empty]
        assert len(empties) >= 1
        assert all(
            c.expected_palette == ()
            and c.expected_fonts == ()
            and c.expected_spacing == ()
            and c.expected_radius == ()
            and all(v is None for v in c.expected_heading.values())
            for c in empties
        )


# ── Per-fixture × per-dimension snapshot tests ──────────────────────


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestPaletteDimension:
    """Dimension #1 — palette extraction snapshot."""

    def test_palette_matches_expected(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.palette == case.expected_palette

    def test_palette_canonical_lowercase_hex(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for hex_value in spec.palette:
            assert hex_value == hex_value.lower()
            assert hex_value.startswith("#")
            assert len(hex_value) == 7  # `#rrggbb`

    def test_palette_size_within_default_k(self, case: ReferenceCase):
        # k=5 default ⇒ palette ≤ 5 (or all unique colours when fewer).
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert len(spec.palette) <= DEFAULT_PALETTE_K


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestFontsDimension:
    """Dimension #2 — font-family extraction snapshot."""

    def test_fonts_match_expected(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.fonts == case.expected_fonts

    def test_fonts_lowercase_canonicalised(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for font in spec.fonts:
            assert font == font.lower()
            # No surrounding quotes leaked through the canonicaliser.
            assert "'" not in font and '"' not in font

    def test_no_generic_keyword_in_fonts(self, case: ReferenceCase):
        # `sans-serif` / `serif` / `monospace` / `system-ui` must be
        # filtered out — they are CSS keywords, not brand families.
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for keyword in ("sans-serif", "serif", "monospace", "system-ui",
                        "cursive", "fantasy"):
            assert keyword not in spec.fonts, (case.name, keyword)


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestHeadingDimension:
    """Dimension #3 — heading-scale extraction snapshot."""

    def test_heading_matches_expected(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.heading.to_dict() == case.expected_heading

    def test_heading_keys_are_h1_to_h6(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert set(spec.heading.to_dict().keys()) == {
            "h1", "h2", "h3", "h4", "h5", "h6",
        }

    def test_heading_values_non_negative(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for level, px in spec.heading.to_dict().items():
            if px is not None:
                assert px >= 0, (case.name, level, px)


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestSpacingDimension:
    """Dimension #4 — spacing-scale extraction snapshot."""

    def test_spacing_matches_expected(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.spacing == case.expected_spacing

    def test_spacing_sorted_ascending_and_deduped(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        # ``BrandSpec.__post_init__`` canonicalises — drift guard.
        assert spec.spacing == tuple(sorted(set(spec.spacing)))

    def test_spacing_non_negative(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for value in spec.spacing:
            assert value >= 0, (case.name, value)


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestRadiusDimension:
    """Dimension #5 — border-radius scale extraction snapshot."""

    def test_radius_matches_expected(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.radius == case.expected_radius

    def test_radius_sorted_ascending_and_deduped(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.radius == tuple(sorted(set(spec.radius)))

    def test_radius_non_negative(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        for value in spec.radius:
            assert value >= 0, (case.name, value)


# ── Whole-spec snapshot ─────────────────────────────────────────────


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestWholeSpecSnapshot:
    """Combined assertion — the full :class:`BrandSpec` must equal the
    snapshot constructed from the per-dimension expected fields.

    Per-dimension tests above already cover the individual fields;
    this test additionally pins:
      * ``is_empty`` semantics line up with the per-dim emptiness.
      * Provenance plumbing (``source_url`` + ``extracted_at``) survives
        from the extractor through to the spec.
      * ``schema_version`` is baked in at construction.
    """

    def test_full_spec_matches(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        expected = BrandSpec(
            palette=case.expected_palette,
            fonts=case.expected_fonts,
            heading=HeadingScale(**case.expected_heading),
            spacing=case.expected_spacing,
            radius=case.expected_radius,
            source_url=case.url,
            extracted_at=_FIXED_TS,
        )
        assert spec == expected

    def test_is_empty_matches(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.is_empty == case.expected_is_empty

    def test_provenance_carried_through(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.source_url == case.url
        assert spec.extracted_at == _FIXED_TS

    def test_schema_version_pinned(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        assert spec.schema_version == BRAND_SPEC_SCHEMA_VERSION


# ── Determinism contract ────────────────────────────────────────────


class TestDeterminism:
    """Identical input + identical seed ⇒ byte-identical output.  This
    is what makes the matrix a meaningful regression gate — any
    extraction drift surfaces as a diff, not as flake."""

    @pytest.mark.parametrize(
        "case", REFERENCE_FIXTURES, ids=lambda c: c.name,
    )
    def test_repeated_extraction_byte_stable(self, case: ReferenceCase):
        runs = [
            extract_brand_from_text(
                case.payload, source_url=case.url, extracted_at=_FIXED_TS,
            )
            for _ in range(3)
        ]
        for i in range(1, len(runs)):
            assert runs[i] == runs[0], case.name

    @pytest.mark.parametrize(
        "case", REFERENCE_FIXTURES, ids=lambda c: c.name,
    )
    def test_default_seed_is_zero(self, case: ReferenceCase):
        # The matrix snapshots are captured at ``DEFAULT_KMEANS_SEED``;
        # if that constant ever drifts the snapshots invalidate.
        # Pin it here so the matrix doc-string + reality stay aligned.
        assert DEFAULT_KMEANS_SEED == 0

    @pytest.mark.parametrize(
        "case", REFERENCE_FIXTURES, ids=lambda c: c.name,
    )
    def test_text_path_matches_url_path(self, case: ReferenceCase):
        # ``extract_brand_from_url`` should reduce to
        # ``extract_brand_from_text`` after fetching — they must
        # produce identical specs given identical payload + same
        # provenance.  Any drift here means url-path injected
        # something the text-path didn't, which would be a bug.
        text_spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        url_spec = extract_brand_from_url(
            case.url,
            fetch=_fake_fetch_for(case),
            now=_fake_now,
        )
        assert text_spec == url_spec, case.name


# ── JSON round-trip (W12.5 file format) ─────────────────────────────


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestJsonRoundTrip:
    """Every matrix spec must survive the W12.5 ``.omnisight/brand.json``
    serialisation byte-for-byte."""

    def test_spec_to_json_then_from_json_is_lossless(self, case: ReferenceCase):
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        text = spec_to_json(spec)
        restored = spec_from_json(text)
        assert restored == spec

    def test_spec_to_json_canonical(self, case: ReferenceCase):
        # Canonical JSON discipline: identical spec ⇒ identical bytes.
        spec = extract_brand_from_text(
            case.payload, source_url=case.url, extracted_at=_FIXED_TS,
        )
        a = spec_to_json(spec)
        b = spec_to_json(spec)
        assert a == b


# ── End-to-end resolver → store integration ─────────────────────────


@pytest.mark.parametrize("case", REFERENCE_FIXTURES, ids=lambda c: c.name)
class TestEndToEnd:
    """W12.4 resolver ⇒ W12.5 store ⇒ downstream-agent read.

    This is the real shape an operator running
    ``python -m backend.<stack>_scaffolder --reference-url URL`` would
    exercise: parse flag → fetch + extract → write
    ``.omnisight/brand.json`` → later agent reads it back.  The matrix
    pins the entire chain rather than the extractor alone.
    """

    def test_resolver_returns_same_spec_as_extractor(
        self, case: ReferenceCase,
    ):
        direct = extract_brand_from_url(
            case.url,
            fetch=_fake_fetch_for(case),
            now=_fake_now,
        )
        via_resolver = resolve_reference_url(
            case.url,
            fetch=_fake_fetch_for(case),
            now=_fake_now,
        )
        assert via_resolver == direct

    def test_resolver_then_store_round_trip(
        self, case: ReferenceCase, tmp_path: Path,
    ):
        spec = resolve_reference_url(
            case.url,
            fetch=_fake_fetch_for(case),
            now=_fake_now,
        )
        assert spec is not None
        path = write_brand_spec(spec, project_root=tmp_path)
        assert path.exists()
        loaded = read_brand_spec(tmp_path)
        assert loaded == spec

    def test_soft_read_returns_same_spec(
        self, case: ReferenceCase, tmp_path: Path,
    ):
        spec = resolve_reference_url(
            case.url,
            fetch=_fake_fetch_for(case),
            now=_fake_now,
        )
        assert spec is not None
        write_brand_spec(spec, project_root=tmp_path)
        loaded = read_brand_spec_if_exists(tmp_path)
        assert loaded == spec


# ── Network discipline ──────────────────────────────────────────────


class TestNetworkDiscipline:
    """No test in this matrix may touch the real network — every
    call uses ``_fake_fetch_for`` + ``_fake_now``.  This class enforces
    the discipline as a static assertion against the matrix structure."""

    def test_no_real_url_in_fixtures(self):
        # Reserved ``.example`` TLD per RFC 2606 — defence-in-depth.
        for c in REFERENCE_FIXTURES:
            assert ".example/" in c.url, c.url

    def test_fetch_callable_is_required_by_url_path(self):
        # Sanity: omitting ``fetch=`` would invoke the default urllib
        # fetcher and try DNS.  We never want that in this matrix.
        case = REFERENCE_FIXTURES[0]
        sentinel: list[str] = []

        def fetch(url: str) -> tuple[int, str]:
            sentinel.append(url)
            return 200, case.payload

        extract_brand_from_url(case.url, fetch=fetch, now=_fake_now)
        assert sentinel == [case.url]

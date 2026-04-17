"""V4 #4 (issue #320) — brand-consistency validator contract tests.

Pins ``backend/brand_consistency_validator.py`` against:

  * structural invariants of :class:`BrandRule` / :class:`BrandViolation` /
    :class:`BrandValidationReport` / :class:`AllowedBrandSets`;
  * the rule catalogue (stable ids, stable warn-only severity);
  * every extractor (hex / rgb / hsl / font-family / tailwind palette /
    var) pinned against both the positive and the negative path;
  * the ``warn``-only contract — the post-deploy gate is coaching,
    NOT gating (task rubric: "違規項列為 warning");
  * graceful fallback paths: None tokens, missing directory, unreadable
    files, fetch-failure all yield a clean well-formed report rather
    than raising;
  * round-trip JSON safety of :meth:`BrandValidationReport.to_dict` and
    :func:`run_brand_consistency_validator`;
  * byte-determinism of :func:`render_report`.

If a new extractor, rule id or severity lands, update this file in the
same commit — the deliberately strict catalogue assertion stops silent
drift.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import brand_consistency_validator as bcv
from backend.brand_consistency_validator import (
    AllowedBrandSets,
    BrandRule,
    BrandValidationReport,
    BrandViolation,
    DEFAULT_EXCLUDES,
    RULES,
    SCAN_EXTENSIONS,
    SEVERITIES,
    VALIDATOR_SCHEMA_VERSION,
    collect_allowed_colors,
    collect_allowed_css_var_names,
    collect_allowed_fonts,
    color_allowed,
    extract_font_families,
    extract_hex_colors,
    extract_hsl_colors,
    extract_rgb_colors,
    extract_tailwind_palette_classes,
    font_allowed,
    hsl_to_hex,
    iter_asset_files,
    normalize_font_name,
    normalize_hex,
    render_report,
    report_to_json,
    rgb_to_hex,
    run_brand_consistency_validator,
    scan_build_artifact,
    scan_text,
    scan_url,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ── Module-level invariants ──────────────────────────────────────────


class TestModuleInvariants:
    def test_schema_version_semver(self):
        parts = VALIDATOR_SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_severities_warn_only(self):
        assert SEVERITIES == ("warn",)

    def test_scan_extensions_cover_deploy_artefacts(self):
        # Every extension is lowercase and starts with a dot.
        for ext in SCAN_EXTENSIONS:
            assert ext.startswith("."), ext
            assert ext == ext.lower(), ext
        # We MUST scan HTML + CSS + JS — the three deliverable surfaces.
        assert ".html" in SCAN_EXTENSIONS
        assert ".css" in SCAN_EXTENSIONS
        assert ".js" in SCAN_EXTENSIONS

    def test_default_excludes_non_empty(self):
        assert "node_modules" in DEFAULT_EXCLUDES
        assert ".git" in DEFAULT_EXCLUDES

    def test_exports_alphabetised(self):
        assert bcv.__all__ == sorted(bcv.__all__)

    def test_public_surface_exported(self):
        for name in (
            "RULES", "SEVERITIES", "scan_text", "scan_build_artifact",
            "scan_url", "run_brand_consistency_validator",
            "collect_allowed_colors", "collect_allowed_fonts",
            "normalize_hex", "normalize_font_name",
            "BrandRule", "BrandViolation", "BrandValidationReport",
            "AllowedBrandSets",
        ):
            assert name in bcv.__all__, name


class TestRuleCatalogue:
    def test_rules_is_immutable_mapping(self):
        with pytest.raises(TypeError):
            RULES["color-out-of-palette"] = None  # type: ignore[misc]

    def test_expected_rule_ids(self):
        expected = {
            "color-out-of-palette",
            "rgb-out-of-palette",
            "hsl-out-of-palette",
            "font-out-of-stack",
            "hard-pinned-palette-class",
            "unknown-css-var",
        }
        assert set(RULES) == expected

    @pytest.mark.parametrize("rule_id", sorted(RULES))
    def test_every_rule_is_warn_only(self, rule_id):
        assert RULES[rule_id].severity == "warn", (
            "Post-deploy validator must not emit errors; task rubric "
            "says 違規項列為 warning."
        )

    @pytest.mark.parametrize("rule_id", sorted(RULES))
    def test_rule_fields_valid(self, rule_id):
        rule = RULES[rule_id]
        assert rule.rule_id == rule_id
        assert rule.summary.strip()

    def test_bad_severity_rejected(self):
        with pytest.raises(ValueError):
            BrandRule("x", "error", "nope")

    def test_blank_rule_id_rejected(self):
        with pytest.raises(ValueError):
            BrandRule("", "warn", "nope")

    def test_blank_summary_rejected(self):
        with pytest.raises(ValueError):
            BrandRule("x", "warn", "   ")


class TestBrandViolationDataclass:
    def test_is_frozen(self):
        v = BrandViolation(
            rule_id="color-out-of-palette",
            severity="warn",
            source="a.css",
            line=1, column=1,
            offender="#ff00aa",
            message="m",
        )
        with pytest.raises(FrozenInstanceError):
            v.line = 2  # type: ignore[misc]

    def test_rejects_unknown_rule_id(self):
        with pytest.raises(ValueError):
            BrandViolation(
                rule_id="made-up", severity="warn", source="a",
                line=1, column=1, offender="x", message="m",
            )

    def test_rejects_line_zero(self):
        with pytest.raises(ValueError):
            BrandViolation(
                rule_id="color-out-of-palette", severity="warn",
                source="a", line=0, column=1, offender="x", message="m",
            )

    def test_rejects_blank_offender(self):
        with pytest.raises(ValueError):
            BrandViolation(
                rule_id="color-out-of-palette", severity="warn",
                source="a", line=1, column=1, offender="", message="m",
            )


class TestAllowedBrandSets:
    def test_default_empty(self):
        a = AllowedBrandSets()
        assert a.colors == frozenset()
        assert a.fonts == frozenset()
        assert a.css_var_names == frozenset()

    def test_to_dict_sorted(self):
        a = AllowedBrandSets(
            colors=frozenset({"#ff0000", "#00ff00"}),
            fonts=frozenset({"inter", "mono"}),
            css_var_names=frozenset({"primary", "background"}),
        )
        d = a.to_dict()
        assert d["colors"] == ["#00ff00", "#ff0000"]
        assert d["fonts"] == ["inter", "mono"]
        assert d["css_var_names"] == ["background", "primary"]


# ── Canonicalisation helpers ─────────────────────────────────────────


class TestNormalizeHex:
    @pytest.mark.parametrize("raw, expected", [
        ("#abc", "#aabbcc"),
        ("#ABC", "#aabbcc"),
        ("#abcf", "#aabbcc"),
        ("#ABCDEF", "#abcdef"),
        ("#aabbcc", "#aabbcc"),
        ("#aabbccdd", "#aabbcc"),
        ("  #ff0000  ", "#ff0000"),
    ])
    def test_canonicalises(self, raw, expected):
        assert normalize_hex(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", "not-a-color", "#", "#gg", "#12345", "ff0000", None, 0, "#abcde",
    ])
    def test_rejects_malformed(self, raw):
        assert normalize_hex(raw) is None


class TestNormalizeFontName:
    @pytest.mark.parametrize("raw, expected", [
        ("Inter", "inter"),
        ("'Inter'", "inter"),
        ('"Inter"', "inter"),
        ("  Inter  ", "inter"),
        ("Helvetica Neue", "helvetica neue"),
    ])
    def test_canonicalises(self, raw, expected):
        assert normalize_font_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", None, 0])
    def test_rejects_empty(self, raw):
        assert normalize_font_name(raw) is None


class TestRgbToHex:
    @pytest.mark.parametrize("r, g, b, expected", [
        (255, 0, 0, "#ff0000"),
        (0, 255, 0, "#00ff00"),
        (0, 0, 255, "#0000ff"),
        (17, 34, 51, "#112233"),
        (-10, 999, 255.4, "#00ffff"),   # out-of-range clamping
        (128.6, 128.4, 128.5, "#818080"),
    ])
    def test_roundtrip(self, r, g, b, expected):
        assert rgb_to_hex(r, g, b) == expected


class TestHslToHex:
    @pytest.mark.parametrize("h, s, lightness, expected", [
        (0, 1.0, 0.5, "#ff0000"),
        (120, 1.0, 0.5, "#00ff00"),
        (240, 1.0, 0.5, "#0000ff"),
        (0, 0, 0.5, "#808080"),
        (0, 0, 1.0, "#ffffff"),
        (0, 0, 0.0, "#000000"),
    ])
    def test_known_pairs(self, h, s, lightness, expected):
        assert hsl_to_hex(h, s, lightness) == expected

    def test_out_of_range_clamped(self):
        # 480 degrees = 120 mod 360 → green
        assert hsl_to_hex(480, 1.5, 0.5) == "#00ff00"


# ── Extractors ───────────────────────────────────────────────────────


class TestExtractHexColors:
    def test_finds_multiple(self):
        out = extract_hex_colors("a: #abc; b: #ffffff; c: #12345678;")
        assert [c for c, _ in out] == ["#abc", "#ffffff", "#12345678"]

    def test_ignores_url_fragment(self):
        # `#top` after `href=` must not match — there's no `#` at word-
        # boundary start following a hex string.  Our boundary is "no
        # hex digit immediately before `#`" which means this DOES match
        # and gets rejected later by normalize_hex.  We assert the
        # extractor stays bounded and the normaliser says "None".
        raw = 'a href="#top"'
        out = extract_hex_colors(raw)
        # `#top` has "top" which is not all hex, so the extractor's
        # look-ahead (`(?![0-9a-fA-F])`) rejects it.
        assert out == ()

    def test_ignores_longer_sequences(self):
        # `#abcdefabc` is 9 hex chars — not 3/4/6/8. Must not match.
        out = extract_hex_colors("x: #abcdefabc;")
        assert out == ()

    def test_empty_input(self):
        assert extract_hex_colors("") == ()

    def test_non_str_input(self):
        assert extract_hex_colors(None) == ()  # type: ignore[arg-type]


class TestExtractRgbColors:
    def test_finds_comma_form(self):
        out = extract_rgb_colors("color: rgb(255, 0, 0);")
        assert out[0][0] == "#ff0000"

    def test_finds_space_form(self):
        out = extract_rgb_colors("background: rgb(0 128 255);")
        assert out[0][0] == "#0080ff"

    def test_finds_with_alpha(self):
        out = extract_rgb_colors("c: rgba(10, 20, 30, 0.5);")
        assert out[0][0] == "#0a141e"

    def test_percentage_form(self):
        out = extract_rgb_colors("c: rgb(100%, 0%, 0%);")
        assert out[0][0] == "#ff0000"

    def test_non_str_input(self):
        assert extract_rgb_colors(None) == ()  # type: ignore[arg-type]


class TestExtractHslColors:
    def test_finds_standard(self):
        out = extract_hsl_colors("c: hsl(0, 100%, 50%);")
        assert out[0][0] == "#ff0000"

    def test_finds_with_degrees(self):
        out = extract_hsl_colors("c: hsl(240deg 100% 50%);")
        assert out[0][0] == "#0000ff"

    def test_finds_with_alpha(self):
        out = extract_hsl_colors("c: hsla(120, 100%, 50%, 0.5);")
        assert out[0][0] == "#00ff00"

    def test_turn_unit(self):
        out = extract_hsl_colors("c: hsl(0.5turn 100% 50%);")
        # 0.5 turn = 180 deg → cyan
        assert out[0][0] == "#00ffff"


class TestExtractFontFamilies:
    def test_plain_css(self):
        out = extract_font_families('body { font-family: "Inter", sans-serif; }')
        # Normalise for comparison — just check the value side contains Inter.
        assert any("Inter" in v for v, _ in out)

    def test_jsx_inline_style(self):
        out = extract_font_families("style={{ fontFamily: 'Inter' }}")
        assert out[0][0] == "Inter"

    def test_multiple_decls(self):
        out = extract_font_families(
            "h1 { font-family: Inter } h2 { font-family: Mono }"
        )
        assert len(out) == 2

    def test_empty_value_ignored(self):
        # `font-family: ;` is malformed — no family listed — must not emit.
        out = extract_font_families("h1 { font-family:  ; }")
        # Our regex matches but the value strips to empty string, which
        # the extractor filters out.
        assert all(v.strip() for v, _ in out)


class TestExtractTailwindPaletteClasses:
    def test_finds_bg(self):
        out = extract_tailwind_palette_classes('<div className="bg-slate-900">')
        assert out[0][0] == "bg-slate-900"

    def test_finds_multiple_utilities(self):
        raw = "bg-blue-500 text-rose-600 ring-amber-300"
        out = extract_tailwind_palette_classes(raw)
        assert {name for name, _ in out} == {
            "bg-blue-500", "text-rose-600", "ring-amber-300",
        }

    def test_ignores_non_palette(self):
        out = extract_tailwind_palette_classes("text-primary bg-background")
        assert out == ()

    def test_non_str_input(self):
        assert extract_tailwind_palette_classes(None) == ()  # type: ignore[arg-type]


# ── Allowed-set builders ─────────────────────────────────────────────


class _FakeTok(SimpleNamespace):
    """Stand-in for DesignToken without importing the loader."""


class _FakeTokens(SimpleNamespace):
    """Stand-in for DesignTokens with only the ``all_tokens`` surface."""


def _make_tokens(tokens):
    return _FakeTokens(all_tokens=tuple(tokens))


class TestCollectAllowedColors:
    def test_extracts_hex_from_tokens(self):
        toks = _make_tokens([
            _FakeTok(name="primary", value="#38bdf8", kind="color", scope="root"),
            _FakeTok(name="accent", value="#FF00AA", kind="color", scope="root"),
        ])
        allowed = collect_allowed_colors(toks)
        assert allowed == frozenset({"#38bdf8", "#ff00aa"})

    def test_extracts_rgb_from_tokens(self):
        toks = _make_tokens([
            _FakeTok(name="x", value="rgb(255, 0, 0)", kind="color", scope="root"),
        ])
        assert collect_allowed_colors(toks) == frozenset({"#ff0000"})

    def test_extracts_hsl_from_tokens(self):
        toks = _make_tokens([
            _FakeTok(name="x", value="hsl(0, 100%, 50%)", kind="color", scope="root"),
        ])
        assert collect_allowed_colors(toks) == frozenset({"#ff0000"})

    def test_ignores_non_color_kind(self):
        toks = _make_tokens([
            _FakeTok(name="spacing", value="#ff0000", kind="spacing", scope="root"),
        ])
        assert collect_allowed_colors(toks) == frozenset()

    def test_none_tokens_empty(self):
        assert collect_allowed_colors(None) == frozenset()


class TestCollectAllowedFonts:
    def test_extracts_stack(self):
        toks = _make_tokens([
            _FakeTok(
                name="font-sans",
                value="Inter, 'Helvetica Neue', sans-serif",
                kind="font", scope="root",
            ),
        ])
        allowed = collect_allowed_fonts(toks)
        assert "inter" in allowed
        assert "helvetica neue" in allowed
        assert "sans-serif" in allowed

    def test_none_tokens_empty(self):
        assert collect_allowed_fonts(None) == frozenset()


class TestCollectAllowedCssVarNames:
    def test_captures_names(self):
        toks = _make_tokens([
            _FakeTok(name="primary", value="#fff", kind="color", scope="root"),
            _FakeTok(name="font-sans", value="Inter", kind="font", scope="root"),
        ])
        assert collect_allowed_css_var_names(toks) == frozenset(
            {"primary", "font-sans"}
        )


# ── Matchers ─────────────────────────────────────────────────────────


class TestColorAllowed:
    def test_short_form_matches_long(self):
        allowed = frozenset({"#aabbcc"})
        assert color_allowed("#abc", allowed)
        assert color_allowed("#AABBCC", allowed)

    def test_alpha_stripped(self):
        assert color_allowed("#aabbccdd", frozenset({"#aabbcc"}))

    def test_unknown_denied(self):
        assert not color_allowed("#112233", frozenset({"#aabbcc"}))

    def test_malformed_denied(self):
        assert not color_allowed("garbage", frozenset({"#aabbcc"}))


class TestFontAllowed:
    def test_generic_keyword_always_allowed(self):
        assert font_allowed("sans-serif", frozenset())
        assert font_allowed("monospace", frozenset())
        assert font_allowed("serif", frozenset())
        assert font_allowed("system-ui", frozenset())

    def test_allowed_name(self):
        assert font_allowed("Inter", frozenset({"inter"}))
        assert font_allowed("'Inter'", frozenset({"inter"}))

    def test_unknown_denied(self):
        assert not font_allowed("Comic Sans", frozenset({"inter"}))


# ── scan_text core contract ──────────────────────────────────────────


class TestScanText:
    def test_empty_text(self):
        violations = scan_text("", AllowedBrandSets())
        assert violations == ()

    def test_bad_type_raises(self):
        with pytest.raises(TypeError):
            scan_text(b"not a str", AllowedBrandSets())  # type: ignore[arg-type]

    def test_bad_allowed_type_raises(self):
        with pytest.raises(TypeError):
            scan_text("x", {"colors": frozenset()})  # type: ignore[arg-type]

    def test_hex_outside_palette_flagged(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        violations = scan_text("p { color: #ff00aa; }", allowed, source="x.css")
        rule_ids = [v.rule_id for v in violations]
        assert "color-out-of-palette" in rule_ids

    def test_hex_inside_palette_not_flagged(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        violations = scan_text("p { color: #FFFFFF; }", allowed, source="x.css")
        assert all(v.rule_id != "color-out-of-palette" for v in violations)

    def test_rgb_outside_palette_flagged(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        violations = scan_text("p { color: rgb(0, 0, 0); }", allowed, source="x.css")
        assert any(v.rule_id == "rgb-out-of-palette" for v in violations)

    def test_rgb_inside_palette_not_flagged(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ff0000"}))
        violations = scan_text("p { color: rgb(255,0,0); }", allowed)
        assert all(v.rule_id != "rgb-out-of-palette" for v in violations)

    def test_hsl_outside_palette_flagged(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        violations = scan_text("p { color: hsl(0, 100%, 50%); }", allowed)
        assert any(v.rule_id == "hsl-out-of-palette" for v in violations)

    def test_unknown_font_flagged(self):
        allowed = AllowedBrandSets(fonts=frozenset({"inter"}))
        violations = scan_text(
            "body { font-family: 'Comic Sans', sans-serif; }", allowed,
        )
        assert any(v.rule_id == "font-out-of-stack" for v in violations)

    def test_allowed_font_not_flagged(self):
        allowed = AllowedBrandSets(fonts=frozenset({"inter"}))
        violations = scan_text(
            "body { font-family: Inter, sans-serif; }", allowed,
        )
        assert all(v.rule_id != "font-out-of-stack" for v in violations)

    def test_tailwind_palette_flagged(self):
        violations = scan_text(
            '<div className="bg-slate-900 text-white">', AllowedBrandSets(),
        )
        assert any(v.rule_id == "hard-pinned-palette-class" for v in violations)

    def test_semantic_utilities_not_flagged(self):
        violations = scan_text(
            '<div className="bg-primary text-foreground">', AllowedBrandSets(),
        )
        assert all(
            v.rule_id != "hard-pinned-palette-class" for v in violations
        )

    def test_unknown_css_var_flagged(self):
        allowed = AllowedBrandSets(css_var_names=frozenset({"primary"}))
        violations = scan_text("p { color: var(--made-up); }", allowed)
        assert any(v.rule_id == "unknown-css-var" for v in violations)

    def test_known_css_var_not_flagged(self):
        allowed = AllowedBrandSets(css_var_names=frozenset({"primary"}))
        violations = scan_text("p { color: var(--primary); }", allowed)
        assert all(v.rule_id != "unknown-css-var" for v in violations)

    def test_css_var_check_skipped_if_no_allowlist(self):
        # Empty allowlist => validator has no way to know what's
        # defined; do not emit unknown-css-var warnings (would flag
        # every project with var() usage that hasn't provided tokens).
        violations = scan_text("p { color: var(--primary); }", AllowedBrandSets())
        assert all(v.rule_id != "unknown-css-var" for v in violations)

    def test_violations_sorted_deterministic(self):
        text = "a {color: #ff00aa;} b {color: #ffee00;}"
        allowed = AllowedBrandSets(colors=frozenset())
        v1 = scan_text(text, allowed, source="x.css")
        v2 = scan_text(text, allowed, source="x.css")
        assert v1 == v2
        assert all(
            (a.source, a.line, a.column, a.rule_id)
            <= (b.source, b.line, b.column, b.rule_id)
            for a, b in zip(v1, v1[1:])
        )

    def test_every_violation_is_warn(self):
        text = (
            "a {color: #ff00aa; font-family: 'Nope'; bg-slate-900; "
            "color: rgb(0,0,0); color: hsl(0,100%,50%); color: var(--nope);}"
        )
        allowed = AllowedBrandSets(css_var_names=frozenset({"primary"}))
        for v in scan_text(text, allowed):
            assert v.severity == "warn"


# ── Directory walker ─────────────────────────────────────────────────


class TestIterAssetFiles:
    def test_missing_dir_empty(self, tmp_path):
        assert iter_asset_files(tmp_path / "no-such") == ()

    def test_lists_files(self, tmp_path):
        (tmp_path / "a.css").write_text("a{}", encoding="utf-8")
        (tmp_path / "b.html").write_text("<div></div>", encoding="utf-8")
        (tmp_path / "c.txt").write_text("not scanned", encoding="utf-8")
        files = iter_asset_files(tmp_path)
        names = {f.name for f in files}
        assert names == {"a.css", "b.html"}

    def test_skips_excluded_dirs(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "evil.css").write_text("a{}", encoding="utf-8")
        (tmp_path / "ok.css").write_text("a{}", encoding="utf-8")
        files = iter_asset_files(tmp_path)
        assert {f.name for f in files} == {"ok.css"}

    def test_deterministic_sorted(self, tmp_path):
        for n in ["z.css", "a.css", "m.css"]:
            (tmp_path / n).write_text("a{}", encoding="utf-8")
        files = iter_asset_files(tmp_path)
        assert [f.name for f in files] == ["a.css", "m.css", "z.css"]


# ── scan_build_artifact ──────────────────────────────────────────────


class TestScanBuildArtifact:
    def test_missing_dir_returns_empty_report(self, tmp_path):
        report = scan_build_artifact(tmp_path / "missing")
        assert report.is_clean
        assert report.scanned_sources == ()

    def test_clean_artefact(self, tmp_path):
        (tmp_path / "app.css").write_text(
            ":root{--primary:#ffffff;} p{color:var(--primary);}",
            encoding="utf-8",
        )
        allowed = AllowedBrandSets(
            colors=frozenset({"#ffffff"}),
            css_var_names=frozenset({"primary"}),
        )
        report = scan_build_artifact(tmp_path, allowed)
        assert report.is_clean
        assert "app.css" in report.scanned_sources

    def test_reports_violation(self, tmp_path):
        (tmp_path / "app.css").write_text(
            "p { color: #123456; }", encoding="utf-8",
        )
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        report = scan_build_artifact(tmp_path, allowed)
        assert not report.is_clean
        assert len(report.violations) == 1
        v = report.violations[0]
        assert v.rule_id == "color-out-of-palette"
        assert v.source == "app.css"

    def test_excludes_node_modules(self, tmp_path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "evil.css").write_text("p{color:#112233;}", encoding="utf-8")
        (tmp_path / "ok.css").write_text("p{}", encoding="utf-8")
        report = scan_build_artifact(tmp_path)
        assert "ok.css" in report.scanned_sources
        assert not any("node_modules" in s for s in report.scanned_sources)

    def test_skips_oversize_file(self, tmp_path):
        big = tmp_path / "big.css"
        big.write_text("a{color:#123456;}" + "/*pad*/" * 100, encoding="utf-8")
        report = scan_build_artifact(
            tmp_path, max_bytes_per_file=10,
        )
        assert "big.css" not in report.scanned_sources

    def test_skips_unreadable_file(self, tmp_path):
        # Write something that's not valid UTF-8.
        bad = tmp_path / "bad.css"
        bad.write_bytes(b"\xff\xfe\xfa invalid-utf8-bytes")
        (tmp_path / "ok.css").write_text("a{}", encoding="utf-8")
        report = scan_build_artifact(tmp_path)
        # bad.css is counted as "tried" — iter_asset_files finds it —
        # but not as "scanned_sources" because read_text failed.
        assert "bad.css" not in report.scanned_sources
        assert "ok.css" in report.scanned_sources

    def test_tokens_allow_net_new(self, tmp_path):
        """When tokens are a DesignTokens-shaped object, the scanner
        distils the allow-list itself and treats all its colours as ok."""
        toks = _make_tokens([
            _FakeTok(name="primary", value="#38bdf8", kind="color", scope="root"),
        ])
        (tmp_path / "app.css").write_text(
            "p { color: #38bdf8; }", encoding="utf-8",
        )
        report = scan_build_artifact(tmp_path, toks)
        assert report.is_clean


# ── scan_url ─────────────────────────────────────────────────────────


class TestScanUrl:
    def test_rejects_blank_url(self):
        with pytest.raises(ValueError):
            scan_url("   ")

    def test_uses_injected_fetch(self):
        def fake_fetch(url):
            assert url == "https://example.com"
            return (200, "p { color: #ff00aa; }")

        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        report = scan_url("https://example.com", allowed, fetch=fake_fetch)
        assert not report.is_clean
        assert report.scanned_sources == ("https://example.com",)

    def test_non_200_empty(self):
        def fake_fetch(url):
            return (404, "not found")

        report = scan_url("https://example.com", fetch=fake_fetch)
        assert report.is_clean

    def test_fetch_raises_empty_report(self):
        def fake_fetch(url):
            raise RuntimeError("network down")

        report = scan_url("https://example.com", fetch=fake_fetch)
        assert report.is_clean


# ── Report rendering / JSON ──────────────────────────────────────────


class TestRenderReport:
    def test_clean_report(self):
        report = BrandValidationReport(
            scanned_sources=("app.css",),
            allowed=AllowedBrandSets(),
        )
        md = render_report(report)
        assert "no brand drift detected" in md
        assert md.endswith("\n")

    def test_with_violations(self):
        v = BrandViolation(
            rule_id="color-out-of-palette",
            severity="warn",
            source="app.css",
            line=3, column=5,
            offender="#ff00aa",
            message="bad",
        )
        report = BrandValidationReport(
            violations=(v,),
            scanned_sources=("app.css",),
        )
        md = render_report(report)
        assert "1 warning(s)" in md
        assert "`color-out-of-palette`: 1" in md
        assert "app.css" in md

    def test_deterministic(self):
        v = BrandViolation(
            rule_id="color-out-of-palette", severity="warn",
            source="a.css", line=1, column=2, offender="#abc", message="m",
        )
        report = BrandValidationReport(violations=(v,))
        assert render_report(report) == render_report(report)


class TestReportToJson:
    def test_roundtrip(self):
        v = BrandViolation(
            rule_id="color-out-of-palette", severity="warn",
            source="a.css", line=1, column=1, offender="#ff00aa", message="m",
        )
        report = BrandValidationReport(violations=(v,))
        blob = report_to_json(report)
        decoded = json.loads(blob)
        assert decoded["violations"][0]["rule_id"] == "color-out-of-palette"
        assert decoded["is_clean"] is False


class TestReportDict:
    def test_is_clean_empty(self):
        report = BrandValidationReport()
        assert report.is_clean
        assert report.severity_counts == {"warn": 0}
        assert report.rule_counts == {}

    def test_rule_counts_sorted(self):
        v1 = BrandViolation(
            rule_id="color-out-of-palette", severity="warn",
            source="a", line=1, column=1, offender="#abc", message="m",
        )
        v2 = BrandViolation(
            rule_id="font-out-of-stack", severity="warn",
            source="a", line=2, column=1, offender="X", message="m",
        )
        report = BrandValidationReport(violations=(v1, v2))
        assert list(report.rule_counts) == [
            "color-out-of-palette", "font-out-of-stack",
        ]

    def test_violations_for(self):
        v1 = BrandViolation(
            rule_id="color-out-of-palette", severity="warn",
            source="a", line=1, column=1, offender="#abc", message="m",
        )
        report = BrandValidationReport(violations=(v1,))
        assert report.violations_for("color-out-of-palette") == (v1,)
        assert report.violations_for("font-out-of-stack") == ()

    def test_to_dict_shape(self):
        v = BrandViolation(
            rule_id="color-out-of-palette", severity="warn",
            source="a", line=1, column=1, offender="#abc", message="m",
        )
        report = BrandValidationReport(violations=(v,))
        d = report.to_dict()
        for k in ("schema_version", "is_clean", "scanned_sources",
                  "severity_counts", "rule_counts", "allowed", "violations"):
            assert k in d
        assert d["schema_version"] == VALIDATOR_SCHEMA_VERSION


# ── Agent-facing tool ────────────────────────────────────────────────


class TestRunBrandConsistencyValidator:
    def test_requires_exactly_one_input(self):
        with pytest.raises(ValueError):
            run_brand_consistency_validator()
        with pytest.raises(ValueError):
            run_brand_consistency_validator(text="x", url="http://x")

    def test_text_mode(self):
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        out = run_brand_consistency_validator(
            text="p { color: #ff00aa; }", tokens=allowed,
        )
        assert out["schema_version"] == VALIDATOR_SCHEMA_VERSION
        assert out["is_clean"] is False
        assert "markdown" in out
        rule_ids = {v["rule_id"] for v in out["violations"]}
        assert "color-out-of-palette" in rule_ids

    def test_build_artifact_mode(self, tmp_path):
        (tmp_path / "x.css").write_text("p{color:#aaaaaa;}", encoding="utf-8")
        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        out = run_brand_consistency_validator(
            build_artifact=str(tmp_path), tokens=allowed,
        )
        assert out["is_clean"] is False
        assert "x.css" in out["scanned_sources"]

    def test_url_mode_with_fetcher(self):
        def fetch(url):
            return (200, "p{color:#112233;}")

        allowed = AllowedBrandSets(colors=frozenset({"#ffffff"}))
        out = run_brand_consistency_validator(
            url="https://example.com", tokens=allowed, fetch=fetch,
        )
        assert out["is_clean"] is False

    def test_empty_tokens_still_returns_dict(self):
        out = run_brand_consistency_validator(text="p{}")
        assert isinstance(out, dict)
        assert out["is_clean"] is True

    def test_project_root_loads_tokens_live(self, tmp_path):
        # Synthesise a tiny project with a globals.css that defines
        # one colour token; the validator should honour it.
        app = tmp_path / "app"
        app.mkdir()
        (app / "globals.css").write_text(
            ":root { --primary: #38bdf8; }", encoding="utf-8",
        )
        out = run_brand_consistency_validator(
            text="p{color:#38bdf8;}",
            project_root=str(tmp_path),
        )
        assert out["is_clean"] is True

    def test_result_is_json_safe(self):
        out = run_brand_consistency_validator(text="p{color:#112233;}")
        # Must round-trip through JSON.
        serialised = json.dumps(out)
        assert json.loads(serialised)["schema_version"] == VALIDATOR_SCHEMA_VERSION


# ── Live project integration (smoke) ─────────────────────────────────


class TestLiveProjectIntegration:
    def test_loader_integration_smokes(self):
        """With real DesignTokens loaded from the checked-in project,
        scanning its own globals.css should never produce a colour
        warning for the project's own brand colours — those are the
        allow-list by definition."""
        from backend.design_token_loader import load_design_tokens

        tokens = load_design_tokens(PROJECT_ROOT)
        allowed = AllowedBrandSets(
            colors=collect_allowed_colors(tokens),
            fonts=collect_allowed_fonts(tokens),
            css_var_names=collect_allowed_css_var_names(tokens),
        )
        # Build a payload that references ONLY allowed tokens.
        payload = (
            ":root { --foo: #38bdf8; }\n"
            "body { color: var(--neural-blue); font-family: Inter, sans-serif; }"
        )
        violations = scan_text(payload, allowed, source="inline.css")
        colour_warns = [v for v in violations if v.rule_id.endswith("-out-of-palette")]
        # `#38bdf8` IS in the project palette (see app/globals.css).
        assert all(v.offender != "#38bdf8" for v in colour_warns)

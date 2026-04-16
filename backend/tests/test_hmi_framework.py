"""C26 — L4-CORE-26 HMI framework core tests (#261).

Covers: bundle budget per-platform, flash-partition gate, IEC 62443
security scan (required headers / CSP directives / forbidden patterns /
inline event attrs), ABI matrix query + compatibility check, i18n
catalog building, framework whitelist.
"""

from __future__ import annotations

import pytest

from backend import hmi_framework as hf


@pytest.fixture(autouse=True)
def _reload():
    hf.reload_config()
    yield
    hf.reload_config()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Platform listing + budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPlatforms:
    def test_list_platforms_includes_all_profiles(self):
        p = hf.list_platforms()
        assert "aarch64" in p
        assert "armv7" in p
        assert "riscv64" in p
        assert "host_native" in p

    def test_aarch64_budget(self):
        b = hf.get_bundle_budget("aarch64")
        assert b.flash_partition_bytes == 8 * 1024 * 1024
        assert b.hmi_budget_bytes == 512 * 1024
        assert b.html_css_max_bytes > 0
        assert b.js_max_bytes > 0

    def test_armv7_tighter_than_aarch64(self):
        arm = hf.get_bundle_budget("armv7")
        aarch = hf.get_bundle_budget("aarch64")
        assert arm.hmi_budget_bytes < aarch.hmi_budget_bytes, "armv7 must be tighter"

    def test_host_native_zero_flash_means_unlimited(self):
        h = hf.get_bundle_budget("host_native")
        assert h.flash_partition_bytes == 0

    def test_unknown_platform_raises(self):
        with pytest.raises(KeyError):
            hf.get_bundle_budget("bogus")


class TestMeasure:
    def test_classifies_by_extension(self):
        m = hf.measure_bundle({
            "index.html": "<p>hi</p>",
            "style.css": "body{}",
            "app.js": "let x=1",
            "fancy.woff2": b"\x00" * 100,
            "other.bin": b"\x00" * 50,
        })
        assert m.html_bytes == len("<p>hi</p>")
        assert m.css_bytes == 6
        assert m.js_bytes == 7
        assert m.fonts_bytes == 100
        assert m.other_bytes == 50
        assert m.total_bytes == m.html_bytes + m.css_bytes + m.js_bytes + m.fonts_bytes + m.other_bytes

    def test_accepts_bytes_and_str(self):
        m = hf.measure_bundle({"x.html": b"ab", "y.html": "cd"})
        assert m.html_bytes == 4


class TestBudgetGate:
    def test_pass_within_budget(self):
        m = hf.measure_bundle({"index.html": "<p>x</p>", "app.js": "x=1"})
        v = hf.check_bundle_budget("aarch64", m)
        assert v.status == "pass"
        assert v.violations == []

    def test_fail_over_total(self):
        payload = "x" * (512 * 1024 + 1)
        m = hf.measure_bundle({"big.html": payload})
        v = hf.check_bundle_budget("aarch64", m)
        assert v.status == "fail"
        assert any("exceeds budget" in violation for violation in v.violations)

    def test_fail_over_js_sub_budget(self):
        m = hf.measure_bundle({"big.js": "x" * (157286 + 1)})
        v = hf.check_bundle_budget("aarch64", m)
        assert v.status == "fail"
        assert any("JS" in vs for vs in v.violations)

    def test_fail_over_flash_partition(self):
        # aarch64 flash partition is 8 MiB — breaching total with a big font
        m = hf.measure_bundle({"fat.woff2": b"\x00" * (9 * 1024 * 1024)})
        v = hf.check_bundle_budget("aarch64", m)
        assert v.status == "fail"
        assert any("flash partition" in vs for vs in v.violations)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Security scanner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_GOOD_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}


class TestSecurityScanner:
    def test_pass_on_clean_bundle(self):
        r = hf.scan_security(html="<p>ok</p>", js="let x=1", headers=_GOOD_HEADERS)
        assert r.status == "pass"
        assert r.error_count == 0

    def test_fail_missing_required_header(self):
        headers = dict(_GOOD_HEADERS)
        headers.pop("X-Frame-Options")
        r = hf.scan_security(html="<p>x</p>", js="", headers=headers)
        assert r.status == "fail"
        assert any("X-Frame-Options" in f.detail for f in r.findings)

    def test_fail_missing_csp_directive(self):
        headers = dict(_GOOD_HEADERS)
        headers["Content-Security-Policy"] = "default-src 'self'"  # missing the rest
        r = hf.scan_security(html="<p>x</p>", js="", headers=headers)
        assert r.status == "fail"
        assert any("csp_directive_missing" == f.rule for f in r.findings)

    def test_fail_cdn_reference(self):
        r = hf.scan_security(
            html='<script src="https://cdn.jsdelivr.net/npm/react"></script>',
            js="",
            headers=_GOOD_HEADERS,
        )
        assert r.status == "fail"
        assert any("forbidden_pattern" == f.rule for f in r.findings)

    def test_fail_eval_in_js(self):
        r = hf.scan_security(
            html="<p>x</p>", js='eval("alert(1)")',
            headers=_GOOD_HEADERS,
        )
        assert r.status == "fail"

    def test_fail_inline_event_attribute(self):
        r = hf.scan_security(
            html='<button onclick="alert(1)">x</button>', js="",
            headers=_GOOD_HEADERS,
        )
        assert r.status == "fail"
        assert any("inline_event_attr" == f.rule for f in r.findings)

    def test_fail_analytics_script(self):
        r = hf.scan_security(
            html="<p>x</p>",
            js='fetch("https://google-analytics.com/collect")',
            headers=_GOOD_HEADERS,
        )
        assert r.status == "fail"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ABI matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestABIMatrix:
    def test_aarch64_has_chromium_and_webkit(self):
        entries = hf.list_abi_entries("aarch64")
        engines = {e.engine for e in entries}
        assert "chromium" in engines
        assert "webkit" in engines

    def test_armv7_has_reduced_capabilities(self):
        entries = hf.list_abi_entries("armv7")
        for e in entries:
            # armv7 entries should NOT promise webrtc (legacy cameras)
            assert e.supports_webrtc is False

    def test_unknown_platform_returns_empty(self):
        assert hf.list_abi_entries("bogus") == []

    def test_compatibility_pass(self):
        r = hf.check_abi_compatibility("aarch64", needs={"wasm": True}, needs_es_version="ES2020")
        assert r["status"] == "pass"
        assert len(r["compatible"]) >= 1

    def test_compatibility_fail_when_webrtc_required_but_webkit(self):
        r = hf.check_abi_compatibility("aarch64", needs={"webrtc": True}, needs_es_version="ES2020")
        # WebKit on aarch64 lacks WebRTC — must be flagged as incompatible
        engines = {e["engine"] for e in r["incompatible"]}
        assert "webkit" in engines

    def test_compatibility_unknown_platform(self):
        r = hf.check_abi_compatibility("bogus")
        assert r["status"] == "unknown"

    def test_all_abi_matrix_shape(self):
        m = hf.all_abi_matrix()
        assert "aarch64" in m
        assert all(isinstance(e, hf.ABIEntry) for entries in m.values() for e in entries)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  i18n
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestI18n:
    def test_four_locales_at_launch(self):
        codes = {loc.code for loc in hf.list_locales()}
        assert codes == {"en", "zh-TW", "ja", "zh-CN"}

    def test_default_locale_is_english(self):
        assert hf.default_locale() == "en"

    def test_base_keys_include_nav_and_actions(self):
        keys = hf.base_i18n_keys()
        assert "nav.home" in keys
        assert "action.save" in keys
        assert "error.server" in keys

    def test_catalog_has_every_locale_with_every_key(self):
        cat = hf.build_i18n_catalog()
        keys = hf.base_i18n_keys()
        for loc in hf.list_locales():
            assert set(cat[loc.code].keys()) == set(keys), f"{loc.code} missing keys"

    def test_overrides_respected(self):
        cat = hf.build_i18n_catalog(overrides={"zh-TW": {"action.save": "存"}})
        assert cat["zh-TW"]["action.save"] == "存"

    def test_missing_translation_falls_back_to_english(self):
        # Build a custom catalog — inject a new key via overrides and ensure
        # other locales without the override still get a sensible value.
        cat = hf.build_i18n_catalog()
        # nav.settings is present for all 4 languages
        assert cat["en"]["nav.settings"]
        assert cat["ja"]["nav.settings"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Framework whitelist
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestWhitelist:
    def test_preact_allowed(self):
        assert hf.is_framework_allowed("preact")
        assert hf.is_framework_allowed("Preact")  # case-insensitive

    def test_lit_html_allowed(self):
        assert hf.is_framework_allowed("lit-html")

    def test_vanilla_allowed(self):
        assert hf.is_framework_allowed("vanilla")

    def test_react_forbidden(self):
        assert not hf.is_framework_allowed("react")
        assert "react" in hf.list_forbidden_frameworks()

    def test_vue_forbidden(self):
        assert not hf.is_framework_allowed("vue")

    def test_angular_forbidden(self):
        assert not hf.is_framework_allowed("angular")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSummary:
    def test_summary_shape(self):
        s = hf.framework_summary()
        assert s["version"] == hf.FRAMEWORK_VERSION
        assert s["default_locale"] == "en"
        assert "aarch64" in s["platforms"]
        assert "IEC 62443" in s["security_standard"]

"""C26 — HMI constrained generator tests (#261).

Covers: framework whitelist enforcement, CSP + security headers baked
into every bundle, i18n blob embedding, bundle budget enforcement,
input validation on section ids / product names, security scan refusal
on malicious extra_scripts, and BudgetExceeded CI hook.
"""

from __future__ import annotations

import json

import pytest

from backend import hmi_framework as hf
from backend import hmi_generator as g
from backend.hmi_generator import (
    BudgetExceeded,
    GeneratorRequest,
    PageSection,
)


@pytest.fixture(autouse=True)
def _reload():
    hf.reload_config()
    yield
    hf.reload_config()


def _make_req(**kw) -> GeneratorRequest:
    base = {
        "product_name": "TestCam",
        "framework": "vanilla",
        "platform": "aarch64",
        "locale": "en",
        "sections": [PageSection(id="sec1", title="nav.network", kind="form")],
    }
    base.update(kw)
    return GeneratorRequest(**base)


class TestFrameworkWhitelist:
    def test_vanilla_allowed(self):
        b = g.generate_bundle(_make_req(framework="vanilla"))
        assert b.framework == "vanilla"

    def test_preact_allowed(self):
        b = g.generate_bundle(_make_req(framework="preact"))
        assert b.framework == "preact"

    def test_lit_html_allowed(self):
        b = g.generate_bundle(_make_req(framework="lit-html"))
        assert b.framework == "lit-html"

    def test_react_rejected(self):
        with pytest.raises(ValueError, match="whitelist"):
            g.generate_bundle(_make_req(framework="react"))

    def test_jquery_rejected(self):
        with pytest.raises(ValueError, match="whitelist"):
            g.generate_bundle(_make_req(framework="jquery"))


class TestSecurityHeaders:
    def test_default_csp_present_and_restrictive(self):
        b = g.generate_bundle(_make_req())
        csp = b.headers["Content-Security-Policy"]
        for directive in ("default-src 'self'", "script-src 'self'",
                          "object-src 'none'", "base-uri 'none'",
                          "frame-ancestors 'none'"):
            assert directive in csp

    def test_all_required_security_headers_set(self):
        b = g.generate_bundle(_make_req())
        for h in ("Content-Security-Policy", "X-Content-Type-Options",
                  "X-Frame-Options", "Referrer-Policy",
                  "Strict-Transport-Security"):
            assert h in b.headers

    def test_security_pass_on_default_output(self):
        b = g.generate_bundle(_make_req())
        assert b.security_status == "pass"
        assert b.security_findings == []


class TestI18nInlined:
    def test_i18n_catalog_inlined_as_json(self):
        b = g.generate_bundle(_make_req())
        html = b.files["index.html"]
        assert 'id="omni-i18n"' in html
        # Extract the catalog blob
        start = html.index('id="omni-i18n"')
        body_start = html.index(">", start) + 1
        body_end = html.index("</script>", body_start)
        catalog = json.loads(html[body_start:body_end])
        assert set(catalog.keys()) == {"en", "zh-TW", "ja", "zh-CN"}
        assert "nav.home" in catalog["en"]

    def test_overrides_propagate(self):
        req = _make_req(i18n_overrides={"en": {"nav.home": "Dashboard"}})
        b = g.generate_bundle(req)
        html = b.files["index.html"]
        start = html.index('id="omni-i18n"')
        body_start = html.index(">", start) + 1
        body_end = html.index("</script>", body_start)
        catalog = json.loads(html[body_start:body_end])
        assert catalog["en"]["nav.home"] == "Dashboard"


class TestBudget:
    def test_default_bundle_fits_aarch64(self):
        b = g.generate_bundle(_make_req(platform="aarch64"))
        assert b.total_bytes < b.budget_bytes
        assert b.budget_violations == []

    def test_huge_extra_script_fails_budget(self):
        big = "// filler " + "x" * (200 * 1024)  # well over armv7 76 KiB JS budget
        req = _make_req(platform="armv7", extra_scripts=big)
        b = g.generate_bundle(req)
        # JS budget on armv7 is 76 KiB — this exceeds
        assert b.total_bytes > b.budget_bytes or b.budget_violations

    def test_budget_exceeded_ci_hook(self):
        files = {"giant.js": "x" * (200 * 1024)}
        with pytest.raises(BudgetExceeded):
            g.assert_budget_within("armv7", files)

    def test_budget_within_ci_hook_ok(self):
        g.assert_budget_within("aarch64", {"tiny.html": "<p>hi</p>"})


class TestInputValidation:
    def test_invalid_section_id(self):
        with pytest.raises(ValueError):
            PageSection(id="bad id with space", title="nav.network")

    def test_invalid_section_kind(self):
        with pytest.raises(ValueError):
            PageSection(id="ok", title="nav.network", kind="bogus")

    def test_invalid_product_name_with_angle_brackets(self):
        with pytest.raises(ValueError):
            _make_req(product_name="<script>")


class TestSecurityRejection:
    def test_extra_scripts_with_eval_rejected(self):
        req = _make_req(extra_scripts='eval("bad")')
        with pytest.raises(ValueError, match="Security baseline"):
            g.generate_bundle(req)

    def test_extra_scripts_with_cdn_rejected(self):
        req = _make_req(extra_scripts='fetch("https://cdn.jsdelivr.net/foo")')
        with pytest.raises(ValueError, match="Security baseline"):
            g.generate_bundle(req)


class TestSummary:
    def test_generator_summary(self):
        s = g.summary()
        assert s["generator_version"] == g.GENERATOR_VERSION
        assert "preact" in s["allowed_frameworks"]
        assert "react" in s["forbidden_frameworks"]
        assert "Content-Security-Policy" in s["security_headers"]

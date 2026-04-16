"""C26 — HMI shared components library tests (#261).

Covers: registry completeness, per-component HTML + JS + HAL endpoints,
security scan compliance on assembled bundles, skills coverage
(D2/D8/D9/D17/D24/D25).
"""

from __future__ import annotations

import pytest

from backend import hmi_components as hc
from backend import hmi_framework as hf


@pytest.fixture(autouse=True)
def _reload():
    hf.reload_config()
    yield
    hf.reload_config()


class TestRegistry:
    def test_three_base_components(self):
        ids = {c.id for c in hc.list_components()}
        assert ids == {"network", "ota", "logs"}

    def test_get_by_id(self):
        assert hc.get_component("network").id == "network"

    def test_unknown_component_raises(self):
        with pytest.raises(KeyError):
            hc.get_component("bogus")

    def test_all_components_reference_target_skills(self):
        targets = {"D2", "D8", "D9", "D17", "D24", "D25"}
        for c in hc.list_components():
            assert targets.issubset(set(c.used_by_skills)), \
                f"{c.id} misses skills: {targets - set(c.used_by_skills)}"


class TestNetworkComponent:
    def test_html_has_form(self):
        html = hc.get_component("network").render_html()
        assert 'data-bind-submit="net_apply"' in html
        assert "pattern" in html  # IP pattern validation

    def test_js_has_listener(self):
        js = hc.get_component("network").render_js()
        assert "addEventListener" in js
        assert "OmniHMI.clients.net_status" in js

    def test_endpoints_shape(self):
        eps = hc.get_component("network").hal_endpoints()
        ids = {e.id for e in eps}
        assert ids == {"net_status", "net_apply"}


class TestOTAComponent:
    def test_html_has_upload_form(self):
        html = hc.get_component("ota").render_html()
        assert 'enctype="multipart/form-data"' in html
        assert 'data-action="ota_apply"' in html
        assert 'data-action="ota_rollback"' in html

    def test_endpoints_include_rollback(self):
        ids = {e.id for e in hc.get_component("ota").hal_endpoints()}
        assert ids == {"ota_status", "ota_upload", "ota_apply", "ota_rollback"}


class TestLogsComponent:
    def test_html_has_filter(self):
        html = hc.get_component("logs").render_html()
        assert 'name="query"' in html
        assert 'name="level"' in html

    def test_endpoints_include_export(self):
        ids = {e.id for e in hc.get_component("logs").hal_endpoints()}
        assert ids == {"logs_tail", "logs_export"}


class TestAssembly:
    def test_assemble_all(self):
        bundle = hc.assemble_components(["network", "ota", "logs"])
        assert bundle["components"] == ["network", "ota", "logs"]
        assert len(bundle["endpoints"]) == 2 + 4 + 2
        assert "addEventListener" in bundle["js"]

    def test_assembled_html_passes_security_scan(self):
        bundle = hc.assemble_components(["network", "ota", "logs"])
        report = hf.scan_security(
            html=bundle["html"],
            js=bundle["js"],
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Strict-Transport-Security": "max-age=31536000",
            },
        )
        assert report.status == "pass", [f.detail for f in report.findings]

    def test_assembled_partial_selection(self):
        bundle = hc.assemble_components(["logs"])
        assert len(bundle["endpoints"]) == 2
        assert "c-logs" in bundle["html"]


class TestSummary:
    def test_summary_structure(self):
        s = hc.summary()
        assert s["library_version"] == hc.COMPONENT_LIBRARY_VERSION
        assert len(s["components"]) == 3

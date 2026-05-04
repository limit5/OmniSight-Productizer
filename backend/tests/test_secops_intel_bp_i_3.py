"""BP.I.3 -- SecOps intel pre-install / pre-blueprint hook smoke tests.

This is intentionally not the full BP.I.5 ``test_secops_intel.py``
matrix. It only guards the two passive hook entry points introduced in
BP.I.3.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend import secops_intel_hooks as hooks


NOW = datetime(2026, 5, 4, tzinfo=timezone.utc)


def _client_factory(handler):  # noqa: ANN001
    def factory(**kwargs):  # noqa: ANN003
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "services.nvd.nist.gov" in url:
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2026-4242",
                            "published": "2026-05-03T00:00:00.000",
                            "lastModified": "2026-05-03T01:00:00.000",
                            "descriptions": [
                                {"lang": "en", "value": "Install target RCE"}
                            ],
                            "metrics": {
                                "cvssMetricV31": [
                                    {"cvssData": {"baseSeverity": "CRITICAL"}}
                                ]
                            },
                        }
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "vulnerabilities": [
                {
                    "cveID": "CVE-2026-4242",
                    "vendorProject": "Acme",
                    "product": "Camera",
                    "vulnerabilityName": "Acme active exploit",
                    "shortDescription": "Known exploited package.",
                }
            ]
        },
    )


def test_integration_engineer_pre_install_hook_returns_passive_brief():
    result = hooks.integration_engineer_pre_install_hook(
        product_name="Acme Camera",
        install_targets=["vite-plugin-camera"],
        client_factory=_client_factory(_handler),
        now=NOW,
    )

    assert result["hook"] == "integration_engineer_pre_install"
    assert result["guild"] == "intel"
    assert result["status"] == "findings"
    assert result["blocking"] is False
    assert result["query"] == "Acme Camera vite-plugin-camera"
    assert [report["kind"] for report in result["reports"]] == [
        "cve",
        "zero_day",
        "best_practice",
    ]
    assert "CVE-2026-4242" in result["brief"]
    assert "BP.I.3 does not block automatically" in result["recommended_action"]


def test_architect_pre_blueprint_hook_uses_blueprint_keywords():
    result = hooks.architect_pre_blueprint_hook(
        product_name="Acme Camera",
        blueprint_keywords=["qt", "linux"],
        client_factory=_client_factory(_handler),
        now=NOW,
    )

    assert result["hook"] == "architect_pre_blueprint"
    assert result["status"] == "findings"
    assert result["query"] == "Acme Camera qt linux"
    assert "Best-practice topic: secure architecture Acme Camera qt linux" in result["brief"]
    assert result["reports"][2]["source"] == "curated"


def test_pre_install_hook_returns_clean_status_when_feeds_are_empty():
    def empty_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"vulnerabilities": []})

    result = hooks.integration_engineer_pre_install_hook(
        product_name="Empty Product",
        client_factory=_client_factory(empty_handler),
        now=NOW,
    )

    assert result["status"] == "clean"
    assert result["blocking"] is False
    assert "No items returned." in result["brief"]


"""BP.I.1 -- SecOps threat-intel helper smoke tests.

This is intentionally not the full BP.I.5 ``test_secops_intel.py``
matrix. It only guards the three helper skills introduced in BP.I.1.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from backend import secops_intel as intel


NOW = datetime(2026, 5, 4, tzinfo=timezone.utc)


def _client_factory(handler):  # noqa: ANN001
    def factory(**kwargs):  # noqa: ANN003
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def test_search_latest_cve_normalises_nvd_item_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2026-1234",
                            "published": "2026-05-01T00:00:00.000",
                            "lastModified": "2026-05-02T00:00:00.000",
                            "descriptions": [
                                {"lang": "en", "value": "Camera RCE"}
                            ],
                            "metrics": {
                                "cvssMetricV31": [
                                    {"cvssData": {"baseSeverity": "HIGH"}}
                                ]
                            },
                            "references": {
                                "referenceData": [
                                    {"url": "https://example.test/advisory"}
                                ]
                            },
                        }
                    }
                ]
            },
        )

    report = intel.search_latest_cve(
        "camera",
        severity="high",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "cve"
    assert report["source"] == "nvd"
    assert "keywordSearch=camera" in seen["url"]
    assert "cvssV3Severity=HIGH" in seen["url"]
    assert report["items"][0]["id"] == "CVE-2026-1234"
    assert report["items"][0]["severity"] == "HIGH"


def test_search_latest_cve_returns_error_envelope_on_http_failure():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    report = intel.search_latest_cve(
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "cve"
    assert report["items"] == []
    assert "HTTPStatusError" in report["error"]


def test_query_zero_day_feeds_filters_cisa_kev_terms():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    {
                        "cveID": "CVE-2026-0001",
                        "vendorProject": "Acme",
                        "product": "Camera",
                        "vulnerabilityName": "Acme camera exploit",
                        "dateAdded": "2026-05-03",
                        "shortDescription": "Exploited in the wild.",
                        "dueDate": "2026-05-10",
                    },
                    {
                        "cveID": "CVE-2026-0002",
                        "vendorProject": "Other",
                        "product": "Router",
                        "vulnerabilityName": "Router exploit",
                    },
                ]
            },
        )

    report = intel.query_zero_day_feeds(
        "acme camera",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "zero_day"
    assert [item["id"] for item in report["items"]] == ["CVE-2026-0001"]
    assert report["items"][0]["severity"] == "KNOWN_EXPLOITED"


def test_query_zero_day_feeds_returns_error_envelope_on_bad_json():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    report = intel.query_zero_day_feeds(
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "zero_day"
    assert report["items"] == []
    assert report["error"]


def test_fetch_latest_best_practices_filters_topic():
    report = intel.fetch_latest_best_practices("secure sdlc", now=NOW)

    assert report["kind"] == "best_practice"
    assert report["source"] == "curated"
    assert [item["id"] for item in report["items"]] == ["nist-ssdf-sp-800-218"]


def test_fetch_latest_best_practices_has_stable_limit():
    report = intel.fetch_latest_best_practices(limit=1, now=NOW)

    assert report["total_items"] == 1
    assert report["items"][0]["source"] in {"cisa", "nist", "owasp"}

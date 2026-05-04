"""BP.I.5 -- SecOps threat-intel helper contract matrix."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from backend import secops_intel as intel


NOW = datetime(2026, 5, 4, 12, 30, tzinfo=timezone.utc)


def _client_factory(handler):  # noqa: ANN001
    def factory(**kwargs):  # noqa: ANN003
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def _nvd_entry(
    cve_id: str,
    *,
    severity: str = "HIGH",
    summary: str = "Camera RCE",
) -> dict:
    return {
        "cve": {
            "id": cve_id,
            "published": "2026-05-01T00:00:00.000",
            "lastModified": "2026-05-02T00:00:00.000",
            "descriptions": [{"lang": "en", "value": summary}],
            "metrics": {
                "cvssMetricV31": [
                    {"cvssData": {"baseSeverity": severity}}
                ]
            },
            "references": {
                "referenceData": [
                    {"url": "https://example.test/advisory"},
                    {"url": ""},
                ]
            },
        }
    }


def _kev_entry(
    cve_id: str,
    *,
    vendor: str = "Acme",
    product: str = "Camera",
) -> dict:
    return {
        "cveID": cve_id,
        "vendorProject": vendor,
        "product": product,
        "vulnerabilityName": f"{vendor} {product} exploit",
        "dateAdded": "2026-05-03",
        "shortDescription": f"{vendor} {product} exploited in the wild.",
        "dueDate": "2026-05-10",
        "notes": "https://example.test/kev",
    }


def test_intel_item_to_dict_includes_default_lists():
    item = intel.IntelItem(id="cisa", title="Patch", source="cisa")

    assert item.to_dict() == {
        "id": "cisa",
        "title": "Patch",
        "source": "cisa",
        "url": "",
        "severity": "UNKNOWN",
        "published_at": "",
        "updated_at": "",
        "summary": "",
        "affected": [],
        "references": [],
        "tags": [],
    }


def test_intel_report_to_dict_counts_items():
    report = intel.IntelReport(
        kind="cve",
        query="camera",
        source="nvd",
        fetched_at="2026-05-04T12:30:00+00:00",
        items=[intel.IntelItem(id="CVE-1", title="CVE-1", source="nvd")],
    )

    assert report.total_items == 1
    assert report.to_dict()["total_items"] == 1
    assert report.to_dict()["items"][0]["id"] == "CVE-1"


def test_normalise_severity_maps_moderate_to_medium():
    assert intel._normalise_severity("moderate") == "MEDIUM"


def test_normalise_severity_rejects_unknown_values():
    assert intel._normalise_severity(None) == "UNKNOWN"
    assert intel._normalise_severity("urgent") == "UNKNOWN"


def test_nvd_timestamp_uses_utc_api_format():
    value = intel._nvd_timestamp(NOW)

    assert value == "2026-05-04T12:30:00.000Z"


def test_http_get_json_passes_params_and_timeout():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    payload, error = intel._http_get_json(
        "https://example.test/feed",
        params={"q": "camera"},
        timeout_s=3.5,
        client_factory=_client_factory(handler),
    )

    assert payload == {"ok": True}
    assert error == ""
    assert "q=camera" in seen["url"]


def test_http_get_json_returns_error_for_http_status():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    payload, error = intel._http_get_json(
        "https://example.test/feed",
        client_factory=_client_factory(handler),
    )

    assert payload is None
    assert "HTTPStatusError" in error


def test_http_get_json_returns_error_for_invalid_json():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    payload, error = intel._http_get_json(
        "https://example.test/feed",
        client_factory=_client_factory(handler),
    )

    assert payload is None
    assert error


def test_pick_english_description_prefers_en():
    value = intel._pick_english_description(
        [
            {"lang": "fr", "value": "francais"},
            {"lang": "en", "value": "english"},
        ]
    )

    assert value == "english"


def test_pick_english_description_falls_back_to_first_value():
    value = intel._pick_english_description(
        [
            {"lang": "fr", "value": "francais"},
            {"lang": "de", "value": "deutsch"},
        ]
    )

    assert value == "francais"


def test_cvss_severity_prefers_v31_then_v30_then_v2():
    cve = {
        "metrics": {
            "cvssMetricV31": [{"baseSeverity": "CRITICAL"}],
            "cvssMetricV30": [{"baseSeverity": "LOW"}],
            "cvssMetricV2": [{"baseSeverity": "MEDIUM"}],
        }
    }

    assert intel._cvss_severity(cve) == "CRITICAL"


def test_cvss_severity_reads_cvss_data_fallback():
    cve = {"metrics": {"cvssMetricV30": [{"cvssData": {"baseSeverity": "LOW"}}]}}

    assert intel._cvss_severity(cve) == "LOW"


def test_cvss_severity_returns_unknown_when_metrics_missing():
    assert intel._cvss_severity({}) == "UNKNOWN"


def test_nvd_item_normalises_full_shape_and_caps_lists():
    entry = _nvd_entry("CVE-2026-1234")
    entry["cve"]["references"]["referenceData"] = [
        {"url": f"https://example.test/{idx}"} for idx in range(12)
    ]
    entry["cve"]["configurations"] = [
        {
            "nodes": [
                {
                    "cpeMatch": [
                        {"criteria": f"cpe:2.3:a:acme:camera:{idx}"}
                        for idx in range(12)
                    ]
                }
            ]
        }
    ]

    item = intel._nvd_item(entry).to_dict()

    assert item["id"] == "CVE-2026-1234"
    assert item["url"] == "https://nvd.nist.gov/vuln/detail/CVE-2026-1234"
    assert item["severity"] == "HIGH"
    assert item["summary"] == "Camera RCE"
    assert len(item["references"]) == 10
    assert len(item["affected"]) == 10
    assert item["tags"] == ["cve"]


def test_search_latest_cve_builds_bounded_nvd_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"vulnerabilities": []})

    report = intel.search_latest_cve(
        "  camera  ",
        days=0,
        limit=250,
        severity="moderate",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["fetched_at"] == "2026-05-04T12:30:00+00:00"
    assert seen["query"]["keywordSearch"] == ["camera"]
    assert seen["query"]["cvssV3Severity"] == ["MEDIUM"]
    assert seen["query"]["resultsPerPage"] == ["100"]
    assert seen["query"]["pubStartDate"] == ["2026-05-03T12:30:00.000Z"]


def test_search_latest_cve_omits_empty_query_and_unknown_severity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = parse_qs(urlparse(str(request.url)).query)
        return httpx.Response(200, json={"vulnerabilities": []})

    intel.search_latest_cve(
        "   ",
        severity="urgent",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert "keywordSearch" not in seen["query"]
    assert "cvssV3Severity" not in seen["query"]


def test_search_latest_cve_limits_items_to_request_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    _nvd_entry("CVE-2026-0001"),
                    _nvd_entry("CVE-2026-0002"),
                ]
            },
        )

    report = intel.search_latest_cve(
        limit=1,
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["total_items"] == 1
    assert [item["id"] for item in report["items"]] == ["CVE-2026-0001"]


def test_search_latest_cve_returns_error_envelope_on_http_failure():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    report = intel.search_latest_cve(
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "cve"
    assert report["source"] == "nvd"
    assert report["items"] == []
    assert "HTTPStatusError" in report["error"]


def test_search_latest_cve_maps_nvd_items():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"vulnerabilities": [_nvd_entry("CVE-2026-4242")]},
        )

    report = intel.search_latest_cve(
        "camera",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "cve"
    assert report["query"] == "camera"
    assert report["items"][0]["id"] == "CVE-2026-4242"
    assert report["items"][0]["source"] == "nvd"


def test_kev_item_normalises_cisa_shape():
    item = intel._kev_item(_kev_entry("CVE-2026-5678")).to_dict()

    assert item["id"] == "CVE-2026-5678"
    assert item["source"] == "cisa-kev"
    assert item["severity"] == "KNOWN_EXPLOITED"
    assert item["affected"] == ["Acme", "Camera"]
    assert item["references"] == ["https://example.test/kev"]
    assert item["tags"] == ["known-exploited", "zero-day-watch"]


def test_matches_terms_requires_all_terms():
    item = intel.IntelItem(
        id="CVE-2026-0001",
        title="Acme camera exploit",
        source="cisa-kev",
        summary="Known exploited.",
        affected=["Acme", "Camera"],
    )

    assert intel._matches_terms(item, ["acme", "camera"]) is True
    assert intel._matches_terms(item, ["acme", "router"]) is False


def test_matches_terms_empty_terms_match_everything():
    item = intel.IntelItem(id="CVE-2026-0001", title="Any", source="nvd")

    assert intel._matches_terms(item, []) is True


def test_query_zero_day_feeds_filters_by_product_terms():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    _kev_entry("CVE-2026-0001"),
                    _kev_entry("CVE-2026-0002", vendor="Other", product="Router"),
                ]
            },
        )

    report = intel.query_zero_day_feeds(
        "acme camera",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "zero_day"
    assert report["source"] == "cisa-kev"
    assert [item["id"] for item in report["items"]] == ["CVE-2026-0001"]


def test_query_zero_day_feeds_empty_product_returns_capped_feed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vulnerabilities": [
                    _kev_entry("CVE-2026-0001"),
                    _kev_entry("CVE-2026-0002"),
                ]
            },
        )

    report = intel.query_zero_day_feeds(
        limit=1,
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["total_items"] == 1
    assert report["items"][0]["id"] == "CVE-2026-0001"


def test_query_zero_day_feeds_returns_error_envelope_on_bad_json():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    report = intel.query_zero_day_feeds(
        "camera",
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["kind"] == "zero_day"
    assert report["query"] == "camera"
    assert report["items"] == []
    assert report["error"]


def test_query_zero_day_feeds_bounds_limit_to_at_least_one():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"vulnerabilities": [_kev_entry("CVE-2026-0001")]},
        )

    report = intel.query_zero_day_feeds(
        limit=0,
        client_factory=_client_factory(handler),
        now=NOW,
    )

    assert report["total_items"] == 1


def test_query_zero_day_feeds_handles_missing_optional_kev_fields():
    item = intel._kev_item({"cveID": "CVE-2026-9999"}).to_dict()

    assert item["title"] == "CVE-2026-9999"
    assert item["affected"] == []
    assert item["references"] == []


def test_fetch_latest_best_practices_filters_topic_terms():
    report = intel.fetch_latest_best_practices("secure sdlc", now=NOW)

    assert report["kind"] == "best_practice"
    assert report["source"] == "curated"
    assert [item["id"] for item in report["items"]] == ["nist-ssdf-sp-800-218"]


def test_fetch_latest_best_practices_matches_summary_text():
    report = intel.fetch_latest_best_practices("memory-safe", now=NOW)

    assert [item["id"] for item in report["items"]] == ["cisa-secure-by-design"]


def test_fetch_latest_best_practices_returns_empty_for_unknown_topic():
    report = intel.fetch_latest_best_practices("mainframe tokenization", now=NOW)

    assert report["total_items"] == 0
    assert report["items"] == []


def test_fetch_latest_best_practices_bounds_limit_to_at_least_one():
    report = intel.fetch_latest_best_practices(limit=0, now=NOW)

    assert report["total_items"] == 1


def test_fetch_latest_best_practices_caps_large_limit_to_available_items():
    report = intel.fetch_latest_best_practices(limit=500, now=NOW)

    assert report["total_items"] == 5
    assert {item["source"] for item in report["items"]} == {"cisa", "nist", "owasp"}


def test_public_exports_include_three_skill_helpers():
    assert {
        "search_latest_cve",
        "query_zero_day_feeds",
        "fetch_latest_best_practices",
    }.issubset(set(intel.__all__))

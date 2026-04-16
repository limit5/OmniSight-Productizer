"""N6 — unit tests for `scripts/cve_triage.py`.

The CVE triage script consumes osv-scanner JSON output and renders a
markdown issue body for severe findings (HIGH/CRITICAL). These tests
exercise the parse/classify/render layers in isolation so the daily
cron workflow is guarded by coverage without depending on a live
network or the actual scanner binary.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cve_triage.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cve_triage  # noqa: E402


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

class TestClassifySeverity:
    """Map OSV severity shapes → (label, score)."""

    def test_ghsa_string_severity_critical(self) -> None:
        label, score = cve_triage.classify_severity(
            {"database_specific": {"severity": "Critical"}}
        )
        assert label == "CRITICAL"
        assert score is None

    def test_ghsa_moderate_maps_to_medium(self) -> None:
        # GitHub advisory feed uses "MODERATE"; we normalize.
        label, _ = cve_triage.classify_severity(
            {"database_specific": {"severity": "MODERATE"}}
        )
        assert label == "MEDIUM"

    def test_cvss_numeric_9_5_is_critical(self) -> None:
        label, score = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3", "score": "9.5"}]}
        )
        assert label == "CRITICAL"
        assert score == pytest.approx(9.5)

    def test_cvss_numeric_7_5_is_high(self) -> None:
        label, score = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3", "score": "7.5"}]}
        )
        assert label == "HIGH"
        assert score == pytest.approx(7.5)

    def test_cvss_numeric_5_5_is_medium(self) -> None:
        label, _ = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3", "score": "5.5"}]}
        )
        assert label == "MEDIUM"

    def test_cvss_numeric_1_0_is_low(self) -> None:
        label, _ = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3", "score": "1.0"}]}
        )
        assert label == "LOW"

    def test_empty_severity_is_unknown(self) -> None:
        label, score = cve_triage.classify_severity({})
        assert label == "UNKNOWN"
        assert score is None

    def test_unparseable_score_falls_back_to_unknown(self) -> None:
        label, score = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3",
                           "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N"}]}
        )
        assert label == "UNKNOWN"
        assert score is None

    def test_cvss_with_leading_numeric_parses(self) -> None:
        # Some feeds embed "7.5 (CVSS:..)" — we accept the leading num.
        label, score = cve_triage.classify_severity(
            {"severity": [{"type": "CVSS_V3",
                           "score": "7.5 (CVSS:3.1/AV:N)"}]}
        )
        assert label == "HIGH"
        assert score == pytest.approx(7.5)

    def test_ghsa_wins_over_cvss_if_present(self) -> None:
        # Fast path — if GHSA marked a CVE as CRITICAL we trust that
        # even when the CVSS vector is unparseable.
        label, _ = cve_triage.classify_severity(
            {"database_specific": {"severity": "HIGH"},
             "severity": [{"type": "CVSS_V3", "score": "garbage"}]}
        )
        assert label == "HIGH"


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

class TestParseOsvReport:
    """Flatten OSV-Scanner JSON output into Finding objects."""

    def test_empty_report_yields_empty_list(self) -> None:
        assert cve_triage.parse_osv_report({}) == []
        assert cve_triage.parse_osv_report({"results": []}) == []

    def test_single_high_finding(self) -> None:
        raw = {
            "results": [{
                "source": {"path": "backend/requirements.txt"},
                "packages": [{
                    "package": {
                        "name": "requests", "ecosystem": "PyPI",
                        "version": "2.25.0",
                    },
                    "vulnerabilities": [{
                        "id": "GHSA-xxxx",
                        "aliases": ["CVE-2024-1234"],
                        "summary": "Requests mis-handles redirects",
                        "database_specific": {"severity": "HIGH"},
                        "affected": [{
                            "ranges": [{"events": [{"fixed": "2.32.0"}]}]
                        }],
                    }],
                }],
            }]
        }
        findings = cve_triage.parse_osv_report(raw)
        assert len(findings) == 1
        f = findings[0]
        assert f.package == "requests"
        assert f.ecosystem == "PyPI"
        assert f.version == "2.25.0"
        assert f.severity == "HIGH"
        assert f.primary_id == "CVE-2024-1234"
        assert "2.32.0" in f.fixed_versions
        assert f.source_path == "backend/requirements.txt"

    def test_missing_package_block_is_skipped(self) -> None:
        raw = {"results": [{"packages": [{"vulnerabilities": [{"id": "x"}]}]}]}
        findings = cve_triage.parse_osv_report(raw)
        # The package block is malformed (no `package` key). The
        # parser should still produce one finding with blank name
        # so operators see the raw ID rather than silently dropping.
        assert len(findings) == 1
        assert findings[0].package == ""

    def test_malformed_ranges_do_not_crash(self) -> None:
        raw = {
            "results": [{
                "source": {"path": "p"},
                "packages": [{
                    "package": {"name": "x", "ecosystem": "PyPI", "version": "1"},
                    "vulnerabilities": [{
                        "id": "X-1",
                        "affected": [{"ranges": "not a list"}],
                    }],
                }],
            }]
        }
        findings = cve_triage.parse_osv_report(raw)
        assert len(findings) == 1
        assert findings[0].fixed_versions == []

    def test_non_dict_entries_are_skipped(self) -> None:
        raw = {
            "results": ["garbage", None,
                        {"packages": [None, {"package": None}]}]
        }
        # Shouldn't raise; just returns empty.
        assert cve_triage.parse_osv_report(raw) == []


# ---------------------------------------------------------------------------
# Severity filter
# ---------------------------------------------------------------------------

class TestFilterSevere:
    def _finding(self, severity: str) -> cve_triage.Finding:
        return cve_triage.Finding(
            package="p", ecosystem="PyPI", version="1",
            vulnerability_id="X", severity=severity,
        )

    def test_filters_below_threshold(self) -> None:
        findings = [
            self._finding("LOW"),
            self._finding("MEDIUM"),
            self._finding("HIGH"),
            self._finding("CRITICAL"),
        ]
        severe = cve_triage.filter_severe(findings, "HIGH")
        assert [f.severity for f in severe] == ["HIGH", "CRITICAL"]

    def test_threshold_critical_only(self) -> None:
        findings = [self._finding("HIGH"), self._finding("CRITICAL")]
        severe = cve_triage.filter_severe(findings, "CRITICAL")
        assert [f.severity for f in severe] == ["CRITICAL"]

    def test_unknown_threshold_defaults_to_high(self) -> None:
        findings = [self._finding("LOW"), self._finding("HIGH")]
        severe = cve_triage.filter_severe(findings, "BOGUS")
        assert [f.severity for f in severe] == ["HIGH"]


# ---------------------------------------------------------------------------
# Issue body rendering
# ---------------------------------------------------------------------------

class TestRenderIssueBody:
    def _finding(self, sev: str = "HIGH", score: float = 7.5) -> cve_triage.Finding:
        return cve_triage.Finding(
            package="requests", ecosystem="PyPI", version="2.25.0",
            vulnerability_id="GHSA-xxxx", aliases=["CVE-2024-1234"],
            severity=sev, cvss_score=score,
            summary="Requests mis-handles redirects",
            fixed_versions=["2.32.0"],
            source_path="backend/requirements.txt",
        )

    def test_header_has_run_url(self) -> None:
        body = cve_triage.render_issue_body(
            [self._finding()], [self._finding()],
            run_url="https://example/run/42",
        )
        assert "https://example/run/42" in body

    def test_summary_table_present(self) -> None:
        body = cve_triage.render_issue_body(
            [self._finding()], [self._finding()],
            run_url="",
        )
        assert "| Severity | CVSS" in body
        assert "`requests`" in body
        assert "CVE-2024-1234" in body

    def test_per_cve_detail_present(self) -> None:
        body = cve_triage.render_issue_body(
            [self._finding()], [self._finding()],
            run_url="",
        )
        assert "## Per-CVE detail" in body
        assert "Requests mis-handles redirects" in body
        assert "Fixed in" in body

    def test_no_severe_message_when_empty(self) -> None:
        body = cve_triage.render_issue_body(
            [], [self._finding("LOW", 2.0)], run_url="",
        )
        assert "No severe findings" in body

    def test_renovate_reference_in_body(self) -> None:
        # Operators following the issue need to know the fix PR
        # flows through Renovate's vulnerability fast-path.
        body = cve_triage.render_issue_body(
            [self._finding()], [self._finding()], run_url="",
        )
        assert "Renovate" in body
        assert "vulnerabilityAlerts" in body

    def test_body_respects_60kb_cap(self) -> None:
        # Generate a huge count of fake findings to overflow.
        many = [
            cve_triage.Finding(
                package=f"p{i}", ecosystem="PyPI", version="1",
                vulnerability_id=f"X-{i}", severity="HIGH",
                summary="A" * 500,
            )
            for i in range(500)
        ]
        body = cve_triage.render_issue_body(many, many, run_url="")
        # Either fits or gets truncated — must never exceed 60_000 bytes.
        assert len(body.encode("utf-8")) <= cve_triage.ISSUE_BODY_MAX


# ---------------------------------------------------------------------------
# CLI smoke test (subprocess round-trip)
# ---------------------------------------------------------------------------

class TestCli:
    def test_empty_scan_no_issue(self, tmp_path: Path) -> None:
        scan = tmp_path / "scan.json"
        scan.write_text(json.dumps({"results": []}), encoding="utf-8")
        out = tmp_path / "body.md"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--input", str(scan),
             "--out", str(out),
             "--severity-threshold", "HIGH",
             "--run-url", "https://example"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "0 severe findings" in body

    def test_severe_scan_emits_github_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan = tmp_path / "scan.json"
        scan.write_text(json.dumps({
            "results": [{
                "source": {"path": "p"},
                "packages": [{
                    "package": {"name": "x", "ecosystem": "PyPI", "version": "1"},
                    "vulnerabilities": [{
                        "id": "CVE-2025-1",
                        "database_specific": {"severity": "CRITICAL"},
                    }],
                }],
            }]
        }), encoding="utf-8")
        out = tmp_path / "body.md"
        gh_out = tmp_path / "gh-output"
        gh_out.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))

        code = cve_triage.main(
            ["--input", str(scan), "--out", str(out),
             "--severity-threshold", "HIGH", "--run-url", ""]
        )
        assert code == 0
        assert "has_severe=true" in gh_out.read_text(encoding="utf-8")

    def test_missing_input_still_emits_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "body.md"
        gh_out = tmp_path / "gh-output"
        gh_out.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        code = cve_triage.main(
            ["--input", str(tmp_path / "nope.json"),
             "--out", str(out),
             "--severity-threshold", "HIGH",
             "--run-url", "https://example/run/1"]
        )
        assert code == 0
        # Missing input is treated as an alert condition.
        assert "has_severe=true" in gh_out.read_text(encoding="utf-8")
        assert "did not produce output" in out.read_text(encoding="utf-8")

    def test_malformed_json_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan = tmp_path / "scan.json"
        scan.write_text("{ not: json", encoding="utf-8")
        out = tmp_path / "body.md"
        gh_out = tmp_path / "gh-output"
        gh_out.write_text("", encoding="utf-8")
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        code = cve_triage.main(
            ["--input", str(scan), "--out", str(out),
             "--severity-threshold", "HIGH", "--run-url", ""]
        )
        assert code == 0
        assert "has_severe=true" in gh_out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stdlib-only invariant
# ---------------------------------------------------------------------------

class TestStdlibOnly:
    def test_no_third_party_imports(self) -> None:
        src = SCRIPT.read_text(encoding="utf-8")
        forbidden = (
            "import requests",
            "import httpx",
            "import yaml",
            "from pydantic",
        )
        for needle in forbidden:
            assert needle not in src, (
                f"scripts/cve_triage.py must stay stdlib-only, found {needle!r}"
            )

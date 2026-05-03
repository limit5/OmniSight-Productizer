"""SC.1.1 — Unit tests for SAST scanner adapters.

All external tools (``codeql`` / ``semgrep`` / ``snyk``) are
monkey-patched so the adapter contract stays offline and deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from backend.security_scanning import scan_sast
from backend.security_scanning.sast import _normalise_severity


_SARIF_PAYLOAD = {
    "runs": [
        {
            "tool": {
                "driver": {
                    "rules": [
                        {
                            "id": "py/sql-injection",
                            "properties": {
                                "security-severity": "8.8",
                                "tags": ["external/cwe/cwe-089", "external/owasp/a03"],
                            },
                        }
                    ]
                }
            },
            "results": [
                {
                    "ruleId": "py/sql-injection",
                    "message": {"text": "Query built from user input"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "app.py"},
                                "region": {"startLine": 12, "startColumn": 7},
                            }
                        }
                    ],
                }
            ],
        }
    ]
}


_SEMGREP_PAYLOAD = {
    "results": [
        {
            "check_id": "python.flask.security.injection",
            "path": "server.py",
            "start": {"line": 21, "col": 5},
            "extra": {
                "message": "Avoid shell=True",
                "severity": "ERROR",
                "metadata": {
                    "cwe": ["CWE-078"],
                    "owasp": ["A03:2021 - Injection"],
                },
            },
        }
    ]
}


_SNYK_PAYLOAD = {
    "runs": [
        {
            "tool": {"driver": {"rules": [{"id": "js/xss", "properties": {}}]}},
            "results": [
                {
                    "ruleId": "js/xss",
                    "level": "warning",
                    "message": {"text": "Unsanitized HTML"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "pages/index.tsx"},
                                "region": {"startLine": 44, "startColumn": 13},
                            }
                        }
                    ],
                }
            ],
        }
    ]
}


class TestSASTSeverity:
    def test_numeric_cvss_like_score(self):
        assert _normalise_severity("9.1") == "CRITICAL"
        assert _normalise_severity("7.0") == "HIGH"
        assert _normalise_severity("4.0") == "MEDIUM"

    def test_vendor_labels(self):
        assert _normalise_severity("ERROR") == "HIGH"
        assert _normalise_severity("WARNING") == "MEDIUM"
        assert _normalise_severity("note") == "LOW"


class TestSASTCodeQL:
    def test_parses_codeql_sarif(self, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("fastapi\n")
        with mock.patch(
            "backend.security_scanning.sast.shutil.which",
            lambda x: "/fake/codeql" if x == "codeql" else None,
        ), mock.patch(
            "backend.security_scanning.sast._run",
            side_effect=[
                (0, "", ""),
                (1, json.dumps(_SARIF_PAYLOAD), ""),
            ],
        ):
            report = scan_sast(tmp_path, scanner="codeql")

        assert report.source == "codeql"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "py/sql-injection"
        assert finding.path == "app.py"
        assert finding.line == 12
        assert finding.severity == "HIGH"
        assert len(report.blocking_findings) == 1
        assert not report.passed


class TestSASTSemgrep:
    def test_parses_semgrep_json(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sast.shutil.which",
            lambda x: "/fake/semgrep" if x == "semgrep" else None,
        ), mock.patch(
            "backend.security_scanning.sast._run",
            return_value=(1, json.dumps(_SEMGREP_PAYLOAD), ""),
        ):
            report = scan_sast(tmp_path, scanner="semgrep")

        assert report.source == "semgrep"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "python.flask.security.injection"
        assert finding.severity == "HIGH"
        assert finding.cwe == ["CWE-078"]
        assert report.severity_counts == {"HIGH": 1}

    def test_threshold_can_be_loosened(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sast.shutil.which",
            lambda x: "/fake/semgrep" if x == "semgrep" else None,
        ), mock.patch(
            "backend.security_scanning.sast._run",
            return_value=(1, json.dumps(_SEMGREP_PAYLOAD), ""),
        ):
            report = scan_sast(tmp_path, scanner="semgrep", fail_on={"CRITICAL"})

        assert report.passed


class TestSASTSnykCode:
    def test_parses_snyk_sarif_shape(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sast.shutil.which",
            lambda x: "/fake/snyk" if x == "snyk" else None,
        ), mock.patch(
            "backend.security_scanning.sast._run",
            return_value=(1, json.dumps(_SNYK_PAYLOAD), ""),
        ):
            report = scan_sast(tmp_path, scanner="snyk-code")

        assert report.source == "snyk-code"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "js/xss"
        assert finding.severity == "MEDIUM"
        assert finding.path == "pages/index.tsx"
        assert report.passed


class TestSASTNoScanner:
    def test_mock_when_nothing_on_path(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sast.shutil.which", return_value=None
        ):
            report = scan_sast(tmp_path)
        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed

    def test_invalid_scanner_name(self, tmp_path: Path):
        report = scan_sast(tmp_path, scanner="bandit")
        assert report.error
        assert not report.passed

    def test_invalid_app_path(self, tmp_path: Path):
        report = scan_sast(tmp_path / "missing")
        assert report.error

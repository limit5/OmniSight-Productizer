"""SC.5.1 — Unit tests for secret scanner adapters.

All external tools (``gitleaks`` / ``trufflehog``) are monkey-patched
so the adapter contract stays offline and deterministic, mirroring the
SC.1 SAST, SC.3 SCA, and SC.4 container test shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from backend.security_scanning import (
    SecretFinding,
    SecretScanReport,
    scan_secrets,
)
from backend.security_scanning.secrets import _main, _normalise_severity


_GITLEAKS_PAYLOAD = [
    {
        "RuleID": "generic-api-key",
        "Description": "Generic API Key",
        "File": ".env",
        "StartLine": 3,
        "Commit": "abc123",
        "Fingerprint": "abc123:.env:generic-api-key:3",
        "Secret": "super-secret-token",
    }
]


_TRUFFLEHOG_PAYLOAD = {
    "SourceID": 7,
    "SourceMetadata": {
        "Data": {
            "Filesystem": {
                "file": "config/settings.py",
                "line": 12,
            }
        }
    },
    "DetectorName": "AWS",
    "DetectorType": 2,
    "DecoderName": "PLAIN",
    "Verified": True,
    "Raw": "AKIAIOSFODNN7EXAMPLE",
    "Redacted": "AKIAIOSF********AMPLE",
}


class TestSecretSeverity:
    def test_vendor_labels(self):
        assert _normalise_severity("verified") == "CRITICAL"
        assert _normalise_severity("unverified") == "HIGH"
        assert _normalise_severity("low") == "LOW"

    def test_numeric_cvss_like_score(self):
        assert _normalise_severity("9.8") == "CRITICAL"
        assert _normalise_severity("7.5") == "HIGH"
        assert _normalise_severity("4.2") == "MEDIUM"
        assert _normalise_severity("1.0") == "LOW"


class TestSecretGitleaks:
    def test_parses_gitleaks_json_without_raw_secret(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/gitleaks" if x == "gitleaks" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(1, json.dumps(_GITLEAKS_PAYLOAD), ""),
        ):
            report = scan_secrets(tmp_path, scanner="gitleaks")

        assert report.source == "gitleaks"
        assert report.scanner_binary == "/fake/gitleaks"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "generic-api-key"
        assert finding.path == ".env"
        assert finding.line == 3
        assert finding.fingerprint == "abc123:.env:generic-api-key:3"
        assert finding.secret_sha256_prefix
        assert "super-secret-token" not in json.dumps(report.to_dict())
        assert not report.passed

    def test_parses_wrapped_gitleaks_findings(self, tmp_path: Path):
        payload = {"findings": _GITLEAKS_PAYLOAD}
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/gitleaks" if x == "gitleaks" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(1, json.dumps(payload), ""),
        ):
            report = scan_secrets(tmp_path, scanner="gitleaks")

        assert report.source == "gitleaks"
        assert report.severity_counts == {"HIGH": 1}


class TestSecretTruffleHog:
    def test_parses_trufflehog_json_lines(self, tmp_path: Path):
        raw = json.dumps(_TRUFFLEHOG_PAYLOAD) + "\n"
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/trufflehog" if x == "trufflehog" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(0, raw, ""),
        ):
            report = scan_secrets(tmp_path, scanner="trufflehog")

        assert report.source == "trufflehog"
        assert report.scanner_binary == "/fake/trufflehog"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "AWS"
        assert finding.path == "config/settings.py"
        assert finding.line == 12
        assert finding.fingerprint == "7"
        assert finding.redacted == "AKIAIOSF********AMPLE"
        assert finding.verified is True
        assert finding.severity == "CRITICAL"
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(report.to_dict())
        assert not report.passed

    def test_unverified_trufflehog_finding_is_high(self, tmp_path: Path):
        payload = dict(_TRUFFLEHOG_PAYLOAD, Verified=False)
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/trufflehog" if x == "trufflehog" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(183, json.dumps(payload), ""),
        ):
            report = scan_secrets(tmp_path, scanner="trufflehog")

        assert report.severity_counts == {"HIGH": 1}
        assert report.blocking_findings[0].severity == "HIGH"


class TestSecretScanContract:
    def test_mock_when_nothing_on_path(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which", return_value=None
        ):
            report = scan_secrets(tmp_path)

        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed

    def test_invalid_scanner_name(self, tmp_path: Path):
        report = scan_secrets(tmp_path, scanner="detect-secrets")

        assert report.source == "mock"
        assert report.error
        assert not report.passed

    def test_invalid_app_path(self, tmp_path: Path):
        report = scan_secrets(tmp_path / "missing")

        assert report.error
        assert not report.passed

    def test_threshold_can_be_loosened(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/gitleaks" if x == "gitleaks" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(1, json.dumps(_GITLEAKS_PAYLOAD), ""),
        ):
            report = scan_secrets(tmp_path, scanner="gitleaks", fail_on={"CRITICAL"})

        assert report.passed

    def test_report_to_dict_exposes_blocking_count(self):
        report = SecretScanReport(
            source="gitleaks",
            app_path="/tmp/app",
            fail_on=["HIGH"],
            findings=[
                SecretFinding(
                    rule_id="generic-api-key",
                    path=".env",
                    severity="HIGH",
                )
            ],
            total_findings=1,
            severity_counts={"HIGH": 1},
        )

        payload = report.to_dict()

        assert payload["passed"] is False
        assert payload["blocking_count"] == 1
        assert payload["findings"][0]["rule_id"] == "generic-api-key"

    def test_exports_secret_symbols_from_package(self):
        assert SecretFinding
        assert SecretScanReport
        assert scan_secrets


class TestSecretScanCli:
    def test_cli_writes_json_summary(self, tmp_path: Path, capsys):
        with mock.patch(
            "backend.security_scanning.secrets.shutil.which",
            lambda x: "/fake/gitleaks" if x == "gitleaks" else None,
        ), mock.patch(
            "backend.security_scanning.secrets._run",
            return_value=(1, json.dumps(_GITLEAKS_PAYLOAD), ""),
        ):
            rc = _main(
                [
                    "--app-path",
                    str(tmp_path),
                    "--scanner",
                    "gitleaks",
                ]
            )

        summary = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert summary["source"] == "gitleaks"
        assert summary["blocking_count"] == 1

    def test_cli_subprocess_invalid_scanner(self, tmp_path: Path):
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.security_scanning.secrets",
                "--app-path",
                str(tmp_path),
                "--scanner",
                "detect-secrets",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )

        payload = json.loads(proc.stdout)
        assert payload["source"] == "mock"
        assert payload["passed"] is False
        assert "unknown scanner" in payload["error"]
        assert proc.returncode == 1

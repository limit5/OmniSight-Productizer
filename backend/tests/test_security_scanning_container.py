"""SC.4.1 — Unit tests for W4 container artifact scanners.

External Trivy / Grype binaries are monkey-patched so the adapter
contract stays offline and deterministic, mirroring the SC.1/SC.2/SC.3
test shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from backend.deploy.base import BuildArtifact
from backend.security_scanning import (
    ContainerArtifactReport,
    ContainerFinding,
    scan_container_artifact,
)
from backend.security_scanning.container import _main, _normalise_severity


_TRIVY_PAYLOAD = {
    "Results": [
        {
            "Target": "package-lock.json",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-0001",
                    "PkgName": "openssl",
                    "InstalledVersion": "3.0.0",
                    "FixedVersion": "3.0.13",
                    "Severity": "HIGH",
                    "Title": "OpenSSL memory issue",
                }
            ],
        }
    ]
}


_GRYPE_PAYLOAD = {
    "matches": [
        {
            "vulnerability": {
                "id": "GHSA-yyyy",
                "severity": "Critical",
                "description": "BusyBox shell issue",
                "fix": {"versions": ["1.36.1-r2"]},
            },
            "artifact": {
                "name": "busybox",
                "version": "1.36.1-r0",
                "type": "apk",
                "locations": [{"path": "/lib/apk/db/installed"}],
            },
        }
    ]
}


class TestContainerSeverity:
    def test_vendor_labels_and_scores(self):
        assert _normalise_severity("critical") == "CRITICAL"
        assert _normalise_severity("high") == "HIGH"
        assert _normalise_severity("unknown") == "INFO"
        assert _normalise_severity("9.8") == "CRITICAL"
        assert _normalise_severity("7.1") == "HIGH"
        assert _normalise_severity("4.0") == "MEDIUM"


class TestTrivy:
    def test_parses_trivy_json(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, cwd: Path, timeout: int):
            calls.append(cmd)
            assert cwd == tmp_path.resolve()
            assert timeout == 600
            return 0, json.dumps(_TRIVY_PAYLOAD), ""

        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.security_scanning.container._run",
            side_effect=fake_run,
        ):
            report = scan_container_artifact(
                BuildArtifact(path=tmp_path, framework="astro"),
                scanner="trivy",
            )

        assert report.source == "trivy"
        assert report.scanner_binary == "/fake/trivy"
        assert report.artifact_framework == "astro"
        assert report.total_findings == 1
        assert report.severity_counts == {"HIGH": 1}
        assert calls[0][:5] == ["trivy", "fs", "--format", "json", "--quiet"]
        finding = report.findings[0]
        assert finding.vulnerability_id == "CVE-2024-0001"
        assert finding.package == "openssl"
        assert finding.installed_version == "3.0.0"
        assert finding.fixed_version == "3.0.13"
        assert finding.target == "package-lock.json"
        assert not report.passed

    def test_threshold_can_be_loosened(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.security_scanning.container._run",
            return_value=(0, json.dumps(_TRIVY_PAYLOAD), ""),
        ):
            report = scan_container_artifact(
                tmp_path,
                scanner="trivy",
                fail_on={"CRITICAL"},
            )

        assert report.passed


class TestGrype:
    def test_parses_grype_json(self, tmp_path: Path):
        (tmp_path / "Dockerfile").write_text("FROM nginx:alpine\n")
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, cwd: Path, timeout: int):
            calls.append(cmd)
            return 0, json.dumps(_GRYPE_PAYLOAD), ""

        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            lambda x: "/fake/grype" if x == "grype" else None,
        ), mock.patch(
            "backend.security_scanning.container._run",
            side_effect=fake_run,
        ):
            report = scan_container_artifact(tmp_path, scanner="grype")

        assert report.source == "grype"
        assert report.total_findings == 1
        assert report.severity_counts == {"CRITICAL": 1}
        assert calls == [["grype", f"dir:{tmp_path.resolve()}", "-o", "json"]]
        finding = report.findings[0]
        assert finding.vulnerability_id == "GHSA-yyyy"
        assert finding.package == "busybox"
        assert finding.installed_version == "1.36.1-r0"
        assert finding.fixed_version == "1.36.1-r2"
        assert finding.path == "/lib/apk/db/installed"
        assert not report.passed


class TestContainerNoScanner:
    def test_mock_when_nothing_on_path(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            return_value=None,
        ):
            report = scan_container_artifact(tmp_path)

        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed

    def test_probe_order_prefers_trivy(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        calls: list[list[str]] = []

        def fake_which(name: str):
            return {
                "trivy": "/fake/trivy",
                "grype": "/fake/grype",
            }.get(name)

        def fake_run(cmd: list[str], *, cwd: Path, timeout: int):
            calls.append(cmd)
            return 0, json.dumps({"Results": []}), ""

        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            side_effect=fake_which,
        ), mock.patch(
            "backend.security_scanning.container._run",
            side_effect=fake_run,
        ):
            report = scan_container_artifact(tmp_path)

        assert report.source == "trivy"
        assert calls[0][0] == "trivy"

    def test_invalid_scanner_name(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        report = scan_container_artifact(tmp_path, scanner="clair")

        assert report.error == "unknown scanner 'clair' (supported: trivy, grype)"
        assert not report.passed

    def test_invalid_artifact_path(self, tmp_path: Path):
        report = scan_container_artifact(tmp_path / "missing", scanner="trivy")

        assert "Build artifact path does not exist" in report.error
        assert not report.passed

    def test_tool_error_is_reported(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            lambda x: "/fake/trivy" if x == "trivy" else None,
        ), mock.patch(
            "backend.security_scanning.container._run",
            return_value=(2, "", "trivy failed"),
        ):
            report = scan_container_artifact(tmp_path, scanner="trivy")

        assert report.source == "trivy"
        assert report.scanner_binary == "/fake/trivy"
        assert report.error == "trivy failed"
        assert not report.passed

    def test_report_to_dict_exposes_blocking_count(self):
        report = ContainerArtifactReport(
            source="trivy",
            artifact_path="/tmp/app",
            fail_on=["HIGH"],
            findings=[
                ContainerFinding(
                    vulnerability_id="CVE-2024-1",
                    package="openssl",
                    severity="HIGH",
                )
            ],
            total_findings=1,
            severity_counts={"HIGH": 1},
        )

        payload = report.to_dict()

        assert payload["passed"] is False
        assert payload["blocking_count"] == 1
        assert payload["findings"][0]["package"] == "openssl"

    def test_exports_container_symbols_from_package(self):
        assert ContainerArtifactReport
        assert ContainerFinding
        assert scan_container_artifact


class TestContainerCli:
    def test_cli_writes_json_summary(self, tmp_path: Path, capsys):
        (tmp_path / "index.html").write_text("ok\n")
        with mock.patch(
            "backend.security_scanning.container.shutil.which",
            lambda x: "/fake/grype" if x == "grype" else None,
        ), mock.patch(
            "backend.security_scanning.container._run",
            return_value=(0, json.dumps(_GRYPE_PAYLOAD), ""),
        ):
            rc = _main(
                [
                    "--artifact-path",
                    str(tmp_path),
                    "--framework",
                    "astro",
                    "--scanner",
                    "grype",
                ]
            )

        summary = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert summary["source"] == "grype"
        assert summary["artifact_framework"] == "astro"
        assert summary["blocking_count"] == 1

    def test_cli_subprocess_invalid_scanner(self, tmp_path: Path):
        (tmp_path / "index.html").write_text("ok\n")
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.security_scanning.container",
                "--artifact-path",
                str(tmp_path),
                "--scanner",
                "clair",
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

"""SC.3.1 — Unit tests for dependency vulnerability scanner adapters.

All external tools (``npm audit`` / ``osv-scanner`` / ``snyk``) are
monkey-patched so the adapter contract stays offline and deterministic,
mirroring the SC.1 SAST and SC.2 DAST test shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

from backend.security_scanning import (
    SCA_FIX_PR_ARTIFACT,
    SCAFinding,
    SCAFixPR,
    SCAReport,
    plan_sca_fix_prs,
    scan_sca,
    write_sca_fix_pr_artifact,
)
from backend.security_scanning.sca import _normalise_severity


_NPM_AUDIT_PAYLOAD = {
    "vulnerabilities": {
        "ansi-regex": {
            "name": "ansi-regex",
            "severity": "high",
            "range": "<5.0.1",
            "via": [
                {
                    "source": 1097677,
                    "name": "ansi-regex",
                    "title": "Inefficient Regular Expression Complexity",
                    "severity": "high",
                    "range": "<5.0.1",
                }
            ],
            "fixAvailable": {"name": "ansi-regex", "version": "5.0.1"},
        }
    }
}


_OSV_PAYLOAD = {
    "results": [
        {
            "source": {"path": "package-lock.json"},
            "packages": [
                {
                    "package": {
                        "name": "lodash",
                        "version": "4.17.20",
                        "ecosystem": "npm",
                    },
                    "groups": [{"ids": ["GHSA-xxxx"], "max_severity": "7.5"}],
                    "vulnerabilities": [
                        {
                            "id": "GHSA-xxxx",
                            "summary": "Prototype pollution",
                            "affected": [
                                {
                                    "ranges": [
                                        {"events": [{"fixed": "4.17.21"}]}
                                    ]
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}


_SNYK_PAYLOAD = {
    "vulnerabilities": [
        {
            "id": "SNYK-JS-MINIMIST-2429795",
            "packageName": "minimist",
            "version": "1.2.5",
            "severity": "critical",
            "title": "Prototype Pollution",
            "fixedIn": ["1.2.6"],
        }
    ],
    "packageManager": "npm",
    "path": "package.json",
}


class TestSCASeverity:
    def test_vendor_labels(self):
        assert _normalise_severity("critical") == "CRITICAL"
        assert _normalise_severity("high") == "HIGH"
        assert _normalise_severity("moderate") == "MEDIUM"
        assert _normalise_severity("negligible") == "INFO"

    def test_numeric_cvss_like_score(self):
        assert _normalise_severity("9.8") == "CRITICAL"
        assert _normalise_severity("7.5") == "HIGH"
        assert _normalise_severity("4.2") == "MEDIUM"
        assert _normalise_severity("1.0") == "LOW"


class TestSCANpmAudit:
    def test_parses_npm_audit_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/npm" if x == "npm" else None,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(1, json.dumps(_NPM_AUDIT_PAYLOAD), ""),
        ):
            report = scan_sca(tmp_path, scanner="npm-audit")

        assert report.source == "npm-audit"
        assert report.scanner_binary == "/fake/npm"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.package == "ansi-regex"
        assert finding.vulnerability_id == "1097677"
        assert finding.fixed_version == "5.0.1"
        assert finding.severity == "HIGH"
        assert finding.path == "<5.0.1"
        assert not report.passed

    def test_requires_package_json_before_claiming_npm_source(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/npm" if x == "npm" else None,
        ):
            report = scan_sca(tmp_path, scanner="npm-audit")

        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed


class TestSCAOsv:
    def test_parses_osv_json(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/osv-scanner" if x == "osv-scanner" else None,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(1, json.dumps(_OSV_PAYLOAD), ""),
        ):
            report = scan_sca(tmp_path, scanner="osv-scanner")

        assert report.source == "osv-scanner"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.vulnerability_id == "GHSA-xxxx"
        assert finding.package == "lodash"
        assert finding.version == "4.17.20"
        assert finding.fixed_version == "4.17.21"
        assert finding.severity == "HIGH"
        assert finding.path == "package-lock.json"


class TestSCASnyk:
    def test_parses_snyk_json(self, tmp_path: Path):
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/snyk" if x == "snyk" else None,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(1, json.dumps(_SNYK_PAYLOAD), ""),
        ):
            report = scan_sca(tmp_path, scanner="snyk")

        assert report.source == "snyk"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.vulnerability_id == "SNYK-JS-MINIMIST-2429795"
        assert finding.package == "minimist"
        assert finding.version == "1.2.5"
        assert finding.fixed_version == "1.2.6"
        assert finding.severity == "CRITICAL"
        assert finding.path == "package.json"

    def test_threshold_can_be_loosened(self, tmp_path: Path):
        payload = {
            "vulnerabilities": [
                {
                    "id": "SNYK-JS-LOW",
                    "packageName": "debug",
                    "severity": "high",
                }
            ]
        }
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/snyk" if x == "snyk" else None,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(1, json.dumps(payload), ""),
        ):
            report = scan_sca(tmp_path, scanner="snyk", fail_on={"CRITICAL"})

        assert report.passed


class TestSCANoScanner:
    def test_mock_when_nothing_on_path(self, tmp_path: Path):
        with mock.patch("backend.security_scanning.sca.shutil.which", return_value=None):
            report = scan_sca(tmp_path)

        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed

    def test_probe_order_prefers_npm_audit_for_node_project(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        calls: list[list[str]] = []

        def fake_which(name: str):
            return {
                "npm": "/fake/npm",
                "osv-scanner": "/fake/osv-scanner",
                "snyk": "/fake/snyk",
            }.get(name)

        def fake_run(cmd: list[str], *, cwd: Path, timeout: int):
            calls.append(cmd)
            return 0, json.dumps({"vulnerabilities": {}}), ""

        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            side_effect=fake_which,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            side_effect=fake_run,
        ):
            report = scan_sca(tmp_path)

        assert report.source == "npm-audit"
        assert calls == [["npm", "audit", "--json"]]

    def test_npm_absent_falls_through_to_osv(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')

        def fake_which(name: str):
            return "/fake/osv-scanner" if name == "osv-scanner" else None

        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            side_effect=fake_which,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(0, json.dumps({"results": []}), ""),
        ):
            report = scan_sca(tmp_path)

        assert report.source == "osv-scanner"

    def test_invalid_scanner_name(self, tmp_path: Path):
        report = scan_sca(tmp_path, scanner="retirejs")

        assert report.error == (
            "unknown scanner 'retirejs' "
            "(supported: npm-audit, osv-scanner, snyk)"
        )
        assert not report.passed

    def test_invalid_app_path(self, tmp_path: Path):
        report = scan_sca(tmp_path / "missing")

        assert report.error
        assert not report.passed

    def test_tool_error_is_reported(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        with mock.patch(
            "backend.security_scanning.sca.shutil.which",
            lambda x: "/fake/npm" if x == "npm" else None,
        ), mock.patch(
            "backend.security_scanning.sca._run",
            return_value=(2, "", "audit failed"),
        ):
            report = scan_sca(tmp_path, scanner="npm-audit")

        assert report.source == "npm-audit"
        assert report.scanner_binary == "/fake/npm"
        assert report.error == "audit failed"
        assert not report.passed

    def test_report_to_dict_exposes_blocking_count(self):
        report = SCAReport(
            source="snyk",
            app_path="/tmp/app",
            fail_on=["HIGH"],
            findings=[
                SCAFinding(
                    vulnerability_id="SNYK-JS-X",
                    package="x",
                    severity="HIGH",
                )
            ],
            total_findings=1,
            severity_counts={"HIGH": 1},
        )

        payload = report.to_dict()

        assert payload["passed"] is False
        assert payload["blocking_count"] == 1
        assert payload["findings"][0]["package"] == "x"

    def test_exports_sca_symbols_from_package(self):
        assert SCA_FIX_PR_ARTIFACT
        assert SCAFinding
        assert SCAFixPR
        assert SCAReport
        assert plan_sca_fix_prs


class TestSCAFixPrPlan:
    def test_plans_dependabot_style_security_pr_for_fixable_blocker(self):
        report = SCAReport(
            source="osv-scanner",
            app_path="/tmp/generated-app",
            fail_on=["CRITICAL", "HIGH"],
            findings=[
                SCAFinding(
                    vulnerability_id="GHSA-xxxx",
                    package="lodash",
                    version="4.17.20",
                    fixed_version="4.17.21",
                    severity="HIGH",
                    ecosystem="npm",
                    path="package-lock.json",
                    tool="osv-scanner",
                ),
                SCAFinding(
                    vulnerability_id="CVE-2021-23337",
                    package="lodash",
                    version="4.17.20",
                    fixed_version=">=4.17.21",
                    severity="CRITICAL",
                    ecosystem="npm",
                    path="package-lock.json",
                    tool="osv-scanner",
                ),
            ],
            total_findings=2,
            severity_counts={"CRITICAL": 1, "HIGH": 1},
        )

        fixes = plan_sca_fix_prs(report, base_branch="main")

        assert len(fixes) == 1
        fix = fixes[0]
        assert fix.package == "lodash"
        assert fix.ecosystem == "npm"
        assert fix.fixed_version == "4.17.21"
        assert fix.severity == "CRITICAL"
        assert fix.base_branch == "main"
        assert fix.branch == "omnisight/sca-fix/npm/lodash-4.17.21"
        assert fix.title == "fix(deps): bump lodash to 4.17.21"
        assert fix.update_command == "npm install lodash@4.17.21 --package-lock-only"
        assert fix.labels == [
            "security",
            "dependencies",
            "auto-merge",
            "priority/critical",
        ]
        assert fix.vulnerability_ids == ["CVE-2021-23337", "GHSA-xxxx"]
        assert "docs/ops/dependency_upgrade_runbook.md" in fix.body

    def test_ignores_non_blocking_or_unfixable_findings(self):
        report = SCAReport(
            fail_on=["HIGH"],
            findings=[
                SCAFinding(
                    vulnerability_id="LOW-1",
                    package="debug",
                    fixed_version="4.3.7",
                    severity="LOW",
                    ecosystem="npm",
                ),
                SCAFinding(
                    vulnerability_id="HIGH-1",
                    package="left-pad",
                    severity="HIGH",
                    ecosystem="npm",
                ),
            ],
        )

        assert plan_sca_fix_prs(report) == []

    def test_python_ecosystem_command_uses_pip_compile(self):
        report = SCAReport(
            fail_on=["HIGH"],
            findings=[
                SCAFinding(
                    vulnerability_id="PYSEC-1",
                    package="jinja2",
                    version="3.1.2",
                    fixed_version="3.1.4",
                    severity="HIGH",
                    ecosystem="PyPI",
                    path="backend/requirements.txt",
                )
            ],
        )

        fix = plan_sca_fix_prs(report)[0]

        assert fix.ecosystem == "pypi"
        assert fix.update_command == (
            "pip-compile --upgrade-package jinja2==3.1.4 "
            "backend/requirements.in"
        )

    def test_fix_pr_artifact_is_written(self, tmp_path: Path):
        fix = SCAFixPR(
            package="minimist",
            ecosystem="npm",
            fixed_version="1.2.6",
            vulnerability_ids=["SNYK-JS-MINIMIST-2429795"],
            severity="CRITICAL",
            branch="omnisight/sca-fix/npm/minimist-1.2.6",
            title="fix(deps): bump minimist to 1.2.6",
        )

        out = write_sca_fix_pr_artifact([fix], tmp_path)
        payload = json.loads(out.read_text())

        assert out == tmp_path.resolve() / SCA_FIX_PR_ARTIFACT
        assert payload["fix_pr_count"] == 1
        assert payload["fix_prs"][0]["package"] == "minimist"


class TestSCACli:
    def test_cli_writes_json_summary(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.security_scanning.sca",
                "--app-path",
                str(tmp_path),
                "--scanner",
                "retirejs",
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

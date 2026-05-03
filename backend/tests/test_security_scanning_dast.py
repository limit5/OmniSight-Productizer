"""SC.2.1 — Unit tests for OWASP ZAP DAST adapter.

External ZAP binaries are monkey-patched so the adapter contract stays
offline and deterministic, mirroring the SC.1 SAST test shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from backend.security_scanning import (
    DASTFinding,
    DASTPreviewScan,
    DASTReport,
    scan_web_preview_zap,
    scan_zap_baseline,
)
from backend.security_scanning.dast import _normalise_severity
from backend.web_sandbox import WebSandboxConfig, WebSandboxInstance, WebSandboxStatus


_ZAP_PAYLOAD = {
    "site": [
        {
            "name": "https://preview.example.test",
            "alerts": [
                {
                    "pluginid": "10038",
                    "alert": "Content Security Policy Header Not Set",
                    "riskcode": "2",
                    "riskdesc": "Medium (High)",
                    "confidence": "Medium",
                    "cweid": "693",
                    "wascid": "15",
                    "desc": "CSP is missing",
                    "solution": "Set a Content-Security-Policy header",
                    "instances": [
                        {
                            "uri": "https://preview.example.test/",
                            "param": "",
                            "evidence": "",
                        }
                    ],
                }
            ],
        }
    ]
}


def _write_zap_report(cmd: list[str], *, cwd: Path, timeout: int):
    assert timeout == 600
    assert "-J" in cmd
    report_arg = cmd[cmd.index("-J") + 1]
    report_path = Path(report_arg)
    if not report_path.is_absolute():
        report_path = cwd / report_path
    report_path.write_text(json.dumps(_ZAP_PAYLOAD))
    return 1, "", ""


class TestDASTSeverity:
    def test_zap_risk_labels_and_codes(self):
        assert _normalise_severity("High (Medium)") == "HIGH"
        assert _normalise_severity("Informational") == "INFO"
        assert _normalise_severity("2") == "LOW"
        assert _normalise_severity("7.1") == "HIGH"


class TestZAPBaseline:
    def test_parses_zap_json_report(self):
        with mock.patch(
            "backend.security_scanning.dast.shutil.which",
            lambda x: "/fake/zap-baseline.py" if x == "zap-baseline.py" else None,
        ), mock.patch(
            "backend.security_scanning.dast._run",
            side_effect=_write_zap_report,
        ):
            report = scan_zap_baseline("https://preview.example.test/")

        assert report.source == "zap"
        assert report.scanner_binary == "/fake/zap-baseline.py"
        assert report.total_findings == 1
        finding = report.findings[0]
        assert finding.rule_id == "10038"
        assert finding.name == "Content Security Policy Header Not Set"
        assert finding.url == "https://preview.example.test/"
        assert finding.severity == "MEDIUM"
        assert finding.cwe == ["CWE-693"]
        assert finding.owasp == ["WASC-15"]
        assert report.severity_counts == {"MEDIUM": 1}
        assert report.passed

    def test_high_finding_blocks_by_default(self):
        payload = {
            "alerts": [
                {
                    "pluginid": "40012",
                    "alert": "Reflected XSS",
                    "riskdesc": "High (Medium)",
                    "instances": [{"uri": "http://127.0.0.1:40123/search?q=x"}],
                }
            ]
        }

        def write_high_report(cmd: list[str], *, cwd: Path, timeout: int):
            report_path = Path(cmd[cmd.index("-J") + 1])
            if not report_path.is_absolute():
                report_path = cwd / report_path
            report_path.write_text(json.dumps(payload))
            return 2, "", ""

        with mock.patch(
            "backend.security_scanning.dast.shutil.which",
            lambda x: "/fake/zap-baseline.py" if x == "zap-baseline.py" else None,
        ), mock.patch(
            "backend.security_scanning.dast._run",
            side_effect=write_high_report,
        ):
            report = scan_zap_baseline("http://127.0.0.1:40123/")

        assert report.source == "zap"
        assert report.total_findings == 1
        assert report.blocking_findings[0].severity == "HIGH"
        assert not report.passed

    def test_threshold_can_be_loosened(self):
        payload = {
            "alerts": [
                {
                    "pluginid": "40012",
                    "alert": "Reflected XSS",
                    "riskdesc": "High (Medium)",
                    "instances": [{"uri": "http://127.0.0.1:40123/search?q=x"}],
                }
            ]
        }

        def write_high_report(cmd: list[str], *, cwd: Path, timeout: int):
            report_path = Path(cmd[cmd.index("-J") + 1])
            if not report_path.is_absolute():
                report_path = cwd / report_path
            report_path.write_text(json.dumps(payload))
            return 2, "", ""

        with mock.patch(
            "backend.security_scanning.dast.shutil.which",
            lambda x: "/fake/zap-baseline.py" if x == "zap-baseline.py" else None,
        ), mock.patch(
            "backend.security_scanning.dast._run",
            side_effect=write_high_report,
        ):
            report = scan_zap_baseline(
                "http://127.0.0.1:40123/",
                fail_on={"CRITICAL"},
            )

        assert report.passed

    def test_uses_docker_fallback_when_zap_script_missing(self):
        calls: list[list[str]] = []

        def fake_which(name: str):
            if name == "docker":
                return "/usr/bin/docker"
            return None

        def fake_run(cmd: list[str], *, cwd: Path, timeout: int):
            calls.append(cmd)
            (cwd / "zap.json").write_text(json.dumps({"alerts": []}))
            return 0, "", ""

        with mock.patch(
            "backend.security_scanning.dast.shutil.which",
            side_effect=fake_which,
        ), mock.patch(
            "backend.security_scanning.dast._run",
            side_effect=fake_run,
        ):
            report = scan_zap_baseline("https://preview.example.test/")

        assert report.source == "zap"
        assert report.scanner_binary == "/usr/bin/docker"
        assert calls[0][:3] == ["docker", "run", "--rm"]
        assert "ghcr.io/zaproxy/zaproxy:stable" in calls[0]

    def test_mock_when_no_zap_runner_on_path(self):
        with mock.patch("backend.security_scanning.dast.shutil.which", return_value=None):
            report = scan_zap_baseline("https://preview.example.test/")

        assert report.source == "mock"
        assert report.total_findings == 0
        assert report.passed

    def test_rejects_invalid_url(self):
        report = scan_zap_baseline("file:///tmp/index.html")

        assert report.error
        assert not report.passed


class TestWebPreviewZAP:
    @staticmethod
    def _instance(status: WebSandboxStatus = WebSandboxStatus.running):
        return WebSandboxInstance(
            workspace_id="ws-42",
            sandbox_id="preview-ws-42",
            container_name="omnisight-preview-ws-42",
            config=WebSandboxConfig(
                workspace_id="ws-42",
                workspace_path="/tmp/ws-42",
            ),
            status=status,
            preview_url="http://127.0.0.1:40123/",
            ingress_url="https://preview-ws-42.example.test/",
            created_at=1.0,
            last_request_at=1.0,
        )

    def test_scans_running_web_sandbox_instance_ingress_url(self):
        calls: list[str] = []

        def fake_scan(target_url: str, **_kwargs):
            calls.append(target_url)
            return DASTReport(source="mock", target_url=target_url, fail_on=["HIGH"])

        with mock.patch(
            "backend.security_scanning.dast.scan_zap_baseline",
            side_effect=fake_scan,
        ):
            scan = scan_web_preview_zap(self._instance())

        assert scan.triggered
        assert scan.reason == "web_preview"
        assert scan.workspace_id == "ws-42"
        assert scan.sandbox_id == "preview-ws-42"
        assert scan.target_url == "https://preview-ws-42.example.test/"
        assert calls == ["https://preview-ws-42.example.test/"]

    def test_accepts_web_sandbox_to_dict_shape(self):
        preview = self._instance().to_dict()
        preview["ingress_url"] = None
        calls: list[str] = []

        def fake_scan(target_url: str, **_kwargs):
            calls.append(target_url)
            return DASTReport(source="mock", target_url=target_url, fail_on=["HIGH"])

        with mock.patch(
            "backend.security_scanning.dast.scan_zap_baseline",
            side_effect=fake_scan,
        ):
            scan = scan_web_preview_zap(preview)

        assert scan.triggered
        assert scan.target_url == "http://127.0.0.1:40123/"
        assert calls == ["http://127.0.0.1:40123/"]

    def test_rejects_preview_before_ready(self):
        scan = scan_web_preview_zap(self._instance(status=WebSandboxStatus.installing))

        assert not scan.triggered
        assert scan.reason == "preview_not_ready"
        assert scan.report.error

    def test_rejects_preview_without_url(self):
        preview = self._instance().to_dict()
        preview["ingress_url"] = None
        preview["preview_url"] = None

        scan = scan_web_preview_zap(preview)

        assert not scan.triggered
        assert scan.reason == "missing_preview_url"
        assert scan.report.error

    def test_exports_dast_symbols_from_package(self):
        assert DASTFinding
        assert DASTPreviewScan
        assert DASTReport

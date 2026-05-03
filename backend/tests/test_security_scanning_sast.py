"""SC.1.1 — Unit tests for SAST scanner adapters.

All external tools (``codeql`` / ``semgrep`` / ``snyk``) are
monkey-patched so the adapter contract stays offline and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from backend import workspace as workspace_mod
from backend.security_scanning import (
    SAST_COMMIT_SCAN_ARTIFACT,
    SASTCommitScan,
    SASTReport,
    scan_generated_workspace_commit,
    scan_sast,
    write_sast_commit_scan_artifact,
)
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


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _init_generated_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "generated-app"
    workspace.mkdir()
    _git("init", "-q", "-b", "main", cwd=workspace)
    _git("config", "user.email", "sast-test@example.com", cwd=workspace)
    _git("config", "user.name", "sast-test", cwd=workspace)
    (workspace / "app.py").write_text("print('hello')\n")
    _git("add", "app.py", cwd=workspace)
    _git("commit", "-q", "-m", "initial", cwd=workspace)
    return workspace


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


class TestSASTGeneratedWorkspaceCommit:
    def test_scans_git_workspace_after_commit(self, tmp_path: Path):
        workspace = _init_generated_workspace(tmp_path)
        head = _git("rev-parse", "HEAD", cwd=workspace)

        with mock.patch(
            "backend.security_scanning.sast.shutil.which",
            lambda x: "/fake/semgrep" if x == "semgrep" else None,
        ), mock.patch(
            "backend.security_scanning.sast._run",
            return_value=(1, json.dumps(_SEMGREP_PAYLOAD), ""),
        ):
            scan = scan_generated_workspace_commit(
                workspace,
                commit_sha=head,
                scanner="semgrep",
            )

        assert scan.triggered
        assert scan.reason == "commit"
        assert scan.commit_sha == head
        assert scan.workspace_path == str(workspace.resolve())
        assert scan.report.source == "semgrep"
        assert scan.report.total_findings == 1

    def test_rejects_non_git_workspace(self, tmp_path: Path):
        scan = scan_generated_workspace_commit(tmp_path)
        assert not scan.triggered
        assert scan.reason == "not_git_workspace"
        assert scan.report.error

    def test_commit_scan_artifact_is_written(self, tmp_path: Path):
        workspace = _init_generated_workspace(tmp_path)
        scan = SASTCommitScan(
            workspace_path=str(workspace.resolve()),
            commit_sha="abc123",
            triggered=True,
            reason="commit",
            report=SASTReport(source="mock", app_path=str(workspace)),
        )

        out = write_sast_commit_scan_artifact(scan, workspace)
        payload = json.loads(out.read_text())

        assert out == workspace / SAST_COMMIT_SCAN_ARTIFACT
        assert payload["triggered"] is True
        assert payload["commit_sha"] == "abc123"

    def test_workspace_provision_installs_post_commit_hook(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        source = _init_generated_workspace(tmp_path)
        ws_root = tmp_path / "ws-root"
        ws_root.mkdir()
        monkeypatch.setattr(workspace_mod, "_WORKSPACES_ROOT", ws_root, raising=True)
        info = asyncio.run(workspace_mod.provision(
            agent_id="agent-sast-hook",
            task_id="SC.1.2",
            remote_url=str(source),
        ))
        try:
            (info.path / "app.py").write_text("print('hook')\n")
            _git("add", "app.py", cwd=info.path)
            _git("commit", "-q", "-m", "hook scan", cwd=info.path)

            artifact = info.path / SAST_COMMIT_SCAN_ARTIFACT
            payload = json.loads(artifact.read_text())
            assert payload["triggered"] is True
            assert payload["reason"] == "commit"
            assert payload["commit_sha"] == _git("rev-parse", "HEAD", cwd=info.path)
            assert payload["report"]["source"] == "mock"
        finally:
            asyncio.run(workspace_mod.cleanup("agent-sast-hook"))

    def test_finalize_auto_commit_includes_sast_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        workspace = _init_generated_workspace(tmp_path)
        (workspace / "app.py").write_text("print('finalized')\n")
        calls: list[Path] = []

        def fake_scan(path: Path | str, *, commit_sha: str = ""):
            calls.append(Path(path).resolve())
            return SASTCommitScan(
                workspace_path=str(Path(path).resolve()),
                commit_sha=commit_sha or "abc123",
                triggered=True,
                reason="commit",
                report=SASTReport(source="mock", app_path=str(path)),
            )

        monkeypatch.setattr(
            "backend.security_scanning.scan_generated_workspace_commit",
            fake_scan,
        )
        monkeypatch.setitem(
            workspace_mod._workspaces,
            "agent-sast-finalize",
            workspace_mod.WorkspaceInfo(
                agent_id="agent-sast-finalize",
                task_id="SC.1.2",
                branch="main",
                path=workspace,
                repo_source=str(workspace),
            ),
        )
        try:
            result = asyncio.run(workspace_mod.finalize("agent-sast-finalize"))
        finally:
            workspace_mod._workspaces.pop("agent-sast-finalize", None)

        assert calls == [workspace.resolve()]
        assert result["commit_count"] == 1
        assert result["sast_scan"]["triggered"] is True
        assert result["sast_scan"]["report"]["source"] == "mock"

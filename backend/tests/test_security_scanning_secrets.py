"""SC.5.1 — Unit tests for secret scanner adapters.

All external tools (``gitleaks`` / ``trufflehog``) are monkey-patched
so the adapter contract stays offline and deterministic, mirroring the
SC.1 SAST, SC.3 SCA, and SC.4 container test shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from backend import workspace as workspace_mod
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


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _init_git_repo(path: Path) -> Path:
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "secret-test@example.com", cwd=path)
    _git("config", "user.name", "secret-test", cwd=path)
    (path / "app.py").write_text("print('hello')\n")
    _git("add", "app.py", cwd=path)
    _git("commit", "-q", "-m", "initial", cwd=path)
    return path


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


class TestSecretPreCommitHook:
    @staticmethod
    def _stub_workspace_events(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(workspace_mod, "emit_pipeline_phase", lambda *a, **k: None)
        monkeypatch.setattr(workspace_mod, "emit_workspace", lambda *a, **k: None)
        monkeypatch.setattr(workspace_mod, "emit_agent_update", lambda *a, **k: None)

    def test_workspace_provision_installs_pre_commit_hook(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        self._stub_workspace_events(monkeypatch)
        source = _init_git_repo(tmp_path / "source")
        ws_root = tmp_path / "ws-root"
        ws_root.mkdir()
        monkeypatch.setattr(workspace_mod, "_WORKSPACES_ROOT", ws_root, raising=True)
        info = asyncio.run(
            workspace_mod.provision(
                agent_id="agent-secret-hook",
                task_id="SC.5.2",
                remote_url=str(source),
            )
        )
        try:
            hook = Path(
                _git("rev-parse", "--git-path", "hooks/pre-commit", cwd=info.path)
            )
            if not hook.is_absolute():
                hook = info.path / hook
            text = hook.read_text()

            assert hook.exists()
            assert "backend.security_scanning.secrets" in text
            assert "--app-path \"$PWD\"" in text
        finally:
            asyncio.run(workspace_mod.cleanup("agent-secret-hook"))

    def test_pre_commit_hook_blocks_detected_secret(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        self._stub_workspace_events(monkeypatch)
        source = _init_git_repo(tmp_path / "source")
        ws_root = tmp_path / "ws-root"
        ws_root.mkdir()
        monkeypatch.setattr(workspace_mod, "_WORKSPACES_ROOT", ws_root, raising=True)
        info = asyncio.run(
            workspace_mod.provision(
                agent_id="agent-secret-block",
                task_id="SC.5.2",
                remote_url=str(source),
            )
        )
        try:
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            gitleaks = fake_bin / "gitleaks"
            gitleaks.write_text(
                "#!/bin/sh\n"
                "report=\"\"\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --report-path) shift; report=\"$1\" ;;\n"
                "  esac\n"
                "  shift || true\n"
                "done\n"
                "printf '%s' '[{\"RuleID\":\"generic-api-key\","
                "\"File\":\"app.py\",\"StartLine\":1,"
                "\"Secret\":\"super-secret-token\"}]' > \"$report\"\n"
                "exit 1\n"
            )
            gitleaks.chmod(0o755)
            monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

            (info.path / "app.py").write_text("print('secret hook')\n")
            _git("add", "app.py", cwd=info.path)
            proc = subprocess.run(
                ["git", "commit", "-m", "blocked secret"],
                cwd=info.path,
                capture_output=True,
                text=True,
                check=False,
            )

            assert proc.returncode != 0
            assert "OmniSight secret scan blocked this commit." in proc.stderr
            assert (
                _git("rev-parse", "--verify", "HEAD", cwd=info.path)
                == info.anchor_sha
            )
        finally:
            asyncio.run(workspace_mod.cleanup("agent-secret-block"))


class TestRepoSecretPreCommitConfig:
    def test_repo_pre_commit_runs_gitleaks_and_trufflehog(self):
        repo_root = Path(__file__).resolve().parents[2]
        config = (repo_root / ".pre-commit-config.yaml").read_text()

        assert "id: omnisight-gitleaks" in config
        assert "--scanner gitleaks" in config
        assert "id: omnisight-trufflehog" in config
        assert "--scanner trufflehog" in config

    def test_ci_runs_secret_pre_commit_hooks(self):
        repo_root = Path(__file__).resolve().parents[2]
        workflow = (repo_root / ".github/workflows/ci.yml").read_text()

        assert "secret-pre-commit:" in workflow
        assert "pre-commit run --all-files --show-diff-on-failure" in workflow

    def test_ci_secret_pre_commit_is_pull_request_hard_gate(self):
        repo_root = Path(__file__).resolve().parents[2]
        workflow = (repo_root / ".github/workflows/ci.yml").read_text()

        triggers = workflow.split("jobs:", 1)[0]
        job = workflow.split("  secret-pre-commit:", 1)[1].split(
            "\n  catalog-schema:",
            1,
        )[0]

        assert "pull_request:" in triggers
        assert "KS.4.2" in job
        assert "go install github.com/gitleaks/gitleaks/v8@v8.24.0" in job
        assert "go install github.com/trufflesecurity/trufflehog/v3@v3.88.8" in job
        assert "continue-on-error" not in job

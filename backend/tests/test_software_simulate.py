"""X1 #297 — integration test for `scripts/simulate.sh --type=software`.

Exercises the shell wrapper end-to-end (bash → python3
backend.software_simulator → JSON report). Same pattern as
`test_web_simulate.py` / `test_mobile_simulate.py`: module-scoped
subprocess, JSON parse, assert on top-level fields.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "simulate.sh"


def _run_sim(*extra: str, app_path: Path | None = None, timeout: int = 120) -> dict:
    if not _SCRIPT.exists():
        pytest.skip("simulate.sh not present")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    env = os.environ.copy()
    env["WORKSPACE"] = str(_REPO)
    argv = ["bash", str(_SCRIPT), "--type=software", *extra]
    if app_path is not None:
        argv.append(f"--software-app-path={app_path}")
    proc = subprocess.run(
        argv, env=env, cwd=_REPO, capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        raise


class TestSoftwareShellEnvelope:
    def test_envelope_shape(self, tmp_path: Path):
        # Empty dir → language detection fails → envelope still emitted
        # (driver always prints JSON), but overall_pass is false.
        data = _run_sim("--module=linux-x86_64-native", app_path=tmp_path)
        assert data["track"] == "software"
        assert data["module"] == "linux-x86_64-native"
        assert "software" in data
        sw = data["software"]
        for key in (
            "language", "packaging", "test_runner", "test_status",
            "coverage_status", "coverage_pct", "coverage_threshold",
            "benchmark_status", "overall_pass",
        ):
            assert key in sw, f"missing {key}"
        assert sw["packaging"] == "deb"

    def test_python_project_detected(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.dummy]\n")
        data = _run_sim("--module=linux-x86_64-native", app_path=tmp_path)
        sw = data["software"]
        assert sw["language"] == "python"
        # Coverage threshold must equal X1 spec for Python.
        assert float(sw["coverage_threshold"]) == 80.0

    def test_go_project_detected(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module x\ngo 1.22\n")
        data = _run_sim("--module=linux-arm64-native", app_path=tmp_path)
        sw = data["software"]
        assert sw["language"] == "go"
        assert float(sw["coverage_threshold"]) == 70.0

    def test_rust_project_detected(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
        data = _run_sim("--module=macos-arm64-native", app_path=tmp_path)
        sw = data["software"]
        assert sw["language"] == "rust"
        assert float(sw["coverage_threshold"]) == 75.0

    def test_language_override(self, tmp_path: Path):
        # No marker — force the language.
        data = _run_sim(
            "--module=linux-x86_64-native",
            "--language=java",
            app_path=tmp_path,
        )
        sw = data["software"]
        assert sw["language"] == "java"
        assert float(sw["coverage_threshold"]) == 70.0

    def test_coverage_override_threshold(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        data = _run_sim(
            "--module=linux-x86_64-native",
            "--coverage-override=50",
            app_path=tmp_path,
        )
        sw = data["software"]
        assert float(sw["coverage_threshold"]) == 50.0

    def test_benchmark_opt_in_skips_without_baseline(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        data = _run_sim(
            "--module=linux-x86_64-native",
            "--benchmark=on",
            app_path=tmp_path,
        )
        sw = data["software"]
        # No baseline json under test_assets/benchmarks/<module>.json
        # ⇒ benchmark gate must degrade to skip, never fail.
        assert sw["benchmark_status"] in ("skip", "mock")

    def test_non_software_profile_surfaces_error(self, tmp_path: Path):
        # `aarch64` is an embedded profile — the driver must refuse it.
        data = _run_sim("--module=aarch64", app_path=tmp_path)
        # Driver catches SoftwareSimError and emits a non-passing
        # summary; simulate.sh translates that into a failed gate.
        sw = data["software"]
        assert sw["overall_pass"] is False

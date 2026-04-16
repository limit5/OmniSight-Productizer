"""C26 — integration test for scripts/simulate.sh hmi track (#261).

Exercises the shell track end-to-end, parses the JSON report, and
verifies bundle / budget / security / components fields are populated.
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


@pytest.fixture(scope="module")
def simulate_json() -> dict:
    if not _SCRIPT.exists():
        pytest.skip("simulate.sh not present")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    env = os.environ.copy()
    env["WORKSPACE"] = str(_REPO)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--type=hmi", "--module=preact", "--platform=aarch64"],
        env=env, cwd=_REPO, capture_output=True, text=True, timeout=60,
    )
    # JSON report is on stdout
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        raise


class TestHMITrack:
    def test_track_label(self, simulate_json):
        assert simulate_json["track"] == "hmi"

    def test_framework_and_components_populated(self, simulate_json):
        assert simulate_json["hmi"]["framework"] == "preact"
        assert "network" in simulate_json["hmi"]["components"]
        assert "ota" in simulate_json["hmi"]["components"]
        assert "logs" in simulate_json["hmi"]["components"]

    def test_bundle_within_budget(self, simulate_json):
        bundle = simulate_json["hmi"]["bundle_bytes"]
        budget = simulate_json["hmi"]["budget_bytes"]
        assert 0 < bundle <= budget

    def test_security_gate_pass(self, simulate_json):
        assert simulate_json["hmi"]["security_status"] == "pass"

    def test_tests_passed(self, simulate_json):
        assert simulate_json["tests"]["failed"] == 0
        assert simulate_json["tests"]["passed"] >= 3

    def test_overall_status(self, simulate_json):
        assert simulate_json["status"] == "pass"


class TestRejectUnknownType:
    def test_unknown_track_fails(self):
        if not _SCRIPT.exists() or shutil.which("bash") is None:
            pytest.skip("simulate.sh / bash not available")
        env = os.environ.copy()
        env["WORKSPACE"] = str(_REPO)
        proc = subprocess.run(
            ["bash", str(_SCRIPT), "--type=bogus", "--module=x"],
            env=env, cwd=_REPO, capture_output=True, text=True, timeout=15,
        )
        # Script exits non-zero for invalid type
        assert proc.returncode != 0
        data = json.loads(proc.stdout)
        assert data["status"] == "error"

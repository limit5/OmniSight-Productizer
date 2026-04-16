"""P2 #287 — integration test for `scripts/simulate.sh --type=mobile`.

Exercises the shell wrapper end-to-end (bash → python3
backend.mobile_simulator → JSON report). Same pattern as
`test_web_simulate.py` / `test_hmi_simulate.py`: module-scoped
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


def _run_sim(*extra: str) -> dict:
    if not _SCRIPT.exists():
        pytest.skip("simulate.sh not present")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    env = os.environ.copy()
    env["WORKSPACE"] = str(_REPO)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--type=mobile", *extra],
        env=env, cwd=_REPO, capture_output=True, text=True, timeout=120,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        raise


class TestMobileShellEnvelope:
    def test_android_profile_produces_envelope(self):
        data = _run_sim("--module=android-arm64-v8a")
        assert data["track"] == "mobile"
        assert data["module"] == "android-arm64-v8a"
        assert data["mobile"]["platform"] == "android"
        assert data["mobile"]["abi"] == "arm64-v8a"
        assert data["mobile"]["emulator_status"] == "mock"
        # With no external tools on PATH, the driver fills every gate
        # with a mock/skip result — status must still be "pass".
        assert data["status"] == "pass"
        assert data["tests"]["total"] >= 5  # driver + 5 gates

    def test_ios_profile_with_farm(self):
        data = _run_sim(
            "--module=ios-simulator",
            "--farm=firebase",
            "--devices=iPhone 15 Pro",
            "--locales=en-US,zh-TW",
        )
        assert data["mobile"]["platform"] == "ios"
        assert data["mobile"]["ui_framework"] == "xcuitest"
        assert data["mobile"]["device_farm_name"] == "firebase"

    def test_invalid_type_rejected(self):
        env = os.environ.copy()
        env["WORKSPACE"] = str(_REPO)
        proc = subprocess.run(
            ["bash", str(_SCRIPT), "--type=bogus", "--module=x"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode != 0
        data = json.loads(proc.stdout)
        assert data["status"] == "error"
        assert "mobile" in " ".join(data["errors"])

"""W5 #279 — End-to-end integration test for ``simulate.sh --w5-compliance=on``.

Exercises the full shell→python wire: when the compliance fixture
(``configs/web/fixtures/compliance-site``) is fed through simulate.sh
with ``--w5-compliance=on``, the top-level report should pick up an
extra ``w5_compliance`` test detail and still pass overall.

The default (flag off) is covered by ``test_web_simulate.py``; this
file only verifies that the opt-in path runs and produces the expected
wire-level shape.
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
_FIXTURE = _REPO / "configs" / "web" / "fixtures" / "compliance-site"


def _run(*extra: str) -> dict:
    if not _SCRIPT.exists():
        pytest.skip("simulate.sh not present")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    env = os.environ.copy()
    env["WORKSPACE"] = str(_REPO)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--type=web", *extra],
        env=env, cwd=_REPO, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode not in (0, 1):
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        pytest.fail(f"simulate.sh exited {proc.returncode}")
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def sim_w5_compliant() -> dict:
    return _run(
        "--module=web-static",
        f"--app-path={_FIXTURE}",
        "--w5-compliance=on",
    )


class TestFixtureExists:
    def test_compliance_fixture_present(self):
        assert _FIXTURE.is_dir(), f"W5 fixture missing at {_FIXTURE}"

    def test_fixture_has_gdpr_artefacts(self):
        assert (_FIXTURE / "docs" / "privacy" / "retention.md").is_file()
        assert (_FIXTURE / "docs" / "privacy" / "dpa.md").is_file()
        assert (_FIXTURE / "server.py").is_file()
        assert (_FIXTURE / "index.html").is_file()


class TestW5OnPasses:
    def test_w5_detail_recorded(self, sim_w5_compliant):
        details = sim_w5_compliant.get("tests", {}).get("details", [])
        names = {d.get("name") for d in details}
        assert "w5_compliance" in names

    def test_w5_detail_passed(self, sim_w5_compliant):
        details = sim_w5_compliant.get("tests", {}).get("details", [])
        w5 = next(d for d in details if d.get("name") == "w5_compliance")
        assert w5["status"] == "pass"

    def test_overall_still_passes(self, sim_w5_compliant):
        assert sim_w5_compliant["status"] == "pass"
        assert sim_w5_compliant["tests"]["failed"] == 0


class TestW5OffIsDefault:
    """If --w5-compliance is omitted, no w5_compliance detail appears."""

    def test_default_run_has_no_w5_detail(self):
        result = _run("--module=web-static", f"--app-path={_FIXTURE}")
        details = result.get("tests", {}).get("details", [])
        names = {d.get("name") for d in details}
        assert "w5_compliance" not in names

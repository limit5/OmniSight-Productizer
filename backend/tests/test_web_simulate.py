"""W2 #276 — integration test for `scripts/simulate.sh --type=web`.

Exercises the shell wrapper end-to-end (bash → python3
backend.web_simulator → JSON report) against the repo's built-in
`configs/web/fixtures/static-site` bundle. Follows the same pattern as
`test_hmi_simulate.py`: module-scoped subprocess, JSON parse, assert on
top-level fields.
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
_FIXTURE = _REPO / "configs" / "web" / "fixtures" / "static-site"


def _run_sim(*extra: str) -> dict:
    if not _SCRIPT.exists():
        pytest.skip("simulate.sh not present")
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    env = os.environ.copy()
    env["WORKSPACE"] = str(_REPO)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), "--type=web", *extra],
        env=env, cwd=_REPO, capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        raise


@pytest.fixture(scope="module")
def sim_static() -> dict:
    return _run_sim("--module=web-static")


@pytest.fixture(scope="module")
def sim_ssr() -> dict:
    return _run_sim("--module=web-ssr-node")


class TestFixtureIsValid:
    """The repo fixture should exist — if missing the whole W2 simulate
    track is unusable, so flag it explicitly instead of letting every
    test below fail opaquely."""

    def test_fixture_dir_exists(self):
        assert _FIXTURE.is_dir(), f"W2 fixture missing at {_FIXTURE}"

    def test_fixture_dist_has_index(self):
        assert (_FIXTURE / "dist" / "index.html").is_file()


class TestWebTrackStaticProfile:
    def test_track_label(self, sim_static):
        assert sim_static["track"] == "web"

    def test_profile_recorded(self, sim_static):
        assert sim_static["web"]["profile"] == "web-static"

    def test_bundle_budget_matches_profile(self, sim_static):
        # web-static → 500 KiB = 512000 B
        assert sim_static["web"]["bundle_budget_bytes"] == 500 * 1024

    def test_bundle_under_budget(self, sim_static):
        w = sim_static["web"]
        assert 0 < w["bundle_total_bytes"] <= w["bundle_budget_bytes"]

    def test_lighthouse_meets_baselines(self, sim_static):
        w = sim_static["web"]
        assert w["lighthouse_perf"] >= 80, w
        assert w["lighthouse_a11y"] >= 90, w
        assert w["lighthouse_seo"] >= 95, w

    def test_seo_clean(self, sim_static):
        assert sim_static["web"]["seo_issues"] == 0

    def test_a11y_clean(self, sim_static):
        assert sim_static["web"]["a11y_violations"] == 0

    def test_overall_gate_passes(self, sim_static):
        assert sim_static["status"] == "pass"
        assert sim_static["web"]["overall_pass"] is True

    def test_all_subtests_passed(self, sim_static):
        t = sim_static["tests"]
        assert t["failed"] == 0
        # driver + 8 gates = 9 subtests
        assert t["passed"] >= 9


class TestWebTrackSSRNodeProfile:
    """SSR-Node profile carries a 5 MiB budget — the same fixture must
    remain under it since 1.8 KiB << 5 MiB."""

    def test_profile_recorded(self, sim_ssr):
        assert sim_ssr["web"]["profile"] == "web-ssr-node"

    def test_bundle_budget_matches_profile(self, sim_ssr):
        assert sim_ssr["web"]["bundle_budget_bytes"] == 5 * 1024 * 1024

    def test_gate_passes(self, sim_ssr):
        assert sim_ssr["status"] == "pass"


class TestBudgetOverrideForcesFailure:
    """Setting --budget-override below the bundle size must make the
    gate fail. This guards the gate wiring — if some refactor makes the
    gate always pass, this test fires immediately."""

    @pytest.fixture(scope="class")
    def sim_tight(self):
        return _run_sim("--module=web-static", "--budget-override=500")

    def test_status_fail(self, sim_tight):
        assert sim_tight["status"] == "fail"

    def test_errors_include_bundle(self, sim_tight):
        msgs = " ".join(sim_tight.get("errors", []))
        assert "exceeds budget" in msgs

    def test_bundle_budget_is_override(self, sim_tight):
        assert sim_tight["web"]["bundle_budget_bytes"] == 500

    def test_overall_pass_false(self, sim_tight):
        assert sim_tight["web"]["overall_pass"] is False


class TestNonWebModuleFallsBackToStatic:
    """If a user passes --type=web with a non-web MODULE (say "core"),
    the shell should silently dispatch against `web-static` so the
    track never explodes on unexpected input — matches the behaviour
    documented in simulate.sh::run_web."""

    def test_fallback(self):
        sim = _run_sim("--module=core")
        assert sim["web"]["profile"] == "web-static"
        assert sim["status"] == "pass"


class TestUnknownTypeRejected:
    """Regression guard for the --type validation extension — ensures
    the W2 patch didn't accidentally loosen the allow-list."""

    def test_unknown_track_still_rejected(self):
        if not _SCRIPT.exists() or shutil.which("bash") is None:
            pytest.skip("simulate.sh / bash not available")
        env = os.environ.copy()
        env["WORKSPACE"] = str(_REPO)
        proc = subprocess.run(
            ["bash", str(_SCRIPT), "--type=definitelynot", "--module=x"],
            env=env, cwd=_REPO, capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode != 0
        data = json.loads(proc.stdout)
        assert data["status"] == "error"
        # Error message should now mention 'web' as allowed
        assert "web" in " ".join(data["errors"])

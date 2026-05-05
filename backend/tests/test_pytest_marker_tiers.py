"""BP.L regression tests for pytest marker tier aggregation and CI gates."""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import yaml

from tests.conftest import _BP_L_MARKER_TIERS, _bp_l_marker_for_path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_bp_l_marker_tier_file_sets_are_disjoint() -> None:
    seen: dict[str, str] = {}
    overlaps: list[tuple[str, str, str]] = []
    for marker_name, filenames in _BP_L_MARKER_TIERS.items():
        for filename in filenames:
            previous = seen.setdefault(filename, marker_name)
            if previous != marker_name:
                overlaps.append((filename, previous, marker_name))

    assert overlaps == []


def test_bp_l_marker_for_path_assigns_representative_files() -> None:
    assert _bp_l_marker_for_path("backend/tests/test_auth.py") == "critical"
    assert _bp_l_marker_for_path("backend/tests/test_skill_framework.py") == "guild_loadout"
    assert _bp_l_marker_for_path("backend/tests/test_compliance_harness.py") == "compliance"
    assert _bp_l_marker_for_path("backend/tests/test_nodes.py") is None


def test_bp_l_markers_registered_in_pytest_ini() -> None:
    parser = ConfigParser()
    parser.read(_REPO_ROOT / "backend" / "pytest.ini")
    marker_lines = parser.get("pytest", "markers").splitlines()
    registered = {line.split(":", 1)[0].strip() for line in marker_lines if ":" in line}

    assert {"critical", "guild_loadout", "compliance"} <= registered


def test_bp_l_ci_workflow_runs_three_ordered_marker_tiers() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert jobs["backend-critical"]["timeout-minutes"] == 5
    assert jobs["backend-loadout"]["timeout-minutes"] == 30
    assert jobs["backend-compliance"]["timeout-minutes"] == 60
    assert jobs["backend-loadout"]["needs"] == "backend-critical"
    assert jobs["backend-compliance"]["needs"] == "backend-loadout"

    expected_markers = {
        "backend-critical": "-m critical",
        "backend-loadout": "-m guild_loadout",
        "backend-compliance": "-m compliance",
    }
    for job_name, marker_arg in expected_markers.items():
        run_blocks = [
            step.get("run", "")
            for step in jobs[job_name]["steps"]
            if step.get("name", "").startswith("pytest ")
        ]
        assert len(run_blocks) == 1
        assert "python3 -m pytest backend/tests/" in run_blocks[0]
        assert "--rootdir=backend" in run_blocks[0]
        assert "-c backend/pytest.ini" in run_blocks[0]
        assert marker_arg in run_blocks[0]


def test_bp_l_ci_backend_tests_uses_eight_coverage_shards() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    shards = jobs["backend-tests"]["strategy"]["matrix"]["shard"]
    assert [shard["name"] for shard in shards] == [
        "decision",
        "pipeline",
        "schema",
        "auth-security",
        "runtime",
        "product",
        "infra",
        "rest",
    ]

    assert jobs["backend-coverage-combine"]["needs"] == "backend-tests"
    for shard in shards:
        assert shard["paths"]
        assert isinstance(shard["min"], int)
        for path_token in shard["paths"].split():
            if "*" in path_token:
                assert list(_REPO_ROOT.glob(path_token)), path_token
            elif path_token.endswith("/"):
                assert (_REPO_ROOT / path_token).is_dir(), path_token
            else:
                assert (_REPO_ROOT / path_token).is_file(), path_token


def test_bp_l_ci_coverage_gate_counts_new_backend_modules() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    parser = ConfigParser()
    parser.read(_REPO_ROOT / "backend" / "pytest.ini")

    assert parser.get("coverage:run", "source").strip() == "backend"
    omitted = {
        line.strip()
        for line in parser.get("coverage:run", "omit").splitlines()
        if line.strip()
    }
    assert "backend/*.py" not in omitted

    combine_steps = jobs["backend-coverage-combine"]["steps"]
    combine_runs = [
        step.get("run", "")
        for step in combine_steps
        if step.get("name") == "combine + report"
    ]
    assert len(combine_runs) == 1
    combine_run = combine_runs[0]

    assert "python3 -m coverage combine .coverage.*" in combine_run
    assert "python3 -m coverage report --rcfile=backend/pytest.ini --fail-under=60" in combine_run
    assert "python3 -m coverage xml -o coverage-combined.xml --rcfile=backend/pytest.ini" in combine_run

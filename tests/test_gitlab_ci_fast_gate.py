"""OP-1568 / RT-04a - develop submit-gate CI contract.

The fast gate is deliberately per-change: it runs on develop push
pipelines keyed by the submitted SHA, while release/candidate
certification remains out of scope.
"""
from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"


def _load() -> dict:
    return yaml.safe_load(GITLAB_CI.read_text())


def _flatten_script(job: dict) -> str:
    parts: list[str] = []
    for key in ("before_script", "script"):
        block = job.get(key) or []
        if isinstance(block, str):
            parts.append(block)
        else:
            parts.extend(block)
    return "\n".join(parts)


def test_workflow_allows_develop_push_fast_gate_without_release_jobs() -> None:
    ci = _load()
    workflow_rules = ci["workflow"]["rules"]
    develop_push_rule = {
        "if": '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == "develop"'
    }
    assert develop_push_rule in workflow_rules

    job = ci["fast-gate"]
    assert job["stage"] == "gate"
    assert job["extends"] == [".fast_gate_rules"]
    assert ci[".fast_gate_rules"]["rules"] == [develop_push_rule]


def test_fast_gate_is_keyed_by_full_commit_sha_artifacts() -> None:
    ci = _load()
    job = ci["fast-gate"]
    flat = _flatten_script(job)

    assert 'test "$CI_COMMIT_BRANCH" = "develop"' in flat
    assert 'test "${#CI_COMMIT_SHA}" = "40"' in flat
    assert "FAST_GATE_SHA=%s" in flat
    assert "FAST_GATE_BASE_SHA=%s" in flat
    assert job["artifacts"]["name"] == "fast-gate-${CI_COMMIT_SHA}"
    assert job["artifacts"]["reports"]["dotenv"] == "fast-gate.env"


def test_fast_gate_runs_required_fast_checks() -> None:
    ci = _load()
    flat = _flatten_script(ci["fast-gate"])

    for required in (
        "ruff check",
        "scripts/ci_test_impact.py",
        "backend/.venv/bin/python -m pytest --no-cov -q $TEST_FILES",
        "pnpm exec tsc --noEmit",
        "pnpm run build",
        "scripts/check_migration_syntax.py --strict --only",
        "cd backend && .venv/bin/alembic -c alembic.ini heads",
    ):
        assert required in flat


def test_fast_gate_broken_change_goes_red_at_submit() -> None:
    ci = _load()
    flat = _flatten_script(ci["fast-gate"])

    # These are the failure exits a deliberately broken develop submit hits:
    # Python lint/test failures, FE type/build failures, changed-migration
    # syntax failures, or multiple Alembic heads.
    assert "set -euo pipefail" in flat
    assert "xargs backend/.venv/bin/ruff check" in flat
    assert "backend/.venv/bin/python -m pytest --no-cov -q" in flat
    assert "pnpm exec tsc --noEmit" in flat
    assert "pnpm run build" in flat
    assert "exit 1" in flat
    assert "FAIL: expected exactly one Alembic head" in flat


def test_fast_gate_does_not_define_candidate_certification() -> None:
    ci = _load()
    job_names = {name for name, value in ci.items() if isinstance(value, dict)}
    gate_job_names = {name for name in job_names if "gate" in name}

    assert gate_job_names == {".fast_gate_rules", "fast-gate"}
    flat = _flatten_script(ci["fast-gate"])
    assert "CANDIDATE_SHA" not in flat
    assert "green_status" not in flat
    assert "sha-${CI_COMMIT_SHORT_SHA}" not in flat

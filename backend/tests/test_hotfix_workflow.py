"""OP-775 hotfix fast-track workflow tests."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_SCRIPT = REPO_ROOT / "scripts" / "hotfix_pipeline.py"
CHERRY_PICK_SCRIPT = REPO_ROOT / "scripts" / "cherry_pick_hotfix.py"
RUNBOOK = REPO_ROOT / "docs" / "runbook" / "hotfix-workflow.md"


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hotfix_pipeline = _load_script(PIPELINE_SCRIPT, "hotfix_pipeline_under_test")
cherry_pick_hotfix = _load_script(CHERRY_PICK_SCRIPT, "cherry_pick_hotfix_under_test")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "OP-775 Test",
            "GIT_AUTHOR_EMAIL": "op-775@example.test",
            "GIT_COMMITTER_NAME": "OP-775 Test",
            "GIT_COMMITTER_EMAIL": "op-775@example.test",
        },
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, name: str, body: str, message: str) -> str:
    path = repo / name
    path.write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_hotfix_label_and_tier_s_trigger_fast_track_pipeline() -> None:
    decision = hotfix_pipeline.plan_hotfix(
        ticket_key="OP-775",
        labels=("hotfix:v1.0.1", "tier:S", "area:devops"),
        smoke_status="green",
        critical_slo_status="green",
        operator_approved=True,
    )

    assert decision.event == "hotfix_fast_track_ready"
    assert decision.fast_track is True
    assert decision.hotfix_version == "v1.0.1"
    assert decision.skipped_gates == (
        "milestone_wait",
        "metric_baseline_observation",
        "canary_5_percent",
    )


def test_cherry_pick_script_handles_single_commit_hotfix_and_tags_patch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _commit_file(repo, "app.txt", "base\n", "base")
    _git(repo, "checkout", "-b", "release/v1.0")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _git(repo, "checkout", "develop")
    hotfix_sha = _commit_file(repo, "fix.txt", "hotfix\n", "OP-775 hotfix")

    proc = subprocess.run(
        [
            sys.executable,
            str(CHERRY_PICK_SCRIPT),
            hotfix_sha,
            "--to",
            "release/v1.0",
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["source_tag"] == "v1.0.0"
    assert payload["next_tag"] == "v1.0.1"
    assert _git(repo, "branch", "--show-current") == "release/v1.0"
    assert (repo / "fix.txt").read_text(encoding="utf-8") == "hotfix\n"
    assert _git(repo, "rev-parse", "v1.0.1^{commit}") == _git(repo, "rev-parse", "HEAD")


def test_patch_tag_auto_increment_is_semver_patch_only() -> None:
    assert cherry_pick_hotfix.increment_patch_tag("v1.2.0") == "v1.2.1"
    assert cherry_pick_hotfix.increment_patch_tag("v1.2.9") == "v1.2.10"


def test_operator_approval_still_blocks_prod_deploy() -> None:
    decision = hotfix_pipeline.plan_hotfix(
        ticket_key="OP-775",
        labels=("hotfix:v1.0.1", "tier:S"),
        smoke_status="green",
        critical_slo_status="green",
        operator_approved=False,
    )

    assert decision.event == "hotfix_fast_track_blocked"
    assert decision.operator_approval_required is True
    assert {"gate": "operator_approval", "code": "approval_required"} in decision.blocked_reasons


def test_synthetic_hotfix_v1_0_0_to_v1_0_1_deploys_under_30_minutes() -> None:
    decision = hotfix_pipeline.plan_hotfix(
        ticket_key="OP-775",
        labels=("hotfix:v1.0.1", "tier:S"),
        smoke_status="green",
        critical_slo_status="green",
        operator_approved=True,
    )

    assert decision.canary_stages == (25, 100)
    assert 5 not in decision.canary_stages
    assert decision.synthetic_budget_minutes == 29
    assert decision.synthetic_budget_minutes < 30


def test_hotfix_runbook_documents_fast_path_contract() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "hotfix:vX.Y.Z" in text
    assert "tier:S" in text
    assert "scripts/cherry_pick_hotfix.py <commit-sha> --to release/vX.Y" in text
    assert "25% -> 100%" in text
    assert "operator approval" in text.lower()

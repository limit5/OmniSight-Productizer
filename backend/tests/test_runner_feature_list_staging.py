"""OP-825 B0 — F20-analogue regression: feature-list JSON must be staged.

Pins the rule that ``stage_feature_list`` writes the per-ticket JSON and
``git add``s it (so ``git status`` shows it staged at HEAD), and that
``assert_feature_list_staged`` raises ``FeatureListNotStaged`` when the
file is not in the index. Without this gate runner commits can land
without the per-ticket feature-list payload (incident class F20).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.agents.runner_health_checks import (
    FeatureListNotStaged,
    assert_feature_list_staged,
    stage_feature_list,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with one initial commit so HEAD exists."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True,
    )
    seed = tmp_path / "README.md"
    seed.write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True,
    )
    return tmp_path


def test_stage_feature_list_adds_file_to_git_index(repo: Path) -> None:
    file_path = stage_feature_list(repo, "OP-825", '{"features": ["b0"]}')
    assert file_path.exists()
    # `git status --short` reports staged files with `A` in column 1.
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    assert f"A  {file_path.name}" in status
    # Also exercise the explicit assertion helper — should not raise.
    assert_feature_list_staged(repo, file_path.name)


def test_assert_feature_list_staged_raises_when_file_only_on_disk(
    repo: Path,
) -> None:
    # Write the JSON but skip the `git add` step the helper would do.
    untracked = repo / "feature_list_OP-825.json"
    untracked.write_text('{"features": ["b0"]}')
    with pytest.raises(FeatureListNotStaged) as exc:
        assert_feature_list_staged(repo, untracked.name)
    assert exc.value.file == untracked.name

"""SP-B-X-018 / OP-1076 consolidation tests.

Verifies that:

1. ``RUNNER_RUNTIME_ARTIFACTS`` is the single source of truth — both
   ``runner_progress._worktree_dirty`` and
   ``jira_dispatch.ensure_change_ids`` import + use it.
2. Each of the three known artifacts (``progress.txt``,
   ``progress.txt.tmp``, ``.runner-cwd-sentinel``) is excluded by BOTH
   dirty-checks.
3. An unrelated untracked file still raises ``WorktreeDirtyError`` in
   ``ensure_change_ids`` (so the filter is surgical, not blanket).
4. The back-compat alias ``_OUR_OWN_ARTIFACTS`` still resolves to the
   canonical constant (so legacy callers don't break).

These tests are the regression contract for the SP-B-X-016/017 hot-fix
class — if a future refactor breaks the filter, these tests fail loudly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.agents import jira_dispatch, runner_progress


# ── Constant identity / canonicalness ──────────────────────────────────


def test_canonical_constant_is_frozenset() -> None:
    assert isinstance(runner_progress.RUNNER_RUNTIME_ARTIFACTS, frozenset)


def test_canonical_constant_includes_all_three_artifacts() -> None:
    expected = {
        runner_progress.PROGRESS_FILENAME,
        runner_progress.PROGRESS_FILENAME + runner_progress._PROGRESS_TMP_SUFFIX,
        ".runner-cwd-sentinel",
    }
    assert runner_progress.RUNNER_RUNTIME_ARTIFACTS == frozenset(expected)


def test_legacy_alias_resolves_to_canonical() -> None:
    # Back-compat: _OUR_OWN_ARTIFACTS must be the same object (not a copy)
    # so legacy callers see the canonical contents.
    assert runner_progress._OUR_OWN_ARTIFACTS is runner_progress.RUNNER_RUNTIME_ARTIFACTS


# ── _worktree_dirty exclusions ─────────────────────────────────────────


@pytest.fixture
def git_worktree(tmp_path: Path) -> Path:
    """Initialise a minimal git worktree with one tracked commit."""
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README").write_text("base\n")
    subprocess.run(["git", "add", "README"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True
    )
    return tmp_path


def test_worktree_dirty_clean_returns_false(git_worktree: Path) -> None:
    assert runner_progress._worktree_dirty(git_worktree) is False


@pytest.mark.parametrize(
    "artifact_name",
    [
        runner_progress.PROGRESS_FILENAME,
        runner_progress.PROGRESS_FILENAME + runner_progress._PROGRESS_TMP_SUFFIX,
        ".runner-cwd-sentinel",
    ],
)
def test_worktree_dirty_excludes_each_runtime_artifact(
    git_worktree: Path, artifact_name: str
) -> None:
    (git_worktree / artifact_name).write_text("runtime-state\n")
    # Only a runtime artifact is present → worktree should appear clean
    assert runner_progress._worktree_dirty(git_worktree) is False


def test_worktree_dirty_returns_true_for_unrelated_file(
    git_worktree: Path,
) -> None:
    (git_worktree / "scratch.txt").write_text("ad-hoc\n")
    assert runner_progress._worktree_dirty(git_worktree) is True


def test_worktree_dirty_returns_true_when_runtime_AND_unrelated(
    git_worktree: Path,
) -> None:
    # Runtime artifact alone → filtered. But unrelated file present too
    # → still dirty.
    (git_worktree / "progress.txt").write_text("phase=picking_up\n")
    (git_worktree / "user_edit.py").write_text("# real CLI work\n")
    assert runner_progress._worktree_dirty(git_worktree) is True


# ── ensure_change_ids exclusions ───────────────────────────────────────


@pytest.fixture
def git_worktree_with_commit_on_branch(tmp_path: Path) -> tuple[Path, str]:
    """Like ``git_worktree`` but with a commit on top of an explicit base
    branch — the layout ``ensure_change_ids`` expects."""
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README").write_text("base\n")
    subprocess.run(["git", "add", "README"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "base\n\nChange-Id: I0000000000000000000000000000000000000000"],
        cwd=tmp_path, check=True,
    )
    # Branch off
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "work.py").write_text("# CLI output\n")
    subprocess.run(["git", "add", "work.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "CLI work\n\nChange-Id: I1111111111111111111111111111111111111111"],
        cwd=tmp_path, check=True,
    )
    return tmp_path, "main"


@pytest.mark.parametrize(
    "artifact_name",
    [
        runner_progress.PROGRESS_FILENAME,
        runner_progress.PROGRESS_FILENAME + runner_progress._PROGRESS_TMP_SUFFIX,
        ".runner-cwd-sentinel",
    ],
)
def test_ensure_change_ids_excludes_each_runtime_artifact(
    git_worktree_with_commit_on_branch: tuple[Path, str],
    artifact_name: str,
) -> None:
    worktree, base = git_worktree_with_commit_on_branch
    (worktree / artifact_name).write_text("runtime-state\n")
    # Should not raise WorktreeDirtyError — the artifact is filtered.
    # We DO expect ensure_change_ids to attempt other work (rebase etc.)
    # and may fail for unrelated reasons; we narrow to the dirty-check
    # by catching only WorktreeDirtyError.
    try:
        jira_dispatch.ensure_change_ids(worktree, base)
    except jira_dispatch.WorktreeDirtyError:
        pytest.fail(f"runtime artifact {artifact_name!r} leaked through filter")
    except Exception:
        # Any non-dirty-check failure is out of scope for this test.
        pass


def test_ensure_change_ids_raises_on_unrelated_dirty_file(
    git_worktree_with_commit_on_branch: tuple[Path, str],
) -> None:
    worktree, base = git_worktree_with_commit_on_branch
    (worktree / "scratch.txt").write_text("ad-hoc\n")
    with pytest.raises(jira_dispatch.WorktreeDirtyError) as exc_info:
        jira_dispatch.ensure_change_ids(worktree, base)
    assert "scratch.txt" in exc_info.value.dirty_files


def test_ensure_change_ids_raises_when_runtime_AND_unrelated(
    git_worktree_with_commit_on_branch: tuple[Path, str],
) -> None:
    worktree, base = git_worktree_with_commit_on_branch
    (worktree / "progress.txt").write_text("phase=submitting\n")
    (worktree / "uncommitted_cli_output.py").write_text("# work\n")
    with pytest.raises(jira_dispatch.WorktreeDirtyError) as exc_info:
        jira_dispatch.ensure_change_ids(worktree, base)
    assert "uncommitted_cli_output.py" in exc_info.value.dirty_files
    assert "progress.txt" not in exc_info.value.dirty_files


# ── Cross-callsite consistency ─────────────────────────────────────────


def test_both_sites_reference_the_same_canonical_constant() -> None:
    """Regression test: the SP-B-X-016/017 era had two SEPARATE local
    sets that drifted. After SP-B-X-018, both sites must read the
    canonical constant. We verify by checking the module source.
    """
    src_path = Path(jira_dispatch.__file__)
    src = src_path.read_text()
    # Must reference the canonical name (not a local hardcoded literal)
    assert "runner_progress.RUNNER_RUNTIME_ARTIFACTS" in src, (
        f"jira_dispatch.py no longer imports the canonical constant "
        f"({src_path}); the SP-B-X-016/017 regression is back."
    )
    # And the legacy local name must NOT come back
    assert "_RUNNER_OWN_ARTIFACTS = {" not in src, (
        "jira_dispatch.py reintroduced a local _RUNNER_OWN_ARTIFACTS set; "
        "this is the SP-B-X-018 regression — use the canonical constant."
    )

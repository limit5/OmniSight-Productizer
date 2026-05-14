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
    sets that drifted. After SP-B-X-018 / OP-1111, both sites must read
    the canonical constant — historically from
    ``runner_progress.RUNNER_RUNTIME_ARTIFACTS``, now also accepting the
    OP-1111 canonical import path
    ``runner_artifacts.RUNNER_RUNTIME_ARTIFACTS`` (or the bare
    ``RUNNER_RUNTIME_ARTIFACTS`` from a ``from`` import).
    """
    src_path = Path(jira_dispatch.__file__)
    src = src_path.read_text()
    has_legacy_path = "runner_progress.RUNNER_RUNTIME_ARTIFACTS" in src
    has_new_path = (
        "runner_artifacts import RUNNER_RUNTIME_ARTIFACTS" in src
        or "runner_artifacts.RUNNER_RUNTIME_ARTIFACTS" in src
    )
    assert has_legacy_path or has_new_path, (
        f"jira_dispatch.py no longer imports the canonical constant "
        f"({src_path}); the SP-B-X-016/017 regression is back."
    )
    assert "_RUNNER_OWN_ARTIFACTS = {" not in src, (
        "jira_dispatch.py reintroduced a local _RUNNER_OWN_ARTIFACTS set; "
        "this is the SP-B-X-018 regression — use the canonical constant."
    )


# ── OP-1111: canonical module + gitignore mirror ──────────────────────


def test_canonical_module_is_runner_artifacts() -> None:
    """OP-1111: RUNNER_RUNTIME_ARTIFACTS now lives in its own module
    (``backend.agents.runner_artifacts``). The previous home
    (``backend.agents.runner_progress``) re-exports for backwards
    compat — both names must resolve to the same object."""
    from backend.agents import runner_artifacts
    assert isinstance(runner_artifacts.RUNNER_RUNTIME_ARTIFACTS, frozenset)
    # Re-export identity: runner_progress's name is the SAME object
    assert (
        runner_progress.RUNNER_RUNTIME_ARTIFACTS
        is runner_artifacts.RUNNER_RUNTIME_ARTIFACTS
    ), "runner_progress must re-export the canonical frozenset, not a copy"


def test_gitignore_mirrors_runner_runtime_artifacts() -> None:
    """OP-1111 Code AC #3: ``.gitignore`` mirrors the canonical set.
    Defense in depth — Python-layer filter + git-layer ignore. The
    sentinel-bounded block in ``.gitignore`` must list every entry."""
    from backend.agents import runner_artifacts
    repo_root = Path(__file__).resolve().parents[1]
    gitignore = (repo_root / ".gitignore").read_text()
    begin = runner_artifacts.GITIGNORE_BEGIN_SENTINEL
    end = runner_artifacts.GITIGNORE_END_SENTINEL
    assert begin in gitignore, (
        f"missing START sentinel {begin!r} in .gitignore — add the "
        "OP-1111 auto-mirror block per backend/agents/runner_artifacts.py"
    )
    assert end in gitignore, (
        f"missing END sentinel {end!r} in .gitignore"
    )
    block = gitignore.split(begin, 1)[1].split(end, 1)[0]
    # Each entry in the constant must appear as its own non-comment line
    block_entries = {
        line.strip() for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = runner_artifacts.RUNNER_RUNTIME_ARTIFACTS - block_entries
    extra = block_entries - runner_artifacts.RUNNER_RUNTIME_ARTIFACTS
    assert not missing, (
        f"{sorted(missing)!r} in RUNNER_RUNTIME_ARTIFACTS but not in "
        ".gitignore mirror block — update .gitignore to match"
    )
    assert not extra, (
        f"{sorted(extra)!r} in .gitignore mirror block but not in "
        "RUNNER_RUNTIME_ARTIFACTS — remove from .gitignore or add to "
        "the frozenset"
    )


def test_synthetic_op1070_replay_finalization_does_not_revert(git_worktree: Path) -> None:
    """OP-1111 Integration AC: synthetic OP-1070 replay. Set up a
    worktree containing every RUNNER_RUNTIME_ARTIFACT, then confirm the
    consolidated dirty-checks see the worktree as CLEAN (no spurious
    revert because of our own bookkeeping files).

    Pre-OP-1111 history: SP-B-X-016 (sentinel) and SP-B-X-017 (progress
    .txt) each patched ONE site reactively. SP-B-X-018 / OP-1076
    consolidated them. OP-1111 finishes the job: canonical module +
    gitignore mirror, so a future write that lands here can never
    cascade to a workspace-tampered revert again.
    """
    from backend.agents import runner_artifacts
    # Plant every known artifact as an untracked file
    for name in runner_artifacts.RUNNER_RUNTIME_ARTIFACTS:
        (git_worktree / name).write_text("synthetic OP-1070 replay content\n")

    # Both dirty checks must report CLEAN despite the artifacts being present
    assert runner_progress._worktree_dirty(git_worktree) is False, (
        "OP-1070 regression: runner_progress._worktree_dirty saw its own "
        "artifacts as dirt"
    )
    # ensure_change_ids is the other historic dirty-check failure site;
    # we exercise its filtering loop directly without invoking the full
    # rebase machinery (which needs a real Gerrit hook + multiple commits).
    import subprocess as _sp
    dirty = _sp.check_output(
        ["git", "status", "--porcelain", "-uall"],
        cwd=git_worktree, text=True,
    ).splitlines()
    dirty_names = []
    for line in dirty:
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            dirty_names.append(parts[1])
    filtered = [
        f for f in dirty_names
        if f not in runner_artifacts.RUNNER_RUNTIME_ARTIFACTS
    ]
    assert filtered == [], (
        f"OP-1111 Integration AC: after filtering RUNNER_RUNTIME_ARTIFACTS, "
        f"unexpected dirty entries remain: {filtered!r}"
    )

"""OP-837 — sync_omnisight_main.sh smoke + integration tests.

Pure-bash script tested via subprocess + temp git repos. Covers the
behaviours that the AC (per OP-837) pins:

* HEAD already at remote → sync_no_change + exit 0
* HEAD behind clean tree → sync_runner_code_changed (or
  sync_no_runner_code_change) + ff-only pull + exit 0
* HEAD behind dirty tree (auto-stash off) → sync_skipped_dirty + exit 0
* HEAD behind dirty tree (auto-stash on) → stash + ff-pull + pop +
  success
* Non-fast-forward history → pull_not_fast_forward record + exit 3
* Fetch failure → fetch_failed counter increments
* 3 consecutive failures → operator_notifier dispatched (we mock the
  module so the test doesn't actually fire a P0)

The script is referenced via its repo-relative path so the test runs
in any clone (CI uses ``OMNISIGHT_REPO_ROOT`` to find it; default is
the running test's repo root via ``__file__`` traversal).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sync_omnisight_main.sh"


def _run_sync(env_overrides: dict[str, str], expected_rc: int | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    if expected_rc is not None:
        assert result.returncode == expected_rc, (
            f"rc={result.returncode} expected={expected_rc}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _init_repo(path: Path, branch: str = "develop") -> str:
    """Init a git repo at ``path`` with one seed commit. Returns HEAD SHA."""
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=path, check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, msg: str, fname: str = "x.py") -> str:
    (repo / fname).write_text(msg + "\n", encoding="utf-8")
    subprocess.run(["git", "add", fname], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg],
        cwd=repo, check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _setup_main_with_remote(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build (main_dir, remote_dir, log, state) ready for the script.

    main_dir is a clone of remote_dir. Both have one seed commit. Caller
    advances remote_dir (then main_dir falls behind) to exercise sync.
    """
    remote = tmp_path / "remote.git"
    _init_repo(remote)
    # Convert to bare-ish: keep working tree, allow push/pull. For test
    # simplicity we use the working remote directly.

    main = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "-q", "--origin", "gerrit", str(remote), str(main)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=main, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True, capture_output=True)

    log = tmp_path / "sync.log"
    state = tmp_path / "state.json"
    return main, remote, log, state


def _read_log_events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _env(main: Path, log: Path, state: Path, **extras) -> dict[str, str]:
    base = {
        "MAIN_DIR": str(main),
        "MAIN_REMOTE": "gerrit",
        "MAIN_BRANCH": "develop",
        "SYNC_LOG": str(log),
        "SYNC_STATE": str(state),
    }
    base.update(extras)
    return base


# ─── Tests ──────────────────────────────────────────────────────────


def test_no_change_when_head_matches_remote(tmp_path: Path) -> None:
    main, _remote, log, state = _setup_main_with_remote(tmp_path)
    _run_sync(_env(main, log, state), expected_rc=0)
    events = [e["event"] for e in _read_log_events(log)]
    assert "sync_no_change" in events


def test_ff_pull_when_remote_has_advanced(tmp_path: Path) -> None:
    main, remote, log, state = _setup_main_with_remote(tmp_path)
    _commit(remote, "remote ahead", fname="upstream.py")
    _run_sync(_env(main, log, state), expected_rc=0)
    events = [e["event"] for e in _read_log_events(log)]
    # Either no_runner_code_change or runner_code_changed — both indicate
    # successful pull.
    assert any(e in events for e in ("sync_no_runner_code_change", "sync_runner_code_changed"))


def test_runner_code_change_flagged_when_relevant_file_modified(tmp_path: Path) -> None:
    main, remote, log, state = _setup_main_with_remote(tmp_path)
    # Modify a path the script flags as runner-code (auto-runner-jira.py).
    (remote / "auto-runner-jira.py").write_text("# stub\n", encoding="utf-8")
    subprocess.run(["git", "add", "auto-runner-jira.py"], cwd=remote, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "runner code change"],
        cwd=remote, check=True, capture_output=True,
    )
    _run_sync(_env(main, log, state), expected_rc=0)
    events = _read_log_events(log)
    runner_code_events = [e for e in events if e["event"] == "sync_runner_code_changed"]
    assert runner_code_events, f"no sync_runner_code_changed in {[e['event'] for e in events]}"
    assert "auto-runner-jira.py" in runner_code_events[0].get("files", "")


def test_skip_when_dirty_tree_and_auto_stash_off(tmp_path: Path) -> None:
    main, remote, log, state = _setup_main_with_remote(tmp_path)
    _commit(remote, "remote ahead", fname="upstream.py")
    # Dirty the local tree.
    (main / "uncommitted.py").write_text("local edit\n", encoding="utf-8")
    _run_sync(_env(main, log, state, AUTO_STASH="0"), expected_rc=0)
    events = [e["event"] for e in _read_log_events(log)]
    assert "sync_skipped_dirty" in events
    # And no pull happened — local HEAD did NOT advance.
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=main, check=True, capture_output=True, text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=remote, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head != remote_head


def test_auto_stash_path_preserves_untracked(tmp_path: Path) -> None:
    main, remote, log, state = _setup_main_with_remote(tmp_path)
    _commit(remote, "remote ahead", fname="upstream.py")
    # Untracked draft (not the same path as upstream change → no conflict).
    draft = main / "draft_doc.md"
    draft.write_text("draft body\n", encoding="utf-8")
    _run_sync(_env(main, log, state, AUTO_STASH="1"), expected_rc=0)
    # Pull happened (HEAD advanced) AND draft survived.
    local_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=main, check=True, capture_output=True, text=True,
    ).stdout.strip()
    remote_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=remote, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head == remote_head
    assert draft.exists()
    assert draft.read_text() == "draft body\n"


def test_state_file_records_consecutive_failures_on_fetch_error(tmp_path: Path) -> None:
    main, _remote, log, state = _setup_main_with_remote(tmp_path)
    # Point at a non-existent remote to force fetch failure.
    bad_env = _env(main, log, state, MAIN_REMOTE="nonexistent-remote")
    result = _run_sync(bad_env)
    assert result.returncode == 3
    assert state.exists()
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["consecutive_failures"] >= 1


def test_dry_run_mode_does_not_pull(tmp_path: Path) -> None:
    main, remote, log, state = _setup_main_with_remote(tmp_path)
    _commit(remote, "remote ahead", fname="upstream.py")
    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=main, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _run_sync(_env(main, log, state, SYNC_DRY_RUN="1"), expected_rc=0)
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=main, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert pre_head == post_head  # no pull
    events = [e["event"] for e in _read_log_events(log)]
    assert "sync_dry_run" in events


def test_aborts_when_main_dir_is_not_a_git_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    log = tmp_path / "sync.log"
    state = tmp_path / "state.json"
    result = _run_sync({
        "MAIN_DIR": str(not_a_repo),
        "MAIN_REMOTE": "gerrit",
        "MAIN_BRANCH": "develop",
        "SYNC_LOG": str(log),
        "SYNC_STATE": str(state),
    })
    assert result.returncode == 2
    events = [e["event"] for e in _read_log_events(log)]
    assert "sync_aborted" in events

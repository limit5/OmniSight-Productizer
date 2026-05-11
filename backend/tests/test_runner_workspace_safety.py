"""OP-836 — Runner workspace safety tests.

L1 prevention for the OP-811/813/832/835 wedge family. The CLI must not be
able to commit into the main repo's working tree; this module enforces that
via two layered checks (perm-isolation + sentinel verification).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.agents import runner_workspace_safety as wss


# ─── Helpers ────────────────────────────────────────────────────────


def _init_main_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a main repo + linked worktree under ``tmp_path``. Returns
    ``(main_repo, worktree)``. Both are real git working trees sharing one
    common dir."""
    main = tmp_path / "main_repo"
    main.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "develop"],
        cwd=main, check=True, capture_output=True,
    )
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=main, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True, capture_output=True)
    (main / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=main, check=True, capture_output=True,
    )
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/test", str(worktree)],
        cwd=main, check=True, capture_output=True,
    )
    return main, worktree


def _commit(worktree: Path, msg: str, fname: str = "x.py") -> str:
    (worktree / fname).write_text(msg + "\n", encoding="utf-8")
    subprocess.run(["git", "add", fname], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg],
        cwd=worktree, check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()


# ─── assert_main_repo_unwritable_for_cli ───────────────────────────


def test_assert_no_op_when_enforce_unset(tmp_path, monkeypatch):
    """Default off — dev environments where operator owns the main repo
    should NOT trip the guard. Operator must opt in via env var."""
    monkeypatch.delenv(wss.ENV_ENFORCE, raising=False)
    main, worktree = _init_main_and_worktree(tmp_path)
    # Main repo is writable (test fixture owns it) — but enforce is off.
    wss.assert_main_repo_unwritable_for_cli(worktree)  # no raise


def test_assert_raises_when_main_repo_writable_and_enforce_on(tmp_path, monkeypatch):
    """When ENV_ENFORCE=1 and the main repo is writable to the launching
    user, the guard MUST raise — that's the wedge-prevention contract."""
    monkeypatch.setenv(wss.ENV_ENFORCE, "1")
    main, worktree = _init_main_and_worktree(tmp_path)
    with pytest.raises(wss.MainRepoWritableInLaunchEnvError) as ei:
        wss.assert_main_repo_unwritable_for_cli(worktree)
    err = ei.value
    assert err.main_repo == main
    assert err.worktree == worktree.resolve()
    # writable_path is the actual path that failed perms — main_repo or .git
    assert err.writable_path in (main, main / ".git")


def test_assert_passes_when_main_repo_readonly_and_enforce_on(tmp_path, monkeypatch):
    """Production-shaped env: main repo chmod'd read-only for the runner.
    Guard MUST pass — that's the configuration this guard is designed for."""
    monkeypatch.setenv(wss.ENV_ENFORCE, "1")
    main, worktree = _init_main_and_worktree(tmp_path)
    # Strip writable bit from main repo + its .git ref. We restore in
    # cleanup so pytest's tmp_path collection succeeds.
    paths = [main, main / ".git"]
    saved = [(p, p.stat().st_mode) for p in paths if p.exists()]
    try:
        for p, _mode in saved:
            p.chmod(0o555)  # r-x for owner
        wss.assert_main_repo_unwritable_for_cli(worktree)
    finally:
        for p, mode in saved:
            p.chmod(mode)


def test_assert_raises_when_worktree_is_main_repo(tmp_path, monkeypatch):
    """Loud failure if the runner is launched IN the main repo cwd
    (worktree path == main repo path) — the CLI would have nowhere safe to
    write."""
    monkeypatch.setenv(wss.ENV_ENFORCE, "1")
    main, _worktree = _init_main_and_worktree(tmp_path)
    with pytest.raises(wss.MainRepoWritableInLaunchEnvError) as ei:
        wss.assert_main_repo_unwritable_for_cli(main)
    assert ei.value.writable_path == main.resolve()


# ─── write_workspace_sentinel + verify_workspace_sentinel ──────────


def test_sentinel_roundtrip_when_cli_advanced_head_cleanly(tmp_path):
    """Happy path: CLI added one commit, sentinel exists + HEAD is a
    descendant of the pre-launch SHA → verification passes silently."""
    _, worktree = _init_main_and_worktree(tmp_path)
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    assert sentinel.exists()
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    assert payload["ticket_key"] == "OP-TEST"
    pre_sha = payload["head_sha"]

    new_sha = _commit(worktree, "feature work", fname="feature.py")
    assert new_sha != pre_sha

    # No raise; payload returned for audit log.
    out = wss.verify_workspace_sentinel(sentinel, worktree)
    assert out["head_sha"] == pre_sha
    assert out["ticket_key"] == "OP-TEST"


def test_sentinel_no_op_when_head_unchanged(tmp_path):
    """No commits made — sentinel verify still passes (caller's
    NoCommitsOnBranchError path handles the wedge)."""
    _, worktree = _init_main_and_worktree(tmp_path)
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    out = wss.verify_workspace_sentinel(sentinel, worktree)
    assert out["ticket_key"] == "OP-TEST"


def test_sentinel_raises_when_deleted_by_cli(tmp_path):
    """CLI blew away the sentinel — fail loudly so the runner knows the
    workspace was tampered with."""
    _, worktree = _init_main_and_worktree(tmp_path)
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    sentinel.unlink()
    with pytest.raises(wss.WorkspaceTamperedError) as ei:
        wss.verify_workspace_sentinel(sentinel, worktree)
    assert "deleted" in ei.value.reason


def test_sentinel_raises_when_head_regressed_to_non_descendant(tmp_path):
    """CLI reset HEAD to a non-descendant SHA (commit on a divergent branch
    that doesn't share history with the sentinel SHA). The post-CLI HEAD is
    no longer a descendant of the pre-launch HEAD → fail."""
    _, worktree = _init_main_and_worktree(tmp_path)
    # Make a commit, take sentinel, then create a divergent branch from
    # before the sentinel and check that out — the new HEAD is unrelated to
    # sentinel SHA.
    pre_sentinel_sha = _commit(worktree, "first feature", fname="a.py")
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    _commit(worktree, "advanced past sentinel", fname="b.py")
    # Reset HEAD to before the sentinel SHA — non-descendant relationship.
    parent_of_sentinel_sha = subprocess.run(
        ["git", "rev-parse", f"{pre_sentinel_sha}~1"],
        cwd=worktree, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "reset", "--hard", parent_of_sentinel_sha],
        cwd=worktree, check=True, capture_output=True,
    )
    with pytest.raises(wss.WorkspaceTamperedError) as ei:
        wss.verify_workspace_sentinel(sentinel, worktree)
    assert "non-descendant" in ei.value.reason


def test_sentinel_raises_when_payload_corrupt(tmp_path):
    """Sentinel exists but content is unreadable JSON — treat as
    tampered."""
    _, worktree = _init_main_and_worktree(tmp_path)
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    sentinel.write_text("not json{", encoding="utf-8")
    with pytest.raises(wss.WorkspaceTamperedError) as ei:
        wss.verify_workspace_sentinel(sentinel, worktree)
    assert "corrupt" in ei.value.reason


def test_sentinel_payload_includes_required_fields(tmp_path):
    """Payload schema pin — caller's audit log expects these keys."""
    _, worktree = _init_main_and_worktree(tmp_path)
    sentinel = wss.write_workspace_sentinel(worktree, "OP-TEST")
    payload = json.loads(sentinel.read_text(encoding="utf-8"))
    assert {"ticket_key", "head_sha", "timestamp", "worktree_path"} <= set(payload)

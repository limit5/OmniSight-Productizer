"""OP-817 — per-ticket ephemeral worktree isolation.

Pins the contract for ``sync_to_gerrit_develop(..., ephemeral=True)`` +
``cleanup_ephemeral_worktree``. Two flavours of test:

* **End-to-end with real git** (most tests): a local bare repo stands in for
  Gerrit, monkeypatched into ``_gerrit_ssh_url`` so ``git fetch`` resolves to
  the local filesystem. This exercises real ``git worktree add`` semantics
  (administrative metadata under ``.git/worktrees/`` and parallel-tick
  isolation) without needing network or SSH credentials.

* **Pure-mock contract** (signature / backward-compat): ensure ``ephemeral``
  is keyword-only with a default of False so existing callers don't break.

The acceptance criteria the tests are wired to (from JIRA OP-817):

  AC1: WORKTREE_BASE = ~/work/sora-worktrees/; per-tick mkdir
       ``<ticket>-<run_id>`` + ``git worktree add``.
  AC2: After push (success OR failure): ``git worktree remove`` + ``rm -rf``.
  AC3: Concurrent runs of different tickets do NOT conflict.
  AC4: Synthetic: 3 parallel ticks of different tickets → 3 separate
       worktrees, no DU / race.
"""
from __future__ import annotations

import inspect
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import jira_dispatch as jd  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────


def _init_main_repo_with_develop(repo: Path) -> str:
    """Initialise a repo on `develop` with one commit; return that SHA."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "develop", str(repo)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "tester"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tester@example.invalid"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def fake_gerrit_remote(monkeypatch, tmp_path):
    """Stand up a bare repo + main repo so ``git fetch`` resolves locally.

    Yields (main_repo_path, develop_sha). Patches:
    - ``_gerrit_ssh_url`` → path to the bare repo (treated as a remote).
    - ``_gerrit_auth_for_instance`` → returns a placeholder ssh key so the
      caller can still build GIT_SSH_COMMAND without an actual key on disk.
    - ``BREAKERS["gerrit_ssh"].call`` → pass-through (no circuit logic).
    """
    bare = tmp_path / "gerrit-bare.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(bare)],
        check=True, capture_output=True, text=True,
    )

    main_repo = tmp_path / "main-repo"
    sha = _init_main_repo_with_develop(main_repo)
    subprocess.run(
        ["git", "push", str(bare), "develop"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    )

    monkeypatch.setattr(jd, "_gerrit_ssh_url", lambda *a, **kw: str(bare))
    fake_key = tmp_path / "fake-ssh-key"
    fake_key.write_text("")
    monkeypatch.setattr(
        jd, "_gerrit_auth_for_instance",
        lambda agent_class, instance_id=None: ("test-bot", fake_key),
    )
    # Pass-through the gerrit_ssh circuit breaker.
    monkeypatch.setattr(
        jd.BREAKERS["gerrit_ssh"], "call",
        lambda fn, *a, **kw: fn(*a, **kw),
    )
    return main_repo, sha


@pytest.fixture
def ephemeral_base(tmp_path, monkeypatch):
    """Re-root EPHEMERAL_WORKTREE_BASE under tmp_path."""
    base = tmp_path / "sora-worktrees"
    monkeypatch.setattr(jd, "EPHEMERAL_WORKTREE_BASE", base)
    return base


# ── AC1: per-tick mkdir <ticket>-<run_id> + git worktree add ─────────────


def test_sync_ephemeral_creates_per_tick_worktree(
    fake_gerrit_remote, ephemeral_base,
):
    """ephemeral=True creates ``<base>/<ticket>-<run_id>`` and adds a worktree
    there. The returned ``worktree_path`` points at that directory."""
    main_repo, _ = fake_gerrit_remote

    result = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-817",
        ephemeral=True, run_id="abc123",
    )

    expected = ephemeral_base / "OP-817-abc123"
    assert result.worktree_path == expected
    assert expected.is_dir()
    assert (expected / "README.md").exists(), \
        "worktree must materialise content from the develop tip"
    # git considers the path a worktree:
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert str(expected) in listing
    assert result.branch_name == "feature/OP-817-runner-fresh"


def test_sync_ephemeral_uses_default_base_path():
    """``EPHEMERAL_WORKTREE_BASE`` defaults to ``~/work/sora-worktrees`` per AC1.

    No git ops needed — just pin the constant.
    """
    expected = Path("~/work/sora-worktrees").expanduser()
    assert jd.EPHEMERAL_WORKTREE_BASE == expected


def test_sync_ephemeral_default_run_id_is_generated(
    fake_gerrit_remote, ephemeral_base,
):
    """If caller omits ``run_id``, sync_to_gerrit_develop must mint one — the
    resulting directory name still matches ``<ticket>-<suffix>``.
    """
    main_repo, _ = fake_gerrit_remote

    result = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-817", ephemeral=True,
    )

    assert result.worktree_path is not None
    name = result.worktree_path.name
    assert name.startswith("OP-817-"), f"expected ticket prefix, got {name}"
    assert len(name) > len("OP-817-"), "run_id suffix must be non-empty"
    assert result.worktree_path.parent == ephemeral_base


def test_sync_ephemeral_collision_raises(
    fake_gerrit_remote, ephemeral_base,
):
    """If the target dir already exists (run_id collision), refuse — the whole
    point of ephemeral is isolation, so silently reusing is wrong.
    """
    main_repo, _ = fake_gerrit_remote
    ephemeral_base.mkdir(parents=True)
    stale = ephemeral_base / "OP-817-collide"
    stale.mkdir()

    with pytest.raises(RuntimeError, match="already exists"):
        jd.sync_to_gerrit_develop(
            main_repo, "subscription-claude", "OP-817",
            ephemeral=True, run_id="collide",
        )


# ── AC2: cleanup removes worktree + dir, idempotent ──────────────────────


def test_cleanup_ephemeral_worktree_removes_dir_and_git_metadata(
    fake_gerrit_remote, ephemeral_base,
):
    """``cleanup_ephemeral_worktree`` removes both the worktree directory AND
    the git administrative entry under ``<main>/.git/worktrees/``.
    """
    main_repo, _ = fake_gerrit_remote
    result = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-817",
        ephemeral=True, run_id="cleanup",
    )
    wt = result.worktree_path
    assert wt is not None and wt.is_dir()

    # Pre-cleanup: git tracks the worktree.
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert str(wt) in listing

    jd.cleanup_ephemeral_worktree(main_repo, wt)

    # Post-cleanup: dir gone, git no longer tracks it.
    assert not wt.exists()
    listing_after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert str(wt) not in listing_after


def test_cleanup_ephemeral_worktree_idempotent_on_missing_path(
    fake_gerrit_remote, ephemeral_base,
):
    """Cleanup on a path that was never created must be a no-op (no raise) —
    this is the failure-path branch from AC2 (push failed BEFORE worktree
    add succeeded, or two cleanup attempts race).
    """
    main_repo, _ = fake_gerrit_remote
    ghost = ephemeral_base / "OP-817-ghost"
    # Must not raise even though `ghost` was never created.
    jd.cleanup_ephemeral_worktree(main_repo, ghost)
    assert not ghost.exists()


def test_cleanup_ephemeral_worktree_idempotent_on_double_call(
    fake_gerrit_remote, ephemeral_base,
):
    """Calling cleanup twice on the same worktree must be safe."""
    main_repo, _ = fake_gerrit_remote
    result = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-817",
        ephemeral=True, run_id="twice",
    )
    wt = result.worktree_path
    jd.cleanup_ephemeral_worktree(main_repo, wt)
    # Second call: no raise, dir still absent.
    jd.cleanup_ephemeral_worktree(main_repo, wt)
    assert not wt.exists()


def test_cleanup_ephemeral_worktree_handles_missing_main_repo(
    tmp_path,
):
    """If the main repo path is gone, cleanup still removes the dir — we never
    want to leak an ephemeral worktree just because the main repo got moved.
    """
    fake_main = tmp_path / "vanished-main"
    wt = tmp_path / "lingering-worktree"
    wt.mkdir()
    (wt / "evidence.txt").write_text("leak me")

    # Must not raise; must remove the dir.
    jd.cleanup_ephemeral_worktree(fake_main, wt)
    assert not wt.exists()


# ── AC3 + AC4: concurrent ticks of different tickets ─────────────────────


def test_three_parallel_ticks_get_three_separate_worktrees(
    fake_gerrit_remote, ephemeral_base,
):
    """Synthetic AC4: three different tickets running in parallel get three
    distinct worktree directories. No git-administrative race, no shared
    branch, and writing to one worktree is invisible to the others.
    """
    main_repo, _ = fake_gerrit_remote

    results: dict[str, jd.WorktreeSyncResult] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def run_one(ticket: str) -> None:
        try:
            barrier.wait(timeout=10)
            res = jd.sync_to_gerrit_develop(
                main_repo, "subscription-claude", ticket,
                ephemeral=True, run_id=f"r{ticket[-2:]}",
            )
            results[ticket] = res
        except BaseException as exc:  # noqa: BLE001 - propagate to test
            errors.append(exc)

    threads = [
        threading.Thread(target=run_one, args=(t,))
        for t in ("OP-901", "OP-902", "OP-903")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"parallel sync raised: {errors!r}"
    assert set(results.keys()) == {"OP-901", "OP-902", "OP-903"}

    paths = {k: r.worktree_path for k, r in results.items()}
    # All three paths distinct:
    assert len(set(paths.values())) == 3
    # Each on its own ticket-keyed branch:
    for ticket, res in results.items():
        assert res.branch_name == f"feature/{ticket}-runner-fresh"
        assert res.worktree_path.is_dir()

    # Cross-write isolation: a commit in one worktree must not appear in
    # another's working tree. (The branches differ, so this is more of a
    # sanity check that the dirs are independent filesystems.)
    res_a = results["OP-901"]
    (res_a.worktree_path / "evidence_OP-901.txt").write_text("only in 901\n")
    for other in ("OP-902", "OP-903"):
        wt_other = results[other].worktree_path
        assert not (wt_other / "evidence_OP-901.txt").exists()

    # All three worktrees are tracked by the main repo's git metadata.
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    ).stdout
    for res in results.values():
        assert str(res.worktree_path) in listing


def test_parallel_ticks_clean_up_independently(
    fake_gerrit_remote, ephemeral_base,
):
    """Tearing down one ticket's worktree must not affect the others — the
    failure-path branch from AC2 ("after push success OR failure") in a
    concurrent context.
    """
    main_repo, _ = fake_gerrit_remote
    r1 = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-910",
        ephemeral=True, run_id="aa",
    )
    r2 = jd.sync_to_gerrit_develop(
        main_repo, "subscription-claude", "OP-911",
        ephemeral=True, run_id="bb",
    )

    # Cleanup r1; r2 untouched.
    jd.cleanup_ephemeral_worktree(main_repo, r1.worktree_path)
    assert not r1.worktree_path.exists()
    assert r2.worktree_path.is_dir()
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert str(r1.worktree_path) not in listing
    assert str(r2.worktree_path) in listing

    # Now cleanup r2.
    jd.cleanup_ephemeral_worktree(main_repo, r2.worktree_path)
    assert not r2.worktree_path.exists()


# ── Signature / backward-compat pins ─────────────────────────────────────


def test_sync_to_gerrit_develop_signature_accepts_ephemeral_kwarg():
    """``ephemeral`` is a keyword-only argument with default False — protects
    legacy positional callers (e.g. ``ship-pending.py``, ``auto-runner-multi``)
    from breaking when this lands.
    """
    sig = inspect.signature(jd.sync_to_gerrit_develop)
    params = sig.parameters
    assert "ephemeral" in params
    assert params["ephemeral"].default is False
    assert params["ephemeral"].kind == inspect.Parameter.KEYWORD_ONLY
    assert "run_id" in params
    assert params["run_id"].default is None
    assert params["run_id"].kind == inspect.Parameter.KEYWORD_ONLY


def test_worktree_sync_result_exposes_worktree_path_field():
    """The ephemeral path round-trips through ``WorktreeSyncResult`` so the
    caller's ``finally`` block can pass it to ``cleanup_ephemeral_worktree``.
    """
    result = jd.WorktreeSyncResult(
        branch_name="feature/OP-X-runner-fresh",
        develop_sha="deadbeef" * 5,
        detail="x",
        worktree_path=Path("/tmp/ephemeral"),
    )
    assert result.worktree_path == Path("/tmp/ephemeral")
    # Default is None so legacy non-ephemeral callers stay unchanged.
    legacy = jd.WorktreeSyncResult(
        branch_name="feature/OP-Y-runner-fresh",
        develop_sha="cafebabe" * 5,
        detail="y",
    )
    assert legacy.worktree_path is None


def test_non_ephemeral_path_unchanged_by_op817(tmp_path, monkeypatch):
    """Backward compatibility: ephemeral=False is the default and the legacy
    in-place `git switch -C` behaviour is preserved. Mock-only — verifies the
    command sequence didn't regress.
    """
    fake_sha = "ab" * 20
    calls: list[list[str]] = []

    class FakeResult:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return FakeResult(stdout=fake_sha + "\n")
        if cmd[:4] == ["git", "-C", str(tmp_path), "rev-parse"]:
            return FakeResult(stdout=".git\n")
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)

    res = jd.sync_to_gerrit_develop(tmp_path, "subscription-claude", "OP-817")

    assert res.worktree_path is None, \
        "non-ephemeral path must not return an ephemeral worktree_path"
    # Legacy switch-in-place: no `git worktree add`, but `git switch -C` present.
    assert not any(c[:3] == ["git", "worktree", "add"] for c in calls)
    assert any(c[:2] == ["git", "switch"] for c in calls)
    assert any(c[:2] == ["git", "reset"] for c in calls)
    assert any(c[:2] == ["git", "clean"] for c in calls)

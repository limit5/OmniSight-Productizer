"""Tests for OP-2194 / R.2a — sync_routed_repo (routed clone + branch).

Network-free: subprocess.run + the gerrit_ssh circuit breaker are patched so
the test asserts the EXACT git argv (clone targets the routed gerrit_url, not
productizer) and the per-instance clone path, without touching git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents import jira_dispatch
from backend.agents.routed_repo import RoutedRepo


ROUTED = RoutedRepo(
    name="conference-appliance",
    gerrit_url="ssh://claude-bot@sora.services:29418/omnisight/conference-appliance",
    ref="refs/for/develop",
    git_account_ref=None,
    context="conference-appliance",
)


@pytest.fixture
def fake_git(monkeypatch, tmp_path):
    """Patch subprocess.run + the gerrit_ssh breaker + workspace base.

    Records every git argv; rev-parse returns a stub SHA. The breaker just
    calls through to the patched subprocess.run.
    """
    calls = []

    def _run(argv, **kw):
        calls.append((list(argv), kw.get("cwd")))
        class _R:
            stdout = "deadbeefcafebabe0000000000000000deadbeef\n"
            returncode = 0
        return _R()

    monkeypatch.setattr(jira_dispatch.subprocess, "run", _run, raising=False)
    # gerrit_ssh breaker: just invoke fn(*a, **k)
    monkeypatch.setitem(
        jira_dispatch.BREAKERS, "gerrit_ssh",
        type("B", (), {"call": staticmethod(lambda fn, *a, **k: fn(*a, **k))})(),
    )
    monkeypatch.setattr(jira_dispatch, "ROUTED_WORKSPACE_BASE", tmp_path, raising=False)
    # auth: avoid touching real ssh-key resolution
    monkeypatch.setattr(
        jira_dispatch, "_gerrit_auth_for_instance",
        lambda ac, iid=None: ("claude-bot", Path("/fake/key")), raising=False,
    )
    monkeypatch.setattr(
        jira_dispatch.runner_sandbox, "build_allowlisted_env",
        lambda extra=None: {"GIT_SSH_COMMAND": "ssh -i /fake/key"}, raising=False,
    )
    return calls


def test_clones_the_routed_url_not_productizer(fake_git):
    res = jira_dispatch.sync_routed_repo(
        ROUTED, "OP-2170", "subscription-claude", "claude-1", run_id="r1",
    )
    clone_calls = [c for c, _ in fake_git if c[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    assert clone_calls[0][2] == ROUTED.gerrit_url
    # the clone destination is under the per-instance routed path, NOT a
    # productizer worktree
    dest = clone_calls[0][3]
    assert "conference-appliance" in dest
    assert "claude-1-OP-2170-r1" in dest
    # never clones/fetches productizer
    assert all("OmniSight-Productizer" not in arg for c, _ in fake_git for arg in c)


def test_returns_worktree_path_and_branch(fake_git):
    res = jira_dispatch.sync_routed_repo(
        ROUTED, "OP-2170", "subscription-claude", "claude-1", run_id="r1",
    )
    assert res.branch_name == "feature/OP-2170-runner-fresh"
    assert res.develop_sha.startswith("deadbeef")
    assert res.worktree_path is not None
    assert res.worktree_path.name == "claude-1-OP-2170-r1"


def test_fetch_and_branch_target_the_clone_cwd(fake_git):
    jira_dispatch.sync_routed_repo(
        ROUTED, "OP-2170", "subscription-claude", "claude-1", run_id="r1",
    )
    # fetch + switch run with cwd inside the routed clone
    fetches = [(c, cwd) for c, cwd in fake_git if c[:2] == ["git", "fetch"]]
    switches = [(c, cwd) for c, cwd in fake_git if c[:2] == ["git", "switch"]]
    assert fetches and "claude-1-OP-2170-r1" in str(fetches[0][1])
    assert switches and switches[0][0] == [
        "git", "switch", "-C", "feature/OP-2170-runner-fresh",
        "deadbeefcafebabe0000000000000000deadbeef",
    ]


def test_refuses_existing_clone_dir(fake_git, tmp_path):
    # pre-create the target dir → must refuse rather than reuse
    d = tmp_path / "conference-appliance" / "claude-1-OP-2170-r1"
    d.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="already exists"):
        jira_dispatch.sync_routed_repo(
            ROUTED, "OP-2170", "subscription-claude", "claude-1", run_id="r1",
        )


def test_path_isolation_between_instances(fake_git):
    a = jira_dispatch.sync_routed_repo(ROUTED, "OP-2170", "subscription-claude", "claude-1", run_id="r1")
    b = jira_dispatch.sync_routed_repo(ROUTED, "OP-2170", "subscription-claude", "claude-2", run_id="r2")
    assert a.worktree_path != b.worktree_path

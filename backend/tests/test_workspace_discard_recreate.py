"""R8 #314 — ``WorkspaceManager.discard_and_recreate(agent_id, anchor_sha)``.

Locks the second sub-bullet under R8 in TODO.md (row 2873):

    "WorkspaceManager 擴充：discard_and_recreate(agent_id, anchor_sha)
     → 刪除舊 worktree dir（安全刪除，先 git worktree remove --force）
     → 新建 worktree → 回傳新 path"

Per ``docs/design/r8-idempotent-retry-worktree.md`` §7 row 2:

    signature: discard_and_recreate(agent_id, anchor_sha) → WorkspaceInfo
    git worktree remove --force + shutil.rmtree fallback
    git worktree add -b from anchor SHA
    emit ``workspace.retried`` SSE event

Audit-log persistence is row 2874 — out of scope here. Startup orphan
scan is row 2875. Integration test that wires retry orchestrator → this
function → audit is row 2876. This file rigorously locks the contract
of the helper itself.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend import workspace as ws_mod


# ---------------------------------------------------------------------------
# Fixtures: throwaway git repo + redirected workspaces root
# (mirrors test_workspace_anchor.py — same throwaway-source contract)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A 1-commit git repo we can use as a worktree source."""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@local", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def redirected_ws_root(tmp_path: Path, monkeypatch):
    """Point workspace module at a clean tmp root so tests don't pollute
    the project's real ``.agent_workspaces``."""
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


@pytest.fixture
def provisioned(fake_repo, redirected_ws_root):
    """Provision a workspace, yield the WorkspaceInfo, cleanup on teardown.

    Used by tests that don't need to control the agent_id name —
    centralises the setup/teardown so the test bodies stay focused on
    the discard_and_recreate contract under test.
    """
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-r8",
        task_id="task-r8",
        repo_source=str(fake_repo),
    ))
    try:
        yield info
    finally:
        # ``cleanup`` no-ops if discard_and_recreate already removed the
        # registry entry, but normal happy-path tests leave it alive so
        # this final cleanup is the standard teardown.
        try:
            asyncio.run(ws_mod.cleanup("agent-r8"))
        except Exception:
            pass


def _agent_commit(ws_path: Path, content: str = "agent did stuff\n") -> str:
    """Make a commit inside the worktree; return the new tip SHA."""
    (ws_path / "agent_work.txt").write_text(content)
    _git("add", "agent_work.txt", cwd=ws_path)
    _git("commit", "-q", "-m", "agent commit past anchor", cwd=ws_path)
    return _git("rev-parse", "HEAD", cwd=ws_path)


# ---------------------------------------------------------------------------
# 1) Happy path — fresh worktree at anchor, registry intact
# ---------------------------------------------------------------------------


def test_happy_path_recreates_at_anchor(provisioned):
    info = provisioned
    anchor = info.anchor_sha
    ws_path = info.path
    branch = info.branch
    assert anchor and len(anchor) == 40

    # Agent makes a commit past anchor — this is the "dirty" state we
    # want retry to wipe.
    new_tip = _agent_commit(ws_path)
    assert new_tip != anchor
    (ws_path / "scratch_artifact.bin").write_bytes(b"\x00" * 64)

    returned = asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=anchor,
    ))

    # Same WorkspaceInfo instance — identity preserved so external
    # callers' references stay valid.
    assert returned is info
    assert returned.path == ws_path
    assert returned.branch == branch
    assert returned.anchor_sha == anchor

    # Path exists, HEAD is back at the anchor, agent's "scratch" file
    # is gone, agent's commit is gone from the branch tip.
    assert ws_path.is_dir()
    head_after = _git("rev-parse", "HEAD", cwd=ws_path)
    assert head_after == anchor
    assert not (ws_path / "agent_work.txt").exists()
    assert not (ws_path / "scratch_artifact.bin").exists()


# ---------------------------------------------------------------------------
# 2) commit_count + status are reset
# ---------------------------------------------------------------------------


def test_resets_commit_count_and_status(provisioned):
    info = provisioned
    info.commit_count = 7
    info.status = "finalized"

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=info.anchor_sha,
    ))

    assert info.commit_count == 0
    assert info.status == "active"


# ---------------------------------------------------------------------------
# 3) Branch ref in source repo is recreated at anchor
# ---------------------------------------------------------------------------


def test_branch_ref_realigned_to_anchor_in_source_repo(
    provisioned, fake_repo,
):
    """The agent branch in the source repo must point at anchor after
    recreate (not at the abandoned tip past anchor). Otherwise a
    subsequent provision() that re-uses ``git branch <branch> HEAD``
    semantics could drift back to the old tip."""
    info = provisioned
    anchor = info.anchor_sha
    _agent_commit(info.path)

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=anchor,
    ))

    # Source-repo branch tip == anchor (not the old agent commit).
    src_branch_tip = _git(
        "rev-parse", info.branch, cwd=fake_repo,
    )
    assert src_branch_tip == anchor


# ---------------------------------------------------------------------------
# 4) ValueError on empty / whitespace anchor (legacy fallback contract)
# ---------------------------------------------------------------------------


def test_rejects_none_anchor(provisioned):
    with pytest.raises(ValueError, match="anchor_sha required"):
        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="agent-r8", anchor_sha=None,  # type: ignore[arg-type]
        ))


def test_rejects_empty_anchor(provisioned):
    with pytest.raises(ValueError, match="anchor_sha required"):
        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="agent-r8", anchor_sha="",
        ))


def test_rejects_whitespace_anchor(provisioned):
    with pytest.raises(ValueError, match="anchor_sha required"):
        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="agent-r8", anchor_sha="   ",
        ))


# ---------------------------------------------------------------------------
# 5) KeyError on unknown agent_id — no silent no-op
# ---------------------------------------------------------------------------


def test_rejects_unknown_agent(redirected_ws_root):
    with pytest.raises(KeyError, match="No active workspace"):
        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="ghost-agent",
            anchor_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        ))


# ---------------------------------------------------------------------------
# 6) RuntimeError when anchor SHA isn't reachable in the source repo
# ---------------------------------------------------------------------------


def test_rejects_anchor_not_in_object_store(provisioned):
    """Passing a syntactically-valid SHA that isn't a real commit in the
    source repo must raise RuntimeError, not silently produce a half-built
    worktree. Caller (orchestrator) escalates from here."""
    bogus_anchor = "0" * 40
    with pytest.raises(RuntimeError, match="worktree add"):
        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="agent-r8", anchor_sha=bogus_anchor,
        ))


# ---------------------------------------------------------------------------
# 7) Idempotent — three consecutive recreates all succeed
# ---------------------------------------------------------------------------


def test_idempotent_repeated_recreates(provisioned):
    """Per design doc §3: 'discard_and_recreate' is the unit of retry,
    so calling it N times in a row (same anchor) must always end at the
    anchor with no leftover state from prior iterations."""
    info = provisioned
    anchor = info.anchor_sha

    for i in range(3):
        # Dirty the workspace each iteration so we can prove each call
        # actually resets it (and not just lucks out because the prior
        # state was clean).
        (info.path / f"iteration_{i}.txt").write_text(f"iter {i}\n")
        if i > 0:
            _agent_commit(info.path, content=f"iter {i} commit\n")

        asyncio.run(ws_mod.discard_and_recreate(
            agent_id="agent-r8", anchor_sha=anchor,
        ))

        head = _git("rev-parse", "HEAD", cwd=info.path)
        assert head == anchor
        assert not (info.path / f"iteration_{i}.txt").exists()
        assert not (info.path / "agent_work.txt").exists()


# ---------------------------------------------------------------------------
# 8) Tolerates externally-rm'd workspace dir (orphan path)
# ---------------------------------------------------------------------------


def test_tolerates_externally_removed_workspace_dir(provisioned):
    """If the workspace directory was already removed (e.g. operator
    rm -rf'd it, or a prior cleanup half-ran), discard_and_recreate
    should still produce a fresh worktree. The destroy step is best-
    effort — the recreate step is the load-bearing one."""
    import shutil as _shutil
    info = provisioned
    anchor = info.anchor_sha

    _shutil.rmtree(info.path)
    assert not info.path.exists()

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=anchor,
    ))

    assert info.path.is_dir()
    assert _git("rev-parse", "HEAD", cwd=info.path) == anchor


# ---------------------------------------------------------------------------
# 9) Re-establishes per-workspace git identity (provision() parity)
# ---------------------------------------------------------------------------


def test_restores_git_identity_on_fresh_worktree(provisioned):
    """An immediate ``git commit`` in the recreated worktree must use
    the per-agent identity provision() set up. If we forget to re-config
    after the worktree is destroyed/rebuilt, the host's global identity
    leaks in (or strict-ident mode rejects the commit)."""
    info = provisioned
    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=info.anchor_sha,
    ))

    user_name = _git("config", "user.name", cwd=info.path)
    user_email = _git("config", "user.email", cwd=info.path)
    assert user_name == "Agent-agent-r8"
    assert user_email == "agent-r8@omnisight.local"


# ---------------------------------------------------------------------------
# 10) Restores /test_assets/ .gitignore line (Safety Rule defence)
# ---------------------------------------------------------------------------


def test_restores_test_assets_gitignore_line(provisioned):
    """CLAUDE.md Safety Rule: never modify test_assets/. provision()
    inserts ``/test_assets/`` into the worktree's .gitignore so a stray
    ``git add -A`` cannot stage that bind-mount. discard_and_recreate
    must preserve that defence on the rebuilt worktree."""
    info = provisioned
    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=info.anchor_sha,
    ))

    gi = (info.path / ".gitignore").read_text()
    assert "/test_assets/" in gi


# ---------------------------------------------------------------------------
# 11) Emits ``workspace.retried`` SSE event with anchor + reason
# ---------------------------------------------------------------------------


def test_emits_retried_event_on_bus(provisioned, monkeypatch):
    """Per design §7 row 2: emit ``workspace.retried`` SSE event.
    Spy on ``backend.events.bus.publish`` and assert the payload shape
    so dashboard subscribers (Agent Health Card, Workspace panel)
    render the right thing."""
    from backend import events as _events

    captured: list[tuple[str, dict]] = []
    real_publish = _events.bus.publish

    def _spy(channel: str, data, **kw):
        captured.append((channel, dict(data) if isinstance(data, dict) else data))
        return real_publish(channel, data, **kw)

    monkeypatch.setattr(_events.bus, "publish", _spy, raising=True)

    info_anchor = provisioned.anchor_sha
    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8",
        anchor_sha=info_anchor,
        reason="ai-timeout",
    ))

    workspace_events = [
        d for ch, d in captured
        if ch == "workspace" and d.get("agent_id") == "agent-r8"
    ]
    retried = [d for d in workspace_events if d.get("action") == "retried"]
    assert len(retried) == 1, (
        f"expected exactly one workspace.retried event, got {workspace_events}"
    )
    detail = retried[0]["detail"]
    assert "retried" not in detail  # action is in its own field
    assert info_anchor[:12] in detail
    assert "ai-timeout" in detail
    assert provisioned.branch in detail


# ---------------------------------------------------------------------------
# 12) Default reason is "retry" (callers may omit it)
# ---------------------------------------------------------------------------


def test_default_reason_is_retry(provisioned, monkeypatch):
    from backend import events as _events
    captured: list[dict] = []
    real_publish = _events.bus.publish

    def _spy(channel: str, data, **kw):
        if channel == "workspace" and isinstance(data, dict):
            captured.append(dict(data))
        return real_publish(channel, data, **kw)

    monkeypatch.setattr(_events.bus, "publish", _spy, raising=True)

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=provisioned.anchor_sha,
    ))

    retried = [d for d in captured if d.get("action") == "retried"]
    assert retried and "reason=retry" in retried[0]["detail"]


# ---------------------------------------------------------------------------
# 13) Source repo's ``git worktree list`` shows the new worktree
# ---------------------------------------------------------------------------


def test_source_repo_worktree_list_shows_recreated(provisioned, fake_repo):
    """After recreate, ``git worktree list`` in the source repo must
    list exactly one entry for the agent path — proves the .git/worktrees
    admin block was rebuilt cleanly (not a stale orphan + a new entry)."""
    info = provisioned
    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=info.anchor_sha,
    ))

    listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
    matching = [
        line for line in listing.splitlines()
        if line.startswith("worktree ") and str(info.path) in line
    ]
    assert len(matching) == 1, (
        f"expected 1 worktree entry for {info.path}, got: {listing!r}"
    )

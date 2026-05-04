"""R8 #314 row 2875 — startup orphan worktree scan contract.

Locks the fourth sub-bullet under R8 in TODO.md (row 2875):

    "既有 startup cleanup 延伸：啟動時掃描 orphan worktree
     (`git worktree list` 中不屬於任何 active agent 的 worktree)
     → 自動 remove + log"

Per ``docs/design/r8-idempotent-retry-worktree.md`` §7 row 4:

    main.py lifespan 啟動時呼叫 WorkspaceManager.cleanup_orphan_worktrees()
    --- git worktree list 與 _workspaces 記憶體表 / DB 表交集，不在
    active 集合的 worktree 走 git worktree remove --force，emit
    workspace.orphan_cleanup audit event。

The function must:
  1. find admin-block-known orphans (in ``git worktree list`` but
     not registered in ``_workspaces``)
  2. find filesystem-ghost orphans (dir present under
     ``_WORKSPACES_ROOT`` but no admin block / not registered)
  3. ``git worktree remove --force`` + ``shutil.rmtree`` fallback
  4. preserve workspaces that ARE registered in ``_workspaces``
  5. emit ``workspace.orphan_cleanup`` SSE event + audit row per
     orphan removed
  6. survive audit failure (best-effort recovery contract)
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend import workspace as ws_mod


# ---------------------------------------------------------------------------
# Fixtures: throwaway git repo + redirected workspaces root + redirected
# _MAIN_REPO so ``git worktree list`` runs against the throwaway, not the
# real project repo.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
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
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


@pytest.fixture
def redirected_main_repo(fake_repo, monkeypatch):
    """Point ``_MAIN_REPO`` at the throwaway so the function's
    ``git worktree list`` / ``git worktree remove`` calls hit the
    throwaway repo's admin block, not the real project's."""
    monkeypatch.setattr(ws_mod, "_MAIN_REPO", fake_repo, raising=True)
    return fake_repo


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with an empty ``_workspaces`` dict."""
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)


@pytest.fixture
def silenced_audit(monkeypatch):
    """``audit.log`` would no-op in unit tests anyway (no PG pool),
    but make the no-op explicit + capture-able so individual tests
    can assert audit semantics. The internal ``log_impl`` would
    otherwise spam warnings about pool absence."""
    from backend import audit as _audit
    captured: list[dict] = []

    async def _spy(action, entity_kind, entity_id, before=None, after=None,
                   actor="system", session_id=None, conn=None):
        captured.append({
            "action": action,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "before": before,
            "after": after,
            "actor": actor,
        })
        return None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)
    return captured


# ---------------------------------------------------------------------------
# Helpers — provisioning a workspace then "abandoning" it (clearing the
# in-process registry but leaving the disk + admin block behind)
# ---------------------------------------------------------------------------


def _provision_then_abandon(agent_id: str, fake_repo: Path) -> Path:
    """Provision a workspace, then drop it from ``_workspaces`` —
    simulates the state after a process crash where the worktree is
    on disk + in git's admin block but no in-memory registry knows
    about it."""
    info = asyncio.run(ws_mod.provision(
        agent_id=agent_id, task_id=f"task-{agent_id}",
        repo_source=str(fake_repo),
    ))
    ws_mod._workspaces.pop(agent_id, None)
    return info.path


# ---------------------------------------------------------------------------
# 1) Empty workspaces root → no-op, returns []
# ---------------------------------------------------------------------------


def test_empty_workspaces_root_is_noop(
    redirected_ws_root, redirected_main_repo, silenced_audit,
):
    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())
    assert result == []
    assert silenced_audit == []


# ---------------------------------------------------------------------------
# 2) Missing workspaces root → no-op, returns [] (defensive boot path)
# ---------------------------------------------------------------------------


def test_missing_workspaces_root_is_noop(
    tmp_path, redirected_main_repo, silenced_audit, monkeypatch,
):
    """If the ``_WORKSPACES_ROOT`` doesn't exist (first ever boot,
    or operator wiped it) the scan must silently no-op rather than
    blow up the lifespan."""
    nonexistent = tmp_path / "does_not_exist"
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", nonexistent, raising=True)

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())
    assert result == []


# ---------------------------------------------------------------------------
# 3) Single orphan with admin block: removed; admin block gone too
# ---------------------------------------------------------------------------


def test_admin_block_orphan_is_removed(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    orphan_path = _provision_then_abandon("ghost-1", fake_repo)
    assert orphan_path.is_dir()

    pre_listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
    assert str(orphan_path) in pre_listing

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())

    assert len(result) == 1
    assert result[0]["status"] == "removed"
    assert result[0]["path"] == str(orphan_path)
    assert result[0]["agent"] == "ghost-1"
    assert "worktree_remove" in result[0]["method"]
    assert not orphan_path.exists()

    # Admin block in the source repo must be gone too — that's the
    # point of using ``git worktree remove`` over plain ``rmtree``.
    post_listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
    assert str(orphan_path) not in post_listing


# ---------------------------------------------------------------------------
# 4) Active workspace (still in registry) is NOT removed
# ---------------------------------------------------------------------------


def test_active_workspace_is_preserved(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    """The whole point of "orphan" is "not in active set". Anything
    in ``_workspaces`` must come back unscathed."""
    info = asyncio.run(ws_mod.provision(
        agent_id="alive-agent", task_id="task-1",
        repo_source=str(fake_repo),
    ))
    try:
        result = asyncio.run(ws_mod.cleanup_orphan_worktrees())
        assert result == []
        assert info.path.is_dir()
        # admin block intact — workspace still listed
        listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
        assert str(info.path) in listing
    finally:
        asyncio.run(ws_mod.cleanup("alive-agent"))


# ---------------------------------------------------------------------------
# 5) Mixed: orphan + active in same root → only orphan removed
# ---------------------------------------------------------------------------


def test_mixed_orphan_and_active_only_orphan_removed(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    orphan_path = _provision_then_abandon("orphan-a", fake_repo)
    active_info = asyncio.run(ws_mod.provision(
        agent_id="alive-b", task_id="task-b",
        repo_source=str(fake_repo),
    ))
    try:
        result = asyncio.run(ws_mod.cleanup_orphan_worktrees())

        removed_paths = {r["path"] for r in result if r["status"] == "removed"}
        assert str(orphan_path) in removed_paths
        assert str(active_info.path) not in removed_paths
        assert not orphan_path.exists()
        assert active_info.path.is_dir()
    finally:
        asyncio.run(ws_mod.cleanup("alive-b"))


# ---------------------------------------------------------------------------
# 6) Filesystem ghost (dir present, no admin block) is removed via rmtree
# ---------------------------------------------------------------------------


def test_filesystem_ghost_orphan_is_removed(
    redirected_ws_root, redirected_main_repo, silenced_audit,
):
    """A dir in ``_WORKSPACES_ROOT`` that git has no admin block for
    (e.g. ``git worktree prune`` ran but the dir wasn't deleted).
    The function must catch this via the filesystem scan and clear
    it with ``shutil.rmtree``."""
    ghost = redirected_ws_root / "ghost-fs"
    ghost.mkdir()
    (ghost / "leftover.txt").write_text("from a previous life\n")

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())

    assert len(result) == 1
    assert result[0]["agent"] == "ghost-fs"
    assert result[0]["source"] == "fs"
    assert result[0]["status"] == "removed"
    assert "rmtree" in result[0]["method"]
    assert not ghost.exists()


# ---------------------------------------------------------------------------
# 7) Multiple admin-block orphans → all removed in one pass
# ---------------------------------------------------------------------------


def test_multiple_orphans_all_removed(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    paths = [
        _provision_then_abandon(f"crashed-{i}", fake_repo)
        for i in range(3)
    ]

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())

    assert len({r["path"] for r in result}) == 3
    assert all(r["status"] == "removed" for r in result)
    for p in paths:
        assert not p.exists()


# ---------------------------------------------------------------------------
# 8) Audit row emitted per orphan with the design §7 row 4 schema
# ---------------------------------------------------------------------------


def test_audit_log_emitted_per_orphan(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    orphan_path = _provision_then_abandon("audit-target", fake_repo)

    asyncio.run(ws_mod.cleanup_orphan_worktrees())

    matching = [
        c for c in silenced_audit
        if c["action"] == "workspace.orphan_cleanup"
    ]
    assert len(matching) == 1
    row = matching[0]
    assert row["entity_kind"] == "workspace"
    assert row["entity_id"] == "audit-target"
    assert (row["before"] or {}).get("worktree_path") == str(orphan_path)
    assert (row["after"] or {}).get("status") == "removed"


# ---------------------------------------------------------------------------
# 9) SSE ``workspace.orphan_cleanup`` event emitted per orphan
# ---------------------------------------------------------------------------


def test_sse_event_emitted_per_orphan(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
    monkeypatch,
):
    """Per design §7 row 4: emit ``workspace.orphan_cleanup`` SSE
    event so dashboard subscribers (Workspace panel) render the
    boot-time recovery in real time."""
    from backend import events as _events
    captured: list[dict] = []
    real_publish = _events.bus.publish

    def _spy(channel, data, **kw):
        if channel == "workspace" and isinstance(data, dict):
            captured.append(dict(data))
        return real_publish(channel, data, **kw)

    monkeypatch.setattr(_events.bus, "publish", _spy, raising=True)

    _provision_then_abandon("ssereport", fake_repo)
    asyncio.run(ws_mod.cleanup_orphan_worktrees())

    cleanup_events = [
        d for d in captured
        if d.get("action") == "orphan_cleanup"
        and d.get("agent_id") == "ssereport"
    ]
    assert len(cleanup_events) == 1
    detail = cleanup_events[0]["detail"]
    assert "status=removed" in detail
    assert "ssereport" not in detail or "ssereport" in str(cleanup_events[0])


# ---------------------------------------------------------------------------
# 10) Audit failure does NOT break cleanup (best-effort recovery contract)
# ---------------------------------------------------------------------------


def test_audit_failure_does_not_break_cleanup(
    redirected_ws_root, redirected_main_repo, fake_repo, monkeypatch,
):
    """Mirrors discard_and_recreate's best-effort contract: the orphan
    scan is itself a recovery path (boot after a crash). If the audit
    layer is down, we still want the worktree gone — losing a receipt
    is far less harmful than refusing to recover."""
    from backend import audit as _audit

    async def _raising_log(*args, **kwargs):
        raise RuntimeError("simulated audit outage")

    monkeypatch.setattr(_audit, "log", _raising_log, raising=True)

    orphan_path = _provision_then_abandon("audit-down", fake_repo)
    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())

    assert len(result) == 1
    assert result[0]["status"] == "removed"
    assert not orphan_path.exists()


# ---------------------------------------------------------------------------
# 11) ``git worktree prune`` runs in admin-block cleanup (no stale entries)
# ---------------------------------------------------------------------------


def test_git_prune_called_after_orphan_removal(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    """After the function returns, ``git worktree list`` in the source
    repo must NOT contain stale entries pointing at removed paths."""
    orphan_path = _provision_then_abandon("prune-target", fake_repo)

    asyncio.run(ws_mod.cleanup_orphan_worktrees())

    listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
    assert str(orphan_path) not in listing


# ---------------------------------------------------------------------------
# 12) Files in the project root (the source repo's own worktree) ignored
# ---------------------------------------------------------------------------


def test_main_repo_worktree_is_not_treated_as_orphan(
    redirected_ws_root, redirected_main_repo, silenced_audit, fake_repo,
):
    """``git worktree list`` from the source repo also lists the source
    repo's own working tree (the project root). That path is NOT under
    ``_WORKSPACES_ROOT`` and must be skipped — otherwise we'd try to
    rm the project itself."""
    listing = _git("worktree", "list", "--porcelain", cwd=fake_repo)
    assert str(fake_repo) in listing  # confirm the test premise

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())
    assert all(
        Path(r["path"]).is_relative_to(redirected_ws_root)
        for r in result
    )
    # And the source repo dir is intact
    assert fake_repo.is_dir()
    assert (fake_repo / "README.md").is_file()


# ---------------------------------------------------------------------------
# 13) Files (non-dirs) inside ``_WORKSPACES_ROOT`` are not flagged
# ---------------------------------------------------------------------------


def test_non_directory_entries_skipped(
    redirected_ws_root, redirected_main_repo, silenced_audit,
):
    """Operator might drop a stray ``.DS_Store`` or README into the
    workspaces root; the scan only acts on directories."""
    stray = redirected_ws_root / ".DS_Store"
    stray.write_text("noise")

    result = asyncio.run(ws_mod.cleanup_orphan_worktrees())
    assert result == []
    assert stray.exists()  # untouched

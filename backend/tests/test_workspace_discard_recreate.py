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

Row 2874 (audit trail — ``retry.worktree_recreated`` chain row with
``old_worktree_path`` / ``anchor_sha`` / ``reason``) is also locked
in this file (``TestAuditTrail`` class at the bottom) — it's the
same code path's downstream contract. Startup orphan scan is row
2875. Integration test that wires retry orchestrator → this
function → audit is row 2876.
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


# ===========================================================================
# R8 row 2874 — Audit trail contract
# ===========================================================================
#
# "Audit trail：每次 retry 寫入 audit_log（retry.worktree_recreated，
#  附 old_worktree_path / anchor_sha / reason）"
#
# These tests spy on ``backend.audit.log`` (the canonical async write
# entry — see backend/audit.py:142) instead of the DB layer, because:
#
#   1. The unit-test environment has no asyncpg pool. ``audit.log``
#      itself is best-effort and would silently no-op against a pool-
#      less environment, so a DB-level assertion would always pass for
#      the wrong reason.
#   2. The contract this row owns is "we *call* audit with the right
#      shape" — the DB row + hash-chain integrity is owned by
#      backend/tests/test_audit.py (Phase 53 / I8 chain tests) which
#      hammer the persistence layer with a real PG.
#
# The pattern mirrors ``test_emits_retried_event_on_bus`` above —
# monkeypatch the module attribute, capture call args, assert payload
# shape. Lazy-import in the production code (``from backend import
# audit as _audit`` inside discard_and_recreate) means the patch on
# ``backend.audit.log`` is picked up at call time.


def _spy_audit_log(monkeypatch, *, raise_exc: Exception | None = None):
    """Install a spy on ``backend.audit.log`` and return the capture list.

    Each entry in the list is the kwargs dict the production code passed
    to ``audit.log``. If ``raise_exc`` is given, the spy raises it after
    capturing — used to prove ``discard_and_recreate`` survives audit
    failures (best-effort contract)."""
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
            "session_id": session_id,
        })
        if raise_exc is not None:
            raise raise_exc
        return None  # match real signature: row id or None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)
    return captured


# ---------------------------------------------------------------------------
# 14) Audit row is emitted on every successful recreate
# ---------------------------------------------------------------------------


def test_audit_log_emitted_on_recreate(provisioned, monkeypatch):
    captured = _spy_audit_log(monkeypatch)

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8",
        anchor_sha=provisioned.anchor_sha,
        reason="ai-timeout",
    ))

    matching = [c for c in captured if c["action"] == "retry.worktree_recreated"]
    assert len(matching) == 1, (
        f"expected exactly one retry.worktree_recreated audit row, "
        f"got {captured}"
    )
    row = matching[0]
    assert row["entity_kind"] == "workspace"
    assert row["entity_id"] == "agent-r8"


# ---------------------------------------------------------------------------
# 15) Audit payload carries old_worktree_path / anchor_sha / reason
# ---------------------------------------------------------------------------


def test_audit_log_payload_includes_old_path_anchor_reason(
    provisioned, monkeypatch,
):
    """TODO row 2874 contract: '附 old_worktree_path / anchor_sha / reason'.
    All three must be retrievable from the audit row's before/after
    payload (so a forensics query against ``audit_log`` can answer
    'which workspace path was discarded, what anchor was it rebuilt at,
    and why')."""
    captured = _spy_audit_log(monkeypatch)
    info = provisioned
    anchor = info.anchor_sha
    expected_path = str(info.path)
    # Make agent commits past the anchor so old_branch_tip != anchor —
    # proves the snapshot captures the *pre-discard* tip, not the post-
    # recreate HEAD.
    old_tip = _agent_commit(info.path)

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8",
        anchor_sha=anchor,
        reason="operator-rollback",
    ))

    row = next(c for c in captured if c["action"] == "retry.worktree_recreated")
    before = row["before"] or {}
    after = row["after"] or {}

    # `before` must surface the discarded worktree's path + the SHA the
    # branch tip was at *before* discard (so audit history can answer
    # "what was on that branch when we threw it away").
    assert before.get("worktree_path") == expected_path
    assert before.get("branch_tip") == old_tip
    assert before.get("branch") == info.branch

    # `after` must carry the anchor SHA HEAD now points at + the reason
    # label + the same logical worktree path (same-path-reuse design).
    assert after.get("worktree_path") == expected_path
    assert after.get("anchor_sha") == anchor
    assert after.get("branch") == info.branch
    assert after.get("reason") == "operator-rollback"


# ---------------------------------------------------------------------------
# 16) Default reason (no kwarg) lands in the audit payload as "retry"
# ---------------------------------------------------------------------------


def test_audit_log_default_reason_is_retry_in_payload(provisioned, monkeypatch):
    captured = _spy_audit_log(monkeypatch)

    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=provisioned.anchor_sha,
    ))

    row = next(c for c in captured if c["action"] == "retry.worktree_recreated")
    assert (row["after"] or {}).get("reason") == "retry"


# ---------------------------------------------------------------------------
# 17) Audit row is written *after* the worktree exists at the anchor
# ---------------------------------------------------------------------------


def test_audit_log_emitted_only_after_recreate_succeeds(provisioned, monkeypatch):
    """Audit semantics: 'retry.worktree_recreated' means the recreate
    *succeeded*. If we wrote it before ``git worktree add`` finished we'd
    be lying when the add failed. Spy proves the call site happens after
    HEAD == anchor."""
    seen_head_at_audit_time: list[str] = []
    from backend import audit as _audit

    async def _spy(action, entity_kind, entity_id, **_):
        if action == "retry.worktree_recreated":
            head = _git("rev-parse", "HEAD", cwd=provisioned.path)
            seen_head_at_audit_time.append(head)
        return None

    monkeypatch.setattr(_audit, "log", _spy, raising=True)

    _agent_commit(provisioned.path)
    asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=provisioned.anchor_sha,
    ))

    assert seen_head_at_audit_time == [provisioned.anchor_sha]


# ---------------------------------------------------------------------------
# 18) Audit failures must NOT break the retry primitive (best-effort)
# ---------------------------------------------------------------------------


def test_audit_log_failure_does_not_break_recreate(provisioned, monkeypatch):
    """``audit.log`` swallows its own exceptions internally (audit.py:179),
    but if a future maintainer accidentally lifts that try/except (or a
    new caller wraps audit in something synchronous that re-raises),
    discard_and_recreate must still succeed — the retry path is itself
    a recovery path; failing recovery on a failed receipt is the worst-
    case escalation. We force the spy to raise to prove the contract.

    Note: the production call site relies on audit.log's *own* internal
    swallowing. This test simulates a regression where that swallowing
    was lost, and asserts our outer code path is robust."""
    _spy_audit_log(monkeypatch, raise_exc=RuntimeError("simulated audit outage"))

    # If discard_and_recreate doesn't survive audit failure, this raises.
    # We don't explicitly catch — pytest.raises would also fail this test
    # because we're asserting the *opposite*.
    info = asyncio.run(ws_mod.discard_and_recreate(
        agent_id="agent-r8", anchor_sha=provisioned.anchor_sha,
    ))

    # Recreate side effects must all be in place even though audit failed.
    assert info.status == "active"
    assert info.commit_count == 0
    assert _git("rev-parse", "HEAD", cwd=info.path) == provisioned.anchor_sha

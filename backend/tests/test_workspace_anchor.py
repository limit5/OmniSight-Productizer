"""R8 #314 — Anchor commit SHA capture at workspace provision time.

Covers the first row of the R8 sub-bullets in TODO.md:
"Anchor commit 機制：task 開始前記錄 ``anchor_commit_sha``（寫入 CATC metadata）；
retry 時 fresh worktree 從此 SHA 分支".

The retry path (``WorkspaceManager.discard_and_recreate``) and audit trail are
later sub-bullets — this file rigorously locks the contract that anchor_sha is:

  1. captured immediately after ``git worktree add``
  2. equal to ``git rev-parse HEAD`` of the freshly provisioned workspace
  3. immutable across subsequent agent commits on the branch
  4. round-trippable through CATC ``Navigation`` and ``AgentWorkspace``
  5. validated as a hex git SHA when present (rejects garbage)

The CATC field is Optional during the 30-day migration window per
``docs/design/r8-idempotent-retry-worktree.md`` §5; legacy payloads that omit
``anchor_commit_sha`` must continue to validate.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend import workspace as ws_mod
from backend.catc import Navigation, TaskCard
from backend.models import AgentWorkspace


# ---------------------------------------------------------------------------
# Fixtures: throwaway git repo + redirected workspaces root
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
    the real ``.agent_workspaces`` of the project repo."""
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


# ---------------------------------------------------------------------------
# 1) provision() captures anchor_sha == HEAD of the new worktree
# ---------------------------------------------------------------------------


def test_provision_captures_anchor_sha(fake_repo, redirected_ws_root):
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-anchor-1",
        task_id="task-anchor-1",
        repo_source=str(fake_repo),
    ))
    try:
        assert info.anchor_sha is not None, "anchor_sha must be captured"
        assert len(info.anchor_sha) == 40, "expected full 40-char SHA"
        assert all(c in "0123456789abcdef" for c in info.anchor_sha)

        # Anchor must equal the HEAD of the freshly provisioned worktree.
        head_in_ws = _git("rev-parse", "HEAD", cwd=info.path)
        assert info.anchor_sha == head_in_ws

        # And it must equal HEAD of the source repo (worktree branched off it).
        head_in_src = _git("rev-parse", "HEAD", cwd=fake_repo)
        assert info.anchor_sha == head_in_src
    finally:
        asyncio.run(ws_mod.cleanup("agent-anchor-1"))


# ---------------------------------------------------------------------------
# 2) anchor_sha is immutable across subsequent agent commits on the branch
# ---------------------------------------------------------------------------


def test_anchor_sha_does_not_drift_when_agent_commits(fake_repo, redirected_ws_root):
    """Per design doc §4: 'anchor 在整個任務生命週期不變（即使 agent 之後
    commit 了新的 work，anchor 依然指向 provision 那一刻的純淨起點）'.

    The contract is on ``WorkspaceInfo.anchor_sha`` being stored once and not
    mutated by anything else in this module. We verify by simulating an agent
    commit inside the worktree, then re-reading registry state.
    """
    info = asyncio.run(ws_mod.provision(
        agent_id="agent-anchor-drift",
        task_id="task-anchor-drift",
        repo_source=str(fake_repo),
    ))
    try:
        original_anchor = info.anchor_sha
        assert original_anchor

        (info.path / "agent_work.txt").write_text("agent did stuff\n")
        _git("add", "agent_work.txt", cwd=info.path)
        _git("commit", "-q", "-m", "agent commit", cwd=info.path)

        # The branch tip moved.
        new_head = _git("rev-parse", "HEAD", cwd=info.path)
        assert new_head != original_anchor

        # But the registry still holds the original anchor.
        registry_info = ws_mod.get_workspace("agent-anchor-drift")
        assert registry_info is not None
        assert registry_info.anchor_sha == original_anchor
    finally:
        asyncio.run(ws_mod.cleanup("agent-anchor-drift"))


# ---------------------------------------------------------------------------
# 3) CATC Navigation accepts / validates / round-trips anchor_commit_sha
# ---------------------------------------------------------------------------


_VALID_NAV_PAYLOAD = {
    "entry_point": "src/x.cpp",
    "impact_scope": {"allowed": ["src/x.cpp"], "forbidden": []},
}


def test_catc_navigation_accepts_anchor_commit_sha():
    nav = Navigation.model_validate({
        **_VALID_NAV_PAYLOAD,
        "anchor_commit_sha": "ea6ae947a13b2f81c4ee3a9c7d3e8b4f1c5d2e6f",
    })
    assert nav.anchor_commit_sha == "ea6ae947a13b2f81c4ee3a9c7d3e8b4f1c5d2e6f"


def test_catc_navigation_anchor_optional_for_legacy_rows():
    """Legacy CATC payloads predating R8 omit ``anchor_commit_sha`` — they
    must continue to validate (Optional, default None) so we don't break
    existing queue messages during the 30-day migration window."""
    nav = Navigation.model_validate(_VALID_NAV_PAYLOAD)
    assert nav.anchor_commit_sha is None


def test_catc_navigation_rejects_non_hex_anchor():
    with pytest.raises(ValidationError):
        Navigation.model_validate({
            **_VALID_NAV_PAYLOAD,
            "anchor_commit_sha": "not-a-sha-XYZ!",
        })


def test_catc_navigation_rejects_too_short_anchor():
    """6-char anchor is rejected; 7-char abbreviated SHA is accepted."""
    with pytest.raises(ValidationError):
        Navigation.model_validate({
            **_VALID_NAV_PAYLOAD,
            "anchor_commit_sha": "abcdef",
        })

    nav = Navigation.model_validate({
        **_VALID_NAV_PAYLOAD,
        "anchor_commit_sha": "abcdef0",
    })
    assert nav.anchor_commit_sha == "abcdef0"


def test_catc_taskcard_roundtrips_anchor_commit_sha():
    payload = {
        "jira_ticket": "PROJ-314",
        "acceptance_criteria": "anchor wired",
        "navigation": {
            **_VALID_NAV_PAYLOAD,
            "anchor_commit_sha": "ea6ae947a13b",
        },
    }
    card = TaskCard.from_dict(payload)
    re_card = TaskCard.from_json(card.to_json())
    assert re_card.navigation.anchor_commit_sha == "ea6ae947a13b"


# ---------------------------------------------------------------------------
# 4) AgentWorkspace persistence model carries anchor_sha
# ---------------------------------------------------------------------------


def test_agent_workspace_model_serialises_anchor():
    ws = AgentWorkspace(
        branch="agent/x/y",
        path="/tmp/x",
        status="active",
        anchor_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    )
    j = ws.model_dump()
    assert j["anchor_sha"] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    # Legacy serialised row (no anchor_sha key) deserialises with None.
    legacy = AgentWorkspace.model_validate(
        {"branch": "x", "path": "/y", "status": "active"},
    )
    assert legacy.anchor_sha is None

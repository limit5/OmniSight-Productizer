"""Y6 #282 row 1 — Five-layer workspace hierarchy + repo_url_hash leaf.

Locks the contract introduced by the first sub-bullet under Y6 in TODO.md:

    新路徑：``{OMNISIGHT_WORKSPACE_ROOT}/{tenant_id}/{product_line}/
    {project_id}/{agent_id}/{repo_url_hash}/`` 五層階層。
    ``repo_url_hash = sha256(remote_url)[:16]`` 防同 agent clone 多個
    同名 repo 互相覆蓋。

The other Y6 sub-bullets (config knobs, ContextVar wiring on ``provision``,
migration script, quota integration, GC reaper) are tracked separately —
this file only asserts the path scheme + the collision-avoidance property.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from backend import workspace as ws_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _make_repo(path: Path, file_content: str = "hello\n") -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "README.md").write_text(file_content)
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "initial", cwd=path)
    return path


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "src_repo")


@pytest.fixture
def redirected_ws_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "ws_root"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    return root


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)


# ---------------------------------------------------------------------------
# 1) Pure helper — repo_url_hash is sha256(remote_url)[:16]
# ---------------------------------------------------------------------------


def test_repo_url_hash_is_sha256_prefix_16():
    url = "https://github.com/octocat/Hello-World.git"
    expected = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    assert ws_mod._repo_url_hash(url) == expected
    assert len(ws_mod._repo_url_hash(url)) == 16
    # Stable across calls.
    assert ws_mod._repo_url_hash(url) == ws_mod._repo_url_hash(url)


def test_repo_url_hash_collapses_empty_to_self_sentinel():
    """In-repo worktree (no external remote URL) must collapse to a stable
    sentinel so a single agent_id keeps a single in-repo workspace."""
    assert ws_mod._repo_url_hash(None) == ws_mod._SELF_REPO_HASH
    assert ws_mod._repo_url_hash("") == ws_mod._SELF_REPO_HASH


def test_repo_url_hash_different_urls_produce_different_hashes():
    a = "https://github.com/teamA/foo.git"
    b = "https://gitlab.com/teamB/foo.git"  # same basename "foo", different host
    assert ws_mod._repo_url_hash(a) != ws_mod._repo_url_hash(b)


# ---------------------------------------------------------------------------
# 2) Pure helper — _workspace_path_for builds the 5-layer layout
# ---------------------------------------------------------------------------


def test_workspace_path_for_full_hierarchy(redirected_ws_root):
    path = ws_mod._workspace_path_for(
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-42",
        agent_id="agent-x",
        remote_url="https://github.com/acme/foo.git",
    )
    expected_hash = ws_mod._repo_url_hash("https://github.com/acme/foo.git")
    assert path == (
        redirected_ws_root
        / "t-acme" / "cameras" / "proj-42" / "agent-x" / expected_hash
    )


def test_workspace_path_for_defaults_when_keys_missing(redirected_ws_root):
    """Until Y6 row 3 wires ContextVars through ``provision``, callers
    that don't supply tenant / product_line / project_id must still
    end up under a stable transitional namespace rather than at the
    root."""
    path = ws_mod._workspace_path_for(
        tenant_id=None,
        product_line=None,
        project_id=None,
        agent_id="agent-1",
        remote_url=None,
    )
    assert path == (
        redirected_ws_root
        / ws_mod._DEFAULT_TENANT_ID
        / ws_mod._DEFAULT_PRODUCT_LINE
        / ws_mod._DEFAULT_PROJECT_ID
        / "agent-1"
        / ws_mod._SELF_REPO_HASH
    )


def test_workspace_path_for_sanitises_unsafe_components(redirected_ws_root):
    """Pathological tenant slug must NOT escape ``_WORKSPACES_ROOT`` —
    the safe-component pass strips ``..`` / ``/`` / shell metachars."""
    path = ws_mod._workspace_path_for(
        tenant_id="../../escape",
        product_line="cams;rm -rf /",
        project_id="p$1",
        agent_id="agent\nx",
        remote_url="https://example.com/r.git",
    )
    # Every component is regex-sanitised, so the path stays nested under root.
    assert path.is_relative_to(redirected_ws_root)
    parts = path.relative_to(redirected_ws_root).parts
    assert len(parts) == 5
    for part in parts:
        # Only [A-Za-z0-9_-] survives the sanitiser.
        assert all(c.isalnum() or c in "_-" for c in part)


# ---------------------------------------------------------------------------
# 3) provision() materialises the workspace under the new 5-layer layout
# ---------------------------------------------------------------------------


def test_provision_creates_five_layer_path(fake_repo, redirected_ws_root):
    info = asyncio.run(ws_mod.provision(
        agent_id="hier-agent-1",
        task_id="task-1",
        repo_source=str(fake_repo),
    ))
    try:
        rel = info.path.relative_to(redirected_ws_root).parts
        assert len(rel) == 5, f"expected 5-layer layout, got {rel}"
        # Default tenant / product_line / project_id until Y6 rows 2-3 land.
        assert rel[0] == ws_mod._DEFAULT_TENANT_ID
        assert rel[1] == ws_mod._DEFAULT_PRODUCT_LINE
        assert rel[2] == ws_mod._DEFAULT_PROJECT_ID
        assert rel[3] == "hier-agent-1"
        # Leaf is the URL hash for this fake_repo source.
        assert rel[4] == ws_mod._repo_url_hash(str(fake_repo))
        # Workspace dir is real and is a checked-out worktree.
        assert info.path.is_dir()
        assert (info.path / "README.md").is_file()
    finally:
        asyncio.run(ws_mod.cleanup("hier-agent-1"))


# ---------------------------------------------------------------------------
# 4) Same agent + two different remote URLs → two distinct sub-dirs
#    (this is the precise "防同 agent clone 多個同名 repo 互相覆蓋" bug)
# ---------------------------------------------------------------------------


def test_same_agent_different_urls_get_distinct_workspaces(
    tmp_path, redirected_ws_root,
):
    """Two repos with the SAME basename ('foo') but DIFFERENT URLs.

    Pre-Y6 the path was ``_WORKSPACES_ROOT/{agent_id}/`` so the second
    provision silently rm'd + replaced the first. Now they live under
    different ``{repo_url_hash}`` leaves and coexist. The agent id and
    everything above the leaf is identical between the two; only the
    hash leaf disambiguates."""
    repo_a = _make_repo(tmp_path / "team_a" / "foo", file_content="from team A\n")
    repo_b = _make_repo(tmp_path / "team_b" / "foo", file_content="from team B\n")
    assert repo_a.name == repo_b.name == "foo"  # confirm test premise

    info_a = asyncio.run(ws_mod.provision(
        agent_id="multi-repo-agent",
        task_id="task-a",
        repo_source=str(repo_a),
    ))
    # Drop the registry entry without removing the worktree on disk —
    # mimics "agent finished task A, kept its worktree, now starts task
    # B in a different repo". provision() would also auto-cleanup the
    # in-process entry but only for the same agent_id; here we want
    # both worktrees to coexist on disk regardless of registry state.
    snapshot_a = info_a.path
    ws_mod._workspaces.pop("multi-repo-agent", None)

    info_b = asyncio.run(ws_mod.provision(
        agent_id="multi-repo-agent",
        task_id="task-b",
        repo_source=str(repo_b),
    ))
    try:
        assert info_b.path != snapshot_a, (
            "different remote URLs must produce different leaf dirs; "
            f"both resolved to {info_b.path}"
        )
        # Both leaves share the same {tid}/{pl}/{pid}/{agent_id} parent,
        # only the hash differs — that's the property under test.
        assert info_b.path.parent == snapshot_a.parent
        assert info_b.path.name != snapshot_a.name
        # Both worktrees are still checked-out and contain their
        # respective README.md content (no overwrite / no merge).
        assert snapshot_a.is_dir()
        assert info_b.path.is_dir()
        assert (snapshot_a / "README.md").read_text() == "from team A\n"
        assert (info_b.path / "README.md").read_text() == "from team B\n"
    finally:
        asyncio.run(ws_mod.cleanup("multi-repo-agent"))


# ---------------------------------------------------------------------------
# 5) Same agent + same URL twice → idempotent (single leaf)
# ---------------------------------------------------------------------------


def test_same_agent_same_url_yields_same_path(redirected_ws_root):
    """Hash is content-addressed by URL; deriving twice with the same
    inputs yields the identical leaf each time. This is what makes
    same-URL re-provisioning idempotent and what makes the GC reaper
    (Y6 row 5) able to LRU-rank workspaces by ``mtime`` of a stable
    path.
    """
    kw = dict(
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-x",
        agent_id="idem-agent",
        remote_url="https://github.com/acme/foo.git",
    )
    p1 = ws_mod._workspace_path_for(**kw)
    p2 = ws_mod._workspace_path_for(**kw)
    assert p1 == p2


# ---------------------------------------------------------------------------
# 6) Workspace dir is nested below _WORKSPACES_ROOT — does not escape
# ---------------------------------------------------------------------------


def test_workspace_path_is_strictly_under_root(fake_repo, redirected_ws_root):
    info = asyncio.run(ws_mod.provision(
        agent_id="containment-agent",
        task_id="task-c",
        repo_source=str(fake_repo),
    ))
    try:
        # Strictly under root, never == root, never escaping via ``..``.
        assert info.path.is_relative_to(redirected_ws_root)
        assert info.path.resolve() != redirected_ws_root.resolve()
    finally:
        asyncio.run(ws_mod.cleanup("containment-agent"))

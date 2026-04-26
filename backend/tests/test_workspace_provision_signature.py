"""Y6 #282 row 3 — ``provision()`` five-context signature + ContextVar
defaults + ``repo_source=`` deprecation shim.

Locks the contract introduced by row 3 under Y6 in TODO.md:

    `backend/workspace.py` 改 `provision()` 接受
    `tenant_id / product_line / project_id / agent_id / remote_url`
    五個參數，從 ContextVar 預設讀取。舊 callsite 相容 shim 標記
    deprecated 並記 log（下個 release 刪）。

Sibling test files cover other Y6 sub-bullets:

* ``test_workspace_hierarchy.py`` — row 1 path layout + collision avoidance.
* ``test_workspace_anchor.py`` — R8 anchor capture (cross-cutting with row 3).
* ``test_workspace_orphan_cleanup.py`` — startup orphan scan.

This file ONLY asserts row-3 behaviour: signature shape, ContextVar
defaults, explicit-kwarg overrides, and the legacy-shim deprecation
contract.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import warnings
from pathlib import Path

import pytest

from backend import workspace as ws_mod
from backend import db_context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True,
        stderr=subprocess.STDOUT,
    ).strip()


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@local", cwd=path)
    _git("config", "user.name", "test", cwd=path)
    (path / "README.md").write_text("hello\n")
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


@pytest.fixture(autouse=True)
def reset_contextvars():
    """ContextVars are per-asyncio-Task so they normally don't leak,
    but pytest may run setup outside the task scope. Snapshot + restore
    so a test that sets a ContextVar can't leak into the next."""
    tok_t = db_context._tenant_var.set(None)
    tok_p = db_context._project_var.set(None)
    try:
        yield
    finally:
        db_context._tenant_var.reset(tok_t)
        db_context._project_var.reset(tok_p)


# ---------------------------------------------------------------------------
# 1) Required-arg validation: agent_id + task_id must be provided.
# ---------------------------------------------------------------------------


def test_provision_requires_agent_id():
    with pytest.raises(TypeError, match="agent_id"):
        asyncio.run(ws_mod.provision(task_id="t"))


def test_provision_requires_task_id():
    with pytest.raises(TypeError, match="task_id"):
        asyncio.run(ws_mod.provision(agent_id="a"))


# ---------------------------------------------------------------------------
# 2) Explicit five-context kwargs land in the on-disk path verbatim.
# ---------------------------------------------------------------------------


def test_provision_uses_explicit_five_context(fake_repo, redirected_ws_root):
    """Calling with all five new kwargs explicit pins each component
    of the on-disk hierarchy. No ContextVar is set, so the explicit
    values are the ONLY source — verifies the function actually wires
    the kwargs through to ``_workspace_path_for``."""
    info = asyncio.run(ws_mod.provision(
        agent_id="explicit-agent",
        task_id="task-1",
        remote_url=str(fake_repo),
        tenant_id="t-acme",
        product_line="cameras",
        project_id="proj-7",
    ))
    try:
        rel = info.path.relative_to(redirected_ws_root).parts
        assert rel[0] == "t-acme"
        assert rel[1] == "cameras"
        assert rel[2] == "proj-7"
        assert rel[3] == "explicit-agent"
        # leaf is sha256(remote_url)[:16] — non-empty + 16 chars
        assert len(rel[4]) == 16
        assert rel[4] == ws_mod._repo_url_hash(str(fake_repo))
    finally:
        asyncio.run(ws_mod.cleanup("explicit-agent"))


# ---------------------------------------------------------------------------
# 3) ContextVar defaults — tenant_id and project_id read from db_context
#    when not passed explicitly.
# ---------------------------------------------------------------------------


def test_provision_reads_tenant_id_from_contextvar(fake_repo, redirected_ws_root):
    """When ``tenant_id`` is omitted but ``current_tenant_id()`` is set
    in scope, the workspace lands under the ContextVar tenant — proving
    the row-3 ContextVar wiring is live, not just a dead default."""
    db_context.set_tenant_id("t-from-ctx")
    try:
        info = asyncio.run(ws_mod.provision(
            agent_id="ctx-tenant-agent",
            task_id="task-ctx",
            remote_url=str(fake_repo),
            # tenant_id NOT passed
        ))
        try:
            rel = info.path.relative_to(redirected_ws_root).parts
            assert rel[0] == "t-from-ctx", (
                f"expected tenant_id from ContextVar, got {rel[0]}"
            )
        finally:
            asyncio.run(ws_mod.cleanup("ctx-tenant-agent"))
    finally:
        db_context.set_tenant_id(None)


def test_provision_reads_project_id_from_contextvar(fake_repo, redirected_ws_root):
    db_context.set_project_id("proj-from-ctx")
    try:
        info = asyncio.run(ws_mod.provision(
            agent_id="ctx-project-agent",
            task_id="task-ctx",
            remote_url=str(fake_repo),
            # project_id NOT passed
        ))
        try:
            rel = info.path.relative_to(redirected_ws_root).parts
            assert rel[2] == "proj-from-ctx", (
                f"expected project_id from ContextVar, got {rel[2]}"
            )
        finally:
            asyncio.run(ws_mod.cleanup("ctx-project-agent"))
    finally:
        db_context.set_project_id(None)


def test_provision_reads_both_contextvars_together(fake_repo, redirected_ws_root):
    """The combined-context happy-path: a request scope where both
    ``require_tenant`` and ``require_project_member`` have run sets
    BOTH ContextVars, and ``provision()`` picks them up together."""
    db_context.set_tenant_id("t-combined")
    db_context.set_project_id("proj-combined")
    try:
        info = asyncio.run(ws_mod.provision(
            agent_id="combined-agent",
            task_id="task-combined",
            remote_url=str(fake_repo),
        ))
        try:
            rel = info.path.relative_to(redirected_ws_root).parts
            assert rel[0] == "t-combined"
            assert rel[2] == "proj-combined"
        finally:
            asyncio.run(ws_mod.cleanup("combined-agent"))
    finally:
        db_context.set_tenant_id(None)
        db_context.set_project_id(None)


# ---------------------------------------------------------------------------
# 4) Explicit kwarg WINS over ContextVar — caller can override.
# ---------------------------------------------------------------------------


def test_explicit_tenant_kwarg_overrides_contextvar(fake_repo, redirected_ws_root):
    """Operator override (e.g. cross-tenant admin tool) can route the
    workspace to a tenant *other* than the one ``current_tenant_id()``
    reports. Explicit kwarg always wins so that admin tooling can
    bypass the ambient context safely."""
    db_context.set_tenant_id("t-ambient")
    try:
        info = asyncio.run(ws_mod.provision(
            agent_id="override-agent",
            task_id="task-override",
            remote_url=str(fake_repo),
            tenant_id="t-explicit",  # explicit wins
        ))
        try:
            rel = info.path.relative_to(redirected_ws_root).parts
            assert rel[0] == "t-explicit"
        finally:
            asyncio.run(ws_mod.cleanup("override-agent"))
    finally:
        db_context.set_tenant_id(None)


# ---------------------------------------------------------------------------
# 5) Defaults — no ContextVar, no kwarg → transitional ``_DEFAULT_*``
#    constants. Mirrors row-1 behaviour for legacy callsites.
# ---------------------------------------------------------------------------


def test_provision_falls_through_to_default_constants(fake_repo, redirected_ws_root):
    info = asyncio.run(ws_mod.provision(
        agent_id="default-agent",
        task_id="task-default",
        remote_url=str(fake_repo),
    ))
    try:
        rel = info.path.relative_to(redirected_ws_root).parts
        assert rel[0] == ws_mod._DEFAULT_TENANT_ID
        assert rel[1] == ws_mod._DEFAULT_PRODUCT_LINE
        assert rel[2] == ws_mod._DEFAULT_PROJECT_ID
    finally:
        asyncio.run(ws_mod.cleanup("default-agent"))


# ---------------------------------------------------------------------------
# 6) Deprecation shim — ``repo_source=`` kwarg still works, emits
#    DeprecationWarning + logger.warning, value lands in remote_url.
# ---------------------------------------------------------------------------


def test_repo_source_kwarg_emits_deprecation_warning(fake_repo, redirected_ws_root):
    """The deprecation shim must emit a real ``DeprecationWarning`` that
    a Python warning filter (or pytest's ``-W error::DeprecationWarning``)
    can intercept. Without this guard the shim could silently rot."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        info = asyncio.run(ws_mod.provision(
            agent_id="shim-agent-1",
            task_id="task-shim",
            repo_source=str(fake_repo),
        ))
        try:
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert dep_warnings, "expected a DeprecationWarning for repo_source="
            assert "repo_source" in str(dep_warnings[0].message)
            assert "remote_url" in str(dep_warnings[0].message)
        finally:
            asyncio.run(ws_mod.cleanup("shim-agent-1"))


def test_repo_source_kwarg_emits_logger_warning(fake_repo, redirected_ws_root, caplog):
    """The shim must also log via ``logger.warning`` so production log
    aggregators (which usually drop Python warnings) can see the
    deprecation. Audit-line readers grep for ``DEPRECATED provision()``.
    """
    caplog.set_level(logging.WARNING, logger="backend.workspace")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        info = asyncio.run(ws_mod.provision(
            agent_id="shim-agent-2",
            task_id="task-shim",
            repo_source=str(fake_repo),
        ))
    try:
        deprecated_msgs = [
            r for r in caplog.records
            if "DEPRECATED provision()" in r.getMessage()
        ]
        assert deprecated_msgs, (
            "expected a logger.warning containing "
            "'DEPRECATED provision()' from the shim"
        )
    finally:
        asyncio.run(ws_mod.cleanup("shim-agent-2"))


def test_repo_source_kwarg_remaps_to_remote_url(fake_repo, redirected_ws_root):
    """Functional contract: ``repo_source=URL`` produces the same
    workspace layout as ``remote_url=URL`` (same hash leaf, same
    materialised worktree). The shim must be a pure remap, not a
    different code path."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        info_legacy = asyncio.run(ws_mod.provision(
            agent_id="shim-remap-1",
            task_id="task-1",
            repo_source=str(fake_repo),
        ))
    try:
        legacy_path = info_legacy.path
        legacy_leaf = legacy_path.name
    finally:
        asyncio.run(ws_mod.cleanup("shim-remap-1"))

    info_canonical = asyncio.run(ws_mod.provision(
        agent_id="shim-remap-2",
        task_id="task-2",
        remote_url=str(fake_repo),
    ))
    try:
        # Same URL → same hash leaf; only the agent_id differs in the path.
        assert info_canonical.path.name == legacy_leaf
        # And the worktree is real with the source repo's content.
        assert (info_canonical.path / "README.md").read_text() == "hello\n"
    finally:
        asyncio.run(ws_mod.cleanup("shim-remap-2"))


def test_remote_url_wins_when_both_kwargs_passed(fake_repo, tmp_path, redirected_ws_root):
    """Mid-migration callers may pass both kwargs at once. The
    canonical ``remote_url`` wins (per the docstring): the workspace
    must be derived from ``remote_url``, not ``repo_source``. If we
    silently remapped the WRONG way, callers in transition would
    accidentally roll back to the legacy URL on every call."""
    other_repo = _make_repo(tmp_path / "other_repo")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        info = asyncio.run(ws_mod.provision(
            agent_id="both-kwargs-agent",
            task_id="task-both",
            remote_url=str(other_repo),
            repo_source=str(fake_repo),
        ))
    try:
        # Leaf hash is the canonical ``remote_url`` URL's hash.
        assert info.path.name == ws_mod._repo_url_hash(str(other_repo))
        assert info.path.name != ws_mod._repo_url_hash(str(fake_repo))
    finally:
        asyncio.run(ws_mod.cleanup("both-kwargs-agent"))


# ---------------------------------------------------------------------------
# 7) Three-positional legacy form keeps working WITHOUT a deprecation —
#    the third positional slot is now ``remote_url`` (canonical), so
#    callers like ``backend/routers/workspaces.py`` ``provision(body.agent_id,
#    body.task_id, body.repo_url)`` are not legacy.
# ---------------------------------------------------------------------------


def test_three_positional_args_do_not_warn(fake_repo, redirected_ws_root):
    """The third positional has been renamed ``repo_source`` →
    ``remote_url``; same slot, new canonical name. A caller passing
    the URL positionally was already supplying ``remote_url`` by
    position semantics and must not emit a deprecation."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        info = asyncio.run(ws_mod.provision(
            "positional-agent",
            "task-pos",
            str(fake_repo),
        ))
        try:
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert not dep_warnings, (
                f"three-positional form must not deprecate; got {dep_warnings}"
            )
            # And the workspace was actually materialised under the
            # supplied remote_url.
            assert info.path.name == ws_mod._repo_url_hash(str(fake_repo))
        finally:
            asyncio.run(ws_mod.cleanup("positional-agent"))


# ---------------------------------------------------------------------------
# 8) Two-positional legacy form (no remote_url) — in-repo worktree,
#    no deprecation. Mirrors the ``invoke.py`` callsite.
# ---------------------------------------------------------------------------


def test_two_positional_args_use_in_repo_worktree(
    fake_repo, redirected_ws_root, monkeypatch,
):
    """``ws_provision(action["agent_id"], action["task_id"])`` in
    ``invoke.py`` doesn't pass any URL. The function must default
    to the in-repo worktree (collapses to ``_SELF_REPO_HASH`` leaf)
    and emit no deprecation — that callsite is on the canonical API
    already, just relies on defaults.

    Note: ``_MAIN_REPO`` is monkey-patched to ``fake_repo`` so the
    test's ``git worktree add`` does NOT write a worktree admin
    block under the real project's ``.git/`` (which would also
    leak ``git config user.name "Agent-..."`` into the project's
    ``.git/config`` because git config inherits from the host
    repo for worktrees).
    """
    monkeypatch.setattr(ws_mod, "_MAIN_REPO", fake_repo, raising=True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        info = asyncio.run(ws_mod.provision(
            agent_id="twoargs-agent",
            task_id="task-2args",
        ))
        try:
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            assert not dep_warnings
            # Leaf is the ``_SELF_REPO_HASH`` sentinel for in-repo worktrees.
            assert info.path.name == ws_mod._SELF_REPO_HASH
        finally:
            asyncio.run(ws_mod.cleanup("twoargs-agent"))

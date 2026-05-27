"""OP-1778 (1A.1) — tenant-label binding + per-tenant workspace tests.

Covers :mod:`backend.agents.runner_tenant` (label → tenant id resolution,
self vs customer workspace allocation, object-store isolation assertions) and
the runner wiring that sources the RAG tenant from the bound tenant context.

The git-clone integration test is skipped when ``git`` is unavailable; the
argv-shape + isolation-assertion tests cover the same contract without a real
subprocess.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend import db_context, tenant_fs
from backend.agents import runner_tenant as rt


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "auto-runner-jira.py"
_HAS_GIT = shutil.which("git") is not None


# ─── resolve_tenant_id ──────────────────────────────────────────────


def test_labelless_ticket_defaults_to_omnisight_self():
    assert rt.resolve_tenant_id([]) == "omnisight-self"
    assert rt.resolve_tenant_id(["area:backend", "tier:M"]) == "omnisight-self"


def test_self_default_is_not_the_customer_default():
    # Back-compat guard: an internal ticket must NEVER inherit the
    # filesystem customer-tenant default ("t-default").
    assert rt.OMNISIGHT_SELF_TENANT != tenant_fs._DEFAULT_TENANT
    assert rt.resolve_tenant_id([]) != tenant_fs._DEFAULT_TENANT


def test_tenant_label_resolves_to_tid():
    assert rt.resolve_tenant_id(["area:backend", "tenant:t-foo"]) == "t-foo"


def test_tenant_label_prefix_is_case_insensitive():
    assert rt.resolve_tenant_id(["Tenant:t-foo"]) == "t-foo"


def test_duplicate_identical_tenant_labels_collapse():
    assert rt.resolve_tenant_id(["tenant:t-foo", "tenant:t-foo"]) == "t-foo"


def test_conflicting_tenant_labels_raise():
    with pytest.raises(rt.TenantLabelError):
        rt.resolve_tenant_id(["tenant:t-foo", "tenant:t-bar"])


def test_malformed_tenant_id_raises():
    with pytest.raises(rt.TenantLabelError):
        rt.resolve_tenant_id(["tenant:bad/tid"])


def test_empty_tenant_id_raises():
    with pytest.raises(rt.TenantLabelError):
        rt.resolve_tenant_id(["tenant:"])


def test_is_self_tenant():
    assert rt.is_self_tenant("omnisight-self") is True
    assert rt.is_self_tenant("t-foo") is False


# ─── workspace path + clone argv ────────────────────────────────────


def test_tenant_workspace_path_lives_under_tenant_fs(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant_fs, "_TENANTS_ROOT", tmp_path / "tenants")
    p = rt.tenant_workspace_path("t-foo")
    assert p == tmp_path / "tenants" / "t-foo" / "workspace"


def test_build_clone_argv_forces_independent_object_store():
    argv = rt.build_clone_argv("/src/repo", "/dst/ws")
    # --no-local is the whole point: it forbids hardlink/alternates sharing.
    assert argv == ["git", "clone", "--no-local", "/src/repo", "/dst/ws"]


# ─── assert_isolated_git_dir ────────────────────────────────────────


def test_isolation_assert_passes_for_own_git_dir(tmp_path):
    (tmp_path / ".git" / "objects" / "info").mkdir(parents=True)
    rt.assert_isolated_git_dir(tmp_path)  # no raise


def test_isolation_assert_rejects_missing_git(tmp_path):
    with pytest.raises(rt.TenantWorkspaceError):
        rt.assert_isolated_git_dir(tmp_path)


def test_isolation_assert_rejects_gitlink_file(tmp_path):
    # `.git` as a file is the `git worktree` pattern — shared object store.
    (tmp_path / ".git").write_text("gitdir: /host/repo/.git/worktrees/x\n")
    with pytest.raises(rt.TenantWorkspaceError):
        rt.assert_isolated_git_dir(tmp_path)


def test_isolation_assert_rejects_alternates_bind(tmp_path):
    info = tmp_path / ".git" / "objects" / "info"
    info.mkdir(parents=True)
    (info / "alternates").write_text("/host/repo/.git/objects\n")
    with pytest.raises(rt.TenantWorkspaceError):
        rt.assert_isolated_git_dir(tmp_path)


# ─── allocate_tenant_workspace ──────────────────────────────────────


def test_allocate_self_returns_legacy_workspace_without_cloning():
    calls: list = []

    def fake_run(*a, **k):
        calls.append((a, k))
        return subprocess.CompletedProcess(a, 0)

    out = rt.allocate_tenant_workspace(
        "omnisight-self",
        source_repo="/src/repo",
        self_workspace="/host/claude-worktree",
        run=fake_run,
    )
    assert out == Path("/host/claude-worktree")
    assert calls == []  # back-compat: never clones for self


def test_allocate_customer_clones_then_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant_fs, "_TENANTS_ROOT", tmp_path / "tenants")
    dest = rt.tenant_workspace_path("t-foo")

    def fake_run(argv, **k):
        # Simulate a real clone landing an isolated git dir.
        assert argv == rt.build_clone_argv(str(tmp_path / "src"), dest)
        (Path(argv[-1]) / ".git" / "objects" / "info").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0)

    out = rt.allocate_tenant_workspace(
        "t-foo",
        source_repo=str(tmp_path / "src"),
        self_workspace="/unused",
        run=fake_run,
    )
    assert out == dest
    rt.assert_isolated_git_dir(out)  # post-condition holds


def test_allocate_customer_reuses_existing_isolated_clone(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant_fs, "_TENANTS_ROOT", tmp_path / "tenants")
    dest = rt.tenant_workspace_path("t-foo")
    (dest / ".git" / "objects" / "info").mkdir(parents=True)

    def boom_run(*a, **k):  # must NOT clone again
        raise AssertionError("re-cloned an existing isolated workspace")

    out = rt.allocate_tenant_workspace(
        "t-foo", source_repo="/src", self_workspace="/unused", run=boom_run,
    )
    assert out == dest


def test_allocate_customer_refuses_to_clobber_non_repo_cruft(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant_fs, "_TENANTS_ROOT", tmp_path / "tenants")
    dest = rt.tenant_workspace_path("t-foo")
    (dest).mkdir(parents=True)
    (dest / "leftover.txt").write_text("not a repo")

    with pytest.raises(rt.TenantWorkspaceError):
        rt.allocate_tenant_workspace(
            "t-foo", source_repo="/src", self_workspace="/unused",
            run=lambda *a, **k: pytest.fail("should not clone over cruft"),
        )


@pytest.mark.skipif(not _HAS_GIT, reason="git not available")
def test_real_clone_has_own_git_dir_and_no_alternates(tmp_path, monkeypatch):
    # End-to-end: a real `git clone --no-local` yields an isolated object
    # store (a directory .git, no alternates bind) — the L8-fs contract.
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)

    monkeypatch.setattr(tenant_fs, "_TENANTS_ROOT", tmp_path / "tenants")
    out = rt.allocate_tenant_workspace(
        "t-foo", source_repo=str(src), self_workspace="/unused",
    )
    assert (out / ".git").is_dir()
    assert not (out / ".git" / "objects" / "info" / "alternates").exists()
    assert (out / "f.txt").read_text() == "x"


# ─── runner wiring: RAG tenant sourced from bound context ────────────


def _load_jira_runner() -> Any:
    sys.modules.pop("jira_runner_tenant_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "jira_runner_tenant_under_test", _RUNNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reflection_rag_block_sources_tenant_from_bound_context(monkeypatch):
    from backend.agents import rag_indexer, reflection_rag

    mod = _load_jira_runner()

    monkeypatch.setenv("OMNISIGHT_REFLECTION_RAG_PROMPT", "1")
    monkeypatch.setattr(rag_indexer, "_build_embedder_from_env", lambda: object())

    async def _fake_store():
        return object(), None

    monkeypatch.setattr(rag_indexer, "_build_store_from_env", _fake_store)

    captured: dict = {}

    async def _fake_inject(*, tenant_id, **kwargs):
        captured["tenant_id"] = tenant_id
        return "REFLECTION-BLOCK"

    monkeypatch.setattr(
        reflection_rag, "build_reflection_lesson_injection", _fake_inject
    )

    token = db_context._tenant_var.set("t-foo")
    try:
        block = mod._build_reflection_rag_block("OP-1778", "sum", "desc")
    finally:
        db_context._tenant_var.reset(token)

    assert "REFLECTION-BLOCK" in block
    assert captured["tenant_id"] == "t-foo"  # from ticket, not env default


def test_reflection_rag_block_falls_back_to_env_when_no_tenant_bound(monkeypatch):
    from backend.agents import rag_indexer, reflection_rag

    mod = _load_jira_runner()
    monkeypatch.setenv("OMNISIGHT_RAG_TENANT_ID", "t-envfallback")
    monkeypatch.setenv("OMNISIGHT_REFLECTION_RAG_PROMPT", "1")
    monkeypatch.setattr(rag_indexer, "_build_embedder_from_env", lambda: object())

    async def _fake_store():
        return object(), None

    monkeypatch.setattr(rag_indexer, "_build_store_from_env", _fake_store)

    captured: dict = {}

    async def _fake_inject(*, tenant_id, **kwargs):
        captured["tenant_id"] = tenant_id
        return "BLK"

    monkeypatch.setattr(
        reflection_rag, "build_reflection_lesson_injection", _fake_inject
    )

    # No tenant bound in this context → env default path.
    db_context.set_tenant_id(None)
    mod._build_reflection_rag_block("OP-1778", "sum", "desc")
    assert captured["tenant_id"] == "t-envfallback"

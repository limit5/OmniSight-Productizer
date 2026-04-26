"""Y6 #282 row 5 — workspace bytes count toward tenant quota + provision enforce.

Locks the contract introduced by row 5 under Y6 in TODO.md::

    納入 tenant_quota：``backend/tenant_quota.py`` 的 ``check_hard_quota()``
    把 ``data/workspaces/{tid}/**`` 計入 ``used_bytes``；workspace 寫入時
    自動 enforce。

Sibling test files cover the other Y6 sub-bullets:

* ``test_workspace_hierarchy.py``        — row 1 path layout.
* ``test_workspace_provision_signature`` — row 3 ContextVar wiring.
* ``test_y6_row4_workspace_migration``   — row 4 legacy migrator.

This file ONLY asserts row-5 behaviour:

1. ``measure_tenant_usage`` includes ``workspaces_bytes`` and adds it to
   ``total_bytes``.
2. Per-tenant isolation — bytes under tenant A's workspace dir do **not**
   leak into tenant B's measurement.
3. ``check_hard_quota`` raises ``QuotaExceeded`` once the workspace
   footprint alone breaches the configured hard cap.
4. ``backend.workspace.provision`` calls into ``check_hard_quota`` and
   refuses to create a worktree when the tenant is already over hard.
5. The deny-path emits an ``audit_log`` row tagged
   ``workspace_quota_exceeded`` so operators can correlate failed
   provisions with quota state.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend import tenant_fs as tfs
from backend import tenant_quota as tq
from backend import workspace as ws_mod


# ---------------------------------------------------------------------------
# Shared fixtures — isolate tenants_root + workspaces_root onto tmp_path.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_roots(monkeypatch, tmp_path):
    """Redirect every quota-relevant root onto pytest's tmp_path.

    Same shape as ``test_tenant_quota.isolated_tenants`` but also rebases
    ``backend.workspace._WORKSPACES_ROOT`` so the row-5 measurement
    looks at the test's fake hierarchy and not the real
    ``./.agent_workspaces`` tree on the dev host.
    """
    tenants_dir = tmp_path / "tenants"
    ingest_dir = tmp_path / "tmp_ingest"
    workspaces_dir = tmp_path / "workspaces"
    tenants_dir.mkdir()
    ingest_dir.mkdir()
    workspaces_dir.mkdir()
    monkeypatch.setattr(tfs, "_TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(tfs, "_INGEST_BASE", ingest_dir)
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", workspaces_dir, raising=True)
    tq._reset_for_tests()
    yield tmp_path


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Always start each test with an empty workspace registry so a leak
    from a prior test cannot pretend to "own" a path that this test
    expects to be free."""
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True, stderr=subprocess.STDOUT,
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


def _seed_workspace_blob(tenant_id: str, *, size_bytes: int) -> Path:
    """Drop a fixed-size blob under ``{_WORKSPACES_ROOT}/{tid}/...``.

    Uses the same nested layout as ``_workspace_path_for`` so the
    measurement walk has a realistic shape to recurse over.
    """
    blob = (
        ws_mod._WORKSPACES_ROOT
        / tenant_id
        / ws_mod._DEFAULT_PRODUCT_LINE
        / ws_mod._DEFAULT_PROJECT_ID
        / "agent-x"
        / ws_mod._SELF_REPO_HASH
    )
    blob.mkdir(parents=True, exist_ok=True)
    (blob / "blob.bin").write_bytes(b"\0" * size_bytes)
    return blob


# ---------------------------------------------------------------------------
# 1) measure_tenant_usage exposes workspaces_bytes + folds it into total
# ---------------------------------------------------------------------------


class TestMeasureUsageIncludesWorkspaces:
    def test_empty_workspace_dir_zero_bytes(self, isolated_roots):
        u = tq.measure_tenant_usage("t-empty")
        assert u["workspaces_bytes"] == 0
        # Other axes also zero, so total = 0 too.
        assert u["total_bytes"] == 0

    def test_workspace_bytes_added_to_breakdown_and_total(self, isolated_roots):
        # 4 KiB artifact + 8 KiB workspace should sum into total.
        (tfs.tenant_artifacts_root("t-mix") / "f.bin").write_bytes(b"\0" * 4096)
        _seed_workspace_blob("t-mix", size_bytes=8192)
        u = tq.measure_tenant_usage("t-mix")
        assert u["artifacts_bytes"] == 4096
        assert u["workspaces_bytes"] == 8192
        # total = artifacts + workflow_runs + backups + ingest_tmp + workspaces
        assert u["total_bytes"] == 4096 + 8192

    def test_workspaces_bytes_isolated_per_tenant(self, isolated_roots):
        """The audit row's flagship promise: tenant A's workspace footprint
        must NOT show up in tenant B's measurement. The path scheme is
        ``{root}/{tid}/...`` so this falls out of correctness, but a
        regression that flattens / globs across tenants would break the
        whole multi-tenant disk story — pin the property explicitly."""
        _seed_workspace_blob("t-A", size_bytes=10_000)
        _seed_workspace_blob("t-B", size_bytes=2_000)
        a = tq.measure_tenant_usage("t-A")
        b = tq.measure_tenant_usage("t-B")
        assert a["workspaces_bytes"] == 10_000
        assert b["workspaces_bytes"] == 2_000

    def test_nested_workspace_layout_walked_recursively(self, isolated_roots):
        """The five-layer hierarchy means the per-tenant slice contains
        deeply nested files; the measurement must recurse, not just
        ``listdir`` the top level."""
        deep = (
            ws_mod._WORKSPACES_ROOT / "t-deep" / "pl" / "pid" / "agent" / "hash"
        )
        deep.mkdir(parents=True)
        (deep / "a.bin").write_bytes(b"\0" * 1024)
        sub = deep / "sub" / "subsub"
        sub.mkdir(parents=True)
        (sub / "b.bin").write_bytes(b"\0" * 2048)
        u = tq.measure_tenant_usage("t-deep")
        assert u["workspaces_bytes"] == 1024 + 2048


# ---------------------------------------------------------------------------
# 2) check_hard_quota raises when workspace footprint alone breaches the cap
# ---------------------------------------------------------------------------


class TestHardQuotaCountsWorkspaces:
    def test_workspace_only_breach_raises(self, isolated_roots):
        tq.write_quota("t-ws", tq.DiskQuota(
            soft_bytes=100, hard_bytes=200, keep_recent_runs=1,
        ))
        # Single 500-byte file is enough to push past hard=200.
        _seed_workspace_blob("t-ws", size_bytes=500)
        with pytest.raises(tq.QuotaExceeded) as exc:
            tq.check_hard_quota("t-ws")
        assert exc.value.tenant_id == "t-ws"
        assert exc.value.used >= 500
        assert exc.value.hard == 200


# ---------------------------------------------------------------------------
# 3) workspace.provision() refuses to create a worktree when over hard quota
# ---------------------------------------------------------------------------


class TestProvisionEnforceWorkspaceQuota:
    def test_provision_raises_when_over_hard_quota(
        self, isolated_roots, monkeypatch, tmp_path,
    ):
        """End-to-end gate: workspace.provision must call into
        tenant_quota.check_hard_quota before allocating any new
        worktree, and propagate ``QuotaExceeded`` so the caller can
        translate to HTTP 507."""
        # Stub audit so the deny-path doesn't try to hit the real DB pool.
        from backend import audit as _audit
        captured: dict = {}

        async def fake_audit_log(**kwargs):
            captured.setdefault("calls", []).append(kwargs)

        monkeypatch.setattr(_audit, "log", fake_audit_log)

        # Tiny quota; a 500-byte blob takes us over.
        tq.write_quota("t-blocked", tq.DiskQuota(
            soft_bytes=10, hard_bytes=100, keep_recent_runs=1,
        ))
        _seed_workspace_blob("t-blocked", size_bytes=500)

        with pytest.raises(tq.QuotaExceeded):
            asyncio.run(ws_mod.provision(
                agent_id="agent-blocked",
                task_id="task-1",
                tenant_id="t-blocked",
            ))

        # The deny-path must have written an audit row tagged with the
        # row-5 action so operators can correlate failed provisions
        # with quota state.
        actions = [c.get("action") for c in captured.get("calls", [])]
        assert "workspace_quota_exceeded" in actions

    def test_provision_succeeds_when_under_hard_quota(
        self, isolated_roots, tmp_path,
    ):
        """Sanity: a tenant well under quota gets through the gate and
        a worktree is materialised under the row-1 layout. This is the
        regression we'd notice first if the gate misfired and started
        denying every provision."""
        repo = _make_repo(tmp_path / "src_repo")
        # Quota set very high so nothing gets blocked.
        tq.write_quota("t-ok", tq.DiskQuota(
            soft_bytes=10 * 1024 * 1024,
            hard_bytes=20 * 1024 * 1024,
            keep_recent_runs=1,
        ))
        info = asyncio.run(ws_mod.provision(
            agent_id="agent-ok",
            task_id="task-ok",
            tenant_id="t-ok",
            remote_url=str(repo),
        ))
        try:
            assert info.path.is_dir()
            assert (info.path / "README.md").is_file()
            # Must have landed under the per-tenant slice that quota
            # measurement walks — invariant we rely on.
            assert info.path.is_relative_to(
                ws_mod._WORKSPACES_ROOT / "t-ok"
            )
        finally:
            asyncio.run(ws_mod.cleanup("agent-ok"))

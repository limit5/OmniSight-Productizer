"""Y6 #282 row 6 — Background workspace GC reaper contract.

Locks the row-6 sub-bullet under Y6 in TODO.md::

    背景 GC reaper：新 ``backend/workspace_gc.py`` async task
    （lifespan 啟動），每 1 小時跑一次：
      - 找 mtime > keep_recent_workspaces_stale_days（config 預設 30）
        且對應 agent 已結束的 workspace，移到 ``_trash/`` 暫存 7 天後硬刪。
      - 尊重 ``.git/index.lock`` + active agent registry，進行中的不刪。
      - 遇 tenant hard quota 超標時，優先刪舊的 workspace
        （per-project LRU）而非新的。
      - emit SSE ``workspace_gc`` 事件 + audit 記錄。

Sibling row tests:
* ``test_workspace_hierarchy.py``        — row 1 layout
* ``test_workspace_provision_signature`` — row 3 ContextVar wiring
* ``test_y6_row4_workspace_migration``   — row 4 legacy migrator
* ``test_y6_row5_workspace_quota``       — row 5 quota integration
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from backend import workspace as ws_mod
from backend import workspace_gc as gc_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ws_root(monkeypatch, tmp_path):
    """Redirect ``_WORKSPACES_ROOT`` onto pytest's tmp_path so the
    GC sweep walks the test's fake hierarchy and not the real
    ``./.agent_workspaces`` / ``./data/workspaces`` tree on the dev
    host."""
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(ws_mod, "_WORKSPACES_ROOT", root, raising=True)
    yield root


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    monkeypatch.setattr(ws_mod, "_workspaces", {}, raising=True)
    gc_mod._reset_for_tests()


@pytest.fixture
def silenced_audit(monkeypatch):
    """Replace ``audit.log`` with a capture spy so unit tests don't
    need a live PG pool. Mirrors the pattern in
    ``test_workspace_orphan_cleanup``."""
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


@pytest.fixture
def captured_sse(monkeypatch):
    """Replace ``backend.events.bus.publish`` with a recorder so we
    can assert ``workspace_gc`` events fire on trash / purge /
    quota_evict actions."""
    from backend import events as _events
    captured: list[tuple[str, dict]] = []

    def _publish(topic, payload, *args, **kwargs):
        captured.append((topic, payload))

    monkeypatch.setattr(_events.bus, "publish", _publish, raising=True)
    return captured


def _make_leaf(
    root: Path, *, tenant_id: str, project_id: str = "default",
    product_line: str = "default", agent_id: str = "agent-x",
    repo_hash: str = "self", file_size: int = 256,
    age_days: float = 0.0,
) -> Path:
    """Create a workspace leaf at the row-1 layout path. Drops a
    ``.git`` placeholder (file is fine — workspaces produce one in
    real life) and a sized blob, then back-dates everything by
    ``age_days``.
    """
    leaf = (
        root / tenant_id / product_line / project_id / agent_id / repo_hash
    )
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / ".git").write_text("gitdir: /tmp/fake\n")
    (leaf / "blob.bin").write_bytes(b"\0" * file_size)
    if age_days > 0:
        when = time.time() - age_days * 86400
        os.utime(leaf / "blob.bin", (when, when))
        os.utime(leaf / ".git", (when, when))
        os.utime(leaf, (when, when))
    return leaf


# ---------------------------------------------------------------------------
# 1) Stale leaf gets moved to _trash/
# ---------------------------------------------------------------------------


class TestStaleLeafTrashing:
    def test_stale_leaf_moved_to_trash(self, isolated_ws_root, silenced_audit):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-stale",
            agent_id="agent-old", age_days=45,
        )
        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))
        assert not leaf.exists(), "stale leaf must be moved out of its slice"
        assert len(summary.trashed) == 1
        record = summary.trashed[0]
        assert record["tenant_id"] == "t-stale"
        assert record["agent_id"] == "agent-old"
        # Trash entry physically lives under {root}/_trash/{tid}/...
        assert Path(record["trash_path"]).is_dir()
        assert "/_trash/t-stale/" in record["trash_path"]

    def test_fresh_leaf_left_alone(self, isolated_ws_root, silenced_audit):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-fresh",
            agent_id="agent-new", age_days=1,
        )
        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))
        assert leaf.is_dir()
        assert summary.trashed == []
        # Fresh leaves should appear in the skipped_fresh list so
        # operators inspecting telemetry can confirm the sweep saw
        # them but chose not to act.
        assert any(str(leaf) == p for p in summary.skipped_fresh)

    def test_audit_row_emitted_for_trashed(
        self, isolated_ws_root, silenced_audit,
    ):
        _make_leaf(
            isolated_ws_root, tenant_id="t-audit",
            agent_id="agent-trace", age_days=60,
        )
        asyncio.run(gc_mod.sweep_once(stale_days=30))
        actions = [c["action"] for c in silenced_audit]
        assert "workspace.gc_trashed" in actions
        row = next(c for c in silenced_audit
                   if c["action"] == "workspace.gc_trashed")
        assert row["entity_id"] == "agent-trace"
        assert row["before"]["tenant_id"] == "t-audit"

    def test_sse_event_emitted_for_trashed(
        self, isolated_ws_root, silenced_audit, captured_sse,
    ):
        _make_leaf(
            isolated_ws_root, tenant_id="t-sse",
            agent_id="agent-sse", age_days=60,
        )
        asyncio.run(gc_mod.sweep_once(stale_days=30))
        gc_events = [p for t, p in captured_sse if t == "workspace_gc"]
        assert any(p["action"] == "trashed" for p in gc_events)


# ---------------------------------------------------------------------------
# 2) Active registry / fresh git lock prevents trashing
# ---------------------------------------------------------------------------


class TestBusyWorkspacePreserved:
    def test_active_registry_member_not_trashed(
        self, isolated_ws_root, silenced_audit,
    ):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-active",
            agent_id="agent-running", age_days=60,
        )
        # Pretend the agent is still running by registering its
        # workspace in the in-process active registry.
        ws_mod._workspaces["agent-running"] = ws_mod.WorkspaceInfo(
            agent_id="agent-running", task_id="t1",
            branch="agent/agent-running/t1", path=leaf,
            repo_source="(test)",
        )
        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))
        assert leaf.is_dir(), "active workspace must NOT be moved"
        assert summary.trashed == []
        # Registry skip reason recorded for telemetry.
        assert any("registry" in s for s in summary.skipped_busy)

    def test_fresh_index_lock_blocks_trash(
        self, isolated_ws_root, silenced_audit,
    ):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-locked",
            agent_id="agent-mid-op", age_days=0,
        )
        git_dir = leaf / ".git"
        git_dir.unlink()
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")
        # Lock mtime = now → "fresh" by 60s rule.
        os.utime(lock, (time.time(), time.time()))
        # Now back-date the leaf itself (after all contents are
        # placed, so the leaf's directory mtime stays old past the
        # stale cutoff).
        old = time.time() - 60 * 86400
        os.utime(leaf, (old, old))

        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))
        assert leaf.is_dir()
        assert summary.trashed == []
        assert any("index.lock" in s for s in summary.skipped_busy)

    def test_stale_index_lock_does_not_block(
        self, isolated_ws_root, silenced_audit,
    ):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-stalelock",
            agent_id="agent-zombie", age_days=60,
        )
        git_dir = leaf / ".git"
        git_dir.unlink()
        git_dir.mkdir()
        lock = git_dir / "index.lock"
        lock.write_text("")
        # Lock mtime = 5 minutes ago → past the 60s freshness window.
        old = time.time() - 300
        os.utime(lock, (old, old))
        # Push the leaf mtime past the stale threshold too.
        old_leaf = time.time() - 60 * 86400
        os.utime(leaf, (old_leaf, old_leaf))

        summary = asyncio.run(gc_mod.sweep_once(stale_days=30))
        assert not leaf.exists()
        assert len(summary.trashed) == 1


# ---------------------------------------------------------------------------
# 3) Trash entries past TTL get hard-deleted
# ---------------------------------------------------------------------------


class TestTrashPurge:
    def test_old_trash_entry_purged(self, isolated_ws_root, silenced_audit):
        trash = isolated_ws_root / "_trash" / "t-purge"
        trash.mkdir(parents=True)
        old_entry = trash / "1700000000-agent-old"
        old_entry.mkdir()
        (old_entry / "file.bin").write_bytes(b"\0" * 1024)
        old = time.time() - 14 * 86400
        os.utime(old_entry, (old, old))

        summary = asyncio.run(gc_mod.sweep_once(
            stale_days=30, trash_ttl_days=7,
        ))
        assert not old_entry.exists()
        assert len(summary.purged) == 1
        record = summary.purged[0]
        assert record["tenant_id"] == "t-purge"
        assert record["freed_bytes"] >= 1024

    def test_recent_trash_entry_kept(self, isolated_ws_root, silenced_audit):
        trash = isolated_ws_root / "_trash" / "t-keep"
        trash.mkdir(parents=True)
        fresh = trash / f"{int(time.time())}-agent-fresh"
        fresh.mkdir()
        (fresh / "file.bin").write_bytes(b"\0" * 64)

        summary = asyncio.run(gc_mod.sweep_once(
            stale_days=30, trash_ttl_days=7,
        ))
        assert fresh.exists(), "trash within TTL must NOT be purged"
        assert summary.purged == []

    def test_purge_audit_row_emitted(self, isolated_ws_root, silenced_audit):
        trash = isolated_ws_root / "_trash" / "t-purge-audit"
        trash.mkdir(parents=True)
        old_entry = trash / "1700000000-agent-x"
        old_entry.mkdir()
        old = time.time() - 14 * 86400
        os.utime(old_entry, (old, old))
        asyncio.run(gc_mod.sweep_once(stale_days=30, trash_ttl_days=7))
        actions = [c["action"] for c in silenced_audit]
        assert "workspace.gc_purged" in actions


# ---------------------------------------------------------------------------
# 4) Quota-driven LRU eviction
# ---------------------------------------------------------------------------


class TestQuotaLRUEviction:
    def test_over_hard_evicts_oldest_first(
        self, isolated_ws_root, silenced_audit, monkeypatch,
    ):
        """When a tenant is over hard quota, the per-project LRU
        must trash the OLDEST workspace first. Spec: "優先刪舊的
        workspace（per-project LRU）而非新的"."""
        old_leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-quota",
            project_id="proj-A", agent_id="agent-old",
            file_size=1024, age_days=10,
        )
        new_leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-quota",
            project_id="proj-A", agent_id="agent-new",
            file_size=1024, age_days=1,
        )

        # Stub measure_tenant_usage / load_quota so the test does
        # not depend on the real disk-walk (which would also pick
        # up the trash entries we don't care about). The first call
        # reports over-hard; subsequent calls report below.
        from backend import tenant_quota as _tq
        usage_calls = {"n": 0}

        def fake_measure(tid):
            usage_calls["n"] += 1
            if tid == "t-quota" and usage_calls["n"] == 1:
                return {
                    "total_bytes": 10_000,
                    "artifacts_bytes": 0,
                    "workflow_runs_bytes": 0,
                    "backups_bytes": 0,
                    "ingest_tmp_bytes": 0,
                    "workspaces_bytes": 10_000,
                }
            return {
                "total_bytes": 100,
                "artifacts_bytes": 0,
                "workflow_runs_bytes": 0,
                "backups_bytes": 0,
                "ingest_tmp_bytes": 0,
                "workspaces_bytes": 100,
            }

        def fake_load_quota(tid, plan=None):
            return _tq.DiskQuota(
                soft_bytes=500, hard_bytes=1000, keep_recent_runs=1,
            )

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(_tq, "load_quota", fake_load_quota)

        # Skip the stale-leaf branch's age cutoff so it doesn't also
        # trash the old leaf as a stale sweep — we want to observe
        # the QUOTA-driven path specifically.
        summary = asyncio.run(gc_mod.sweep_once(stale_days=999))

        # Oldest must be evicted, newest must survive.
        assert not old_leaf.exists()
        assert new_leaf.is_dir()
        assert len(summary.quota_evicted) >= 1
        record = summary.quota_evicted[0]
        assert record["tenant_id"] == "t-quota"
        assert record["project_id"] == "proj-A"
        assert record["agent_id"] == "agent-old"

    def test_quota_evict_audit_row(
        self, isolated_ws_root, silenced_audit, monkeypatch,
    ):
        _make_leaf(
            isolated_ws_root, tenant_id="t-q-audit",
            project_id="proj", agent_id="agent-evicted",
            file_size=1024, age_days=10,
        )
        from backend import tenant_quota as _tq

        def fake_measure(tid):
            return {
                "total_bytes": 5000, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 5000,
            }

        def fake_load_quota(tid, plan=None):
            return _tq.DiskQuota(
                soft_bytes=100, hard_bytes=200, keep_recent_runs=1,
            )

        monkeypatch.setattr(_tq, "measure_tenant_usage", fake_measure)
        monkeypatch.setattr(_tq, "load_quota", fake_load_quota)

        asyncio.run(gc_mod.sweep_once(stale_days=999))
        actions = [c["action"] for c in silenced_audit]
        assert "workspace.gc_quota_evicted" in actions

    def test_under_hard_no_eviction(
        self, isolated_ws_root, silenced_audit, monkeypatch,
    ):
        leaf = _make_leaf(
            isolated_ws_root, tenant_id="t-under",
            project_id="proj", agent_id="agent-keep",
            file_size=128, age_days=10,
        )
        from backend import tenant_quota as _tq
        monkeypatch.setattr(
            _tq, "measure_tenant_usage",
            lambda tid: {
                "total_bytes": 100, "artifacts_bytes": 0,
                "workflow_runs_bytes": 0, "backups_bytes": 0,
                "ingest_tmp_bytes": 0, "workspaces_bytes": 100,
            },
        )
        monkeypatch.setattr(
            _tq, "load_quota",
            lambda tid, plan=None: _tq.DiskQuota(
                soft_bytes=10_000, hard_bytes=20_000, keep_recent_runs=1,
            ),
        )

        summary = asyncio.run(gc_mod.sweep_once(stale_days=999))
        assert leaf.is_dir()
        assert summary.quota_evicted == []


# ---------------------------------------------------------------------------
# 5) Singleton loop guard
# ---------------------------------------------------------------------------


class TestSingletonGuard:
    def test_second_run_gc_loop_is_noop(
        self, isolated_ws_root, silenced_audit, monkeypatch,
    ):
        """Two ``run_gc_loop`` invocations in the same worker must
        reduce to one. Singleton guard mirrors the
        ``user_drafts_gc._LOOP_RUNNING`` pattern."""
        gc_mod._LOOP_RUNNING = True
        try:
            # Should return immediately without sleeping.
            asyncio.run(asyncio.wait_for(
                gc_mod.run_gc_loop(interval_s=3600), timeout=2.0,
            ))
        finally:
            gc_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# 6) _trash sidecar excluded from leaf scan
# ---------------------------------------------------------------------------


class TestTrashSidecarExcluded:
    def test_trash_dir_not_treated_as_workspace(
        self, isolated_ws_root, silenced_audit,
    ):
        """The leaf scan must skip ``_trash/`` so an entry there
        isn't re-trashed (which would create infinite nested
        timestamp dirs)."""
        # Plant a "leaf-shaped" entry under _trash (mimicking a
        # previously trashed workspace).
        trash_leaf = (
            isolated_ws_root / "_trash" / "t-x"
            / "1700000000-agent-x"
        )
        trash_leaf.mkdir(parents=True)
        (trash_leaf / ".git").write_text("gitdir: /tmp/fake\n")
        # Make it look stale.
        old = time.time() - 60 * 86400
        os.utime(trash_leaf, (old, old))

        summary = asyncio.run(gc_mod.sweep_once(
            stale_days=30, trash_ttl_days=999,
        ))
        # Should NOT show up in the trashed list.
        for record in summary.trashed:
            assert "/_trash/" not in record["leaf"], (
                "leaf scan must skip _trash sidecar"
            )

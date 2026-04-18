"""M2 — per-tenant disk quota + LRU cleanup tests.

Covers:
  * plan → DiskQuota mapping (free / starter / pro / enterprise)
  * quota.yaml load / write / round-trip with hand-edited overrides
  * measure_tenant_usage adds artifacts + workflow_runs + backups + tmp
  * check_hard_quota raises QuotaExceeded above hard threshold
  * lru_cleanup deletes oldest first AND honours .keep markers
  * lru_cleanup never touches the most-recent ``keep_recent_runs`` runs
  * cleanup_tenant_tmp clears /tmp/<tid>/ at sandbox stop
  * sweep_tenant emits SSE warning on soft + auto-runs LRU on hard
  * start_container raises QuotaExceeded when over hard quota
  * /storage/cleanup endpoint returns the LRU summary

The tests rebase TENANTS_ROOT and INGEST_BASE onto pytest's tmp_path
so each test gets a clean per-tenant tree without touching production
``data/tenants/`` or the host /tmp.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from backend import tenant_quota as tq
from backend import tenant_fs as tfs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fixture: isolated tenant filesystem
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture()
def isolated_tenants(monkeypatch, tmp_path):
    """Redirect tenants_root + ingest_base into the test tmp_path."""
    tenants_dir = tmp_path / "tenants"
    ingest_dir = tmp_path / "tmp_ingest"
    tenants_dir.mkdir()
    ingest_dir.mkdir()
    monkeypatch.setattr(tfs, "_TENANTS_ROOT", tenants_dir)
    monkeypatch.setattr(tfs, "_INGEST_BASE", ingest_dir)
    tq._reset_for_tests()
    yield tmp_path


def _make_run(tenant_id: str, run_id: str, *, size_kb: int,
              age_offset_s: float, in_progress: bool = False,
              keep: bool = False) -> Path:
    """Materialise a fake workflow_run dir with one fixed-size file."""
    run = tfs.tenant_workflow_runs_root(tenant_id) / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "output.bin").write_bytes(b"\0" * (size_kb * 1024))
    if in_progress:
        (run / ".in_progress").touch()
    if keep:
        (run / tq.KEEP_MARKER).touch()
    # Force mtime so the LRU sort is deterministic (older = smaller mtime).
    target = time.time() - age_offset_s
    os.utime(run, (target, target))
    return run


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Plan → quota mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPlanQuotaMapping:
    def test_free_tier_defaults(self):
        q = tq.quota_for_plan("free")
        assert q.soft_bytes == 5 * tq.GIB
        assert q.hard_bytes == 10 * tq.GIB
        assert q.keep_recent_runs == 5

    def test_enterprise_tier_larger(self):
        q = tq.quota_for_plan("enterprise")
        assert q.soft_bytes == 500 * tq.GIB
        assert q.hard_bytes == 1000 * tq.GIB

    def test_unknown_plan_falls_back_to_free(self):
        assert tq.quota_for_plan("bogus") == tq.quota_for_plan("free")
        assert tq.quota_for_plan(None) == tq.quota_for_plan("free")

    def test_all_plans_have_hard_above_soft(self):
        for plan, q in tq.PLAN_DISK_QUOTAS.items():
            assert q.hard_bytes > q.soft_bytes, (
                f"plan {plan} hard={q.hard_bytes} not above soft={q.soft_bytes}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  quota.yaml load / write / override
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestQuotaFile:
    def test_load_falls_back_to_plan_when_missing(self, isolated_tenants):
        q = tq.load_quota("t-1", plan="starter")
        assert q == tq.quota_for_plan("starter")

    def test_write_then_load_roundtrip(self, isolated_tenants):
        original = tq.DiskQuota(soft_bytes=123, hard_bytes=456, keep_recent_runs=3)
        path = tq.write_quota("t-1", original, plan="pro")
        assert path.is_file()
        loaded = tq.load_quota("t-1", plan="enterprise")  # plan ignored when file present
        assert loaded == original

    def test_yaml_hand_edit_override_takes_effect(self, isolated_tenants):
        # Operator hand-edits the file with a custom soft budget.
        path = tq.quota_file_path("t-2")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({
            "soft_bytes": 9999,
            "hard_bytes": 99999,
            "keep_recent_runs": 7,
            "plan": "custom",
        }))
        q = tq.load_quota("t-2", plan="free")
        assert q.soft_bytes == 9999
        assert q.hard_bytes == 99999
        assert q.keep_recent_runs == 7

    def test_corrupt_yaml_falls_back_to_plan_default(self, isolated_tenants):
        path = tq.quota_file_path("t-3")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("::: not valid yaml :::")
        q = tq.load_quota("t-3", plan="free")
        # Falls back without raising
        assert q == tq.quota_for_plan("free")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Disk usage measurement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMeasureUsage:
    def test_empty_tenant_zero_usage(self, isolated_tenants):
        u = tq.measure_tenant_usage("t-empty")
        assert u["total_bytes"] == 0
        assert u["artifacts_bytes"] == 0
        assert u["workflow_runs_bytes"] == 0

    def test_aggregates_artifacts_runs_backups_tmp(self, isolated_tenants):
        # artifacts: 1 KiB
        (tfs.tenant_artifacts_root("t-a") / "f1.bin").write_bytes(b"\0" * 1024)
        # workflow_runs: 2 KiB
        rr = tfs.tenant_workflow_runs_root("t-a") / "r1"
        rr.mkdir()
        (rr / "out.bin").write_bytes(b"\0" * 2048)
        # backups: 4 KiB
        bk = tfs.tenant_data_root("t-a") / "backups"
        bk.mkdir(exist_ok=True)
        (bk / "b.tar").write_bytes(b"\0" * 4096)
        # tmp: 8 KiB
        (tfs.tenant_ingest_root("t-a") / "scratch.bin").write_bytes(b"\0" * 8192)

        u = tq.measure_tenant_usage("t-a")
        assert u["artifacts_bytes"] == 1024
        assert u["workflow_runs_bytes"] == 2048
        assert u["backups_bytes"] == 4096
        assert u["ingest_tmp_bytes"] == 8192
        assert u["total_bytes"] == 1024 + 2048 + 4096 + 8192

    def test_measure_skips_symlinks(self, isolated_tenants, tmp_path):
        target = tmp_path / "huge.bin"
        target.write_bytes(b"\0" * (10 * 1024 * 1024))
        link = tfs.tenant_artifacts_root("t-link") / "ptr"
        link.symlink_to(target)
        u = tq.measure_tenant_usage("t-link")
        # Symlink itself isn't counted (we skip 0o120000).
        assert u["artifacts_bytes"] == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hard-quota enforcement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHardQuotaCheck:
    def test_under_hard_does_not_raise(self, isolated_tenants):
        (tfs.tenant_artifacts_root("t-q") / "f.bin").write_bytes(b"\0" * 1024)
        # No exception
        tq.check_hard_quota("t-q", plan="free")

    def test_over_hard_raises_quota_exceeded(self, isolated_tenants, monkeypatch):
        # Use a tiny custom quota file to avoid actually filling 10 GiB.
        tq.write_quota("t-over", tq.DiskQuota(
            soft_bytes=100, hard_bytes=200, keep_recent_runs=1,
        ))
        (tfs.tenant_artifacts_root("t-over") / "big.bin").write_bytes(b"\0" * 500)
        with pytest.raises(tq.QuotaExceeded) as exc:
            tq.check_hard_quota("t-over")
        assert exc.value.tenant_id == "t-over"
        assert exc.value.used >= 500
        assert exc.value.hard == 200

    def test_check_accepts_precomputed_usage(self, isolated_tenants):
        tq.write_quota("t-pre", tq.DiskQuota(
            soft_bytes=100, hard_bytes=200, keep_recent_runs=1,
        ))
        usage = {"total_bytes": 99999, "artifacts_bytes": 99999,
                 "workflow_runs_bytes": 0, "backups_bytes": 0,
                 "ingest_tmp_bytes": 0}
        with pytest.raises(tq.QuotaExceeded):
            tq.check_hard_quota("t-pre", usage=usage)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LRU cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLRUCleanup:
    def test_under_target_does_nothing(self, isolated_tenants):
        _make_run("t-lru", "r1", size_kb=10, age_offset_s=10)
        tq.write_quota("t-lru", tq.DiskQuota(
            soft_bytes=100 * 1024 * 1024, hard_bytes=200 * 1024 * 1024,
            keep_recent_runs=1,
        ))
        summary = tq.lru_cleanup("t-lru")
        assert summary["deleted"] == []

    def test_deletes_oldest_first(self, isolated_tenants):
        # 5 runs * 10 KiB each = 50 KiB. soft=20 KiB → target=18 KiB.
        # keep_recent_runs=1 reserves the newest, so cleanup walks runs
        # 0..3 oldest first and stops once usage <= 18 KiB.
        for i in range(5):
            _make_run("t-lru", f"r{i}", size_kb=10, age_offset_s=100 - i * 10)
        tq.write_quota("t-lru", tq.DiskQuota(
            soft_bytes=20 * 1024, hard_bytes=40 * 1024,
            keep_recent_runs=1,
        ))
        summary = tq.lru_cleanup("t-lru")
        deleted_ids = [d["run_id"] for d in summary["deleted"]]
        # Oldest is r0 (age 100s), newest is r4 (age 60s).
        # We expect r0..r3 progressively until usage <= 18 KiB.
        assert deleted_ids[0] == "r0"
        assert "r4" not in deleted_ids  # newest reserved
        # End state: only r4 (newest) should remain
        remaining = sorted(p.name for p in tfs.tenant_workflow_runs_root("t-lru").iterdir())
        assert "r4" in remaining

    def test_keep_marker_protects_run(self, isolated_tenants):
        # Mark the OLDEST run with .keep — it must survive even though it
        # would normally be the LRU victim.
        _make_run("t-keep", "r-old", size_kb=10, age_offset_s=200, keep=True)
        _make_run("t-keep", "r-mid", size_kb=10, age_offset_s=100)
        _make_run("t-keep", "r-new", size_kb=10, age_offset_s=10)
        tq.write_quota("t-keep", tq.DiskQuota(
            soft_bytes=15 * 1024, hard_bytes=40 * 1024, keep_recent_runs=1,
        ))
        summary = tq.lru_cleanup("t-keep")
        deleted_ids = [d["run_id"] for d in summary["deleted"]]
        skipped_keep = summary["skipped_keep"]
        assert "r-old" not in deleted_ids
        assert "r-old" in skipped_keep
        assert "r-mid" in deleted_ids
        # r-new is reserved by keep_recent_runs=1 (most recent).

    def test_in_progress_run_never_deleted(self, isolated_tenants):
        _make_run("t-prog", "r0", size_kb=10, age_offset_s=200, in_progress=True)
        _make_run("t-prog", "r1", size_kb=10, age_offset_s=10)
        tq.write_quota("t-prog", tq.DiskQuota(
            soft_bytes=5 * 1024, hard_bytes=40 * 1024, keep_recent_runs=1,
        ))
        summary = tq.lru_cleanup("t-prog")
        deleted_ids = [d["run_id"] for d in summary["deleted"]]
        # r0 is in_progress so it's NOT in the candidate list at all.
        assert "r0" not in deleted_ids
        assert (tfs.tenant_workflow_runs_root("t-prog") / "r0").is_dir()

    def test_keep_recent_runs_reservation(self, isolated_tenants):
        # keep_recent_runs=3 so the 3 newest are reserved.
        for i in range(5):
            _make_run("t-rec", f"r{i}", size_kb=10, age_offset_s=100 - i * 10)
        tq.write_quota("t-rec", tq.DiskQuota(
            soft_bytes=1, hard_bytes=10 * 1024 * 1024, keep_recent_runs=3,
        ))
        summary = tq.lru_cleanup("t-rec")
        deleted_ids = [d["run_id"] for d in summary["deleted"]]
        # r2/r3/r4 are the 3 newest — must be skipped.
        assert "r2" not in deleted_ids
        assert "r3" not in deleted_ids
        assert "r4" not in deleted_ids
        # r0/r1 are eligible; both deleted because target=1 forces full cleanup.
        assert "r0" in deleted_ids
        assert "r1" in deleted_ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tenant /tmp force-cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCleanupTenantTmp:
    def test_clears_files_and_dirs(self, isolated_tenants):
        ing = tfs.tenant_ingest_root("t-tmp")
        (ing / "f1").write_bytes(b"\0" * 1024)
        sub = ing / "sub"
        sub.mkdir()
        (sub / "f2").write_bytes(b"\0" * 2048)
        freed = tq.cleanup_tenant_tmp("t-tmp")
        assert freed == 1024 + 2048
        assert list(ing.iterdir()) == []  # empty after cleanup

    def test_safe_when_dir_already_gone(self, isolated_tenants):
        # Function tolerates a missing tenant tmp.
        freed = tq.cleanup_tenant_tmp("t-never-existed")
        assert freed == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSweep:
    @pytest.mark.asyncio
    async def test_sweep_under_threshold_no_warning(self, isolated_tenants, monkeypatch):
        published = []

        class _Bus:
            def publish(self, event, data, **kw):
                published.append((event, data))

        # Stub event bus so we can assert on the warning publish.
        import backend.events as _ev
        monkeypatch.setattr(_ev, "bus", _Bus())

        summary = await tq.sweep_tenant("t-clean")
        assert summary["over_soft"] is False
        assert summary["over_hard"] is False
        assert published == []

    @pytest.mark.asyncio
    async def test_sweep_over_soft_emits_warning_and_lru(self, isolated_tenants, monkeypatch):
        published: list[tuple[str, dict]] = []

        class _Bus:
            def publish(self, event, data, **kw):
                published.append((event, data))

        import backend.events as _ev
        monkeypatch.setattr(_ev, "bus", _Bus())

        # Tiny quota so a single run trips it.
        tq.write_quota("t-soft", tq.DiskQuota(
            soft_bytes=5 * 1024, hard_bytes=100 * 1024, keep_recent_runs=1,
        ))
        for i in range(3):
            _make_run("t-soft", f"r{i}", size_kb=10, age_offset_s=100 - i * 10)

        summary = await tq.sweep_tenant("t-soft")
        assert summary["over_soft"] is True
        assert any(e == "tenant_storage_warning" and d["level"] == "soft"
                   for e, d in published)
        assert summary["cleanup"] is not None
        assert len(summary["cleanup"]["deleted"]) >= 1

    @pytest.mark.asyncio
    async def test_sweep_materialises_quota_yaml_first_time(self, isolated_tenants, monkeypatch):
        # Stub event bus so audit log doesn't fail in test environment
        import backend.events as _ev
        monkeypatch.setattr(_ev, "bus", type("B", (), {"publish": lambda *a, **k: None})())

        path = tq.quota_file_path("t-fresh")
        assert not path.is_file()
        await tq.sweep_tenant("t-fresh")
        assert path.is_file()
        loaded = yaml.safe_load(path.read_text())
        # Free-plan defaults written through.
        assert loaded["soft_bytes"] == 5 * tq.GIB
        assert loaded["plan"] == "free"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Container start gate (unit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestContainerStartGate:
    @pytest.mark.asyncio
    async def test_start_container_raises_507_translatable_error(
        self, isolated_tenants, monkeypatch,
    ):
        """When the tenant is at hard quota, start_container must raise
        QuotaExceeded so the workspaces router can return HTTP 507."""
        # Stub heavy deps so we don't actually try to docker-run.
        from backend import container as ct

        async def fake_run(cmd, timeout=60):
            return (0, "", "")

        async def fake_ensure_image():
            return True

        monkeypatch.setattr(ct, "_run", fake_run)
        monkeypatch.setattr(ct, "ensure_image", fake_ensure_image)
        # Stub audit so we don't hit the DB.
        from backend import audit as _audit
        called = {}

        async def fake_audit_log(**kwargs):
            called.setdefault("actions", []).append(kwargs.get("action"))

        monkeypatch.setattr(_audit, "log", fake_audit_log)

        # Set up a tenant with a tiny hard quota that is already breached.
        tq.write_quota("t-block", tq.DiskQuota(
            soft_bytes=10, hard_bytes=20, keep_recent_runs=1,
        ))
        (tfs.tenant_artifacts_root("t-block") / "huge.bin").write_bytes(b"\0" * 100)

        from backend.db_context import set_tenant_id
        set_tenant_id("t-block")
        try:
            with pytest.raises(tq.QuotaExceeded):
                await ct.start_container("agent-blocked", Path("/tmp"))
        finally:
            set_tenant_id(None)
        # The audit row for the rejection should have been written.
        assert "sandbox_quota_exceeded" in called.get("actions", [])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoints (/storage/*)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStorageEndpoints:
    @pytest.fixture
    def http(self, isolated_tenants, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock

        from backend.routers.storage import router
        from backend import auth as _au

        app = FastAPI()
        app.include_router(router)

        user = MagicMock()
        user.role = "admin"
        user.email = "admin@test"
        user.tenant_id = "t-rest"

        async def _fake_dep():
            return user

        app.dependency_overrides[_au.require_viewer] = _fake_dep
        app.dependency_overrides[_au.require_operator] = _fake_dep
        # Stub audit + DB-backed plan resolver so we don't need a DB.
        from backend import audit as _audit

        async def fake_audit_log(**kwargs):
            return None

        monkeypatch.setattr(_audit, "log", fake_audit_log)

        async def fake_plan(tid):
            return "free"

        monkeypatch.setattr(tq, "_resolve_plan", fake_plan)

        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    def test_get_usage_returns_breakdown(self, http):
        # Prime the tenant with some data.
        (tfs.tenant_artifacts_root("t-rest") / "f.bin").write_bytes(b"\0" * 4096)
        resp = http.get("/storage/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t-rest"
        assert body["plan"] == "free"
        assert body["usage"]["artifacts_bytes"] == 4096
        assert body["usage"]["total_bytes"] == 4096
        assert body["over_soft"] is False

    def test_cleanup_returns_summary(self, http):
        # Build 4 runs over a tiny soft, expect at least 1 deleted.
        for i in range(4):
            _make_run("t-rest", f"r{i}", size_kb=10, age_offset_s=100 - i * 10)
        tq.write_quota("t-rest", tq.DiskQuota(
            soft_bytes=15 * 1024, hard_bytes=80 * 1024, keep_recent_runs=1,
        ))
        resp = http.post("/storage/cleanup")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t-rest"
        assert len(body["deleted"]) >= 1
        assert body["usage_after_bytes"] <= body["usage_before_bytes"]

    def test_admin_can_query_other_tenant(self, http):
        (tfs.tenant_artifacts_root("t-other") / "x.bin").write_bytes(b"\0" * 256)
        resp = http.get("/storage/usage?tenant_id=t-other")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tenant_id"] == "t-other"
        assert body["usage"]["artifacts_bytes"] == 256

"""M4 + H1 — tests for backend/routers/host.py.

ACL enforcement is the main point here — admin can read any tenant or
all tenants; non-admin is locked to their own tenant and gets 403 if
they try to peek at someone else's.

H1 tests cover the whole-host ``host`` block attached to every
successful response: baseline, current snapshot, and history list
derived from the ring buffer.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import auth as _au
from backend import host_metrics as hm
from backend.routers.host import router as host_router


def _make_user(role: str, tenant_id: str = "tenantX") -> _au.User:
    return _au.User(
        id=f"user-{role}", email=f"{role}@test.local", name=role,
        role=role, tenant_id=tenant_id,
    )


@pytest.fixture()
def admin_client():
    app = FastAPI()
    app.dependency_overrides[_au.current_user] = lambda: _make_user("admin", "tenantA")
    app.dependency_overrides[_au.require_admin] = lambda: _make_user("admin", "tenantA")
    app.include_router(host_router)
    hm._reset_for_tests()
    yield TestClient(app)
    hm._reset_for_tests()


@pytest.fixture()
def user_client():
    app = FastAPI()
    app.dependency_overrides[_au.current_user] = lambda: _make_user("viewer", "tenantB")
    app.dependency_overrides[_au.require_admin] = lambda: (_ for _ in ()).throw(
        Exception("admin-only")
    )
    app.include_router(host_router)
    hm._reset_for_tests()
    yield TestClient(app)
    hm._reset_for_tests()


def _seed_snapshot():
    with hm._lock:
        hm._latest_by_tenant["tenantA"] = hm.TenantUsage(
            tenant_id="tenantA", cpu_percent=100.0, mem_used_gb=2.0,
            disk_used_gb=1.0, sandbox_count=3,
        )
        hm._latest_by_tenant["tenantB"] = hm.TenantUsage(
            tenant_id="tenantB", cpu_percent=50.0, mem_used_gb=1.0,
            disk_used_gb=0.5, sandbox_count=1,
        )


def _make_host_snapshot(
    *,
    cpu: float = 42.5,
    mem_pct: float = 55.0,
    mem_used: float = 35.2,
    disk_pct: float = 23.4,
    loadavg: float = 2.5,
    containers: int = 7,
    source: str = "sdk",
    sampled_at: float | None = None,
) -> hm.HostSnapshot:
    ts = sampled_at if sampled_at is not None else time.time()
    host = hm.HostSample(
        cpu_percent=cpu,
        mem_percent=mem_pct,
        mem_used_gb=mem_used,
        mem_total_gb=64.0,
        disk_percent=disk_pct,
        disk_used_gb=120.0,
        disk_total_gb=512.0,
        loadavg_1m=loadavg,
        loadavg_5m=loadavg * 0.8,
        loadavg_15m=loadavg * 0.6,
        sampled_at=ts,
    )
    docker = hm.DockerSample(
        container_count=containers,
        total_mem_reservation_bytes=1 << 30,
        source=source,
        sampled_at=ts,
    )
    return hm.HostSnapshot(host=host, docker=docker, sampled_at=ts)


def _seed_host_history(count: int = 3) -> list[hm.HostSnapshot]:
    """Push ``count`` ticks into the ring buffer (oldest first) and
    return the list for assertion convenience."""
    snaps = [
        _make_host_snapshot(cpu=float(i * 10), containers=i, sampled_at=1000.0 + i)
        for i in range(count)
    ]
    with hm._lock:
        for s in snaps:
            hm._host_history.append(s)
    return snaps


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Admin cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAdmin:
    def test_list_all_tenants(self, admin_client):
        _seed_snapshot()
        resp = admin_client.get("/host/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "tenants" in data
        tids = {t["tenant_id"] for t in data["tenants"]}
        assert tids == {"tenantA", "tenantB"}

    def test_admin_can_read_any_tenant(self, admin_client):
        _seed_snapshot()
        resp = admin_client.get("/host/metrics?tenant_id=tenantB")
        assert resp.status_code == 200
        assert resp.json()["tenant"]["tenant_id"] == "tenantB"
        assert resp.json()["tenant"]["cpu_percent"] == 50.0

    def test_accounting_admin_only(self, admin_client):
        hm.accumulate_usage(
            {"tenantA": hm.TenantUsage(tenant_id="tenantA", cpu_percent=100.0,
                                        mem_used_gb=1.0)},
            interval_s=10.0,
        )
        resp = admin_client.get("/host/accounting")
        assert resp.status_code == 200
        rows = resp.json()["tenants"]
        assert any(r["tenant_id"] == "tenantA" and r["cpu_seconds_total"] > 0 for r in rows)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Non-admin (viewer / operator) cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNonAdmin:
    def test_no_tenant_id_returns_self_only(self, user_client, monkeypatch):
        _seed_snapshot()
        resp = user_client.get("/host/metrics")
        assert resp.status_code == 200
        data = resp.json()
        # Non-admin with no query returns {tenant: ...} (single), not {tenants: [...]}.
        assert "tenant" in data and "tenants" not in data
        assert data["tenant"]["tenant_id"] == "tenantB"

    def test_explicit_self_allowed(self, user_client):
        _seed_snapshot()
        resp = user_client.get("/host/metrics?tenant_id=tenantB")
        assert resp.status_code == 200
        assert resp.json()["tenant"]["tenant_id"] == "tenantB"

    def test_other_tenant_forbidden(self, user_client):
        _seed_snapshot()
        resp = user_client.get("/host/metrics?tenant_id=tenantA")
        assert resp.status_code == 403

    def test_me_endpoint_shortcut(self, user_client, monkeypatch):
        # Pre-seed ONLY disk via tenant_quota so /me works with no samples.
        monkeypatch.setattr(hm, "_measure_disk_gb", lambda tid: 0.0)
        resp = user_client.get("/host/metrics/me")
        assert resp.status_code == 200
        assert resp.json()["tenant"]["tenant_id"] == "tenantB"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shape + rounding
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestShape:
    def test_shape_has_expected_fields(self, admin_client):
        _seed_snapshot()
        resp = admin_client.get("/host/metrics?tenant_id=tenantA")
        keys = set(resp.json()["tenant"].keys())
        assert keys == {"tenant_id", "cpu_percent", "mem_used_gb",
                        "disk_used_gb", "sandbox_count"}

    def test_numbers_rounded_in_transport(self, admin_client):
        with hm._lock:
            hm._latest_by_tenant["tenantA"] = hm.TenantUsage(
                tenant_id="tenantA", cpu_percent=12.345678,
                mem_used_gb=1.234567, disk_used_gb=0.123456,
                sandbox_count=1,
            )
        resp = admin_client.get("/host/metrics?tenant_id=tenantA")
        body = resp.json()["tenant"]
        assert body["cpu_percent"] == 12.35  # 2 dp
        assert body["mem_used_gb"] == 1.235  # 3 dp
        assert body["disk_used_gb"] == 0.123  # 3 dp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H1 — whole-host block (current + history)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestH1HostBlock:
    def test_host_block_present_for_admin_list(self, admin_client):
        _seed_snapshot()
        _seed_host_history(count=3)
        body = admin_client.get("/host/metrics").json()
        assert "host" in body
        assert body["host"]["baseline"]["cpu_cores"] == hm.HOST_BASELINE.cpu_cores
        assert body["host"]["baseline"]["cpu_model"] == hm.HOST_BASELINE.cpu_model
        assert body["host"]["interval_s"] == hm.SAMPLE_INTERVAL_S
        assert body["host"]["history_size"] == hm.HOST_HISTORY_SIZE

    def test_host_block_present_for_non_admin(self, user_client):
        _seed_snapshot()
        _seed_host_history(count=2)
        body = user_client.get("/host/metrics").json()
        assert "host" in body
        # Non-admin still sees the aggregate host numbers — they don't
        # expose any per-tenant state.
        assert body["host"]["current"] is not None
        assert len(body["host"]["history"]) == 2

    def test_host_current_matches_latest_snapshot(self, admin_client):
        snaps = _seed_host_history(count=3)
        body = admin_client.get("/host/metrics").json()
        current = body["host"]["current"]
        # Latest snapshot is the last one appended.
        assert current is not None
        assert current["sampled_at"] == snaps[-1].sampled_at
        assert current["host"]["cpu_percent"] == round(snaps[-1].host.cpu_percent, 2)
        assert current["docker"]["container_count"] == snaps[-1].docker.container_count
        assert current["docker"]["source"] == "sdk"

    def test_host_history_oldest_first(self, admin_client):
        snaps = _seed_host_history(count=4)
        body = admin_client.get("/host/metrics").json()
        history = body["host"]["history"]
        assert len(history) == 4
        got_ts = [h["sampled_at"] for h in history]
        expected_ts = [s.sampled_at for s in snaps]
        assert got_ts == expected_ts  # oldest → newest
        # Strictly monotonic as seeded.
        assert got_ts == sorted(got_ts)

    def test_host_history_capped_at_ring_buffer_size(self, admin_client):
        # Seed way more than HOST_HISTORY_SIZE to exercise rotation.
        overflow = hm.HOST_HISTORY_SIZE + 10
        with hm._lock:
            for i in range(overflow):
                hm._host_history.append(
                    _make_host_snapshot(cpu=float(i), sampled_at=2000.0 + i),
                )
        body = admin_client.get("/host/metrics").json()
        history = body["host"]["history"]
        assert len(history) == hm.HOST_HISTORY_SIZE
        # Ring buffer drops the oldest, so the earliest timestamp in the
        # response is the first that survived rotation (overflow - size).
        earliest = body["host"]["history"][0]["sampled_at"]
        assert earliest == 2000.0 + (overflow - hm.HOST_HISTORY_SIZE)

    def test_host_current_none_on_cold_start(self, admin_client):
        # No ring-buffer entries seeded.
        body = admin_client.get("/host/metrics").json()
        assert body["host"]["current"] is None
        assert body["host"]["history"] == []

    def test_host_block_on_me_endpoint(self, user_client):
        _seed_host_history(count=1)
        body = user_client.get("/host/metrics/me").json()
        assert "host" in body
        assert body["host"]["current"] is not None
        assert body["host"]["baseline"]["mem_total_gb"] == hm.HOST_BASELINE.mem_total_gb

    def test_host_block_on_single_tenant_view(self, admin_client):
        _seed_snapshot()
        _seed_host_history(count=2)
        body = admin_client.get("/host/metrics?tenant_id=tenantA").json()
        assert "tenant" in body
        assert "host" in body
        assert body["host"]["history"][-1]["docker"]["source"] == "sdk"

    def test_host_snapshot_shape(self, admin_client):
        _seed_host_history(count=1)
        body = admin_client.get("/host/metrics").json()
        current = body["host"]["current"]
        # Each snapshot is a (host, docker, sampled_at) bundle.
        assert set(current.keys()) == {"host", "docker", "sampled_at"}
        host_keys = {
            "cpu_percent", "mem_percent", "mem_used_gb", "mem_total_gb",
            "disk_percent", "disk_used_gb", "disk_total_gb",
            "loadavg_1m", "loadavg_5m", "loadavg_15m", "sampled_at",
        }
        assert set(current["host"].keys()) == host_keys
        docker_keys = {"container_count", "total_mem_reservation_bytes",
                       "source", "sampled_at"}
        assert set(current["docker"].keys()) == docker_keys

    def test_host_numbers_rounded(self, admin_client):
        snap = _make_host_snapshot(
            cpu=12.345678, mem_pct=55.555555, mem_used=35.123456,
            disk_pct=23.987654, loadavg=2.55555, sampled_at=9999.0,
        )
        with hm._lock:
            hm._host_history.append(snap)
        body = admin_client.get("/host/metrics").json()
        h = body["host"]["current"]["host"]
        assert h["cpu_percent"] == 12.35  # 2 dp
        assert h["mem_percent"] == 55.56  # 2 dp
        assert h["mem_used_gb"] == 35.123  # 3 dp
        assert h["disk_percent"] == 23.99  # 2 dp
        assert h["loadavg_1m"] == 2.556    # 3 dp

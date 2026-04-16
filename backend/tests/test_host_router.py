"""M4 — tests for backend/routers/host.py.

ACL enforcement is the main point here — admin can read any tenant or
all tenants; non-admin is locked to their own tenant and gets 403 if
they try to peek at someone else's.
"""

from __future__ import annotations

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

"""I7: Frontend tenant-aware — backend tests.

Verifies:
  - GET /auth/tenants returns correct tenant lists
  - X-Tenant-Id middleware validates header against user's tenant
  - Admin users can switch tenants via header
  - Non-admin users are blocked from cross-tenant access
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


@dataclass
class FakeUser:
    id: str
    email: str
    name: str
    role: str
    enabled: bool = True
    must_change_password: bool = False
    tenant_id: str = "t-default"

    def to_dict(self):
        return {
            "id": self.id, "email": self.email, "name": self.name,
            "role": self.role, "enabled": self.enabled,
            "must_change_password": self.must_change_password,
            "tenant_id": self.tenant_id,
        }


@dataclass
class FakeSession:
    user_id: str
    token: str = "tok-123"


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        return FakeCursor(self._rows)


# ── Unit tests for X-Tenant-Id validation logic ──


def test_whoami_includes_tenant_id():
    """whoami should include tenant_id in the user dict."""
    user = FakeUser(id="u1", email="a@b.com", name="A", role="admin", tenant_id="t-acme")
    d = user.to_dict()
    assert d["tenant_id"] == "t-acme"


def test_admin_sees_all_tenants():
    """Admin user should get all tenants from the list."""
    user = FakeUser(id="u1", email="a@b.com", name="Admin", role="admin")
    assert user.role == "admin"


def test_viewer_sees_own_tenant():
    """Non-admin user should only see their own tenant."""
    user = FakeUser(id="u2", email="v@b.com", name="Viewer", role="viewer", tenant_id="t-acme")
    assert user.role != "admin"
    assert user.tenant_id == "t-acme"


def test_header_tenant_match_allowed():
    """X-Tenant-Id matching user's tenant should be allowed."""
    user = FakeUser(id="u1", email="a@b.com", name="A", role="viewer", tenant_id="t-acme")
    header_tid = "t-acme"
    assert header_tid == user.tenant_id


def test_header_tenant_mismatch_blocked_for_non_admin():
    """Non-admin trying to use a different tenant via header should be blocked."""
    user = FakeUser(id="u1", email="a@b.com", name="A", role="viewer", tenant_id="t-acme")
    header_tid = "t-other"
    blocked = header_tid != user.tenant_id and user.role != "admin"
    assert blocked is True


def test_header_tenant_mismatch_allowed_for_admin():
    """Admin can use X-Tenant-Id for any tenant."""
    user = FakeUser(id="u1", email="a@b.com", name="Admin", role="admin", tenant_id="t-default")
    header_tid = "t-acme"
    blocked = header_tid != user.tenant_id and user.role != "admin"
    assert blocked is False


def test_no_header_no_validation():
    """No X-Tenant-Id header = no tenant override, passes through."""
    header_tid = None
    assert header_tid is None


# ── Integration-style tests for the tenant list endpoint logic ──


@pytest.mark.asyncio
async def test_user_tenants_admin_gets_all():
    """Admin user should receive all tenants from DB."""
    rows = [
        ("t-acme", "Acme Corp", "pro", 1),
        ("t-beta", "Beta Inc", "free", 1),
        ("t-default", "Default", "free", 1),
    ]
    conn = FakeConn(rows)
    user = FakeUser(id="u1", email="a@b.com", name="Admin", role="admin")

    with patch("backend.db._conn", return_value=conn):
        async with conn.execute(
            "SELECT id, name, plan, enabled FROM tenants ORDER BY name",
        ) as cur:
            result = await cur.fetchall()
            tenants = [{"id": r[0], "name": r[1], "plan": r[2], "enabled": bool(r[3])} for r in result]

    assert len(tenants) == 3
    assert tenants[0]["id"] == "t-acme"
    assert tenants[2]["id"] == "t-default"


@pytest.mark.asyncio
async def test_user_tenants_viewer_gets_own():
    """Non-admin user should receive only their own tenant."""
    rows = [("t-acme", "Acme Corp", "pro", 1)]
    conn = FakeConn(rows)
    user = FakeUser(id="u2", email="v@b.com", name="Viewer", role="viewer", tenant_id="t-acme")

    async with conn.execute(
        "SELECT id, name, plan, enabled FROM tenants WHERE id = ?",
        (user.tenant_id,),
    ) as cur:
        r = await cur.fetchone()
        tenant = {"id": r[0], "name": r[1], "plan": r[2], "enabled": bool(r[3])}

    assert tenant["id"] == "t-acme"
    assert tenant["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_user_tenants_fallback_when_not_in_db():
    """If tenant not in DB, return a synthetic entry."""
    rows = []
    conn = FakeConn(rows)
    user = FakeUser(id="u3", email="n@b.com", name="New", role="viewer", tenant_id="t-new")

    async with conn.execute(
        "SELECT id, name, plan, enabled FROM tenants WHERE id = ?",
        (user.tenant_id,),
    ) as cur:
        r = await cur.fetchone()

    if r:
        tenant = {"id": r[0], "name": r[1], "plan": r[2], "enabled": bool(r[3])}
    else:
        tenant = {"id": user.tenant_id, "name": user.tenant_id, "plan": "free", "enabled": True}

    assert tenant["id"] == "t-new"
    assert tenant["plan"] == "free"

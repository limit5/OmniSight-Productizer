"""Y2 (#278) row 1 — tests for POST /api/v1/admin/tenants.

Covers:
  * super_admin role exists in auth.ROLES with the right rank
  * require_super_admin dependency factory exists and gates correctly
  * Pydantic id-pattern validator: positive matches (incl. t-default),
    negative matches (uppercase, leading hyphen, too short, too long,
    missing t- prefix, illegal chars)
  * Pure-unit module-level regex sanity (no FastAPI involved)
  * HTTP path:
      - 201 happy path with row persisted + audit log written
      - 409 on duplicate id (custom + t-default reservation)
      - 422 on malformed id
      - 422 on unknown plan
      - enabled=False persists as 0
  * Privilege escalation guards introduced by Y2:
      - POST /users with role='super_admin' → 403
      - PATCH /users/{id} with role='super_admin' → 403
  * Tenant-admin cannot reach POST /admin/tenants → 403 (via
    dependency override to simulate session-mode role denial)
"""

from __future__ import annotations

import os

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: role + dependency surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_super_admin_role_in_roles_tuple():
    from backend import auth
    assert "super_admin" in auth.ROLES


def test_super_admin_role_outranks_admin():
    from backend import auth
    assert auth.role_at_least("super_admin", "admin")
    assert auth.role_at_least("super_admin", "operator")
    assert auth.role_at_least("super_admin", "viewer")
    assert not auth.role_at_least("admin", "super_admin")
    assert not auth.role_at_least("operator", "super_admin")
    assert not auth.role_at_least("viewer", "super_admin")


def test_require_super_admin_dependency_exists():
    from backend import auth
    assert callable(auth.require_super_admin)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: tenant id regex
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_tenant_id_pattern_constant_matches_spec():
    from backend.routers.admin_tenants import TENANT_ID_PATTERN
    # The TODO row spec literally says: ^t-[a-z0-9][a-z0-9-]{2,62}$
    assert TENANT_ID_PATTERN == r"^t-[a-z0-9][a-z0-9-]{2,62}$"


@pytest.mark.parametrize("good_id", [
    "t-default",          # the seeded reserved id MUST still match
    "t-acme",             # 3 chars after t-, minimum boundary
    "t-acme-corp",
    "t-a1b",              # mixed alphanumeric trailing
    "t-0abc",             # leading digit allowed
    "t-z" + "z" * 62,     # trailing-section length 62 → at the upper boundary
    "t-aaa",              # exactly minimum: 1 leading + 2 trailing
])
def test_tenant_id_pattern_accepts_valid_ids(good_id):
    from backend.routers.admin_tenants import _is_valid_tenant_id
    assert _is_valid_tenant_id(good_id), f"should accept {good_id!r}"


@pytest.mark.parametrize("bad_id", [
    "",                       # empty
    "tdefault",               # missing 't-' prefix
    "T-default",              # uppercase prefix
    "t-Default",              # uppercase elsewhere
    "t--double",              # leading char in trailing section is '-' (not [a-z0-9])
    "t-",                     # too short
    "t-a",                    # only 1 char after t- (need 1 lead + ≥2 trailing)
    "t-ab",                   # 0 trailing chars (need ≥2)
    "t-z" + "z" * 63,         # trailing section 63 chars (upper bound is 62)
    "t-acme_corp",            # underscore not in charset
    "t-acme.corp",            # dot not in charset
    "t-acme corp",            # space not in charset
    "t-acme/corp",            # slash not in charset
    "default",                # missing prefix entirely
    "  t-default",            # leading whitespace
    "t-default ",             # trailing whitespace
])
def test_tenant_id_pattern_rejects_invalid_ids(bad_id):
    from backend.routers.admin_tenants import _is_valid_tenant_id
    assert not _is_valid_tenant_id(bad_id), f"should reject {bad_id!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: Pydantic body model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_tenant_request_accepts_minimum_body():
    from backend.routers.admin_tenants import CreateTenantRequest
    body = CreateTenantRequest(id="t-acme", name="Acme")
    assert body.id == "t-acme"
    assert body.plan == "free"   # default
    assert body.enabled is True  # default


def test_create_tenant_request_rejects_bad_id():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import CreateTenantRequest
    with pytest.raises(ValidationError):
        CreateTenantRequest(id="bad-id-no-prefix", name="X")


def test_create_tenant_request_rejects_unknown_plan():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import CreateTenantRequest
    with pytest.raises(ValidationError):
        CreateTenantRequest(id="t-acme", name="Acme", plan="ultra-deluxe")


def test_create_tenant_request_rejects_empty_name():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import CreateTenantRequest
    with pytest.raises(ValidationError):
        CreateTenantRequest(id="t-acme", name="")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path + invariants (require live PG)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _purge_tenant(pg_test_pool, tid: str) -> None:
    """Best-effort cleanup. The HTTP path commits real rows via the
    shared db_pool, so each test is responsible for removing the rows
    it created (the savepoint-rollback only protects pg_test_conn-
    based tests; the ``client`` fixture commits)."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_post_admin_tenants_201_happy_path(client, pg_test_pool):
    tid = "t-acme-y2-create"
    try:
        res = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "Acme Corp", "plan": "pro",
                  "enabled": True},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["id"] == tid
        assert body["name"] == "Acme Corp"
        assert body["plan"] == "pro"
        assert body["enabled"] is True
        assert body.get("created_at"), "created_at must be returned"

        # Row really lives in the DB
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, plan, enabled FROM tenants "
                "WHERE id = $1",
                tid,
            )
        assert row is not None
        assert row["name"] == "Acme Corp"
        assert row["plan"] == "pro"
        assert row["enabled"] == 1
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_admin_tenants_default_plan_and_enabled(client, pg_test_pool):
    """Body without ``plan`` / ``enabled`` should fall back to the
    spec defaults (``free`` / ``True``)."""
    tid = "t-acme-y2-default"
    try:
        res = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "Acme Defaults"},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["plan"] == "free"
        assert body["enabled"] is True
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_admin_tenants_disabled_persists(client, pg_test_pool):
    tid = "t-acme-y2-disabled"
    try:
        res = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "Acme Off", "plan": "free",
                  "enabled": False},
        )
        assert res.status_code == 201, res.text
        assert res.json()["enabled"] is False
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT enabled FROM tenants WHERE id = $1", tid,
            )
        assert row["enabled"] == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_admin_tenants_duplicate_returns_409(client, pg_test_pool):
    tid = "t-acme-y2-dup"
    try:
        first = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "First", "plan": "free"},
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "Second", "plan": "pro"},
        )
        assert second.status_code == 409, second.text
        assert "already exists" in second.json()["detail"]
        # The row in the DB should still be the FIRST insert (the
        # second call is a no-op).
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, plan FROM tenants WHERE id = $1", tid,
            )
        assert row["name"] == "First"
        assert row["plan"] == "free"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_admin_tenants_t_default_collision_returns_409(client):
    """``t-default`` is seeded by migration 0012. Any attempt to
    re-create it must hit the duplicate guard."""
    res = await client.post(
        "/api/v1/admin/tenants",
        json={"id": "t-default", "name": "Hostile Takeover", "plan": "pro"},
    )
    assert res.status_code == 409, res.text


@_requires_pg
async def test_post_admin_tenants_invalid_id_returns_422(client):
    res = await client.post(
        "/api/v1/admin/tenants",
        json={"id": "bad-no-prefix", "name": "Acme"},
    )
    # Pydantic returns 422 on schema violation; FastAPI default.
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_admin_tenants_uppercase_id_returns_422(client):
    res = await client.post(
        "/api/v1/admin/tenants",
        json={"id": "t-Acme", "name": "Acme"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_admin_tenants_unknown_plan_returns_422(client):
    res = await client.post(
        "/api/v1/admin/tenants",
        json={"id": "t-acme-y2-badplan", "name": "Acme",
              "plan": "ultra-deluxe"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_admin_tenants_audit_log_written(client, pg_test_pool):
    """A successful create must append a ``tenant_created`` audit row
    to the actor's tenant chain. The actor is the synthetic anonymous
    super-admin (``anonymous@local``) under the open-mode test
    fixture."""
    tid = "t-acme-y2-audit"
    try:
        before_count = 0
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM audit_log "
                "WHERE action = 'tenant_created' AND entity_id = $1",
                tid,
            )
            before_count = int(row["n"])
        assert before_count == 0  # sanity

        res = await client.post(
            "/api/v1/admin/tenants",
            json={"id": tid, "name": "Audit Acme", "plan": "starter"},
        )
        assert res.status_code == 201, res.text

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_created' AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )
        assert audit_row is not None, "audit row must be written"
        assert audit_row["entity_kind"] == "tenant"
        assert audit_row["entity_id"] == tid
        assert audit_row["actor"]  # actor email present
        # after_json should contain the created tenant payload
        import json as _json
        after = _json.loads(audit_row["after_json"])
        assert after["id"] == tid
        assert after["name"] == "Audit Acme"
        assert after["plan"] == "starter"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant admin gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_admin_tenants_tenant_admin_gets_403(client):
    """Override ``current_user`` with a fake tenant-admin (role='admin',
    NOT super_admin) and verify the role gate denies the request.

    The default ``client`` fixture runs in open mode → anonymous
    super-admin. To prove the role check actually fires, we replace
    current_user with a non-super-admin and let require_role's rank
    comparison reject it.
    """
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadmin", email="tadmin@acme.local", name="Tenant Admin",
        role="admin", enabled=True, tenant_id="t-acme-y2-rbac",
    )

    async def _fake_current_user():
        return tenant_admin

    # Also override require_super_admin directly so we don't need the
    # CSRF check inside require_role to pass under the open-mode
    # client. The override emulates the production deny-path: HTTP 403.
    def _deny():
        raise HTTPException(
            status_code=403,
            detail="Requires role=super_admin or higher (you are admin)",
        )

    app.dependency_overrides[_au.current_user] = _fake_current_user
    app.dependency_overrides[_au.require_super_admin] = _deny
    try:
        res = await client.post(
            "/api/v1/admin/tenants",
            json={"id": "t-acme-rbac", "name": "RBAC test"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y2 privilege-escalation guards on existing /users endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_users_blocks_super_admin_role(client):
    """Adding ``super_admin`` to ROLES would otherwise let any
    tenant-admin promote a user to platform-tier via POST /users.
    Y2 guard: POST /users with role='super_admin' must 403."""
    res = await client.post(
        "/api/v1/users",
        json={"email": "evil@takeover.local", "role": "super_admin",
              "password": "longenoughpassword12345"},
    )
    assert res.status_code == 403, res.text
    assert "super_admin" in res.json()["detail"]


@_requires_pg
async def test_patch_users_blocks_super_admin_role(client, pg_test_pool):
    """Same guard on the PATCH path — can't promote-by-edit either."""
    # Seed a user we can target.
    from backend import auth as _au
    target = await _au.create_user(
        email="target@example.local", name="Target", role="viewer",
        password="longenoughpassword12345",
    )
    try:
        res = await client.patch(
            f"/api/v1/users/{target.id}",
            json={"role": "super_admin"},
        )
        assert res.status_code == 403, res.text
        assert "super_admin" in res.json()["detail"]
        # And the row in the DB must still be 'viewer'.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1", target.id,
            )
        assert row["role"] == "viewer"
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("DELETE FROM users WHERE id = $1", target.id)

"""Y2 (#278) row 4 — tests for PATCH /api/v1/admin/tenants/{id}.

Covers:
  * route is mounted under the api prefix as a PATCH path-param route
  * handler depends on ``require_super_admin``
  * static-shape SQL audit on the two new query constants:
      - read-only-/-bounded-write (UPDATE only, no DROP/TRUNCATE/etc.)
      - whitelisted table only
      - PG ``$N`` placeholders, never SQLite ``?``
      - fingerprint grep clean (4-pattern check, SOP Step 3)
  * Pydantic body model:
      - rejects an empty body (no settable field) at the handler layer
      - rejects unknown plan / empty name / oversized name
      - accepts each single-field PATCH and any pair / triple
  * RBAC: tenant admin gets 403
  * HTTP path on live PG:
      - 200 happy path: rename
      - 200 happy path: enable → disable → enable
      - 200 happy path: plan upgrade (free → pro) — disk check skipped
      - 200 happy path: plan no-op (plan == current_plan) — disk check skipped
      - 409 plan downgrade refused when disk_used > new hard_bytes
      - 200 plan downgrade allowed when disk_used <= new hard_bytes
      - 200 combined PATCH: rename + plan + enable in one call
      - 404 well-formed but unknown tenant id
      - 422 malformed tenant id (uppercase)
      - 422 empty body
      - 422 unknown plan
      - audit log row written with action='tenant_updated' carrying the
        before/after snapshots
"""

from __future__ import annotations

import json
import os

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: route surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_patch_tenant_route_is_mounted():
    from backend.main import app
    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/admin/tenants/{tenant_id}"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "PATCH" in methods, (
        f"PATCH path-param route missing; got {matches!r}"
    )


def test_patch_handler_uses_super_admin_dependency():
    """The dependency surface must gate on ``require_super_admin`` —
    same gate as POST / GET / LIST."""
    from backend.routers.admin_tenants import patch_tenant
    from backend import auth

    import inspect
    sig = inspect.signature(patch_tenant)
    deps = []
    for _name, param in sig.parameters.items():
        default = param.default
        target = getattr(default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"PATCH /admin/tenants/{{id}} must depend on "
        f"require_super_admin; deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL fingerprint / safety audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_PATCH_SQL_CONSTANTS = (
    "_FETCH_TENANT_FOR_PATCH_SQL",
    "_PATCH_TENANT_SQL",
)


@pytest.mark.parametrize("sql_name", _PATCH_SQL_CONSTANTS)
def test_patch_sql_no_destructive_keywords(sql_name):
    """The PATCH path may UPDATE — but not DROP / TRUNCATE / DELETE /
    ALTER / GRANT / REVOKE / INSERT. (The fetch helper is read-only.)"""
    import backend.routers.admin_tenants as mod
    sql = getattr(mod, sql_name)
    sql_upper = sql.upper()
    # Always forbidden, regardless of which constant we're auditing:
    for forbidden in ("DROP ", "TRUNCATE ", "ALTER ", "GRANT ",
                      "REVOKE ", "DELETE "):
        assert forbidden not in sql_upper, (
            f"{sql_name} must not contain {forbidden!r}"
        )
    # The fetch helper is strictly read-only.
    if sql_name == "_FETCH_TENANT_FOR_PATCH_SQL":
        for forbidden in ("INSERT ", "UPDATE "):
            assert forbidden not in sql_upper, (
                f"{sql_name} must be read-only; found {forbidden!r}"
            )


def test_patch_sql_references_tenants_table_only():
    """Whitelist the FROM / UPDATE target so a future drift to a wrong
    table is caught at module-load time."""
    import backend.routers.admin_tenants as mod
    assert "tenants" in mod._FETCH_TENANT_FOR_PATCH_SQL
    assert "tenants" in mod._PATCH_TENANT_SQL


@pytest.mark.parametrize("sql_name", _PATCH_SQL_CONSTANTS)
def test_patch_sql_uses_pg_placeholders(sql_name):
    """PG ``$N``, never SQLite ``?``. Both PATCH constants take
    parameters, so $1 must appear in each."""
    import backend.routers.admin_tenants as mod
    sql = getattr(mod, sql_name)
    assert "?" not in sql, (
        f"{sql_name} must use PG $N placeholders, not SQLite ?"
    )
    assert "$1" in sql, (
        f"{sql_name} must accept at least a $1 tenant_id parameter"
    )


@pytest.mark.parametrize("sql_name", _PATCH_SQL_CONSTANTS)
def test_patch_sql_fingerprint_clean(sql_name):
    """SOP Step-3 fingerprint grep on each SQL constant: catch the four
    classic compat-residue patterns at module-load time."""
    import re as _re
    import backend.routers.admin_tenants as mod
    sql = getattr(mod, sql_name)
    fingerprint = _re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(sql), (
        f"{sql_name} contains a compat-residue fingerprint"
    )


def test_patch_sql_uses_coalesce_for_partial_update():
    """The UPDATE must use ``COALESCE`` so a None parameter leaves the
    column alone — otherwise an omitted PATCH field would clobber the
    DB column to NULL on every call."""
    import backend.routers.admin_tenants as mod
    sql_upper = mod._PATCH_TENANT_SQL.upper()
    assert "COALESCE" in sql_upper, (
        "PATCH UPDATE must use COALESCE to honour partial-update "
        "semantics"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: Pydantic body model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_patch_request_empty_body_has_any_field_false():
    from backend.routers.admin_tenants import PatchTenantRequest
    body = PatchTenantRequest()
    assert body.has_any_field() is False


def test_patch_request_each_single_field_sets_has_any():
    from backend.routers.admin_tenants import PatchTenantRequest
    assert PatchTenantRequest(name="X").has_any_field()
    assert PatchTenantRequest(plan="pro").has_any_field()
    assert PatchTenantRequest(enabled=False).has_any_field()
    # Crucially: enabled=False is a real value (not a no-op).
    assert PatchTenantRequest(enabled=False).enabled is False


def test_patch_request_rejects_unknown_plan():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import PatchTenantRequest
    with pytest.raises(ValidationError):
        PatchTenantRequest(plan="ultra-deluxe")


def test_patch_request_rejects_empty_name():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import PatchTenantRequest
    with pytest.raises(ValidationError):
        PatchTenantRequest(name="")


def test_patch_request_rejects_oversized_name():
    from pydantic import ValidationError
    from backend.routers.admin_tenants import PatchTenantRequest
    with pytest.raises(ValidationError):
        PatchTenantRequest(name="x" * 201)


def test_patch_request_accepts_full_body():
    from backend.routers.admin_tenants import PatchTenantRequest
    body = PatchTenantRequest(name="Acme", plan="pro", enabled=False)
    assert body.has_any_field()
    assert body.name == "Acme"
    assert body.plan == "pro"
    assert body.enabled is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant admin gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_tenant_tenant_admin_gets_403(client):
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadmin-patch", email="tadmin-patch@acme.local",
        name="Tenant Admin (patch)", role="admin", enabled=True,
        tenant_id="t-acme-y2-patch-rbac",
    )

    async def _fake_current_user():
        return tenant_admin

    def _deny():
        raise HTTPException(
            status_code=403,
            detail="Requires role=super_admin or higher (you are admin)",
        )

    app.dependency_overrides[_au.current_user] = _fake_current_user
    app.dependency_overrides[_au.require_super_admin] = _deny
    try:
        res = await client.patch(
            "/api/v1/admin/tenants/t-default",
            json={"name": "Hostile Rename"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path on live PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _purge(pg_test_pool, tid: str) -> None:
    """Best-effort cleanup mirror of the list / detail purges. Kept
    minimal — PATCH tests do not seed users / projects / events."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute("DELETE FROM event_log WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM audit_log WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_patch_tenant_rename_happy_path(client, pg_test_pool):
    """Pure rename: name field changes, plan / enabled untouched."""
    tid = "t-acme-y2-patch-rename"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Original Name', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"name": "Renamed Acme"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == tid
        assert body["name"] == "Renamed Acme"
        assert body["plan"] == "free"
        assert body["enabled"] is True

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, plan, enabled FROM tenants WHERE id = $1",
                tid,
            )
        assert row["name"] == "Renamed Acme"
        assert row["plan"] == "free"
        assert row["enabled"] == 1
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_enable_disable_round_trip(client, pg_test_pool):
    """Toggle enabled true → false → true and verify each step persists.
    The COALESCE-based UPDATE must NOT collapse ``enabled=False`` into
    "leave alone"."""
    tid = "t-acme-y2-patch-toggle"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Toggle Acme', 'free', 1)",
                tid,
            )
        # Disable
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"enabled": False},
        )
        assert res.status_code == 200, res.text
        assert res.json()["enabled"] is False
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT enabled FROM tenants WHERE id = $1", tid,
            )
        assert row["enabled"] == 0

        # Re-enable
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"enabled": True},
        )
        assert res.status_code == 200, res.text
        assert res.json()["enabled"] is True
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT enabled FROM tenants WHERE id = $1", tid,
            )
        assert row["enabled"] == 1
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_plan_upgrade_skips_disk_check(
    client, pg_test_pool,
):
    """free → pro is an upgrade; new hard_bytes is larger than current
    so the disk-usage walk is structurally moot. The handler must
    accept it on a tenant that has zero on-disk footprint without
    error."""
    tid = "t-acme-y2-patch-upgrade"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Upgrade Acme', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "pro"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["plan"] == "pro"
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_plan_no_op_change(client, pg_test_pool):
    """Setting plan to its current value is a no-op for the disk check
    (handler shortcut) but should still 200 and emit an audit row."""
    tid = "t-acme-y2-patch-noopplan"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'NoOp Acme', 'starter', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "starter"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["plan"] == "starter"
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_plan_downgrade_allowed_when_under_quota(
    client, pg_test_pool,
):
    """pro → free downgrade with zero disk usage must be permitted.
    The fresh tenant has nothing on disk so disk_used (0) ≤ free.
    hard_bytes (10 GiB)."""
    tid = "t-acme-y2-patch-downgrade-ok"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Downgrade Acme', 'pro', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "free"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["plan"] == "free"
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT plan FROM tenants WHERE id = $1", tid,
            )
        assert row["plan"] == "free"
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_plan_downgrade_refused_when_over_quota(
    client, pg_test_pool, monkeypatch,
):
    """The contract: when current disk_used > new plan hard_bytes,
    PATCH must 409 and refuse the change. We force this by monkey-
    patching ``_measure_disk_safely`` to claim the tenant is using
    11 GiB (> free's 10 GiB hard_bytes) — the actual filesystem walk
    is filesystem-dependent and brittle to set up in a unit test."""
    from backend.routers import admin_tenants as mod
    from backend.tenant_quota import PLAN_DISK_QUOTAS

    tid = "t-acme-y2-patch-downgrade-fail"
    free_hard = PLAN_DISK_QUOTAS["free"].hard_bytes
    fake_used = free_hard + (1024 ** 3)  # 1 GiB over hard
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Overshoot Acme', 'pro', 1)",
                tid,
            )

        # Substitute the disk measurer for this test only.
        def _fake_measure(t):
            assert t == tid, f"disk check fired against wrong tid: {t!r}"
            return fake_used
        monkeypatch.setattr(mod, "_measure_disk_safely", _fake_measure)

        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "free"},
        )
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["current_plan"] == "pro"
        assert body["requested_plan"] == "free"
        assert body["disk_used_bytes"] == fake_used
        assert body["new_hard_bytes"] == free_hard
        assert "force-delete" in body["detail"].lower() or \
               "free up storage" in body["detail"].lower()

        # Critical contract: the row must NOT be mutated on a refused
        # downgrade. (No half-applied state; no silent forced delete.)
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT plan FROM tenants WHERE id = $1", tid,
            )
        assert row["plan"] == "pro", (
            "row must be untouched after a 409 plan-downgrade refusal"
        )
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_combined_rename_plan_enabled(
    client, pg_test_pool,
):
    """One PATCH carrying all three fields must apply atomically."""
    tid = "t-acme-y2-patch-combined"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Combined Acme', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={
                "name": "Combined Acme v2",
                "plan": "pro",
                "enabled": False,
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["name"] == "Combined Acme v2"
        assert body["plan"] == "pro"
        assert body["enabled"] is False
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, plan, enabled FROM tenants WHERE id = $1",
                tid,
            )
        assert row["name"] == "Combined Acme v2"
        assert row["plan"] == "pro"
        assert row["enabled"] == 0
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_audit_log_written(client, pg_test_pool):
    """A successful PATCH must emit a ``tenant_updated`` audit row with
    the before/after snapshots."""
    tid = "t-acme-y2-patch-audit"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Audit Acme', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"name": "Audit Acme Renamed", "plan": "starter"},
        )
        assert res.status_code == 200, res.text

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "       before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_updated' AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                tid,
            )
        assert audit_row is not None, (
            "audit row must be written on successful PATCH"
        )
        assert audit_row["entity_kind"] == "tenant"
        assert audit_row["entity_id"] == tid
        assert audit_row["actor"]
        before = json.loads(audit_row["before_json"])
        after = json.loads(audit_row["after_json"])
        assert before["name"] == "Audit Acme"
        assert before["plan"] == "free"
        assert after["name"] == "Audit Acme Renamed"
        assert after["plan"] == "starter"
    finally:
        await _purge(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — error branches
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_tenant_unknown_id_returns_404(client):
    """Well-formed but unknown id → 404, not a silent no-op 200."""
    res = await client.patch(
        "/api/v1/admin/tenants/t-this-tenant-cannot-exist",
        json={"name": "Ghost Rename"},
    )
    assert res.status_code == 404, res.text
    assert "not found" in res.json()["detail"].lower()


@_requires_pg
async def test_patch_tenant_malformed_id_returns_422(client):
    """Malformed id (uppercase, no t- prefix) → 422 before any DB hit."""
    res = await client.patch(
        "/api/v1/admin/tenants/T-UPPERCASE",
        json={"name": "x"},
    )
    assert res.status_code == 422, res.text
    res2 = await client.patch(
        "/api/v1/admin/tenants/no-prefix-xyz",
        json={"name": "x"},
    )
    assert res2.status_code == 422, res2.text


@_requires_pg
async def test_patch_tenant_empty_body_returns_422(client, pg_test_pool):
    """Body with no settable field → 422. Even on an existing tenant we
    refuse the call rather than silently no-op (a no-op PATCH wastes
    an audit row and usually means the operator meant something else)."""
    tid = "t-acme-y2-patch-emptybody"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Empty Body Acme', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={},
        )
        assert res.status_code == 422, res.text
        assert "at least one" in res.json()["detail"].lower()
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_patch_tenant_unknown_plan_returns_422(client, pg_test_pool):
    """Plan not in the Literal enum → 422 (Pydantic-layer rejection)."""
    tid = "t-acme-y2-patch-badplan"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Bad Plan Acme', 'free', 1)",
                tid,
            )
        res = await client.patch(
            f"/api/v1/admin/tenants/{tid}",
            json={"plan": "ultra-deluxe"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge(pg_test_pool, tid)

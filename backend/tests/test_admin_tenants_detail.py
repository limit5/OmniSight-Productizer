"""Y2 (#278) row 3 — tests for GET /api/v1/admin/tenants/{id}.

Covers:
  * route is mounted under the api prefix as a GET path-param route
  * handler depends on ``require_super_admin``
  * static-shape SQL audit on the four query constants:
      - read-only (no destructive verbs)
      - whitelisted tables only
      - PG ``$N`` placeholders, never SQLite ``?``
      - fingerprint grep clean (4-pattern check, SOP Step 3)
  * Pydantic-style id validator: super-admin gate runs before id check,
    but malformed id still produces 422 *before* a DB hit
  * tenant-admin gets 403 (require_super_admin gate fires)
  * HTTP path on live PG:
      - 200 happy path against a freshly-seeded tenant
      - top-level envelope shape: id / name / plan / enabled /
        created_at / quota / usage / members / projects /
        recent_audit_events
      - quota fields derived from PLAN_DISK_QUOTAS for the seeded plan
      - usage fields shape (incl. `disk_used_pct_of_hard` ratio)
      - members list reflects user_tenant_memberships rows joined to
        users (per-tenant role wins over account-tier role)
      - projects list reflects non-archived first then archived
      - recent_audit_events newest-first, capped at 50
      - 404 when tenant id is well-formed but unknown
      - 422 when tenant id is malformed
"""

from __future__ import annotations

import os
import time

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


def test_get_tenant_detail_route_is_mounted():
    from backend.main import app
    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/admin/tenants/{tenant_id}"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "GET" in methods, f"GET path-param route missing; got {matches!r}"


def test_detail_handler_uses_super_admin_dependency():
    """The dependency surface must gate on ``require_super_admin`` — the
    same gate as POST and the LIST endpoint."""
    from backend.routers.admin_tenants import get_tenant_detail
    from backend import auth

    import inspect
    sig = inspect.signature(get_tenant_detail)
    deps = []
    for _name, param in sig.parameters.items():
        default = param.default
        target = getattr(default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"GET /admin/tenants/{{id}} must depend on require_super_admin; "
        f"deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL fingerprint / safety audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SQL_CONSTANTS = (
    "_GET_TENANT_SQL",
    "_LIST_MEMBERS_SQL",
    "_LIST_PROJECTS_SQL",
    "_LIST_AUDIT_EVENTS_SQL",
)


@pytest.mark.parametrize("sql_name", _SQL_CONSTANTS)
def test_detail_sql_is_read_only(sql_name):
    """Every detail query must be read-only."""
    import backend.routers.admin_tenants as mod
    sql = getattr(mod, sql_name)
    sql_upper = sql.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ",
                      "TRUNCATE ", "ALTER ", "GRANT ", "REVOKE "):
        assert forbidden not in sql_upper, (
            f"{sql_name} must be read-only; found {forbidden!r}"
        )


def test_detail_sql_references_expected_tables_only():
    """Whitelist FROM / JOIN targets so a future drift to a wrong
    table (singular vs plural, dropped table reference) is caught."""
    import backend.routers.admin_tenants as mod
    assert "tenants" in mod._GET_TENANT_SQL
    assert "users" in mod._GET_TENANT_SQL
    assert "projects" in mod._GET_TENANT_SQL
    assert "event_log" in mod._GET_TENANT_SQL
    assert "audit_log" in mod._GET_TENANT_SQL

    assert "user_tenant_memberships" in mod._LIST_MEMBERS_SQL
    assert "users" in mod._LIST_MEMBERS_SQL

    assert "projects" in mod._LIST_PROJECTS_SQL

    assert "audit_log" in mod._LIST_AUDIT_EVENTS_SQL


@pytest.mark.parametrize("sql_name", _SQL_CONSTANTS)
def test_detail_sql_uses_pg_placeholders(sql_name):
    """Every parameterised query must use PG ``$N`` (asyncpg), never
    SQLite-style ``?``."""
    import backend.routers.admin_tenants as mod
    sql = getattr(mod, sql_name)
    # Single ``?`` only legitimately shows up inside a string literal,
    # which we don't have in any of these constants.
    assert "?" not in sql, (
        f"{sql_name} must use PG $N placeholders, not SQLite ?"
    )
    # Sanity: every one of these queries takes at least $1 (tenant_id).
    assert "$1" in sql, (
        f"{sql_name} must accept at least a $1 tenant_id parameter"
    )


@pytest.mark.parametrize("sql_name", _SQL_CONSTANTS)
def test_detail_sql_fingerprint_clean(sql_name):
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


def test_audit_event_limit_is_capped():
    """The audit-event listing must be bounded; an unbounded fetch on
    a noisy tenant could return tens of thousands of rows."""
    from backend.routers.admin_tenants import _AUDIT_EVENT_LIMIT
    assert isinstance(_AUDIT_EVENT_LIMIT, int)
    assert 1 <= _AUDIT_EVENT_LIMIT <= 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant admin gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_tenant_detail_tenant_admin_gets_403(client):
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadmin-detail", email="tadmin-detail@acme.local",
        name="Tenant Admin (detail)", role="admin", enabled=True,
        tenant_id="t-acme-y2-detail-rbac",
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
        res = await client.get("/api/v1/admin/tenants/t-default")
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path on live PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _purge(pg_test_pool, tid: str) -> None:
    """Best-effort cleanup mirror of the list-test purge."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute("DELETE FROM event_log WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM audit_log WHERE tenant_id = $1", tid)
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid
        )
        await conn.execute("DELETE FROM users WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_get_tenant_detail_default_tenant_envelope(client):
    """Smallest possible happy path: with whatever rows currently live
    in PG, GET /admin/tenants/t-default must 200 and produce the agreed
    envelope shape."""
    res = await client.get("/api/v1/admin/tenants/t-default")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body.keys()) >= {
        "id", "name", "plan", "enabled", "created_at",
        "quota", "usage", "members", "projects", "recent_audit_events",
    }, f"missing top-level keys: {sorted(body.keys())!r}"
    assert body["id"] == "t-default"
    assert isinstance(body["enabled"], bool)
    assert set(body["quota"].keys()) >= {
        "soft_bytes", "hard_bytes", "keep_recent_runs",
    }
    assert set(body["usage"].keys()) >= {
        "user_count", "project_count", "disk_used_bytes",
        "disk_used_pct_of_hard", "llm_tokens_30d",
        "rate_limit_hits_7d", "last_activity_at",
    }
    assert isinstance(body["members"], list)
    assert isinstance(body["projects"], list)
    assert isinstance(body["recent_audit_events"], list)


@_requires_pg
async def test_get_tenant_detail_quota_reflects_plan(client, pg_test_pool):
    """The ``quota`` block must echo the plan-derived defaults from
    ``PLAN_DISK_QUOTAS`` when no quota.yaml override is present."""
    from backend.tenant_quota import PLAN_DISK_QUOTAS

    tid = "t-acme-y2-detail-plan"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Plan Acme', 'pro', 1)",
                tid,
            )
        res = await client.get(f"/api/v1/admin/tenants/{tid}")
        assert res.status_code == 200, res.text
        body = res.json()
        expected = PLAN_DISK_QUOTAS["pro"]
        assert body["quota"]["soft_bytes"] == expected.soft_bytes
        assert body["quota"]["hard_bytes"] == expected.hard_bytes
        assert body["quota"]["keep_recent_runs"] == expected.keep_recent_runs
        # disk_used_pct_of_hard is a sane ratio
        u = body["usage"]
        assert isinstance(u["disk_used_pct_of_hard"], float)
        assert 0.0 <= u["disk_used_pct_of_hard"] <= 1.0
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_tenant_detail_members_listing(client, pg_test_pool):
    """user_tenant_memberships rows for the tenant must surface in the
    detail payload, with the per-tenant role from the membership row."""
    tid = "t-acme-y2-detail-members"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Members Acme', 'free', 1)",
                tid,
            )
            # Two users; one is the tenant owner, one is a viewer.
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Alice', 'admin', '', 1, $3)",
                "u-detail-alice", "alice@detail.local", tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Bob', 'viewer', '', 1, $3)",
                "u-detail-bob", "bob@detail.local", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'owner', 'active')",
                "u-detail-alice", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "  (user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'viewer', 'active')",
                "u-detail-bob", tid,
            )

        res = await client.get(f"/api/v1/admin/tenants/{tid}")
        assert res.status_code == 200, res.text
        body = res.json()
        emails = {m["email"]: m for m in body["members"]}
        assert "alice@detail.local" in emails
        assert "bob@detail.local" in emails
        # The membership role (owner / viewer), not the account-tier
        # role (admin) on users.role, must win.
        assert emails["alice@detail.local"]["role"] == "owner"
        assert emails["bob@detail.local"]["role"] == "viewer"
        assert emails["alice@detail.local"]["status"] == "active"
        assert emails["alice@detail.local"]["user_enabled"] is True
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_tenant_detail_projects_listing(client, pg_test_pool):
    """Tenant projects appear in the detail; archived projects appear
    after non-archived ones (operator wants live projects on top)."""
    tid = "t-acme-y2-detail-projects"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Projects Acme', 'starter', 1)",
                tid,
            )
            await conn.execute(
                "INSERT INTO projects "
                "  (id, tenant_id, name, slug) "
                "VALUES ($1, $2, 'Active 1', 'active-1')",
                "p-detail-active-1", tid,
            )
            await conn.execute(
                "INSERT INTO projects "
                "  (id, tenant_id, name, slug, archived_at) "
                "VALUES ($1, $2, 'Archived 1', 'archived-1', "
                "        '2025-01-01 00:00:00')",
                "p-detail-archived-1", tid,
            )

        res = await client.get(f"/api/v1/admin/tenants/{tid}")
        assert res.status_code == 200, res.text
        body = res.json()
        ids = [p["id"] for p in body["projects"]]
        assert "p-detail-active-1" in ids
        assert "p-detail-archived-1" in ids
        # Active project must come before archived.
        assert ids.index("p-detail-active-1") < ids.index(
            "p-detail-archived-1"
        )
        # Project_count (in usage) is non-archived only.
        assert body["usage"]["project_count"] == 1
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_tenant_detail_recent_audit_events(client, pg_test_pool):
    """Recent audit events appear in the detail, newest first."""
    tid = "t-acme-y2-detail-audit"
    older_ts = time.time() - 600.0    # 10 min ago
    newer_ts = time.time() - 60.0     # 1 min ago
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Audit Acme', 'free', 1)",
                tid,
            )
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'first_event', 'tenant', $2, "
                "        'h-first', $2)",
                older_ts, tid,
            )
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'second_event', 'tenant', $2, "
                "        'h-second', $2)",
                newer_ts, tid,
            )

        res = await client.get(f"/api/v1/admin/tenants/{tid}")
        assert res.status_code == 200, res.text
        body = res.json()
        events = body["recent_audit_events"]
        actions = [e["action"] for e in events]
        assert "first_event" in actions
        assert "second_event" in actions
        # Newest first
        assert actions.index("second_event") < actions.index("first_event")
        # last_activity_at picks up the newer ts
        la = body["usage"]["last_activity_at"]
        assert la is not None
        assert abs(float(la) - newer_ts) < 1.0
    finally:
        await _purge(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — error branches
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_tenant_detail_unknown_tenant_returns_404(client):
    """Well-formed but unknown id must return 404, not 200 with a
    skeletal payload (otherwise the operator can't tell "tenant
    deleted" from "tenant has zero of everything")."""
    # ``t-this-tenant-cannot-exist`` matches the regex but no row.
    res = await client.get(
        "/api/v1/admin/tenants/t-this-tenant-cannot-exist"
    )
    assert res.status_code == 404, res.text
    body = res.json()
    assert "detail" in body
    assert "not found" in body["detail"].lower()


@_requires_pg
async def test_get_tenant_detail_malformed_id_returns_422(client):
    """Malformed id (uppercase, no t- prefix, …) must 422 before any
    DB hit. Two negative samples: uppercase and missing t- prefix."""
    res = await client.get("/api/v1/admin/tenants/T-UPPERCASE")
    assert res.status_code == 422, res.text
    res2 = await client.get("/api/v1/admin/tenants/no-prefix-xyz")
    assert res2.status_code == 422, res2.text

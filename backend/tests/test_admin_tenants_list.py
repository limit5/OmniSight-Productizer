"""Y2 (#278) row 2 — tests for GET /api/v1/admin/tenants.

Covers:
  * route is mounted under the api prefix as a GET
  * tenant-admin gets 403 (require_super_admin gate fires)
  * static-shape SQL audit (column / table whitelist; no destructive
    keywords; no SQLite-isms / asyncpg-incompat fingerprints)
  * response envelope shape
      - 200 happy path against live PG with a freshly-seeded tenant
      - includes ``t-default``
      - ``usage`` sub-object includes every contract field
      - ``user_count`` reflects only enabled users
      - ``project_count`` reflects only non-archived projects
      - ``last_activity_at`` is populated by an audit_log row
      - ``llm_tokens_30d`` is populated by an event_log turn.complete row
      - ``rate_limit_hits_7d`` is the contract zero (not yet tracked)
      - ``disk_used_bytes`` is non-negative integer

The PG-gated tests skip on dev where ``OMNI_TEST_PG_URL`` is unset
(consistent with the row-1 ``test_admin_tenants_create.py`` strategy).
"""

from __future__ import annotations

import json
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


def test_get_admin_tenants_route_is_mounted():
    from backend.main import app
    matches = [
        (r.path, sorted(getattr(r, "methods", []) or []))
        for r in app.routes
        if getattr(r, "path", "") == "/api/v1/admin/tenants"
    ]
    methods = [m for _, ms in matches for m in ms]
    assert "GET" in methods, f"GET route missing; got {matches!r}"


def test_list_handler_uses_super_admin_dependency():
    """The dependency surface must gate on require_super_admin so that
    tenant admins (role='admin') are rejected before the handler body
    runs. We assert the dependency identity rather than re-running the
    auth machinery — that's covered by the require_super_admin tests
    in test_admin_tenants_create.py."""
    from backend.routers.admin_tenants import list_tenants
    from backend import auth

    # FastAPI stashes Depends() callables on the function via
    # ``__signature__``. Walk the parameters and check the dep target.
    import inspect
    sig = inspect.signature(list_tenants)
    deps = []
    for name, param in sig.parameters.items():
        default = param.default
        target = getattr(default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"GET /admin/tenants must depend on require_super_admin; "
        f"deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL fingerprint / safety audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_list_sql_is_read_only():
    """No-op safety net: the listing query must not touch any
    destructive verb. A future regression that accidentally mixes a
    CTE-INSERT into the list query would otherwise be hard to spot in
    code review."""
    from backend.routers.admin_tenants import _LIST_TENANTS_SQL
    sql_upper = _LIST_TENANTS_SQL.upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ",
                      "TRUNCATE ", "ALTER ", "GRANT ", "REVOKE "):
        assert forbidden not in sql_upper, (
            f"list SQL must be read-only; found {forbidden!r}"
        )


def test_list_sql_references_expected_tables_only():
    """Whitelist the FROM / JOIN targets so a future drift to a wrong
    table (e.g. ``user`` singular vs ``users`` plural) is caught
    immediately rather than at request time."""
    from backend.routers.admin_tenants import _LIST_TENANTS_SQL
    sql = _LIST_TENANTS_SQL
    for table in ("tenants", "users", "projects", "event_log", "audit_log"):
        assert table in sql, f"expected table {table!r} missing from list SQL"


def test_list_sql_uses_pg_placeholders_or_none():
    """Sanity check: the list query takes no parameters in this revision,
    but if it ever does, they must be PG ``$N`` (asyncpg) not SQLite
    ``?``. This guards against a later edit that copy-pastes a SQLite-
    style filter."""
    from backend.routers.admin_tenants import _LIST_TENANTS_SQL
    # Single ``?`` would only show up legitimately inside a string literal,
    # which we don't have. So zero is the contract.
    assert "?" not in _LIST_TENANTS_SQL, (
        "list SQL must use PG $N placeholders, not SQLite ?"
    )


def test_list_sql_fingerprint_clean():
    """SOP Step-3 fingerprint grep on the SQL constant: catch the four
    classic compat-residue patterns at module-load time."""
    import re as _re
    from backend.routers.admin_tenants import _LIST_TENANTS_SQL
    fingerprint = _re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(_LIST_TENANTS_SQL)


def test_measure_disk_safely_returns_zero_for_unknown_tenant(tmp_path,
                                                             monkeypatch):
    """``_measure_disk_safely`` must never raise — a missing data dir
    (the common case for a tenant just created via POST) must map to 0.
    """
    from backend.routers import admin_tenants as mod
    # Point the data root at an empty tmp dir so measurement walks
    # nothing.
    from backend import tenant_fs
    monkeypatch.setattr(tenant_fs, "_data_root_override", tmp_path,
                        raising=False)
    n = mod._measure_disk_safely("t-does-not-exist-anywhere")
    assert isinstance(n, int)
    assert n >= 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: tenant admin gets 403
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_admin_tenants_tenant_admin_gets_403(client):
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadmin-list", email="tadmin-list@acme.local",
        name="Tenant Admin (list)", role="admin", enabled=True,
        tenant_id="t-acme-y2-list-rbac",
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
        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path on live PG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _purge(pg_test_pool, tid: str) -> None:
    """Best-effort cleanup. The HTTP path commits real rows; each test
    is responsible for removing rows it created (the savepoint-rollback
    only protects ``pg_test_conn`` tests; this fixture commits)."""
    async with pg_test_pool.acquire() as conn:
        # Order matters: child rows first because the FK to tenants
        # cascades on tenant delete but not the inverse.
        await conn.execute("DELETE FROM event_log WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM audit_log WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM users WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_get_admin_tenants_envelope_and_default_tenant(client):
    """Smallest possible happy path: with whatever rows currently live
    in PG, GET must 200 and produce the agreed envelope shape, and
    ``t-default`` (seeded by alembic 0012) must appear."""
    res = await client.get("/api/v1/admin/tenants")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "tenants" in body and isinstance(body["tenants"], list)
    ids = [t["id"] for t in body["tenants"]]
    assert "t-default" in ids, (
        "seeded t-default tenant must be in the listing; "
        f"got ids={ids!r}"
    )
    # Every row must carry the contract fields.
    for t in body["tenants"]:
        assert set(t.keys()) >= {
            "id", "name", "plan", "enabled", "created_at", "usage",
        }, f"row missing top-level fields: {t!r}"
        assert set(t["usage"].keys()) >= {
            "user_count", "project_count", "disk_used_bytes",
            "llm_tokens_30d", "rate_limit_hits_7d", "last_activity_at",
        }, f"row usage missing fields: {t['usage']!r}"
        # Type sanity
        assert isinstance(t["enabled"], bool)
        u = t["usage"]
        assert isinstance(u["user_count"], int) and u["user_count"] >= 0
        assert isinstance(u["project_count"], int) and u["project_count"] >= 0
        assert isinstance(u["disk_used_bytes"], int) and u["disk_used_bytes"] >= 0
        assert isinstance(u["llm_tokens_30d"], int) and u["llm_tokens_30d"] >= 0
        assert u["rate_limit_hits_7d"] == 0
        assert (
            u["last_activity_at"] is None
            or isinstance(u["last_activity_at"], (int, float))
        )


@_requires_pg
async def test_get_admin_tenants_user_and_project_counts(client, pg_test_pool):
    """Seed 2 users (1 enabled + 1 disabled) and 2 projects (1 active +
    1 archived) on a fresh tenant; the listing must report
    user_count=1 / project_count=1 (active rows only)."""
    tid = "t-acme-y2-list-counts"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Counts Acme', 'pro', 1)",
                tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Active', 'viewer', '', 1, $3)",
                "u-counts-active", "active@counts.local", tid,
            )
            await conn.execute(
                "INSERT INTO users (id, email, name, role, "
                "  password_hash, enabled, tenant_id) "
                "VALUES ($1, $2, 'Disabled', 'viewer', '', 0, $3)",
                "u-counts-disabled", "disabled@counts.local", tid,
            )
            await conn.execute(
                "INSERT INTO projects "
                "  (id, tenant_id, name, slug) "
                "VALUES ($1, $2, 'Active proj', 'active-proj')",
                "p-counts-active", tid,
            )
            await conn.execute(
                "INSERT INTO projects "
                "  (id, tenant_id, name, slug, archived_at) "
                "VALUES ($1, $2, 'Archived proj', 'archived-proj', "
                "        '2025-01-01 00:00:00')",
                "p-counts-archived", tid,
            )

        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tenants"]}
        assert tid in rows
        usage = rows[tid]["usage"]
        assert usage["user_count"] == 1, (
            f"only enabled users counted; got {usage!r}"
        )
        assert usage["project_count"] == 1, (
            f"only non-archived projects counted; got {usage!r}"
        )
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM users WHERE id IN ('u-counts-active', "
                "                               'u-counts-disabled')"
            )
            await conn.execute(
                "DELETE FROM projects WHERE id IN ('p-counts-active', "
                "                                  'p-counts-archived')"
            )
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_admin_tenants_last_activity_from_audit(client, pg_test_pool):
    """A fresh tenant with one audit_log row must surface the row's
    ``ts`` as ``last_activity_at`` (UNIX float)."""
    tid = "t-acme-y2-list-activity"
    audit_ts = time.time() - 60.0  # 1 minute ago
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Activity Acme', 'free', 1)", tid,
            )
            await conn.execute(
                "INSERT INTO audit_log "
                "  (ts, actor, action, entity_kind, entity_id, "
                "   curr_hash, tenant_id) "
                "VALUES ($1, 'system', 'sentinel', 'tenant', $2, "
                "        'sentinel-hash', $2)",
                audit_ts, tid,
            )

        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tenants"]}
        assert tid in rows
        la = rows[tid]["usage"]["last_activity_at"]
        assert la is not None, (
            "expected last_activity_at populated from audit_log row"
        )
        # Allow tiny rounding noise from REAL → float JSON marshalling.
        assert abs(float(la) - audit_ts) < 1.0
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_admin_tenants_llm_tokens_from_event_log(client,
                                                          pg_test_pool):
    """One ``turn.complete`` event_log row with tokens_used=1234 inside
    the 30-day window must show as ``llm_tokens_30d=1234``."""
    tid = "t-acme-y2-list-tokens"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Tokens Acme', 'pro', 1)", tid,
            )
            await conn.execute(
                "INSERT INTO event_log (event_type, data_json, tenant_id) "
                "VALUES ('turn.complete', $1, $2)",
                json.dumps({"tokens_used": 1234, "cost_usd": 0.05}),
                tid,
            )

        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tenants"]}
        assert tid in rows
        assert rows[tid]["usage"]["llm_tokens_30d"] == 1234
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_admin_tenants_no_activity_returns_null(client,
                                                         pg_test_pool):
    """Brand-new tenant with no audit / event / disk rows must surface
    ``last_activity_at=None`` and zero usage counters."""
    tid = "t-acme-y2-list-empty"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Empty Acme', 'free', 1)", tid,
            )

        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tenants"]}
        assert tid in rows
        u = rows[tid]["usage"]
        assert u["user_count"] == 0
        assert u["project_count"] == 0
        assert u["llm_tokens_30d"] == 0
        assert u["last_activity_at"] is None
        assert u["rate_limit_hits_7d"] == 0
    finally:
        await _purge(pg_test_pool, tid)


@_requires_pg
async def test_get_admin_tenants_excludes_old_token_events(client,
                                                          pg_test_pool):
    """A turn.complete event older than 30 days must NOT contribute to
    ``llm_tokens_30d``. Insert via explicit ``created_at`` (TEXT) so we
    don't depend on system clock tricks."""
    tid = "t-acme-y2-list-oldtokens"
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tenants (id, name, plan, enabled) "
                "VALUES ($1, 'Old Tokens Acme', 'free', 1)", tid,
            )
            # 60 days ago — comfortably outside the 30-day window.
            await conn.execute(
                "INSERT INTO event_log "
                "  (event_type, data_json, tenant_id, created_at) "
                "VALUES ('turn.complete', $1, $2, "
                "        to_char(NOW() - INTERVAL '60 days', "
                "                'YYYY-MM-DD HH24:MI:SS'))",
                json.dumps({"tokens_used": 99999}),
                tid,
            )

        res = await client.get("/api/v1/admin/tenants")
        assert res.status_code == 200, res.text
        rows = {t["id"]: t for t in res.json()["tenants"]}
        assert tid in rows
        assert rows[tid]["usage"]["llm_tokens_30d"] == 0, (
            "events older than 30 days must be excluded"
        )
    finally:
        await _purge(pg_test_pool, tid)

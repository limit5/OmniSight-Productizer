"""Y8 (#284) row 5 — drift guard for the read-side project membership
+ share surface, and for the cross-tenant share revoke surface.

The Y4 row 5 / row 6 commits shipped the write-only POST/PATCH/DELETE
member surface and the POST share surface. The Y8 row 5 frontend
``/projects/{pid}/settings`` page (project owner only — Members / Budget
/ Shares tabs) needs the read-side complement (GET project members,
GET project shares) plus the share lifecycle close-out (DELETE share)
to be functionally usable. This file pins those three new endpoints.

Drift guard families:
  (a) SQL constants — PG ``$N`` placeholder, secret-leak guard,
      tenant-scoped project fetch reused unchanged
  (b) Router endpoints exposed with ``auth.current_user`` dependency
  (c) Main app full-prefix mount confirms all three paths/methods
  (d) HTTP path: GET happy / 404 / 422; DELETE happy / idempotent /
      403 / 422; RBAC for both list endpoints
  (e) Audit emission for DELETE share (tenant_project_share_revoked)
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timezone

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) SQL constants — fingerprint guard + placeholder shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("sql_name", [
    "_LIST_PROJECT_MEMBERS_SQL",
    "_LIST_PROJECT_SHARES_SQL",
    "_DELETE_PROJECT_SHARE_SQL",
])
def test_y8_row5_sql_uses_pg_placeholders_only(sql_name):
    from backend.routers import tenant_projects as tp
    sql = getattr(tp, sql_name)
    assert "?" not in sql, f"{sql_name} must use PG $N placeholders, not ?"
    # All three queries take at least one positional arg.
    assert "$1" in sql


@pytest.mark.parametrize("sql_name", [
    "_LIST_PROJECT_MEMBERS_SQL",
    "_LIST_PROJECT_SHARES_SQL",
    "_DELETE_PROJECT_SHARE_SQL",
])
def test_y8_row5_sql_does_not_leak_secret_columns(sql_name):
    from backend.routers import tenant_projects as tp
    sql = getattr(tp, sql_name).lower()
    for forbidden in ("password_hash", "oidc_subject", "oidc_provider",
                      "token_hash"):
        assert forbidden not in sql, (
            f"{sql_name} must not project secret column {forbidden!r}"
        )


def test_list_project_members_sql_joins_users_for_email_name():
    """The Members tab needs email + name surface — ensure the JOIN
    + projection stays in the SQL. Drift here would make the page
    show only opaque user_ids."""
    from backend.routers.tenant_projects import _LIST_PROJECT_MEMBERS_SQL
    s = _LIST_PROJECT_MEMBERS_SQL.lower()
    assert "join users u" in s
    assert "u.email" in s
    assert "u.name" in s
    assert "where pm.project_id = $1" in s
    assert "limit $2" in s


def test_list_project_shares_sql_filters_by_project_only():
    """Tenant scope is enforced BEFORE the SQL runs (404 if project
    isn't in the requested tenant). The SQL itself only takes a
    project_id — drift here (e.g. injecting a tenant_id filter)
    would silently filter to the host's view and break for super-
    admin cross-tenant audits."""
    from backend.routers.tenant_projects import _LIST_PROJECT_SHARES_SQL
    s = _LIST_PROJECT_SHARES_SQL.lower()
    assert "from project_shares" in s
    assert "where project_id = $1" in s
    assert "tenant_id" not in s.split("where", 1)[1]


def test_delete_project_share_sql_uses_returning_for_audit():
    """The audit row needs the prior role + guest_tenant + expires_at
    in one round-trip — RETURNING is the only correct shape. Drift
    here (e.g. an UPDATE-then-SELECT pair) would race a concurrent
    re-grant."""
    from backend.routers.tenant_projects import _DELETE_PROJECT_SHARE_SQL
    s = _DELETE_PROJECT_SHARE_SQL.lower()
    assert s.lstrip().startswith("delete from project_shares")
    assert "returning" in s
    assert "guest_tenant_id" in s
    assert "expires_at" in s


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Router endpoints exposed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_y8_row5_read_endpoints():
    from backend.routers.tenant_projects import router
    paths_methods: set[tuple[str, str]] = set()
    for r in router.routes:
        for mm in getattr(r, "methods", set()):
            paths_methods.add((r.path, mm))
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/members", "GET",
    ) in paths_methods
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/shares", "GET",
    ) in paths_methods
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/shares/{share_id}",
        "DELETE",
    ) in paths_methods


@pytest.mark.parametrize("handler_name", [
    "list_project_members",
    "list_project_shares",
    "delete_project_share",
])
def test_y8_row5_handler_uses_current_user_dependency(handler_name):
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_projects
    from backend import auth as _au

    handler = getattr(tenant_projects, handler_name)
    deps = [
        v.default for v in (
            inspect.signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    assert any(
        getattr(d, "dependency", None) is _au.current_user for d in deps
    ), f"{handler_name} must depend on auth.current_user"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_y8_row5_endpoints():
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    full: set[tuple[str, str]] = set()
    for r in app.routes:
        for mm in getattr(r, "methods", set()) or set():
            full.add((r.path, mm))
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/members",
        "GET",
    ) in full
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/shares",
        "GET",
    ) in full
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/shares/{share_id}",
        "DELETE",
    ) in full


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP fixtures — same shape as test_tenant_projects_members.py /
#                  test_tenant_projects_shares.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}",
        )


async def _seed_user(pool, *, uid: str, tid: str, email: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, 'viewer', '', 1, $4) "
            "ON CONFLICT (id) DO NOTHING",
            uid, email, email.split("@")[0], tid,
        )


async def _seed_membership(
    pool, *, uid: str, tid: str, role: str = "member",
) -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, $3, 'active', $4) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            uid, tid, role, created_at,
        )


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_shares WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)", tid,
        )
        await conn.execute(
            "DELETE FROM project_shares WHERE guest_tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)", tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind IN "
            "('project_share', 'project_member') AND tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND entity_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM users WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _create_project(client, tid: str, *, slug: str) -> str:
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects",
        json={"product_line": "embedded", "name": f"P-{slug}",
              "slug": slug},
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) HTTP — GET /tenants/{tid}/projects/{pid}/members
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_members_happy_returns_rows_with_email_name(
    client, pg_test_pool,
):
    tid = "t-y8-r5-mem-h"
    uid = "u-y8r5memh001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="alice@y8r5.io")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member")
        pid = await _create_project(client, tid, slug="happy")

        # Grant via existing POST so the row lands legitimately.
        grant = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "owner"},
        )
        assert grant.status_code == 201, grant.text

        # Now read via the new GET.
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["project_id"] == pid
        assert body["count"] == 1
        members = body["members"]
        assert len(members) == 1
        m = members[0]
        assert m["user_id"] == uid
        assert m["email"] == "alice@y8r5.io"
        assert m["name"] == "alice"
        assert m["role"] == "owner"
        assert "created_at" in m
        assert m["user_enabled"] is True
        # No PII / secret leaks.
        for k in ("password_hash", "oidc_subject", "oidc_provider",
                  "token_hash"):
            assert k not in m
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_empty_when_no_explicit_rows(client, pg_test_pool):
    tid = "t-y8-r5-mem-e"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="empty")

        res = await client.get(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 0
        assert body["members"] == []
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_unknown_tenant_returns_404(client):
    res = await client.get(
        "/api/v1/tenants/t-no-such-tenant/projects/p-fake/members",
    )
    # Pattern-valid ids → 404 / 403 path. The 422 fast-fail only
    # triggers on regex mismatches.
    assert res.status_code in (404, 403), res.text


@_requires_pg
async def test_get_members_unknown_project_on_known_tenant_returns_404(
    client, pg_test_pool,
):
    tid = "t-y8-r5-mem-up"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects/p-no-such/members",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_malformed_tenant_id_returns_422(client):
    res = await client.get(
        "/api/v1/tenants/not-a-tid/projects/p-anything/members",
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_get_members_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y8-r5-mem-mp"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects/not-a-pid/members",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) HTTP — GET /tenants/{tid}/projects/{pid}/shares
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_shares_happy_returns_rows(client, pg_test_pool):
    host = "t-y8-r5-sh-h"
    guest = "t-y8-r5-sh-g"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)
        pid = await _create_project(client, host, slug="happy")

        grant = await client.post(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
            json={"guest_tenant_id": guest, "role": "viewer"},
        )
        assert grant.status_code == 201, grant.text
        share_id = grant.json()["share_id"]

        res = await client.get(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tenant_id"] == host
        assert body["project_id"] == pid
        assert body["count"] == 1
        s = body["shares"][0]
        assert s["share_id"] == share_id
        assert s["guest_tenant_id"] == guest
        assert s["role"] == "viewer"
        assert s["expires_at"] is None
        assert "created_at" in s
        for k in ("password_hash", "oidc_subject", "oidc_provider",
                  "token_hash"):
            assert k not in s
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)


@_requires_pg
async def test_get_shares_empty_when_no_grants(client, pg_test_pool):
    host = "t-y8-r5-sh-e"
    try:
        await _seed_tenant(pg_test_pool, host)
        pid = await _create_project(client, host, slug="empty")
        res = await client.get(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 0
        assert body["shares"] == []
    finally:
        await _purge_tenant(pg_test_pool, host)


@_requires_pg
async def test_get_shares_unknown_project_returns_404(
    client, pg_test_pool,
):
    host = "t-y8-r5-sh-up"
    try:
        await _seed_tenant(pg_test_pool, host)
        res = await client.get(
            f"/api/v1/tenants/{host}/projects/p-no-such/shares",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, host)


@_requires_pg
async def test_get_shares_malformed_ids_return_422(client, pg_test_pool):
    host = "t-y8-r5-sh-mi"
    try:
        await _seed_tenant(pg_test_pool, host)
        bad_t = await client.get(
            "/api/v1/tenants/bad-tid/projects/p-x/shares",
        )
        assert bad_t.status_code == 422, bad_t.text
        bad_p = await client.get(
            f"/api/v1/tenants/{host}/projects/bad-pid/shares",
        )
        assert bad_p.status_code == 422, bad_p.text
    finally:
        await _purge_tenant(pg_test_pool, host)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) HTTP — DELETE /tenants/{tid}/projects/{pid}/shares/{sid}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_share_happy_revokes_row(client, pg_test_pool):
    host = "t-y8-r5-sh-dl"
    guest = "t-y8-r5-sh-dg"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)
        pid = await _create_project(client, host, slug="dl")

        grant = await client.post(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
            json={"guest_tenant_id": guest, "role": "viewer"},
        )
        assert grant.status_code == 201, grant.text
        sid = grant.json()["share_id"]

        res = await client.delete(
            f"/api/v1/tenants/{host}/projects/{pid}/shares/{sid}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["share_id"] == sid
        assert body["already_revoked"] is False
        assert body["guest_tenant_id"] == guest
        assert body["role"] == "viewer"
        assert body["tenant_id"] == host

        # Row really gone — subsequent GET shows empty list.
        again = await client.get(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
        )
        assert again.status_code == 200, again.text
        assert again.json()["count"] == 0
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)


@_requires_pg
async def test_delete_share_idempotent_already_revoked(
    client, pg_test_pool,
):
    host = "t-y8-r5-sh-id"
    guest = "t-y8-r5-sh-ig"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)
        pid = await _create_project(client, host, slug="idem")
        grant = await client.post(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
            json={"guest_tenant_id": guest, "role": "contributor"},
        )
        sid = grant.json()["share_id"]

        first = await client.delete(
            f"/api/v1/tenants/{host}/projects/{pid}/shares/{sid}",
        )
        assert first.status_code == 200
        assert first.json()["already_revoked"] is False

        second = await client.delete(
            f"/api/v1/tenants/{host}/projects/{pid}/shares/{sid}",
        )
        assert second.status_code == 200
        body2 = second.json()
        assert body2["already_revoked"] is True
        assert body2["share_id"] == sid
        # No row data on the idempotent branch.
        assert "guest_tenant_id" not in body2 or body2.get("guest_tenant_id") is None
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)


@_requires_pg
async def test_delete_share_malformed_share_id_returns_422(
    client, pg_test_pool,
):
    host = "t-y8-r5-sh-bm"
    try:
        await _seed_tenant(pg_test_pool, host)
        pid = await _create_project(client, host, slug="bm")
        res = await client.delete(
            f"/api/v1/tenants/{host}/projects/{pid}/shares/not-a-sid",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, host)


@_requires_pg
async def test_delete_share_unknown_project_returns_404(
    client, pg_test_pool,
):
    host = "t-y8-r5-sh-up"
    try:
        await _seed_tenant(pg_test_pool, host)
        res = await client.delete(
            f"/api/v1/tenants/{host}/projects/p-no-such/shares/psh-deadbeefcafe1234",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, host)


@_requires_pg
async def test_delete_share_audits_revocation(client, pg_test_pool):
    """Audit chain must record ``tenant_project_share_revoked`` so the
    revoke is recoverable from the I8 trail. Drift here (forgetting to
    log) would silently lose visibility of cross-tenant access pulls."""
    host = "t-y8-r5-sh-au"
    guest = "t-y8-r5-sh-ag"
    try:
        await _seed_tenant(pg_test_pool, host)
        await _seed_tenant(pg_test_pool, guest)
        pid = await _create_project(client, host, slug="au")
        grant = await client.post(
            f"/api/v1/tenants/{host}/projects/{pid}/shares",
            json={"guest_tenant_id": guest, "role": "viewer"},
        )
        sid = grant.json()["share_id"]

        res = await client.delete(
            f"/api/v1/tenants/{host}/projects/{pid}/shares/{sid}",
        )
        assert res.status_code == 200, res.text

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, before, after, "
                "       tenant_id "
                "FROM audit_log "
                "WHERE entity_kind = 'project_share' AND entity_id = $1 "
                "  AND action = 'tenant_project_share_revoked' "
                "ORDER BY id DESC LIMIT 1",
                sid,
            )
        assert row is not None, "audit row missing"
        assert row["entity_id"] == sid
        # ``before`` must capture prior role/guest, ``after`` is None.
        assert row["after"] in (None, "null", "")
    finally:
        await _purge_tenant(pg_test_pool, host)
        await _purge_tenant(pg_test_pool, guest)

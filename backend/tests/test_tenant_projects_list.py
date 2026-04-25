"""Y4 (#280) row 2 — drift guard for GET /api/v1/tenants/{tid}/projects.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (visibility tiers, archived filter,
product_line filter, RBAC denial, secret-leak audit) and skip when
``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — LISTABLE_PROJECT_ARCHIVED_FILTERS /
      PROJECTS_LIST_*_LIMIT / _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES
      tuple + frozenset shapes.
  (b) SQL constants — three archived branches × pg-placeholder /
      no-secret-leak / read-only verb / project_members EXISTS shape /
      product_line filter shape.
  (c) Router endpoint mounted with ``auth.current_user`` dependency.
  (d) Main app full-prefix mount confirms
      ``GET /api/v1/tenants/{tenant_id}/projects``.
  (e) HTTP path: super_admin sees all / tenant admin sees all /
      member sees only explicit project_members rows / viewer sees
      only explicit / no-membership 403 / archived filter (false /
      true / all) / product_line filter / unknown tenant 404 /
      malformed tid 422 / unknown product_line 422 / unknown
      archived 422 / limit clamp.
  (f) Self-fingerprint guard.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (a) Module-level constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_listable_archived_filters_match_documented_shape():
    """The TODO row literal accepts an ``archived=`` query param. The
    handler maps it onto a 3-value enum; drift on this list would
    silently flip the default behaviour."""
    from backend.routers import tenant_projects
    assert tenant_projects.LISTABLE_PROJECT_ARCHIVED_FILTERS == (
        "false", "true", "all",
    )


def test_projects_list_limits_relationship():
    """Default must be ≤ Max (otherwise the default itself 422s)."""
    from backend.routers.tenant_projects import (
        PROJECTS_LIST_DEFAULT_LIMIT,
        PROJECTS_LIST_MAX_LIMIT,
    )
    assert PROJECTS_LIST_DEFAULT_LIMIT >= 1
    assert PROJECTS_LIST_DEFAULT_LIMIT <= PROJECTS_LIST_MAX_LIMIT
    # Conservative shape — keeps surface comparable to Y3 invites.
    assert PROJECTS_LIST_DEFAULT_LIMIT == 100
    assert PROJECTS_LIST_MAX_LIMIT == 500


def test_full_visibility_membership_roles_is_admin_tier_only():
    """Only owner / admin tenant-membership rows get full visibility.
    Default-resolution semantics from alembic 0034 — member / viewer
    fall through to explicit-only access."""
    from backend.routers.tenant_projects import (
        _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES,
    )
    assert _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )
    # Defence in depth: must NOT contain member / viewer.
    assert "member" not in _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES
    assert "viewer" not in _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) SQL constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_LIST_SQL_NAMES = (
    "_LIST_PROJECTS_LIVE_SQL",
    "_LIST_PROJECTS_ARCHIVED_SQL",
    "_LIST_PROJECTS_ALL_SQL",
)


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_uses_pg_placeholders_only(sql_name):
    """Drift guard: SQLite-style ``?`` placeholders silently break
    asyncpg. Every list SQL must use ``$N``."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_does_not_leak_secret_columns(sql_name):
    """List SQL projects only project columns; no users.password_hash /
    oidc_* / token_hash should accidentally land here."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "password_hash" not in sql, f"{sql_name} projects password_hash"
    assert "oidc_subject" not in sql, f"{sql_name} projects oidc_subject"
    assert "oidc_provider" not in sql, f"{sql_name} projects oidc_provider"
    assert "token_hash" not in sql, f"{sql_name} projects token_hash"


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_is_read_only(sql_name):
    """List SQL must be SELECT-only (no UPDATE / INSERT / DELETE leaks
    in the same module)."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name).upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in sql, f"{sql_name} contains {verb!r}"
    assert "SELECT" in sql


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_filters_by_tenant_id(sql_name):
    """Every list SQL must scope by tenant_id (placeholder $1).
    A drift that drops the tenant_id filter would expose other tenants'
    project ids — the most serious bug class for a multi-tenant API."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "p.tenant_id = $1" in sql, sql


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_full_visibility_short_circuit(sql_name):
    """The visibility branch must use ``$2::bool OR EXISTS(...)`` so
    full-visibility callers skip the per-row project_members probe.
    Drift here would either over-filter (admins lose visibility) or
    under-filter (members see other people's projects)."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "$2::bool" in sql, sql
    assert "project_members pm" in sql, sql
    assert "pm.project_id = p.id" in sql, sql
    assert "pm.user_id = $3" in sql, sql


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_product_line_filter_shape(sql_name):
    """Optional product_line filter is encoded as
    ``$4::text IS NULL OR p.product_line = $4`` so the same SQL
    handles both filtered and unfiltered cases."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "$4::text IS NULL OR p.product_line = $4" in sql, sql


def test_live_sql_filters_archived_at_is_null():
    from backend.routers.tenant_projects import _LIST_PROJECTS_LIVE_SQL
    assert "p.archived_at IS NULL" in _LIST_PROJECTS_LIVE_SQL


def test_archived_sql_filters_archived_at_is_not_null():
    from backend.routers.tenant_projects import _LIST_PROJECTS_ARCHIVED_SQL
    assert "p.archived_at IS NOT NULL" in _LIST_PROJECTS_ARCHIVED_SQL


def test_all_sql_does_not_filter_archived():
    """``archived=all`` returns both — neither IS NULL nor IS NOT NULL
    predicate may appear on archived_at."""
    from backend.routers.tenant_projects import _LIST_PROJECTS_ALL_SQL
    assert "archived_at IS NULL" not in _LIST_PROJECTS_ALL_SQL
    assert "archived_at IS NOT NULL" not in _LIST_PROJECTS_ALL_SQL


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_orders_newest_first(sql_name):
    """Stable newest-first ordering with id tie-breaker — same shape
    as the Y3 invite list."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "ORDER BY p.created_at DESC, p.id DESC" in sql


@pytest.mark.parametrize("sql_name", _LIST_SQL_NAMES)
def test_list_sql_has_limit_placeholder(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    assert "LIMIT $5" in sql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) Router wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_get_endpoint():
    from backend.routers.tenant_projects import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("GET",), "/tenants/{tenant_id}/projects") in paths


def test_get_handler_depends_on_current_user():
    from backend.routers import tenant_projects
    from backend import auth
    fn = tenant_projects.list_projects
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.current_user in deps, (
        f"list_projects must depend on auth.current_user; "
        f"deps were {deps!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_list_projects_endpoint():
    """End-to-end: backend.main exposes the endpoint at
    ``GET /api/v1/tenants/{tenant_id}/projects``."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (("GET",), "/api/v1/tenants/{tenant_id}/projects") in paths


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) HTTP path — happy + error + visibility branches (require live PG)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}",
        )


async def _seed_user(
    pool, uid: str, email: str, *, tenant_id: str, account_role: str = "viewer",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, $4, '', 1, $5) "
            "ON CONFLICT (id) DO NOTHING",
            uid, email, email, account_role, tenant_id,
        )


async def _seed_membership(pool, uid: str, tid: str, role: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status) "
            "VALUES ($1, $2, $3, 'active') "
            "ON CONFLICT (user_id, tenant_id) DO UPDATE SET "
            "role = EXCLUDED.role, status = 'active'",
            uid, tid, role,
        )


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND entity_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _purge_user(pool, uid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)


async def _create_project(
    client, tid: str, *, product_line: str, slug: str, name: str | None = None,
) -> str:
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects",
        json={
            "product_line": product_line,
            "name": name or f"P-{slug}",
            "slug": slug,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


async def _archive_project(pool, project_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE projects SET archived_at = "
            "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS') "
            "WHERE id = $1",
            project_id,
        )


@_requires_pg
async def test_get_projects_super_admin_sees_all(client, pg_test_pool):
    """Default open-mode caller is super_admin; must see every project
    in the tenant regardless of project_members rows."""
    tid = "t-y4-list-superadmin"
    try:
        await _seed_tenant(pg_test_pool, tid)
        p1 = await _create_project(
            client, tid, product_line="embedded", slug="a",
        )
        p2 = await _create_project(
            client, tid, product_line="web", slug="b",
        )

        res = await client.get(f"/api/v1/tenants/{tid}/projects")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["product_line_filter"] is None
        assert body["archived_filter"] == "false"
        assert body["count"] == 2
        ids = {p["project_id"] for p in body["projects"]}
        assert ids == {p1, p2}
        # Response shape — every documented field present on each row.
        for p in body["projects"]:
            for key in (
                "project_id", "tenant_id", "product_line", "name", "slug",
                "parent_id", "plan_override", "disk_budget_bytes",
                "llm_budget_tokens", "created_by", "created_at", "archived_at",
            ):
                assert key in p, f"missing response key: {key}"
            assert p["tenant_id"] == tid
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_filters_by_product_line(client, pg_test_pool):
    tid = "t-y4-list-pl"
    try:
        await _seed_tenant(pg_test_pool, tid)
        p_emb = await _create_project(
            client, tid, product_line="embedded", slug="emb",
        )
        await _create_project(
            client, tid, product_line="web", slug="web",
        )

        res = await client.get(
            f"/api/v1/tenants/{tid}/projects?product_line=embedded",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["product_line_filter"] == "embedded"
        assert body["count"] == 1
        assert body["projects"][0]["project_id"] == p_emb
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_archived_filter_branches(client, pg_test_pool):
    """archived=false (default) hides archived; archived=true shows
    only archived; archived=all shows both."""
    tid = "t-y4-list-arch"
    try:
        await _seed_tenant(pg_test_pool, tid)
        live_id = await _create_project(
            client, tid, product_line="embedded", slug="live",
        )
        archived_id = await _create_project(
            client, tid, product_line="embedded", slug="dead",
        )
        await _archive_project(pg_test_pool, archived_id)

        # Default: only live.
        r1 = await client.get(f"/api/v1/tenants/{tid}/projects")
        ids1 = {p["project_id"] for p in r1.json()["projects"]}
        assert ids1 == {live_id}

        # Archived only.
        r2 = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=true",
        )
        ids2 = {p["project_id"] for p in r2.json()["projects"]}
        assert ids2 == {archived_id}

        # All.
        r3 = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=all",
        )
        ids3 = {p["project_id"] for p in r3.json()["projects"]}
        assert ids3 == {live_id, archived_id}
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_tenant_admin_sees_all_without_explicit_membership(
    client, pg_test_pool,
):
    """A user with tenant membership role='admin' sees every project
    in the tenant regardless of project_members rows (default
    contributor on every project per alembic 0034)."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-list-tadmin"
    uid = "u-y4listadminxx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid, "tadmin@example.com", tenant_id=tid)
        await _seed_membership(pg_test_pool, uid, tid, "admin")

        # Two projects, no project_members rows for the admin.
        p1 = await _create_project(
            client, tid, product_line="embedded", slug="a",
        )
        p2 = await _create_project(
            client, tid, product_line="embedded", slug="b",
        )

        # Now flip the caller to the tenant admin (not super_admin).
        tadmin = _au.User(
            id=uid, email="tadmin@example.com", name="tadmin",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return tadmin

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/projects")
            assert res.status_code == 200, res.text
            ids = {p["project_id"] for p in res.json()["projects"]}
            assert ids == {p1, p2}
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_member_sees_only_explicit_project_members(
    client, pg_test_pool,
):
    """A user with tenant membership role='member' sees ONLY projects
    where they have an explicit project_members row. The other
    projects in the same tenant are filtered out."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-list-member"
    uid = "u-y4listmemberx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid, "member@example.com", tenant_id=tid)
        await _seed_membership(pg_test_pool, uid, tid, "member")

        # Two projects: one the member is explicitly on, one they aren't.
        explicit = await _create_project(
            client, tid, product_line="embedded", slug="mine",
        )
        await _create_project(
            client, tid, product_line="embedded", slug="not-mine",
        )

        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, 'contributor') "
                "ON CONFLICT (user_id, project_id) DO NOTHING",
                uid, explicit,
            )

        member = _au.User(
            id=uid, email="member@example.com", name="member",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return member

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/projects")
            assert res.status_code == 200, res.text
            ids = {p["project_id"] for p in res.json()["projects"]}
            assert ids == {explicit}, (
                "member must see only projects with explicit "
                "project_members row; got " + repr(ids)
            )
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_viewer_sees_only_explicit_project_members(
    client, pg_test_pool,
):
    """A user with tenant membership role='viewer' is the same as
    ``member`` for visibility — explicit project_members only."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-list-viewer"
    uid = "u-y4listviewerx"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid, "viewer@example.com", tenant_id=tid)
        await _seed_membership(pg_test_pool, uid, tid, "viewer")

        explicit = await _create_project(
            client, tid, product_line="embedded", slug="ok",
        )
        await _create_project(
            client, tid, product_line="embedded", slug="hidden",
        )
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, 'viewer') "
                "ON CONFLICT (user_id, project_id) DO NOTHING",
                uid, explicit,
            )

        viewer = _au.User(
            id=uid, email="viewer@example.com", name="viewer",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return viewer

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/projects")
            assert res.status_code == 200, res.text
            ids = {p["project_id"] for p in res.json()["projects"]}
            assert ids == {explicit}
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_no_membership_returns_403(client, pg_test_pool):
    """A user with no membership row at all (and not super_admin)
    must NOT be able to enumerate the tenant's project list."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-list-stranger"
    uid = "u-y4liststranger"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Stranger user exists but has NO membership row on this tenant.
        await _seed_user(
            pg_test_pool, uid, "stranger@example.com", tenant_id="t-default",
        )
        await _create_project(
            client, tid, product_line="embedded", slug="secret",
        )

        stranger = _au.User(
            id=uid, email="stranger@example.com", name="stranger",
            role="viewer", enabled=True, tenant_id="t-default",
        )

        async def _fake_current_user():
            return stranger

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/projects")
            assert res.status_code == 403, res.text
            assert "active membership" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_suspended_membership_treated_as_no_membership(
    client, pg_test_pool,
):
    """A *suspended* membership row gets the same 403 as no
    membership — same convention as Y3 row 6 PATCH/DELETE."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-list-suspended"
    uid = "u-y4listsuspend"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(
            pg_test_pool, uid, "suspended@example.com", tenant_id=tid,
        )
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'suspended') "
                "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                uid, tid,
            )

        suspended = _au.User(
            id=uid, email="suspended@example.com", name="suspended",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return suspended

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/projects")
            assert res.status_code == 403, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_unknown_tenant_returns_404(client):
    res = await client.get("/api/v1/tenants/t-y4-missing-x/projects")
    assert res.status_code == 404, res.text


@_requires_pg
async def test_get_projects_malformed_tenant_id_returns_422(client):
    res = await client.get("/api/v1/tenants/T-Bad-Id/projects")
    assert res.status_code == 422, res.text


@_requires_pg
async def test_get_projects_unknown_product_line_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-list-bad-pl"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects?product_line=iot",
        )
        assert res.status_code == 422, res.text
        assert "product_line" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_unknown_archived_filter_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-list-bad-arch"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects?archived=maybe",
        )
        assert res.status_code == 422, res.text
        assert "archived" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_oversize_limit_returns_422(client, pg_test_pool):
    tid = "t-y4-list-bigl"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # 501 > MAX (500) → pydantic Query bound 422.
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects?limit=501",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_zero_limit_returns_422(client, pg_test_pool):
    tid = "t-y4-list-zerol"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/projects?limit=0",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_projects_does_not_leak_other_tenants_rows(
    client, pg_test_pool,
):
    """Two tenants each with a project; listing tenant A must not
    return tenant B's rows even though both tables are shared."""
    t_a = "t-y4-list-iso-a"
    t_b = "t-y4-list-iso-b"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        a_pid = await _create_project(
            client, t_a, product_line="embedded", slug="a",
        )
        b_pid = await _create_project(
            client, t_b, product_line="embedded", slug="b",
        )
        res = await client.get(f"/api/v1/tenants/{t_a}/projects")
        assert res.status_code == 200, res.text
        ids = {p["project_id"] for p in res.json()["projects"]}
        assert a_pid in ids
        assert b_pid not in ids
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite fingerprints
    (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    ``VALUES ... ?, ?`` placeholder). asyncpg pool conns don't have
    ``.commit()`` and PG uses ``$1, $2`` parameters."""
    src = Path(
        "backend/routers/tenant_projects.py"
    ).read_text(encoding="utf-8")
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat-era fingerprint(s) hit: {hits}"

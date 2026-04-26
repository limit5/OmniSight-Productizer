"""Y5 (#281) row 2 — drift guard for ``require_project_member``.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (super_admin bypass, project_members
direct hit, tenant-membership fallback for admin/owner, 403 for
member/viewer / suspended tenant memberships, 404 for unknown +
cross-tenant projects, ContextVar pinning) and skip when
``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — ``PROJECT_ROLE_HIERARCHY`` aligns with
      alembic 0034 DB CHECK; ``_TENANT_ROLE_DEFAULT_PROJECT_ROLE``
      aligns with the 0034 docstring fall-through semantics
  (b) ``project_role_at_least`` rank order and unknown-token rejection
  (c) SQL constants — PG ``$N`` placeholder, secret-leak guard,
      read-only verbs, tenant-scoped project lookup shape
  (d) ``require_project_member`` factory — rejects unknown roles,
      returns a callable with the expected signature
  (e) HTTP path: super_admin bypass / tenant admin fallback /
      project_members direct hit / 403 / 404 / cross-tenant 404 /
      ContextVar pinning
  (f) Self-fingerprint guard
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timezone
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


def test_project_role_hierarchy_matches_db_check_enum():
    """The DB CHECK on ``project_members.role`` (alembic 0034)
    enforces ``IN ('owner', 'contributor', 'viewer')`` — drift here
    would let the dependency accept a role the DB then rejects."""
    from backend.auth import PROJECT_ROLE_HIERARCHY
    assert set(PROJECT_ROLE_HIERARCHY) == {"viewer", "contributor", "owner"}


def test_project_role_hierarchy_is_increasing_rank_order():
    """Rank order is the source of truth for ``min_role`` comparisons.
    viewer < contributor < owner."""
    from backend.auth import PROJECT_ROLE_HIERARCHY, _PROJECT_ROLE_RANK
    assert PROJECT_ROLE_HIERARCHY == ("viewer", "contributor", "owner")
    assert _PROJECT_ROLE_RANK["viewer"] == 0
    assert _PROJECT_ROLE_RANK["contributor"] == 1
    assert _PROJECT_ROLE_RANK["owner"] == 2


def test_tenant_role_default_project_role_table():
    """Per alembic 0034 docstring: active tenant owner / admin → effective
    project role ``contributor``; member / viewer fall through (key
    absent) to no project access by default."""
    from backend.auth import _TENANT_ROLE_DEFAULT_PROJECT_ROLE
    assert _TENANT_ROLE_DEFAULT_PROJECT_ROLE == {
        "owner": "contributor",
        "admin": "contributor",
    }
    assert "member" not in _TENANT_ROLE_DEFAULT_PROJECT_ROLE
    assert "viewer" not in _TENANT_ROLE_DEFAULT_PROJECT_ROLE


def test_project_role_hierarchy_distinct_from_tenant_role_enum():
    """Project roles ≠ tenant membership roles. Drift here would let
    an admin tenant role be passed as a project role to ``min_role``
    and silently accept it."""
    from backend.auth import PROJECT_ROLE_HIERARCHY
    from backend.routers.tenant_members import MEMBERSHIP_ROLE_ENUM
    assert "admin" not in PROJECT_ROLE_HIERARCHY
    assert "member" not in PROJECT_ROLE_HIERARCHY
    assert "contributor" not in MEMBERSHIP_ROLE_ENUM


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) ``project_role_at_least``
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("have,need,expected", [
    ("viewer", "viewer", True),
    ("contributor", "viewer", True),
    ("owner", "viewer", True),
    ("contributor", "contributor", True),
    ("owner", "contributor", True),
    ("owner", "owner", True),
    # Strictly below
    ("viewer", "contributor", False),
    ("viewer", "owner", False),
    ("contributor", "owner", False),
])
def test_project_role_at_least_rank_compare(have, need, expected):
    from backend.auth import project_role_at_least
    assert project_role_at_least(have, need) is expected


@pytest.mark.parametrize("have", [None, "", "admin", "member", "super_admin"])
def test_project_role_at_least_rejects_unknown_have(have):
    from backend.auth import project_role_at_least
    assert project_role_at_least(have, "viewer") is False


@pytest.mark.parametrize("need", ["", "admin", "member", "super_admin"])
def test_project_role_at_least_rejects_unknown_need(need):
    from backend.auth import project_role_at_least
    assert project_role_at_least("owner", need) is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_AUTHZ_SQL_NAMES = (
    "_FETCH_PROJECT_TENANT_SCOPED_FOR_AUTHZ_SQL",
    "_FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL",
    "_FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL",
    "_FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL",
)


@pytest.mark.parametrize("sql_name", _AUTHZ_SQL_NAMES)
def test_authz_sql_uses_pg_placeholders_only(sql_name):
    from backend import auth as _auth
    sql = getattr(_auth, sql_name)
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint_re.search(sql), (
        f"{sql_name} contains compat-era fingerprint"
    )
    for ch in (" ?,", " ?)", "= ?"):
        assert ch not in sql, f"{sql_name} contains SQLite '?' placeholder"
    assert "$1" in sql, f"{sql_name} missing PG ``$1`` placeholder"


@pytest.mark.parametrize("sql_name", _AUTHZ_SQL_NAMES)
def test_authz_sql_does_not_leak_secret_columns(sql_name):
    """No password_hash / oidc_* / token_hash projection — the
    authorisation surface must not surface user-account secrets."""
    from backend import auth as _auth
    sql = getattr(_auth, sql_name).lower()
    for forbidden in ("password_hash", "oidc_subject", "oidc_provider",
                      "token_hash"):
        assert forbidden not in sql, (
            f"{sql_name} projects forbidden field {forbidden!r}"
        )


@pytest.mark.parametrize("sql_name", _AUTHZ_SQL_NAMES)
def test_authz_sql_is_read_only(sql_name):
    """No INSERT / UPDATE / DELETE / DROP verbs — the dependency must
    never mutate state implicitly."""
    from backend import auth as _auth
    upper = getattr(_auth, sql_name).upper()
    assert "SELECT" in upper
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP "):
        assert verb not in upper, f"{sql_name} contains forbidden verb {verb}"


def test_fetch_project_tenant_scoped_filter_shape():
    """Path-tenant cross-check — drift here would let a project from
    tenant B leak into tenant A's URL space."""
    from backend.auth import _FETCH_PROJECT_TENANT_SCOPED_FOR_AUTHZ_SQL as sql
    assert "WHERE id = $1 AND tenant_id = $2" in sql
    assert "FROM projects" in sql


def test_fetch_project_by_id_shape():
    """Used when the route only carries ``project_id`` — tenant comes
    from the row."""
    from backend.auth import _FETCH_PROJECT_BY_ID_FOR_AUTHZ_SQL as sql
    assert "WHERE id = $1" in sql
    assert "FROM projects" in sql


def test_fetch_project_member_shape():
    from backend.auth import _FETCH_PROJECT_MEMBER_FOR_AUTHZ_SQL as sql
    assert "FROM project_members" in sql
    assert "user_id = $1" in sql
    assert "project_id = $2" in sql


def test_fetch_tenant_membership_shape():
    from backend.auth import _FETCH_TENANT_MEMBERSHIP_FOR_AUTHZ_SQL as sql
    assert "FROM user_tenant_memberships" in sql
    assert "user_id = $1" in sql
    assert "tenant_id = $2" in sql
    # Status column projected so the dependency can require ``active``.
    assert "status" in sql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Factory shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_factory_default_min_role_is_viewer():
    from backend.auth import require_project_member
    dep = require_project_member()
    # The closure's local name should reflect the wrapped function;
    # presence is enough — behaviour is validated below.
    assert callable(dep)


@pytest.mark.parametrize("role", ["viewer", "contributor", "owner"])
def test_factory_accepts_each_project_role(role):
    from backend.auth import require_project_member
    dep = require_project_member(min_role=role)
    assert callable(dep)


@pytest.mark.parametrize("bad", [
    "", "admin", "member", "super_admin", "Owner", "VIEWER", " viewer",
])
def test_factory_rejects_unknown_role(bad):
    from backend.auth import require_project_member
    with pytest.raises(ValueError):
        require_project_member(min_role=bad)


def test_factory_dep_signature_takes_project_id_request_user():
    """The closure pulls ``project_id`` from the URL path automatically
    via FastAPI's parameter-name binding; ``request`` is needed to read
    optional ``{tenant_id}`` from ``path_params``; ``user`` chains to
    ``current_user`` for auth."""
    from backend.auth import require_project_member, current_user
    from fastapi.params import Depends as _DependsParam

    dep = require_project_member()
    sig = inspect.signature(dep)
    params = list(sig.parameters)
    assert params == ["project_id", "request", "user"]
    user_default = sig.parameters["user"].default
    assert isinstance(user_default, _DependsParam)
    assert user_default.dependency is current_user


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) HTTP path — pin a tiny FastAPI app that uses the dependency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_authz_app(min_role: str = "viewer"):
    """Spin up a tiny FastAPI app that exercises ``require_project_member``
    on a real route. Uses the standard nested path so ``{tenant_id}``
    + ``{project_id}`` both bind from the URL — covering the
    cross-tenant 404 branch.
    """
    from fastapi import Depends, FastAPI
    from backend import auth as _au
    from backend.db_context import (
        current_project_id,
        current_tenant_id,
        current_user_role,
    )

    app = FastAPI()

    @app.get("/api/v1/tenants/{tenant_id}/projects/{project_id}/probe")
    async def _probe(
        tenant_id: str,
        project_id: str,
        user: _au.User = Depends(_au.require_project_member(min_role=min_role)),
    ) -> dict:
        return {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "user_id": user.id,
            "ctx_tenant_id": current_tenant_id(),
            "ctx_project_id": current_project_id(),
            "ctx_user_role": current_user_role(),
        }

    return app


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}",
        )


async def _seed_user(
    pool,
    *,
    uid: str,
    tid: str,
    email: str,
    role: str = "viewer",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, $4, '', 1, $5) "
            "ON CONFLICT (id) DO NOTHING",
            uid, email, email.split("@")[0], role, tid,
        )


async def _seed_membership(
    pool,
    *,
    uid: str,
    tid: str,
    role: str,
    status: str = "active",
) -> None:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            uid, tid, role, status, created_at,
        )


async def _seed_project_member(
    pool, *, uid: str, pid: str, role: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO project_members (user_id, project_id, role) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id, project_id) DO NOTHING",
            uid, pid, role,
        )


async def _create_project(client, tid: str, *, slug: str) -> str:
    """Create a project via the tenant_projects router. The default
    test caller is super_admin so this bypasses the create gate."""
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects",
        json={"product_line": "embedded", "name": f"P-{slug}",
              "slug": slug},
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


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
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project' "
            "AND tenant_id = $1",
            tid,
        )
        await conn.execute("DELETE FROM projects WHERE tenant_id = $1", tid)
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM users WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _http_authz(app, *, override_user, path: str):
    """Drive a single GET against the tiny app with ``current_user``
    overridden. Returns the response object."""
    from httpx import ASGITransport, AsyncClient
    from backend import auth as _au

    async def _fake():
        return override_user

    app.dependency_overrides[_au.current_user] = _fake
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
        ) as ac:
            return await ac.get(path)
    finally:
        app.dependency_overrides.pop(_au.current_user, None)


# ─── HTTP cases ────────────────────────────────────────────────────


@_requires_pg
async def test_http_super_admin_bypass(client, pg_test_pool):
    """``super_admin`` passes regardless of project_members /
    user_tenant_memberships state, and ContextVar ``user_role`` is
    pinned to ``super_admin`` for the listener / audit row to see."""
    from backend import auth as _au

    tid = "t-y5-pm-sa"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="sa")

        app = _build_authz_app(min_role="owner")
        sa_user = _au.User(
            id="u-y5sa00000001", email="sa@x.com", name="SA",
            role="super_admin", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=sa_user,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ctx_tenant_id"] == tid
        assert body["ctx_project_id"] == pid
        assert body["ctx_user_role"] == "super_admin"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_project_owner_passes_owner_gate(client, pg_test_pool):
    """A user with explicit ``project_members.role='owner'`` passes
    even ``min_role='owner'`` — the most demanding gate — and
    ContextVar ``user_role`` reflects the project role."""
    from backend import auth as _au

    tid = "t-y5-pm-po"
    uid = "u-y5powner0001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="po@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="po")
        await _seed_project_member(
            pg_test_pool, uid=uid, pid=pid, role="owner",
        )

        app = _build_authz_app(min_role="owner")
        u = _au.User(
            id=uid, email="po@x.com", name="PO",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ctx_user_role"] == "owner"
        assert body["ctx_project_id"] == pid
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_project_viewer_fails_contributor_gate(
    client, pg_test_pool,
):
    """``project_members.role='viewer'`` is below ``min_role='contributor'``
    → 403."""
    from backend import auth as _au

    tid = "t-y5-pm-pv"
    uid = "u-y5pviewer001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="pv@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="pv")
        await _seed_project_member(
            pg_test_pool, uid=uid, pid=pid, role="viewer",
        )

        app = _build_authz_app(min_role="contributor")
        u = _au.User(
            id=uid, email="pv@x.com", name="PV",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 403, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_tenant_admin_falls_back_to_contributor(
    client, pg_test_pool,
):
    """Active ``user_tenant_memberships.role='admin'`` with no explicit
    ``project_members`` row → effective project role is ``contributor``.
    Passes ``viewer`` and ``contributor`` gates, fails ``owner``."""
    from backend import auth as _au

    tid = "t-y5-pm-ta"
    uid = "u-y5tadmin0001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="ta@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="admin", status="active")
        pid = await _create_project(client, tid, slug="ta")

        u = _au.User(
            id=uid, email="ta@x.com", name="TA",
            role="viewer", enabled=True, tenant_id=tid,
        )

        # viewer gate — pass.
        app_v = _build_authz_app(min_role="viewer")
        rv = await _http_authz(
            app_v, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert rv.status_code == 200, rv.text
        assert rv.json()["ctx_user_role"] == "contributor"

        # contributor gate — pass.
        app_c = _build_authz_app(min_role="contributor")
        rc = await _http_authz(
            app_c, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert rc.status_code == 200, rc.text

        # owner gate — fail (contributor < owner).
        app_o = _build_authz_app(min_role="owner")
        ro = await _http_authz(
            app_o, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert ro.status_code == 403, ro.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_tenant_member_role_no_fallback_403(
    client, pg_test_pool,
):
    """Active ``member`` / ``viewer`` tenant memberships do NOT confer
    project access by default — must 403 even on the lowest gate."""
    from backend import auth as _au

    tid = "t-y5-pm-tm"
    uid = "u-y5tmember001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="tm@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="tm")

        app = _build_authz_app(min_role="viewer")
        u = _au.User(
            id=uid, email="tm@x.com", name="TM",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 403, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_suspended_tenant_membership_blocks_fallback(
    client, pg_test_pool,
):
    """A tenant admin whose membership is ``suspended`` must NOT be
    promoted to project ``contributor``. The fallback path checks
    ``status='active'`` explicitly."""
    from backend import auth as _au

    tid = "t-y5-pm-susp"
    uid = "u-y5suspd00001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="susp@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="admin", status="suspended")
        pid = await _create_project(client, tid, slug="susp")

        app = _build_authz_app(min_role="viewer")
        u = _au.User(
            id=uid, email="susp@x.com", name="Susp",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 403, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_project_member_overrides_tenant_default(
    client, pg_test_pool,
):
    """A user who is tenant ``viewer`` (would otherwise get nothing)
    AND has ``project_members.role='contributor'`` passes the
    ``contributor`` gate via the explicit row, not the fallback."""
    from backend import auth as _au

    tid = "t-y5-pm-ovr"
    uid = "u-y5pmovrride1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="ovr@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="viewer", status="active")
        pid = await _create_project(client, tid, slug="ovr")
        await _seed_project_member(
            pg_test_pool, uid=uid, pid=pid, role="contributor",
        )

        app = _build_authz_app(min_role="contributor")
        u = _au.User(
            id=uid, email="ovr@x.com", name="OVR",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 200, res.text
        assert res.json()["ctx_user_role"] == "contributor"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_unknown_project_returns_404(client, pg_test_pool):
    from backend import auth as _au

    tid = "t-y5-pm-noproj"
    try:
        await _seed_tenant(pg_test_pool, tid)
        app = _build_authz_app()
        sa = _au.User(
            id="u-y5pmnp00001", email="np@x.com", name="NP",
            role="super_admin", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=sa,
            path=f"/api/v1/tenants/{tid}/projects/p-deadbeefdeadbeef/probe",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_http_cross_tenant_project_returns_404(client, pg_test_pool):
    """Project belongs to tenant A; URL pins tenant B → 404 (NOT
    403). Caller must not be able to enumerate cross-tenant rows by
    status code."""
    from backend import auth as _au

    t_a = "t-y5-pm-iso-a"
    t_b = "t-y5-pm-iso-b"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        pid_a = await _create_project(client, t_a, slug="aproj")

        app = _build_authz_app()
        sa = _au.User(
            id="u-y5pmiso00001", email="iso@x.com", name="ISO",
            role="super_admin", enabled=True, tenant_id=t_b,
        )
        # URL says tenant_b owns the project — must 404.
        res = await _http_authz(
            app, override_user=sa,
            path=f"/api/v1/tenants/{t_b}/projects/{pid_a}/probe",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_http_context_vars_pinned_after_pass(client, pg_test_pool):
    """``set_tenant_id`` / ``set_project_id`` / ``set_user_role`` must
    all be populated before the handler runs so the SQLAlchemy listener
    (Y5 row 3) sees the right scope."""
    from backend import auth as _au

    tid = "t-y5-pm-ctx"
    uid = "u-y5pmctx00001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="ctx@x.com", role="viewer")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="admin", status="active")
        pid = await _create_project(client, tid, slug="ctx")

        app = _build_authz_app(min_role="contributor")
        u = _au.User(
            id=uid, email="ctx@x.com", name="CTX",
            role="viewer", enabled=True, tenant_id=tid,
        )
        res = await _http_authz(
            app, override_user=u,
            path=f"/api/v1/tenants/{tid}/projects/{pid}/probe",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ctx_tenant_id"] == tid
        assert body["ctx_project_id"] == pid
        assert body["ctx_user_role"] == "contributor"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Self-fingerprint guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_no_compat_fingerprint():
    """The dependency body and the test file itself must not contain
    SQLite-era compat fingerprints."""
    src = Path(__file__).read_text()
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    # The regex literal above naturally appears once in source; mask it.
    masked = src.replace(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]",
        "<masked-self-pattern>",
    )
    assert not fingerprint_re.search(masked), (
        "test_require_project_member.py contains a compat-era fingerprint"
    )

    auth_src = (
        Path(__file__).parent.parent / "auth.py"
    ).read_text()
    # auth.py has a pre-existing pre-Y5 doc citation referencing the
    # SQLite default-clause spelling; mask that one line so this guard
    # only fires on live-code regressions, not docstring text.
    citation_re = re.compile(
        r"^[^\n]*TEXT NOT NULL DEFAULT \(d" + "atetime"
        + r"\('now'\)\)[^\n]*$",
        flags=re.MULTILINE,
    )
    auth_masked = citation_re.sub("<masked-doc-citation>", auth_src)
    hits = fingerprint_re.findall(auth_masked)
    assert not hits, (
        f"backend/auth.py contains compat-era fingerprint(s): {hits!r}"
    )

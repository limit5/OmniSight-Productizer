"""Y4 (#280) row 5 — drift guard for the project membership surface.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise end-to-end behaviour (POST grant, PATCH update, DELETE soft-
remove via row delete, idempotent branches, RBAC across tenant
admin / super_admin / project owner, tenant-membership precondition,
and audit emission) and skip when ``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — role enum aligns with alembic 0034
      DB CHECK; USER_ID_PATTERN aligns with admin_super_admins;
      tenant + project mgmt allowlists are admin-tier-only
  (b) Pydantic bodies — ``CreateProjectMemberRequest`` /
      ``PatchProjectMemberRequest`` enforce the role enum and
      user_id pattern
  (c) SQL constants — PG ``$N`` placeholder, secret-leak guard,
      INSERT atomic with ``ON CONFLICT DO NOTHING RETURNING``,
      tenant-scoped project fetch, target-user-tenant-membership
      precondition fetch
  (d) Router endpoints exposed with ``auth.current_user`` dependency
  (e) Main app full-prefix mount confirms all three paths/methods
  (f) HTTP path: POST happy / PATCH happy / DELETE happy /
      idempotent branches / 404 / 422 / 409 dup
  (g) RBAC: super_admin / tenant admin / project owner pass;
      contributor / viewer / non-tenant-member fail
  (h) Audit: tenant_project_member_added / _updated / _removed rows
  (i) Self-fingerprint guard
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


def test_project_member_role_enum_matches_db_check():
    """The DB CHECK on ``project_members.role`` from alembic 0034
    enforces this exact enum — drift here would let a regressed
    POST/PATCH set a role the DB then rejects."""
    from backend.routers.tenant_projects import PROJECT_MEMBER_ROLE_ENUM
    assert PROJECT_MEMBER_ROLE_ENUM == ("owner", "contributor", "viewer")


def test_project_member_role_enum_distinct_from_tenant_role_enum():
    """Project roles ≠ tenant roles by design — a project owner is
    NOT a tenant owner; the DB CHECKs are deliberately different."""
    from backend.routers.tenant_projects import PROJECT_MEMBER_ROLE_ENUM
    from backend.routers.tenant_members import MEMBERSHIP_ROLE_ENUM
    assert "admin" not in PROJECT_MEMBER_ROLE_ENUM
    assert "member" not in PROJECT_MEMBER_ROLE_ENUM
    assert "contributor" in PROJECT_MEMBER_ROLE_ENUM
    assert "contributor" not in MEMBERSHIP_ROLE_ENUM


def test_user_id_pattern_constant():
    """User id shape mirrors Y3 row 5 admin_super_admins (drift would
    silently break invite/membership cross-references)."""
    from backend.routers.tenant_projects import USER_ID_PATTERN
    from backend.routers.admin_super_admins import (
        USER_ID_PATTERN as _ADMIN_USER_ID_PATTERN,
    )
    assert USER_ID_PATTERN == _ADMIN_USER_ID_PATTERN
    assert USER_ID_PATTERN == r"^u-[a-z0-9]{4,64}$"


@pytest.mark.parametrize("good_uid", [
    "u-abcd",
    "u-0123456789",
    "u-deadbeef",
    "u-" + "a" * 64,
])
def test_user_id_validator_accepts(good_uid):
    from backend.routers.tenant_projects import _is_valid_user_id
    assert _is_valid_user_id(good_uid)


@pytest.mark.parametrize("bad_uid", [
    "", "abcd", "U-abcd", "u-ABCD", "u-", "u-abc",
    "u-" + "a" * 65, "u-abc_def", " u-abcd", "u-abcd ",
])
def test_user_id_validator_rejects(bad_uid):
    from backend.routers.tenant_projects import _is_valid_user_id
    assert not _is_valid_user_id(bad_uid)


def test_member_mgmt_tenant_roles_admin_tier_only():
    from backend.routers.tenant_projects import (
        _PROJECT_MEMBER_MGMT_TENANT_ROLES,
    )
    assert _PROJECT_MEMBER_MGMT_TENANT_ROLES == frozenset({"owner", "admin"})
    assert "member" not in _PROJECT_MEMBER_MGMT_TENANT_ROLES
    assert "viewer" not in _PROJECT_MEMBER_MGMT_TENANT_ROLES


def test_member_mgmt_project_roles_owner_only():
    """Only project owner may manage membership; contributor + viewer
    deliberately do not — without this asymmetry the project ``owner``
    role would have no distinguishing capability over ``contributor``."""
    from backend.routers.tenant_projects import (
        _PROJECT_MEMBER_MGMT_PROJECT_ROLES,
    )
    assert _PROJECT_MEMBER_MGMT_PROJECT_ROLES == frozenset({"owner"})
    assert "contributor" not in _PROJECT_MEMBER_MGMT_PROJECT_ROLES
    assert "viewer" not in _PROJECT_MEMBER_MGMT_PROJECT_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pydantic bodies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_request_happy():
    from backend.routers.tenant_projects import CreateProjectMemberRequest
    body = CreateProjectMemberRequest(
        user_id="u-deadbeef", role="contributor",
    )
    assert body.user_id == "u-deadbeef"
    assert body.role == "contributor"


@pytest.mark.parametrize("good_role", ["owner", "contributor", "viewer"])
def test_create_request_accepts_each_role(good_role):
    from backend.routers.tenant_projects import CreateProjectMemberRequest
    body = CreateProjectMemberRequest(
        user_id="u-abcd1234", role=good_role,
    )
    assert body.role == good_role


@pytest.mark.parametrize("bad_role", [
    "admin", "Admin", "member", "OWNER", "guest", "",
])
def test_create_request_rejects_bad_role(bad_role):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectMemberRequest
    with pytest.raises(ValidationError):
        CreateProjectMemberRequest(user_id="u-abcd1234", role=bad_role)


@pytest.mark.parametrize("bad_uid", [
    "abcd", "U-abcd", "u-ABCD", "u-", " u-abcd", "u-abcd ",
    "u-" + "a" * 65,
])
def test_create_request_rejects_bad_user_id(bad_uid):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectMemberRequest
    with pytest.raises(ValidationError):
        CreateProjectMemberRequest(user_id=bad_uid, role="viewer")


def test_create_request_requires_both_fields():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectMemberRequest
    with pytest.raises(ValidationError):
        CreateProjectMemberRequest(user_id="u-abcd1234")
    with pytest.raises(ValidationError):
        CreateProjectMemberRequest(role="owner")


def test_patch_request_happy():
    from backend.routers.tenant_projects import PatchProjectMemberRequest
    body = PatchProjectMemberRequest(role="owner")
    assert body.role == "owner"


@pytest.mark.parametrize("good_role", ["owner", "contributor", "viewer"])
def test_patch_request_accepts_each_role(good_role):
    from backend.routers.tenant_projects import PatchProjectMemberRequest
    body = PatchProjectMemberRequest(role=good_role)
    assert body.role == good_role


@pytest.mark.parametrize("bad_role", [
    "admin", "member", "Owner", "VIEWER", "", "system",
])
def test_patch_request_rejects_bad_role(bad_role):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectMemberRequest
    with pytest.raises(ValidationError):
        PatchProjectMemberRequest(role=bad_role)


def test_patch_request_requires_role():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import PatchProjectMemberRequest
    with pytest.raises(ValidationError):
        PatchProjectMemberRequest()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_MEMBER_SQL_NAMES = (
    "_FETCH_PROJECT_TENANT_SCOPED_SQL",
    "_FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL",
    "_FETCH_PROJECT_MEMBER_SQL",
    "_INSERT_PROJECT_MEMBER_SQL",
    "_UPDATE_PROJECT_MEMBER_ROLE_SQL",
    "_DELETE_PROJECT_MEMBER_SQL",
)


@pytest.mark.parametrize("sql_name", _MEMBER_SQL_NAMES)
def test_member_sql_uses_pg_placeholders_only(sql_name):
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name)
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint_re.search(sql), (
        f"{sql_name} contains compat-era fingerprint"
    )
    for ch in (" ?,", " ?)", "= ?"):
        assert ch not in sql, f"{sql_name} contains SQLite '?' placeholder"
    assert "$1" in sql, f"{sql_name} missing PG ``$1`` placeholder"


@pytest.mark.parametrize("sql_name", _MEMBER_SQL_NAMES)
def test_member_sql_does_not_leak_secret_columns(sql_name):
    """No password_hash, no oidc_*, no token_hash projection — the
    member surface must not surface user account secrets even by
    accident through audit blob projection."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name).lower()
    for forbidden in ("password_hash", "oidc_subject", "oidc_provider",
                      "token_hash"):
        assert forbidden not in sql, (
            f"{sql_name} projects forbidden field {forbidden!r}"
        )


def test_fetch_project_tenant_scoped_sql_filters_by_tenant():
    """A stolen project_id pointed at the wrong tenant must resolve
    to row=None, NOT to a row from the rightful tenant — drift here
    would let a non-admin enumerate / mutate projects in tenants they
    have admin on by guessing project ids from another tenant."""
    from backend.routers.tenant_projects import (
        _FETCH_PROJECT_TENANT_SCOPED_SQL,
    )
    assert "WHERE id = $1 AND tenant_id = $2" in _FETCH_PROJECT_TENANT_SCOPED_SQL
    upper = _FETCH_PROJECT_TENANT_SCOPED_SQL.upper()
    assert "SELECT" in upper
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP "):
        assert verb not in upper


def test_fetch_target_user_tenant_membership_sql_shape():
    """Used to enforce the precondition: target user must have an
    active tenant membership before granting a project role."""
    from backend.routers.tenant_projects import (
        _FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL,
    )
    sql = _FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL
    assert "user_tenant_memberships" in sql
    assert "WHERE user_id = $1" in sql
    assert "tenant_id = $2" in sql
    # Read-only — no implicit upserts.
    upper = sql.upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP "):
        assert verb not in upper


def test_insert_project_member_sql_atomic_with_on_conflict():
    """ON CONFLICT DO NOTHING + RETURNING resolves "insert-or-detect-
    duplicate" in one round-trip; concurrent admins racing same target
    resolve to one winner (row populated) and one loser (None → 409)."""
    from backend.routers.tenant_projects import _INSERT_PROJECT_MEMBER_SQL
    sql = _INSERT_PROJECT_MEMBER_SQL
    assert "INSERT INTO project_members" in sql
    assert "(user_id, project_id, role)" in sql
    assert "ON CONFLICT (user_id, project_id) DO NOTHING" in sql
    assert "RETURNING" in sql.upper()


def test_update_project_member_role_sql_atomic_with_returning():
    from backend.routers.tenant_projects import (
        _UPDATE_PROJECT_MEMBER_ROLE_SQL,
    )
    upper = _UPDATE_PROJECT_MEMBER_ROLE_SQL.upper()
    assert "UPDATE PROJECT_MEMBERS" in upper
    assert "SET ROLE = $3" in upper
    assert "WHERE USER_ID = $1" in upper
    assert "PROJECT_ID = $2" in upper
    assert "RETURNING" in upper


def test_delete_project_member_sql_returning_for_audit():
    """RETURNING on DELETE so the audit blob can capture the prior
    role in one round-trip; RETURNING None signals already-absent."""
    from backend.routers.tenant_projects import _DELETE_PROJECT_MEMBER_SQL
    upper = _DELETE_PROJECT_MEMBER_SQL.upper()
    assert "DELETE FROM PROJECT_MEMBERS" in upper
    assert "WHERE USER_ID = $1" in upper
    assert "PROJECT_ID = $2" in upper
    assert "RETURNING" in upper


def test_fetch_project_member_sql_read_only():
    from backend.routers.tenant_projects import _FETCH_PROJECT_MEMBER_SQL
    upper = _FETCH_PROJECT_MEMBER_SQL.upper()
    assert "SELECT" in upper
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP "):
        assert verb not in upper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Router endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_member_post_patch_delete():
    from backend.routers.tenant_projects import router
    paths_methods: set[tuple[str, str]] = set()
    for r in router.routes:
        for mm in getattr(r, "methods", set()):
            paths_methods.add((r.path, mm))
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/members", "POST",
    ) in paths_methods
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
        "PATCH",
    ) in paths_methods
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
        "DELETE",
    ) in paths_methods


@pytest.mark.parametrize("handler_name", [
    "create_project_member",
    "patch_project_member",
    "delete_project_member",
])
def test_member_handler_uses_current_user_dependency(handler_name):
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
#  (e) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_member_endpoints():
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    full: set[tuple[str, str]] = set()
    for r in app.routes:
        for mm in getattr(r, "methods", set()) or set():
            full.add((r.path, mm))
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/members", "POST",
    ) in full
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
        "PATCH",
    ) in full
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
        "DELETE",
    ) in full


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP fixtures (shared)
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
    pool,
    *,
    uid: str,
    tid: str,
    email: str,
    enabled: int = 1,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, "
            "enabled, tenant_id) "
            "VALUES ($1, $2, $3, 'viewer', '', $4, $5) "
            "ON CONFLICT (id) DO NOTHING",
            uid, email, email.split("@")[0], enabled, tid,
        )


async def _seed_membership(
    pool,
    *,
    uid: str,
    tid: str,
    role: str = "member",
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


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project_member' "
            "AND tenant_id = $1",
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
        await conn.execute(
            "DELETE FROM users WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _create_project(client, tid: str, *, slug: str) -> str:
    res = await client.post(
        f"/api/v1/tenants/{tid}/projects",
        json={"product_line": "embedded", "name": f"P-{slug}",
              "slug": slug},
    )
    assert res.status_code == 201, res.text
    return res.json()["project_id"]


async def _read_project_member(pool, *, uid: str, pid: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT user_id, project_id, role FROM project_members "
            "WHERE user_id = $1 AND project_id = $2",
            uid, pid,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) HTTP — POST /tenants/{tid}/projects/{pid}/members
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_member_happy_inserts_row(client, pg_test_pool):
    tid = "t-y4-pm-happy"
    uid = "u-y4pmhappy0001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="happy@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="happy")

        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "contributor"},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["user_id"] == uid
        assert body["project_id"] == pid
        assert body["role"] == "contributor"
        assert body["tenant_id"] == tid
        assert "created_at" in body
        # No PII / secret leaks.
        for k in ("password_hash", "oidc_subject", "oidc_provider",
                  "token_hash"):
            assert k not in body

        # And persisted.
        row = await _read_project_member(pg_test_pool, uid=uid, pid=pid)
        assert row is not None
        assert row["role"] == "contributor"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
@pytest.mark.parametrize("role", ["owner", "contributor", "viewer"])
async def test_post_member_accepts_each_role(client, pg_test_pool, role):
    tid = f"t-y4-pm-r-{role}"
    uid = f"u-y4pmrole{role[:5]}1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email=f"{role}@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="r")

        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": role},
        )
        assert res.status_code == 201, res.text
        assert res.json()["role"] == role
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_duplicate_returns_409_with_existing_role(
    client, pg_test_pool,
):
    tid = "t-y4-pm-dup"
    uid = "u-y4pmdup00001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="d@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="dup")

        first = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "viewer"},
        )
        assert first.status_code == 201

        # Second POST same target — 409 with existing_role.
        second = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "owner"},
        )
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["existing_role"] == "viewer"
        assert body["user_id"] == uid
        assert body["project_id"] == pid

        # Existing row untouched (role still viewer).
        row = await _read_project_member(pg_test_pool, uid=uid, pid=pid)
        assert row["role"] == "viewer"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_unknown_tenant_returns_404(client):
    res = await client.post(
        "/api/v1/tenants/t-y4-pm-noten/projects/"
        "p-deadbeefdeadbeef/members",
        json={"user_id": "u-deadbeef", "role": "viewer"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_member_unknown_project_returns_404(
    client, pg_test_pool,
):
    tid = "t-y4-pm-noproj"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/p-deadbeefdeadbeef/members",
            json={"user_id": "u-deadbeef", "role": "viewer"},
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_cross_tenant_project_returns_404(
    client, pg_test_pool,
):
    """Project owned by tenant A is invisible from tenant B's
    namespace — 404, not 403."""
    t_a = "t-y4-pm-iso-a"
    t_b = "t-y4-pm-iso-b"
    uid = "u-y4pmiso0001"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        await _seed_user(pg_test_pool, uid=uid, tid=t_b, email="iso@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=t_b,
                               role="member", status="active")
        pid_a = await _create_project(client, t_a, slug="aproj")

        # Caller (super_admin in default test mode) tries to add a
        # member to project_a via tenant_b's URL — must 404.
        res = await client.post(
            f"/api/v1/tenants/{t_b}/projects/{pid_a}/members",
            json={"user_id": uid, "role": "viewer"},
        )
        assert res.status_code == 404, res.text

        # Project_a's roster untouched.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM project_members WHERE project_id = $1",
                pid_a,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)


@_requires_pg
async def test_post_member_user_without_tenant_membership_returns_422(
    client, pg_test_pool,
):
    """User exists but has no active tenant membership on this tenant
    → 422 with explanatory detail (not 404; the body is the issue)."""
    tid = "t-y4-pm-nomem"
    other_tid = "t-y4-pm-nomemoth"
    uid = "u-y4pmnomem001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_tenant(pg_test_pool, other_tid)
        # User is a member of OTHER tenant only.
        await _seed_user(pg_test_pool, uid=uid, tid=other_tid,
                         email="otherm@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=other_tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="nm")

        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "viewer"},
        )
        assert res.status_code == 422, res.text
        assert "not a member" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)
        await _purge_tenant(pg_test_pool, other_tid)


@_requires_pg
async def test_post_member_user_with_suspended_tenant_membership_returns_422(
    client, pg_test_pool,
):
    """A suspended tenant membership must NOT be promoted to project
    role — operator must reactivate first."""
    tid = "t-y4-pm-susp"
    uid = "u-y4pmsusp0001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="s@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="suspended")
        pid = await _create_project(client, tid, slug="sp")

        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "viewer"},
        )
        assert res.status_code == 422, res.text
        body = res.json()
        assert body["tenant_membership_status"] == "suspended"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_malformed_tenant_id_returns_422(client):
    res = await client.post(
        "/api/v1/tenants/T-Bad/projects/p-aaaabbbbccccdddd/members",
        json={"user_id": "u-deadbeef", "role": "viewer"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_member_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pm-badpid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/P-BAD/members",
            json={"user_id": "u-deadbeef", "role": "viewer"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_malformed_user_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pm-baduid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="bu")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": "U-BAD", "role": "viewer"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_member_unknown_role_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pm-badrole"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="br")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": "u-deadbeef", "role": "admin"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) HTTP — PATCH /tenants/{tid}/projects/{pid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_member_role_change_succeeds(client, pg_test_pool):
    tid = "t-y4-pmp-role"
    uid = "u-y4pmprole01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="t@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="rc")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "viewer"},
        )

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
            json={"role": "owner"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["role"] == "owner"
        assert body["no_change"] is False

        row = await _read_project_member(pg_test_pool, uid=uid, pid=pid)
        assert row["role"] == "owner"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_member_no_change_returns_no_change_flag(
    client, pg_test_pool,
):
    """Re-PATCH to the same role returns 200 with no_change=True and
    emits no audit row."""
    tid = "t-y4-pmp-nochg"
    uid = "u-y4pmpnochg01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="t@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="nc")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "contributor"},
        )

        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
            json={"role": "contributor"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["no_change"] is True

        # No tenant_project_member_updated audit row emitted.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_project_member_updated' "
                "  AND entity_id = $1",
                f"{pid}:{uid}",
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_member_unknown_membership_returns_404(
    client, pg_test_pool,
):
    """PATCH on a (user, project) pair with no project_members row →
    404 with a hint to use POST."""
    tid = "t-y4-pmp-404"
    uid = "u-y4pmp40400001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="t@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="np")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
            json={"role": "viewer"},
        )
        assert res.status_code == 404, res.text
        assert "POST" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_member_unknown_role_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pmp-badr"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="br")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/u-deadbeef",
            json={"role": "admin"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_member_malformed_user_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pmp-baduid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="bu")
        res = await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/U-BAD",
            json={"role": "viewer"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) HTTP — DELETE /tenants/{tid}/projects/{pid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_member_removes_row(client, pg_test_pool):
    tid = "t-y4-pmd-rm"
    uid = "u-y4pmdrm00001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="t@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="rm")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "owner"},
        )

        res = await client.delete(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["already_removed"] is False
        assert body["role"] == "owner"

        # Row gone — falls back to tenant default.
        row = await _read_project_member(pg_test_pool, uid=uid, pid=pid)
        assert row is None
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_member_idempotent_already_removed(
    client, pg_test_pool,
):
    """Second DELETE on a row that was never granted (or already gone)
    returns 200 with already_removed=True; emits no audit row."""
    tid = "t-y4-pmd-idem"
    uid = "u-y4pmdidem01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="t@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="idem")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
        )
        assert res.status_code == 200, res.text
        assert res.json()["already_removed"] is True

        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_project_member_removed' "
                "  AND entity_id = $1",
                f"{pid}:{uid}",
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_member_unknown_tenant_returns_404(client):
    res = await client.delete(
        "/api/v1/tenants/t-y4-pmd-noten/projects/"
        "p-deadbeefdeadbeef/members/u-deadbeef",
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_delete_member_unknown_project_returns_404(
    client, pg_test_pool,
):
    tid = "t-y4-pmd-noproj"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.delete(
            f"/api/v1/tenants/{tid}/projects/"
            "p-deadbeefdeadbeef/members/u-deadbeef",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_member_malformed_user_id_returns_422(
    client, pg_test_pool,
):
    tid = "t-y4-pmd-baduid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        pid = await _create_project(client, tid, slug="bd")
        res = await client.delete(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/U-BAD",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) RBAC — super_admin / tenant admin / project owner / non-admin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_member_endpoints_non_tenant_member_gets_403(
    client, pg_test_pool,
):
    """A user with membership.role='member' on the target tenant must
    NOT manage project members on any project of that tenant unless
    they are also that project's owner (which they are not in this
    setup)."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-pm-rbac-mem"
    caller_uid = "u-y4pmrbacmem1"
    target_uid = "u-y4pmrbactgt1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=caller_uid, tid=tid,
                         email="caller@x.com")
        await _seed_membership(pg_test_pool, uid=caller_uid, tid=tid,
                               role="member", status="active")
        await _seed_user(pg_test_pool, uid=target_uid, tid=tid,
                         email="target@x.com")
        await _seed_membership(pg_test_pool, uid=target_uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="rbacm")

        caller = _au.User(
            id=caller_uid, email="caller@x.com", name="Caller",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return caller

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            r1 = await client.post(
                f"/api/v1/tenants/{tid}/projects/{pid}/members",
                json={"user_id": target_uid, "role": "viewer"},
            )
            assert r1.status_code == 403, r1.text

            r2 = await client.patch(
                f"/api/v1/tenants/{tid}/projects/{pid}/members/{target_uid}",
                json={"role": "owner"},
            )
            assert r2.status_code == 403, r2.text

            r3 = await client.delete(
                f"/api/v1/tenants/{tid}/projects/{pid}/members/{target_uid}",
            )
            assert r3.status_code == 403, r3.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Roster untouched.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM project_members WHERE project_id = $1",
                pid,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_project_owner_may_manage_members(client, pg_test_pool):
    """A user who is project owner (role='owner' in project_members)
    but only ``member`` at the tenant level CAN manage that project's
    membership — this is the asymmetric capability the project ``owner``
    role was designed to confer."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-pm-projown"
    caller_uid = "u-y4pmpown00001"
    target_uid = "u-y4pmptgt00001"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=caller_uid, tid=tid,
                         email="powner@x.com")
        await _seed_membership(pg_test_pool, uid=caller_uid, tid=tid,
                               role="member", status="active")
        await _seed_user(pg_test_pool, uid=target_uid, tid=tid,
                         email="ptarget@x.com")
        await _seed_membership(pg_test_pool, uid=target_uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="pown")

        # Seed caller as project owner directly (bypass POST gate
        # since super_admin is the default test caller).
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, 'owner')",
                caller_uid, pid,
            )

        caller = _au.User(
            id=caller_uid, email="powner@x.com", name="POwner",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return caller

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{tid}/projects/{pid}/members",
                json={"user_id": target_uid, "role": "contributor"},
            )
            assert res.status_code == 201, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Roster updated.
        row = await _read_project_member(
            pg_test_pool, uid=target_uid, pid=pid,
        )
        assert row is not None
        assert row["role"] == "contributor"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_project_contributor_cannot_manage_members(
    client, pg_test_pool,
):
    """``contributor`` and ``viewer`` project roles do NOT confer
    member-management — only project ``owner`` does."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y4-pm-projctr"
    caller_uid = "u-y4pmpctr00001"
    target_uid = "u-y4pmpctrtgt01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=caller_uid, tid=tid,
                         email="pctr@x.com")
        await _seed_membership(pg_test_pool, uid=caller_uid, tid=tid,
                               role="member", status="active")
        await _seed_user(pg_test_pool, uid=target_uid, tid=tid,
                         email="pctrtgt@x.com")
        await _seed_membership(pg_test_pool, uid=target_uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="pctr")

        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO project_members (user_id, project_id, role) "
                "VALUES ($1, $2, 'contributor')",
                caller_uid, pid,
            )

        caller = _au.User(
            id=caller_uid, email="pctr@x.com", name="PCtr",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return caller

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{tid}/projects/{pid}/members",
                json={"user_id": target_uid, "role": "viewer"},
            )
            assert res.status_code == 403, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) Audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_audit_row_written(client, pg_test_pool):
    tid = "t-y4-pm-audpost"
    uid = "u-y4pmaudpost1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="ap@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="ap")
        res = await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "owner"},
        )
        assert res.status_code == 201, res.text

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, "
                "       before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_member_added' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                f"{pid}:{uid}",
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project_member"
        before_blob = audit_row["before_json"] or ""
        after_blob = audit_row["after_json"] or ""
        # No secret leaks.
        for blob in (before_blob, after_blob):
            for forbidden in ("password_hash", "oidc_subject",
                              "oidc_provider", "token_hash"):
                assert forbidden not in blob
        assert '"role":' in after_blob
        assert '"user_id":' in after_blob
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_audit_row_written(client, pg_test_pool):
    tid = "t-y4-pm-audpatch"
    uid = "u-y4pmaudpat01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="apa@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="apa")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "viewer"},
        )
        await client.patch(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
            json={"role": "owner"},
        )

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_member_updated' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                f"{pid}:{uid}",
            )
        assert audit_row is not None
        before_blob = audit_row["before_json"] or ""
        after_blob = audit_row["after_json"] or ""
        # Role transition recorded both sides.
        assert '"role"' in before_blob
        assert '"viewer"' in before_blob
        assert '"owner"' in after_blob
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_audit_row_written(client, pg_test_pool):
    tid = "t-y4-pm-auddel"
    uid = "u-y4pmauddel01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid, email="ad@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")
        pid = await _create_project(client, tid, slug="ad")
        await client.post(
            f"/api/v1/tenants/{tid}/projects/{pid}/members",
            json={"user_id": uid, "role": "contributor"},
        )
        await client.delete(
            f"/api/v1/tenants/{tid}/projects/{pid}/members/{uid}",
        )

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_member_removed' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                f"{pid}:{uid}",
            )
        assert audit_row is not None
        before_blob = audit_row["before_json"] or ""
        # Prior role recorded for accounting.
        assert '"contributor"' in before_blob
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (i) Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite fingerprints.
    asyncpg pool conns don't have ``.commit()`` and PG uses ``$1, $2``."""
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

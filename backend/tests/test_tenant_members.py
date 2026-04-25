"""Y3 (#279) row 6 — drift guard for GET / PATCH / DELETE
``/api/v1/tenants/{tid}/members[/{user_id}]``.

Pure-unit tests cover the module-level constants, SQL sentinels,
pydantic schema, and router wiring; they run without PG.
HTTP-path tests exercise the end-to-end behaviour (list / role
change / suspend / last-admin floor / RBAC / audit) and skip when
``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) USER_ID_PATTERN + TENANT_ID_PATTERN regex shape
  (b) MEMBERSHIP_ROLE_ENUM / MEMBERSHIP_STATUS_ENUM /
      LISTABLE_MEMBERSHIP_STATUSES / _TENANT_ADMIN_TIER_ROLES tuple/set
      shapes match design
  (c) Pydantic schema (PatchMemberRequest) — at-least-one-field rule
  (d) 9 SQL constants — read-only / FOR UPDATE / RETURNING /
      no-secret-leak / PG ``$N`` placeholder (no SQLite ``?`` regression)
  (e) Router endpoints mounted with ``auth.current_user`` dependency
  (f) Main app full-prefix mount confirms all three paths/methods
  (g) HTTP GET path: happy / status filter / RBAC / 403 / 404 / 422
  (h) HTTP PATCH path: role change / status change / no-change idem /
      last-admin floor 409 / RBAC 403 / 404 / 422
  (i) HTTP DELETE path: suspend happy / already_suspended idem /
      last-admin floor 409 / 404 / 422
  (j) Audit emission: tenant_member_updated row lands; idempotent
      paths emit nothing
  (k) Self-fingerprint guard (pre-commit pattern)
"""

from __future__ import annotations

import inspect
import os
import pathlib
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
#  (a) ID pattern shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_tenant_id_pattern_constant():
    from backend.routers.tenant_members import TENANT_ID_PATTERN
    assert TENANT_ID_PATTERN == r"^t-[a-z0-9][a-z0-9-]{2,62}$"


def test_user_id_pattern_constant():
    from backend.routers.tenant_members import USER_ID_PATTERN
    # Same shape as Y3 row 5 admin_super_admins for consistency.
    from backend.routers.admin_super_admins import (
        USER_ID_PATTERN as _ADMIN_USER_ID_PATTERN,
    )
    assert USER_ID_PATTERN == _ADMIN_USER_ID_PATTERN


@pytest.mark.parametrize("good_uid", [
    "u-abcd",
    "u-0123456789",
    "u-deadbeef",
    "u-" + "a" * 64,
])
def test_user_id_validator_accepts(good_uid):
    from backend.routers.tenant_members import _is_valid_user_id
    assert _is_valid_user_id(good_uid)


@pytest.mark.parametrize("bad_uid", [
    "", "abcd", "U-abcd", "u-ABCD", "u-", "u-abc",
    "u-" + "a" * 65, "u-abc_def", " u-abcd", "u-abcd ",
])
def test_user_id_validator_rejects(bad_uid):
    from backend.routers.tenant_members import _is_valid_user_id
    assert not _is_valid_user_id(bad_uid)


@pytest.mark.parametrize("good_tid", [
    "t-acme", "t-default", "t-y3-test", "t-aaa",
])
def test_tenant_id_validator_accepts(good_tid):
    from backend.routers.tenant_members import _is_valid_tenant_id
    assert _is_valid_tenant_id(good_tid)


@pytest.mark.parametrize("bad_tid", [
    "", "T-Acme", "t-", "t-x", "tenant-acme", "t-Acme",
    "t--leading-dash", "t- spaces ",
])
def test_tenant_id_validator_rejects(bad_tid):
    from backend.routers.tenant_members import _is_valid_tenant_id
    assert not _is_valid_tenant_id(bad_tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Constant shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_membership_role_enum_matches_db_check():
    """Mirrors the CHECK on ``user_tenant_memberships.role`` from
    alembic 0032 — drift here would let a regressed PATCH set a role
    the DB will then reject."""
    from backend.routers.tenant_members import MEMBERSHIP_ROLE_ENUM
    assert MEMBERSHIP_ROLE_ENUM == ("owner", "admin", "member", "viewer")


def test_membership_status_enum_matches_db_check():
    from backend.routers.tenant_members import MEMBERSHIP_STATUS_ENUM
    assert MEMBERSHIP_STATUS_ENUM == ("active", "suspended")


def test_listable_membership_statuses():
    from backend.routers.tenant_members import LISTABLE_MEMBERSHIP_STATUSES
    assert LISTABLE_MEMBERSHIP_STATUSES == ("active", "suspended", "all")


def test_tenant_admin_tier_roles_match_invite_allowlist():
    """Admin-tier (used for the floor check) must be exactly the same
    set as the invite-management allowlist from the invite router —
    both encode the "tenant admin or owner" trust boundary."""
    from backend.routers.tenant_members import _TENANT_ADMIN_TIER_ROLES
    assert _TENANT_ADMIN_TIER_ROLES == frozenset({"owner", "admin"})


def test_default_and_max_limit_relationship():
    from backend.routers.tenant_members import (
        LIST_MEMBERS_DEFAULT_LIMIT, LIST_MEMBERS_MAX_LIMIT,
    )
    assert 1 <= LIST_MEMBERS_DEFAULT_LIMIT <= LIST_MEMBERS_MAX_LIMIT


def test_membership_demote_lock_prefix():
    from backend.routers.tenant_members import _MEMBERSHIP_DEMOTE_LOCK_PREFIX
    # Per-tenant suffix → keys must include the tenant id; the
    # constant is the prefix only.
    assert _MEMBERSHIP_DEMOTE_LOCK_PREFIX.endswith(":")
    assert "membership" in _MEMBERSHIP_DEMOTE_LOCK_PREFIX
    assert "demote" in _MEMBERSHIP_DEMOTE_LOCK_PREFIX


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_patch_request_accepts_role_only():
    from backend.routers.tenant_members import PatchMemberRequest
    body = PatchMemberRequest(role="admin")
    assert body.role == "admin"
    assert body.status is None


def test_patch_request_accepts_status_only():
    from backend.routers.tenant_members import PatchMemberRequest
    body = PatchMemberRequest(status="suspended")
    assert body.role is None
    assert body.status == "suspended"


def test_patch_request_accepts_both():
    from backend.routers.tenant_members import PatchMemberRequest
    body = PatchMemberRequest(role="member", status="active")
    assert body.role == "member"
    assert body.status == "active"


def test_patch_request_rejects_empty_body():
    from pydantic import ValidationError
    from backend.routers.tenant_members import PatchMemberRequest
    with pytest.raises(ValidationError):
        PatchMemberRequest()


@pytest.mark.parametrize("bad_role", ["super_admin", "Admin", "guest", ""])
def test_patch_request_rejects_bad_role(bad_role):
    from pydantic import ValidationError
    from backend.routers.tenant_members import PatchMemberRequest
    with pytest.raises(ValidationError):
        PatchMemberRequest(role=bad_role)


@pytest.mark.parametrize("bad_status", ["Active", "deleted", "removed", ""])
def test_patch_request_rejects_bad_status(bad_status):
    from pydantic import ValidationError
    from backend.routers.tenant_members import PatchMemberRequest
    with pytest.raises(ValidationError):
        PatchMemberRequest(status=bad_status)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) SQL sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SQL_NAMES = (
    "_FETCH_TENANT_SQL",
    "_LIST_MEMBERS_ACTIVE_SQL",
    "_LIST_MEMBERS_BY_STATUS_SQL",
    "_LIST_MEMBERS_ALL_SQL",
    "_FETCH_MEMBERSHIP_SQL",
    "_FETCH_MEMBERSHIP_FOR_UPDATE_SQL",
    "_UPDATE_MEMBERSHIP_ROLE_SQL",
    "_UPDATE_MEMBERSHIP_STATUS_SQL",
    "_UPDATE_MEMBERSHIP_ROLE_AND_STATUS_SQL",
    "_COUNT_OTHER_ACTIVE_ADMINS_SQL",
)


@pytest.mark.parametrize("sql_name", _SQL_NAMES)
def test_sql_uses_pg_placeholders_only(sql_name):
    """No SQLite ``?`` placeholders, no compat-era idioms."""
    from backend.routers import tenant_members as m
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


@pytest.mark.parametrize("sql_name", _SQL_NAMES)
def test_sql_does_not_project_secrets(sql_name):
    """Membership / user secrets must never reach the response or the
    audit blob — no password_hash, no oidc_*, no token_hash."""
    from backend.routers import tenant_members as m
    sql = getattr(m, sql_name).lower()
    for forbidden in ("password_hash", "oidc_subject", "oidc_provider",
                      "token_hash"):
        assert forbidden not in sql, (
            f"{sql_name} projects forbidden field {forbidden!r}"
        )


def test_fetch_tenant_sql_is_read_only():
    from backend.routers.tenant_members import _FETCH_TENANT_SQL
    upper = _FETCH_TENANT_SQL.upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in upper
    assert "SELECT" in upper


def test_list_members_active_sql_filters_active_only():
    from backend.routers.tenant_members import _LIST_MEMBERS_ACTIVE_SQL
    sql = _LIST_MEMBERS_ACTIVE_SQL
    # Active filter present.
    assert "m.status = 'active'" in sql
    # Joined onto users for email/name.
    assert "JOIN users u" in sql
    # Stable ordering.
    assert "ORDER BY" in sql
    # LIMIT placeholder.
    assert "LIMIT $2" in sql
    # Read-only.
    upper = sql.upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in upper


def test_list_members_all_sql_does_not_filter_status():
    from backend.routers.tenant_members import _LIST_MEMBERS_ALL_SQL
    # No "m.status =" expression — the 'all' sentinel skips that filter.
    assert "m.status =" not in _LIST_MEMBERS_ALL_SQL
    # Still tenant-scoped.
    assert "m.tenant_id = $1" in _LIST_MEMBERS_ALL_SQL


def test_list_members_by_status_sql_parameterises_status():
    from backend.routers.tenant_members import _LIST_MEMBERS_BY_STATUS_SQL
    assert "m.status = $2" in _LIST_MEMBERS_BY_STATUS_SQL
    assert "LIMIT $3" in _LIST_MEMBERS_BY_STATUS_SQL


def test_fetch_membership_for_update_sql_has_for_update():
    """FOR UPDATE is needed inside the write transaction so concurrent
    PATCH/DELETE serialise on the row lock."""
    from backend.routers.tenant_members import _FETCH_MEMBERSHIP_FOR_UPDATE_SQL
    sql = _FETCH_MEMBERSHIP_FOR_UPDATE_SQL
    assert "FOR UPDATE" in sql


def test_fetch_membership_sql_has_no_for_update():
    """The non-locking fetch is for reads outside a write tx (404
    disambiguation, etc.). Drift guard against the locking variant
    leaking into read-only paths."""
    from backend.routers.tenant_members import _FETCH_MEMBERSHIP_SQL
    assert "FOR UPDATE" not in _FETCH_MEMBERSHIP_SQL


def test_update_role_sql_atomic_with_returning():
    from backend.routers.tenant_members import _UPDATE_MEMBERSHIP_ROLE_SQL
    upper = _UPDATE_MEMBERSHIP_ROLE_SQL.upper()
    assert "UPDATE USER_TENANT_MEMBERSHIPS" in upper
    assert "SET ROLE = $3" in upper
    assert "WHERE USER_ID = $1" in upper
    assert "TENANT_ID = $2" in upper
    assert "RETURNING" in upper


def test_update_status_sql_atomic_with_returning():
    from backend.routers.tenant_members import _UPDATE_MEMBERSHIP_STATUS_SQL
    upper = _UPDATE_MEMBERSHIP_STATUS_SQL.upper()
    assert "UPDATE USER_TENANT_MEMBERSHIPS" in upper
    assert "SET STATUS = $3" in upper
    assert "WHERE USER_ID = $1" in upper
    assert "TENANT_ID = $2" in upper
    assert "RETURNING" in upper


def test_update_role_and_status_sql_atomic_with_returning():
    from backend.routers.tenant_members import (
        _UPDATE_MEMBERSHIP_ROLE_AND_STATUS_SQL,
    )
    upper = _UPDATE_MEMBERSHIP_ROLE_AND_STATUS_SQL.upper()
    assert "UPDATE USER_TENANT_MEMBERSHIPS" in upper
    assert "SET ROLE = $3" in upper
    assert "STATUS = $4" in upper
    assert "RETURNING" in upper


def test_count_other_admins_sql_excludes_target_and_filters_active_enabled():
    """Floor check correctness: must exclude target, only count
    role∈{owner,admin}, only count status=active, only count
    enabled=1 users."""
    from backend.routers.tenant_members import _COUNT_OTHER_ACTIVE_ADMINS_SQL
    sql = _COUNT_OTHER_ACTIVE_ADMINS_SQL
    assert "COUNT(*)" in sql
    assert "m.user_id <> $2" in sql
    assert "m.status = 'active'" in sql
    assert "m.role IN ('owner', 'admin')" in sql
    assert "u.enabled = 1" in sql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Router endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_get_patch_delete():
    from backend.routers.tenant_members import router
    paths_methods: set[tuple[str, str]] = set()
    for r in router.routes:
        for m in getattr(r, "methods", set()):
            paths_methods.add((r.path, m))
    assert ("/tenants/{tenant_id}/members", "GET") in paths_methods
    assert ("/tenants/{tenant_id}/members/{user_id}", "PATCH") in paths_methods
    assert ("/tenants/{tenant_id}/members/{user_id}", "DELETE") in paths_methods


@pytest.mark.parametrize("handler_name", [
    "list_members", "patch_member", "delete_member",
])
def test_handler_uses_current_user_dependency(handler_name):
    """All three handlers must depend on auth.current_user. Per-tenant
    membership RBAC happens inside each handler (via
    ``_user_can_manage_members``) — putting it on the dependency layer
    would force an awkward generic gate."""
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_members
    from backend import auth as _au

    handler = getattr(tenant_members, handler_name)
    deps = [
        v.default for v in (
            inspect.signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    assert any(
        getattr(d, "dependency", None) is _au.current_user for d in deps
    ), f"{handler_name} must depend on auth.current_user"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_member_endpoints():
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    full = set()
    for r in app.routes:
        for m in getattr(r, "methods", set()):
            full.add((r.path, m))
    assert ("/api/v1/tenants/{tenant_id}/members", "GET") in full
    assert (
        "/api/v1/tenants/{tenant_id}/members/{user_id}", "PATCH",
    ) in full
    assert (
        "/api/v1/tenants/{tenant_id}/members/{user_id}", "DELETE",
    ) in full


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Test {tid}",
        )


async def _purge_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM users WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'tenant_membership' "
            "AND tenant_id = $1",
            tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


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
    created_at: str | None = None,
) -> None:
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S",
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_tenant_memberships "
            "(user_id, tenant_id, role, status, created_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            uid, tid, role, status, created_at,
        )


async def _read_membership_row(pool, *, uid: str, tid: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            uid, tid,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) HTTP — GET /tenants/{tid}/members
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_members_default_lists_active_only(client, pg_test_pool):
    tid = "t-y3-mem-listdef"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid="u-mem-act-1", tid=tid,
                         email="active@example.com")
        await _seed_user(pg_test_pool, uid="u-mem-sus-1", tid=tid,
                         email="suspended@example.com")
        await _seed_membership(pg_test_pool, uid="u-mem-act-1", tid=tid,
                               role="admin", status="active")
        await _seed_membership(pg_test_pool, uid="u-mem-sus-1", tid=tid,
                               role="member", status="suspended")

        res = await client.get(f"/api/v1/tenants/{tid}/members")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["status_filter"] == "active"
        assert body["count"] == 1
        emails = [m["email"] for m in body["members"]]
        assert emails == ["active@example.com"]
        # Required fields surfaced.
        m = body["members"][0]
        assert m["user_id"] == "u-mem-act-1"
        assert m["role"] == "admin"
        assert m["status"] == "active"
        assert "user_enabled" in m
        assert "joined_at" in m
        # No PII / secret leaks.
        for k in ("password_hash", "oidc_subject", "oidc_provider",
                  "token_hash"):
            assert k not in m
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_status_all_includes_suspended(client, pg_test_pool):
    tid = "t-y3-mem-listall"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid="u-mem-allact", tid=tid,
                         email="a@x.com")
        await _seed_user(pg_test_pool, uid="u-mem-allsus", tid=tid,
                         email="b@x.com")
        await _seed_membership(pg_test_pool, uid="u-mem-allact", tid=tid,
                               role="member", status="active")
        await _seed_membership(pg_test_pool, uid="u-mem-allsus", tid=tid,
                               role="member", status="suspended")

        res = await client.get(
            f"/api/v1/tenants/{tid}/members?status=all",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status_filter"] == "all"
        assert body["count"] == 2
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_unknown_status_returns_422(client, pg_test_pool):
    tid = "t-y3-mem-bad-status"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/members?status=banished",
        )
        assert res.status_code == 422, res.text
        assert "invalid status filter" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_limit_clamp(client, pg_test_pool):
    tid = "t-y3-mem-limit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        for i in range(3):
            uid = f"u-mem-lim-{i}"
            await _seed_user(pg_test_pool, uid=uid, tid=tid,
                             email=f"u{i}@x.com")
            await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                                   role="member", status="active")

        # Limit 2 truncates.
        res = await client.get(
            f"/api/v1/tenants/{tid}/members?limit=2",
        )
        assert res.status_code == 200
        assert res.json()["count"] == 2

        # Oversized limit → 422 (FastAPI Query ge/le).
        res = await client.get(
            f"/api/v1/tenants/{tid}/members?limit=999999",
        )
        assert res.status_code == 422
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_members_malformed_tenant_id_returns_422(client):
    res = await client.get("/api/v1/tenants/T-Bad/members")
    assert res.status_code == 422


@_requires_pg
async def test_get_members_unknown_tenant_returns_404(client):
    res = await client.get(
        "/api/v1/tenants/t-does-not-exist/members",
    )
    assert res.status_code == 404


@_requires_pg
async def test_get_members_member_role_gets_403(client, pg_test_pool):
    """A user with membership.role='member' on the target tenant must
    NOT list members — the email list is PII."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-mem-rbac-list"
    uid = "u-mem-rbac-list-m"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=uid, tid=tid,
                         email="m@x.com")
        await _seed_membership(pg_test_pool, uid=uid, tid=tid,
                               role="member", status="active")

        member = _au.User(
            id=uid, email="m@x.com", name="M",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return member

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/members")
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) HTTP — PATCH /tenants/{tid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_role_change_succeeds(client, pg_test_pool):
    tid = "t-y3-mem-patch-role"
    target = "u-mem-patch-role-1"
    keeper = "u-mem-patch-role-k"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Keep one independent admin so floor check passes when target
        # is admin-tier and we demote it.
        await _seed_user(pg_test_pool, uid=keeper, tid=tid,
                         email="keep@x.com")
        await _seed_membership(pg_test_pool, uid=keeper, tid=tid,
                               role="admin", status="active")
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"role": "admin"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["role"] == "admin"
        assert body["no_change"] is False
        row = await _read_membership_row(pg_test_pool, uid=target, tid=tid)
        assert row["role"] == "admin"
        assert row["status"] == "active"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_status_change_succeeds(client, pg_test_pool):
    tid = "t-y3-mem-patch-status"
    target = "u-mem-patch-st-1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"status": "suspended"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "suspended"
        row = await _read_membership_row(pg_test_pool, uid=target, tid=tid)
        assert row["status"] == "suspended"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_no_change_returns_no_change_flag(client, pg_test_pool):
    """Re-PATCH to the same state returns 200 with no_change=True."""
    tid = "t-y3-mem-patch-nochg"
    target = "u-mem-patch-nochg"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"role": "member", "status": "active"},
        )
        assert res.status_code == 200, res.text
        assert res.json()["no_change"] is True
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_empty_body_returns_422(client, pg_test_pool):
    tid = "t-y3-mem-patch-empty"
    target = "u-mem-patch-empty-x"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_last_admin_demotion_returns_409(client, pg_test_pool):
    """Demoting the last active admin-tier member of a tenant must be
    refused with 409 and leave the row untouched."""
    tid = "t-y3-mem-patch-floor"
    lone = "u-mem-patch-lone"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=lone, tid=tid,
                         email="lone@x.com")
        # The ONLY admin-tier active enabled member of the tenant.
        await _seed_membership(pg_test_pool, uid=lone, tid=tid,
                               role="admin", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{lone}",
            json={"role": "member"},
        )
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["would_leave_zero_admin_members"] is True
        assert body["other_active_admin_member_count"] == 0
        # Row untouched.
        row = await _read_membership_row(pg_test_pool, uid=lone, tid=tid)
        assert row["role"] == "admin"
        assert row["status"] == "active"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_last_admin_suspension_returns_409(client, pg_test_pool):
    """Suspending the last active admin via PATCH (not DELETE) is also
    blocked by the floor."""
    tid = "t-y3-mem-patch-floor-st"
    lone = "u-mem-patch-floor-st"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=lone, tid=tid,
                         email="lone@x.com")
        await _seed_membership(pg_test_pool, uid=lone, tid=tid,
                               role="admin", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{lone}",
            json={"status": "suspended"},
        )
        assert res.status_code == 409, res.text
        row = await _read_membership_row(pg_test_pool, uid=lone, tid=tid)
        assert row["status"] == "active"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_demote_with_other_admin_succeeds(client, pg_test_pool):
    """When another active admin exists, demoting an admin-tier member
    must succeed (floor not breached)."""
    tid = "t-y3-mem-patch-with-other"
    target = "u-mem-patch-other-t"
    keeper = "u-mem-patch-other-k"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=keeper, tid=tid,
                         email="k@x.com")
        await _seed_membership(pg_test_pool, uid=keeper, tid=tid,
                               role="admin", status="active")
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="admin", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"role": "member"},
        )
        assert res.status_code == 200, res.text
        row = await _read_membership_row(pg_test_pool, uid=target, tid=tid)
        assert row["role"] == "member"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_unknown_membership_returns_404(client, pg_test_pool):
    tid = "t-y3-mem-patch-404"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/u-deadbeef99",
            json={"role": "member"},
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_patch_malformed_user_id_returns_422(client, pg_test_pool):
    tid = "t-y3-mem-patch-baduid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/U-BAD",
            json={"role": "member"},
        )
        assert res.status_code == 422
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (i) HTTP — DELETE /tenants/{tid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_member_soft_deletes_to_suspended(client, pg_test_pool):
    """DELETE flips status to 'suspended' (soft delete) — preserves
    the membership row and audit trail. Hard delete only happens on
    tenant deletion (cascade)."""
    tid = "t-y3-mem-del-soft"
    target = "u-mem-del-soft-t"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/{target}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "suspended"
        assert body["already_suspended"] is False
        # Row preserved, status flipped.
        row = await _read_membership_row(pg_test_pool, uid=target, tid=tid)
        assert row is not None
        assert row["status"] == "suspended"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_member_already_suspended_is_idempotent(
    client, pg_test_pool,
):
    tid = "t-y3-mem-del-idem"
    target = "u-mem-del-idem-t"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="suspended")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/{target}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["already_suspended"] is True
        assert body["status"] == "suspended"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_last_admin_returns_409(client, pg_test_pool):
    """DELETE on the last active admin-tier member must be refused
    with 409 and leave the row untouched."""
    tid = "t-y3-mem-del-floor"
    lone = "u-mem-del-floor-1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=lone, tid=tid,
                         email="lone@x.com")
        await _seed_membership(pg_test_pool, uid=lone, tid=tid,
                               role="admin", status="active")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/{lone}",
        )
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["would_leave_zero_admin_members"] is True
        # Row untouched.
        row = await _read_membership_row(pg_test_pool, uid=lone, tid=tid)
        assert row["status"] == "active"
        assert row["role"] == "admin"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_disabled_admin_bypasses_floor(client, pg_test_pool):
    """A disabled user can't log in, so demoting their admin row does
    not reduce the live operator count — DELETE should succeed even if
    they'd otherwise be the last admin."""
    tid = "t-y3-mem-del-disabled"
    target = "u-mem-del-disabled"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="dis@x.com", enabled=0)
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="admin", status="active")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/{target}",
        )
        assert res.status_code == 200, res.text
        row = await _read_membership_row(pg_test_pool, uid=target, tid=tid)
        assert row["status"] == "suspended"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_unknown_member_returns_404(client, pg_test_pool):
    tid = "t-y3-mem-del-404"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/u-deadbeef00",
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_malformed_user_id_returns_422(client, pg_test_pool):
    tid = "t-y3-mem-del-baduid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/U-BAD",
        )
        assert res.status_code == 422
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_delete_unknown_tenant_returns_404(client):
    res = await client.delete(
        "/api/v1/tenants/t-does-not-exist/members/u-deadbeef00",
    )
    assert res.status_code == 404


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (j) Audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_patch_emits_member_updated_audit(client, pg_test_pool):
    tid = "t-y3-mem-audit-patch"
    target = "u-mem-audit-patch-t"
    keeper = "u-mem-audit-patch-k"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=keeper, tid=tid,
                         email="k@x.com")
        await _seed_membership(pg_test_pool, uid=keeper, tid=tid,
                               role="admin", status="active")
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"role": "admin"},
        )
        assert res.status_code == 200

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, "
                "before_json, after_json FROM audit_log "
                "WHERE action = 'tenant_member_updated' "
                "AND entity_id = $1 ORDER BY id DESC LIMIT 1",
                target,
            )
        assert row is not None
        assert row["entity_kind"] == "tenant_membership"
        import json
        before = json.loads(row["before_json"])
        after = json.loads(row["after_json"])
        assert before["role"] == "member"
        assert after["role"] == "admin"
        # Status unchanged but mirrored on both sides.
        assert before["status"] == after["status"] == "active"
        # No secret leaks.
        for blob in (row["before_json"] or "", row["after_json"] or ""):
            for forbidden in ("password_hash", "oidc_subject",
                              "oidc_provider", "token_hash"):
                assert forbidden not in blob
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_no_change_patch_emits_no_audit(client, pg_test_pool):
    """Same-state PATCH must NOT emit an audit row."""
    tid = "t-y3-mem-audit-noop"
    target = "u-mem-audit-noop"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="active")

        res = await client.patch(
            f"/api/v1/tenants/{tid}/members/{target}",
            json={"role": "member"},
        )
        assert res.status_code == 200
        assert res.json()["no_change"] is True

        async with pg_test_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_member_updated' "
                "AND entity_id = $1",
                target,
            )
        assert int(n) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_already_suspended_delete_emits_no_audit(client, pg_test_pool):
    tid = "t-y3-mem-audit-idem"
    target = "u-mem-audit-idem"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_user(pg_test_pool, uid=target, tid=tid,
                         email="t@x.com")
        await _seed_membership(pg_test_pool, uid=target, tid=tid,
                               role="member", status="suspended")

        res = await client.delete(
            f"/api/v1/tenants/{tid}/members/{target}",
        )
        assert res.status_code == 200
        assert res.json()["already_suspended"] is True

        async with pg_test_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'tenant_member_updated' "
                "AND entity_id = $1",
                target,
            )
        assert int(n) == 0
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (k) Self-fingerprint guard — pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """Refuse to ship any of the four compat-era markers documented
    in ``docs/sop/implement_phase_step.md`` Step 3."""
    src = pathlib.Path(
        "backend/routers/tenant_members.py"
    ).read_text(encoding="utf-8")
    fingerprint_re = re.compile(
        r"_conn\(\)|await\s+conn\.commit\(\)|datetime\('now'\)"
        r"|VALUES\s*\([^)]*\?[,)]"
    )
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat fingerprint hit: {hits}"

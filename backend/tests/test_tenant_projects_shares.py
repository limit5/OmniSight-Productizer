"""Y4 (#280) row 6 — drift guard for the cross-tenant project-share surface.

Pure-unit + ASGI mount tests run without PG.  Live-PG HTTP path tests
exercise end-to-end behaviour (POST share, atomic ON CONFLICT 409,
self-share guard, cross-tenant guest existence, expires_at validation,
RBAC) and skip when ``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) Module-level constants — role enum aligns with alembic 0036
      DB CHECK (no 'owner'); SHARE_ID_PATTERN shape; EXPIRES_AT_FORMAT
      string-comparison-safe; tenant role allowlist admin-tier-only
  (b) Pydantic body — ``CreateProjectShareRequest`` enforces the role
      enum, guest_tenant_id pattern, and accepts the expires_at field
  (c) SQL constants — PG ``$N`` placeholder, secret-leak guard,
      INSERT atomic with ``ON CONFLICT (project_id, guest_tenant_id)
      DO NOTHING RETURNING``, read-only existing-row fetch
  (d) Router endpoint exposed with ``auth.current_user`` dependency
  (e) Main app full-prefix mount confirms POST path + method
  (f) HTTP path: happy / dup 409 / 404 unknown tenant / 404 unknown
      project / 404 unknown guest tenant / 422 self-share / 422
      malformed body / 422 expires_at malformed / 422 expires_at past
  (g) RBAC: super_admin / tenant admin pass; tenant member / no
      membership fail
  (h) Audit: tenant_project_shared row written with no secret leaks
  (i) Self-fingerprint guard
"""

from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta, timezone
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


def test_project_share_role_enum_matches_db_check():
    """The DB CHECK on ``project_shares.role`` from alembic 0036
    enforces this exact enum — drift here would let a regressed POST
    set a role the DB then rejects."""
    from backend.routers.tenant_projects import PROJECT_SHARE_ROLE_ENUM
    assert PROJECT_SHARE_ROLE_ENUM == ("viewer", "contributor")


def test_project_share_role_enum_excludes_owner():
    """A guest tenant fundamentally cannot own a project belonging to
    a different tenant — drift here regresses the migration's CHECK
    intent."""
    from backend.routers.tenant_projects import PROJECT_SHARE_ROLE_ENUM
    assert "owner" not in PROJECT_SHARE_ROLE_ENUM


def test_project_share_role_enum_distinct_from_project_member_enum():
    """Project member enum has 'owner'; share enum does not — by
    design.  Conflating them would let a regression promote a guest
    tenant to owner."""
    from backend.routers.tenant_projects import (
        PROJECT_SHARE_ROLE_ENUM,
        PROJECT_MEMBER_ROLE_ENUM,
    )
    assert PROJECT_SHARE_ROLE_ENUM != PROJECT_MEMBER_ROLE_ENUM
    assert "owner" in PROJECT_MEMBER_ROLE_ENUM
    assert "owner" not in PROJECT_SHARE_ROLE_ENUM


def test_share_id_pattern_constant():
    """Share id shape uses ``psh-`` prefix per migration 0036."""
    from backend.routers.tenant_projects import SHARE_ID_PATTERN
    assert SHARE_ID_PATTERN == r"^psh-[a-z0-9]{4,64}$"


@pytest.mark.parametrize("good_sid", [
    "psh-abcd",
    "psh-0123456789abcdef",
    "psh-deadbeefdeadbeef",
    "psh-" + "a" * 64,
])
def test_share_id_validator_accepts(good_sid):
    from backend.routers.tenant_projects import _is_valid_share_id
    assert _is_valid_share_id(good_sid)


@pytest.mark.parametrize("bad_sid", [
    "", "abcd", "PSH-abcd", "psh-ABCD", "psh-",
    "psh-abc", "psh-" + "a" * 65, "psh-abc_def",
    " psh-abcd", "psh-abcd ",
])
def test_share_id_validator_rejects(bad_sid):
    from backend.routers.tenant_projects import _is_valid_share_id
    assert not _is_valid_share_id(bad_sid)


def test_mint_share_id_shape_and_uniqueness():
    """``psh-`` + 16 hex chars; collisions over a 50-sample batch are
    a non-event."""
    from backend.routers.tenant_projects import (
        _mint_share_id, _is_valid_share_id,
    )
    seen = {_mint_share_id() for _ in range(50)}
    assert len(seen) == 50
    for sid in seen:
        assert sid.startswith("psh-")
        assert len(sid) == len("psh-") + 16
        assert _is_valid_share_id(sid)


def test_expires_at_format_constant():
    """Same shape as ``projects.created_at`` / ``projects.archived_at``
    — keeps text comparisons (``expires_at < $now``) correct without
    dialect-specific date arithmetic."""
    from backend.routers.tenant_projects import EXPIRES_AT_FORMAT
    assert EXPIRES_AT_FORMAT == "%Y-%m-%d %H:%M:%S"


def test_share_create_allowed_membership_roles_admin_tier_only():
    from backend.routers.tenant_projects import (
        _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES,
    )
    assert _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES == frozenset(
        {"owner", "admin"}
    )
    assert "member" not in _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES
    assert "viewer" not in _PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pydantic body
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_request_happy_minimal():
    from backend.routers.tenant_projects import CreateProjectShareRequest
    body = CreateProjectShareRequest(
        guest_tenant_id="t-guestcorp", role="viewer",
    )
    assert body.guest_tenant_id == "t-guestcorp"
    assert body.role == "viewer"
    assert body.expires_at is None


def test_create_request_with_expires_at():
    from backend.routers.tenant_projects import CreateProjectShareRequest
    body = CreateProjectShareRequest(
        guest_tenant_id="t-guestcorp",
        role="contributor",
        expires_at="2099-12-31 23:59:59",
    )
    assert body.expires_at == "2099-12-31 23:59:59"


@pytest.mark.parametrize("good_role", ["viewer", "contributor"])
def test_create_request_accepts_each_role(good_role):
    from backend.routers.tenant_projects import CreateProjectShareRequest
    body = CreateProjectShareRequest(
        guest_tenant_id="t-guestcorp", role=good_role,
    )
    assert body.role == good_role


@pytest.mark.parametrize("bad_role", [
    "owner", "OWNER", "admin", "Admin", "member", "guest", "",
])
def test_create_request_rejects_bad_role(bad_role):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectShareRequest
    with pytest.raises(ValidationError):
        CreateProjectShareRequest(
            guest_tenant_id="t-guestcorp", role=bad_role,
        )


@pytest.mark.parametrize("bad_tid", [
    "guestcorp", "T-Guestcorp", "t-", "t-A", "t-bad_id",
    " t-guestcorp", "t-guestcorp ",
])
def test_create_request_rejects_bad_guest_tenant_id(bad_tid):
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectShareRequest
    with pytest.raises(ValidationError):
        CreateProjectShareRequest(
            guest_tenant_id=bad_tid, role="viewer",
        )


def test_create_request_requires_guest_tenant_id_and_role():
    from pydantic import ValidationError
    from backend.routers.tenant_projects import CreateProjectShareRequest
    with pytest.raises(ValidationError):
        CreateProjectShareRequest(role="viewer")
    with pytest.raises(ValidationError):
        CreateProjectShareRequest(guest_tenant_id="t-guestcorp")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  expires_at validator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_validate_expires_at_future_ok():
    from backend.routers.tenant_projects import _validate_expires_at
    future = (datetime.utcnow() + timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    ok, normalized = _validate_expires_at(future)
    assert ok
    assert normalized == future


def test_validate_expires_at_past_rejects():
    from backend.routers.tenant_projects import _validate_expires_at
    past = (datetime.utcnow() - timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    ok, reason = _validate_expires_at(past)
    assert not ok
    assert "future" in reason


def test_validate_expires_at_now_rejects():
    """``<=`` semantics — same-second is treated as past."""
    from backend.routers.tenant_projects import _validate_expires_at
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ok, reason = _validate_expires_at(now)
    assert not ok


@pytest.mark.parametrize("bad", [
    "", "not-a-date", "2099/12/31 23:59:59", "2099-12-31",
    "2099-12-31T23:59:59", "2099-12-31 23:59:59Z",
])
def test_validate_expires_at_bad_format(bad):
    from backend.routers.tenant_projects import _validate_expires_at
    ok, reason = _validate_expires_at(bad)
    assert not ok


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SHARE_SQL_NAMES = (
    "_INSERT_PROJECT_SHARE_SQL",
    "_FETCH_EXISTING_PROJECT_SHARE_SQL",
)


@pytest.mark.parametrize("sql_name", _SHARE_SQL_NAMES)
def test_share_sql_uses_pg_placeholders_only(sql_name):
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
    assert "$1" in sql


@pytest.mark.parametrize("sql_name", _SHARE_SQL_NAMES)
def test_share_sql_does_not_leak_secret_columns(sql_name):
    """No password_hash / oidc_* / token_hash projection — share rows
    must never accidentally surface user account secrets even via
    audit blob projection."""
    from backend.routers import tenant_projects as m
    sql = getattr(m, sql_name).lower()
    for forbidden in ("password_hash", "oidc_subject", "oidc_provider",
                      "token_hash"):
        assert forbidden not in sql


def test_insert_project_share_sql_atomic_with_on_conflict():
    """ON CONFLICT (project_id, guest_tenant_id) DO NOTHING + RETURNING
    resolves "insert-or-detect-duplicate" in one round-trip."""
    from backend.routers.tenant_projects import _INSERT_PROJECT_SHARE_SQL
    sql = _INSERT_PROJECT_SHARE_SQL
    assert "INSERT INTO project_shares" in sql
    assert "ON CONFLICT (project_id, guest_tenant_id) DO NOTHING" in sql
    assert "RETURNING" in sql.upper()


def test_insert_project_share_sql_includes_expected_columns():
    from backend.routers.tenant_projects import _INSERT_PROJECT_SHARE_SQL
    for col in ("id", "project_id", "guest_tenant_id", "role",
                "granted_by", "expires_at"):
        assert col in _INSERT_PROJECT_SHARE_SQL
    # 6 placeholders → $1..$6 supplied by handler.
    for placeholder in ("$1", "$2", "$3", "$4", "$5", "$6"):
        assert placeholder in _INSERT_PROJECT_SHARE_SQL


def test_fetch_existing_project_share_sql_read_only():
    from backend.routers.tenant_projects import (
        _FETCH_EXISTING_PROJECT_SHARE_SQL,
    )
    upper = _FETCH_EXISTING_PROJECT_SHARE_SQL.upper()
    assert "SELECT" in upper
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP "):
        assert verb not in upper
    assert "WHERE PROJECT_ID = $1 AND GUEST_TENANT_ID = $2" in upper


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) Router endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_share_post():
    from backend.routers.tenant_projects import router
    paths_methods: set[tuple[str, str]] = set()
    for r in router.routes:
        for mm in getattr(r, "methods", set()):
            paths_methods.add((r.path, mm))
    assert (
        "/tenants/{tenant_id}/projects/{project_id}/shares", "POST",
    ) in paths_methods


def test_share_handler_uses_current_user_dependency():
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_projects
    from backend import auth as _au

    handler = tenant_projects.create_project_share
    deps = [
        v.default for v in (
            inspect.signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    assert any(
        getattr(d, "dependency", None) is _au.current_user for d in deps
    ), "create_project_share must depend on auth.current_user"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_share_endpoint():
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    full: set[tuple[str, str]] = set()
    for r in app.routes:
        for mm in getattr(r, "methods", set()) or set():
            full.add((r.path, mm))
    assert (
        "/api/v1/tenants/{tenant_id}/projects/{project_id}/shares",
        "POST",
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
        # project_shares — both as host (project lives here) and as
        # guest (rows referencing this tenant as guest).
        await conn.execute(
            "DELETE FROM project_shares "
            "WHERE project_id IN (SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM project_shares WHERE guest_tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM project_members WHERE project_id IN "
            "(SELECT id FROM projects WHERE tenant_id = $1)",
            tid,
        )
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'project_share' "
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


async def _read_project_share(pool, *, pid: str, guest_tid: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, project_id, guest_tenant_id, role, "
            "       granted_by, created_at, expires_at "
            "FROM project_shares "
            "WHERE project_id = $1 AND guest_tenant_id = $2",
            pid, guest_tid,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) HTTP — POST /tenants/{tid}/projects/{pid}/shares
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_share_happy_inserts_row(client, pg_test_pool):
    host_tid = "t-y4-sh-host"
    guest_tid = "t-y4-sh-guest"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="happy")

        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": guest_tid, "role": "viewer"},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["project_id"] == pid
        assert body["guest_tenant_id"] == guest_tid
        assert body["role"] == "viewer"
        assert body["tenant_id"] == host_tid
        assert body["share_id"].startswith("psh-")
        assert "created_at" in body
        assert body["expires_at"] is None
        for k in ("password_hash", "oidc_subject", "oidc_provider",
                  "token_hash"):
            assert k not in body

        row = await _read_project_share(
            pg_test_pool, pid=pid, guest_tid=guest_tid,
        )
        assert row is not None
        assert row["role"] == "viewer"
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
@pytest.mark.parametrize("role", ["viewer", "contributor"])
async def test_post_share_accepts_each_role(client, pg_test_pool, role):
    host_tid = f"t-y4-sh-r-{role}"
    guest_tid = f"t-y4-sh-rg-{role}"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="rl")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": guest_tid, "role": role},
        )
        assert res.status_code == 201, res.text
        assert res.json()["role"] == role
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_post_share_with_future_expires_at(client, pg_test_pool):
    host_tid = "t-y4-sh-exp"
    guest_tid = "t-y4-sh-expg"
    future = (datetime.utcnow() + timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="exp")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={
                "guest_tenant_id": guest_tid,
                "role": "contributor",
                "expires_at": future,
            },
        )
        assert res.status_code == 201, res.text
        assert res.json()["expires_at"] == future
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_post_share_duplicate_returns_409_with_existing_role(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-dup"
    guest_tid = "t-y4-sh-dupg"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="dup")
        first = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": guest_tid, "role": "viewer"},
        )
        assert first.status_code == 201

        second = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": guest_tid, "role": "contributor"},
        )
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["existing_role"] == "viewer"
        assert body["existing_share_id"]
        assert body["guest_tenant_id"] == guest_tid

        # Existing row untouched.
        row = await _read_project_share(
            pg_test_pool, pid=pid, guest_tid=guest_tid,
        )
        assert row["role"] == "viewer"
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_post_share_self_share_returns_422(client, pg_test_pool):
    """A tenant cannot share a project to itself — 422 (body
    validation), not 403."""
    host_tid = "t-y4-sh-self"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        pid = await _create_project(client, host_tid, slug="self")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": host_tid, "role": "viewer"},
        )
        assert res.status_code == 422, res.text
        assert "cannot share a project to itself" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, host_tid)


@_requires_pg
async def test_post_share_unknown_owning_tenant_returns_404(client):
    res = await client.post(
        "/api/v1/tenants/t-y4-sh-noten/projects/"
        "p-deadbeefdeadbeef/shares",
        json={"guest_tenant_id": "t-y4-sh-other", "role": "viewer"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_share_unknown_project_returns_404(client, pg_test_pool):
    host_tid = "t-y4-sh-noproj"
    guest_tid = "t-y4-sh-noprojg"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/"
            "p-deadbeefdeadbeef/shares",
            json={"guest_tenant_id": guest_tid, "role": "viewer"},
        )
        assert res.status_code == 404, res.text
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_post_share_cross_tenant_project_returns_404(
    client, pg_test_pool,
):
    """Project owned by tenant A is invisible from tenant B's
    namespace — 404, not 403."""
    t_a = "t-y4-sh-iso-a"
    t_b = "t-y4-sh-iso-b"
    t_c = "t-y4-sh-iso-c"
    try:
        await _seed_tenant(pg_test_pool, t_a)
        await _seed_tenant(pg_test_pool, t_b)
        await _seed_tenant(pg_test_pool, t_c)
        pid_a = await _create_project(client, t_a, slug="aproj")

        res = await client.post(
            f"/api/v1/tenants/{t_b}/projects/{pid_a}/shares",
            json={"guest_tenant_id": t_c, "role": "viewer"},
        )
        assert res.status_code == 404, res.text

        # Project_a's share roster untouched.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM project_shares WHERE project_id = $1",
                pid_a,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, t_a)
        await _purge_tenant(pg_test_pool, t_b)
        await _purge_tenant(pg_test_pool, t_c)


@_requires_pg
async def test_post_share_unknown_guest_tenant_returns_404(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-noguest"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        pid = await _create_project(client, host_tid, slug="ngt")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": "t-doesnotexist1234",
                  "role": "viewer"},
        )
        assert res.status_code == 404, res.text
        assert "guest tenant" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, host_tid)


@_requires_pg
async def test_post_share_malformed_tenant_id_returns_422(client):
    res = await client.post(
        "/api/v1/tenants/T-Bad/projects/p-aaaabbbbccccdddd/shares",
        json={"guest_tenant_id": "t-guestcorp", "role": "viewer"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_share_malformed_project_id_returns_422(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-badpid"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/P-BAD/shares",
            json={"guest_tenant_id": "t-guestcorp", "role": "viewer"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, host_tid)


@_requires_pg
async def test_post_share_malformed_guest_tenant_id_returns_422(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-badgtid"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        pid = await _create_project(client, host_tid, slug="bgt")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": "T-BAD", "role": "viewer"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, host_tid)


@_requires_pg
async def test_post_share_unknown_role_returns_422(client, pg_test_pool):
    host_tid = "t-y4-sh-badrole"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        pid = await _create_project(client, host_tid, slug="br")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": "t-guestcorp", "role": "owner"},
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, host_tid)


@_requires_pg
async def test_post_share_expires_at_in_past_returns_422(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-pastexp"
    guest_tid = "t-y4-sh-pastexpg"
    past = (datetime.utcnow() - timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="px")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={
                "guest_tenant_id": guest_tid,
                "role": "viewer",
                "expires_at": past,
            },
        )
        assert res.status_code == 422, res.text
        assert "future" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_post_share_expires_at_malformed_returns_422(
    client, pg_test_pool,
):
    host_tid = "t-y4-sh-badexp"
    guest_tid = "t-y4-sh-badexpg"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="bex")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={
                "guest_tenant_id": guest_tid,
                "role": "viewer",
                "expires_at": "next tuesday",
            },
        )
        assert res.status_code == 422, res.text
        assert "format" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) RBAC — super_admin / tenant admin / non-admin / no-membership
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_share_endpoint_non_tenant_member_gets_403(
    client, pg_test_pool,
):
    """A user with membership.role='member' on the owning tenant must
    NOT grant cross-tenant shares — share grants change the project's
    exposure surface beyond the tenant and are tenant-admin-only."""
    from backend.main import app
    from backend import auth as _au

    host_tid = "t-y4-sh-rbac-mem"
    guest_tid = "t-y4-sh-rbac-memg"
    caller_uid = "u-y4shrbacmem1"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        await _seed_user(pg_test_pool, uid=caller_uid, tid=host_tid,
                         email="caller@x.com")
        await _seed_membership(pg_test_pool, uid=caller_uid, tid=host_tid,
                               role="member", status="active")
        pid = await _create_project(client, host_tid, slug="rbacm")

        caller = _au.User(
            id=caller_uid, email="caller@x.com", name="Caller",
            role="viewer", enabled=True, tenant_id=host_tid,
        )

        async def _fake_current_user():
            return caller

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
                json={"guest_tenant_id": guest_tid, "role": "viewer"},
            )
            assert res.status_code == 403, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # Roster untouched.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM project_shares WHERE project_id = $1",
                pid,
            )
        assert int(count) == 0
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


@_requires_pg
async def test_share_endpoint_tenant_admin_may_grant(client, pg_test_pool):
    from backend.main import app
    from backend import auth as _au

    host_tid = "t-y4-sh-rbac-adm"
    guest_tid = "t-y4-sh-rbac-admg"
    caller_uid = "u-y4shrbacadm1"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        await _seed_user(pg_test_pool, uid=caller_uid, tid=host_tid,
                         email="adm@x.com")
        await _seed_membership(pg_test_pool, uid=caller_uid, tid=host_tid,
                               role="admin", status="active")
        pid = await _create_project(client, host_tid, slug="rbaca")

        caller = _au.User(
            id=caller_uid, email="adm@x.com", name="Adm",
            role="viewer", enabled=True, tenant_id=host_tid,
        )

        async def _fake_current_user():
            return caller

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
                json={"guest_tenant_id": guest_tid, "role": "viewer"},
            )
            assert res.status_code == 201, res.text
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) Audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_share_audit_row_written(client, pg_test_pool):
    host_tid = "t-y4-sh-audit"
    guest_tid = "t-y4-sh-auditg"
    try:
        await _seed_tenant(pg_test_pool, host_tid)
        await _seed_tenant(pg_test_pool, guest_tid)
        pid = await _create_project(client, host_tid, slug="aud")
        res = await client.post(
            f"/api/v1/tenants/{host_tid}/projects/{pid}/shares",
            json={"guest_tenant_id": guest_tid, "role": "contributor"},
        )
        assert res.status_code == 201, res.text
        share_id = res.json()["share_id"]

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, "
                "       before_json, after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_project_shared' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                share_id,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "project_share"
        before_blob = audit_row["before_json"] or ""
        after_blob = audit_row["after_json"] or ""
        for blob in (before_blob, after_blob):
            for forbidden in ("password_hash", "oidc_subject",
                              "oidc_provider", "token_hash"):
                assert forbidden not in blob
        assert '"role":' in after_blob
        assert '"contributor"' in after_blob
        assert '"guest_tenant_id"' in after_blob
    finally:
        await _purge_tenant(pg_test_pool, host_tid)
        await _purge_tenant(pg_test_pool, guest_tid)


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

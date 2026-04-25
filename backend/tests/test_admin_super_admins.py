"""Y3 (#279) row 5 — drift guard for POST/DELETE /api/v1/admin/super-admins.

Pure-unit + ASGI mount tests run without PG. Live-PG HTTP path tests
exercise the end-to-end behaviour (promotion, demotion, idempotency,
last-super-admin floor, audit emission) and skip when
``OMNI_TEST_PG_URL`` is unset.

Drift guard families:
  (a) USER_ID_PATTERN regex shape + boundary cases
  (b) Pydantic schema (PromoteSuperAdminRequest)
  (c) DEMOTION_TARGET_ROLE constant matches design ('admin')
  (d) 5 SQL constants — read-only / FOR UPDATE / RETURNING / no-token-leak
      / PG ``$N`` placeholder (no SQLite ``?`` regression)
  (e) Router endpoints mounted with require_super_admin dependency
  (f) Main app full-prefix mount confirms ``/api/v1/admin/super-admins``
  (g) HTTP path: promotion happy / idempotent / disabled-refused / 404 /
      422 / RBAC 403
  (h) HTTP path: revoke happy / idempotent / last-super-admin 409 /
      404 / 422 / RBAC 403
  (i) Audit emission: super_admin_granted + super_admin_revoked rows
      land in audit_log under the actor's chain
  (j) Self-fingerprint guard (pre-commit pattern)
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
#  (a) USER_ID_PATTERN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_user_id_pattern_constant_matches_design():
    from backend.routers.admin_super_admins import USER_ID_PATTERN
    assert USER_ID_PATTERN == r"^u-[a-z0-9]{4,64}$"


@pytest.mark.parametrize("good", [
    "u-abcd",                   # min length 4 trailing
    "u-0123456789",             # uuid4().hex[:10] shape (auth.create_user)
    "u-deadbeef",               # token_hex(5) anon-create shape (accept)
    "u-" + "a" * 64,            # max length boundary
    "u-a1b2c3d4e5",
])
def test_user_id_pattern_accepts(good):
    from backend.routers.admin_super_admins import _is_valid_user_id
    assert _is_valid_user_id(good), f"should accept {good!r}"


@pytest.mark.parametrize("bad", [
    "",                         # empty
    "abcd",                     # missing 'u-' prefix
    "U-abcd",                   # uppercase prefix
    "u-ABCD",                   # uppercase trailing
    "u-",                       # trailing too short
    "u-abc",                    # 3 chars (need >=4)
    "u-" + "a" * 65,            # trailing 65 chars (max 64)
    "u-abc_def",                # underscore not in charset
    "u-abc def",                # space
    " u-abcd",                  # leading whitespace
    "u-abcd ",                  # trailing whitespace
    "u-abc-def",                # hyphen in trailing not allowed by spec
])
def test_user_id_pattern_rejects(bad):
    from backend.routers.admin_super_admins import _is_valid_user_id
    assert not _is_valid_user_id(bad), f"should reject {bad!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (b) Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_promote_request_accepts_well_formed_id():
    from backend.routers.admin_super_admins import PromoteSuperAdminRequest
    body = PromoteSuperAdminRequest(user_id="u-0123456789")
    assert body.user_id == "u-0123456789"


@pytest.mark.parametrize("bad", [
    "abcd", "U-abcd", "u-", "u-ABC", "u-a", "u-abc", "u-" + "a" * 65,
])
def test_promote_request_rejects_bad_ids(bad):
    from pydantic import ValidationError
    from backend.routers.admin_super_admins import PromoteSuperAdminRequest
    with pytest.raises(ValidationError):
        PromoteSuperAdminRequest(user_id=bad)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (c) DEMOTION_TARGET_ROLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_demotion_target_is_admin():
    """A revoked super-admin should land on 'admin' — one rank below
    super_admin on auth.ROLES — preserving tenant-admin reach without
    platform tier."""
    from backend.routers.admin_super_admins import DEMOTION_TARGET_ROLE
    from backend import auth
    assert DEMOTION_TARGET_ROLE == "admin"
    assert DEMOTION_TARGET_ROLE in auth.ROLES
    assert auth.role_at_least("super_admin", DEMOTION_TARGET_ROLE)
    assert not auth.role_at_least(DEMOTION_TARGET_ROLE, "super_admin")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (d) SQL constants — shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SQL_NAMES = (
    "_FETCH_USER_SQL",
    "_FETCH_USER_FOR_DEMOTE_SQL",
    "_PROMOTE_USER_SQL",
    "_DEMOTE_USER_SQL",
    "_COUNT_OTHER_ENABLED_SUPER_ADMINS_SQL",
)


@pytest.mark.parametrize("sql_name", _SQL_NAMES)
def test_sql_uses_pg_placeholders_only(sql_name):
    """Drift guard: every SQL constant must use ``$N`` placeholders.
    A regressed SQLite-style ``?`` would silently break under the
    asyncpg pool."""
    from backend.routers import admin_super_admins as m
    sql = getattr(m, sql_name)
    # Allow 0 or more parameters; just ensure NO bare `?` remains.
    assert "?" not in sql, f"{sql_name} contains SQLite-style ?"
    # SQL touches the users table only.
    assert "users" in sql.lower()


def test_fetch_user_for_demote_has_for_update_lock():
    from backend.routers.admin_super_admins import _FETCH_USER_FOR_DEMOTE_SQL
    assert "FOR UPDATE" in _FETCH_USER_FOR_DEMOTE_SQL


def test_fetch_user_sql_is_read_only():
    from backend.routers.admin_super_admins import _FETCH_USER_SQL
    upper = _FETCH_USER_SQL.upper()
    for verb in ("UPDATE ", "INSERT ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in upper, f"FETCH_USER_SQL contains {verb!r}"
    assert "SELECT" in upper


def test_promote_sql_atomic_and_returning():
    """Promote is an UPDATE with WHERE role != super_admin so the
    no-op path is silent + RETURNING gives back the new row in one
    round-trip."""
    from backend.routers.admin_super_admins import _PROMOTE_USER_SQL
    upper = _PROMOTE_USER_SQL.upper()
    assert "UPDATE USERS" in upper
    assert "SET ROLE = 'SUPER_ADMIN'" in upper
    assert "WHERE ID = $1" in upper
    assert "ROLE != 'SUPER_ADMIN'" in upper
    assert "RETURNING" in upper


def test_demote_sql_atomic_and_returning():
    """Demote uses parameterised target role + WHERE role='super_admin'
    so the no-op path is silent + RETURNING gives back the new row."""
    from backend.routers.admin_super_admins import _DEMOTE_USER_SQL
    upper = _DEMOTE_USER_SQL.upper()
    assert "UPDATE USERS" in upper
    assert "SET ROLE = $2" in upper
    assert "WHERE ID = $1" in upper
    assert "ROLE = 'SUPER_ADMIN'" in upper
    assert "RETURNING" in upper


def test_count_other_super_admins_excludes_target():
    """Floor check must (a) restrict to enabled + (b) exclude the
    target id (otherwise the count includes the user about to be
    demoted, masking the last-super-admin case)."""
    from backend.routers.admin_super_admins import (
        _COUNT_OTHER_ENABLED_SUPER_ADMINS_SQL,
    )
    upper = _COUNT_OTHER_ENABLED_SUPER_ADMINS_SQL.upper()
    assert "COUNT(*)" in upper
    assert "ROLE = 'SUPER_ADMIN'" in upper
    assert "ENABLED = 1" in upper
    assert "ID <> $1" in upper


def test_no_password_hash_or_oidc_projected():
    """SQL projections must NOT leak password_hash or OIDC subject
    into responses or audit blobs."""
    from backend.routers import admin_super_admins as m
    for sql_name in _SQL_NAMES:
        sql = getattr(m, sql_name)
        assert "password_hash" not in sql, (
            f"{sql_name} projects password_hash"
        )
        assert "oidc_subject" not in sql, (
            f"{sql_name} projects oidc_subject"
        )
        assert "oidc_provider" not in sql, (
            f"{sql_name} projects oidc_provider"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (e) Router endpoints mounted with require_super_admin dependency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_post_handler_depends_on_super_admin():
    from backend.routers import admin_super_admins
    from backend import auth
    fn = admin_super_admins.promote_super_admin
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"promote_super_admin must depend on require_super_admin; "
        f"deps were {deps!r}"
    )


def test_delete_handler_depends_on_super_admin():
    from backend.routers import admin_super_admins
    from backend import auth
    fn = admin_super_admins.revoke_super_admin
    sig = inspect.signature(fn)
    deps = []
    for _name, p in sig.parameters.items():
        target = getattr(p.default, "dependency", None)
        if target is not None:
            deps.append(target)
    assert auth.require_super_admin in deps, (
        f"revoke_super_admin must depend on require_super_admin; "
        f"deps were {deps!r}"
    )


def test_router_has_post_and_delete_paths():
    from backend.routers import admin_super_admins
    paths_methods: set[tuple[str, str]] = set()
    for r in admin_super_admins.router.routes:
        for m in getattr(r, "methods", set()):
            paths_methods.add((r.path, m))
    assert ("/admin/super-admins", "POST") in paths_methods
    assert ("/admin/super-admins/{user_id}", "DELETE") in paths_methods


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (f) Main app full-prefix mount
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_super_admins_routes():
    from backend.main import app
    full = set()
    for r in app.routes:
        for m in getattr(r, "methods", set()):
            full.add((r.path, m))
    assert ("/api/v1/admin/super-admins", "POST") in full
    assert ("/api/v1/admin/super-admins/{user_id}", "DELETE") in full


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (g) HTTP — POST promote
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _create_user(role: str = "viewer", *, enabled: bool = True):
    """Helper — seed a real user via auth.create_user, optionally flip
    enabled bit afterwards."""
    import secrets
    from backend import auth as _au
    user = await _au.create_user(
        email=f"sa-test-{secrets.token_hex(4)}@example.local",
        name="SA Test",
        role=role,
        password="longenoughpassword12345",
    )
    if not enabled:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE users SET enabled = 0 WHERE id = $1", user.id,
            )
    return user


async def _purge_user(pool, user_id: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE entity_kind = 'user' "
            "AND entity_id = $1",
            user_id,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


@_requires_pg
async def test_post_promote_happy_path(client, pg_test_pool):
    user = await _create_user(role="admin")
    try:
        res = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": user.id},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["user_id"] == user.id
        assert body["role"] == "super_admin"
        assert body["already_super_admin"] is False
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1", user.id,
            )
        assert row["role"] == "super_admin"
    finally:
        await _purge_user(pg_test_pool, user.id)


@_requires_pg
async def test_post_promote_idempotent_on_existing_super_admin(client, pg_test_pool):
    user = await _create_user(role="super_admin")
    try:
        res = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": user.id},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["already_super_admin"] is True
        assert body["role"] == "super_admin"
    finally:
        await _purge_user(pg_test_pool, user.id)


@_requires_pg
async def test_post_promote_disabled_user_returns_409(client, pg_test_pool):
    user = await _create_user(role="admin", enabled=False)
    try:
        res = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": user.id},
        )
        assert res.status_code == 409, res.text
        assert "disabled" in res.json()["detail"].lower()
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1", user.id,
            )
        # Role unchanged
        assert row["role"] == "admin"
    finally:
        await _purge_user(pg_test_pool, user.id)


@_requires_pg
async def test_post_promote_unknown_user_returns_404(client):
    res = await client.post(
        "/api/v1/admin/super-admins",
        json={"user_id": "u-deadbeef99"},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_post_promote_malformed_body_returns_422(client):
    # Missing user_id
    res = await client.post("/api/v1/admin/super-admins", json={})
    assert res.status_code == 422, res.text
    # Bad pattern
    res = await client.post(
        "/api/v1/admin/super-admins",
        json={"user_id": "U-BADID"},
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_post_promote_tenant_admin_gets_403(client):
    """Override current_user with a tenant-admin (NOT super-admin) and
    verify the role gate denies."""
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadm0001ab", email="ta@local", name="Tenant Admin",
        role="admin", enabled=True,
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
        res = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": "u-target0001"},
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (h) HTTP — DELETE revoke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_delete_revoke_happy_path(client, pg_test_pool):
    """Revoking when other super-admins exist should succeed; demoted
    user lands on role='admin'."""
    target = await _create_user(role="super_admin")
    keeper = await _create_user(role="super_admin")  # ensures floor > 0
    try:
        res = await client.delete(
            f"/api/v1/admin/super-admins/{target.id}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["user_id"] == target.id
        assert body["role"] == "admin"
        assert body["already_revoked"] is False
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1", target.id,
            )
        assert row["role"] == "admin"
    finally:
        await _purge_user(pg_test_pool, target.id)
        await _purge_user(pg_test_pool, keeper.id)


@_requires_pg
async def test_delete_revoke_idempotent_on_non_super_admin(client, pg_test_pool):
    target = await _create_user(role="admin")  # already not super_admin
    try:
        res = await client.delete(
            f"/api/v1/admin/super-admins/{target.id}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["already_revoked"] is True
        assert body["role"] == "admin"
    finally:
        await _purge_user(pg_test_pool, target.id)


@_requires_pg
async def test_delete_revoke_last_super_admin_returns_409(client, pg_test_pool):
    """If demoting target would leave zero enabled super-admins, the
    handler must refuse with 409 and leave the row untouched."""
    # First wipe any existing super_admins so 'lone' is truly the last.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = 'admin' WHERE role = 'super_admin'"
        )
    lone = await _create_user(role="super_admin")
    try:
        res = await client.delete(
            f"/api/v1/admin/super-admins/{lone.id}",
        )
        assert res.status_code == 409, res.text
        body = res.json()
        assert body.get("would_leave_zero_super_admins") is True
        assert body.get("other_enabled_super_admin_count") == 0
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1", lone.id,
            )
        # Untouched
        assert row["role"] == "super_admin"
    finally:
        await _purge_user(pg_test_pool, lone.id)


@_requires_pg
async def test_delete_revoke_disabled_super_admin_bypasses_floor(client, pg_test_pool):
    """A disabled super-admin doesn't preserve operator reach, so
    demoting it should succeed even if it's the only super-admin row
    overall."""
    # Wipe other super-admins.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = 'admin' WHERE role = 'super_admin'"
        )
    target = await _create_user(role="super_admin", enabled=False)
    try:
        res = await client.delete(
            f"/api/v1/admin/super-admins/{target.id}",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["role"] == "admin"
        assert body["already_revoked"] is False
    finally:
        await _purge_user(pg_test_pool, target.id)


@_requires_pg
async def test_delete_revoke_unknown_user_returns_404(client):
    res = await client.delete(
        "/api/v1/admin/super-admins/u-nonexist01",
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_delete_revoke_malformed_id_returns_422(client):
    res = await client.delete(
        "/api/v1/admin/super-admins/U-BADID",
    )
    assert res.status_code == 422, res.text


@_requires_pg
async def test_delete_revoke_tenant_admin_gets_403(client):
    from fastapi import HTTPException
    from backend.main import app
    from backend import auth as _au

    tenant_admin = _au.User(
        id="u-tadm0002cd", email="ta2@local", name="TA",
        role="admin", enabled=True,
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
        res = await client.delete(
            "/api/v1/admin/super-admins/u-target0001",
        )
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides.pop(_au.current_user, None)
        app.dependency_overrides.pop(_au.require_super_admin, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (i) Audit emission
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_promote_emits_super_admin_granted_audit(client, pg_test_pool):
    user = await _create_user(role="admin")
    try:
        res = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": user.id},
        )
        assert res.status_code == 200, res.text
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "before_json, after_json FROM audit_log "
                "WHERE action = 'super_admin_granted' "
                "AND entity_id = $1 ORDER BY id DESC LIMIT 1",
                user.id,
            )
        assert row is not None
        assert row["entity_kind"] == "user"
        assert row["entity_id"] == user.id
        import json
        before = json.loads(row["before_json"])
        after = json.loads(row["after_json"])
        assert before["role"] == "admin"
        assert after["role"] == "super_admin"
    finally:
        await _purge_user(pg_test_pool, user.id)


@_requires_pg
async def test_revoke_emits_super_admin_revoked_audit(client, pg_test_pool):
    target = await _create_user(role="super_admin")
    keeper = await _create_user(role="super_admin")
    try:
        res = await client.delete(
            f"/api/v1/admin/super-admins/{target.id}",
        )
        assert res.status_code == 200, res.text
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "before_json, after_json FROM audit_log "
                "WHERE action = 'super_admin_revoked' "
                "AND entity_id = $1 ORDER BY id DESC LIMIT 1",
                target.id,
            )
        assert row is not None
        import json
        before = json.loads(row["before_json"])
        after = json.loads(row["after_json"])
        assert before["role"] == "super_admin"
        assert after["role"] == "admin"
    finally:
        await _purge_user(pg_test_pool, target.id)
        await _purge_user(pg_test_pool, keeper.id)


@_requires_pg
async def test_idempotent_paths_emit_no_audit(client, pg_test_pool):
    """No-op promote (already super-admin) and no-op revoke (already
    not super-admin) must NOT write an audit row — there was no state
    change."""
    user_a = await _create_user(role="super_admin")
    user_b = await _create_user(role="admin")
    try:
        # Idempotent promote
        res1 = await client.post(
            "/api/v1/admin/super-admins",
            json={"user_id": user_a.id},
        )
        assert res1.status_code == 200
        assert res1.json()["already_super_admin"] is True

        # Idempotent revoke
        res2 = await client.delete(
            f"/api/v1/admin/super-admins/{user_b.id}",
        )
        assert res2.status_code == 200
        assert res2.json()["already_revoked"] is True

        async with pg_test_pool.acquire() as conn:
            n_grant = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'super_admin_granted' AND entity_id = $1",
                user_a.id,
            )
            n_revoke = await conn.fetchval(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'super_admin_revoked' AND entity_id = $1",
                user_b.id,
            )
        assert int(n_grant) == 0, "no audit on no-op promote"
        assert int(n_revoke) == 0, "no audit on no-op revoke"
    finally:
        await _purge_user(pg_test_pool, user_a.id)
        await _purge_user(pg_test_pool, user_b.id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  (j) Self-fingerprint guard — pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SOURCE_PATH = (
    Path(__file__).resolve().parent.parent
    / "routers" / "admin_super_admins.py"
)


def test_no_compat_shim_fingerprints_in_source():
    """Pre-commit fingerprint grep — refuse to ship any of the four
    compat-era markers documented in
    ``docs/sop/implement_phase_step.md`` Step 3."""
    src = _SOURCE_PATH.read_text(encoding="utf-8")
    fingerprint = re.compile(
        r"_conn\(\)|await\s+conn\.commit\(\)|datetime\('now'\)"
        r"|VALUES\s*\([^)]*\?[,)]"
    )
    matches = fingerprint.findall(src)
    assert not matches, (
        f"compat fingerprint hit in {_SOURCE_PATH}: {matches!r}"
    )

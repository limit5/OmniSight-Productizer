"""Y3 (#279) row 3 — tests for DELETE /api/v1/tenants/{tid}/invites/{id}.

Covers the contract from the TODO row literal "撤銷邀請":

  * pending → revoked happy path: atomic UPDATE … RETURNING flips
    the row, response body carries the post-flip status and an
    ``already_revoked=False`` discriminator.
  * Idempotent revoke: a second DELETE on a row that's already
    ``revoked`` returns 200 with ``already_revoked=True`` (no toast
    spam on operator double-click / retry).
  * Terminal-state guard: ``accepted`` and ``expired`` cannot be
    re-revoked (409, distinct error text per branch).
  * Tenant scoping: an invite that belongs to a *different* tenant
    cannot be revoked via the wrong tenant's URL — 404, the row is
    not visible cross-tenant.
  * Defence-in-depth: a ``status='pending' AND expires_at < now`` row
    (housekeeping sweep gap) is still revocable — the admin asked for
    revoke, the difference between "stale-pending" and "live-pending"
    isn't user-visible at click time and refusing would be confusing.
  * RBAC: tenant member / viewer cannot revoke (403), tenant admin
    can (200), platform super_admin can.
  * Validation: malformed tenant id 422, malformed invite id 422,
    unknown tenant 404, well-formed-but-unknown invite 404.
  * Audit: on a successful flip, ``audit_log`` gains a row with
    action=``tenant_invite_revoked``, before/after carry the
    transition without leaking ``token_hash`` or any plaintext.
  * Pre-commit fingerprint grep stays clean (re-runs the row 1 / row 2
    guard so an accidental copy-paste regression in row 3 changes is
    caught here).

Running: requires ``OMNI_TEST_PG_URL`` for the HTTP layer; pure-unit
tests (constants, SQL sentinels, router wiring) run without PG.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="HTTP path depends on asyncpg pool — requires OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: invite-id pattern + module-level constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_invite_id_pattern_matches_post_handler_shape():
    """The DELETE id validator must accept what POST issues
    (``inv-<hex>``) and reject obvious garbage."""
    from backend.routers.tenant_invites import (
        _is_valid_invite_id, INVITE_ID_PATTERN,
    )
    # POST emits ``inv-<token_hex(8)>`` → 16 hex chars.
    assert _is_valid_invite_id("inv-0123456789abcdef") is True
    # Minimum length per pattern: 4 hex chars.
    assert _is_valid_invite_id("inv-abcd") is True
    # Reject empty / non-prefixed / mixed-case / too short.
    assert _is_valid_invite_id("") is False
    assert _is_valid_invite_id("inv-") is False
    assert _is_valid_invite_id("inv-abc") is False  # < 4 chars
    assert _is_valid_invite_id("INV-abcd") is False  # uppercase prefix
    assert _is_valid_invite_id("u-abcd") is False
    assert _is_valid_invite_id("inv-ABCD") is False  # uppercase hex
    # Pattern is anchored — embedded match must not pass.
    assert _is_valid_invite_id("xxinv-abcdyy") is False
    # Pattern itself must be anchored on both ends.
    assert INVITE_ID_PATTERN.startswith("^")
    assert INVITE_ID_PATTERN.endswith("$")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL sentinels — atomic check-and-flip + scoping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_sql_is_pending_only_atomic_check_and_flip():
    """The revoke UPDATE must scope to ``status='pending'`` so a
    concurrent accept can't be silently overwritten."""
    from backend.routers.tenant_invites import _REVOKE_INVITE_SQL
    sql = _REVOKE_INVITE_SQL
    assert "UPDATE tenant_invites" in sql
    assert "SET status = 'revoked'" in sql
    assert "status = 'pending'" in sql, (
        "revoke must only flip pending rows — guards concurrent accept"
    )
    assert "RETURNING" in sql, (
        "revoke must use UPDATE … RETURNING for atomic check-and-flip "
        "rather than SELECT-then-UPDATE TOCTOU"
    )


def test_revoke_sql_is_tenant_scoped():
    """The UPDATE WHERE must include tenant_id so an attacker can't
    revoke another tenant's invite by guessing the id."""
    from backend.routers.tenant_invites import _REVOKE_INVITE_SQL
    sql = _REVOKE_INVITE_SQL
    assert "tenant_id = $2" in sql or "tenant_id=$2" in sql.replace(" ", "")
    assert "id = $1" in sql or "id=$1" in sql.replace(" ", "")


def test_revoke_sql_does_not_project_token_hash():
    """token_hash MUST NEVER reach the response."""
    from backend.routers.tenant_invites import _REVOKE_INVITE_SQL
    assert "token_hash" not in _REVOKE_INVITE_SQL


def test_fetch_for_revoke_sql_is_select_only_and_no_token_hash():
    """The disambiguation read is a strict SELECT; it must not project
    token_hash or contain mutating verbs."""
    from backend.routers.tenant_invites import _FETCH_INVITE_FOR_REVOKE_SQL
    sql = _FETCH_INVITE_FOR_REVOKE_SQL.upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ",
                 "TRUNCATE "):
        assert verb not in sql, f"forbidden verb {verb!r} in fetch sql"
    assert "TOKEN_HASH" not in sql


@pytest.mark.parametrize("attr", [
    "_REVOKE_INVITE_SQL",
    "_FETCH_INVITE_FOR_REVOKE_SQL",
])
def test_revoke_sql_uses_pg_placeholders_only(attr):
    """No SQLite-style '?' placeholders, no compat-era idioms."""
    from backend.routers import tenant_invites
    sql = getattr(tenant_invites, attr)
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint_re.search(sql), (
        f"{attr} contains compat-era fingerprint"
    )
    assert "$1" in sql, f"{attr} missing PG-style $1 placeholder"
    for ch in (" ?,", " ?)", "= ?"):
        assert ch not in sql, f"{attr} contains SQLite '?' placeholder"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: router wiring (no PG required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_delete_endpoint():
    from backend.routers.tenant_invites import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (
        ("DELETE",), "/tenants/{tenant_id}/invites/{invite_id}",
    ) in paths


def test_main_app_mounts_delete_endpoint_at_full_prefix():
    """End-to-end: backend.main exposes the endpoint at
    ``/api/v1/tenants/{tenant_id}/invites/{invite_id}`` DELETE — guards
    against a deployment that forgets to ``include_router`` the new
    module."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (
        ("DELETE",), "/api/v1/tenants/{tenant_id}/invites/{invite_id}",
    ) in paths


def test_delete_handler_uses_current_user_dependency():
    """The handler must depend on auth.current_user (RBAC happens
    inside the handler, not at the dependency layer, because Y3
    needs the per-tenant membership lookup)."""
    import inspect
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_invites
    from backend import auth as _au

    handler = tenant_invites.revoke_invite
    deps = [
        v.default for v in (
            inspect.signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    assert any(
        getattr(d, "dependency", None) is _au.current_user for d in deps
    ), "DELETE /tenants/{tid}/invites/{id} must depend on auth.current_user"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — fixtures (mirror the row 2 list-tests fixtures)
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
            "DELETE FROM tenant_invites WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _seed_invite_row(
    pool,
    *,
    tid: str,
    invite_id: str,
    email: str = "x@example.com",
    role: str = "member",
    status: str = "pending",
    expires_at: str | None = None,
    created_at: str | None = None,
) -> None:
    if expires_at is None:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=7)
        ).strftime("%Y-%m-%d %H:%M:%S")
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    token_hash = hashlib.sha256(
        f"seed-{invite_id}-{secrets.token_hex(8)}".encode("ascii"),
    ).hexdigest()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, role, token_hash, expires_at, "
            " status, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            invite_id, tid, email, role, token_hash, expires_at,
            status, created_at,
        )


async def _read_invite_status(pool, invite_id: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM tenant_invites WHERE id = $1", invite_id,
        )
    return row["status"] if row else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_pending_flips_to_revoked(client, pg_test_pool):
    tid = "t-y3-revoke-happy"
    iid = "inv-revoke-happy01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid,
            email="revoke@example.com", role="admin", status="pending",
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["invite_id"] == iid
        assert body["tenant_id"] == tid
        assert body["status"] == "revoked"
        assert body["already_revoked"] is False
        assert body["email"] == "revoke@example.com"
        assert body["role"] == "admin"
        # Persisted state must match the response.
        assert await _read_invite_status(pg_test_pool, iid) == "revoked"
        # No token_hash leaks.
        assert "token_hash" not in body
        for v in body.values():
            if isinstance(v, str):
                assert not re.fullmatch(r"[0-9a-f]{64}", v)
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_revoke_already_revoked_is_idempotent(client, pg_test_pool):
    """Second DELETE on the same id returns 200 with
    already_revoked=True so a UI can suppress duplicate toasts."""
    tid = "t-y3-revoke-idem"
    iid = "inv-revoke-idem01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid, status="revoked",
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "revoked"
        assert body["already_revoked"] is True
        # Persisted state remains revoked (UPDATE was a no-op because
        # the WHERE status='pending' did not match).
        assert await _read_invite_status(pg_test_pool, iid) == "revoked"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_revoke_stale_pending_still_succeeds(client, pg_test_pool):
    """A row with persisted status='pending' but expires_at in the
    past is still flippable to revoked — the admin asked for it and
    the housekeeping sweep gap should not change UX."""
    tid = "t-y3-revoke-stale"
    iid = "inv-revoke-stale01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        past = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid,
            status="pending", expires_at=past,
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "revoked"
        assert await _read_invite_status(pg_test_pool, iid) == "revoked"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — terminal-state refusals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_accepted_invite_returns_409(client, pg_test_pool):
    """Already-accepted invite cannot be revoked — the membership row
    is materialised; admin must use membership management instead."""
    tid = "t-y3-revoke-accepted"
    iid = "inv-revoke-accepted1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid, status="accepted",
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["current_status"] == "accepted"
        assert "accepted" in body["detail"].lower()
        assert "membership" in body["detail"].lower()
        # Persisted state untouched.
        assert await _read_invite_status(pg_test_pool, iid) == "accepted"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_revoke_expired_invite_returns_409(client, pg_test_pool):
    """Already-expired invite cannot be revoked — distinct terminal
    state; admin should see "expired" rather than have the system
    silently transmute it into "revoked"."""
    tid = "t-y3-revoke-expired"
    iid = "inv-revoke-expired1"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid, status="expired",
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["current_status"] == "expired"
        assert "expired" in body["detail"].lower()
        assert await _read_invite_status(pg_test_pool, iid) == "expired"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — tenant scoping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_invite_from_wrong_tenant_returns_404(
    client, pg_test_pool,
):
    """Invite that belongs to tenant A cannot be revoked via tenant
    B's URL — the row must not be visible cross-tenant."""
    tid_a = "t-y3-revoke-scope-a"
    tid_b = "t-y3-revoke-scope-b"
    iid = "inv-revoke-scope01"
    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        await _seed_invite_row(
            pg_test_pool, tid=tid_a, invite_id=iid, status="pending",
        )

        # Try to revoke the invite via tenant B's URL.
        res = await client.delete(
            f"/api/v1/tenants/{tid_b}/invites/{iid}",
        )
        assert res.status_code == 404, res.text
        # Original tenant's row must still be pending.
        assert await _read_invite_status(pg_test_pool, iid) == "pending"
    finally:
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — validation + not-found
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_malformed_tenant_id_returns_422(client):
    res = await client.delete("/api/v1/tenants/T-Bad/invites/inv-aaaa")
    assert res.status_code == 422
    assert "invalid tenant id" in res.json()["detail"]


@_requires_pg
async def test_revoke_malformed_invite_id_returns_422(client, pg_test_pool):
    tid = "t-y3-revoke-bad-iid"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.delete(
            f"/api/v1/tenants/{tid}/invites/INV-BAD",
        )
        assert res.status_code == 422
        assert "invalid invite id" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_revoke_unknown_tenant_returns_404(client):
    res = await client.delete(
        "/api/v1/tenants/t-does-not-exist/invites/inv-aaaa",
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_revoke_unknown_invite_returns_404(client, pg_test_pool):
    """Tenant exists, invite id well-formed but never issued — 404."""
    tid = "t-y3-revoke-no-invite"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.delete(
            f"/api/v1/tenants/{tid}/invites/inv-deadbeef00",
        )
        assert res.status_code == 404, res.text
        assert "invite not found" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_non_admin_member_gets_403(client, pg_test_pool):
    """A user with membership.role='member' on the target tenant must
    NOT revoke invites. RBAC happens before the existence probe so a
    guess-the-id scan can't enumerate which invites exist."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-revoke-rbac-member"
    uid = "u-y3-revoke-rbac-member"
    iid = "inv-revoke-rbac01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, $3, 'viewer', '', 1, $4) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "memberonly@example.com", "Member Only", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'member', 'active') "
                "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                uid, tid,
            )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid, status="pending",
        )

        member = _au.User(
            id=uid, email="memberonly@example.com", name="Member Only",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return member

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.delete(
                f"/api/v1/tenants/{tid}/invites/{iid}",
            )
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
            # Persisted state must remain pending — RBAC reject must
            # not silently flip the row.
            assert await _read_invite_status(pg_test_pool, iid) == "pending"
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_revoke_tenant_admin_member_allowed(client, pg_test_pool):
    """Membership role='admin' on the target tenant succeeds even
    when the *account* role is only 'viewer'."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-revoke-rbac-admin"
    uid = "u-y3-revoke-rbac-admin"
    iid = "inv-revoke-rbac02"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, $3, 'viewer', '', 1, $4) "
                "ON CONFLICT (id) DO NOTHING",
                uid, "tadmin@example.com", "T Admin", tid,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'admin', 'active') "
                "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                uid, tid,
            )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid, status="pending",
        )

        tadmin = _au.User(
            id=uid, email="tadmin@example.com", name="T Admin",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return tadmin

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.delete(
                f"/api/v1/tenants/{tid}/invites/{iid}",
            )
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "revoked"
            assert await _read_invite_status(pg_test_pool, iid) == "revoked"
        finally:
            app.dependency_overrides.pop(_au.current_user, None)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit chain — successful revoke emits a row, no token leak
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoke_emits_audit_row_without_token_hash(
    client, pg_test_pool,
):
    """Successful revoke must drop a tenant_invite_revoked row in
    audit_log; before/after must NOT contain token_hash or any
    plaintext-shaped value."""
    tid = "t-y3-revoke-audit"
    iid = "inv-revoke-audit01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id=iid,
            email="audit@example.com", role="admin", status="pending",
        )

        res = await client.delete(f"/api/v1/tenants/{tid}/invites/{iid}")
        assert res.status_code == 200

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, before_json, "
                "after_json FROM audit_log WHERE entity_id = $1 "
                "AND action = 'tenant_invite_revoked' "
                "ORDER BY id DESC LIMIT 1",
                iid,
            )
        assert row is not None, "audit row missing for revoke"
        assert row["action"] == "tenant_invite_revoked"
        assert row["entity_kind"] == "tenant_invite"
        assert row["entity_id"] == iid
        # Before / after must not contain token_hash key or value.
        for blob in (row["before_json"] or "", row["after_json"] or ""):
            assert "token_hash" not in blob
            # Sha256 hex strings are 64 chars of [0-9a-f]; flag any
            # accidental leak.
            assert not re.search(r"[0-9a-f]{64}", blob), (
                f"sha256-shaped string in audit blob: {blob!r}"
            )
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite
    fingerprints (``_conn()`` / ``await conn.commit()`` /
    ``datetime('now')`` / ``VALUES ... ?, ?`` placeholder). Re-runs
    the guard from rows 1 and 2 to defend against accidental
    copy-paste regressions during row 3 changes."""
    src = pathlib.Path(
        "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat-era fingerprint(s) hit: {hits}"

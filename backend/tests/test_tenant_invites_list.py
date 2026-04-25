"""Y3 (#279) row 2 — tests for GET /api/v1/tenants/{tid}/invites.

Covers the contract from the TODO row literal:

  * Default ``status=pending`` returns only live pending invites
    (``status='pending' AND expires_at > now``).
  * ``status=accepted/revoked/expired`` returns rows whose persisted
    status equals the filter, verbatim.
  * ``status=all`` returns every invite for the tenant.
  * Tenant-admin-or-above gating (membership role ∈ {owner, admin}
    OR platform super_admin) — non-admin members get 403.
  * 422 on malformed tenant id, 422 on unknown status filter,
    404 on missing tenant.
  * Response shape: ``{tenant_id, status_filter, count, invites}``;
    each invite contains ``invite_id, email, role, status,
    invited_by, created_at, expires_at`` and **NEVER** ``token_hash``
    or any plaintext.
  * Newest-first ordering on ``created_at DESC, id DESC``.
  * Limit clamps (default 100, max 500).
  * Pre-commit fingerprint grep stays clean.

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
#  Pure-unit: module-level constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_listable_invite_statuses_match_db_check_plus_all_sentinel():
    """Five values: the four CHECK-constraint members + 'all' sentinel.
    Drift here would let a status filter through that the DB rejects."""
    from backend.routers import tenant_invites
    assert tenant_invites.LISTABLE_INVITE_STATUSES == (
        "pending", "accepted", "revoked", "expired", "all",
    )


def test_invites_list_default_limit_is_conservative():
    """Default ≤ max; max ≤ 500 keeps single-call payload bounded."""
    from backend.routers import tenant_invites
    assert tenant_invites.INVITES_LIST_DEFAULT_LIMIT == 100
    assert tenant_invites.INVITES_LIST_MAX_LIMIT == 500
    assert (
        tenant_invites.INVITES_LIST_DEFAULT_LIMIT
        <= tenant_invites.INVITES_LIST_MAX_LIMIT
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL sentinels — read-only, $N placeholders, no token_hash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("attr", [
    "_LIST_INVITES_PENDING_SQL",
    "_LIST_INVITES_BY_STATUS_SQL",
    "_LIST_INVITES_ALL_SQL",
])
def test_list_sql_is_select_only(attr):
    """Every list query is read-only — no INSERT / UPDATE / DELETE
    keywords leak in."""
    from backend.routers import tenant_invites
    sql = getattr(tenant_invites, attr).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ",
                 "TRUNCATE "):
        assert verb not in sql, f"{attr} contains forbidden verb {verb!r}"


@pytest.mark.parametrize("attr", [
    "_LIST_INVITES_PENDING_SQL",
    "_LIST_INVITES_BY_STATUS_SQL",
    "_LIST_INVITES_ALL_SQL",
])
def test_list_sql_uses_pg_placeholders_only(attr):
    """No SQLite-style '?' placeholders, no compat-era idioms."""
    from backend.routers import tenant_invites
    sql = getattr(tenant_invites, attr)
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint_re.search(sql), (
        f"{attr} contains compat-era fingerprint"
    )
    # All placeholders must be PG-style $N.
    assert "$1" in sql
    # Direct '?' parameter style is banned.
    for ch in (" ?,", " ?)", "= ?"):
        assert ch not in sql, f"{attr} contains SQLite '?' placeholder"


@pytest.mark.parametrize("attr", [
    "_LIST_INVITES_PENDING_SQL",
    "_LIST_INVITES_BY_STATUS_SQL",
    "_LIST_INVITES_ALL_SQL",
])
def test_list_sql_does_not_project_token_hash(attr):
    """token_hash MUST NEVER reach the response: no operator value
    + broadens leak surface for any future logging accident."""
    from backend.routers import tenant_invites
    sql = getattr(tenant_invites, attr)
    assert "token_hash" not in sql, (
        f"{attr} projects token_hash — strip it before merging"
    )


def test_pending_sql_filters_by_expires_at():
    """The pending variant must include an ``expires_at >`` filter so
    the housekeeping sweep gap doesn't surface dead rows as 'live'."""
    from backend.routers.tenant_invites import _LIST_INVITES_PENDING_SQL
    assert "expires_at" in _LIST_INVITES_PENDING_SQL
    assert "status = 'pending'" in _LIST_INVITES_PENDING_SQL


def test_all_sql_omits_status_filter():
    """The 'all' variant must NOT filter on status — that's the
    whole point of the sentinel."""
    from backend.routers.tenant_invites import _LIST_INVITES_ALL_SQL
    assert "status" not in _LIST_INVITES_ALL_SQL.split("ORDER BY")[0].upper().split("WHERE")[1]


def test_list_sql_orders_newest_first():
    """ORDER BY created_at DESC, id DESC — stable ordering even when
    two invites share a created_at second."""
    from backend.routers import tenant_invites
    for attr in (
        "_LIST_INVITES_PENDING_SQL",
        "_LIST_INVITES_BY_STATUS_SQL",
        "_LIST_INVITES_ALL_SQL",
    ):
        sql = getattr(tenant_invites, attr)
        assert "ORDER BY created_at DESC, id DESC" in sql, attr


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: router wiring (no PG required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_get_endpoint():
    from backend.routers.tenant_invites import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("GET",), "/tenants/{tenant_id}/invites") in paths


def test_main_app_mounts_get_endpoint_at_full_prefix():
    """End-to-end: backend.main exposes the endpoint at
    ``/api/v1/tenants/{tenant_id}/invites`` GET — guards against a
    deployment that forgets to ``include_router`` the new module."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (("GET",), "/api/v1/tenants/{tenant_id}/invites") in paths


def test_get_handler_uses_current_user_dependency():
    """The handler must depend on auth.current_user (RBAC happens
    inside the handler, not at the dependency layer, because Y3
    needs the per-tenant membership lookup)."""
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_invites
    from backend import auth as _au

    handler = tenant_invites.list_invites
    # Walk default values for the auth dependency marker.
    deps = [
        v.default for v in handler.__wrapped__.__defaults__ or ()  # type: ignore
    ] if hasattr(handler, "__wrapped__") else [
        v.default for v in (
            __import__("inspect").signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    # current_user is the dependency on the actor parameter.
    assert any(
        getattr(d, "dependency", None) is _au.current_user for d in deps
    ), "GET /tenants/{tid}/invites must depend on auth.current_user"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — fixtures
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
    email: str,
    role: str = "member",
    status: str = "pending",
    expires_at: str | None = None,
    created_at: str | None = None,
) -> None:
    """Insert a synthetic invite row with explicit status / expiry so
    we can exercise filter branches without depending on the real
    POST handler timing."""
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_invites_default_pending_empty_returns_200(client, pg_test_pool):
    tid = "t-y3-list-empty"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(f"/api/v1/tenants/{tid}/invites")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["tenant_id"] == tid
        assert body["status_filter"] == "pending"
        assert body["count"] == 0
        assert body["invites"] == []
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_default_lists_only_pending(client, pg_test_pool):
    """Seed pending + accepted + revoked + expired; default filter
    returns only pending live rows."""
    tid = "t-y3-list-pending-only"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-pending-1",
            email="alice@example.com", role="admin", status="pending",
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-accepted-1",
            email="bob@example.com", status="accepted",
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-revoked-1",
            email="carol@example.com", status="revoked",
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-expired-1",
            email="dave@example.com", status="expired",
        )

        res = await client.get(f"/api/v1/tenants/{tid}/invites")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 1
        assert len(body["invites"]) == 1
        only = body["invites"][0]
        assert only["invite_id"] == "inv-pending-1"
        assert only["email"] == "alice@example.com"
        assert only["role"] == "admin"
        assert only["status"] == "pending"
        # Schema: required keys, NO token_hash, NO plaintext.
        assert set(only) == {
            "invite_id", "email", "role", "status",
            "invited_by", "created_at", "expires_at",
        }
        assert "token_hash" not in only
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_pending_excludes_expired_pending_rows(
    client, pg_test_pool,
):
    """A row with persisted status='pending' but expires_at in the
    past must NOT appear under ?status=pending — defence in depth
    against the housekeeping sweep gap."""
    tid = "t-y3-list-expired-pending"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Live pending row.
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-live",
            email="live@example.com", status="pending",
        )
        # 'pending' but expired (sweep hasn't flipped status yet).
        past = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-stale-pending",
            email="stale@example.com", status="pending",
            expires_at=past,
        )

        res = await client.get(f"/api/v1/tenants/{tid}/invites")
        assert res.status_code == 200, res.text
        ids = [r["invite_id"] for r in res.json()["invites"]]
        assert ids == ["inv-live"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_status_accepted_returns_only_accepted(
    client, pg_test_pool,
):
    tid = "t-y3-list-accepted"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-accepted-A",
            email="a@example.com", status="accepted",
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-pending-A",
            email="p@example.com", status="pending",
        )

        res = await client.get(
            f"/api/v1/tenants/{tid}/invites?status=accepted",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status_filter"] == "accepted"
        ids = [r["invite_id"] for r in body["invites"]]
        assert ids == ["inv-accepted-A"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_status_all_returns_every_row(client, pg_test_pool):
    tid = "t-y3-list-all"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-all-pending",
            email="p@example.com", status="pending",
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-all-revoked",
            email="r@example.com", status="revoked",
        )

        res = await client.get(f"/api/v1/tenants/{tid}/invites?status=all")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status_filter"] == "all"
        assert body["count"] == 2
        ids = sorted(r["invite_id"] for r in body["invites"])
        assert ids == ["inv-all-pending", "inv-all-revoked"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_orders_newest_first(client, pg_test_pool):
    tid = "t-y3-list-order"
    try:
        await _seed_tenant(pg_test_pool, tid)
        oldest = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).strftime("%Y-%m-%d %H:%M:%S")
        middle = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        newest = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Insert in scrambled order to make sure the ORDER BY does the
        # work, not insertion order.
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-order-mid",
            email="m@example.com", status="pending", created_at=middle,
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-order-old",
            email="o@example.com", status="pending", created_at=oldest,
        )
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-order-new",
            email="n@example.com", status="pending", created_at=newest,
        )

        res = await client.get(f"/api/v1/tenants/{tid}/invites")
        assert res.status_code == 200, res.text
        ids = [r["invite_id"] for r in res.json()["invites"]]
        assert ids == ["inv-order-new", "inv-order-mid", "inv-order-old"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_response_omits_token_hash(client, pg_test_pool):
    """Even though token_hash exists on the row, it must not appear in
    the projected response — the test is paranoid because a future
    refactor that copy-pastes the SELECT * shape would silently leak
    the hash."""
    tid = "t-y3-list-no-hash"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_row(
            pg_test_pool, tid=tid, invite_id="inv-no-hash",
            email="nh@example.com", status="pending",
        )

        res = await client.get(f"/api/v1/tenants/{tid}/invites")
        assert res.status_code == 200, res.text
        for inv in res.json()["invites"]:
            assert "token_hash" not in inv
            # Also nothing that LOOKS like a hash hex-string field.
            for v in inv.values():
                if isinstance(v, str):
                    assert not re.fullmatch(r"[0-9a-f]{64}", v), (
                        f"raw sha256 hex appears in field of {inv}"
                    )
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — limit clamps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_invites_respects_limit_param(client, pg_test_pool):
    tid = "t-y3-list-limit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        for i in range(5):
            ts = (
                datetime.now(timezone.utc) - timedelta(minutes=i)
            ).strftime("%Y-%m-%d %H:%M:%S")
            await _seed_invite_row(
                pg_test_pool, tid=tid, invite_id=f"inv-lim-{i}",
                email=f"u{i}@example.com", status="pending",
                created_at=ts,
            )

        res = await client.get(
            f"/api/v1/tenants/{tid}/invites?limit=2",
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 2
        # Newest two by created_at.
        ids = [r["invite_id"] for r in body["invites"]]
        assert ids == ["inv-lim-0", "inv-lim-1"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_rejects_oversized_limit(client, pg_test_pool):
    tid = "t-y3-list-limit-cap"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/invites?limit=999999",
        )
        assert res.status_code == 422, res.text
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — validation errors (id pattern, unknown status)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_invites_malformed_tenant_id_returns_422(client):
    res = await client.get("/api/v1/tenants/T-Bad-Id/invites")
    assert res.status_code == 422
    assert "invalid tenant id" in res.json()["detail"]


@_requires_pg
async def test_get_invites_unknown_status_returns_422(client, pg_test_pool):
    tid = "t-y3-list-bad-status"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.get(
            f"/api/v1/tenants/{tid}/invites?status=approved",
        )
        assert res.status_code == 422, res.text
        assert "invalid status filter" in res.json()["detail"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_get_invites_unknown_tenant_returns_404(client):
    res = await client.get("/api/v1/tenants/t-does-not-exist/invites")
    assert res.status_code == 404, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: member / viewer cannot enumerate invites
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_get_invites_non_admin_member_gets_403(client, pg_test_pool):
    """A user with membership.role='member' on the target tenant must
    NOT enumerate invites — the email addresses leak the tenant
    admin's recruitment pipeline."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-list-rbac-member"
    uid = "u-y3-list-rbac-member"
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

        member = _au.User(
            id=uid, email="memberonly@example.com", name="Member Only",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return member

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/invites")
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
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
async def test_get_invites_tenant_admin_member_allowed(client, pg_test_pool):
    """Membership role='admin' on the target tenant — should succeed
    even when the *account* role is only 'viewer'."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-list-rbac-admin"
    uid = "u-y3-list-rbac-admin"
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
            pg_test_pool, tid=tid, invite_id="inv-admin-can-see",
            email="x@example.com", status="pending",
        )

        tadmin = _au.User(
            id=uid, email="tadmin@example.com", name="T Admin",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return tadmin

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.get(f"/api/v1/tenants/{tid}/invites")
            assert res.status_code == 200, res.text
            ids = [r["invite_id"] for r in res.json()["invites"]]
            assert "inv-admin-can-see" in ids
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
#  Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite
    fingerprints (``_conn()`` / ``await conn.commit()`` /
    ``datetime('now')`` / ``VALUES ... ?, ?`` placeholder). Re-runs
    the guard from row 1 to defend against accidental copy-paste
    regressions during row 2 changes."""
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

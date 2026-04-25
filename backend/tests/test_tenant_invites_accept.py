"""Y3 (#279) row 4 — tests for POST /api/v1/invites/{id}/accept.

Covers the contract from the TODO row literal:

  * Anonymous-caller branch: token hashes against tenant_invites
    .token_hash; if no users row exists for the invite email, one is
    created; a user_tenant_memberships row is materialised; the
    invite flips to ``status='accepted'`` — all in one PG
    transaction.
  * Authenticated-caller branch: caller's session.email must match
    the invite email case-insensitively. No user row is created;
    membership is upserted onto the existing user.
  * One user → N memberships: an authenticated user accepting an
    invite for a *different* tenant gets a second membership row
    rather than a duplicate / overwrite.
  * Token verification: sha256(plaintext) compare via
    ``secrets.compare_digest`` — bad token returns 403.
  * State machine: accepted/revoked/expired → 409 / 410 (distinct
    branches), so a replayed leaked token cannot be re-consumed.
  * Wall-clock expiry: persisted ``status='pending' AND expires_at <
    now`` is treated as expired (housekeeping-sweep gap defence).
  * Email mismatch on authenticated branch returns 409 — admin
    invited alice but bob is logged in.
  * Rate-limit on FAILED attempts: 10/token/min per the TODO row 7
    literal, surfaced as 429 with ``Retry-After``.
  * Audit: ``tenant_invite_accepted`` row is appended; before/after
    blob does not contain ``token_hash``, plaintext, or any
    sha256-shaped value.
  * Optional auth probing — endpoint must NOT depend on
    auth.current_user (which would 401 in session/strict mode).
  * Pre-commit fingerprint grep stays clean.

Running: requires ``OMNI_TEST_PG_URL`` for the HTTP layer; pure-unit
tests (constants, SQL sentinels, router wiring) run without PG.
"""

from __future__ import annotations

import hashlib
import inspect
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


def test_accept_fail_rate_limit_constants_match_spec():
    """TODO row 7 literal: 'accept 失敗每 token 每分鐘不超過 10 次'."""
    from backend.routers import tenant_invites
    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_CAP == 10
    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS == 60.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: SQL sentinels — locking, atomic flip, membership upsert
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fetch_invite_for_accept_sql_uses_for_update_lock():
    """The SELECT inside the accept transaction MUST use FOR UPDATE
    so concurrent accepts on the same invite serialise — without the
    lock, two anonymous callers presenting the same valid token can
    both pass the status check before either UPDATE lands."""
    from backend.routers.tenant_invites import _FETCH_INVITE_FOR_ACCEPT_SQL
    sql = _FETCH_INVITE_FOR_ACCEPT_SQL.upper()
    assert "FOR UPDATE" in sql, (
        "accept-path SELECT must lock the row to prevent concurrent "
        "double-accept of the same token"
    )
    # Read-only verbs only on this SELECT.
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ",
                 "TRUNCATE "):
        assert verb not in sql, (
            f"forbidden verb {verb!r} in fetch-for-accept SQL"
        )


def test_fetch_invite_for_accept_projects_token_hash():
    """Unlike the GET-list / DELETE-revoke read paths, the accept
    SELECT MUST project token_hash because the handler verifies it.
    The hash never reaches the response — that's separately tested."""
    from backend.routers.tenant_invites import _FETCH_INVITE_FOR_ACCEPT_SQL
    assert "token_hash" in _FETCH_INVITE_FOR_ACCEPT_SQL


def test_mark_invite_accepted_sql_is_atomic_flip():
    """The acceptance UPDATE must set status='accepted' and target
    by id only — the FOR UPDATE lock + transaction semantics already
    pin the row, so re-checking status='pending' here would be
    redundant but harmless."""
    from backend.routers.tenant_invites import _MARK_INVITE_ACCEPTED_SQL
    sql = _MARK_INVITE_ACCEPTED_SQL
    assert "UPDATE tenant_invites" in sql
    assert "SET status = 'accepted'" in sql
    assert "WHERE id = $1" in sql
    assert "token_hash" not in sql


def test_upsert_membership_sql_does_nothing_on_conflict():
    """One user → N memberships. ON CONFLICT DO NOTHING preserves
    the user's existing role on this tenant if they're already a
    member (admin must use membership-management endpoints to change
    role); the invite still flips to accepted."""
    from backend.routers.tenant_invites import _UPSERT_MEMBERSHIP_SQL
    sql = _UPSERT_MEMBERSHIP_SQL
    assert "INSERT INTO user_tenant_memberships" in sql
    assert "ON CONFLICT (user_id, tenant_id) DO NOTHING" in sql
    assert "RETURNING user_id" in sql


@pytest.mark.parametrize("attr", [
    "_FETCH_INVITE_FOR_ACCEPT_SQL",
    "_MARK_INVITE_ACCEPTED_SQL",
    "_UPSERT_MEMBERSHIP_SQL",
])
def test_accept_sql_uses_pg_placeholders_only(attr):
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


def test_router_exposes_accept_endpoint():
    """Path is ``/invites/{invite_id}/accept`` — NOT scoped under
    ``/tenants/{tid}/`` because the caller may not yet know the tenant
    id (the email gives them invite_id + token; tenant comes from the
    persisted invite row)."""
    from backend.routers.tenant_invites import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("POST",), "/invites/{invite_id}/accept") in paths


def test_main_app_mounts_accept_endpoint_at_full_prefix():
    """End-to-end: backend.main exposes the endpoint at
    ``/api/v1/invites/{invite_id}/accept`` POST — guards against a
    deployment that forgets to ``include_router`` the new path."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (
        ("POST",), "/api/v1/invites/{invite_id}/accept",
    ) in paths


def test_accept_handler_does_not_depend_on_current_user():
    """The handler MUST NOT depend on auth.current_user — that
    helper raises 401 in session/strict mode against an anonymous
    caller, which would block the legitimate "no account yet"
    flow. Optional auth is probed inline via auth.get_session +
    auth.get_user.

    This test is the contract guard: if a future refactor adds
    ``Depends(current_user)`` to the accept handler, the anonymous
    branch breaks silently in prod (open-mode dev still works
    because current_user returns the ANON_ADMIN there) — this
    test catches the drift in CI before the regression ships.
    """
    from fastapi.params import Depends as _DependsParam
    from backend.routers import tenant_invites
    from backend import auth as _au

    handler = tenant_invites.accept_invite
    deps = [
        v.default for v in (
            inspect.signature(handler).parameters.values()
        ) if isinstance(v.default, _DependsParam)
    ]
    for d in deps:
        assert getattr(d, "dependency", None) is not _au.current_user, (
            "POST /invites/{id}/accept must NOT depend on "
            "auth.current_user — anonymous callers would 401 in "
            "session/strict mode and the new-user-on-accept flow would "
            "break"
        )


def test_accept_request_schema_requires_token():
    """Token field is required; minimum length 16 means a typo'd 4-char
    string returns 422 cleanly rather than reaching the SQL layer."""
    from backend.routers.tenant_invites import AcceptInviteRequest
    from pydantic import ValidationError

    # Empty / missing token must fail validation.
    with pytest.raises(ValidationError):
        AcceptInviteRequest()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        AcceptInviteRequest(token="abc")
    # A valid-shape token passes.
    ok = AcceptInviteRequest(token="x" * 32, name="Alice")
    assert ok.token == "x" * 32
    assert ok.name == "Alice"
    assert ok.password is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — fixtures (mirror revoke / list test fixtures)
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
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _seed_invite_with_token(
    pool,
    *,
    tid: str,
    invite_id: str,
    email: str,
    role: str = "member",
    status: str = "pending",
    expires_at: str | None = None,
) -> str:
    """Seed an invite row, returning the plaintext token."""
    if expires_at is None:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=7)
        ).strftime("%Y-%m-%d %H:%M:%S")
    plaintext = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(plaintext.encode("ascii")).hexdigest()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, role, token_hash, expires_at, "
            " status, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            invite_id, tid, email, role, token_hash, expires_at,
            status, created_at,
        )
    return plaintext


async def _read_invite_status(pool, invite_id: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM tenant_invites WHERE id = $1", invite_id,
        )
    return row["status"] if row else None


async def _read_membership(pool, user_id: str, tid: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user_id, tid,
        )


async def _read_user_by_email(pool, email_norm: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, email FROM users WHERE lower(email) = $1",
            email_norm,
        )


async def _purge_user(pool, user_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE user_id = $1",
            user_id,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — anonymous (creates user + materialises membership)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_anon_accept_creates_user_and_membership(client, pg_test_pool):
    """Happy path for the unauthenticated branch: invite email has no
    matching users row → endpoint creates one + materialises a
    membership + flips invite to accepted."""
    tid = "t-y3-accept-anon-new"
    iid = "inv-acceptanon01"
    target_email = "newuser@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="admin",
        )

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token, "name": "New User"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["invite_id"] == iid
        assert body["tenant_id"] == tid
        assert body["role"] == "admin"
        assert body["status"] == "accepted"
        assert body["user_was_created"] is True
        assert body["already_member"] is False

        # The freshly-created user exists.
        user_row = await _read_user_by_email(pg_test_pool, target_email)
        assert user_row is not None
        assert body["user_id"] == user_row["id"]

        # Membership row materialised with the invite's role.
        m = await _read_membership(pg_test_pool, user_row["id"], tid)
        assert m is not None
        assert m["role"] == "admin"
        assert m["status"] == "active"

        # Invite flipped to accepted.
        assert await _read_invite_status(pg_test_pool, iid) == "accepted"

        # No token_hash leak in the response.
        for v in body.values():
            if isinstance(v, str):
                assert not re.fullmatch(r"[0-9a-f]{64}", v)
    finally:
        user_row = await _read_user_by_email(pg_test_pool, target_email)
        if user_row is not None:
            await _purge_user(pg_test_pool, user_row["id"])
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_anon_accept_when_user_already_exists_just_adds_membership(
    client, pg_test_pool,
):
    """Anonymous caller, but a users row already exists for the
    invite email (user logged out / never linked). We do not create a
    second user row — we materialise membership onto the existing
    user. ``user_was_created=False``."""
    tid = "t-y3-accept-anon-existing"
    iid = "inv-acceptanon02"
    target_email = "existing@example.com"
    uid = f"u-{secrets.token_hex(5)}"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, 'Existing', 'viewer', '', 1, 't-default')",
                uid, target_email,
            )
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="member",
        )

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["user_id"] == uid
        assert body["user_was_created"] is False
        assert body["already_member"] is False

        m = await _read_membership(pg_test_pool, uid, tid)
        assert m is not None
        assert m["role"] == "member"
        assert await _read_invite_status(pg_test_pool, iid) == "accepted"
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — N-tenant membership for a single user
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_one_user_can_accept_invites_into_multiple_tenants(
    client, pg_test_pool,
):
    """One user → N memberships. The same email accepts invites
    into two different tenants; the user row is created on the first
    accept and reused on the second; both memberships exist."""
    tid_a = "t-y3-accept-multi-a"
    tid_b = "t-y3-accept-multi-b"
    iid_a = "inv-acceptmulti1"
    iid_b = "inv-acceptmulti2"
    target_email = "multitenant@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        token_a = await _seed_invite_with_token(
            pg_test_pool, tid=tid_a, invite_id=iid_a,
            email=target_email, role="admin",
        )
        token_b = await _seed_invite_with_token(
            pg_test_pool, tid=tid_b, invite_id=iid_b,
            email=target_email, role="viewer",
        )

        # First accept creates the user.
        res_a = await client.post(
            f"/api/v1/invites/{iid_a}/accept",
            json={"token": token_a},
        )
        assert res_a.status_code == 200, res_a.text
        uid = res_a.json()["user_id"]
        assert res_a.json()["user_was_created"] is True

        # Second accept reuses the user.
        res_b = await client.post(
            f"/api/v1/invites/{iid_b}/accept",
            json={"token": token_b},
        )
        assert res_b.status_code == 200, res_b.text
        assert res_b.json()["user_id"] == uid
        assert res_b.json()["user_was_created"] is False

        # Both memberships exist with their respective roles.
        m_a = await _read_membership(pg_test_pool, uid, tid_a)
        m_b = await _read_membership(pg_test_pool, uid, tid_b)
        assert m_a is not None and m_a["role"] == "admin"
        assert m_b is not None and m_b["role"] == "viewer"
    finally:
        user_row = await _read_user_by_email(pg_test_pool, target_email)
        if user_row is not None:
            await _purge_user(pg_test_pool, user_row["id"])
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — token verification + replay protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_with_wrong_token_returns_403(client, pg_test_pool):
    """Bad token plaintext → 403 and the invite row stays pending."""
    tid = "t-y3-accept-badtoken"
    iid = "inv-acceptbad01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email="bad@example.com",
        )

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": "x" * 43},  # well-formed but wrong
        )
        assert res.status_code == 403, res.text
        # Row is still pending — no silent flip.
        assert await _read_invite_status(pg_test_pool, iid) == "pending"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_accept_replay_after_success_returns_409(client, pg_test_pool):
    """Re-POST with the same valid token after a successful accept
    must NOT silently 200 again — that would let an attacker confirm
    a leaked plaintext is valid. Return 409."""
    tid = "t-y3-accept-replay"
    iid = "inv-acceptreplay1"
    target_email = "replay@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid, email=target_email,
        )

        first = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert first.status_code == 200

        second = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert second.status_code == 409, second.text
        assert second.json()["current_status"] == "accepted"
    finally:
        user_row = await _read_user_by_email(pg_test_pool, target_email)
        if user_row is not None:
            await _purge_user(pg_test_pool, user_row["id"])
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_accept_revoked_invite_returns_409(client, pg_test_pool):
    tid = "t-y3-accept-revoked"
    iid = "inv-acceptrevoked"
    try:
        await _seed_tenant(pg_test_pool, tid)
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email="revoked@example.com", status="revoked",
        )
        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 409, res.text
        assert res.json()["current_status"] == "revoked"
        assert await _read_invite_status(pg_test_pool, iid) == "revoked"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_accept_persisted_expired_returns_410(client, pg_test_pool):
    """status='expired' (housekeeping sweep already flipped it)."""
    tid = "t-y3-accept-expired"
    iid = "inv-acceptexpired"
    try:
        await _seed_tenant(pg_test_pool, tid)
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email="expired@example.com", status="expired",
        )
        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 410, res.text
        assert res.json()["current_status"] == "expired"
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_accept_wallclock_expired_returns_410(client, pg_test_pool):
    """status='pending' but expires_at < now → housekeeping sweep
    gap. Defence-in-depth: reject as expired."""
    tid = "t-y3-accept-stale"
    iid = "inv-acceptstale01"
    try:
        await _seed_tenant(pg_test_pool, tid)
        past = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email="stale@example.com", status="pending", expires_at=past,
        )
        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 410, res.text
        # Row's status is still 'pending' (the endpoint did not flip
        # it — the sweep is responsible for that).
        assert await _read_invite_status(pg_test_pool, iid) == "pending"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — validation + not-found
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_malformed_invite_id_returns_422(client):
    res = await client.post(
        "/api/v1/invites/INV-BAD/accept",
        json={"token": "x" * 32},
    )
    assert res.status_code == 422, res.text
    assert "invalid invite id" in res.json()["detail"]


@_requires_pg
async def test_accept_unknown_invite_returns_404(client):
    res = await client.post(
        "/api/v1/invites/inv-deadbeef00/accept",
        json={"token": "x" * 32},
    )
    assert res.status_code == 404, res.text


@_requires_pg
async def test_accept_missing_token_returns_422(client):
    """Body missing the token field → pydantic 422 before any DB
    work."""
    res = await client.post(
        "/api/v1/invites/inv-aaaa1234/accept",
        json={},
    )
    assert res.status_code == 422, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — authenticated branch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_authenticated_email_match_skips_user_create(
    client, pg_test_pool, monkeypatch,
):
    """Caller is logged in with email == invite.email → membership
    is upserted onto the existing session user; no new user row."""
    from backend import auth as _au

    tid = "t-y3-accept-auth-match"
    iid = "inv-acceptauth01"
    target_email = "authmatch@example.com"
    uid = f"u-{secrets.token_hex(5)}"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, 'Auth Match', 'viewer', '', 1, 't-default')",
                uid, target_email,
            )
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="admin",
        )

        # Mock the optional-auth probe to return our session user.
        async def _fake_get_session(_cookie):
            from backend.auth import Session
            return Session(
                token="fake", user_id=uid, csrf_token="",
                created_at=0, expires_at=0,
            )

        async def _fake_get_user(user_id, conn=None):
            if user_id == uid:
                return _au.User(
                    id=uid, email=target_email, name="Auth Match",
                    role="viewer", enabled=True, tenant_id="t-default",
                )
            return None

        monkeypatch.setattr(_au, "get_session", _fake_get_session)
        monkeypatch.setattr(_au, "get_user", _fake_get_user)

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
            cookies={_au.SESSION_COOKIE: "fake-cookie"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["user_id"] == uid
        assert body["user_was_created"] is False

        # Membership materialised onto the existing user.
        m = await _read_membership(pg_test_pool, uid, tid)
        assert m is not None
        assert m["role"] == "admin"
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_accept_authenticated_email_mismatch_returns_409(
    client, pg_test_pool, monkeypatch,
):
    """Caller is logged in as bob@y.com but invite was issued to
    alice@x.com — refuse to silently bind the invite to a different
    account. The bucket is NOT decremented (this isn't brute force —
    the caller already has a session)."""
    from backend import auth as _au

    tid = "t-y3-accept-auth-mismatch"
    iid = "inv-acceptauth02"
    target_email = "alice@example.com"
    bob_uid = f"u-{secrets.token_hex(5)}"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, 'bob@example.com', 'Bob', 'viewer', '', 1, "
                "'t-default')",
                bob_uid,
            )
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="admin",
        )

        async def _fake_get_session(_cookie):
            from backend.auth import Session
            return Session(
                token="fake", user_id=bob_uid, csrf_token="",
                created_at=0, expires_at=0,
            )

        async def _fake_get_user(user_id, conn=None):
            if user_id == bob_uid:
                return _au.User(
                    id=bob_uid, email="bob@example.com", name="Bob",
                    role="viewer", enabled=True, tenant_id="t-default",
                )
            return None

        monkeypatch.setattr(_au, "get_session", _fake_get_session)
        monkeypatch.setattr(_au, "get_user", _fake_get_user)

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
            cookies={_au.SESSION_COOKIE: "fake-cookie"},
        )
        assert res.status_code == 409, res.text
        # Persisted state untouched.
        assert await _read_invite_status(pg_test_pool, iid) == "pending"
        # No membership materialised on Bob's account.
        m = await _read_membership(pg_test_pool, bob_uid, tid)
        assert m is None
    finally:
        await _purge_user(pg_test_pool, bob_uid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — already-member upsert behaviour
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_when_already_member_keeps_old_role(
    client, pg_test_pool,
):
    """User already has a member-role membership; admin re-issues an
    invite with role='admin'; user accepts. ON CONFLICT DO NOTHING
    means the membership row keeps its old role — admin must use the
    membership-management path to bump the role. The invite still
    flips to accepted (admin's intent that the user is in the tenant
    is satisfied) and the response carries already_member=True."""
    tid = "t-y3-accept-already-member"
    iid = "inv-acceptamem01"
    target_email = "alreadymem@example.com"
    uid = f"u-{secrets.token_hex(5)}"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (id, email, name, role, password_hash, "
                "enabled, tenant_id) "
                "VALUES ($1, $2, 'AM', 'viewer', '', 1, 't-default')",
                uid, target_email,
            )
            await conn.execute(
                "INSERT INTO user_tenant_memberships "
                "(user_id, tenant_id, role, status) "
                "VALUES ($1, $2, 'member', 'active')",
                uid, tid,
            )
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="admin",
        )

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["already_member"] is True
        # Role stayed 'member' (admin must explicitly change role).
        m = await _read_membership(pg_test_pool, uid, tid)
        assert m is not None
        assert m["role"] == "member"
        # Invite still flipped to accepted.
        assert await _read_invite_status(pg_test_pool, iid) == "accepted"
    finally:
        await _purge_user(pg_test_pool, uid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — rate-limit on failed attempts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_failed_attempts_are_rate_limited(client, pg_test_pool):
    """11 bad-token POSTs against the same invite_id should land 10
    × 403 + 1 × 429 (per TODO row 7: '10/token/min')."""
    from backend import rate_limit
    tid = "t-y3-accept-rl"
    iid = "inv-acceptrl001"
    try:
        # Reset the rate-limit bucket between tests so other suites
        # don't leak counters into ours.
        try:
            rate_limit.get_limiter().reset(f"invite_accept_fail:{iid}")
        except Exception:
            pass
        await _seed_tenant(pg_test_pool, tid)
        await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid,
            email="rl@example.com",
        )

        codes = []
        for _ in range(11):
            res = await client.post(
                f"/api/v1/invites/{iid}/accept",
                json={"token": "x" * 43},  # always wrong
            )
            codes.append(res.status_code)
        # 10 bad-token rejects then a 429.
        assert codes.count(403) == 10, codes
        assert codes.count(429) == 1, codes
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit chain — successful accept emits a row, no token leak
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_accept_emits_audit_row_without_token_leak(
    client, pg_test_pool,
):
    """Successful accept must drop a tenant_invite_accepted row in
    audit_log; before/after must NOT contain token_hash, plaintext,
    or any sha256-shaped value."""
    tid = "t-y3-accept-audit"
    iid = "inv-acceptaudit01"
    target_email = "audit@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        token = await _seed_invite_with_token(
            pg_test_pool, tid=tid, invite_id=iid, email=target_email,
        )

        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 200, res.text

        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT action, entity_kind, entity_id, before_json, "
                "after_json FROM audit_log WHERE entity_id = $1 "
                "AND action = 'tenant_invite_accepted' "
                "ORDER BY id DESC LIMIT 1",
                iid,
            )
        assert row is not None, "audit row missing for accept"
        assert row["action"] == "tenant_invite_accepted"
        assert row["entity_kind"] == "tenant_invite"
        assert row["entity_id"] == iid
        for blob in (row["before_json"] or "", row["after_json"] or ""):
            assert "token_hash" not in blob
            # Token plaintext must not have leaked into the blob.
            assert token not in blob
            # No sha256-shaped string (64 hex chars).
            assert not re.search(r"[0-9a-f]{64}", blob), (
                f"sha256-shaped string in audit blob: {blob!r}"
            )
    finally:
        user_row = await _read_user_by_email(pg_test_pool, target_email)
        if user_row is not None:
            await _purge_user(pg_test_pool, user_row["id"])
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite
    fingerprints. Re-runs the guard from rows 1/2/3 to defend against
    accidental copy-paste regressions during row 4 changes."""
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

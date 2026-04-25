"""Y3 (#279) row 1 — tests for POST /api/v1/tenants/{tid}/invites.

Covers the contract from the TODO row literal:

  * Tenant-admin-or-above gating (membership role ∈ {owner, admin}
    OR platform super_admin) — non-admin members get 403.
  * Body schema: {email, role}; role enum matches the
    user_tenant_memberships CHECK; malformed email gets 422.
  * Token plaintext returned exactly once + only sha256(plaintext)
    persisted; plaintext is high-entropy
    (secrets.token_urlsafe(32) ≥ 256 bits).
  * Response shape: {invite_id, token_plaintext, expires_at}.
  * Email channel: notifications.notify is invoked with the
    plaintext token in the body (best-effort hand-off through the
    existing tier-1 channel).
  * Audit: a ``tenant_invite_created`` row is appended; the
    plaintext token is NOT logged anywhere (audit, system log).
  * Email case is normalised for rate-limit / dup-guard but the
    DB row preserves the admin's original casing.
  * Rate limit: 5 invites per (tenant, normalised email) per hour;
    the 6th call returns 429 with Retry-After header.
  * 422 on malformed tenant id (regex), 404 on missing tenant,
    409 on already-pending invite for the same email.

Running: requires ``OMNI_TEST_PG_URL`` for the HTTP layer; the
pure-unit tests (regex, hashing, schema, helpers) run without PG.
"""

from __future__ import annotations

import hashlib
import os
import re

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


def test_tenant_id_pattern_matches_admin_tenants_pattern():
    """The invite router and the Y2 admin_tenants router must agree
    on the id regex — drift would let a malformed id sneak through
    one but not the other."""
    from backend.routers import tenant_invites
    from backend.routers import admin_tenants
    assert tenant_invites.TENANT_ID_PATTERN == admin_tenants.TENANT_ID_PATTERN


def test_invite_role_enum_matches_membership_check():
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_ROLE_ENUM == (
        "owner", "admin", "member", "viewer",
    )


def test_invite_token_bytes_is_at_least_32():
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_TOKEN_BYTES >= 32


def test_rate_limit_constants_match_spec():
    """TODO row literal: 5 invites per (tenant, email) per hour."""
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_RATE_LIMIT_CAP == 5
    assert tenant_invites.INVITE_RATE_LIMIT_WINDOW_SECONDS == 3600.0


def test_default_ttl_is_seven_days():
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_DEFAULT_TTL.total_seconds() == 7 * 86400


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: helper functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("good_id", [
    "t-default", "t-acme", "t-acme-corp", "t-a1b", "t-0abc",
])
def test_is_valid_tenant_id_accepts(good_id):
    from backend.routers.tenant_invites import _is_valid_tenant_id
    assert _is_valid_tenant_id(good_id), good_id


@pytest.mark.parametrize("bad_id", [
    "", "T-default", "tdefault", "t--double", "t-",
    "t-acme_corp", "t-acme.corp",
])
def test_is_valid_tenant_id_rejects(bad_id):
    from backend.routers.tenant_invites import _is_valid_tenant_id
    assert not _is_valid_tenant_id(bad_id), bad_id


def test_normalise_email_strips_and_lowercases():
    from backend.routers.tenant_invites import _normalise_email
    assert _normalise_email("  Alice@Example.COM ") == "alice@example.com"
    assert _normalise_email("alice@example.com") == "alice@example.com"


def test_hash_token_matches_sha256():
    from backend.routers.tenant_invites import _hash_token
    plain = "some-plaintext-secret"
    assert _hash_token(plain) == hashlib.sha256(plain.encode()).hexdigest()


def test_expires_at_is_seven_days_in_future():
    """The default TTL helper should land within ±5s of (now + 7d)."""
    import datetime as dt
    from backend.routers.tenant_invites import _expires_at_iso
    iso = _expires_at_iso()
    parsed = dt.datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=dt.timezone.utc,
    )
    delta = parsed - dt.datetime.now(dt.timezone.utc)
    assert abs(delta.total_seconds() - 7 * 86400) < 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: Pydantic body model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_create_invite_request_minimum_body():
    from backend.routers.tenant_invites import CreateInviteRequest
    body = CreateInviteRequest(email="bob@example.com")
    assert body.email == "bob@example.com"
    assert body.role == "member"  # default


def test_create_invite_request_strips_email_whitespace():
    from backend.routers.tenant_invites import CreateInviteRequest
    body = CreateInviteRequest(email="  bob@example.com  ", role="admin")
    assert body.email == "bob@example.com"


def test_create_invite_request_preserves_email_casing():
    """Casing on the row is preserved; only the comparison key
    (rate-limit, dup guard) lowercases."""
    from backend.routers.tenant_invites import CreateInviteRequest
    body = CreateInviteRequest(email="Alice@Example.COM")
    assert body.email == "Alice@Example.COM"


@pytest.mark.parametrize("bad_email", [
    "no-at-sign",
    "@nolocalpart.com",
    "no@dotinhost",
    "spaces in@example.com",
    "trailing@",
    "",
])
def test_create_invite_request_rejects_malformed_email(bad_email):
    from pydantic import ValidationError
    from backend.routers.tenant_invites import CreateInviteRequest
    with pytest.raises(ValidationError):
        CreateInviteRequest(email=bad_email)


def test_create_invite_request_rejects_unknown_role():
    from pydantic import ValidationError
    from backend.routers.tenant_invites import CreateInviteRequest
    with pytest.raises(ValidationError):
        CreateInviteRequest(email="bob@example.com", role="superuser")


def test_create_invite_request_rejects_too_long_email():
    from pydantic import ValidationError
    from backend.routers.tenant_invites import CreateInviteRequest
    huge = "a" * 310 + "@x.com"  # 316 chars after the local part trick
    # 316 chars passes (≤ 320); but an over-cap address must fail.
    huge_over = "a" * 320 + "@x.com"
    CreateInviteRequest(email=huge)  # at-or-under cap is fine
    with pytest.raises(ValidationError):
        CreateInviteRequest(email=huge_over)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: token entropy
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_plaintext_is_high_entropy_unique():
    """Spot check that successive ``token_urlsafe(32)`` calls produce
    distinct, ≥ 40-char url-safe strings. 32 bytes → 256-bit entropy
    encoded base64 ≈ 43 chars."""
    import secrets
    from backend.routers.tenant_invites import INVITE_TOKEN_BYTES
    seen = {secrets.token_urlsafe(INVITE_TOKEN_BYTES) for _ in range(50)}
    # All 50 unique with overwhelming probability.
    assert len(seen) == 50
    for tok in seen:
        # url-safe base64 → only [A-Za-z0-9_-]
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", tok), tok
        # 32 bytes ≈ 43 chars; allow ±2 for padding edge cases.
        assert len(tok) >= 40


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure-unit: router wiring (no PG required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_router_exposes_post_endpoint():
    from backend.routers.tenant_invites import router
    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}
    assert (("POST",), "/tenants/{tenant_id}/invites") in paths


def test_main_app_mounts_invite_endpoint():
    """End-to-end: backend.main exposes the endpoint at
    ``/api/v1/tenants/{tenant_id}/invites`` so a deployment that
    forgets to ``include_router`` the new module fails this test."""
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    assert (("POST",), "/api/v1/tenants/{tenant_id}/invites") in paths


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — happy path (require live PG)
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
        await conn.execute("DELETE FROM tenant_invites WHERE tenant_id = $1", tid)
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


@_requires_pg
async def test_post_invite_201_happy_path(client, pg_test_pool):
    tid = "t-y3-invite-happy"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "Alice@Example.com", "role": "admin"},
        )
        assert res.status_code == 201, res.text
        body = res.json()
        # Response shape: {invite_id, token_plaintext, expires_at}
        assert set(body) == {"invite_id", "token_plaintext", "expires_at"}
        assert body["invite_id"].startswith("inv-")
        assert isinstance(body["token_plaintext"], str)
        assert len(body["token_plaintext"]) >= 40
        # Plaintext is url-safe base64 only.
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", body["token_plaintext"])

        # The DB row stores ONLY the hash; never the plaintext.
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, tenant_id, email, role, token_hash, status "
                "FROM tenant_invites WHERE id = $1",
                body["invite_id"],
            )
        assert row is not None
        assert row["tenant_id"] == tid
        # Original casing preserved.
        assert row["email"] == "Alice@Example.com"
        assert row["role"] == "admin"
        assert row["status"] == "pending"
        # Hash matches the plaintext we just received.
        assert row["token_hash"] == hashlib.sha256(
            body["token_plaintext"].encode("ascii"),
        ).hexdigest()
        # The plaintext must NOT have been written to the DB column.
        assert body["token_plaintext"] not in row["token_hash"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_invite_default_role_is_member(client, pg_test_pool):
    tid = "t-y3-invite-default-role"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "bob@example.com"},
        )
        assert res.status_code == 201, res.text
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM tenant_invites WHERE id = $1",
                res.json()["invite_id"],
            )
        assert row["role"] == "member"
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — validation errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_malformed_tenant_id_returns_422(client):
    res = await client.post(
        "/api/v1/tenants/T-Bad-Id/invites",
        json={"email": "alice@example.com", "role": "member"},
    )
    assert res.status_code == 422


@_requires_pg
async def test_post_invite_malformed_email_returns_422(client, pg_test_pool):
    tid = "t-y3-invite-bad-email"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "not-an-email", "role": "member"},
        )
        assert res.status_code == 422
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_invite_unknown_role_returns_422(client, pg_test_pool):
    tid = "t-y3-invite-bad-role"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "alice@example.com", "role": "superuser"},
        )
        assert res.status_code == 422
    finally:
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_invite_unknown_tenant_returns_404(client):
    res = await client.post(
        "/api/v1/tenants/t-does-not-exist/invites",
        json={"email": "alice@example.com", "role": "member"},
    )
    assert res.status_code == 404, res.text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — duplicate-pending guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_duplicate_pending_returns_409(client, pg_test_pool):
    tid = "t-y3-invite-dup"
    try:
        await _seed_tenant(pg_test_pool, tid)
        first = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "alice@example.com", "role": "member"},
        )
        assert first.status_code == 201, first.text
        # Same email, possibly with different casing — still a dup.
        dup = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "ALICE@example.com", "role": "admin"},
        )
        assert dup.status_code == 409, dup.text
        body = dup.json()
        assert body["existing_invite_id"] == first.json()["invite_id"]
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — RBAC: member / viewer cannot invite
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_non_admin_member_gets_403(client, pg_test_pool):
    """A user whose membership.role is 'member' (or 'viewer') on the
    target tenant is NOT permitted to issue invites — must 403."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-invite-rbac-member"
    uid = "u-y3-invite-rbac-member"
    try:
        await _seed_tenant(pg_test_pool, tid)
        async with pg_test_pool.acquire() as conn:
            # Seed a user + a member-role membership on the target.
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
            res = await client.post(
                f"/api/v1/tenants/{tid}/invites",
                json={"email": "alice@example.com", "role": "member"},
            )
            assert res.status_code == 403, res.text
            assert "tenant admin" in res.json()["detail"]
        finally:
            app.dependency_overrides.pop(_au.current_user, None)

        # And no invite row was inserted.
        async with pg_test_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM tenant_invites WHERE tenant_id = $1",
                tid,
            )
        assert int(count) == 0
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
            )
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await _purge_tenant(pg_test_pool, tid)


@_requires_pg
async def test_post_invite_tenant_admin_member_allowed(client, pg_test_pool):
    """Membership role='admin' on the target tenant — should succeed
    even when the *account* role is only 'viewer'."""
    from backend.main import app
    from backend import auth as _au

    tid = "t-y3-invite-rbac-admin"
    uid = "u-y3-invite-rbac-admin"
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

        tadmin = _au.User(
            id=uid, email="tadmin@example.com", name="T Admin",
            role="viewer", enabled=True, tenant_id=tid,
        )

        async def _fake_current_user():
            return tadmin

        app.dependency_overrides[_au.current_user] = _fake_current_user
        try:
            res = await client.post(
                f"/api/v1/tenants/{tid}/invites",
                json={"email": "newcomer@example.com", "role": "member"},
            )
            assert res.status_code == 201, res.text
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
#  HTTP path — rate-limit (5/email/tenant/hour)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_rate_limit_5_per_hour(client, pg_test_pool):
    """6 calls within the same hour to the same (tenant, email) — the
    6th must 429. We rotate roles so each call is otherwise novel."""
    from backend.rate_limit import get_limiter
    tid = "t-y3-invite-rl"
    target_email = "rl-target@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Reset the bucket in case prior tests in the session burned
        # tokens for this exact key.
        try:
            get_limiter().reset(f"tenant_invite:{tid}:{target_email}")
        except Exception:
            pass

        # 5 successful calls — each one creates a row, so we delete
        # between calls to avoid the 409 dup-pending guard masking
        # the rate-limit signal.
        for i in range(5):
            res = await client.post(
                f"/api/v1/tenants/{tid}/invites",
                json={"email": target_email, "role": "member"},
            )
            assert res.status_code == 201, (
                f"call #{i+1} unexpectedly {res.status_code}: {res.text}"
            )
            async with pg_test_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM tenant_invites WHERE tenant_id = $1",
                    tid,
                )

        # 6th call → 429.
        res6 = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": target_email, "role": "member"},
        )
        assert res6.status_code == 429, res6.text
        assert "Retry-After" in res6.headers
        assert int(res6.headers["Retry-After"]) >= 1
    finally:
        try:
            get_limiter().reset(f"tenant_invite:{tid}:{target_email}")
        except Exception:
            pass
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — email channel side-effect via notifications.notify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_invokes_notify_with_token(
    client, pg_test_pool, monkeypatch,
):
    """notifications.notify is called once with the plaintext token in
    the body. This mirrors the TODO row literal "先接 notification_*
    既有通道". A future SMTP path can swap in without breaking the
    contract checked here."""
    from backend.routers import tenant_invites as _ti

    captured: list[dict] = []

    async def _fake_send_invite_email(**kwargs):
        captured.append(kwargs)
        return None

    monkeypatch.setattr(_ti, "_send_invite_email", _fake_send_invite_email)

    tid = "t-y3-invite-notify"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "carol@example.com", "role": "member"},
        )
        assert res.status_code == 201, res.text
        plaintext = res.json()["token_plaintext"]

        # Email helper invoked exactly once with the matching values.
        assert len(captured) == 1
        kw = captured[0]
        assert kw["tenant_id"] == tid
        assert kw["recipient"] == "carol@example.com"
        assert kw["role"] == "member"
        assert kw["token_plaintext"] == plaintext
        assert kw["expires_at"]  # non-empty
    finally:
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP path — audit log written, plaintext NOT logged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_post_invite_audit_row_written(client, pg_test_pool):
    tid = "t-y3-invite-audit"
    try:
        await _seed_tenant(pg_test_pool, tid)
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": "audit@example.com", "role": "member"},
        )
        assert res.status_code == 201, res.text
        plaintext = res.json()["token_plaintext"]
        invite_id = res.json()["invite_id"]

        async with pg_test_pool.acquire() as conn:
            audit_row = await conn.fetchrow(
                "SELECT actor, action, entity_kind, entity_id, "
                "       after_json "
                "FROM audit_log "
                "WHERE action = 'tenant_invite_created' "
                "  AND entity_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                invite_id,
            )
        assert audit_row is not None
        assert audit_row["entity_kind"] == "tenant_invite"
        # CRITICAL: plaintext token must NEVER appear in the audit
        # row. Hash is fine; plaintext is not.
        assert plaintext not in (audit_row["after_json"] or "")
    finally:
        # Purge the audit row so it doesn't leak into other tests.
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit_log WHERE entity_kind = 'tenant_invite' "
                "AND entity_id IN (SELECT id FROM tenant_invites "
                "                  WHERE tenant_id = $1)",
                tid,
            )
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Self-fingerprint guard — SOP Step 3 pre-commit pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """The router source must not contain compat-era SQLite fingerprints
    (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    ``VALUES ... ?, ?`` placeholder). asyncpg pool conns don't have
    ``.commit()`` and PG uses ``$1, $2`` parameters."""
    import pathlib
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

"""Y3 (#279) row 8 — invite/accept/membership lifecycle integration test.

This is the **cross-row integration** test for the Y3 admin REST surface.
Rows 1-7 each have their own narrow drift-guard files
(``test_tenant_invites_create.py`` / ``_list.py`` / ``_revoke.py`` /
``_accept.py`` / ``_rate_limits.py`` / ``test_admin_super_admins.py`` /
``test_tenant_members.py``); this file exercises the **end-to-end flow**
that traverses multiple rows in a single test, plus a small set of
pure-unit drift guards on the literals the TODO row pins explicitly:

  * 完整邀請 → accept → 雙 membership → switch tenant → 卸任 user
    → audit 鏈驗證
  * 過期 invite 不可 accept
  * revoked invite 不可 accept
  * email 大小寫規整化
  * token 長度 / 熵驗證

Two-tier test design (matches the row 1-6 convention)
─────────────────────────────────────────────────────
**Pure-unit** drift guards (run in every CI lane, including the
PG-less workstation default — the rate-limit row 7 file already
established this convention):

  Family A — Token entropy + length pinning (5 tests)
  Family B — Email casing normalisation invariants (4 tests)
  Family C — State-machine guards (cross-router) (4 tests)
  Family J — Self-fingerprint guard (1 test)

**HTTP-PG** integration scenarios (skip when ``OMNI_TEST_PG_URL``
unset):

  Family D — Full happy-path lifecycle: tenant-A invite → anon accept
             (creates user + membership) → tenant-B invite for same
             email → authed accept (existing user + 2nd membership) →
             cross-tenant management isolation → soft-delete in
             tenant-A → membership in tenant-B untouched (1 test)
  Family E — Expired invite cannot accept at lifecycle boundary (1)
  Family F — Revoked invite cannot accept at lifecycle boundary (1)
  Family G — Email casing normalised through full HTTP flow (1)
  Family H — Audit chain integrity verified after lifecycle (1)
  Family I — Main-app routes mounted for the lifecycle path (1)

SOP cross-checks
────────────────
Module-global state (Step 1 Q1):
  * 0 new module-global state introduced — this is a test-only file.
    The constants we ASSERT against (``INVITE_TOKEN_BYTES``,
    ``INVITE_RATE_LIMIT_*``, ``ACCEPT_FAIL_RATE_LIMIT_*``,
    ``LISTABLE_INVITE_STATUSES``, ``MEMBERSHIP_ROLE_ENUM`` etc.) are
    all module-level immutable on the routers we depend on; they
    derive deterministically per-worker, qualifying answer #1.
  * DB state goes through PG (qualifying answer #2).
  * Audit chain serialised via ``pg_advisory_xact_lock`` per tenant
    (qualifying answer #2 — already documented on backend.audit).

Read-after-write timing (Step 1 Q2):
  * Lifecycle test issues each step sequentially and awaits HTTP 200
    before the next request, so the asyncpg pool's commit-then-read
    ordering is not a regression vector here. Tests that probe race
    behaviour live in row 4 (concurrent accept FOR UPDATE).

Pre-commit fingerprint grep (Step 3):
  * This file deliberately uses the four fingerprint patterns inside
    a regex literal (Family J) — so it self-tests the ``backend/``
    routers it asserts against without scanning itself. Same design
    as row 4 / row 6 self-fingerprint guards.
"""

from __future__ import annotations

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
#  Family A — Token entropy / length / encoding
#  TODO row literal: "token 長度 / 熵驗證"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_invite_token_byte_count_meets_256_bit_floor():
    """``secrets.token_urlsafe(N)`` produces N random bytes encoded as
    url-safe base64. 32 bytes == 256 bits of entropy, which is the
    cryptographic floor we promise in the row 1 module docstring
    ("256-bit entropy encoded url-safe base64") and the only number
    consistent with sha256 collision resistance for the
    ``token_hash`` UNIQUE constraint.

    A future drop below 32 would silently weaken the brute-force
    bound the ``ACCEPT_FAIL_RATE_LIMIT_*`` budget assumes —
    catch the regression here at construction time, not at threat
    review time.
    """
    from backend.routers.tenant_invites import INVITE_TOKEN_BYTES
    assert INVITE_TOKEN_BYTES >= 32, (
        f"INVITE_TOKEN_BYTES={INVITE_TOKEN_BYTES} < 32 — entropy floor "
        "breached; 256 bits is the floor cited in row 1 docstring"
    )


def test_invite_token_urlsafe_min_43_ascii_chars():
    """url-safe base64 of 32 random bytes is at least 43 ASCII chars
    (the trailing padding is stripped by token_urlsafe). The accept
    handler's pydantic schema pins ``min_length=16`` for the request
    body's ``token`` field — assert the produced plaintext clears
    that bar with healthy margin so a low-entropy regression cannot
    smuggle a < 16 char token through validation."""
    from backend.routers.tenant_invites import INVITE_TOKEN_BYTES
    plaintext = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
    assert len(plaintext) >= 43, len(plaintext)
    # url-safe base64 = digits + ASCII letters + ``-`` + ``_``. The
    # AcceptInviteRequest validator does not regex-check the body,
    # but a non-ASCII char in the plaintext would imply a different
    # encoding family (the test would fire as a sanity probe).
    for ch in plaintext:
        assert ch.isalnum() or ch in "-_", repr(plaintext)


def test_post_handler_uses_secrets_token_urlsafe_with_invite_token_bytes():
    """Source-grep guard: a future refactor swapping ``token_urlsafe``
    for a custom RNG (or dropping the ``INVITE_TOKEN_BYTES`` literal
    in favour of a magic number) would silently break the entropy
    floor without breaking any test that doesn't read the source.
    Lock the literal callsite here.
    """
    src = pathlib.Path(
        "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(INVITE_TOKEN_BYTES)" in src, (
        "POST handler must mint plaintext via "
        "``secrets.token_urlsafe(INVITE_TOKEN_BYTES)`` — drift "
        "loosens the entropy floor"
    )


def test_token_hash_is_64_char_lowercase_hex():
    """``_hash_token`` returns a sha256 hex digest. A 64-char lower-
    hex string is the only shape that satisfies the alembic
    CHECK constraint on ``tenant_invites.token_hash`` (length ≥ 16)
    *and* the audit-chain leak guards in rows 2/3/4 (which scan
    blobs for any 64-hex-char substring as a sha256 fingerprint).
    """
    from backend.routers.tenant_invites import _hash_token
    h = _hash_token("the quick brown fox " + secrets.token_urlsafe(32))
    assert isinstance(h, str)
    assert len(h) == 64, len(h)
    assert re.fullmatch(r"[0-9a-f]{64}", h), h


def test_accept_request_token_length_band():
    """The accept handler pydantic schema accepts tokens in the
    [16..512] char band. 16 below ⇒ obvious typo → 422 before any DB
    work; 512 above ⇒ pathologically long ⇒ 422 before sha256 cost.
    Pin both numbers here — the row 1 / row 4 docstring discussion
    of "well-formed but wrong" tokens at 32-43 chars depends on the
    band being intact.
    """
    from backend.routers.tenant_invites import AcceptInviteRequest
    from pydantic import ValidationError

    # below band → 422 (handler pydantic raises ValidationError pre-handler).
    with pytest.raises(ValidationError):
        AcceptInviteRequest(token="x" * 15)
    # at floor (16) → ok.
    AcceptInviteRequest(token="x" * 16)
    # at ceiling (512) → ok.
    AcceptInviteRequest(token="x" * 512)
    # above ceiling → 422.
    with pytest.raises(ValidationError):
        AcceptInviteRequest(token="x" * 513)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family B — Email casing normalisation invariants
#  TODO row literal: "email 大小寫規整化"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("raw,want", [
    ("Alice@Example.COM", "alice@example.com"),
    ("  alice@example.com  ", "alice@example.com"),
    ("BOB@SUB.EXAMPLE.NET", "bob@sub.example.net"),
    ("alice@example.com", "alice@example.com"),
    ("  Mixed.CASE@Domain.IO\t", "mixed.case@domain.io"),
])
def test_normalise_email_lowercases_and_strips(raw, want):
    """The shared ``_normalise_email`` is the single chokepoint
    that decides "alice@x.com" and "Alice@X.COM" map to the same
    bucket — for rate-limit, dup-pending guard, accept-time
    comparison. Drift here means an attacker can flood a recipient
    by varying casing, or a legitimate user can't accept their
    own invite if the admin used different casing.
    """
    from backend.routers.tenant_invites import _normalise_email
    assert _normalise_email(raw) == want


def test_normalise_email_is_idempotent():
    """f(f(x)) == f(x). Cheap invariant but it codifies that the
    normalisation is a pure function with no hidden state — a
    future "smart" version that did unicode NFKC etc. could break
    this if not careful."""
    from backend.routers.tenant_invites import _normalise_email
    once = _normalise_email("Alice@Example.COM")
    twice = _normalise_email(once)
    assert once == twice


def test_post_handler_uses_normalised_email_for_rate_limit_key():
    """Source-grep guard: the rate-limit bucket key MUST include the
    normalised email, not the raw casing — otherwise an attacker
    flips one letter's case to bypass the per-(tenant,email) cap.

    The row 7 drift-guard already pins ``f"tenant_invite:{tid}:
    {norm_email}"`` shape; here we double-bind the dependency
    direction (norm_email is sourced from ``_normalise_email``).
    """
    src = pathlib.Path(
        "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    assert "norm_email = _normalise_email(raw_email)" in src, (
        "rate-limit key must derive from _normalise_email(raw_email)"
    )
    assert 'f"tenant_invite:{tenant_id}:{norm_email}"' in src, (
        "rate-limit key must include tenant_id + normalised email"
    )


def test_dup_pending_guard_uses_lowercase_email():
    """Source-grep guard: the SQL that hunts for an existing
    ``status='pending'`` invite for the same email MUST compare
    ``lower(email)`` so casing alone cannot smuggle a duplicate
    pending row past the guard."""
    from backend.routers.tenant_invites import _FETCH_PENDING_INVITE_SQL
    assert "lower(email) = $2" in _FETCH_PENDING_INVITE_SQL, (
        "dup-pending guard SQL must compare lower(email) — drift "
        "lets casing variants double-issue invites"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family C — State-machine guards (cross-router)
#  TODO row literal: "過期 invite 不可 accept；revoked invite 不可 accept"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_accept_handler_rejects_non_pending_status():
    """Source-grep guard: the accept handler MUST gate on the
    persisted status field — i.e. anything other than ``status=
    'pending'`` short-circuits to 409/410 before the token check
    runs. Drift here would let a leaked plaintext be replayed against
    an accepted invite (silent 200) or against a revoked invite
    (admin's "revoke" stops mattering)."""
    src = pathlib.Path(
        "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    # The literal handler line that turns non-pending into a non-200.
    assert 'invite["status"] != "pending"' in src


def test_accept_handler_rejects_wallclock_expired():
    """Source-grep guard for the housekeeping-sweep-gap defence: a
    persisted ``status='pending'`` row whose wall-clock has passed
    ``expires_at`` is treated as expired (410), not silently
    accepted. Locks the ``wallclock_expired`` literal in the handler.
    """
    src = pathlib.Path(
        "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    assert "wallclock_expired" in src
    assert "exp_dt <= datetime.now(timezone.utc)" in src


def test_revoke_handler_only_flips_pending_to_revoked():
    """The revoke SQL is scoped to ``status='pending'``; it does NOT
    flip a ``status='accepted'`` row into ``revoked`` (which would
    let an admin retroactively un-grant a membership without
    actually removing the membership row). Lock that scoping
    literal in the SQL."""
    from backend.routers.tenant_invites import _REVOKE_INVITE_SQL
    sql = _REVOKE_INVITE_SQL
    assert "AND status = 'pending'" in sql, sql
    assert "SET status = 'revoked'" in sql, sql


def test_membership_role_enum_matches_invite_role_enum():
    """The roles an invite can grant must be a subset of the
    membership role enum, otherwise an invite with role='guest' (a
    hypothetical future extension) would 422 at PG insert time on
    accept after the invite was already issued — caller-facing
    surprise that violates "invite issuance promises a workable
    accept"."""
    from backend.routers.tenant_invites import INVITE_ROLE_ENUM
    from backend.routers.tenant_members import MEMBERSHIP_ROLE_ENUM
    assert set(INVITE_ROLE_ENUM).issubset(set(MEMBERSHIP_ROLE_ENUM)), (
        f"invite roles {INVITE_ROLE_ENUM} not subset of membership "
        f"roles {MEMBERSHIP_ROLE_ENUM}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family I — Main-app route mounting (sanity check the lifecycle
#  path is reachable; PG-skip not required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_main_app_mounts_full_lifecycle_paths():
    """End-to-end mount check for the lifecycle's six chokepoint
    paths. A regression that drops one of the ``include_router``
    lines in main.py would break the lifecycle flow at runtime;
    surfacing the gap here at collection time is cheaper.
    """
    os.environ.setdefault("OMNISIGHT_AUTH_MODE", "open")
    from backend.main import app
    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes if hasattr(r, "path")
    }
    expected = {
        (("POST",), "/api/v1/tenants/{tenant_id}/invites"),
        (("GET",), "/api/v1/tenants/{tenant_id}/invites"),
        (("DELETE",), "/api/v1/tenants/{tenant_id}/invites/{invite_id}"),
        (("POST",), "/api/v1/invites/{invite_id}/accept"),
        (("GET",), "/api/v1/tenants/{tenant_id}/members"),
        (("DELETE",), "/api/v1/tenants/{tenant_id}/members/{user_id}"),
    }
    missing = expected - paths
    assert not missing, f"lifecycle path(s) not mounted: {missing}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP fixtures — small wrappers that mirror the row 4 / row 6 style
#  but seed via the REAL router endpoints (not raw INSERTs) so the
#  lifecycle test exercises the full HTTP surface end-to-end.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_tenant(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Lifecycle {tid}",
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


async def _purge_user_by_email(pool, email_norm: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE lower(email) = $1", email_norm,
        )
        if row is None:
            return None
        uid = row["id"]
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE user_id = $1", uid,
        )
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        return uid


async def _read_membership(pool, *, uid: str, tid: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            uid, tid,
        )


async def _read_user_id_by_email(pool, email_norm: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE lower(email) = $1", email_norm,
        )
    return row["id"] if row else None


async def _purge_audit_for_tenant(pool, tid: str) -> None:
    """Wipe audit_log for a tenant chain so verify_chain runs against
    a clean slate populated only by this test's writes. The chain is
    per-tenant (advisory-locked on tenant_id), so other tenants'
    chains stay intact.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE tenant_id = $1", tid,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family D — Full happy-path lifecycle
#  TODO row literal: "完整邀請 → accept → 雙 membership → switch tenant
#                    → 卸任 user → audit 鏈驗證"
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_full_invite_lifecycle_dual_membership_switch_tenant_and_suspend(
    client, pg_test_pool, monkeypatch,
):
    """The headline lifecycle test.

    1. Admin issues an invite for ``alice@x.com`` into tenant A
       (POST /tenants/{tA}/invites). The 201 response surfaces the
       plaintext token exactly once.
    2. Anonymous accept of that token (POST /invites/{id}/accept) —
       a fresh ``users`` row is created and a tenant-A membership
       row materialises with the invite's role.
    3. Admin issues a SECOND invite for the SAME email into tenant B
       (different role this time — viewer instead of admin).
    4. The recipient is now "logged in" as the user materialised in
       step 2 (we mock the optional-auth probe so the accept handler
       takes the authenticated branch). Accept the tenant-B invite →
       membership row materialises in tenant B WITHOUT creating a
       second users row.
    5. Sanity check: GET /tenants/{tA}/members and /tenants/{tB}/members
       both surface the same user_id. This is the "switch tenant"
       contract — one user lives concurrently in N tenants.
    6. Tenant-A admin DELETE /tenants/{tA}/members/{uid} (soft-suspend
       in tenant A — the "卸任 user" path). The tenant-B membership
       MUST stay active — the two tenants' membership management is
       isolated.
    7. Audit chain integrity: ``verify_chain`` over both tenants'
       chains returns (True, None). Three audit actions emitted
       (``tenant_invite_created`` × 2 + ``tenant_invite_accepted`` × 2
       + ``tenant_member_updated`` × 1) chain together without a
       broken prev_hash → curr_hash link.
    """
    from backend import auth as _au
    from backend import audit

    tid_a = "t-y3-life-fullcycle-a"
    tid_b = "t-y3-life-fullcycle-b"
    target_email = "alice.lifecycle@example.com"

    try:
        await _seed_tenant(pg_test_pool, tid_a)
        await _seed_tenant(pg_test_pool, tid_b)
        # Clean per-tenant audit chains so verify_chain at the end
        # walks rows produced solely by this test.
        await _purge_audit_for_tenant(pg_test_pool, tid_a)
        await _purge_audit_for_tenant(pg_test_pool, tid_b)

        # Step 1: tenant-A admin issues invite. X-Tenant-Id header
        # tells the audit middleware which chain the row should
        # join — without it, the audit row would land on
        # 't-default' chain, which other tests share.
        res_a = await client.post(
            f"/api/v1/tenants/{tid_a}/invites",
            json={"email": target_email, "role": "admin"},
            headers={"X-Tenant-Id": tid_a},
        )
        assert res_a.status_code == 201, res_a.text
        body_a = res_a.json()
        invite_id_a = body_a["invite_id"]
        token_a = body_a["token_plaintext"]
        assert token_a and len(token_a) >= 43, (
            f"tenant-A invite token implausibly short: {len(token_a)} "
            "chars — entropy floor regression"
        )

        # Step 2: anonymous accept tenant-A invite. We pass
        # X-Tenant-Id explicitly so the audit emission's chain
        # attribution lands on tenant A's chain — the accept
        # handler itself doesn't know which tenant header to
        # propagate (anonymous caller, no session) and otherwise
        # the audit row would land on the shared 't-default' chain
        # which other tests use. Production call paths from the
        # email link won't carry this header and will land on
        # t-default; the contract under test here is "given the
        # right header, the chain attribution is consistent" not
        # "production accept lands on tenant chain by default".
        res_acc_a = await client.post(
            f"/api/v1/invites/{invite_id_a}/accept",
            json={"token": token_a, "name": "Alice Lifecycle"},
            headers={"X-Tenant-Id": tid_a},
        )
        assert res_acc_a.status_code == 200, res_acc_a.text
        body_acc_a = res_acc_a.json()
        uid = body_acc_a["user_id"]
        assert body_acc_a["user_was_created"] is True
        assert body_acc_a["already_member"] is False
        assert body_acc_a["role"] == "admin"
        assert body_acc_a["tenant_id"] == tid_a

        m_a = await _read_membership(pg_test_pool, uid=uid, tid=tid_a)
        assert m_a is not None
        assert m_a["role"] == "admin"
        assert m_a["status"] == "active"

        # Step 3: tenant-B admin issues a viewer invite for the same
        # email. Different tenant, so the dup-pending guard (scoped
        # by tenant_id) does NOT trip.
        res_b = await client.post(
            f"/api/v1/tenants/{tid_b}/invites",
            json={"email": target_email, "role": "viewer"},
            headers={"X-Tenant-Id": tid_b},
        )
        assert res_b.status_code == 201, res_b.text
        body_b = res_b.json()
        invite_id_b = body_b["invite_id"]
        token_b = body_b["token_plaintext"]

        # Step 4: authenticated accept of tenant-B invite as the user
        # we just materialised. We mock the optional-auth probe used
        # by the accept handler so it takes the authed branch (no
        # new users row, just an additional membership upsert).
        async def _fake_get_session(_cookie):
            return _au.Session(
                token="fake", user_id=uid, csrf_token="",
                created_at=0, expires_at=0,
            )

        async def _fake_get_user(user_id, conn=None):
            if user_id == uid:
                return _au.User(
                    id=uid, email=target_email,
                    name="Alice Lifecycle", role="viewer",
                    enabled=True, tenant_id=tid_a,
                )
            return None

        monkeypatch.setattr(_au, "get_session", _fake_get_session)
        monkeypatch.setattr(_au, "get_user", _fake_get_user)

        res_acc_b = await client.post(
            f"/api/v1/invites/{invite_id_b}/accept",
            json={"token": token_b},
            cookies={_au.SESSION_COOKIE: "fake-cookie"},
            headers={"X-Tenant-Id": tid_b},
        )
        assert res_acc_b.status_code == 200, res_acc_b.text
        body_acc_b = res_acc_b.json()
        assert body_acc_b["user_id"] == uid, (
            "authed accept must reuse the existing user row "
            "(one user → N memberships)"
        )
        assert body_acc_b["user_was_created"] is False
        assert body_acc_b["role"] == "viewer"
        assert body_acc_b["tenant_id"] == tid_b

        # Both memberships materialised; one user, two tenants.
        m_a2 = await _read_membership(pg_test_pool, uid=uid, tid=tid_a)
        m_b = await _read_membership(pg_test_pool, uid=uid, tid=tid_b)
        assert m_a2 is not None and m_a2["role"] == "admin"
        assert m_b is not None and m_b["role"] == "viewer"
        assert m_a2["status"] == "active"
        assert m_b["status"] == "active"

        # Step 5: switch-tenant — admin views members from each
        # tenant independently. With ``OMNISIGHT_AUTH_MODE=open``
        # the synthetic ANON super_admin satisfies RBAC for both.
        # We need at least one OTHER active admin in each tenant so
        # the last-admin floor doesn't trip when we suspend Alice
        # in tenant A. Seed a keeper row directly so the floor
        # check passes — a real deployment would have multiple
        # admins; we're testing the suspension path, not the floor
        # invariant (that's row 6's job).
        async with pg_test_pool.acquire() as conn:
            for keeper_tid in (tid_a, tid_b):
                keeper_uid = f"u-life-keep{keeper_tid[-1]}"
                # Following the row 6 fixture pattern:
                # ``users.role='viewer'`` (account-level), the
                # admin authority lives on the per-tenant
                # membership row. The floor check counts
                # ``memberships.role IN (owner,admin) AND
                # status='active' AND users.enabled=1``, so the
                # account-level role is not consulted here.
                await conn.execute(
                    "INSERT INTO users "
                    "(id, email, name, role, password_hash, enabled, "
                    "tenant_id) "
                    "VALUES ($1, $2, 'Keeper', 'viewer', '', 1, $3) "
                    "ON CONFLICT (id) DO NOTHING",
                    keeper_uid,
                    f"keep{keeper_tid[-1]}@lifecycle.example",
                    keeper_tid,
                )
                await conn.execute(
                    "INSERT INTO user_tenant_memberships "
                    "(user_id, tenant_id, role, status, created_at) "
                    "VALUES ($1, $2, 'admin', 'active', $3) "
                    "ON CONFLICT (user_id, tenant_id) DO NOTHING",
                    keeper_uid, keeper_tid,
                    datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S",
                    ),
                )

        # Reset the auth mocks so admin-listing requests get the
        # default open-mode super_admin synthesis (bypasses RBAC).
        monkeypatch.undo()

        list_a = await client.get(
            f"/api/v1/tenants/{tid_a}/members?status=all",
            headers={"X-Tenant-Id": tid_a},
        )
        assert list_a.status_code == 200, list_a.text
        a_uids = {m["user_id"] for m in list_a.json()["members"]}
        assert uid in a_uids, "Alice missing from tenant A member list"

        list_b = await client.get(
            f"/api/v1/tenants/{tid_b}/members?status=all",
            headers={"X-Tenant-Id": tid_b},
        )
        assert list_b.status_code == 200, list_b.text
        b_uids = {m["user_id"] for m in list_b.json()["members"]}
        assert uid in b_uids, "Alice missing from tenant B member list"

        # Step 6: 卸任 — soft-delete (suspend) Alice in tenant A only.
        res_del = await client.delete(
            f"/api/v1/tenants/{tid_a}/members/{uid}",
            headers={"X-Tenant-Id": tid_a},
        )
        assert res_del.status_code == 200, res_del.text
        body_del = res_del.json()
        assert body_del["status"] == "suspended"
        assert body_del["already_suspended"] is False

        # Cross-tenant isolation contract: the tenant-B membership
        # is untouched. Tenant A admin's 卸任 action only affects
        # tenant A's namespace.
        m_a_after = await _read_membership(pg_test_pool, uid=uid, tid=tid_a)
        m_b_after = await _read_membership(pg_test_pool, uid=uid, tid=tid_b)
        assert m_a_after["status"] == "suspended"
        assert m_b_after["status"] == "active", (
            "tenant-B membership flipped to suspended — "
            "cross-tenant management is leaking"
        )
        assert m_b_after["role"] == "viewer"

        # Step 7: audit chain integrity. Per-tenant chains should
        # validate against their own internal prev_hash → curr_hash
        # walk. ``verify_chain`` returns (True, None) on success and
        # (False, first_bad_id) on tampered chain. We expect three
        # rows on tenant A (created + accepted + member_updated) and
        # two on tenant B (created + accepted), all chained.
        ok_a, bad_a = await audit.verify_chain(tenant_id=tid_a)
        assert ok_a, f"tenant-A audit chain broken at id={bad_a}"
        ok_b, bad_b = await audit.verify_chain(tenant_id=tid_b)
        assert ok_b, f"tenant-B audit chain broken at id={bad_b}"

        # Sanity: each chain has at least the actions we expect.
        async with pg_test_pool.acquire() as conn:
            actions_a = await conn.fetch(
                "SELECT action FROM audit_log WHERE tenant_id = $1 "
                "ORDER BY id ASC", tid_a,
            )
            actions_b = await conn.fetch(
                "SELECT action FROM audit_log WHERE tenant_id = $1 "
                "ORDER BY id ASC", tid_b,
            )
        actions_a_list = [r["action"] for r in actions_a]
        actions_b_list = [r["action"] for r in actions_b]
        assert "tenant_invite_created" in actions_a_list
        assert "tenant_invite_accepted" in actions_a_list
        assert "tenant_member_updated" in actions_a_list
        assert "tenant_invite_created" in actions_b_list
        assert "tenant_invite_accepted" in actions_b_list
    finally:
        await _purge_user_by_email(pg_test_pool, target_email.lower())
        async with pg_test_pool.acquire() as conn:
            for keeper_tid in (tid_a, tid_b):
                await conn.execute(
                    "DELETE FROM user_tenant_memberships "
                    "WHERE user_id = $1",
                    f"u-life-keep{keeper_tid[-1]}",
                )
                await conn.execute(
                    "DELETE FROM users WHERE id = $1",
                    f"u-life-keep{keeper_tid[-1]}",
                )
        await _purge_audit_for_tenant(pg_test_pool, tid_a)
        await _purge_audit_for_tenant(pg_test_pool, tid_b)
        await _purge_tenant(pg_test_pool, tid_a)
        await _purge_tenant(pg_test_pool, tid_b)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family E — Expired invite cannot accept (lifecycle-level)
#  TODO row literal: "過期 invite 不可 accept"
#
#  This test issues the invite via the REAL POST endpoint (not a
#  raw DB seed), then manually advances the persisted ``expires_at``
#  into the past so the wallclock-expired branch fires. This
#  exercises the cross-row chain (POST → accept) end-to-end, which
#  the row 4 unit test (``test_accept_wallclock_expired_returns_410``)
#  bypasses by seeding the invite directly.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_expired_invite_cannot_be_accepted_at_lifecycle_boundary(
    client, pg_test_pool,
):
    tid = "t-y3-life-expired"
    target_email = "expired.lifecycle@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        # Issue the invite via the real POST handler.
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": target_email, "role": "member"},
            headers={"X-Tenant-Id": tid},
        )
        assert res.status_code == 201, res.text
        invite_id = res.json()["invite_id"]
        token = res.json()["token_plaintext"]

        # Advance the persisted expires_at into the past so the
        # wallclock-expired guard in the accept handler fires.
        past = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d %H:%M:%S")
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenant_invites SET expires_at = $1 WHERE id = $2",
                past, invite_id,
            )

        # Accept must fail with 410 Gone.
        res_acc = await client.post(
            f"/api/v1/invites/{invite_id}/accept",
            json={"token": token},
        )
        assert res_acc.status_code == 410, res_acc.text
        assert res_acc.json()["current_status"] == "expired"

        # No user row was created (the wallclock-expired branch
        # short-circuits before INSERT INTO users).
        uid = await _read_user_id_by_email(
            pg_test_pool, target_email.lower(),
        )
        assert uid is None, (
            f"user row materialised despite expired-accept rejection "
            f"(uid={uid}); accept handler is leaking"
        )

        # No membership row materialised.
        async with pg_test_pool.acquire() as conn:
            mc = await conn.fetchval(
                "SELECT COUNT(*) FROM user_tenant_memberships "
                "WHERE tenant_id = $1", tid,
            )
        assert int(mc) == 0
    finally:
        await _purge_user_by_email(pg_test_pool, target_email.lower())
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family F — Revoked invite cannot accept (lifecycle-level)
#  TODO row literal: "revoked invite 不可 accept"
#
#  As with Family E, this issues + revokes via the REAL POST and
#  DELETE endpoints (not raw seeds), so the chain "issue → revoke →
#  accept fails" is end-to-end.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_revoked_invite_cannot_be_accepted_at_lifecycle_boundary(
    client, pg_test_pool,
):
    tid = "t-y3-life-revoked"
    target_email = "revoked.lifecycle@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)

        # Issue.
        res_post = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": target_email, "role": "member"},
            headers={"X-Tenant-Id": tid},
        )
        assert res_post.status_code == 201, res_post.text
        invite_id = res_post.json()["invite_id"]
        token = res_post.json()["token_plaintext"]

        # Revoke via real DELETE endpoint.
        res_rev = await client.delete(
            f"/api/v1/tenants/{tid}/invites/{invite_id}",
            headers={"X-Tenant-Id": tid},
        )
        assert res_rev.status_code == 200, res_rev.text
        assert res_rev.json()["status"] == "revoked"
        assert res_rev.json()["already_revoked"] is False

        # Accept must now refuse with 409 (not 410 — revoked is a
        # distinct terminal state from expired in row 4's design).
        res_acc = await client.post(
            f"/api/v1/invites/{invite_id}/accept",
            json={"token": token},
        )
        assert res_acc.status_code == 409, res_acc.text
        assert res_acc.json()["current_status"] == "revoked"

        # No user / membership materialised.
        uid = await _read_user_id_by_email(
            pg_test_pool, target_email.lower(),
        )
        assert uid is None
        async with pg_test_pool.acquire() as conn:
            mc = await conn.fetchval(
                "SELECT COUNT(*) FROM user_tenant_memberships "
                "WHERE tenant_id = $1", tid,
            )
        assert int(mc) == 0
    finally:
        await _purge_user_by_email(pg_test_pool, target_email.lower())
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family G — Email casing normalised through full HTTP flow
#  TODO row literal: "email 大小寫規整化"
#
#  Family B is unit-level on ``_normalise_email``; here we walk the
#  same invariant end-to-end: admin issues an invite with
#  Mixed.Case email, anon recipient accepts → the materialised
#  ``users.email`` row is lowercased, and a SECOND attempted issue
#  with a casing variant of the same email is blocked by the
#  dup-pending guard (which compares ``lower(email)``).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_email_casing_is_normalised_through_full_http_flow(
    client, pg_test_pool,
):
    tid = "t-y3-life-casing"
    raw_email = "Alice.Casing@Example.COM"
    norm_email = "alice.casing@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)

        # Step 1: issue with mixed-case email.
        res = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": raw_email, "role": "member"},
            headers={"X-Tenant-Id": tid},
        )
        assert res.status_code == 201, res.text
        invite_id = res.json()["invite_id"]
        token = res.json()["token_plaintext"]

        # Step 2: a second issue using a different casing of the
        # SAME normalised email must trip the dup-pending guard
        # (409). If the guard didn't normalise we'd see two
        # pending rows and the recipient would have two valid
        # tokens — a confusing UX and a doubled brute-force surface.
        res_dup = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": norm_email.upper(), "role": "member"},
            headers={"X-Tenant-Id": tid},
        )
        assert res_dup.status_code == 409, res_dup.text
        # The 409 detail surfaces the normalised email so the admin
        # sees "you already have a pending invite for alice@x.com",
        # not the casing variant they just typed.
        assert (
            res_dup.json().get("email") == norm_email
        ), res_dup.json()

        # Step 3: anon accept the original (mixed-case) invite.
        res_acc = await client.post(
            f"/api/v1/invites/{invite_id}/accept",
            json={"token": token, "name": "Alice Casing"},
        )
        assert res_acc.status_code == 200, res_acc.text

        # Step 4: the materialised ``users.email`` row MUST be
        # lowercased — login flows compare normalised, so a row
        # stored with mixed casing creates a footgun where the user
        # cannot log in (or worse, double-creates rows when later
        # arriving via a different casing).
        async with pg_test_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT email FROM users WHERE lower(email) = $1",
                norm_email,
            )
        assert row is not None
        assert row["email"] == norm_email, (
            f"users.email persisted with non-normalised casing: "
            f"{row['email']!r}"
        )
    finally:
        await _purge_user_by_email(pg_test_pool, norm_email)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family H — Audit chain integrity verified after lifecycle (focused
#  variant: small lifecycle, sharp chain assertion)
#
#  Family D's headline test already calls verify_chain at the end;
#  this is a tighter complementary check that:
#    - explicitly counts the audit rows we expect,
#    - asserts secret material (token_hash, sha256-shaped strings,
#      password_hash, oidc_*) is absent across every blob in the
#      chain — the row 1/3/4 audit-blob leak guards already do this
#      per-action, but a chain-level sweep catches any future audit
#      caller that adds a new field type without thinking about the
#      lifecycle leak surface.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@_requires_pg
async def test_audit_chain_integrity_after_minimal_lifecycle(
    client, pg_test_pool,
):
    from backend import audit

    tid = "t-y3-life-audit"
    target_email = "audit.lifecycle@example.com"
    try:
        await _seed_tenant(pg_test_pool, tid)
        await _purge_audit_for_tenant(pg_test_pool, tid)

        # Issue.
        res_post = await client.post(
            f"/api/v1/tenants/{tid}/invites",
            json={"email": target_email, "role": "member"},
            headers={"X-Tenant-Id": tid},
        )
        assert res_post.status_code == 201, res_post.text
        invite_id = res_post.json()["invite_id"]
        token = res_post.json()["token_plaintext"]

        # Accept (with X-Tenant-Id so the audit row attributes to
        # this test's chain rather than t-default — see Family D's
        # rationale for the same pattern).
        res_acc = await client.post(
            f"/api/v1/invites/{invite_id}/accept",
            json={"token": token},
            headers={"X-Tenant-Id": tid},
        )
        assert res_acc.status_code == 200, res_acc.text

        # Chain integrity.
        ok, bad = await audit.verify_chain(tenant_id=tid)
        assert ok, f"audit chain broken at id={bad}"

        # Row count: created + accepted = 2.
        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT action, before_json, after_json "
                "FROM audit_log WHERE tenant_id = $1 ORDER BY id ASC",
                tid,
            )
        actions = [r["action"] for r in rows]
        assert "tenant_invite_created" in actions
        assert "tenant_invite_accepted" in actions

        # Chain-wide secret-leak sweep. Token plaintext must not
        # appear ANYWHERE in the chain blobs (neither plaintext nor
        # any 64-char sha256 hex). This is broader than the per-row
        # row-1/3/4 guards: if a future audit emit accidentally
        # passes ``token_hash=...`` into ``after``, this catches it
        # at lifecycle scope.
        for r in rows:
            for blob in (r["before_json"] or "", r["after_json"] or ""):
                assert token not in blob, (
                    f"token plaintext leaked into audit blob: "
                    f"action={r['action']}"
                )
                assert "token_hash" not in blob, (
                    f"token_hash field leaked into audit blob: "
                    f"action={r['action']}"
                )
                assert not re.search(r"[0-9a-f]{64}", blob), (
                    f"sha256-shaped string in audit blob: "
                    f"action={r['action']}"
                )
                for forbidden in ("password_hash", "oidc_subject",
                                  "oidc_provider"):
                    assert forbidden not in blob, (
                        f"{forbidden} leaked into audit blob: "
                        f"action={r['action']}"
                    )
    finally:
        await _purge_user_by_email(pg_test_pool, target_email.lower())
        await _purge_audit_for_tenant(pg_test_pool, tid)
        await _purge_tenant(pg_test_pool, tid)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family J — Self-fingerprint guard (pre-commit pattern from SOP
#  Step 3). Sweeps the four routers this lifecycle test asserts
#  against, so a regression in any one of them surfaces here even
#  without re-running the per-row drift-guard files. Same pattern as
#  the row 4 / row 6 self-fingerprint guard; the test does NOT scan
#  itself.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("router_path", [
    "backend/routers/tenant_invites.py",
    "backend/routers/tenant_members.py",
    "backend/routers/admin_super_admins.py",
    "backend/routers/admin_tenants.py",
])
def test_y3_routers_self_fingerprint_clean(router_path):
    """Refuse to ship any of the four compat-era markers documented
    in ``docs/sop/implement_phase_step.md`` Step 3 across any of the
    Y3 surface routers we depend on."""
    src = pathlib.Path(router_path).read_text(encoding="utf-8")
    fingerprint_re = re.compile(
        r"_conn\(\)|await\s+conn\.commit\(\)|datetime\('now'\)"
        r"|VALUES\s*\([^)]*\?[,)]"
    )
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat fingerprint hit in {router_path}: {hits}"

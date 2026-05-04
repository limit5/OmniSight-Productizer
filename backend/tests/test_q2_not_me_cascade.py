"""Q.2 (#296) — 「這不是我」cascade path.

When the new-device-login toast renders and the user decides the login
wasn't them, the frontend calls ``DELETE /auth/sessions/{token_hint}
?cascade=not_me``. The backend must:

  1. Revoke the flagged session (same as default path).
  2. Rotate every OTHER session on the account via the Q.1 path
     (``reason=user_security_event, trigger=not_me_cascade``) — no
     exclude_token, so the caller's own device also gets rotated.
  3. Flip ``users.must_change_password = 1`` so the K1 428 gate forces
     the password change on the very next authenticated call after
     re-login.
  4. Clear the caller's own session + CSRF cookies so the browser
     drops the dead token immediately.

Covered here:
  * cascade=not_me end-to-end: every session rotated, MCP flag flipped,
    cookies cleared, response body carries cascade metadata.
  * default path (no cascade) unchanged — single session revoked,
    other sessions untouched, MCP flag untouched, cookies untouched.
  * 404 on unknown token_hint must NOT run the cascade (no accidental
    session nuking on a typo).
  * cascade against another user's token is refused (403) so an admin
    can't use this self-service button to blast a peer's account.
  * Resilience: cascade completes even when there are zero peer
    sessions — rotate_user_sessions returns 0 without erroring, the
    MCP flag still flips, and the response reflects rotated_count=0.

Follows the ``_auth_db`` + direct-handler pattern from the Q.1 peer-
rotation suite — avoids the unrelated ``client`` fixture lifespan bug
that currently breaks full HTTP integration tests. The cookie contract
is verified via a ``_FakeResponse`` double that captures delete_cookie
calls (cookie-setting contract proper lives in test_auth.py K4).
"""

from __future__ import annotations

import time
import types

import pytest


@pytest.fixture()
async def _auth_db(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
    from backend import db, auth
    try:
        yield (db, auth)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")


def _make_request(session_token: str | None = None, user_agent: str = "UA-test"):
    cookies = {}
    if session_token:
        cookies["omnisight_session"] = session_token
    return types.SimpleNamespace(
        cookies=cookies,
        headers={"user-agent": user_agent},
        client=None,
    )


class _FakeResponse:
    """Minimal Response double capturing set_cookie / delete_cookie so
    we can assert the cascade cleared the caller's cookies without
    needing a real Starlette Response."""

    def __init__(self):
        self.cookies_set: list[tuple[str, str]] = []
        self.cookies_deleted: list[str] = []

    def set_cookie(self, key: str, value: str, **_kwargs):
        self.cookies_set.append((key, value))

    def delete_cookie(self, key: str, **_kwargs):
        self.cookies_deleted.append(key)


# ──────────────────────────────────────────────────────────────────
#  cascade=not_me end-to-end
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_me_cascade_rotates_all_sessions_and_flips_mcp(_auth_db):
    """Full cascade path: target session deleted, every other session
    rotated, must_change_password flipped, cookies cleared, response
    carries cascade metadata."""
    _, auth = _auth_db
    u = await auth.create_user(
        "alice@example.com", "Alice", role="operator",
        password="oldpass-123456-aa",
    )
    # Reload to the fresh DB state — create_user returns with MCP=0
    # already; the cascade must flip it to True.
    assert (await auth.get_user(u.id)).must_change_password is False

    laptop = await auth.create_session(
        u.id, ip="1.1.1.1", user_agent="Laptop",
    )
    phone = await auth.create_session(
        u.id, ip="2.2.2.2", user_agent="Phone",
    )
    # The "new device" the user is flagging as suspicious.
    bad_device = await auth.create_session(
        u.id, ip="203.0.113.42", user_agent="Unknown-new-device",
    )
    bad_hint = auth.session_token_hint(bad_device.token)

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    result = await revoke_session(
        token_hint=bad_hint,
        request=request, response=response,
        cascade="not_me", user=u,
    )

    # Response contract
    assert result["status"] == "revoked"
    assert result["token_hint"] == bad_hint
    assert result["cascade"] == "not_me"
    assert result["must_change_password"] is True
    # rotated_count reflects sessions left after the target DELETE — so
    # laptop + phone = 2 are the rotation targets (bad_device is gone
    # by the time rotate_user_sessions runs).
    assert result["rotated_count"] == 2, (
        f"cascade should rotate the 2 remaining sessions, got "
        f"rotated_count={result['rotated_count']}"
    )

    # Target session row actually deleted.
    assert await auth.get_session(bad_device.token) is None

    # Peer sessions expires_at shortened to the 30s grace window.
    now = time.time()
    laptop_after = await auth.get_session(laptop.token)
    phone_after = await auth.get_session(phone.token)
    assert laptop_after is not None, "within grace window — still findable"
    assert phone_after is not None
    assert laptop_after.expires_at <= now + auth.ROTATION_GRACE_S + 5, (
        "laptop (caller's own session) must be rotated too — cascade "
        "uses exclude_token=None on purpose"
    )
    assert phone_after.expires_at <= now + auth.ROTATION_GRACE_S + 5

    # must_change_password flipped to 1 — the 428 gate picks it up on
    # the next authenticated request post re-login.
    reloaded = await auth.get_user(u.id)
    assert reloaded.must_change_password is True

    # Caller's own cookies cleared so the browser drops the dead token
    # before the UI navigates to /login.
    assert auth.SESSION_COOKIE in response.cookies_deleted
    assert auth.CSRF_COOKIE in response.cookies_deleted


@pytest.mark.asyncio
async def test_default_delete_does_not_trigger_cascade(_auth_db):
    """Legacy behaviour: DELETE without ``cascade`` revokes only the
    target row. Other sessions stay active, MCP flag stays 0, no
    cookie clearing."""
    _, auth = _auth_db
    u = await auth.create_user(
        "bob@example.com", "Bob", role="operator",
        password="oldpass-654321-aa",
    )
    laptop = await auth.create_session(u.id, user_agent="Laptop")
    phone = await auth.create_session(u.id, user_agent="Phone")
    phone_hint = auth.session_token_hint(phone.token)

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    result = await revoke_session(
        token_hint=phone_hint,
        request=request, response=response,
        cascade=None, user=u,
    )

    # Response shape = legacy
    assert result == {"status": "revoked", "token_hint": phone_hint}

    # Phone gone, laptop still full-TTL alive.
    assert await auth.get_session(phone.token) is None
    laptop_after = await auth.get_session(laptop.token)
    assert laptop_after is not None
    assert laptop_after.expires_at > time.time() + auth.ROTATION_GRACE_S + 3600, (
        "legacy revoke must NOT shrink other sessions into the grace window"
    )

    # MCP flag untouched.
    assert (await auth.get_user(u.id)).must_change_password is False
    # Cookies untouched (no delete_cookie calls).
    assert response.cookies_deleted == []


@pytest.mark.asyncio
async def test_not_me_cascade_404_does_not_mutate_account(_auth_db):
    """A typo / double-click race that lands on a stale token_hint must
    404 BEFORE the cascade runs — we do not want a 404 to silently
    rotate every session or flip must_change_password."""
    from fastapi import HTTPException

    _, auth = _auth_db
    u = await auth.create_user(
        "carol@example.com", "Carol", role="operator",
        password="ca-pass-123456",
    )
    laptop = await auth.create_session(u.id, user_agent="Laptop")

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    with pytest.raises(HTTPException) as exc_info:
        await revoke_session(
            token_hint="nope***none",
            request=request, response=response,
            cascade="not_me", user=u,
        )
    assert exc_info.value.status_code == 404

    # Laptop still alive, MCP still 0, cookies not cleared.
    laptop_after = await auth.get_session(laptop.token)
    assert laptop_after is not None
    assert laptop_after.expires_at > time.time() + auth.ROTATION_GRACE_S + 3600
    assert (await auth.get_user(u.id)).must_change_password is False
    assert response.cookies_deleted == []


@pytest.mark.asyncio
async def test_not_me_cascade_refuses_other_users_session(_auth_db):
    """An admin invoking cascade against a peer user's session gets 403.
    Cascade is self-service only — the right admin primitive for
    disabling a peer is ``PATCH /users/{id}`` with enabled=False,
    which already triggers its own rotation via routers/auth.py:542."""
    from fastapi import HTTPException

    _, auth = _auth_db
    admin = await auth.create_user(
        "admin@example.com", "Admin", role="admin",
        password="ad-pass-123456",
    )
    peer = await auth.create_user(
        "peer@example.com", "Peer", role="operator",
        password="pe-pass-123456",
    )
    admin_sess = await auth.create_session(admin.id, user_agent="Admin-console")
    peer_sess = await auth.create_session(peer.id, user_agent="Peer-laptop")
    peer_hint = auth.session_token_hint(peer_sess.token)

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=admin_sess.token)
    response = _FakeResponse()
    with pytest.raises(HTTPException) as exc_info:
        await revoke_session(
            token_hint=peer_hint,
            request=request, response=response,
            cascade="not_me", user=admin,
        )
    assert exc_info.value.status_code == 403
    assert "cascade" in str(exc_info.value.detail).lower() \
        or "self-service" in str(exc_info.value.detail).lower()

    # Peer's MCP flag is NOT flipped — admin's cascade attempt had no
    # side effect on the peer user's row.
    assert (await auth.get_user(peer.id)).must_change_password is False
    # Peer's session still active.
    assert await auth.get_session(peer_sess.token) is not None


@pytest.mark.asyncio
async def test_not_me_cascade_with_only_target_session(_auth_db):
    """Single-device compromise: only the flagged session exists. After
    DELETE the sessions table is empty; rotate_user_sessions finds
    zero rows to touch (rotated_count=0) but must_change_password
    still flips — so if the attacker creates a NEW session later
    (e.g. via stolen password), they'll be forced into the 428 gate."""
    _, auth = _auth_db
    u = await auth.create_user(
        "dan@example.com", "Dan", role="operator",
        password="dan-pass-123456",
    )
    only = await auth.create_session(
        u.id, ip="203.0.113.99", user_agent="OnlyDevice",
    )
    only_hint = auth.session_token_hint(only.token)

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=only.token)
    response = _FakeResponse()
    result = await revoke_session(
        token_hint=only_hint,
        request=request, response=response,
        cascade="not_me", user=u,
    )

    assert result["cascade"] == "not_me"
    assert result["rotated_count"] == 0, (
        "no peer sessions left after target DELETE → rotated_count=0 "
        "is the correct idempotent answer, NOT an error"
    )
    assert result["must_change_password"] is True
    assert (await auth.get_user(u.id)).must_change_password is True
    assert await auth.get_session(only.token) is None


@pytest.mark.asyncio
async def test_not_me_cascade_unknown_value_falls_through_to_legacy(_auth_db):
    """A client that sends ``cascade=bogus`` should NOT silently trigger
    the cascade — only the canonical ``cascade=not_me`` token opts in.
    Anything else falls through to the legacy single-session delete so
    typos don't accidentally nuke the account."""
    _, auth = _auth_db
    u = await auth.create_user(
        "erin@example.com", "Erin", role="operator",
        password="er-pass-123456",
    )
    laptop = await auth.create_session(u.id, user_agent="Laptop")
    phone = await auth.create_session(u.id, user_agent="Phone")
    phone_hint = auth.session_token_hint(phone.token)

    from backend.routers.auth import revoke_session
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    result = await revoke_session(
        token_hint=phone_hint,
        request=request, response=response,
        cascade="bogus-value", user=u,
    )

    # Legacy shape — no cascade keys in the response.
    assert "cascade" not in result
    # Laptop stayed at full TTL, MCP flag untouched.
    laptop_after = await auth.get_session(laptop.token)
    assert laptop_after is not None
    assert laptop_after.expires_at > time.time() + auth.ROTATION_GRACE_S + 3600
    assert (await auth.get_user(u.id)).must_change_password is False

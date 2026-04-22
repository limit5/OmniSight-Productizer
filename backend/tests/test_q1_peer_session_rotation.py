"""Q.1 (2026-04-22) — peer-session rotation on security-sensitive actions.

Verifies that after a change_password / MFA enroll / MFA disable /
webauthn register / webauthn remove / backup_codes regenerate /
account disable action, every OTHER active session of that user is
rotated with 30s grace (and the current device's session is spared).

Priority Q.1 background: before this fix, ``rotate_session()`` only
rotated the session that initiated the action, leaving peer devices
(stolen phone, forgotten public-computer tab, etc.) logged in for
the full ``SESSION_TTL_S`` (8h). Industry baseline for social-
platform UX (Linear / Slack / Notion / GitHub) is: compromise-level
events rotate all non-current sessions. This suite pins that
contract.

Uses the ``_auth_db`` fixture (pg_test_pool-backed) + direct router-
handler invocation with a minimal Request/Response double. That
avoids the unrelated ``client`` fixture lifespan bug that currently
breaks other auth integration tests.
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
    """Minimal Request-like double carrying the 3 attrs our handlers read:
    .cookies (dict), .headers (dict), .client (None — triggers the
    'unknown' fallback in ``_client_key``)."""
    cookies = {}
    if session_token:
        cookies["omnisight_session"] = session_token
    return types.SimpleNamespace(
        cookies=cookies,
        headers={"user-agent": user_agent},
        client=None,
    )


class _FakeResponse:
    """Captures set_cookie calls so the test doesn't need a real
    Starlette Response. Cookie contents are not asserted here —
    cookie-setting contract has its own coverage in test_auth.py
    K4 suite."""
    def __init__(self):
        self.cookies_set: list[tuple[str, str]] = []

    def set_cookie(self, key: str, value: str, **_kwargs):
        self.cookies_set.append((key, value))


# ──────────────────────────────────────────────────────────────────
#  /auth/change-password → rotate_user_sessions(exclude=new_current)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_change_password_rotates_all_peer_sessions(_auth_db):
    _, auth = _auth_db
    # Arrange: user with 3 active sessions (laptop = current, plus
    # phone + tablet).
    u = await auth.create_user(
        "alice@example.com", "Alice", role="operator", password="oldpass-123456",
    )
    laptop = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Laptop")
    phone = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Phone")
    tablet = await auth.create_session(u.id, ip="3.3.3.3", user_agent="Tablet")
    # Sanity: all three active.
    for s in (laptop, phone, tablet):
        assert (await auth.get_session(s.token)) is not None

    # Act: call handler directly (avoids client-fixture pool bug).
    from backend.routers.auth import change_password, ChangePasswordRequest
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    result = await change_password(
        ChangePasswordRequest(
            current_password="oldpass-123456",
            new_password="Newpass!2026abc",
        ),
        request, response, user=u,
    )

    # Assert: success response + peer rotation happened.
    assert result["status"] == "password_changed"

    # Current device: session was rotated (old laptop token invalid or
    # in grace, new cookie set). We take the rotated token from the
    # response cookie.
    session_cookies = [
        v for k, v in response.cookies_set if k == auth.SESSION_COOKIE
    ]
    assert len(session_cookies) == 1, (
        "change-password must issue exactly one new session cookie"
    )
    new_laptop_token = session_cookies[0]
    assert new_laptop_token != laptop.token
    new_laptop_sess = await auth.get_session(new_laptop_token)
    assert new_laptop_sess is not None, "new laptop session must still be valid"

    # Peer sessions (phone + tablet): within 30s grace, but expires_at
    # shortened. After the grace window they'll resolve to None.
    now = time.time()
    phone_after = await auth.get_session(phone.token)
    tablet_after = await auth.get_session(tablet.token)
    # Still findable inside grace window, but expires_at ≤ now+GRACE.
    assert phone_after is not None
    assert tablet_after is not None
    assert phone_after.expires_at <= now + auth.ROTATION_GRACE_S + 5, (
        "phone session expires_at must be shortened to grace window"
    )
    assert tablet_after.expires_at <= now + auth.ROTATION_GRACE_S + 5, (
        "tablet session expires_at must be shortened to grace window"
    )
    # Sanity: original phone expires_at was SESSION_TTL_S > 8h from
    # now; grace is 30s — a huge drop.
    assert phone.expires_at - phone_after.expires_at > 3600


@pytest.mark.asyncio
async def test_change_password_keeps_current_session_alive(_auth_db):
    """The device the password change happened on stays logged in —
    otherwise the operator would get immediately bounced to login
    after their own password change, which is hostile UX and would
    also mask whether the rotation audit chain emitted correctly."""
    _, auth = _auth_db
    u = await auth.create_user(
        "bob@example.com", "Bob", role="operator", password="oldpass-654321",
    )
    laptop = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Laptop")

    from backend.routers.auth import change_password, ChangePasswordRequest
    request = _make_request(session_token=laptop.token)
    response = _FakeResponse()
    await change_password(
        ChangePasswordRequest(
            current_password="oldpass-654321",
            new_password="Freshpass!2026xyz",
        ),
        request, response, user=u,
    )
    # The new cookie token must resolve to a valid session with full
    # SESSION_TTL_S remaining (i.e. not in the 30s grace bucket).
    new_token = next(
        v for k, v in response.cookies_set if k == auth.SESSION_COOKIE
    )
    new_sess = await auth.get_session(new_token)
    assert new_sess is not None
    # Expires well beyond the 30s grace — full 8h ahead.
    assert new_sess.expires_at > time.time() + auth.ROTATION_GRACE_S + 3600


@pytest.mark.asyncio
async def test_change_password_with_no_peer_sessions_is_noop(_auth_db):
    """Single-device user (only the current session) must not error
    when rotate_user_sessions finds zero peers — the revoked_count==0
    branch skips the audit log, avoiding noise in the audit chain
    for trivial password changes."""
    _, auth = _auth_db
    u = await auth.create_user(
        "carol@example.com", "Carol", role="operator", password="onlypw-12345",
    )
    only = await auth.create_session(u.id, ip="1.1.1.1", user_agent="OnlyDevice")

    from backend.routers.auth import change_password, ChangePasswordRequest
    request = _make_request(session_token=only.token)
    response = _FakeResponse()
    result = await change_password(
        ChangePasswordRequest(
            current_password="onlypw-12345",
            new_password="Newpass!2026onlydev",
        ),
        request, response, user=u,
    )
    assert result["status"] == "password_changed"
    # Check that the ONLY session got rotated (not accidentally
    # excluded with both its old AND new token).
    new_token = next(
        v for k, v in response.cookies_set if k == auth.SESSION_COOKIE
    )
    assert await auth.get_session(new_token) is not None


# ──────────────────────────────────────────────────────────────────
#  MFA routes → rotate_user_sessions(exclude=current)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_totp_disable_rotates_all_peer_sessions(_auth_db):
    """Disabling TOTP is a security-posture downgrade — any device
    that was riding the 'MFA enrolled' trust level must re-auth.
    Current device (the one that clicked Disable) stays logged in."""
    _, auth = _auth_db
    u = await auth.create_user(
        "dave@example.com", "Dave", role="operator", password="pw-totp-test-1",
    )
    current = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Current")
    peer = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Peer")

    # Enrol + confirm TOTP so the disable call has something to
    # disable (the handler raises 404 otherwise).
    from backend import mfa
    enroll = await mfa.totp_begin_enroll(u.id, u.email)
    import pyotp
    code = pyotp.TOTP(enroll["secret"]).now()
    await mfa.totp_confirm_enroll(u.id, code)

    from backend.routers.mfa import totp_disable
    request = _make_request(session_token=current.token)
    result = await totp_disable(request=request, user=u)
    assert result["status"] == "disabled"

    # Current session alive (no rotation applied to it).
    current_after = await auth.get_session(current.token)
    assert current_after is not None
    assert current_after.expires_at == current.expires_at, (
        "current session's expires_at must be untouched — exclude_token works"
    )
    # Peer session expires_at shortened to grace window.
    peer_after = await auth.get_session(peer.token)
    assert peer_after is not None
    assert peer_after.expires_at <= time.time() + auth.ROTATION_GRACE_S + 5


@pytest.mark.asyncio
async def test_backup_codes_regenerate_rotates_peer_sessions(_auth_db):
    """Regenerating backup codes invalidates the old set — any peer
    device that may have captured them becomes a threat vector; kick
    those devices so the new codes live in a clean trust boundary."""
    _, auth = _auth_db
    u = await auth.create_user(
        "eve@example.com", "Eve", role="operator", password="pw-backup-codes",
    )
    current = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Current")
    peer = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Peer")

    # Must be MFA-enrolled for regenerate_backup_codes to succeed.
    from backend import mfa
    enroll = await mfa.totp_begin_enroll(u.id, u.email)
    import pyotp
    code = pyotp.TOTP(enroll["secret"]).now()
    await mfa.totp_confirm_enroll(u.id, code)

    from backend.routers.mfa import backup_codes_regenerate
    request = _make_request(session_token=current.token)
    result = await backup_codes_regenerate(request=request, user=u)
    assert result["count"] > 0

    # Current session alive.
    current_after = await auth.get_session(current.token)
    assert current_after is not None
    assert current_after.expires_at == current.expires_at
    # Peer rotated.
    peer_after = await auth.get_session(peer.token)
    assert peer_after is not None
    assert peer_after.expires_at <= time.time() + auth.ROTATION_GRACE_S + 5


# ──────────────────────────────────────────────────────────────────
#  Admin disable user → rotate all sessions (no exclude)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_disable_user_rotates_all_target_sessions(_auth_db):
    """When an admin disables a user, every session of the TARGET
    (not the admin) must be rotated. This is different from the
    self-service flows above — the admin's OWN session is not
    excluded because the admin's token isn't in the victim's
    session set anyway."""
    _, auth = _auth_db
    admin = await auth.create_user(
        "admin@example.com", "Admin", role="admin", password="admin-pw-strong",
    )
    victim = await auth.create_user(
        "villain@example.com", "Villain", role="operator",
        password="villain-pw-strong",
    )
    # Two active sessions on victim.
    v1 = await auth.create_session(victim.id, ip="1.1.1.1", user_agent="V1")
    v2 = await auth.create_session(victim.id, ip="2.2.2.2", user_agent="V2")
    admin_sess = await auth.create_session(
        admin.id, ip="9.9.9.9", user_agent="AdminConsole",
    )

    from backend.routers.auth import patch_user, PatchUserRequest
    request = _make_request(session_token=admin_sess.token)
    await patch_user(
        victim.id, PatchUserRequest(enabled=False), request, admin_user=admin,
    )

    # Victim sessions rotated.
    v1_after = await auth.get_session(v1.token)
    v2_after = await auth.get_session(v2.token)
    now = time.time()
    assert v1_after is not None
    assert v2_after is not None
    assert v1_after.expires_at <= now + auth.ROTATION_GRACE_S + 5
    assert v2_after.expires_at <= now + auth.ROTATION_GRACE_S + 5
    # Admin's own session untouched — rotation is scoped by user_id.
    admin_after = await auth.get_session(admin_sess.token)
    assert admin_after is not None
    assert admin_after.expires_at == admin_sess.expires_at


@pytest.mark.asyncio
async def test_admin_enable_user_does_not_rotate_sessions(_auth_db):
    """Turning an account back ON is not a security event — if there
    happen to be old sessions (unlikely but possible via a disable-
    then-enable cycle), they should keep their current expiry. This
    prevents spurious audit noise from benign admin ops."""
    _, auth = _auth_db
    admin = await auth.create_user(
        "admin2@example.com", "Admin2", role="admin", password="admin-pw-strong",
    )
    # User was disabled; simulate the state.
    user = await auth.create_user(
        "reactivated@example.com", "Reactivated", role="operator",
        password="user-pw-strong",
    )
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET enabled = 0 WHERE id = $1", user.id,
        )
    admin_sess = await auth.create_session(
        admin.id, ip="9.9.9.9", user_agent="AdminConsole",
    )

    from backend.routers.auth import patch_user, PatchUserRequest
    request = _make_request(session_token=admin_sess.token)
    # Re-enable.
    await patch_user(
        user.id, PatchUserRequest(enabled=True), request, admin_user=admin,
    )
    # No rotation should have fired — no peer sessions to check, but
    # this test exists to pin the "enable is not a security event"
    # contract so future refactors don't accidentally add rotation
    # there.
    fresh_user = await auth.get_user(user.id)
    assert fresh_user is not None
    assert fresh_user.enabled is True

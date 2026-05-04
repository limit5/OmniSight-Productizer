"""Q.1 UI follow-up (2026-04-24) — session revocation probe + structured
401 on peer-device requests.

The main Q.1 suite (``test_q1_peer_session_rotation.py``) locks the
contract: password change / TOTP toggle / admin disable → every peer
session's ``expires_at`` is shortened to the 30 s grace window. This
suite locks the UX-facing half: after the peer session evicts, the
next request from that device must come back 401 with a *structured*
detail identifying the security event that kicked them, so the UI
can render a tailored "your password was changed on another device"
banner instead of a bare 401 toast.

Specifically we verify:
  1. ``rotate_user_sessions(reason="user_security_event", trigger=...)``
     logs each affected token into ``session_revocations`` with
     ``reason`` + ``trigger``.
  2. ``get_session_revocation(token)`` returns that record.
  3. Outside the report window (``SESSION_REVOCATION_REPORT_WINDOW_S``)
     the probe returns ``None`` so stale records don't leak into
     fresh 401s.
  4. ``current_user`` raises ``HTTPException(401, detail=dict)`` with
     ``{reason, trigger, message}`` when the cookie points at a
     revoked token whose live session row is already evicted.
  5. The ``exclude_token`` (the current device that performed the
     action) is NOT logged — the initiator must not see itself as
     "revoked for a security event".
"""

from __future__ import annotations

import time
import types

import pytest
from fastapi import HTTPException


@pytest.fixture()
async def _auth_db(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_revocations")
    from backend import db, auth
    try:
        yield (db, auth)
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE session_revocations")


def _make_request(session_token: str | None = None, user_agent: str = "UA-test",
                  method: str = "POST"):
    cookies = {}
    if session_token:
        cookies["omnisight_session"] = session_token
    return types.SimpleNamespace(
        cookies=cookies,
        headers={"user-agent": user_agent},
        client=None,
        method=method,
        state=types.SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_rotate_user_sessions_writes_revocation_log(_auth_db):
    """When ``reason`` is passed, every non-excluded peer token gets
    a row in ``session_revocations`` with the right ``reason`` +
    ``trigger``. The excluded token does NOT appear — the device
    that initiated the action shouldn't see itself kicked."""
    _, auth = _auth_db
    u = await auth.create_user(
        "zelda@example.com", "Zelda", role="operator", password="oldpass-qq-111",
    )
    laptop = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Laptop")
    phone = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Phone")
    tablet = await auth.create_session(u.id, ip="3.3.3.3", user_agent="Tablet")

    revoked = await auth.rotate_user_sessions(
        u.id, exclude_token=laptop.token,
        reason="user_security_event", trigger="password_change",
    )
    assert revoked == 2  # phone + tablet

    # The current device (laptop) must NOT have a revocation record.
    rev_laptop = await auth.get_session_revocation(laptop.token)
    assert rev_laptop is None, (
        "initiator device must not be in session_revocations — it "
        "would see its own password change as a peer event."
    )

    # Peers: both get records with the right reason + trigger.
    rev_phone = await auth.get_session_revocation(phone.token)
    rev_tablet = await auth.get_session_revocation(tablet.token)
    assert rev_phone is not None
    assert rev_tablet is not None
    for rev in (rev_phone, rev_tablet):
        assert rev["reason"] == "user_security_event"
        assert rev["trigger"] == "password_change"
        assert rev["user_id"] == u.id
        assert rev["revoked_at"] > 0


@pytest.mark.asyncio
async def test_rotate_user_sessions_no_reason_no_log(_auth_db):
    """When ``reason`` is omitted (legacy callers, admin bulk-logout
    without a security semantic), the revocation log stays empty.
    This keeps the log limited to security-event rotations that
    actually need UX copy."""
    _, auth = _auth_db
    u = await auth.create_user(
        "yoshi@example.com", "Yoshi", role="operator", password="ypass-111-qq",
    )
    phone = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Phone")
    await auth.rotate_user_sessions(u.id, exclude_token=None)
    assert await auth.get_session_revocation(phone.token) is None


@pytest.mark.asyncio
async def test_revocation_record_expires_after_report_window(_auth_db):
    """Records older than ``SESSION_REVOCATION_REPORT_WINDOW_S`` return
    ``None`` from the probe — we don't want to surface "your password
    was changed 30 days ago" on a fresh 401."""
    _, auth = _auth_db
    u = await auth.create_user(
        "xena@example.com", "Xena", role="operator", password="xpass-qq-111",
    )
    peer = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Peer")
    await auth.rotate_user_sessions(
        u.id, exclude_token=None,
        reason="user_security_event", trigger="password_change",
    )
    # Sanity: within window → returns record.
    assert await auth.get_session_revocation(peer.token) is not None
    # Push the recorded time past the window and re-probe.
    from backend.db_pool import get_pool
    stale = time.time() - (auth.SESSION_REVOCATION_REPORT_WINDOW_S + 60)
    async with get_pool().acquire() as conn:
        # FX.11.2: ``session_revocations.token`` now stores the
        # sha256 lookup hash propagated from
        # ``sessions.token_lookup_index`` — direct DB pokes must hash
        # the plaintext cookie token before lookup.
        await conn.execute(
            "UPDATE session_revocations SET revoked_at = $1 "
            "WHERE token = $2",
            stale, auth._token_lookup_hash(peer.token),
        )
    assert await auth.get_session_revocation(peer.token) is None, (
        "probe must not report records older than the report window"
    )


@pytest.mark.asyncio
async def test_current_user_returns_structured_401_after_revocation(_auth_db):
    """Peer device's next request (after grace) carries the old cookie,
    which no longer resolves to a session. ``current_user`` must raise
    HTTPException(401, detail={reason, trigger, message}) so the UI
    can route to /login with the right banner."""
    _, auth = _auth_db
    u = await auth.create_user(
        "walt@example.com", "Walt", role="operator", password="wpass-qq-111",
    )
    laptop = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Laptop")
    phone = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Phone")
    await auth.rotate_user_sessions(
        u.id, exclude_token=laptop.token,
        reason="user_security_event", trigger="password_change",
    )
    # Force the peer session past its grace window so ``get_session``
    # evicts it and ``current_user`` lands on the revocation probe.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # FX.11.2: lookup-index keyed UPDATE.
        await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE token_lookup_index = $2",
            time.time() - 1, auth._token_lookup_hash(phone.token),
        )

    # Ensure session/strict auth mode is active so ``current_user``
    # actually 401s rather than falling back to anonymous-admin.
    import os
    os.environ["OMNISIGHT_AUTH_MODE"] = "strict"

    request = _make_request(session_token=phone.token, method="POST")
    with pytest.raises(HTTPException) as excinfo:
        await auth.current_user(request)
    exc = excinfo.value
    assert exc.status_code == 401
    assert isinstance(exc.detail, dict), (
        f"expected structured detail, got {type(exc.detail).__name__}: "
        f"{exc.detail!r}"
    )
    assert exc.detail["reason"] == "user_security_event"
    assert exc.detail["trigger"] == "password_change"
    assert "password was changed" in (exc.detail.get("message") or "").lower()


@pytest.mark.asyncio
async def test_current_user_generic_401_when_no_revocation(_auth_db):
    """If the cookie doesn't match any revocation record (session
    just aged out naturally), ``current_user`` falls back to the
    historical plain-string detail. This keeps the generic case
    lean — no churn for every stale cookie."""
    _, auth = _auth_db
    u = await auth.create_user(
        "ursula@example.com", "Ursula", role="operator", password="upass-qq-111",
    )
    sess = await auth.create_session(u.id, ip="1.1.1.1", user_agent="X")
    # Age the session out without recording a revocation.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # FX.11.2: lookup-index keyed UPDATE.
        await conn.execute(
            "UPDATE sessions SET expires_at = $1 "
            "WHERE token_lookup_index = $2",
            time.time() - 1, auth._token_lookup_hash(sess.token),
        )
    import os
    os.environ["OMNISIGHT_AUTH_MODE"] = "strict"

    request = _make_request(session_token=sess.token, method="POST")
    with pytest.raises(HTTPException) as excinfo:
        await auth.current_user(request)
    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.detail == "Authentication required"


@pytest.mark.asyncio
async def test_change_password_marks_peers_not_initiator(_auth_db):
    """End-to-end: actually call the change-password router handler
    and verify (a) the initiator's new session token is NOT in the
    revocation log, (b) peer tokens ARE, with trigger=password_change.
    """
    _, auth = _auth_db
    u = await auth.create_user(
        "vera@example.com", "Vera", role="operator",
        password="old-vera-111",
    )
    laptop = await auth.create_session(u.id, ip="1.1.1.1", user_agent="Laptop")
    phone = await auth.create_session(u.id, ip="2.2.2.2", user_agent="Phone")

    from backend.routers.auth import change_password, ChangePasswordRequest

    class _Resp:
        def __init__(self):
            self.cookies_set = []

        def set_cookie(self, key, value, **_):
            self.cookies_set.append((key, value))

    req = _make_request(session_token=laptop.token)
    resp = _Resp()
    await change_password(
        ChangePasswordRequest(
            current_password="old-vera-111",
            new_password="Freshpass!9999abc",
        ),
        req, resp, user=u,
    )
    new_laptop_token = next(
        v for k, v in resp.cookies_set if k == auth.SESSION_COOKIE
    )
    # The new (rotated) laptop token is the "current device" post-
    # change. It must not appear in the revocation log — otherwise
    # the operator would see their own device flagged on next
    # request.
    assert await auth.get_session_revocation(new_laptop_token) is None
    # The phone is the peer and MUST be flagged with trigger=password_change.
    rev = await auth.get_session_revocation(phone.token)
    assert rev is not None
    assert rev["reason"] == "user_security_event"
    assert rev["trigger"] == "password_change"

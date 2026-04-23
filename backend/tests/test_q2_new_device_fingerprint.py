"""Q.2 device-fingerprint history (2026-04-24) — ``create_session`` must
consult the ``session_fingerprints`` table and flag the returned
session with ``is_new_device=True`` when ``(user_id, ua_hash,
ip_subnet_/24)`` has not been observed within the past 30 days.

The downstream Q.2 alert (email + SSE ``security.new_device_login``)
is a separate checkbox; this suite locks the primitive it depends on.

Covered:
  1. First-ever session for a user → is_new_device=True; subsequent
     session with the same fingerprint → False (de-dupe).
  2. Different UA on the same /24 → is_new_device=True (legit new
     browser); different /24 with the same UA → is_new_device=True
     (travel / new ISP).
  3. Same /24 different host-octet (``1.2.3.42`` vs ``1.2.3.99``) →
     collapsed to one fingerprint, second session → is_new_device=False
     (DHCP-lease tolerance per Q.2 spec).
  4. Record older than ``FINGERPRINT_LOOKBACK_S`` → re-login treated
     as new again (30-day cutoff).
  5. Empty / malformed IP collapses to a single bucket — two empty-IP
     logins with the same UA don't double-alert.
  6. The public ``fingerprint_seen_before`` probe matches the internal
     decision (symmetric with ``_create_session_impl``).
  7. IPv6: /64 prefix is the collapse unit, not the full address.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
async def _auth_db(pg_test_pool):
    """Clean start: empty ``users``, ``sessions``, ``session_fingerprints``.

    CASCADE via ``users`` wipes sessions; fingerprints live in their
    own table and need an explicit TRUNCATE.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_fingerprints")
    from backend import auth
    try:
        yield auth
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE session_fingerprints")


@pytest.mark.asyncio
async def test_first_session_flags_new_device(_auth_db):
    auth = _auth_db
    u = await auth.create_user(
        "alpha@example.com", "Alpha", role="operator",
        password="alpha-qqq-111",
    )
    sess = await auth.create_session(
        u.id, ip="10.0.0.42",
        user_agent="Mozilla/5.0 (Laptop)",
    )
    assert sess.is_new_device is True, (
        "first session for a brand-new (user, UA, /24) tuple must "
        "flag is_new_device=True so Q.2 alert fires"
    )


@pytest.mark.asyncio
async def test_same_fingerprint_second_session_not_new(_auth_db):
    auth = _auth_db
    u = await auth.create_user(
        "bravo@example.com", "Bravo", role="operator",
        password="bravo-qqq-111",
    )
    s1 = await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    s2 = await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert s1.is_new_device is True
    assert s2.is_new_device is False, (
        "same fingerprint within the 30d window must de-dupe — no "
        "repeat alert on re-login from the same browser + subnet"
    )


@pytest.mark.asyncio
async def test_different_ua_same_subnet_flags_new(_auth_db):
    auth = _auth_db
    u = await auth.create_user(
        "charlie@example.com", "Charlie", role="operator",
        password="charlie-qqq-111",
    )
    await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    sess = await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Phone)",
    )
    assert sess.is_new_device is True, (
        "new UA on the same network is still a new device"
    )


@pytest.mark.asyncio
async def test_different_subnet_same_ua_flags_new(_auth_db):
    auth = _auth_db
    u = await auth.create_user(
        "delta@example.com", "Delta", role="operator",
        password="delta-qqq-111",
    )
    await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    sess = await auth.create_session(
        u.id, ip="192.168.5.99", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert sess.is_new_device is True, (
        "same browser but new network (travel, new ISP) is a new device"
    )


@pytest.mark.asyncio
async def test_dhcp_churn_within_24_collapses_to_one_fingerprint(_auth_db):
    """Per Q.2 spec: same /24 is treated as one device — we don't
    re-alert just because DHCP rotated the host octet."""
    auth = _auth_db
    u = await auth.create_user(
        "echo@example.com", "Echo", role="operator",
        password="echo-qqq-111",
    )
    s1 = await auth.create_session(
        u.id, ip="203.0.113.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    s2 = await auth.create_session(
        u.id, ip="203.0.113.201", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert s1.is_new_device is True
    assert s2.is_new_device is False, (
        "host-octet change within the same /24 must NOT re-flag — "
        "DHCP lease churn tolerance"
    )


@pytest.mark.asyncio
async def test_record_older_than_30d_is_treated_as_new(_auth_db, pg_test_pool):
    """A login from a fingerprint last seen 31 days ago re-triggers
    the new-device flag — the alert is opt-in for anything over the
    30-day window."""
    auth = _auth_db
    u = await auth.create_user(
        "foxtrot@example.com", "Foxtrot", role="operator",
        password="foxtrot-qqq-111",
    )
    s1 = await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert s1.is_new_device is True
    # Age the recorded last_seen_at past the 30d cutoff.
    stale = time.time() - (auth.FINGERPRINT_LOOKBACK_S + 60)
    async with pg_test_pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE session_fingerprints SET last_seen_at = $1 "
            "WHERE user_id = $2",
            stale, u.id,
        )
        # Sanity: the aging UPDATE actually hit the row we just created.
        assert n.endswith(" 1"), f"expected 1 row updated, got {n!r}"
    s2 = await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert s2.is_new_device is True, (
        "fingerprint older than FINGERPRINT_LOOKBACK_S must be "
        "treated as a new device — the user may have forgotten the "
        "old session existed and a fresh alert is warranted"
    )


@pytest.mark.asyncio
async def test_empty_ip_collapses_consistently(_auth_db):
    """Sessions created with no IP (internal worker flow, tests, or
    a proxy that didn't forward X-Forwarded-For) must collapse into
    one bucket per UA — otherwise every such login would double-alert.
    """
    auth = _auth_db
    u = await auth.create_user(
        "golf@example.com", "Golf", role="operator",
        password="golf-qqq-111",
    )
    s1 = await auth.create_session(u.id, ip="", user_agent="UA-X")
    s2 = await auth.create_session(u.id, ip="", user_agent="UA-X")
    assert s1.is_new_device is True
    assert s2.is_new_device is False


@pytest.mark.asyncio
async def test_fingerprint_seen_before_probe_matches_create_session(_auth_db):
    """``fingerprint_seen_before`` is the read-only sibling of the
    internal check inside ``_create_session_impl``. They must agree
    so downstream Q.2 logic (notification dispatcher, rate limiter)
    can reason about fingerprints without re-entering create_session.
    """
    auth = _auth_db
    u = await auth.create_user(
        "hotel@example.com", "Hotel", role="operator",
        password="hotel-qqq-111",
    )
    ua_h = auth.compute_ua_hash("Mozilla/5.0 (Laptop)")
    subnet = auth.compute_ip_subnet("10.0.0.42")

    # Before any login: probe says "not seen".
    assert await auth.fingerprint_seen_before(u.id, ua_h, subnet) is False

    # After login: probe flips to "seen".
    await auth.create_session(
        u.id, ip="10.0.0.42", user_agent="Mozilla/5.0 (Laptop)",
    )
    assert await auth.fingerprint_seen_before(u.id, ua_h, subnet) is True


@pytest.mark.asyncio
async def test_compute_ip_subnet_v4_and_v6():
    """Unit: the subnet collapse must be /24 for IPv4 and /64 for
    IPv6. Port suffixes are stripped. Bracketed IPv6 with port too.
    Unparseable input returns ''."""
    from backend.auth import compute_ip_subnet

    # IPv4: /24 prefix (3 octets).
    assert compute_ip_subnet("10.0.0.42") == "10.0.0"
    assert compute_ip_subnet("192.168.5.99") == "192.168.5"
    assert compute_ip_subnet("10.0.0.42:5432") == "10.0.0"
    # Same /24 with different host octets collapse identically.
    assert compute_ip_subnet("203.0.113.42") == compute_ip_subnet("203.0.113.201")

    # IPv6: /64 prefix (4 groups of 16 bits in exploded form).
    v6 = compute_ip_subnet("2001:db8:85a3:8a2e:370:7334:0:1")
    assert v6 == "2001:0db8:85a3:8a2e", (
        f"IPv6 /64 collapse wrong: {v6!r}"
    )
    # Bracketed + port.
    assert compute_ip_subnet("[2001:db8:85a3:8a2e::1]:443") == "2001:0db8:85a3:8a2e"
    # Different /64 → different collapse.
    other = compute_ip_subnet("2001:db8:85a3:ffff::1")
    assert other != v6

    # Empty / malformed.
    assert compute_ip_subnet("") == ""
    assert compute_ip_subnet("not-an-ip") == ""
    assert compute_ip_subnet("999.999.999.999") == ""

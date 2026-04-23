"""Q.2 (#296) — new-device-alert rate-limit + DHCP-tolerance gates.

Layer sandwiched between the fingerprint primitive
(``test_q2_new_device_fingerprint``) and the fan-out wiring
(``test_q2_new_device_alert``): once the fingerprint layer says "this
is a new device", two gates inside ``notify_new_device_login`` decide
whether the alert is actually delivered.

  * **Per-user 1/min (anti-spam)** — a burst of logins from many
    subnets / UAs must not flood the user with alerts. At most one
    alert per user per minute; subsequent ones in the same minute
    are silently dropped. Spec: "同 user 每分鐘最多發一則新裝置通知".
  * **Per-(user, subnet) 24h (DHCP tolerance)** — once a user has been
    alerted about a given /24 (IPv4) or /64 (IPv6) prefix, we won't
    re-alert for that prefix for 24h, even if the UA hash changes
    (browser update, second browser on same laptop, DHCP lease
    churn). Spec: "同一 IP subnet 24h 內視為同裝置".

Both gates ride on the shared ``backend.rate_limit.get_limiter()``
token-bucket primitive, so they work in-memory in tests and
coordinate across workers via Redis in prod.
"""

from __future__ import annotations

import time

import pytest

from backend.rate_limit import reset_limiters


@pytest.fixture()
async def _rl_env(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_fingerprints")

    # Both gates share the module-singleton limiter; each test starts
    # from a cold cache so prior tests can't pre-consume a token and
    # mask a bug.
    reset_limiters()

    from backend import auth, events as _events, notifications

    captured_emits: list[dict] = []
    captured_notifies: list[dict] = []

    real_emit = _events.emit_new_device_login

    def _record_emit(*args, **kwargs):
        captured_emits.append({"args": args, "kwargs": kwargs})
        real_emit(*args, **kwargs)

    monkeypatch.setattr(_events, "emit_new_device_login", _record_emit)

    async def _record_notify(**kwargs):
        captured_notifies.append(kwargs)

        class _Notif:
            id = "notif-test"

        return _Notif

    monkeypatch.setattr(notifications, "notify", _record_notify)

    try:
        yield {
            "auth": auth,
            "emits": captured_emits,
            "notifies": captured_notifies,
            "pool": pg_test_pool,
        }
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE session_fingerprints")
        reset_limiters()


def _make_session(user_id, token_suffix, subnet_ip, ua):
    """Build a bare ``Session`` flagged ``is_new_device=True`` without
    going through ``create_session`` — lets us drive the notify helper
    under arbitrary fingerprint states without racing the UPSERT."""
    from backend.auth import Session
    now = time.time()
    return Session(
        token=f"tok-{token_suffix}-padded-to-be-long-enough-x",
        user_id=user_id,
        csrf_token="csrf-x",
        created_at=now,
        expires_at=now + 3600,
        ip=subnet_ip,
        user_agent=ua,
        last_seen_at=now,
        is_new_device=True,
    )


@pytest.mark.asyncio
async def test_second_alert_within_60s_is_rate_limited(_rl_env):
    """Two new-device alerts for the same user within 60s: only the
    first fires. The second (different subnet AND different UA, so
    the fingerprint primitive DOES say is_new_device=True) is dropped
    by the per-user 1/min gate."""
    auth = _rl_env["auth"]
    u = await auth.create_user(
        "burst@example.com", "Burst", role="operator",
        password="burstpw-qq-111",
    )

    sess_a = _make_session(u.id, "A", "203.0.113.10", "UA/A")
    sess_b = _make_session(u.id, "B", "198.51.100.20", "UA/B")

    await auth.notify_new_device_login(u, sess_a, "203.0.113.10", "UA/A")
    assert len(_rl_env["emits"]) == 1, "first new-device alert must fire"
    assert len(_rl_env["notifies"]) == 1

    await auth.notify_new_device_login(u, sess_b, "198.51.100.20", "UA/B")
    assert len(_rl_env["emits"]) == 1, (
        "second alert within 60s must be swallowed by the per-user gate"
    )
    assert len(_rl_env["notifies"]) == 1


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_per_user(_rl_env):
    """The 1/min gate is keyed by user_id. A burst across two
    different users must deliver both alerts — we only suppress spam
    at the single-victim level."""
    auth = _rl_env["auth"]
    u1 = await auth.create_user(
        "alice@example.com", "Alice", role="operator",
        password="alicepw-qq-111",
    )
    u2 = await auth.create_user(
        "bob@example.com", "Bob", role="operator",
        password="bobpw-qq-111111",
    )

    s1 = _make_session(u1.id, "1", "203.0.113.1", "UA/X")
    s2 = _make_session(u2.id, "2", "203.0.113.1", "UA/X")

    await auth.notify_new_device_login(u1, s1, "203.0.113.1", "UA/X")
    await auth.notify_new_device_login(u2, s2, "203.0.113.1", "UA/X")
    assert len(_rl_env["emits"]) == 2, (
        "cross-user alerts must not share a rate-limit bucket"
    )


@pytest.mark.asyncio
async def test_rate_limit_window_tunable_via_monkeypatch(_rl_env, monkeypatch):
    """Shrinking the user-window to effectively zero proves the
    suppression is gate-driven, not a side effect of some other
    per-test state. A zero window means the bucket refills before the
    second allow() call, so the second alert fires."""
    auth = _rl_env["auth"]
    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_USER_WINDOW_S", 0.001)

    u = await auth.create_user(
        "short@example.com", "Short", role="operator",
        password="shortpw-qq-111",
    )
    s1 = _make_session(u.id, "1", "203.0.113.50", "UA/A")
    s2 = _make_session(u.id, "2", "198.51.100.70", "UA/B")

    await auth.notify_new_device_login(u, s1, "203.0.113.50", "UA/A")
    # Allow the token bucket to refill: with window=0.001s the rate
    # is 1000 tokens/s, so even one scheduler tick is enough.
    time.sleep(0.01)
    await auth.notify_new_device_login(u, s2, "198.51.100.70", "UA/B")
    assert len(_rl_env["emits"]) == 2, (
        "with the per-user window relaxed to ~0s, both alerts deliver"
    )


@pytest.mark.asyncio
async def test_same_subnet_within_24h_is_dedup_despite_new_ua(
    _rl_env, monkeypatch,
):
    """The DHCP-tolerance gate: once a user has been alerted about
    a /24, a second alert for the SAME /24 with a different UA
    (which the fingerprint primitive flags as a new device) is
    swallowed.

    We relax the per-user 1/min window so the per-(user, subnet)
    gate is the only thing that could suppress — otherwise the user
    gate would block and we wouldn't actually exercise the subnet
    gate.
    """
    auth = _rl_env["auth"]
    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_USER_WINDOW_S", 0.001)

    u = await auth.create_user(
        "dhcp@example.com", "DHCP", role="operator",
        password="dhcppw-qq-11111",
    )
    # First login: Chrome on /24 = 203.0.113.x
    s1 = _make_session(u.id, "1", "203.0.113.5", "Chrome/120")
    await auth.notify_new_device_login(u, s1, "203.0.113.5", "Chrome/120")
    assert len(_rl_env["emits"]) == 1

    time.sleep(0.01)  # let the per-user bucket refill
    # Second login: SAME /24 but a different UA (browser update /
    # Firefox on same laptop) → fingerprint primitive says
    # is_new_device=True; our gate MUST suppress.
    s2 = _make_session(u.id, "2", "203.0.113.99", "Firefox/123")
    await auth.notify_new_device_login(u, s2, "203.0.113.99", "Firefox/123")
    assert len(_rl_env["emits"]) == 1, (
        "same /24 within 24h must not re-alert even with a new UA"
    )


@pytest.mark.asyncio
async def test_different_subnet_bypasses_subnet_dedup(
    _rl_env, monkeypatch,
):
    """The dedup is keyed by (user, subnet). A login from a
    genuinely different /24 must still deliver (subject to the
    per-user 1/min gate, which we relax here to isolate the subnet
    dimension)."""
    auth = _rl_env["auth"]
    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_USER_WINDOW_S", 0.001)

    u = await auth.create_user(
        "travel@example.com", "Travel", role="operator",
        password="travelpw-qq-111",
    )
    s1 = _make_session(u.id, "1", "203.0.113.5", "Chrome/120")
    await auth.notify_new_device_login(u, s1, "203.0.113.5", "Chrome/120")
    assert len(_rl_env["emits"]) == 1

    time.sleep(0.01)
    # Genuinely different /24 (203.0.113.0/24 → 198.51.100.0/24 —
    # different network per RFC 5737 TEST-NET-3 vs TEST-NET-2)
    s2 = _make_session(u.id, "2", "198.51.100.8", "Chrome/120")
    await auth.notify_new_device_login(u, s2, "198.51.100.8", "Chrome/120")
    assert len(_rl_env["emits"]) == 2, (
        "different /24 must fire its own alert"
    )


@pytest.mark.asyncio
async def test_subnet_dedup_window_tunable_proves_fresh_alert_after_expiry(
    _rl_env, monkeypatch,
):
    """After the per-(user, subnet) window elapses, the same /24
    re-alerts. We simulate this by shrinking the window to ~0 so
    the second call sees a refilled bucket."""
    auth = _rl_env["auth"]
    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_USER_WINDOW_S", 0.001)
    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_SUBNET_WINDOW_S", 0.001)

    u = await auth.create_user(
        "roll@example.com", "Roll", role="operator",
        password="rollpw-qq-1111",
    )
    s1 = _make_session(u.id, "1", "203.0.113.5", "Chrome/120")
    await auth.notify_new_device_login(u, s1, "203.0.113.5", "Chrome/120")
    assert len(_rl_env["emits"]) == 1

    time.sleep(0.02)
    s2 = _make_session(u.id, "2", "203.0.113.200", "Firefox/123")
    await auth.notify_new_device_login(u, s2, "203.0.113.200", "Firefox/123")
    assert len(_rl_env["emits"]) == 2, (
        "window elapsed → same /24 re-alerts"
    )


@pytest.mark.asyncio
async def test_unparseable_ip_skips_subnet_gate_but_honors_user_gate(
    _rl_env,
):
    """Empty / unparseable IP → subnet gate is skipped (see helper
    docstring: we don't want every user's "unknown IP" logins to
    collapse into one dedup bucket). The per-user 1/min gate still
    applies, so two back-to-back empty-IP alerts for the same user
    see the second suppressed."""
    auth = _rl_env["auth"]

    u = await auth.create_user(
        "blank@example.com", "Blank", role="operator",
        password="blankpw-qq-111",
    )
    s1 = _make_session(u.id, "1", "", "UA/A")
    await auth.notify_new_device_login(u, s1, "", "UA/A")
    assert len(_rl_env["emits"]) == 1

    s2 = _make_session(u.id, "2", "", "UA/B")
    await auth.notify_new_device_login(u, s2, "", "UA/B")
    # Per-user gate suppresses the immediate re-alert — empty IP or
    # not, the spam-prevention floor still holds.
    assert len(_rl_env["emits"]) == 1


@pytest.mark.asyncio
async def test_gate_helper_direct_contract(_rl_env):
    """Unit-level probe on ``_new_device_alert_should_fire``. Covers
    the three reason codes (user / subnet / allow) without going
    through the full notify helper."""
    from backend import auth

    u_id = "user-direct-probe"

    # Cold: first probe delivers.
    ok, reason = auth._new_device_alert_should_fire(u_id, "203.0.113")
    assert ok is True and reason == ""

    # Second probe in the same minute → blocked at the user gate.
    ok, reason = auth._new_device_alert_should_fire(u_id, "198.51.100")
    assert ok is False and reason == "rate_limited_user"


@pytest.mark.asyncio
async def test_gate_helper_subnet_reason_isolated(
    _rl_env, monkeypatch,
):
    """With the per-user gate relaxed, repeated probes on the SAME
    subnet hit the subnet-reason branch, not the user-reason one."""
    from backend import auth

    monkeypatch.setattr(auth, "NEW_DEVICE_ALERT_USER_WINDOW_S", 0.001)

    u_id = "user-subnet-reason"

    ok, reason = auth._new_device_alert_should_fire(u_id, "203.0.113")
    assert ok is True and reason == ""

    time.sleep(0.01)
    ok, reason = auth._new_device_alert_should_fire(u_id, "203.0.113")
    assert ok is False and reason == "rate_limited_subnet", (
        "with user-gate relaxed, same-subnet must report subnet-specific reason"
    )

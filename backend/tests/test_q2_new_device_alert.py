"""Q.2 (#296) — new-device-login alert dispatch.

The fingerprint primitive is locked by ``test_q2_new_device_fingerprint``
— this suite covers the next layer: when ``Session.is_new_device`` is
True, ``auth.notify_new_device_login`` must fan out to:

  1. ``backend.events.emit_new_device_login`` → SSE event
     ``security.new_device_login`` (broadcast_scope=user) carrying
     ``user_id``, ``token_hint``, ``ip``, ``user_agent``.
  2. ``backend.notifications.notify`` → ``warning`` level, source
     ``auth.security``. The notify() module already routes warning+
     to Slack/Jira/PagerDuty per existing tier rules; we don't
     re-test routing here.

When ``is_new_device`` is False the helper is a strict no-op — a
duplicate login on a known fingerprint must NOT spam the user.

We also drive ``_create_session_impl`` end-to-end against the live
fingerprint table (login → MFA-less route would be the second site,
but here we exercise the bare ``create_session`` because the helper
takes a ``Session`` directly and its decision is Session-driven).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.events import bus as _bus


@pytest.fixture()
async def _q2_alert_env(pg_test_pool, monkeypatch):
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_fingerprints")

    # Q.2 rate-limit gates (per-user 60s + per-subnet 24h) share a
    # module-singleton limiter with the rest of the backend. Clear it
    # so state from a prior test doesn't suppress the alert this test
    # is trying to verify.
    from backend.rate_limit import reset_limiters
    reset_limiters()

    from backend import auth, events as _events, notifications

    captured_emits: list[dict] = []
    captured_notifies: list[dict] = []

    real_emit = _events.emit_new_device_login

    def _record_emit(*args, **kwargs):
        captured_emits.append({"args": args, "kwargs": kwargs})
        # Still call through so the SSE bus + log line happen — test
        # also verifies the bus payload via subscribe below.
        real_emit(*args, **kwargs)

    monkeypatch.setattr(_events, "emit_new_device_login", _record_emit)
    # auth.notify_new_device_login imports lazily, so patch the SAME
    # symbol on the auth module too if it were re-exported. We use
    # the late ``from backend.events import emit_new_device_login as
    # _emit`` form which resolves through ``backend.events`` namespace
    # at call time → patching events module is sufficient.

    async def _record_notify(**kwargs):
        captured_notifies.append(kwargs)

        class _Notif:
            id = "notif-test"
            level = kwargs.get("level", "info")
            title = kwargs.get("title", "")
            message = kwargs.get("message", "")
            source = kwargs.get("source", "")
            timestamp = "2026-04-24T00:00:00"
            action_url = kwargs.get("action_url")
            action_label = kwargs.get("action_label")

        return _Notif

    monkeypatch.setattr(notifications, "notify", _record_notify)

    try:
        yield {
            "auth": auth,
            "events": _events,
            "emits": captured_emits,
            "notifies": captured_notifies,
            "pool": pg_test_pool,
        }
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE session_fingerprints")
        reset_limiters()


@pytest.mark.asyncio
async def test_first_device_login_emits_and_notifies(_q2_alert_env):
    """Brand-new (user, UA, /24) tuple → both fan-outs fire."""
    auth = _q2_alert_env["auth"]
    u = await auth.create_user(
        "first@example.com", "First", role="operator",
        password="firstpw-qq-111",
    )
    sess = await auth.create_session(
        u.id, ip="203.0.113.42",
        user_agent="Mozilla/5.0 (Test-NewDevice)",
    )
    assert sess.is_new_device is True, (
        "fixture sanity: a brand-new tuple must flag is_new_device=True"
    )

    await auth.notify_new_device_login(
        u, sess, "203.0.113.42", "Mozilla/5.0 (Test-NewDevice)",
    )

    assert len(_q2_alert_env["emits"]) == 1, (
        "is_new_device=True must trigger exactly one SSE emit"
    )
    emit_kwargs = _q2_alert_env["emits"][0]["kwargs"]
    assert emit_kwargs["user_id"] == u.id
    assert emit_kwargs["ip"] == "203.0.113.42"
    assert emit_kwargs["user_agent"] == "Mozilla/5.0 (Test-NewDevice)"
    # token_hint is _mask_token(sess.token) = first4 + *** + last4
    expected_hint = sess.token[:4] + "***" + sess.token[-4:]
    assert emit_kwargs["token_hint"] == expected_hint
    # session_id passes through so future per-session UI affordances
    # (e.g. "this card belongs to your current device") have a stable
    # opaque key without exposing the cookie.
    assert emit_kwargs["session_id"], "session_id must be derived for SSE filter"

    assert len(_q2_alert_env["notifies"]) == 1, (
        "is_new_device=True must trigger exactly one notify() dispatch"
    )
    nkw = _q2_alert_env["notifies"][0]
    assert nkw["level"] == "warning", "Q.2 alert is L2 (warning) by spec"
    assert nkw["source"] == "auth.security"
    assert "新裝置登入" in nkw["title"]
    assert "203.0.113.42" in nkw["message"], (
        "operator must see the originating IP in the email/IM body"
    )
    assert nkw["action_url"] == "/settings/security"


@pytest.mark.asyncio
async def test_known_device_login_is_silent(_q2_alert_env):
    """is_new_device=False → strict no-op. Re-login on the same tuple
    inside the 30-day window must NOT spam the user."""
    auth = _q2_alert_env["auth"]
    u = await auth.create_user(
        "second@example.com", "Second", role="operator",
        password="secondpw-qq-111",
    )
    s1 = await auth.create_session(
        u.id, ip="198.51.100.10", user_agent="Mozilla/5.0 (Known)",
    )
    assert s1.is_new_device is True
    s2 = await auth.create_session(
        u.id, ip="198.51.100.10", user_agent="Mozilla/5.0 (Known)",
    )
    assert s2.is_new_device is False, (
        "fixture sanity: second login on same fingerprint must be False"
    )

    # Reset captures for the second login alone.
    _q2_alert_env["emits"].clear()
    _q2_alert_env["notifies"].clear()

    await auth.notify_new_device_login(
        u, s2, "198.51.100.10", "Mozilla/5.0 (Known)",
    )

    assert _q2_alert_env["emits"] == [], (
        "duplicate-fingerprint login must NOT emit a security SSE event"
    )
    assert _q2_alert_env["notifies"] == [], (
        "duplicate-fingerprint login must NOT dispatch a notification"
    )


@pytest.mark.asyncio
async def test_emit_payload_lands_on_event_bus_with_user_scope(_q2_alert_env):
    """The emitted SSE event has ``broadcast_scope='user'`` and the
    payload contains the fields the frontend filter relies on
    (``user_id``, ``token_hint``, ``ip``, ``user_agent``).

    Until Q.4 (#298) tightens server-side enforcement, the bus
    delivers user-scoped events to all local subscribers; the contract
    we lock here is the *payload shape* so the frontend filter is
    well-defined.
    """
    import json
    auth = _q2_alert_env["auth"]
    queue = _bus.subscribe(tenant_id=None)
    try:
        u = await auth.create_user(
            "third@example.com", "Third", role="operator",
            password="thirdpw-qq-111",
        )
        sess = await auth.create_session(
            u.id, ip="192.0.2.7",
            user_agent="Mozilla/5.0 (BusTest)",
        )
        await auth.notify_new_device_login(
            u, sess, "192.0.2.7", "Mozilla/5.0 (BusTest)",
        )
        # Drain non-matching events (e.g. a notification echoed by
        # the patched notify won't appear because we replaced the
        # function entirely).
        msg = None
        for _ in range(10):
            try:
                m = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if m.get("event") == "security.new_device_login":
                msg = m
                break
        assert msg is not None, "security.new_device_login must reach the bus"
        data = json.loads(msg["data"])
        assert data["user_id"] == u.id
        assert data["ip"] == "192.0.2.7"
        assert data["user_agent"] == "Mozilla/5.0 (BusTest)"
        assert data["_broadcast_scope"] == "user", (
            "Q.2 alert must carry broadcast_scope=user so Q.4 (#298) "
            "can enforce per-user delivery without a payload change"
        )
        assert data["token_hint"], "token_hint must be present for revoke flow"
    finally:
        _bus.unsubscribe(queue)


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_login(_q2_alert_env, monkeypatch):
    """A flaky SSE bus / Slack webhook must NEVER fail the login —
    notify_new_device_login swallows both branches independently."""
    auth = _q2_alert_env["auth"]
    events_mod = _q2_alert_env["events"]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    monkeypatch.setattr(events_mod, "emit_new_device_login", _boom)

    u = await auth.create_user(
        "fourth@example.com", "Fourth", role="operator",
        password="fourthpw-qq-111",
    )
    sess = await auth.create_session(
        u.id, ip="192.0.2.99", user_agent="Mozilla/5.0 (Boom)",
    )
    # Must not raise even though the SSE emit blew up.
    await auth.notify_new_device_login(
        u, sess, "192.0.2.99", "Mozilla/5.0 (Boom)",
    )
    # notify() (the second branch) is still patched + recording —
    # it should have been called despite the upstream emit crash.
    assert len(_q2_alert_env["notifies"]) == 1, (
        "an SSE failure must not short-circuit the IM/email branch"
    )


@pytest.mark.asyncio
async def test_thirty_day_recurrence_re_emits(_q2_alert_env, monkeypatch):
    """A device whose fingerprint last seen >30 d ago re-triggers the
    alert (the fingerprint primitive's 30-day cutoff propagates
    end-to-end through the helper)."""
    auth = _q2_alert_env["auth"]
    pool = _q2_alert_env["pool"]

    u = await auth.create_user(
        "fifth@example.com", "Fifth", role="operator",
        password="fifthpw-qq-111",
    )
    s_old = await auth.create_session(
        u.id, ip="198.51.100.50", user_agent="Mozilla/5.0 (Periodic)",
    )
    assert s_old.is_new_device is True

    # Backdate the fingerprint row to 31 days ago to simulate the
    # 30-day-stale scenario without monkeypatching time itself
    # (which would also affect session expiry and break the test).
    cutoff = time.time() - (31 * 24 * 3600.0)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE session_fingerprints SET last_seen_at = $1 WHERE user_id = $2",
            cutoff, u.id,
        )

    _q2_alert_env["emits"].clear()
    _q2_alert_env["notifies"].clear()

    s_new = await auth.create_session(
        u.id, ip="198.51.100.50", user_agent="Mozilla/5.0 (Periodic)",
    )
    assert s_new.is_new_device is True, (
        "fingerprint row >30 d old must be treated as a new device"
    )
    await auth.notify_new_device_login(
        u, s_new, "198.51.100.50", "Mozilla/5.0 (Periodic)",
    )
    assert len(_q2_alert_env["emits"]) == 1
    assert len(_q2_alert_env["notifies"]) == 1

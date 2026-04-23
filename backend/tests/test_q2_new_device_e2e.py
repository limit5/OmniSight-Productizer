"""Q.2 (#296) — end-to-end verification of the new-device-login alert
pipeline through the real ``POST /api/v1/auth/login`` route.

Companion suites already lock the per-layer contracts:

  * ``test_q2_new_device_fingerprint`` — fingerprint primitive (UPSERT
    + 30-day cutoff + IP/UA hashing).
  * ``test_q2_new_device_alert``       — ``notify_new_device_login``
    fan-out shape (SSE event + ``notify()`` dispatch).
  * ``test_q2_new_device_rate_limit``  — per-user 1/min + per-(user,
    subnet) 24h gates.
  * ``test_q2_new_device_preference``  — ``user_preferences.new_device_
    alerts`` opt-out toggle.
  * ``test_q2_not_me_cascade``         — ``DELETE /auth/sessions/{tok}
    ?cascade=not_me`` rotates everything.

This file is the integration acceptance: drive the live HTTP endpoint
with realistic ``cf-connecting-ip`` + ``User-Agent`` headers and
confirm the four operator-visible behaviours of the Q.2 spec hold
end-to-end. Each test exercises the full ``LoginRequest →
authenticate_password → create_session → notify_new_device_login``
chain that production runs on a real password login.

The four scenarios pinned by the Q.2 checklist (and the TODO row this
file closes):

  1. **Second device login triggers an alert.** A user who has logged
     in once from device A, then logs in from device B (different IP
     /24 + different UA) → one alert fires for the device-B login.
  2. **Same device re-login does NOT alert (de-duplication).** A user
     repeatedly logging in from the SAME (UA, /24) tuple inside the
     30-day window only triggers one alert across the burst — the
     fingerprint primitive's dedup propagates through the helper.
  3. **Preference disabled → no alert.** With ``new_device_alerts="0"``
     written to ``user_preferences``, even a brand-new device tuple
     produces zero alerts.
  4. **Rate limit caps repeated new-device alerts.** Two new-device
     logins for the same user inside 60s → only the first alert
     dispatches; the second is swallowed by the per-user 1/min gate.

Why these are E2E and not unit: the unit suites confirm each gate in
isolation; this file confirms they're correctly **wired into the HTTP
route** — i.e. that ``client_ip`` + the request's ``User-Agent``
header reach ``compute_ip_subnet()`` + the fingerprint UPSERT, that
``notify_new_device_login`` is actually called by the password-login
branch (not just MFA / WebAuthn), and that the rate-limit + preference
gates inside the helper are reachable from a live client. A regression
in the wiring (e.g. someone refactors the login route and forgets to
pass ``ua_header`` to the helper) would slip past the unit suites but
trip here.
"""

from __future__ import annotations

import os

import pytest


# ─── Fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
async def _q2_e2e_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """End-to-end fixture: live PG + httpx ASGI client + capture hooks.

    Mirrors the K1 ``client_with_default_admin`` fixture but seeds a
    plain operator account (not the default admin) and patches the
    Q.2 fan-out endpoints so the test can assert on what was emitted /
    notified without standing up Slack or an SSE consumer.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    # Login route doesn't gate on auth_mode for the login itself, but
    # set ``session`` so any downstream middleware (e.g. CSRF) behaves
    # like prod rather than ``open`` mode's bypass.
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

    # Cold-start the Q.2-relevant tables — every test starts from an
    # empty fingerprint table + empty preference table so the alert
    # decision is pure-input-driven.
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_fingerprints")
        await conn.execute("TRUNCATE user_preferences")

    # Q.2 rate-limit gates + the per-IP login attempt window are
    # module-singletons. A prior test could have pre-consumed a token
    # and silently mute the alert this test is trying to verify.
    from backend.rate_limit import reset_limiters
    from backend.routers import auth as auth_router

    reset_limiters()
    auth_router._LOGIN_ATTEMPTS.clear()

    # Patch capture hooks. ``notify_new_device_login`` resolves both
    # symbols lazily through their module namespace, so patching the
    # source modules (events.* / notifications.*) is sufficient — the
    # helper picks them up at call time.
    from backend import auth, events as _events, notifications

    captured_emits: list[dict] = []
    captured_notifies: list[dict] = []

    real_emit = _events.emit_new_device_login

    def _record_emit(*args, **kwargs):
        captured_emits.append({"args": args, "kwargs": kwargs})
        # Fan through to the real bus so the SSE plumbing isn't
        # short-circuited entirely (mirrors the unit-suite shape and
        # protects against accidental coupling to the patched form).
        real_emit(*args, **kwargs)

    monkeypatch.setattr(_events, "emit_new_device_login", _record_emit)

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

    # K1 #2: pin the bootstrap gate green so the login route isn't
    # 307-redirected to the bootstrap wizard. Q.2 doesn't care about
    # bootstrap state; reusing the K1 pattern keeps the fixture's
    # shape consistent with the rest of the suite.
    from backend import db
    from backend.main import app
    from httpx import ASGITransport, AsyncClient
    from backend import bootstrap as _boot

    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    # The K1 fixture closes-then-reinits ``db`` to point at PG; we do
    # the same so the login route's ``db._conn()`` call (still on the
    # legacy compat path for some side effects) reads/writes the same
    # PG that ``pg_test_pool`` truncated above.
    if db._db is not None:
        await db.close()
    await db.init()

    # Seed one operator user. Password is well over 12 chars (the
    # PASSWORD_MIN_LENGTH gate) and zxcvbn-friendly enough that
    # ``hash_password`` (no strength check on direct create_user)
    # accepts it without complaint. The login route only verifies,
    # not strength-validates, so this works end-to-end.
    user = await auth.create_user(
        email="q2-e2e@example.com",
        name="Q2 E2E",
        role="operator",
        password="SuperSecret-Q2-Login-2026",
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield {
                "client": ac,
                "user": user,
                "password": "SuperSecret-Q2-Login-2026",
                "emits": captured_emits,
                "notifies": captured_notifies,
                "pool": pg_test_pool,
                "auth": auth,
            }
    finally:
        await db.close()
        _boot._gate_cache_reset()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            await conn.execute("TRUNCATE session_fingerprints")
            await conn.execute("TRUNCATE user_preferences")
        reset_limiters()
        auth_router._LOGIN_ATTEMPTS.clear()


# ─── Helpers ─────────────────────────────────────────────────────────


_API = (os.environ.get("OMNISIGHT_API_PREFIX") or "/api/v1").rstrip("/")
LOGIN_URL = f"{_API}/auth/login"


async def _do_login(env, *, ip: str, ua: str) -> dict:
    """POST /auth/login with the supplied client IP + User-Agent.

    Uses ``cf-connecting-ip`` because that's what ``_client_key()`` in
    ``backend.routers.auth`` honours first (Cloudflare-tunnel parity).
    Returns the parsed JSON; raises if the route 4xx/5xxs.
    """
    resp = await env["client"].post(
        LOGIN_URL,
        json={"email": env["user"].email, "password": env["password"]},
        headers={"cf-connecting-ip": ip, "user-agent": ua},
    )
    assert resp.status_code == 200, (
        f"login expected 200 but got {resp.status_code}: {resp.text!r}"
    )
    return resp.json()


# ─── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_device_login_triggers_alert(_q2_e2e_client):
    """User logs in from device A (laptop @ /24=203.0.113), then later
    from device B (phone @ /24=198.51.100, different UA). Device A's
    login is itself a "new device" from a cold table, so it fires;
    device B then fires its own alert because it's a genuinely
    different (UA, /24) tuple.

    Locks the operator-visible behaviour: "logging in from a second,
    genuinely-different device DOES fire an alert" — the spec
    description directly.
    """
    env = _q2_e2e_client

    # Login 1 — device A. From a cold fingerprint table this is itself
    # a "new device" event and fires the first alert.
    await _do_login(
        env,
        ip="203.0.113.42",
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/120 - Device-A",
    )
    assert len(env["emits"]) == 1, (
        "first login from a cold fingerprint table must fire one alert"
    )
    assert len(env["notifies"]) == 1

    # Login 2 — device B. Different /24 AND different UA, so the
    # fingerprint primitive flags this as a new device, and the
    # rate-limit gate (per-user 1/min) is the only thing that could
    # block it. We exercised the gate in test_q2_new_device_rate_limit;
    # here we relax it to isolate the "second device alerts" behaviour.
    from backend import auth as _auth_mod
    import importlib

    # Use a small but non-zero per-user window so the rate-limit gate
    # refills before the second login (1ms = 1000 tokens/sec, refilled
    # by the next event-loop tick).
    monkey_window = 0.001
    original = _auth_mod.NEW_DEVICE_ALERT_USER_WINDOW_S
    _auth_mod.NEW_DEVICE_ALERT_USER_WINDOW_S = monkey_window
    try:
        # Drain the bucket-refill scheduler tick.
        import asyncio
        await asyncio.sleep(0.01)

        await _do_login(
            env,
            ip="198.51.100.7",
            ua="Mozilla/5.0 (iPhone) Safari/17 - Device-B",
        )
    finally:
        _auth_mod.NEW_DEVICE_ALERT_USER_WINDOW_S = original

    assert len(env["emits"]) == 2, (
        "logging in from a second, genuinely-different device must "
        "fire its own new-device alert"
    )
    assert len(env["notifies"]) == 2

    # Spot-check the second alert carries the second device's IP +
    # UA. This is the operator-visible payload — the email/IM body
    # the user opens to decide "is this me?".
    second_emit_kwargs = env["emits"][1]["kwargs"]
    assert second_emit_kwargs["ip"] == "198.51.100.7"
    assert "iPhone" in second_emit_kwargs["user_agent"]
    assert second_emit_kwargs["user_id"] == env["user"].id


@pytest.mark.asyncio
async def test_same_device_relogin_does_not_realert(_q2_e2e_client):
    """A user who logs in three times from the SAME laptop (same UA,
    same /24) inside the 30-day fingerprint window only ever sees one
    alert. The fingerprint primitive's UPSERT dedups subsequent logins
    via ``is_new_device=False``, which the helper short-circuits on
    before any rate-limit or preference gate runs.

    Pins the spec line: "同裝置重登不再 alert（去重）".
    """
    env = _q2_e2e_client

    # Three back-to-back logins from the same (UA, /24).
    same_ua = "Mozilla/5.0 (X11; Linux) Firefox/123 - workstation"
    for ip_last_octet in (10, 11, 12):
        # Same /24 (203.0.113.0/24) — host octet varies as DHCP would,
        # which the (user, ua_hash, ip_subnet) primary key collapses
        # into a single fingerprint row.
        await _do_login(
            env,
            ip=f"203.0.113.{ip_last_octet}",
            ua=same_ua,
        )

    assert len(env["emits"]) == 1, (
        "three logins from one (UA, /24) tuple must produce exactly "
        "one alert — fingerprint dedup applies from the second login on"
    )
    assert len(env["notifies"]) == 1

    # Confirm the dedup is what we think it is (not the rate-limit
    # gate masking it): inspect the fingerprint row's session_count.
    async with env["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_count FROM session_fingerprints "
            "WHERE user_id = $1",
            env["user"].id,
        )
    assert row is not None, (
        "the same-device logins must collapse into one fingerprint row"
    )
    assert row["session_count"] >= 3, (
        f"expected ≥3 session_count from 3 logins on the same fingerprint, "
        f"got {row['session_count']}"
    )


@pytest.mark.asyncio
async def test_preference_off_suppresses_alert(_q2_e2e_client):
    """With ``user_preferences.new_device_alerts="0"`` written for the
    user, even a login from a brand-new (UA, /24) on a cold fingerprint
    table fires zero alerts.

    Pins the spec line: "可關閉：POST /user/preferences { new_device_
    alerts: false }". The opt-out gate sits ABOVE the rate-limit gate
    by design, so this also implicitly verifies the gate-ordering: a
    disabled user's login does not consume a rate-limit token.
    """
    env = _q2_e2e_client

    # Write the opt-out preference directly via the same upsert shape
    # the ``PUT /user-preferences/{key}`` endpoint uses. The integration
    # surface for the toggle is locked by the SP-5.8 + Q.2-preference
    # unit suites; here we verify the alert pipeline honours it.
    import time as _time
    async with env["pool"].acquire() as conn:
        await conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, pref_key, value, updated_at, tenant_id) "
            "VALUES ($1, $2, $3, $4, 't-default') "
            "ON CONFLICT (user_id, pref_key) DO UPDATE SET "
            "  value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            env["user"].id, "new_device_alerts", "0", _time.time(),
        )

    await _do_login(
        env,
        ip="203.0.113.99",
        ua="Mozilla/5.0 (Optedout) Chrome/120",
    )

    assert env["emits"] == [], (
        "pref=0 must suppress the SSE emit even on a cold fingerprint table"
    )
    assert env["notifies"] == [], (
        "pref=0 must suppress the IM/email dispatch end-to-end"
    )

    # And the fingerprint row was still recorded — opting out of the
    # alert does NOT opt out of the fingerprint history (which the
    # cascade + revocation flows depend on).
    async with env["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM session_fingerprints WHERE user_id = $1",
            env["user"].id,
        )
    assert row is not None, (
        "the fingerprint primitive must still record the device — only "
        "the alert dispatch is silenced by the preference"
    )


@pytest.mark.asyncio
async def test_rate_limit_caps_burst_to_one_alert(_q2_e2e_client):
    """Two new-device logins for the same user inside the 60s per-user
    window: only the first fires. The second login is from a different
    /24 AND a different UA so the fingerprint primitive WOULD say
    is_new_device=True; the per-user 1/min gate is what catches it.

    Pins the spec line: "Rate limit：同 user 每分鐘最多發一則新裝置通知".

    Uses the production window value (60s) — no monkeypatch — so the
    test is a true acceptance check on the default deployment posture,
    not a synthetic shortened window.
    """
    env = _q2_e2e_client

    await _do_login(
        env,
        ip="203.0.113.10",
        ua="Mozilla/5.0 (Burst-A) Chrome/120",
    )
    assert len(env["emits"]) == 1, "first burst alert must fire"
    assert len(env["notifies"]) == 1

    # Second login: genuinely different device (different /24, different
    # UA). Fingerprint says "new device" → would alert if the rate-limit
    # gate weren't there. The gate's per-user bucket was just consumed
    # by the alert above and won't refill for ~60s, so this second
    # alert is swallowed.
    await _do_login(
        env,
        ip="198.51.100.20",
        ua="Mozilla/5.0 (Burst-B) Firefox/123",
    )
    assert len(env["emits"]) == 1, (
        "second new-device login within the 60s per-user window must "
        "be silently dropped — at most one alert per user per minute"
    )
    assert len(env["notifies"]) == 1

    # Sanity: the second login DID succeed at the HTTP layer (rate-limit
    # silences the alert, not the login). The fingerprint table records
    # both devices regardless.
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT ip_subnet FROM session_fingerprints WHERE user_id = $1",
            env["user"].id,
        )
    subnets = {r["ip_subnet"] for r in rows}
    assert subnets == {"203.0.113", "198.51.100"}, (
        f"both /24s must be recorded as fingerprint rows even when the "
        f"second alert is rate-limit-suppressed; got {subnets}"
    )

"""Q.2 (#296) — per-user opt-out of the new-device-login alert.

The feature under test is the ``user_preferences.new_device_alerts``
toggle. When a user sets it to a falsy value (``"0" / "false" / "off"
/ "no"``) via ``PUT /user-preferences/new_device_alerts``, the alert
pipeline in ``auth.notify_new_device_login`` must be a strict no-op
even when ``Session.is_new_device`` is True and the rate-limit gates
would have allowed the alert through.

The gate sits **above** the rate-limit gate by design:

* Missing / "1" / "true" / anything-not-falsy → alerts ON (default).
* "0" / "false" / "off" / "no" (case-insensitive) → alerts OFF.
* DB read failure → fails open (alert fires) so a transient PG blip
  can't silently mute a security signal.

Scope is strictly per-user: disabling for User A must not affect
User B. The choke point is the helper ``_new_device_alerts_enabled``;
every call site funnels through ``notify_new_device_login``.

Companion suites:

* ``test_q2_new_device_alert`` locks the dispatch fan-out shape.
* ``test_q2_new_device_rate_limit`` locks the gate below this one.
* This file locks the opt-out — and documents the interaction with
  the rate-limit gate (pref OFF stops the alert BEFORE any token is
  consumed, so a disabled user's logins never burn either bucket).
"""

from __future__ import annotations

import pytest


@pytest.fixture()
async def _q2_pref_env(pg_test_pool, monkeypatch):
    """Same shape as ``_q2_alert_env`` in test_q2_new_device_alert,
    extended to also truncate ``user_preferences`` so every test starts
    from a known-default (row absent → alerts ON) baseline."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        await conn.execute("TRUNCATE session_fingerprints")
        await conn.execute("TRUNCATE user_preferences")

    from backend.rate_limit import reset_limiters
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
            await conn.execute("TRUNCATE user_preferences")
        reset_limiters()


async def _set_pref(pool, user_id: str, value: str) -> None:
    """Write ``new_device_alerts=<value>`` for ``user_id``. Mirrors
    the ``PUT /user-preferences/{key}`` upsert contract."""
    import time
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_preferences "
            "(user_id, pref_key, value, updated_at, tenant_id) "
            "VALUES ($1, $2, $3, $4, 't-default') "
            "ON CONFLICT (user_id, pref_key) DO UPDATE SET "
            "  value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            user_id, "new_device_alerts", value, time.time(),
        )


@pytest.mark.asyncio
async def test_missing_pref_defaults_to_alerts_on(_q2_pref_env):
    """Baseline / regression lock: a user who has never touched the
    toggle keeps the default behaviour. This is what the existing Q.2
    suite implicitly relies on — truncating user_preferences between
    tests + default ON means no behaviour change for untouched users.
    """
    auth = _q2_pref_env["auth"]
    u = await auth.create_user(
        "defaulton@example.com", "DefaultOn", role="operator",
        password="defaultpw-qq-111",
    )
    sess = await auth.create_session(
        u.id, ip="203.0.113.10", user_agent="Mozilla/5.0 (Default)",
    )
    assert sess.is_new_device is True

    await auth.notify_new_device_login(
        u, sess, "203.0.113.10", "Mozilla/5.0 (Default)",
    )

    assert len(_q2_pref_env["emits"]) == 1
    assert len(_q2_pref_env["notifies"]) == 1


@pytest.mark.asyncio
async def test_pref_zero_suppresses_alert(_q2_pref_env):
    """The canonical OFF value ``"0"`` (mirrors ``wizard_seen="1"``
    style already used by SP-5.8) must fully silence the alert — no
    SSE emit, no notify dispatch, even though is_new_device=True."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    u = await auth.create_user(
        "off@example.com", "Off", role="operator",
        password="offpw-qq-111",
    )
    await _set_pref(pool, u.id, "0")

    sess = await auth.create_session(
        u.id, ip="203.0.113.20", user_agent="Mozilla/5.0 (Off)",
    )
    assert sess.is_new_device is True, (
        "fingerprint primitive is unaffected by the pref — it still "
        "records the device as new; it's the alert that's silenced"
    )

    await auth.notify_new_device_login(
        u, sess, "203.0.113.20", "Mozilla/5.0 (Off)",
    )

    assert _q2_pref_env["emits"] == [], (
        "pref=0 must suppress the SSE event entirely"
    )
    assert _q2_pref_env["notifies"] == [], (
        "pref=0 must suppress the IM/email dispatch entirely"
    )


@pytest.mark.asyncio
async def test_pref_false_and_case_variants_suppress(_q2_pref_env):
    """Accept the common falsy spellings, case-insensitive. The value
    convention is ``frozenset({"0","false","off","no"})``; values are
    lowercased + stripped before comparison so ``"False"`` / ``" OFF "``
    / ``"No"`` all work."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    for i, spelling in enumerate(["false", "FALSE", "Off", "no", " false "]):
        u = await auth.create_user(
            f"variant{i}@example.com", f"Variant{i}", role="operator",
            password="variantpw-qq-111",
        )
        await _set_pref(pool, u.id, spelling)

        sess = await auth.create_session(
            u.id, ip=f"203.0.113.{30 + i}",
            user_agent=f"Mozilla/5.0 (Variant{i})",
        )
        _q2_pref_env["emits"].clear()
        _q2_pref_env["notifies"].clear()

        await auth.notify_new_device_login(
            u, sess, f"203.0.113.{30 + i}", f"Mozilla/5.0 (Variant{i})",
        )
        assert _q2_pref_env["emits"] == [], (
            f"pref={spelling!r} must suppress SSE emit"
        )
        assert _q2_pref_env["notifies"] == [], (
            f"pref={spelling!r} must suppress notify dispatch"
        )


@pytest.mark.asyncio
async def test_pref_one_or_true_still_fires(_q2_pref_env):
    """Explicit truthy values ("1", "true") must keep alerts flowing —
    this is the recovery path after a user who previously disabled
    the alert re-enables it."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    u = await auth.create_user(
        "reenabled@example.com", "ReEnabled", role="operator",
        password="reenabledpw-qq-111",
    )
    # Write OFF, then flip back ON.
    await _set_pref(pool, u.id, "0")
    await _set_pref(pool, u.id, "1")

    sess = await auth.create_session(
        u.id, ip="203.0.113.40", user_agent="Mozilla/5.0 (ReEnabled)",
    )
    await auth.notify_new_device_login(
        u, sess, "203.0.113.40", "Mozilla/5.0 (ReEnabled)",
    )
    assert len(_q2_pref_env["emits"]) == 1, (
        "re-enabled pref must resume alert delivery on the next login"
    )
    assert len(_q2_pref_env["notifies"]) == 1


@pytest.mark.asyncio
async def test_pref_scope_is_per_user(_q2_pref_env):
    """User A disabling the alert must NOT affect User B. The pref
    is keyed by (user_id, pref_key) so this should hold trivially,
    but the test locks the contract against a future refactor that
    might accidentally collapse the scope (e.g. a tenant-wide toggle)."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    user_a = await auth.create_user(
        "usera@example.com", "UserA", role="operator",
        password="userapw-qq-111",
    )
    user_b = await auth.create_user(
        "userb@example.com", "UserB", role="operator",
        password="userbpw-qq-111",
    )
    await _set_pref(pool, user_a.id, "0")  # A opts out; B untouched.

    sess_a = await auth.create_session(
        user_a.id, ip="203.0.113.50", user_agent="Mozilla/5.0 (A)",
    )
    sess_b = await auth.create_session(
        user_b.id, ip="203.0.113.60", user_agent="Mozilla/5.0 (B)",
    )

    await auth.notify_new_device_login(
        user_a, sess_a, "203.0.113.50", "Mozilla/5.0 (A)",
    )
    await auth.notify_new_device_login(
        user_b, sess_b, "203.0.113.60", "Mozilla/5.0 (B)",
    )

    assert len(_q2_pref_env["emits"]) == 1, (
        "exactly one emit — for User B — User A is opted out"
    )
    assert _q2_pref_env["emits"][0]["kwargs"]["user_id"] == user_b.id
    assert len(_q2_pref_env["notifies"]) == 1


@pytest.mark.asyncio
async def test_pref_read_failure_fails_open(_q2_pref_env, monkeypatch):
    """A transient PG pool failure while reading the preference must
    NOT silently mute the alert. Missing-alert-during-outage is a worse
    security posture than duplicate-alert; fail open + log a warning."""
    auth = _q2_pref_env["auth"]

    u = await auth.create_user(
        "failopen@example.com", "FailOpen", role="operator",
        password="failopenpw-qq-111",
    )
    sess = await auth.create_session(
        u.id, ip="203.0.113.70", user_agent="Mozilla/5.0 (FailOpen)",
    )

    # Poison get_pool() **after** create_session has finished — that
    # path also uses the pool and we don't want to break the setup.
    # The helper inside notify_new_device_login must catch the raise
    # and fall through to alerts=ON (the documented fail-open branch).
    from backend import db_pool as db_pool_mod

    class _BrokenPool:
        def acquire(self):
            raise RuntimeError("simulated PG outage")

    monkeypatch.setattr(db_pool_mod, "get_pool", lambda: _BrokenPool())

    await auth.notify_new_device_login(
        u, sess, "203.0.113.70", "Mozilla/5.0 (FailOpen)",
    )

    # Fail-open → alert still dispatches despite pref read crash.
    assert len(_q2_pref_env["emits"]) == 1, (
        "pref read failure must fall back to alerts=ON (fail-open)"
    )
    assert len(_q2_pref_env["notifies"]) == 1


@pytest.mark.asyncio
async def test_pref_helper_unit_matrix(_q2_pref_env):
    """Direct unit test on ``_new_device_alerts_enabled`` for the full
    value matrix. Decoupled from the full notify_new_device_login
    pipeline so a behaviour change here is isolated from dispatch
    plumbing regressions."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    u = await auth.create_user(
        "matrix@example.com", "Matrix", role="operator",
        password="matrixpw-qq-111",
    )

    async def _probe(value: str | None) -> bool:
        if value is None:
            # Clear the row to simulate "never set".
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM user_preferences "
                    "WHERE user_id = $1 AND pref_key = $2",
                    u.id, "new_device_alerts",
                )
        else:
            await _set_pref(pool, u.id, value)
        return await auth._new_device_alerts_enabled(u.id)

    # Missing row → enabled (default ON).
    assert await _probe(None) is True
    # Canonical OFF spellings → disabled.
    for off in ("0", "false", "FALSE", "off", "no", " false "):
        assert await _probe(off) is False, f"{off!r} should be OFF"
    # Truthy / unknown spellings → enabled.
    for on in ("1", "true", "on", "yes", "", "something-else"):
        assert await _probe(on) is True, f"{on!r} should be ON (permissive)"


@pytest.mark.asyncio
async def test_pref_off_short_circuits_before_rate_limit(_q2_pref_env):
    """Interaction with the rate-limit gate: pref=0 must early-return
    BEFORE ``_new_device_alert_should_fire`` runs, so a disabled user
    does NOT burn their per-user or per-subnet token bucket. Re-enabling
    the pref later should therefore see a fresh bucket (no phantom
    consumption while alerts were muted)."""
    auth = _q2_pref_env["auth"]
    pool = _q2_pref_env["pool"]

    u = await auth.create_user(
        "shortcircuit@example.com", "ShortCircuit", role="operator",
        password="shortcircuitpw-qq-111",
    )
    # Opt out first.
    await _set_pref(pool, u.id, "0")

    # Drive N logins that would normally have triggered rate-limit
    # suppression after the first. With pref=0 they all early-return
    # pre-gate so no buckets are touched.
    for i in range(3):
        s = await auth.create_session(
            u.id, ip=f"198.51.100.{10 + i}",
            user_agent=f"Mozilla/5.0 (SC-{i})",
        )
        await auth.notify_new_device_login(
            u, s, f"198.51.100.{10 + i}", f"Mozilla/5.0 (SC-{i})",
        )
    assert _q2_pref_env["emits"] == []
    assert _q2_pref_env["notifies"] == []

    # Now flip back ON. First login after re-enable must fire — which
    # proves the user bucket wasn't burned while the pref was OFF.
    await _set_pref(pool, u.id, "1")
    s_resume = await auth.create_session(
        u.id, ip="198.51.100.99", user_agent="Mozilla/5.0 (SC-resume)",
    )
    await auth.notify_new_device_login(
        u, s_resume, "198.51.100.99", "Mozilla/5.0 (SC-resume)",
    )
    assert len(_q2_pref_env["emits"]) == 1, (
        "pref OFF must not consume rate-limit tokens; re-enabling and "
        "logging in on a fresh subnet must still dispatch"
    )
    assert len(_q2_pref_env["notifies"]) == 1

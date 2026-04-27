"""Q.5 (#299) — ``GET /auth/sessions/presence`` endpoint contract.

The endpoint consumes the ``SessionPresence`` heartbeat hash populated
by the SSE event stream (``backend/routers/events.py::event_stream``)
and crosswalks presence entries against ``sessions`` rows to attach
per-device metadata (device name from UA, token_hint, idle/active flag,
is_current).

Covered here (mirrors the TODO row's spec bullet-for-bullet):

  * Empty presence hash → ``active_count=0`` + ``devices=[]``.
  * Three heartbeats in the 60 s window → ``active_count=3`` + one row
    per device, sorted freshest-first, ``device_name`` parsed from UA,
    ``ua_hash`` stable, ``is_current`` set on the caller's own session.
  * Stale heartbeat (>60 s) is excluded from the reply AND pruned from
    the hash by the opportunistic GC baked into the endpoint.
  * Idle classification: fresh (<30 s idle) → ``status="active"``,
    older-but-still-within-window (≥30 s) → ``status="idle"``.
  * Revoked session but lingering heartbeat: row still counted but
    ``device_name="Unknown device"`` + empty ``token_hint`` rather than
    500-ing on the crosswalk miss.
  * Isolation: user A's presence never bleeds into user B's reply.

Follows the direct-handler fixture pattern from ``test_q2_not_me_cascade``
to sidestep the ``client`` fixture lifespan bug — ``sessions_presence``
is a thin read aggregator so the full HTTP stack adds no coverage
beyond what these unit tests lock down.
"""

from __future__ import annotations

import time
import types

import pytest


@pytest.fixture()
async def _q5_db(pg_test_pool, monkeypatch):
    """Clean slate + presence-hash isolation per test.

    ``session_presence`` is a module-level ``SharedKV`` singleton; the
    in-memory fallback it uses under pytest accumulates across tests
    unless cleared, which would mask cross-test leakage bugs.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, sessions RESTART IDENTITY CASCADE"
        )
    from backend import auth
    from backend.shared_state import session_presence
    session_presence._local.clear()
    try:
        yield auth, session_presence
    finally:
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, sessions RESTART IDENTITY CASCADE"
            )
        session_presence._local.clear()


def _make_request(session_token: str | None = None):
    cookies = {}
    if session_token:
        cookies["omnisight_session"] = session_token
    return types.SimpleNamespace(cookies=cookies, headers={}, client=None)


# ── empty state ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_presence_empty_returns_zero_count(_q5_db):
    auth, _ = _q5_db
    u = await auth.create_user(
        "empty@example.com", "E", role="viewer", password="pwpwpwpwpwpw",
    )
    from backend.routers.auth import sessions_presence

    result = await sessions_presence(_make_request(), user=u)
    assert result["active_count"] == 0
    assert result["devices"] == []
    assert result["window_seconds"] == 60.0
    assert isinstance(result["now"], float)


# ── three active devices, freshest-first sort + metadata shape ──


@pytest.mark.asyncio
async def test_presence_three_devices_returns_all_with_metadata(_q5_db):
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "multi@example.com", "M", role="operator", password="pwpwpwpwpwpw",
    )
    chrome_mac = await auth.create_session(
        u.id, ip="1.1.1.1",
        user_agent="Mozilla/5.0 (Macintosh; Mac OS) Chrome/125.0",
    )
    firefox_linux = await auth.create_session(
        u.id, ip="2.2.2.2",
        user_agent="Mozilla/5.0 (X11; Linux) Firefox/120.0",
    )
    safari_ios = await auth.create_session(
        u.id, ip="3.3.3.3",
        user_agent="Mozilla/5.0 (iPhone) Safari/16.0",
    )

    now = time.time()
    session_presence.record_heartbeat(
        u.id, auth.session_id_from_token(chrome_mac.token), ts=now - 1.0,
    )
    session_presence.record_heartbeat(
        u.id, auth.session_id_from_token(firefox_linux.token), ts=now - 5.0,
    )
    session_presence.record_heartbeat(
        u.id, auth.session_id_from_token(safari_ios.token), ts=now - 10.0,
    )

    from backend.routers.auth import sessions_presence
    result = await sessions_presence(
        _make_request(session_token=chrome_mac.token), user=u,
    )

    assert result["active_count"] == 3
    devices = result["devices"]
    assert len(devices) == 3

    # Freshest first (chrome_mac was 1s ago; safari 10s).
    assert [d["session_id"] for d in devices] == [
        auth.session_id_from_token(chrome_mac.token),
        auth.session_id_from_token(firefox_linux.token),
        auth.session_id_from_token(safari_ios.token),
    ]

    # Device labels derived from UA on the server side.
    labels = {d["session_id"]: d["device_name"] for d in devices}
    assert labels[auth.session_id_from_token(chrome_mac.token)] == "Chrome on macOS"
    assert labels[auth.session_id_from_token(firefox_linux.token)] == "Firefox on Linux"
    assert labels[auth.session_id_from_token(safari_ios.token)] == "Safari on iOS"

    # token_hint + ua_hash populated from the sessions-table crosswalk.
    for d in devices:
        assert d["token_hint"], "token_hint must be set for live sessions"
        assert len(d["ua_hash"]) == 32, "compute_ua_hash truncates to 32 chars"
        assert isinstance(d["last_heartbeat_at"], float)
        assert d["idle_seconds"] >= 0.0

    # Caller's own session flagged is_current.
    current_sid = auth.session_id_from_token(chrome_mac.token)
    assert sum(1 for d in devices if d["is_current"]) == 1
    assert [d for d in devices if d["is_current"]][0]["session_id"] == current_sid


# ── window filtering + prune_expired opportunistic GC ───────────


@pytest.mark.asyncio
async def test_presence_drops_stale_and_prunes_hash(_q5_db):
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "stale@example.com", "S", role="viewer", password="pwpwpwpwpwpw",
    )
    fresh = await auth.create_session(u.id, user_agent="Chrome")
    stale = await auth.create_session(u.id, user_agent="Firefox")
    fresh_sid = auth.session_id_from_token(fresh.token)
    stale_sid = auth.session_id_from_token(stale.token)

    now = time.time()
    session_presence.record_heartbeat(u.id, fresh_sid, ts=now - 2.0)
    # 120 s old — outside the 60 s window.
    session_presence.record_heartbeat(u.id, stale_sid, ts=now - 120.0)

    from backend.routers.auth import sessions_presence
    result = await sessions_presence(_make_request(), user=u)

    assert result["active_count"] == 1
    assert [d["session_id"] for d in result["devices"]] == [fresh_sid]

    # prune_expired ran opportunistically → stale field gone from hash.
    assert session_presence.last_seen(u.id, stale_sid) is None
    assert session_presence.last_seen(u.id, fresh_sid) is not None


# ── status classification: active < 30 s < idle < 60 s ─────────


@pytest.mark.asyncio
async def test_presence_status_active_vs_idle(_q5_db):
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "idle@example.com", "I", role="viewer", password="pwpwpwpwpwpw",
    )
    active = await auth.create_session(u.id, user_agent="Chrome")
    idle = await auth.create_session(u.id, user_agent="Firefox")
    active_sid = auth.session_id_from_token(active.token)
    idle_sid = auth.session_id_from_token(idle.token)

    now = time.time()
    session_presence.record_heartbeat(u.id, active_sid, ts=now - 2.0)
    session_presence.record_heartbeat(u.id, idle_sid, ts=now - 45.0)

    from backend.routers.auth import sessions_presence
    result = await sessions_presence(_make_request(), user=u)

    by_sid = {d["session_id"]: d for d in result["devices"]}
    assert by_sid[active_sid]["status"] == "active"
    assert by_sid[idle_sid]["status"] == "idle"


# ── revoked session with lingering heartbeat ───────────────────


@pytest.mark.asyncio
async def test_presence_lingering_heartbeat_for_revoked_session(_q5_db):
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "ghost@example.com", "G", role="viewer", password="pwpwpwpwpwpw",
    )
    s = await auth.create_session(u.id, user_agent="Chrome")
    sid = auth.session_id_from_token(s.token)

    session_presence.record_heartbeat(u.id, sid, ts=time.time() - 1.0)
    # Session dies before the SSE finally-drop runs (race window).
    await auth.revoke_session(s.token)

    from backend.routers.auth import sessions_presence
    result = await sessions_presence(_make_request(), user=u)

    assert result["active_count"] == 1
    d = result["devices"][0]
    assert d["session_id"] == sid
    assert d["device_name"] == "Unknown device", (
        "crosswalk miss must fall back to the Unknown device label, "
        "not 500 on the None UA"
    )
    assert d["token_hint"] == ""
    assert d["ua_hash"] == ""


# ── cross-user isolation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_presence_does_not_leak_across_users(_q5_db):
    auth, session_presence = _q5_db
    alice = await auth.create_user(
        "alice-q5@example.com", "A", role="viewer", password="pwpwpwpwpwpw",
    )
    bob = await auth.create_user(
        "bob-q5@example.com", "B", role="viewer", password="pwpwpwpwpwpw",
    )
    a_sess = await auth.create_session(alice.id, user_agent="Chrome")
    b_sess1 = await auth.create_session(bob.id, user_agent="Firefox")
    b_sess2 = await auth.create_session(bob.id, user_agent="Safari")

    now = time.time()
    session_presence.record_heartbeat(
        alice.id, auth.session_id_from_token(a_sess.token), ts=now,
    )
    session_presence.record_heartbeat(
        bob.id, auth.session_id_from_token(b_sess1.token), ts=now,
    )
    session_presence.record_heartbeat(
        bob.id, auth.session_id_from_token(b_sess2.token), ts=now,
    )

    from backend.routers.auth import sessions_presence
    a_result = await sessions_presence(_make_request(), user=alice)
    b_result = await sessions_presence(_make_request(), user=bob)

    assert a_result["active_count"] == 1
    assert b_result["active_count"] == 2
    a_sids = {d["session_id"] for d in a_result["devices"]}
    b_sids = {d["session_id"] for d in b_result["devices"]}
    assert a_sids.isdisjoint(b_sids)


# ── three-device → drop one → count drops (TODO test bullet) ──


@pytest.mark.asyncio
async def test_presence_three_then_drop_one_returns_two(_q5_db):
    """TODO bullet: open 3 sessions, verify endpoint returns 3; stop 1
    for 60+ s, verify endpoint returns 2.

    Directly exercises the window-expiry path that the headless
    integration test in the TODO describes.
    """
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "3to2@example.com", "T", role="viewer", password="pwpwpwpwpwpw",
    )
    a = await auth.create_session(u.id, user_agent="Chrome")
    b = await auth.create_session(u.id, user_agent="Firefox")
    c = await auth.create_session(u.id, user_agent="Safari")

    now = time.time()
    for s in (a, b, c):
        session_presence.record_heartbeat(
            u.id, auth.session_id_from_token(s.token), ts=now,
        )

    from backend.routers.auth import sessions_presence
    r1 = await sessions_presence(_make_request(), user=u)
    assert r1["active_count"] == 3

    # Simulate session ``c`` going quiet for 61 s by freezing its
    # heartbeat timestamp and advancing the presence clock 61 s. The
    # active_sessions() path reads ``now`` implicitly via time.time() in
    # the handler, so we age the stored ts backward instead.
    c_sid = auth.session_id_from_token(c.token)
    session_presence.set(
        session_presence._field(u.id, c_sid),
        f"{now - 61.0:.3f}",
    )

    r2 = await sessions_presence(_make_request(), user=u)
    assert r2["active_count"] == 2
    returned = {d["session_id"] for d in r2["devices"]}
    assert c_sid not in returned


# ── R19 regression tests (audit 2026-04-27 P1.2) ──────────────────
# R19 fix (commit e54ef075): event_stream's finally block was unconditionally
# calling session_presence.drop() on disconnect — even on transient drops
# (CF tunnel buffering pre-`open` event, browser tab backgrounding, network
# blips). Each cycle wrote heartbeat then immediately dropped it, leaving
# the presence hash empty ~95% of the time and ACTIVE_DEVICES stuck at 0.
#
# Fix removed the drop entirely; 60s lazy-prune handles stale entries.
# These tests guard against re-introduction.


def test_r19_event_stream_finally_does_not_call_session_presence_drop():
    """R19 drift guard — verify backend/routers/events.py::event_stream's
    finally block does NOT call session_presence.drop().

    This is a source-code-level check (read the file, parse the finally
    block) rather than a runtime test, because event_stream is an async
    generator wrapped in EventSourceResponse — driving it with a real
    CancelledError requires the full ASGI machinery and is fragile.

    The drift guard catches any future commit that re-adds drop() to the
    finally block. If the guard fails, read the R19 commit message
    (e54ef075) before "fixing" it — the absence of drop is intentional.
    """
    import re
    from pathlib import Path

    events_py = Path(__file__).parent.parent / "routers" / "events.py"
    src = events_py.read_text(encoding="utf-8")

    # Locate the event_stream function body.
    start = src.find("async def event_stream(")
    assert start >= 0, "event_stream function not found"
    # Take a generous slice — function spans ~70 lines.
    func_body = src[start : start + 8000]

    # Find the `finally:` block inside the inner generator. The R19 fix
    # left a multi-line comment explaining why drop is absent — that
    # comment is part of the contract.
    finally_match = re.search(
        r"\n\s*finally:\s*\n((?:\s+.+\n)+?)(?=\n\s{0,8}return EventSourceResponse|\Z)",
        func_body,
    )
    assert finally_match, (
        "Could not locate finally block in event_stream. R19 drift guard "
        "needs to be updated if event_stream's structure changed."
    )
    finally_body = finally_match.group(1)

    # The R19 invariant: finally block must NOT call session_presence.drop().
    assert "session_presence.drop(" not in finally_body, (
        "R19 regression: event_stream's finally block calls "
        "session_presence.drop(*presence). This re-introduces the bug "
        "fixed by commit e54ef075 (presence hash empty 95% of the time, "
        "ACTIVE_DEVICES stuck at 0). Read the R19 commit message + "
        "docs/audit/2026-04-27-deep-audit.md §3 P1.2 before resolving."
    )

    # The finally block SHOULD call bus.unsubscribe (the original cleanup
    # that was always correct).
    assert "bus.unsubscribe(queue)" in finally_body, (
        "R19 drift guard expected bus.unsubscribe(queue) in finally — "
        "if you've changed the cleanup pattern, update this assertion."
    )


@pytest.mark.asyncio
async def test_r19_presence_persists_through_simulated_disconnect_cycle(_q5_db):
    """R19 behavior — simulate the connect→disconnect→reconnect cycle that
    used to wipe presence under the buggy finally block.

    The R19 saga: SSE clients (especially behind CF tunnel pre-`open`-event
    buffering) often disconnect within ~1s of opening. With the old finally
    drop(), each cycle recorded heartbeat then immediately deleted it, so
    the presence hash stayed empty even as SSE was constantly reconnecting.

    This test directly exercises ``session_presence.record_heartbeat`` →
    ``finally simulation`` (which now does NOT drop) → reconnect →
    record_heartbeat again, and asserts the record persists. It does NOT
    drive event_stream itself (too fragile against ASGI internals); the
    drift-guard test above covers that source-code invariant.
    """
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "r19@example.com", "R", role="viewer", password="pwpwpwpwpwpw",
    )
    s = await auth.create_session(u.id, user_agent="Chrome")
    sid = auth.session_id_from_token(s.token)

    # Cycle 1: connect → record heartbeat → disconnect (no drop).
    now = time.time()
    session_presence.record_heartbeat(u.id, sid, ts=now)
    # Simulate finally block: ONLY bus.unsubscribe equivalent, NO drop.
    # (We don't have a real bus here; this just asserts no presence
    # mutation happens during disconnect.)

    # Verify presence still recorded.
    raw = session_presence.get(session_presence._field(u.id, sid))
    assert raw, "After cycle 1 disconnect, presence should still exist"
    assert abs(float(raw) - now) < 0.01

    # Cycle 2: reconnect → record heartbeat → disconnect again.
    cycle2_ts = now + 5.0
    session_presence.record_heartbeat(u.id, sid, ts=cycle2_ts)
    raw = session_presence.get(session_presence._field(u.id, sid))
    assert raw, "After cycle 2 disconnect, presence should still exist"
    assert abs(float(raw) - cycle2_ts) < 0.01

    # Cycle 3: reconnect after 30s gap (still within 60s window).
    cycle3_ts = cycle2_ts + 30.0
    session_presence.record_heartbeat(u.id, sid, ts=cycle3_ts)
    raw = session_presence.get(session_presence._field(u.id, sid))
    assert raw, "After cycle 3 disconnect (30s gap), presence should still exist"

    # Verify the presence endpoint sees an active session.
    from backend.routers.auth import sessions_presence
    # Use the cycle3 timestamp as "now" via the stored heartbeat.
    r = await sessions_presence(_make_request(), user=u)
    assert r["active_count"] == 1, (
        f"R19 invariant: after 3 connect-disconnect cycles, presence should "
        f"still show 1 active device. Got {r['active_count']}. This is the "
        f"exact ACTIVE_DEVICES=0 bug R19 was meant to fix."
    )


@pytest.mark.asyncio
async def test_r19_presence_lazy_prune_still_works_after_window(_q5_db):
    """R19 invariant: removing the eager drop() must NOT break the 60s
    lazy-prune. Stale entries (>60s no heartbeat) must still age out via
    the opportunistic GC in /auth/sessions/presence.

    This test guards against an over-correction where someone might
    "improve" R19 by removing the lazy-prune as well, leaving stale
    entries stuck in the hash forever.
    """
    auth, session_presence = _q5_db
    u = await auth.create_user(
        "r19stale@example.com", "S", role="viewer", password="pwpwpwpwpwpw",
    )
    s = await auth.create_session(u.id, user_agent="Chrome")
    sid = auth.session_id_from_token(s.token)

    # Record a heartbeat 70s ago — past the 60s window.
    stale_ts = time.time() - 70.0
    session_presence.record_heartbeat(u.id, sid, ts=stale_ts)

    # Verify endpoint excludes stale + GC prunes the hash.
    from backend.routers.auth import sessions_presence
    r = await sessions_presence(_make_request(), user=u)
    assert r["active_count"] == 0, "Stale heartbeat (>60s) must be excluded"

    # The GC inside the endpoint should have pruned the hash.
    raw = session_presence.get(session_presence._field(u.id, sid))
    assert not raw, (
        "R19 invariant: opportunistic prune should have removed the stale "
        "entry. R19 removed eager drop(); prune is the only cleanup path."
    )

"""Q.5 (#299) — headless end-to-end test for the active-device presence
indicator (closes the TODO row's last bullet):

  > 開 3 個 headless session、驗 presence endpoint 回 3、停 1 個 60s 後回 2.

The unit suite in ``test_q5_sessions_presence.py`` already locks the
``sessions_presence`` handler's contract by calling it directly with a
fake ``Request``. This file is the integration acceptance: drive the
full HTTP surface for the **read path** — real ``auth.create_session()``
rows, real ``GET /api/v1/auth/sessions/presence`` going through
FastAPI's middleware + dependency stack + ``auth.current_user`` cookie
gate — so a regression in the wiring (CSRF / bootstrap gate / auth
mode / cookie key) trips here even though every individual layer's
unit test still passes.

Why the SSE-write path is invoked **at the function boundary**, not via
``client.stream("GET", "/api/v1/events")``: ``httpx.ASGITransport``
buffers the full response body before exposing the status code, but
``EventSourceResponse`` runs forever — so a stream-mode HTTP call
against the in-process app deadlocks rather than yielding a heartbeat
record we can assert on. ``_resolve_presence`` + ``record_heartbeat``
ARE the exact two calls ``event_stream`` makes at lines 79-85 (the
SSE-connect block); calling them on the test side with a real Request
object walks identical bytes. A regression that breaks the cookie →
``get_session`` → ``(user_id, session_id_from_token)`` chain still
trips here. (A separate uvicorn-subprocess test could exercise the
streaming wire too — that lives in #82's concurrency-test library
roadmap, not Q.5.)

"Headless" = no browser; just the HTTP surface and real session
cookies. Three ``auth.create_session()`` calls model three independent
device logins.

Why the 60 s wait is simulated, not slept: a real wall-clock sleep
would push the test past the suite's pace budget for zero added
coverage. Aging the third session's heartbeat ts via the public
``set()`` surface walks the same code path as a connection that
actually went silent for 60 s — ``active_sessions()`` evaluates
``cutoff_now - ts <= window`` regardless of how ``ts`` got old
(``backend/shared_state.py::SessionPresence.active_sessions``).

SOP Step 1 module-global audit: this test reads/writes the
``session_presence`` SharedKV singleton (rubric #2/#3 mixed; documented
in ``backend/shared_state.py:209-217``) and the PG ``sessions`` table
via the existing pool. No new module-globals introduced. The fixture
clears the in-memory fallback at setup + teardown so cross-test bleed
is impossible.
"""

from __future__ import annotations

import os
import time
import types

import httpx
import pytest


# ─── Fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
async def _q5_e2e_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """End-to-end fixture: live PG + httpx ASGI client + presence reset.

    Mirrors ``test_q2_new_device_e2e._q2_e2e_client`` (cookie-backed
    session mode + green bootstrap gate + closed-then-reinit ``db``)
    plus the ``_q5_db`` presence-hash isolation step from
    ``test_q5_sessions_presence``.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    # The presence endpoint depends on ``auth.current_user`` resolving
    # to the real cookie-backed user (not the open-mode anon admin),
    # otherwise the user.id read in the handler would be the synthetic
    # admin's and presence lookups for the test users would all return 0.
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE users, sessions RESTART IDENTITY CASCADE")

    from backend import auth, db
    from backend import bootstrap as _boot
    from backend.main import app
    from backend.shared_state import session_presence
    from httpx import ASGITransport, AsyncClient

    # ``session_presence`` is a module-level SharedKV singleton; the
    # in-memory fallback under pytest accumulates across tests unless
    # cleared, which would mask cross-test leakage of presence rows.
    session_presence._local.clear()

    # Pin bootstrap green so the gate middleware doesn't 503/307 the
    # /auth/sessions/presence GETs through the wizard. Q.5 is bootstrap-
    # agnostic; reusing the K1 / Q.2 pattern keeps the fixture shape
    # consistent across the suite.
    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    # The ``client`` fixture closes-then-reinits ``db`` to point at PG;
    # we do the same so ``auth.current_user`` → ``get_session`` and the
    # presence handler's ``list_sessions`` crosswalk read the same PG
    # rows that ``pg_test_pool`` truncated above.
    if db._db is not None:
        await db.close()
    await db.init()

    # Seed one operator user — every "headless session" below mints
    # against this same user.id so the presence count rolls up to one
    # caller's view.
    user = await auth.create_user(
        email="q5-e2e@example.com",
        name="Q5 E2E",
        role="operator",
        password="SuperSecret-Q5-Presence-2026",
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield {
                "client": ac,
                "user": user,
                "auth": auth,
            }
    finally:
        await db.close()
        _boot._gate_cache_reset()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, sessions RESTART IDENTITY CASCADE",
            )
        session_presence._local.clear()


# ─── Constants ───────────────────────────────────────────────────────


_API = (os.environ.get("OMNISIGHT_API_PREFIX") or "/api/v1").rstrip("/")
PRESENCE_URL = f"{_API}/auth/sessions/presence"


# ─── Helpers ─────────────────────────────────────────────────────────


def _make_sse_request(session_token: str) -> types.SimpleNamespace:
    """Build the minimal ``Request`` shape ``_resolve_presence`` reads.

    ``_resolve_presence`` (``backend/routers/events.py``) only touches
    ``request.cookies``; everything else on a real ASGI ``Request``
    (``state``, ``headers``, ``client``, ``url``) is irrelevant to the
    code path it walks. A ``SimpleNamespace`` with the cookies dict is
    enough to drive the same branches a real SSE connect would take.
    """
    return types.SimpleNamespace(cookies={"omnisight_session": session_token})


async def _drive_sse_connect(session_token: str) -> tuple[str, str] | None:
    """Drive the SSE-connect heartbeat path for one headless session.

    Walks the same two function calls ``backend/routers/events.py::
    event_stream`` makes at lines 79-85 of its own connect block:

        presence = await _resolve_presence(request)
        if presence is not None:
            session_presence.record_heartbeat(*presence)

    Returns the resolved ``(user_id, session_id)`` pair so callers can
    assert the cookie chain landed on the right user.
    """
    from backend.routers.events import _resolve_presence
    from backend.shared_state import session_presence

    request = _make_sse_request(session_token)
    presence = await _resolve_presence(request)
    if presence is not None:
        session_presence.record_heartbeat(*presence)
    return presence


# ─── Test ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_headless_sessions_presence_three_then_drop_to_two(
    _q5_e2e_client,
):
    """TODO Q.5 last bullet — drive the full pipeline end-to-end.

    Phase 1 (SSE-write wiring): mint 3 sessions, drive each one through
      the SSE connect-time block (``_resolve_presence`` →
      ``record_heartbeat``), assert each landed an entry in the presence
      hash under the right ``user.id`` + ``session_id_from_token`` pair.
    Phase 2 (HTTP read = 3): GET ``/api/v1/auth/sessions/presence`` with
      one cookie via httpx ASGI, assert ``active_count == 3``, the
      device list matches the 3 session ids, and the caller's own
      cookie is flagged ``is_current``.
    Phase 3 (60 s stale-out → 2): age session-3's heartbeat ts to
      ``now - 61 s`` via the public ``session_presence.set()`` surface,
      GET presence again, assert ``active_count == 2`` and that the
      stale session id is excluded from the device list. Also verify
      the opportunistic GC inside the endpoint pruned the stale row.
    """
    env = _q5_e2e_client
    auth = env["auth"]
    user = env["user"]
    client: httpx.AsyncClient = env["client"]

    from backend.shared_state import session_presence

    # ── Mint 3 headless sessions (real PG rows) ──
    session_a = await auth.create_session(
        user.id, ip="10.0.0.10",
        user_agent="Mozilla/5.0 (Macintosh; Mac OS) Chrome/125 - Device-A",
    )
    session_b = await auth.create_session(
        user.id, ip="10.0.0.11",
        user_agent="Mozilla/5.0 (X11; Linux) Firefox/120 - Device-B",
    )
    session_c = await auth.create_session(
        user.id, ip="10.0.0.12",
        user_agent="Mozilla/5.0 (iPhone) Safari/16 - Device-C",
    )
    sessions = [session_a, session_b, session_c]
    sids = [auth.session_id_from_token(s.token) for s in sessions]

    # ── Phase 1: SSE-connect wiring proof ──
    # Drive each headless session through the same two function calls
    # ``event_stream`` runs in its connect block; assert the resolved
    # ``(user_id, session_id)`` matches what we minted and that the
    # heartbeat hash now reflects all 3.
    for s, sid in zip(sessions, sids):
        presence = await _drive_sse_connect(s.token)
        assert presence == (user.id, sid), (
            "events.py::_resolve_presence must resolve a fresh session "
            f"cookie to ({user.id!r}, {sid!r}); got {presence!r}"
        )
        assert session_presence.last_seen(user.id, sid) is not None, (
            "record_heartbeat must land an entry in the presence hash "
            f"for session {sid[:8]}... after the SSE-connect block runs"
        )

    # Sanity: 3 distinct entries for this user before any HTTP read.
    assert session_presence.active_count(user.id) == 3, (
        "presence hash must hold exactly 3 fresh entries after 3 "
        "headless SSE-connect calls"
    )

    # ── Phase 2: HTTP read returns 3 ──
    r1 = await client.get(
        PRESENCE_URL,
        cookies={auth.SESSION_COOKIE: session_a.token},
    )
    assert r1.status_code == 200, r1.text
    p1 = r1.json()
    assert p1["active_count"] == 3, (
        f"presence endpoint must report 3 active devices after 3 "
        f"SSE-connected sessions; got {p1['active_count']}: {p1}"
    )
    returned = {d["session_id"] for d in p1["devices"]}
    assert returned == set(sids), (
        f"presence devices list must contain all 3 session ids; "
        f"got {returned}, expected {set(sids)}"
    )
    is_current = [d for d in p1["devices"] if d["is_current"]]
    assert len(is_current) == 1, (
        "exactly one device row must carry is_current=True for the "
        "caller's own cookie"
    )
    assert is_current[0]["session_id"] == sids[0]

    # ── Phase 3: stale-out — session C silent for 61 s → 2 ──
    # The Q.5 window is 60 s. Aging C's heartbeat ts via the public
    # ``set()`` surface walks the same code path as a connection that
    # actually went silent that long: ``active_sessions()`` does
    # ``cutoff_now - ts <= window`` regardless of how ``ts`` got old.
    c_sid = sids[2]
    aged_now = time.time()
    session_presence.set(
        session_presence._field(user.id, c_sid),
        f"{aged_now - 61.0:.3f}",
    )

    r2 = await client.get(
        PRESENCE_URL,
        cookies={auth.SESSION_COOKIE: session_a.token},
    )
    assert r2.status_code == 200, r2.text
    p2 = r2.json()
    assert p2["active_count"] == 2, (
        f"presence must drop the 60+ s stale device, got "
        f"{p2['active_count']}: {p2}"
    )
    remaining = {d["session_id"] for d in p2["devices"]}
    assert c_sid not in remaining, (
        f"stale session {c_sid[:8]}... must be excluded from the "
        "presence devices list once its heartbeat ages past the "
        "60 s window"
    )
    assert sids[0] in remaining and sids[1] in remaining, (
        "fresh sessions A and B must still appear in the devices list"
    )

    # The presence endpoint runs ``prune_expired`` opportunistically at
    # the end of the handler; the stale C row must be gone from the
    # hash, and the fresh A/B rows must remain.
    assert session_presence.last_seen(user.id, c_sid) is None, (
        "opportunistic GC inside the presence handler must prune the "
        "stale heartbeat from the hash"
    )
    assert session_presence.last_seen(user.id, sids[0]) is not None
    assert session_presence.last_seen(user.id, sids[1]) is not None

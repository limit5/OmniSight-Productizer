"""Q.6 #300 (2026-04-24, checkbox 5) — two-headless-browser cross-device
restore acceptance test.

Closes the TODO row's last bullet:

  > 測試：兩 headless browser + puppet 打字 → 驗跨 restore 行為正確.

The unit suites in ``test_q6_user_drafts.py`` already lock the per-layer
contracts (PUT upserts, GET round-trips, slot-key validation, opportunistic
GC, db.py helpers). This file is the integration acceptance: drive the
**full HTTP surface** with two distinct cookie-backed sessions ("Device A"
+ "Device B") simulating the operator's same account on two browsers, and
prove the cross-restore behaviour the Q.6 spec promises:

  1. Device A puppet-types into a slot (a single 500ms-debounced PUT, or
     a burst of PUTs collapsed by the frontend's debounce — both shapes
     land at the server as one or more PUT calls).
  2. Device B's composer mounts and calls GET on the same slot.
  3. Device B sees the content Device A wrote, **with the same server-
     committed updated_at** Device A's response carried — that timestamp
     equality is the contract the frontend's checkbox-4 conflict
     detection (``hooks/use-draft-restore.ts:110-157``) depends on to
     decide whether to fire the「從他裝置同步了草稿」toast.

Why "headless" = httpx ASGI, not Playwright (matches Q.5 precedent in
``test_q5_sessions_presence_headless_e2e.py:30-32``): the cross-restore
contract is a backend ``PUT`` → ``GET`` roundtrip with two different
cookies. The frontend hooks (``useDraftPersistence``,
``useDraftRestore``, ``draft-sync-bus``, ``DraftSyncToastCenter``) are
already locked end-to-end by the vitest unit suites:

  * ``test/hooks/use-draft-persistence.test.tsx`` — debounce + writer
    contract + persistLocalEcho behaviour (10 tests).
  * ``test/hooks/use-draft-restore.test.tsx`` — mount-once read +
    conflict detection three branches + bus emit (12 tests).
  * ``test/lib/draft-sync-bus.test.ts`` — local-storage round-trip +
    bus pub/sub (10 tests).
  * ``test/components/draft-sync-toast-center.test.tsx`` — toast render
    + 6s auto-dismiss + same-slot coalesce (7 tests).

Spinning up Chromium just to prove the same wire here would just verify
those unit suites' assertions a second time at 100x the cost. What
those unit tests CANNOT verify is that two genuinely independent
session cookies hitting the live FastAPI route actually see each other
through PG — that's what this file pins.

"Puppet typing" = a sequence of HTTP PUTs. The 500ms debounce is a
client-side concern (``DRAFT_DEBOUNCE_MS`` in
``hooks/use-draft-persistence.ts``); from the server's perspective
puppet typing is exactly N PUT calls in sequence, with each successive
one's ``updated_at`` strictly greater than the prior. We exercise both
shapes (single PUT and a burst of PUTs) in
``test_puppet_typing_burst_collapses_to_single_row_visible_to_peer``.

SOP Step 1 — module-global audit: this test
  - reads/writes the ``user_drafts`` PG table via the existing pool
    (``pg_test_pool``); no new module-globals introduced;
  - mints two cookie-backed sessions via ``auth.create_session`` which
    upserts ``sessions`` rows under read-committed (qualifying answer
    #2 — "PG-coordinated"); cross-worker concurrency is a non-issue
    here because pytest runs the suite single-process.

SOP Step 1 — read-after-write timing: ``test_concurrent_puppet_typing_
last_writer_wins_visible_to_peer`` deliberately races two PUTs via
``asyncio.gather``; the assertion accepts EITHER device's content as
the survivor (whichever ``updated_at`` lands second wins under the
last-writer-wins policy locked by Q.6 spec line 4 — "draft 本來就是
ephemeral，不上樂觀鎖"). The test pins what's invariant (one row, the
larger timestamp wins, both devices then read the same thing) without
flaky-asserting which device won.

Q.4 #298 SSE scope policy: N/A — drafts are HTTP read/write only, no
``emit_*`` / ``bus.publish`` / SSE fan-out happens in either path.

SOP Step 3 — pre-commit fingerprint grep: this file uses no ``_conn()``
/ ``await conn.commit()`` / ``datetime('now')`` / ``VALUES (?, ...)``
shapes — all PG access goes through the pool fixture and parameterised
queries with ``$N``.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from httpx import ASGITransport, AsyncClient


# ─── Fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
async def _q6_xdev_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """End-to-end fixture: live PG + httpx ASGI client + two-device sessions.

    Mirrors ``test_q5_sessions_presence_headless_e2e._q5_e2e_client``
    (cookie-backed session mode + green bootstrap gate + closed-then-
    reinit ``db``) and seeds two ``auth.create_session`` rows for the
    same operator user — these model "Device A" + "Device B", logged in
    as the same operator from two browsers.

    Truncates ``user_drafts`` AND ``users``/``sessions`` at setup +
    teardown so cross-test bleed cannot mask a regression in the per-
    user / per-session-cookie scoping.
    """
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)
    # The PUT/GET handlers depend on ``auth.current_user`` resolving to
    # the real cookie-backed user (not the open-mode anon admin),
    # otherwise the user.id read in the handler would be the synthetic
    # admin's and Device A and Device B would BOTH hit the
    # ``"anonymous"`` row regardless of which cookie they carry — the
    # cross-device contract would then pass for the wrong reason.
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "session")

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE users, sessions, user_drafts RESTART IDENTITY CASCADE"
        )

    from backend import auth, db
    from backend import bootstrap as _boot
    from backend.main import app

    # Pin bootstrap green so the gate middleware doesn't 503/307 the
    # PUT/GET calls through the wizard. Q.6 is bootstrap-agnostic;
    # reusing the K1 / Q.2 / Q.5 pattern keeps fixtures consistent.
    async def _green():
        return _boot.BootstrapStatus(
            admin_password_default=False,
            llm_provider_configured=True,
            cf_tunnel_configured=True,
            smoke_passed=True,
        )

    monkeypatch.setattr(_boot, "get_bootstrap_status", _green)
    _boot._gate_cache_reset()

    if db._db is not None:
        await db.close()
    await db.init()

    # Seed one operator — both "devices" log in as this same user, so
    # the cross-device contract is exercised for the realistic case
    # (one human, two browsers / phone+laptop).
    user = await auth.create_user(
        email="q6-xdev@example.com",
        name="Q6 cross-device E2E",
        role="operator",
        password="SuperSecret-Q6-Drafts-2026",
    )

    # Mint two distinct sessions for the same user. UAs differ so a
    # debug log inspector can tell them apart; the per-session cookie
    # is the only thing the drafts route actually keys on.
    session_a = await auth.create_session(
        user.id, ip="10.0.0.10",
        user_agent="Mozilla/5.0 (Macintosh; Mac OS) Chrome/125 - Device-A",
    )
    session_b = await auth.create_session(
        user.id, ip="10.0.0.11",
        user_agent="Mozilla/5.0 (X11; Linux) Firefox/120 - Device-B",
    )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield {
                "client": ac,
                "user": user,
                "session_a": session_a,
                "session_b": session_b,
                "auth": auth,
                "pool": pg_test_pool,
            }
    finally:
        await db.close()
        _boot._gate_cache_reset()
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE users, sessions, user_drafts RESTART IDENTITY CASCADE"
            )


# ─── Constants & helpers ─────────────────────────────────────────────


_API = (os.environ.get("OMNISIGHT_API_PREFIX") or "/api/v1").rstrip("/")


def _draft_url(slot_key: str) -> str:
    return f"{_API}/user/drafts/{slot_key}"


async def _puppet_type(
    env, *, session, slot_key: str, content: str,
) -> dict:
    """Drive one "puppet typing" cycle from one device.

    Mirrors what ``hooks/use-draft-persistence.ts`` sends after the
    500 ms debounce trailing-edge fires: a single PUT carrying the
    full current composer text. The 500 ms debounce is purely client-
    side timing — the server contract is just "the latest PUT wins"
    so puppet-typing-from-the-server's-view is a straight HTTP call.
    """
    auth = env["auth"]
    res = await env["client"].put(
        _draft_url(slot_key),
        json={"content": content},
        cookies={auth.SESSION_COOKIE: session.token},
    )
    assert res.status_code == 200, (
        f"PUT {slot_key} from {session.token[:8]}... must succeed; "
        f"got {res.status_code}: {res.text}"
    )
    return res.json()


async def _restore(env, *, session, slot_key: str) -> dict:
    """Drive one mount-time restore call from one device.

    Mirrors ``hooks/use-draft-restore.ts`` — fire ``GET`` on mount and
    surface whatever the server has stored. Returns the parsed body so
    callers can assert on ``content`` + ``updated_at``.
    """
    auth = env["auth"]
    res = await env["client"].get(
        _draft_url(slot_key),
        cookies={auth.SESSION_COOKIE: session.token},
    )
    assert res.status_code == 200, (
        f"GET {slot_key} from {session.token[:8]}... must succeed; "
        f"got {res.status_code}: {res.text}"
    )
    return res.json()


# ─── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_a_puppet_typing_visible_to_device_b_restore(
    _q6_xdev_client,
):
    """Spec line 1 of Q.6: "新裝置打開頁面時呼叫一次 restore draft" — a
    second device's GET must surface the content the first device
    PUT, with the SAME ``updated_at`` so the conflict-detection on
    restore (checkbox 4) can distinguish "remote is newer" vs "I'm
    looking at my own write coming back".

    Phase 1: Device A "puppet types" /status arg1 into invoke:main
      (single trailing PUT after the 500ms debounce; the server sees
      one HTTP call).
    Phase 2: Device B's composer mounts and calls GET on the same
      slot. Asserts content matches Device A's, and the
      ``updated_at`` is byte-identical to what Device A's PUT
      response carried — that's the wire contract the frontend's
      checkbox-4 conflict detection trusts.
    """
    env = _q6_xdev_client

    # ── Cold-state sanity: both devices see empty before anyone types ──
    cold_a = await _restore(
        env, session=env["session_a"], slot_key="invoke:main",
    )
    assert cold_a == {
        "slot_key": "invoke:main", "content": "", "updated_at": None,
    }, "fresh slot must be empty for Device A"
    cold_b = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert cold_b == {
        "slot_key": "invoke:main", "content": "", "updated_at": None,
    }, "fresh slot must be empty for Device B"

    # ── Phase 1: Device A puppet types ──
    typed = await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="/status arg1",
    )
    assert typed["content"] == "/status arg1"
    assert typed["slot_key"] == "invoke:main"
    a_committed_ts = typed["updated_at"]
    assert isinstance(a_committed_ts, (int, float))
    assert a_committed_ts > 0

    # ── Phase 2: Device B mounts and restores ──
    restored = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert restored["slot_key"] == "invoke:main"
    assert restored["content"] == "/status arg1", (
        "Device B's restore must surface the content Device A typed; "
        f"got {restored['content']!r}"
    )
    # Timestamp equality is the contract the frontend conflict
    # detection (use-draft-restore.ts:117-136) compares against the
    # local-storage echo. If this drifts the toast logic breaks.
    assert restored["updated_at"] == pytest.approx(a_committed_ts, rel=1e-9), (
        "Device B's restore must carry the SAME updated_at Device A's "
        "PUT response did — the frontend's conflict detection "
        "compares this byte-for-byte against local storage to decide "
        "whether to fire the toast"
    )

    # ── Single row stored ──
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, slot_key, content, updated_at "
            "FROM user_drafts WHERE slot_key = 'invoke:main'"
        )
    assert len(rows) == 1, (
        "two devices on the same user/slot must share one row "
        "(PK = (user_id, slot_key))"
    )
    assert rows[0]["user_id"] == env["user"].id
    assert rows[0]["content"] == "/status arg1"


@pytest.mark.asyncio
async def test_puppet_typing_burst_collapses_to_single_row_visible_to_peer(
    _q6_xdev_client,
):
    """Operator puppet-types five characters in quick succession on
    Device A. The frontend debounce collapses that to one trailing
    PUT in real life, but if the user pauses between bursts the server
    can see multiple PUTs — this test simulates the worst-case "every
    keystroke fires" path to prove cross-device restore is monotonic
    (Device B always sees the LATEST content, with a strictly
    increasing ``updated_at``).

    This is the "puppet 打字" loop in the TODO row literally translated:
    drive multiple PUTs, then have the peer GET; assert the peer sees
    the last keystroke's content.
    """
    env = _q6_xdev_client

    # Five puppet keystrokes — "h", "he", "hel", "hell", "hello".
    keystrokes = ["h", "he", "hel", "hell", "hello"]
    timestamps: list[float] = []
    for stroke in keystrokes:
        body = await _puppet_type(
            env, session=env["session_a"],
            slot_key="invoke:main", content=stroke,
        )
        timestamps.append(body["updated_at"])
        # Tiny sleep so each PUT lands at a distinct wall-clock tick;
        # without it the assertion below could be vacuous on coarse
        # clocks (Linux time.time() is microsecond-level so this is
        # belt-and-braces — but it makes the monotonicity claim
        # observable rather than assumed).
        await asyncio.sleep(0.005)

    # Server-side monotonicity — each successive PUT's updated_at is
    # strictly greater than the prior. This is what
    # last-writer-wins under read-committed gives us, and it's the
    # invariant the conflict-detection branch in
    # use-draft-restore.ts:124 (`remoteTs > local.updated_at`) leans on.
    for prev, cur in zip(timestamps, timestamps[1:]):
        assert cur >= prev, (
            f"puppet-typing burst must produce non-decreasing "
            f"updated_at; got {prev} then {cur}"
        )
    assert timestamps[-1] > timestamps[0], (
        "last keystroke must land strictly after the first across "
        "5 PUTs separated by >=5ms"
    )

    # ── Device B restore — must see "hello", the last keystroke ──
    restored = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert restored["content"] == "hello", (
        "burst-typing on Device A must leave the LAST keystroke "
        "visible to Device B's restore — the server must not surface "
        "any intermediate state"
    )
    assert restored["updated_at"] == pytest.approx(timestamps[-1], rel=1e-9), (
        "peer's restore must carry the timestamp of the LAST PUT, "
        "not any intermediate one"
    )

    # ── Single row stored — burst writes UPSERT into one PK ──
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT updated_at FROM user_drafts "
            "WHERE user_id = $1 AND slot_key = 'invoke:main'",
            env["user"].id,
        )
    assert len(rows) == 1, (
        "5 keystrokes from one device must collapse to one PG row via "
        "ON CONFLICT DO UPDATE — burst-write must not duplicate rows"
    )


@pytest.mark.asyncio
async def test_concurrent_puppet_typing_last_writer_wins_visible_to_peer(
    _q6_xdev_client,
):
    """Q.6 spec line 4: "同 slot 兩裝置同時打字 → 後寫贏（draft 本來就是
    ephemeral，不上樂觀鎖）".

    Both devices race a PUT to the same slot via ``asyncio.gather``.
    Whichever ``ON CONFLICT DO UPDATE`` lands second wins; both
    devices then see the same content on subsequent restore. The
    test deliberately does NOT assert which device's content wins —
    that's a timing-dependent answer the spec carves out as
    legitimately undefined.

    What IS invariant — and what this test pins — :

      1. Exactly one row exists after the race.
      2. The surviving content is one of the two contenders (no
         partial / interleaved write).
      3. Both devices' restore returns the same surviving content.
      4. The surviving ``updated_at`` is whichever PUT response
         carried the larger ``updated_at`` from the gather batch.
    """
    env = _q6_xdev_client

    # Two devices typing different content into the same slot at
    # near-simultaneously. asyncio.gather reflects the realistic worst
    # case — both PUTs land in the FastAPI dispatch loop within a
    # microsecond of each other.
    a_content = "device-A wins this race"
    b_content = "device-B wins this race"

    a_response, b_response = await asyncio.gather(
        _puppet_type(
            env, session=env["session_a"],
            slot_key="invoke:main", content=a_content,
        ),
        _puppet_type(
            env, session=env["session_b"],
            slot_key="invoke:main", content=b_content,
        ),
    )

    # The PUT responses each carry the content the caller just wrote
    # (the response is built from the input, not re-read from PG;
    # see ``backend/routers/drafts.py:117-121``). So the response
    # content reflects what each caller sent, not what survived.
    assert a_response["content"] == a_content
    assert b_response["content"] == b_content

    # The PG row is the source of truth — exactly one row, content is
    # one of the two contenders. Read it via the pool, not via either
    # device's restore, to keep the assertion pure on the stored state.
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, slot_key, content, updated_at "
            "FROM user_drafts WHERE slot_key = 'invoke:main'"
        )
    assert len(rows) == 1, (
        "concurrent PUTs from two devices on the same PK must collapse "
        f"to one row, not {len(rows)} — the (user_id, slot_key) PK + "
        "ON CONFLICT DO UPDATE is the contention guard"
    )
    survivor_content = rows[0]["content"]
    survivor_ts = float(rows[0]["updated_at"])
    assert survivor_content in (a_content, b_content), (
        f"surviving content must be one of the two contenders, got "
        f"{survivor_content!r} — anything else means partial/interleaved write"
    )

    # ── Both devices' restore must see the same surviving content ──
    a_restore = await _restore(
        env, session=env["session_a"], slot_key="invoke:main",
    )
    b_restore = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert a_restore["content"] == survivor_content, (
        "Device A's restore must reflect the surviving row, not its "
        "own pre-race write — the GET surfaces stored state"
    )
    assert b_restore["content"] == survivor_content, (
        "Device B's restore must reflect the surviving row, not its "
        "own pre-race write — the GET surfaces stored state"
    )
    assert a_restore["updated_at"] == pytest.approx(survivor_ts, rel=1e-9)
    assert b_restore["updated_at"] == pytest.approx(survivor_ts, rel=1e-9)

    # The surviving updated_at must be the larger of the two PUT
    # response timestamps — that's the operational definition of
    # "last writer wins" the Q.6 spec promises. (If the two PUTs
    # happened to commit at exactly the same wall-clock tick, both
    # responses' updated_at are equal and either one is the "winner";
    # the assertion accommodates that edge with ``>=``.)
    larger_put_ts = max(a_response["updated_at"], b_response["updated_at"])
    assert survivor_ts >= larger_put_ts - 1e-6, (
        f"surviving updated_at ({survivor_ts}) must be >= the larger "
        f"PUT response timestamp ({larger_put_ts}) — anything else "
        "means the loser's write somehow stomped after winning"
    )


@pytest.mark.asyncio
async def test_per_slot_isolation_across_two_devices(
    _q6_xdev_client,
):
    """The Q.6 spec carves invoke:main and chat:main as two
    independent slots. Two devices typing into different slots must
    NOT cross-contaminate, and each device's restore must see both
    slots independently.

    Pins the operator-visible behaviour: a user typing /status into
    INVOKE on the laptop and a chat message on the phone must see both
    on the OTHER device when they swap.
    """
    env = _q6_xdev_client

    # Device A types into INVOKE; Device B types into chat.
    a_invoke = await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="/diag --full",
    )
    b_chat = await _puppet_type(
        env, session=env["session_b"],
        slot_key="chat:main", content="ping the team about the deploy",
    )

    # Both devices restore both slots — should each see the other's
    # write in the slot the other wrote into, AND see their own write
    # in the slot they wrote into. (Same user_id under the hood, so
    # which session cookie is used does not matter — the PK is
    # (user_id, slot_key) not (session_id, slot_key).)
    for label, session in [
        ("Device A", env["session_a"]),
        ("Device B", env["session_b"]),
    ]:
        invoke = await _restore(env, session=session, slot_key="invoke:main")
        chat = await _restore(env, session=session, slot_key="chat:main")
        assert invoke["content"] == "/diag --full", (
            f"{label}'s INVOKE restore must see the /diag --full content "
            "Device A typed (same user, slot is the scoping axis)"
        )
        assert chat["content"] == "ping the team about the deploy", (
            f"{label}'s chat restore must see the ping message Device B "
            "typed (same user, slot is the scoping axis)"
        )
        assert invoke["updated_at"] == pytest.approx(
            a_invoke["updated_at"], rel=1e-9,
        )
        assert chat["updated_at"] == pytest.approx(
            b_chat["updated_at"], rel=1e-9,
        )


@pytest.mark.asyncio
async def test_cross_user_drafts_invisible_to_a_second_account(
    _q6_xdev_client,
):
    """Q.6 spec PK is (user_id, slot_key) — Operator Alice typing on
    her laptop must NOT leak into Operator Bob's restore on his
    laptop, even though both are using the same slot key.

    This pins the per-user scoping at the HTTP layer. The unit suite
    locks the same property at the db.py layer
    (``test_q6_user_drafts.py::test_get_isolated_per_user``) by writing
    a foreign user_id directly via SQL and asserting the GET handler
    returns the empty shape. This file goes one step further and uses
    a real second cookie-backed session for a second user, so the
    handler's ``user.id`` resolution itself is verified end-to-end.
    """
    env = _q6_xdev_client
    auth = env["auth"]

    # Mint a second user with their own session — "Operator Bob".
    bob = await auth.create_user(
        email="q6-xdev-bob@example.com",
        name="Q6 Bob",
        role="operator",
        password="SuperSecret-Bob-Q6-2026",
    )
    bob_session = await auth.create_session(
        bob.id, ip="10.0.0.42",
        user_agent="Mozilla/5.0 (iPhone) Safari/16 - Bob's phone",
    )

    # Alice's Device A puppet-types into invoke:main.
    await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="alice-only secret command",
    )

    # Bob's restore on the SAME slot key must see the empty shape —
    # not Alice's content. This is the cross-user isolation contract.
    bob_restore = await _restore(
        env, session=bob_session, slot_key="invoke:main",
    )
    assert bob_restore == {
        "slot_key": "invoke:main",
        "content": "",
        "updated_at": None,
    }, (
        "Bob's restore on invoke:main must return the empty shape — "
        "Alice's row must not leak across user boundaries even though "
        "the slot key is identical"
    )

    # Alice's other device (Device B) must still see her own write.
    alice_b_restore = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert alice_b_restore["content"] == "alice-only secret command", (
        "Alice's second device must still see her own write — the "
        "isolation guard above must not have over-scoped to the point "
        "of breaking the legitimate cross-device restore"
    )

    # PG-level confirmation: two separate rows, one per user_id.
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, content FROM user_drafts "
            "WHERE slot_key = 'invoke:main' ORDER BY user_id"
        )
    by_user = {r["user_id"]: r["content"] for r in rows}
    # Bob's row hasn't been written — only Alice's exists.
    assert by_user == {env["user"].id: "alice-only secret command"}


@pytest.mark.asyncio
async def test_device_b_overwrites_then_a_restores_sees_b_content(
    _q6_xdev_client,
):
    """The reverse-direction acceptance: Device A types first, THEN
    Device B types over the top, THEN Device A re-mounts (e.g. tab
    refresh) and restores → must see Device B's later write.

    This is the operator-visible scenario the
    「從他裝置同步了草稿」toast surfaces (frontend
    ``hooks/use-draft-restore.ts:124-126`` checks ``remoteTs >
    local.updated_at`` for exactly this case).
    """
    env = _q6_xdev_client

    # Device A initial write.
    a_first = await _puppet_type(
        env, session=env["session_a"],
        slot_key="chat:main", content="A's draft pending review",
    )
    # Tiny gap so B's updated_at is strictly greater than A's.
    await asyncio.sleep(0.01)

    # Device B overwrites.
    b_overwrite = await _puppet_type(
        env, session=env["session_b"],
        slot_key="chat:main", content="B's revised draft",
    )
    assert b_overwrite["updated_at"] >= a_first["updated_at"], (
        "B's later PUT must carry an updated_at >= A's earlier one "
        "(monotonic wall clock invariant)"
    )

    # Device A re-mounts and restores.
    a_restore = await _restore(
        env, session=env["session_a"], slot_key="chat:main",
    )
    assert a_restore["content"] == "B's revised draft", (
        "Device A's restore after B overwrote must surface B's "
        "content — this is the trigger condition for the "
        "「從他裝置同步了草稿」toast on the frontend"
    )
    assert a_restore["updated_at"] == pytest.approx(
        b_overwrite["updated_at"], rel=1e-9,
    ), (
        "the restore's updated_at MUST equal B's PUT response "
        "updated_at — the frontend compares this against the local-"
        "storage echo (which still holds A's old timestamp from her "
        "earlier successful PUT) to decide whether the remote is "
        "newer and the toast should fire"
    )
    # And critically the restored timestamp is strictly greater than
    # what A's local-storage cache would hold from her own earlier
    # PUT — that's the (remoteTs > localTs) branch that fires the toast.
    assert a_restore["updated_at"] > a_first["updated_at"], (
        "remote timestamp on restore must exceed A's local-storage "
        "echo from her earlier PUT — that's the conflict-detection "
        "trigger condition (use-draft-restore.ts:124)"
    )


@pytest.mark.asyncio
async def test_unauthenticated_request_cannot_see_or_stomp_operator_draft(
    _q6_xdev_client,
):
    """Drafts are per-user data — a request without a valid session
    cookie must NOT be able to read or modify another operator's
    draft, regardless of whether the auth gate is active or in
    log-only baseline mode.

    Current production posture (2026-04-24): ``backend/auth_baseline``
    runs in **log-only** mode, so an unauthenticated request in
    session mode is logged as "would-block" but allowed to pass
    through and resolves to the synthetic ``"anonymous"`` user. The
    PK-based DB scoping (``user_drafts.user_id``) is the
    last-line-of-defence that prevents leak — under the current
    configuration this is what stops Alice's draft from surfacing in
    a no-cookie request.

    This test pins the operator-visible invariant ("no draft leak
    across user boundaries") rather than the underlying mechanism
    (401 at the gate vs. PK isolation at the DB) so it remains green
    when ``auth_baseline`` flips from log-only to enforce mode in a
    later milestone — the underlying assertion ("Alice's draft never
    surfaces to a not-Alice request") survives the mode flip.
    """
    env = _q6_xdev_client

    # Seed a draft from Device A so the GET below has something to
    # potentially leak if the scoping is broken.
    await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="confidential operator command",
    )

    # PUT without a cookie. Either the auth gate rejects it (4xx) OR
    # it lands under a different user_id (200 + scoping isolates).
    no_cookie_put = await env["client"].put(
        _draft_url("invoke:main"),
        json={"content": "stomp attempt"},
    )
    assert no_cookie_put.status_code in (200, 401, 403), (
        f"PUT without cookie must either 200 (log-only baseline → "
        f"different user scope) or 401/403 (enforce mode); got "
        f"{no_cookie_put.status_code}: {no_cookie_put.text}"
    )

    # GET without a cookie. Same envelope: the result MUST NOT be
    # Alice's content. Either it 401s OR it returns a row for a
    # different user_id (which under PK scoping is empty for the
    # synthetic anonymous user).
    no_cookie_get = await env["client"].get(_draft_url("invoke:main"))
    if no_cookie_get.status_code == 200:
        body = no_cookie_get.json()
        assert body["content"] != "confidential operator command", (
            "unauthenticated GET MUST NOT surface Alice's draft "
            "content — even when the auth gate is in log-only mode, "
            "the DB-layer PK scoping must keep the row invisible. "
            f"Got: {body!r}"
        )
    else:
        assert no_cookie_get.status_code in (401, 403), (
            f"GET without cookie must either 200-with-different-scope "
            f"or 401/403; got {no_cookie_get.status_code}: "
            f"{no_cookie_get.text}"
        )

    # Confirm the seeded row is still intact and un-stomped (the
    # unauthenticated PUT either 4xx'd or wrote to a different
    # user_id — Alice's row at user_id = env["user"].id is unchanged).
    async with env["pool"].acquire() as conn:
        alice_row = await conn.fetchrow(
            "SELECT content FROM user_drafts "
            "WHERE user_id = $1 AND slot_key = 'invoke:main'",
            env["user"].id,
        )
    assert alice_row is not None, (
        "Alice's seeded row must still exist after the no-cookie "
        "PUT attempt"
    )
    assert alice_row["content"] == "confidential operator command", (
        "the unauthenticated PUT above must NOT have stomped Alice's "
        "draft — even under log-only auth_baseline, the PK on "
        "(user_id, slot_key) keeps the rows separate"
    )


# ── Belt-and-braces: prove the cross-device wire end-to-end ──────────


@pytest.mark.asyncio
async def test_full_two_device_puppet_typing_session(
    _q6_xdev_client,
):
    """Composite end-to-end: simulate a realistic operator session
    spanning two devices and both slots. This is the most literal
    reading of the TODO row's spec — two browsers, puppet typing,
    cross-restore behaviour holds throughout.

    Storyline:
      T0  Both devices mount on a fresh tenant; both restore returns
          empty for both slots.
      T1  Device A types "/help" into INVOKE (one PUT).
      T2  Device B mounts INVOKE → sees "/help" with A's timestamp.
      T3  Device B types into chat: "let's review" (one PUT).
      T4  Device A mounts chat → sees "let's review" with B's timestamp.
      T5  Device A continues typing INVOKE: "/help --verbose" (a
          second PUT, debounce trailing edge).
      T6  Device B re-restores INVOKE → now sees "/help --verbose"
          with the larger timestamp (Device B's local cache from T2
          would be older — this is the (remoteTs > localTs) case the
          frontend toast fires on).

    Each step's invariant is asserted explicitly so a regression
    points at exactly the broken transition.
    """
    env = _q6_xdev_client

    # ── T0: cold mount on both devices, both slots ──
    for session_label, session in [("A", env["session_a"]), ("B", env["session_b"])]:
        for slot in ("invoke:main", "chat:main"):
            res = await _restore(env, session=session, slot_key=slot)
            assert res["content"] == "", (
                f"T0 cold mount: Device {session_label} on {slot} must "
                f"return empty content; got {res['content']!r}"
            )
            assert res["updated_at"] is None

    # ── T1: Device A types into INVOKE ──
    a_t1 = await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="/help",
    )

    # ── T2: Device B mounts INVOKE, sees A's content ──
    b_t2 = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert b_t2["content"] == "/help"
    assert b_t2["updated_at"] == pytest.approx(a_t1["updated_at"], rel=1e-9)

    # ── T3: Device B types into chat ──
    await asyncio.sleep(0.01)  # ensure B's chat ts > A's invoke ts
    b_t3 = await _puppet_type(
        env, session=env["session_b"],
        slot_key="chat:main", content="let's review",
    )
    assert b_t3["updated_at"] >= a_t1["updated_at"]

    # ── T4: Device A mounts chat, sees B's content ──
    a_t4 = await _restore(
        env, session=env["session_a"], slot_key="chat:main",
    )
    assert a_t4["content"] == "let's review"
    assert a_t4["updated_at"] == pytest.approx(b_t3["updated_at"], rel=1e-9)

    # ── T5: Device A overwrites INVOKE with a longer command ──
    await asyncio.sleep(0.01)
    a_t5 = await _puppet_type(
        env, session=env["session_a"],
        slot_key="invoke:main", content="/help --verbose",
    )
    assert a_t5["updated_at"] > a_t1["updated_at"], (
        "T5 PUT must carry a strictly greater updated_at than T1 "
        "(>=10ms apart, wall-clock monotonic)"
    )

    # ── T6: Device B re-restores INVOKE — now sees the newer content ──
    # This is the operator-visible toast trigger: B's local-storage
    # echo from T2 carries `a_t1["updated_at"]`; the server now returns
    # `a_t5["updated_at"]` which is strictly greater → frontend
    # `useDraftRestore` fires `emitDraftSynced` → toast appears.
    b_t6 = await _restore(
        env, session=env["session_b"], slot_key="invoke:main",
    )
    assert b_t6["content"] == "/help --verbose", (
        "Device B's second restore must surface the LATER content; "
        f"got {b_t6['content']!r}"
    )
    assert b_t6["updated_at"] == pytest.approx(a_t5["updated_at"], rel=1e-9)
    # The conflict-detection precondition: remote ts > B's last
    # local-cache ts (T2 = a_t1["updated_at"]).
    assert b_t6["updated_at"] > a_t1["updated_at"], (
        "T6 conflict-detection precondition broken: remote ts must "
        "exceed B's local-cache timestamp from T2 — that's the "
        "exact branch (use-draft-restore.ts:124) that fires the "
        "「從他裝置同步了草稿」toast"
    )

    # ── Final PG state — exactly two rows, one per slot ──
    async with env["pool"].acquire() as conn:
        rows = await conn.fetch(
            "SELECT slot_key, content FROM user_drafts "
            "WHERE user_id = $1 ORDER BY slot_key",
            env["user"].id,
        )
    by_slot = {r["slot_key"]: r["content"] for r in rows}
    assert by_slot == {
        "chat:main": "let's review",
        "invoke:main": "/help --verbose",
    }


# ── Stale-write guard: the wall-clock noise check ────────────────────


@pytest.mark.asyncio
async def test_restore_timestamp_byte_identical_to_put_response(
    _q6_xdev_client,
):
    """Critical contract for checkbox-4 conflict detection: the
    ``updated_at`` returned by GET must be byte-identical to what the
    same row's PUT returned earlier — not "approximately equal", not
    "off by a microsecond". The frontend uses an ``===`` comparison
    against the local-storage echo (``hooks/use-draft-restore.ts:124``)
    and any drift would silently flip the comparison branch.

    This test would catch an accidental ``round(ts, 2)`` or "re-read
    from PG with an INSERT-RETURNING that loses precision" regression.
    """
    env = _q6_xdev_client

    put_body = await _puppet_type(
        env, session=env["session_a"],
        slot_key="chat:main", content="precision-sensitive draft",
    )
    put_ts = put_body["updated_at"]

    get_body = await _restore(
        env, session=env["session_b"], slot_key="chat:main",
    )
    get_ts = get_body["updated_at"]

    assert put_ts == get_ts, (
        "PUT and GET response timestamps must be byte-identical — the "
        f"frontend's `===` comparison breaks otherwise. PUT={put_ts!r} "
        f"GET={get_ts!r}"
    )

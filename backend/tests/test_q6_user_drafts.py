"""Q.6 #300 (2026-04-24) — checkbox 1 contract: PUT /user/drafts/{slot_key}.

The TODO row spec — verbatim:

    目標路徑：(a) INVOKE 指令輸入框、(b) chat 輸入框。打字 500 ms
    debounce 後寫 ``user_drafts`` 表 ``(user_id, slot_key, content,
    updated_at)``，slot_key = ``invoke:main`` / ``chat:main``
    （未來擴：``chat:<thread_id>``）。

This suite locks the write-path contract:

  * ``test_put_creates_user_draft_row`` — first PUT inserts a row.
  * ``test_put_overwrites_existing_draft`` — second PUT upserts.
  * ``test_put_returns_server_committed_timestamp`` — the response
    carries the ``updated_at`` the server wrote, so the frontend
    can echo it into local storage for the Q.6 conflict-detection
    check on restore (checkbox 4).
  * ``test_put_isolated_per_slot_key`` — ``invoke:main`` and
    ``chat:main`` round-trip independently.
  * ``test_put_isolated_per_user`` — user A's PUT does not leak
    into user B's row.
  * ``test_put_rejects_malformed_slot_key`` — slot keys outside the
    ``ns:scope`` shape get a 400.
  * ``test_put_accepts_future_chat_thread_slot_key`` — the
    ``chat:<thread_id>`` extension shape passes validation.
  * ``test_put_opportunistic_gc_drops_stale_rows`` — the 24 h sweep
    fires after every PUT and prunes rows with ``updated_at`` older
    than the window.
  * ``test_put_appears_in_migrator_tables_in_order`` — drift guard
    (SOP Step 4 list-vs-source rule); the migrator must list
    ``user_drafts`` so a PG cutover doesn't silently lose data.

Audit evidence: alembic/versions/0022_user_drafts.py docstring +
backend/db.py::upsert_user_draft.
"""
from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def _drafts_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """Open auth mode (``current_user`` resolves to the synthetic
    anonymous-admin) + clean ``user_drafts`` between tests.

    Mirrors ``test_chat_emit_cross_device.py``'s ``_chat_client``
    pattern; the drafts table has no FK on ``users`` so we don't
    need to seed a row.
    """
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")

    from backend import db as _db
    from backend.main import app
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

    if _db._db is not None:
        await _db.close()
    await _db.init()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        _boot._gate_cache_reset()
        await _db.close()
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")


# ── PUT contract ──────────────────────────────────────────────────


async def test_put_creates_user_draft_row(
    _drafts_client: AsyncClient, pg_test_pool,
):
    res = await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main",
        json={"content": "/status arg1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["slot_key"] == "invoke:main"
    assert body["content"] == "/status arg1"
    assert isinstance(body["updated_at"], (int, float))
    assert body["updated_at"] > 0

    async with pg_test_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, slot_key, content, updated_at "
            "FROM user_drafts WHERE slot_key = 'invoke:main'"
        )
    assert row is not None
    assert row["user_id"] == "anonymous"
    assert row["content"] == "/status arg1"
    assert float(row["updated_at"]) == pytest.approx(body["updated_at"], rel=1e-6)


async def test_put_overwrites_existing_draft(
    _drafts_client: AsyncClient, pg_test_pool,
):
    """Q.6 conflict policy is last-writer-wins; ON CONFLICT DO UPDATE
    must REPLACE both ``content`` and ``updated_at``."""
    first = await _drafts_client.put(
        "/api/v1/user/drafts/chat:main",
        json={"content": "first draft"},
    )
    assert first.status_code == 200
    first_ts = first.json()["updated_at"]

    # Sleep enough that the second timestamp is strictly greater than
    # the first so the assertion is meaningful even on coarse clocks.
    time.sleep(0.01)

    second = await _drafts_client.put(
        "/api/v1/user/drafts/chat:main",
        json={"content": "second draft"},
    )
    assert second.status_code == 200
    second_ts = second.json()["updated_at"]
    assert second_ts >= first_ts

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT content, updated_at FROM user_drafts "
            "WHERE slot_key = 'chat:main'"
        )
    assert len(rows) == 1, "upsert must not duplicate the row"
    assert rows[0]["content"] == "second draft"
    assert float(rows[0]["updated_at"]) == pytest.approx(second_ts, rel=1e-6)


async def test_put_returns_server_committed_timestamp(
    _drafts_client: AsyncClient, pg_test_pool,
):
    """The Q.6 conflict-detection flow on restore (checkbox 4)
    compares the server-side ``updated_at`` against the local-storage
    cache, so the PUT response must echo what the server actually
    wrote."""
    before = time.time()
    res = await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main",
        json={"content": "x"},
    )
    after = time.time()
    assert res.status_code == 200
    ts = res.json()["updated_at"]
    assert before - 1.0 <= ts <= after + 1.0


async def test_put_isolated_per_slot_key(
    _drafts_client: AsyncClient, pg_test_pool,
):
    await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main", json={"content": "INVOKE"},
    )
    await _drafts_client.put(
        "/api/v1/user/drafts/chat:main", json={"content": "CHAT"},
    )

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT slot_key, content FROM user_drafts "
            "WHERE user_id = 'anonymous' ORDER BY slot_key"
        )
    by_slot = {r["slot_key"]: r["content"] for r in rows}
    assert by_slot == {"chat:main": "CHAT", "invoke:main": "INVOKE"}


async def test_put_isolated_per_user(
    _drafts_client: AsyncClient, pg_test_pool,
):
    """Direct DB write simulates user B; confirm the HTTP PUT for
    user A (anonymous) does not stomp on B's row.

    We can't easily multi-tenant the open-auth fixture, but the
    PK is (user_id, slot_key) so the per-user isolation is a pure
    db.py contract — verify by writing a "user-b" row directly,
    PUT-ing as anonymous, then asserting both rows survive.
    """
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_drafts (user_id, slot_key, content, "
            "updated_at, tenant_id) VALUES "
            "($1, $2, $3, $4, $5)",
            "user-b", "invoke:main", "B's draft", time.time(), "t-default",
        )

    res = await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main",
        json={"content": "A's draft"},
    )
    assert res.status_code == 200

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, content FROM user_drafts "
            "WHERE slot_key = 'invoke:main' ORDER BY user_id"
        )
    by_user = {r["user_id"]: r["content"] for r in rows}
    assert by_user == {"anonymous": "A's draft", "user-b": "B's draft"}


# ── slot_key validation ──────────────────────────────────────────


@pytest.mark.parametrize("bad_key", [
    "no-colon",                # missing namespace separator
    ":missing-ns",             # empty namespace
    "missing-scope:",          # empty scope
    "Invoke:Main",             # uppercase rejected (kebab/snake-case only)
    "invoke:main:extra",       # too many colons
    "ns/with/slash:scope",     # slashes are not in [a-z0-9_-]
])
async def test_put_rejects_malformed_slot_key(
    _drafts_client: AsyncClient, bad_key: str,
):
    res = await _drafts_client.put(
        f"/api/v1/user/drafts/{bad_key}",
        json={"content": "x"},
    )
    # FastAPI's path parser may reject "no-colon" or slash-bearing
    # strings at the routing layer — accept either 400 (handler
    # validator) or 404 (path didn't match the route at all). Both
    # leave the row unwritten which is the user-facing contract.
    assert res.status_code in (400, 404), (
        f"expected 400/404 for {bad_key!r}, got {res.status_code}: {res.text}"
    )


async def test_put_accepts_future_chat_thread_slot_key(
    _drafts_client: AsyncClient,
):
    """The Q.6 spec carves out ``chat:<thread_id>`` as the future
    extension; lock that path now so a checkbox 2/3 sweep doesn't
    silently regress."""
    res = await _drafts_client.put(
        "/api/v1/user/drafts/chat:01jw3p7c8e2v8fk9wnh7m5q4tz",
        json={"content": "thread-scoped draft"},
    )
    assert res.status_code == 200
    assert res.json()["slot_key"] == "chat:01jw3p7c8e2v8fk9wnh7m5q4tz"


# ── GET restore contract (checkbox 2) ────────────────────────────


async def test_get_returns_empty_shape_for_unknown_slot(
    _drafts_client: AsyncClient,
):
    """A slot that was never written returns a shaped empty body
    (``content=""`` / ``updated_at=null``) not 404 — the restore
    flow calls this on every page mount and a 404 would just
    pollute the DevTools network tab while forcing the frontend
    into a ``DraftResponse | null`` branch."""
    res = await _drafts_client.get("/api/v1/user/drafts/invoke:main")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body == {"slot_key": "invoke:main", "content": "", "updated_at": None}


async def test_get_returns_stored_content_after_put(
    _drafts_client: AsyncClient,
):
    """Round-trip: PUT then GET returns the same ``content`` and
    the server-committed ``updated_at`` so the frontend can echo
    the timestamp into local storage for the checkbox 4 conflict
    check."""
    put = await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main",
        json={"content": "/status --verbose"},
    )
    assert put.status_code == 200
    put_ts = put.json()["updated_at"]

    res = await _drafts_client.get("/api/v1/user/drafts/invoke:main")
    assert res.status_code == 200
    body = res.json()
    assert body["slot_key"] == "invoke:main"
    assert body["content"] == "/status --verbose"
    assert body["updated_at"] == pytest.approx(put_ts, rel=1e-6)


async def test_get_isolated_per_slot_key(
    _drafts_client: AsyncClient,
):
    """INVOKE and chat drafts must not cross-leak — they share the
    user_id but the PK is (user_id, slot_key) and GET must honour
    that scope."""
    await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main", json={"content": "INVOKE body"},
    )
    await _drafts_client.put(
        "/api/v1/user/drafts/chat:main", json={"content": "CHAT body"},
    )
    invoke = (await _drafts_client.get("/api/v1/user/drafts/invoke:main")).json()
    chat = (await _drafts_client.get("/api/v1/user/drafts/chat:main")).json()
    assert invoke["content"] == "INVOKE body"
    assert chat["content"] == "CHAT body"


async def test_get_isolated_per_user(
    _drafts_client: AsyncClient, pg_test_pool,
):
    """User B's draft must not surface to user A's GET — PK scopes
    on user_id and the handler must pass the current operator's id
    into ``get_user_draft``."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_drafts (user_id, slot_key, content, "
            "updated_at, tenant_id) VALUES "
            "($1, $2, $3, $4, $5)",
            "user-b", "invoke:main", "B's private draft",
            time.time(), "t-default",
        )

    res = await _drafts_client.get("/api/v1/user/drafts/invoke:main")
    assert res.status_code == 200
    body = res.json()
    # Fixture user id is "anonymous" (open auth) — B's row must be
    # invisible.
    assert body["content"] == ""
    assert body["updated_at"] is None


@pytest.mark.parametrize("bad_key", [
    "no-colon",
    ":missing-ns",
    "missing-scope:",
    "Invoke:Main",
    "invoke:main:extra",
    "ns/with/slash:scope",
])
async def test_get_rejects_malformed_slot_key(
    _drafts_client: AsyncClient, bad_key: str,
):
    """The same ``_validate_slot_key`` guard that gates PUT must
    also gate GET so a caller cannot probe arbitrary shapes."""
    res = await _drafts_client.get(f"/api/v1/user/drafts/{bad_key}")
    assert res.status_code in (400, 404), (
        f"expected 400/404 for {bad_key!r}, got {res.status_code}: {res.text}"
    )


async def test_get_accepts_future_chat_thread_slot_key(
    _drafts_client: AsyncClient,
):
    """``chat:<thread_id>`` — lock the future extension shape now
    so a later checkbox sweep doesn't silently regress the GET
    validator."""
    thread_slot = "chat:01jw3p7c8e2v8fk9wnh7m5q4tz"
    await _drafts_client.put(
        f"/api/v1/user/drafts/{thread_slot}",
        json={"content": "thread-scoped"},
    )
    res = await _drafts_client.get(f"/api/v1/user/drafts/{thread_slot}")
    assert res.status_code == 200
    assert res.json()["slot_key"] == thread_slot
    assert res.json()["content"] == "thread-scoped"


# ── opportunistic GC ─────────────────────────────────────────────


async def test_put_opportunistic_gc_drops_stale_rows(
    _drafts_client: AsyncClient, pg_test_pool,
):
    """The PUT handler runs ``prune_user_drafts`` after every upsert
    so the table bounds itself without a dedicated cron. Prove the
    sweep deletes a stale row when a fresh PUT lands."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_drafts (user_id, slot_key, content, "
            "updated_at, tenant_id) VALUES "
            "($1, $2, $3, $4, $5)",
            "stale-user", "chat:main", "old draft",
            time.time() - (25 * 3600),  # 25 h old → outside 24 h window
            "t-default",
        )

    res = await _drafts_client.put(
        "/api/v1/user/drafts/invoke:main",
        json={"content": "fresh draft"},
    )
    assert res.status_code == 200

    async with pg_test_pool.acquire() as conn:
        stale = await conn.fetchrow(
            "SELECT user_id FROM user_drafts WHERE user_id = 'stale-user'"
        )
        fresh = await conn.fetchrow(
            "SELECT content FROM user_drafts WHERE user_id = 'anonymous'"
        )
    assert stale is None, "GC must drop the >24h stale row"
    assert fresh is not None
    assert fresh["content"] == "fresh draft"


# ── helper-level contract (db.py direct call) ────────────────────


async def test_db_helpers_roundtrip(pg_test_pool):
    """Lock the ``backend.db`` helpers' contract independent of the
    HTTP router so future router refactors can move freely."""
    from backend import db as _db

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")

        ts = await _db.upsert_user_draft(
            conn, "u1", "invoke:main", "hello",
        )
        assert ts > 0

        got = await _db.get_user_draft(conn, "u1", "invoke:main")
        assert got is not None
        assert got["slot_key"] == "invoke:main"
        assert got["content"] == "hello"
        assert got["updated_at"] == pytest.approx(ts, rel=1e-6)

        miss = await _db.get_user_draft(conn, "u1", "chat:nope")
        assert miss is None


async def test_db_prune_drops_only_stale_rows(pg_test_pool):
    from backend import db as _db
    now = time.time()
    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")
        await conn.execute(
            "INSERT INTO user_drafts (user_id, slot_key, content, "
            "updated_at, tenant_id) VALUES "
            "($1, $2, $3, $4, $5), ($6, $7, $8, $9, $10)",
            "u-fresh", "invoke:main", "ok", now, "t-default",
            "u-stale", "chat:main", "old", now - (48 * 3600), "t-default",
        )
        deleted = await _db.prune_user_drafts(conn, now=now)
        assert deleted >= 1
        rows = await conn.fetch(
            "SELECT user_id FROM user_drafts ORDER BY user_id"
        )
    assert [r["user_id"] for r in rows] == ["u-fresh"]


# ── drift guard (SOP Step 4) ─────────────────────────────────────


@pytest.mark.usefixtures()
async def test_put_appears_in_migrator_tables_in_order():
    """Belt-and-braces: ``user_drafts`` MUST be in the migrator's
    ``TABLES_IN_ORDER`` so a PG cutover doesn't silently lose
    Q.6 data. The general drift guard
    (``test_migrator_schema_coverage.py``) covers this too, but a
    table-specific anchor here points future failures at this row's
    spec instead of the generic catch-all assertion.

    Marked async only because ``pytestmark = pytest.mark.asyncio`` at
    the top of this file applies to every test; the body is sync —
    no awaits, no I/O.
    """
    import importlib.util
    import sys
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "migrate_sqlite_to_pg",
        repo_root / "scripts" / "migrate_sqlite_to_pg.py",
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so dataclass + InitVar
    # introspection finds the module on lookup. Mirrors the pattern
    # in test_migrator_schema_coverage.py.
    sys.modules["migrate_sqlite_to_pg"] = mig
    spec.loader.exec_module(mig)
    assert "user_drafts" in mig.TABLES_IN_ORDER, (
        "alembic 0022 added the user_drafts table; the SQLite→PG "
        "migrator must include it in TABLES_IN_ORDER or the next "
        "cutover will silently drop Q.6 draft data."
    )
    # PK is composite TEXT — must NOT be in the IDENTITY subset
    # (which would crash sequence-reset at cutover time).
    assert "user_drafts" not in mig.TABLES_WITH_IDENTITY_ID


# ── Q.6 checkbox 3: dedicated 24h GC loop ────────────────────────


async def test_gc_sweep_drops_stale_rows(pg_test_pool, pg_test_dsn, monkeypatch):
    """``user_drafts_gc.sweep_once`` must remove rows older than the
    24 h retention window. The contract mirrors the opportunistic
    sweep in the PUT handler, but this codepath runs even when no
    HTTP traffic is hitting the worker."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    from backend import db as _db
    from backend import user_drafts_gc as gc

    if _db._db is not None:
        await _db.close()
    await _db.init()
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")
            await conn.execute(
                "INSERT INTO user_drafts (user_id, slot_key, content, "
                "updated_at, tenant_id) VALUES "
                "($1, $2, $3, $4, $5), ($6, $7, $8, $9, $10)",
                "u-fresh", "invoke:main", "new", time.time(), "t-default",
                "u-stale", "chat:main", "old",
                time.time() - (25 * 3600), "t-default",
            )

        deleted = await gc.sweep_once()
        assert deleted >= 1

        async with pg_test_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id FROM user_drafts ORDER BY user_id"
            )
        assert [r["user_id"] for r in rows] == ["u-fresh"]
    finally:
        await _db.close()


async def test_gc_sweep_idempotent_on_clean_table(pg_test_pool, pg_test_dsn, monkeypatch):
    """On a table whose rows are all inside the 24 h window the sweep
    must be a safe no-op (0 deleted, no exception). Guards against
    DELETE-without-predicate regressions."""
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    from backend import db as _db
    from backend import user_drafts_gc as gc

    if _db._db is not None:
        await _db.close()
    await _db.init()
    try:
        async with pg_test_pool.acquire() as conn:
            await conn.execute("TRUNCATE user_drafts RESTART IDENTITY CASCADE")
            await conn.execute(
                "INSERT INTO user_drafts (user_id, slot_key, content, "
                "updated_at, tenant_id) VALUES ($1, $2, $3, $4, $5)",
                "u-fresh", "invoke:main", "new", time.time(), "t-default",
            )
        deleted = await gc.sweep_once()
        assert deleted == 0
        async with pg_test_pool.acquire() as conn:
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM user_drafts WHERE user_id = 'u-fresh'"
            )
        assert remaining == 1
    finally:
        await _db.close()


async def test_gc_loop_exits_cleanly_on_cancel(monkeypatch):
    """The background loop must release the ``_LOOP_RUNNING`` flag on
    cancellation so a subsequent start (e.g. lifespan restart in a
    reload-under-test) succeeds. Mirrors the DLQ loop guarantee."""
    import asyncio as _aio

    from backend import user_drafts_gc as gc

    gc._reset_for_tests()

    # Drive the body fast so we don't wait for the real 1 h interval.
    async def _noop_sweep() -> int:
        return 0
    monkeypatch.setattr(gc, "sweep_once", _noop_sweep)

    task = _aio.create_task(gc.run_gc_loop(interval_s=0.01))
    await _aio.sleep(0.05)
    assert gc._LOOP_RUNNING is True

    task.cancel()
    try:
        await task
    except _aio.CancelledError:
        pass

    assert gc._LOOP_RUNNING is False
    assert task.done()


async def test_gc_loop_second_start_is_noop(monkeypatch):
    """Singleton guard: calling ``run_gc_loop`` while one is already
    running must return immediately without blocking. Prevents two
    sweeps from racing against the same pool in a single worker."""
    import asyncio as _aio

    from backend import user_drafts_gc as gc

    gc._reset_for_tests()

    async def _noop_sweep() -> int:
        return 0
    monkeypatch.setattr(gc, "sweep_once", _noop_sweep)

    t1 = _aio.create_task(gc.run_gc_loop(interval_s=0.01))
    await _aio.sleep(0.05)
    assert gc._LOOP_RUNNING is True

    result = await _aio.wait_for(gc.run_gc_loop(interval_s=0.01), timeout=0.5)
    assert result is None  # early return, did not block

    t1.cancel()
    try:
        await t1
    except _aio.CancelledError:
        pass
    gc._reset_for_tests()


async def test_gc_loop_survives_sweep_errors(monkeypatch):
    """If one sweep raises (transient PG hiccup, pool exhaustion),
    the loop must log + carry on to the next tick instead of dying.
    This is the "idle workers need this" correctness property — the
    loop has to keep running once started."""
    import asyncio as _aio

    from backend import user_drafts_gc as gc

    gc._reset_for_tests()

    call_count = {"n": 0}

    async def _flaky_sweep() -> int:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient boom")
        return 0
    monkeypatch.setattr(gc, "sweep_once", _flaky_sweep)

    task = _aio.create_task(gc.run_gc_loop(interval_s=0.01))
    # Wait long enough for ≥ 2 ticks (first raises, second returns 0).
    await _aio.sleep(0.15)

    assert call_count["n"] >= 2, (
        f"loop died after the first sweep raised; only {call_count['n']} "
        "call(s) observed — expected the loop to catch and continue."
    )

    task.cancel()
    try:
        await task
    except _aio.CancelledError:
        pass
    gc._reset_for_tests()

"""Q.3-SUB-6 (#297) — Chat history DB migration + cross-device SSE sync.

Before Q.3-SUB-6 ``POST /chat`` appended to a module-global
``_history: list[OrchestratorMessage]`` in ``backend/routers/chat.py``
— per-worker, cleared on restart, invisible across
``uvicorn --workers N``. The list was also unbounded (grew forever
within the worker) and the frontend's ``getChatHistory()`` consumer
was orphaned because the list it returned was per-worker and thus
inconsistent across requests.

This suite locks the migration + the cross-device contract:

  * ``test_chat_emit_on_post`` — POST /chat both persists to
    ``chat_messages`` and publishes a ``chat.message`` SSE event.
  * ``test_chat_emit_scope_is_user`` — payload contract lock:
    ``_broadcast_scope='user'`` so Q.4 (#298) can flip enforcement
    without a payload change.
  * ``test_chat_history_returns_persisted_rows`` — GET /chat/history
    reads from PG (not the retired module-global).
  * ``test_chat_cross_device_fanout`` — two parallel bus subscribers
    (simulating two devices on the same user); the HTTP POST from
    session A drives a ``chat.message`` onto session B's queue.
  * ``test_chat_persistence_independent_of_emit`` — if the SSE bus
    raises the chat message is still persisted (best-effort emit).
  * ``test_chat_clear_history_deletes_rows`` — DELETE /chat/history
    removes the current user's rows.
  * ``test_chat_messages_retention`` — ``prune_chat_messages`` drops
    messages older than the retention window (30 days).

Audit evidence: ``docs/design/multi-device-state-sync.md`` Path 5.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from httpx import AsyncClient, ASGITransport

from backend.events import bus as _bus

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def _chat_client(pg_test_pool, pg_test_dsn, monkeypatch):
    """Shared fixture — open auth mode with a seeded anonymous user.

    The chat router path resolves ``user.id`` from ``current_user``.
    Open-auth returns the synthetic anonymous-admin whose id is the
    literal string ``"anonymous"``; we seed no row in ``users``
    because chat_messages has no FK on users.
    """
    monkeypatch.setenv("OMNISIGHT_AUTH_MODE", "open")
    monkeypatch.setenv("OMNISIGHT_DATABASE_URL", pg_test_dsn)

    async with pg_test_pool.acquire() as conn:
        await conn.execute("TRUNCATE chat_messages RESTART IDENTITY CASCADE")

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
            await conn.execute("TRUNCATE chat_messages RESTART IDENTITY CASCADE")


async def _drain_for_chat_message(
    queue, role: str | None = None, timeout: float = 2.0,
):
    """Drain the SSE queue until a matching ``chat.message`` arrives.

    ``role`` filter lets the caller pick out the user turn vs the
    orchestrator reply when a single POST produces both.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        if msg.get("event") != "chat.message":
            continue
        data = json.loads(msg["data"])
        if role and data.get("role") != role:
            continue
        return data
    return None


async def test_chat_emit_on_post(_chat_client: AsyncClient, pg_test_pool):
    """POST /chat must both INSERT the row + publish a ``chat.message``
    event carrying id + role + user_id.

    The user's turn ("/status") is also persisted — Q.3-SUB-6 captures
    both sides of the conversation so cross-device history is complete.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await _chat_client.post(
            "/api/v1/chat", json={"message": "/status"},
        )
        assert res.status_code == 200, res.text

        user_evt = await _drain_for_chat_message(q, role="user")
        assert user_evt is not None, (
            "POST /chat must publish a chat.message for the user "
            "turn — cross-device history would otherwise miss it."
        )
        assert user_evt["content"] == "/status"
        assert user_evt["user_id"] == "anonymous"

        orch_evt = await _drain_for_chat_message(q, role="orchestrator")
        assert orch_evt is not None, (
            "POST /chat must publish a chat.message for the reply."
        )
        assert "user_id" in orch_evt
    finally:
        _bus.unsubscribe(q)

    # The PG row count must match the SSE event count — persistence and
    # emit are wired together, not one without the other.
    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role FROM chat_messages WHERE user_id = $1 "
            "ORDER BY timestamp",
            "anonymous",
        )
    roles = [r["role"] for r in rows]
    assert roles == ["user", "orchestrator"], (
        f"expected persisted [user, orchestrator], got {roles}"
    )


async def test_chat_emit_scope_is_user(_chat_client: AsyncClient):
    """Lock the ``broadcast_scope='user'`` payload contract. Frontend
    filter (and the eventual Q.4 #298 server enforcement) both rely
    on this label being present on every emit.
    """
    q = _bus.subscribe(tenant_id=None)
    try:
        res = await _chat_client.post(
            "/api/v1/chat", json={"message": "/help"},
        )
        assert res.status_code == 200, res.text

        data = await _drain_for_chat_message(q, role="user")
        assert data is not None
        assert data["_broadcast_scope"] == "user", (
            "chat.message must carry broadcast_scope=user so Q.4 "
            "(#298) can enforce per-user delivery without changing "
            "the payload shape."
        )
    finally:
        _bus.unsubscribe(q)


async def test_chat_history_returns_persisted_rows(_chat_client: AsyncClient):
    """GET /chat/history reads from PG, not the retired module-global.

    Three consecutive POSTs must land in GET /chat/history in
    chronological order so the chat UI can ``setMessages`` directly
    without reversing.
    """
    for message in ("/status", "/help", "/status"):
        res = await _chat_client.post("/api/v1/chat", json={"message": message})
        assert res.status_code == 200, res.text

    history_res = await _chat_client.get("/api/v1/chat/history")
    assert history_res.status_code == 200, history_res.text
    history = history_res.json()

    # Each /command POST yields a user turn + orchestrator turn.
    assert len(history) >= 6, (
        f"expected >=6 entries (3 user + 3 orchestrator), got {len(history)}"
    )
    # Chronological order: user → orchestrator → user → orchestrator …
    roles = [m["role"] for m in history[:6]]
    assert roles == ["user", "orchestrator", "user", "orchestrator",
                     "user", "orchestrator"], (
        f"expected alternating user/orchestrator order, got {roles}"
    )


async def test_chat_cross_device_fanout(_chat_client: AsyncClient):
    """Two bus subscribers (simulating two devices on the same user);
    the HTTP POST from session A must land a ``chat.message`` onto
    session B's queue. This is the contract use-engine.ts consumes
    to append new lines without a history refetch.
    """
    q_a = _bus.subscribe(tenant_id=None)
    q_b = _bus.subscribe(tenant_id=None)
    try:
        res = await _chat_client.post(
            "/api/v1/chat", json={"message": "/status"},
        )
        assert res.status_code == 200, res.text

        data_b = await _drain_for_chat_message(q_b, role="user")
        assert data_b is not None, (
            "session B must receive the chat.message event"
        )
        assert data_b["content"] == "/status"

        data_a = await _drain_for_chat_message(q_a, role="user")
        assert data_a is not None, (
            "originator must also see its own event — the dispatcher "
            "dedupes by id so double-apply is safe"
        )
    finally:
        _bus.unsubscribe(q_a)
        _bus.unsubscribe(q_b)


async def test_chat_persistence_independent_of_emit(
    _chat_client: AsyncClient, pg_test_pool, monkeypatch,
):
    """A flaky SSE bus / Redis outage must NEVER fail the chat mutation.

    The PG row is the source of truth; the emit is latency-optimisation
    for cross-device fan-out. We monkey-patch ``emit_chat_message`` to
    boom on every call and verify POST /chat still returns 200 with
    the row landed.
    """
    from backend import events as _events

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SSE bus outage")

    monkeypatch.setattr(_events, "emit_chat_message", _boom)

    res = await _chat_client.post(
        "/api/v1/chat", json={"message": "/status"},
    )
    assert res.status_code == 200, res.text

    async with pg_test_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id = $1",
            "anonymous",
        )
    assert int(n) >= 2, (
        f"POST /chat must persist even if SSE emit fails; got {n} rows"
    )


async def test_chat_clear_history_deletes_rows(
    _chat_client: AsyncClient, pg_test_pool,
):
    """DELETE /chat/history must wipe the current user's rows but
    leave other users untouched.
    """
    # Seed a direct row for a different user to prove scope.
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_messages (id, user_id, session_id, role, "
            "content, timestamp, tenant_id) VALUES "
            "($1, $2, $3, $4, $5, $6, $7)",
            "other-msg-1", "other-user", "", "user", "other content",
            time.time(), "t-default",
        )

    await _chat_client.post("/api/v1/chat", json={"message": "/status"})

    del_res = await _chat_client.delete("/api/v1/chat/history")
    assert del_res.status_code == 204, del_res.text

    async with pg_test_pool.acquire() as conn:
        anon = await conn.fetchval(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id = $1",
            "anonymous",
        )
        other = await conn.fetchval(
            "SELECT COUNT(*) FROM chat_messages WHERE user_id = $1",
            "other-user",
        )
    assert int(anon) == 0
    assert int(other) == 1, (
        "DELETE /chat/history must be scoped to the current user — "
        "other-user's row must survive"
    )


async def test_chat_messages_retention(pg_test_pool):
    """``prune_chat_messages`` drops messages older than ``days``
    (30 by default) while preserving fresh rows.

    The hot-path (POST /chat) calls this opportunistically after
    every INSERT so the table bounds itself without a cron job.
    """
    from backend import db as _db

    now = time.time()
    old = now - (40 * 86400)   # 40 days ago — past the 30-day window
    fresh = now - (5 * 86400)  # 5 days ago — should survive

    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE chat_messages RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "INSERT INTO chat_messages (id, user_id, session_id, role, "
            "content, timestamp, tenant_id) VALUES "
            "($1, $2, $3, $4, $5, $6, $7)",
            "old-1", "ret-user", "", "user", "ancient history",
            old, "t-default",
        )
        await conn.execute(
            "INSERT INTO chat_messages (id, user_id, session_id, role, "
            "content, timestamp, tenant_id) VALUES "
            "($1, $2, $3, $4, $5, $6, $7)",
            "fresh-1", "ret-user", "", "user", "recent history",
            fresh, "t-default",
        )

        removed = await _db.prune_chat_messages(conn, "ret-user")
        assert removed == 1, f"expected to prune 1 ancient row, got {removed}"

        remaining = await conn.fetch(
            "SELECT id FROM chat_messages WHERE user_id = $1 "
            "ORDER BY id",
            "ret-user",
        )
        ids = [r["id"] for r in remaining]
        assert ids == ["fresh-1"], (
            f"retention sweep must preserve fresh rows; got {ids}"
        )

        # Idempotent: running again removes nothing.
        removed2 = await _db.prune_chat_messages(conn, "ret-user")
        assert removed2 == 0

        await conn.execute(
            "TRUNCATE chat_messages RESTART IDENTITY CASCADE"
        )


async def test_chat_messages_in_migrator_tables_in_order():
    """SOP Step 4 drift-guard: the migrator's ``TABLES_IN_ORDER`` must
    include ``chat_messages`` so the SQLite→PG cutover doesn't silently
    lose the history table.

    The live-schema drift guard (``test_migrator_schema_coverage.py``)
    catches this too — the present assertion is a belt-and-braces
    cheap check that runs even when that heavier test is excluded
    from a fast slice.
    """
    import importlib.util
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    migrator_path = repo_root / "scripts" / "migrate_sqlite_to_pg.py"
    spec = importlib.util.spec_from_file_location(
        "_migrator_for_chat_test", migrator_path,
    )
    assert spec and spec.loader
    mig = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so decorator-using
    # classes inside the script (e.g. ``@dataclass(frozen=True)``) can
    # resolve their own module when dataclasses introspects via
    # ``sys.modules.get(cls.__module__)``.
    sys.modules[spec.name] = mig
    try:
        spec.loader.exec_module(mig)
    finally:
        sys.modules.pop(spec.name, None)

    assert "chat_messages" in mig.TABLES_IN_ORDER, (
        "scripts/migrate_sqlite_to_pg.py::TABLES_IN_ORDER must include "
        "chat_messages — the cutover would otherwise drop the per-user "
        "chat history."
    )
    # TEXT primary key (uuid) — must NOT appear in the identity list or
    # the sequence-reset logic would crash.
    assert "chat_messages" not in mig.TABLES_WITH_IDENTITY_ID, (
        "chat_messages.id is TEXT; listing it as IDENTITY would crash "
        "the sequence-reset logic on PG."
    )

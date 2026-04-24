"""ZZ.B2 #304-2 checkbox 1 — session auto-title tests.

Locks the contract between the 3-user-turn trigger, the
``chat_sessions`` row lifecycle, the ``session.titled`` SSE event, and
the at-most-once ``set_session_auto_title`` UPDATE.

Covers:

1. ``emit_session_titled`` payload shape + scope contract.
2. ``SSE_EVENT_SCHEMAS['session.titled']`` drift guard.
3. ``upsert_chat_session`` is idempotent + refreshes ``updated_at``.
4. ``count_user_turns_in_session`` is tenant-scoped and role-filtered.
5. ``set_session_auto_title`` only applies once even across concurrent
   callers (PG conditional UPDATE wins).
6. ``list_chat_sessions_for_user`` orders by ``updated_at DESC`` +
   decodes ``metadata`` from JSONB.
7. ``_maybe_schedule_auto_title`` fires at exactly 3 user turns,
   doesn't re-fire if ``auto_title`` is already set, and is
   per-worker deduped via ``_auto_title_inflight``.
8. ``_compose_title_via_llm`` sanitises model output (strips quotes,
   "Title:" prefix, trims to first line + 80 chars).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import db, events
from backend.db_context import set_tenant_id


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


# ─── emit_session_titled payload shape ───────────────────────────────

@pytest.mark.asyncio
async def test_emit_session_titled_publishes_canonical_payload():
    q = events.bus.subscribe()
    try:
        events.emit_session_titled(
            session_id="sess-abc123",
            user_id="u-42",
            title="Wire up dashboard deep link",
            source="auto",
            broadcast_scope="user",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "session.titled"
        payload = json.loads(msg["data"])
        assert payload["session_id"] == "sess-abc123"
        assert payload["user_id"] == "u-42"
        assert payload["title"] == "Wire up dashboard deep link"
        assert payload["source"] == "auto"
        assert "timestamp" in payload
    finally:
        events.bus.unsubscribe(q)


def test_session_titled_schema_registered_for_frontend_export():
    """Drift guard — if this fails, /system/sse-schema export lost the
    type and the frontend SSE codegen would silently drop it."""
    from backend.sse_schemas import SSE_EVENT_SCHEMAS, SSESessionTitled
    assert "session.titled" in SSE_EVENT_SCHEMAS
    assert SSE_EVENT_SCHEMAS["session.titled"] is SSESessionTitled


# ─── chat_sessions db helpers ────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_chat_session_idempotent_and_refreshes_updated_at(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    rows = await db.list_chat_sessions_for_user(pg_test_conn, "u-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-1"
    assert rows[0]["created_at"] == 100.0
    assert rows[0]["updated_at"] == 100.0

    # Second call with a later ``now`` touches updated_at only.
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=250.0,
    )
    rows = await db.list_chat_sessions_for_user(pg_test_conn, "u-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["created_at"] == 100.0  # unchanged
    assert rows[0]["updated_at"] == 250.0  # bumped


@pytest.mark.asyncio
async def test_count_user_turns_respects_role_and_tenant(pg_test_conn):
    set_tenant_id("t-alpha")
    # Three user turns + two orchestrator replies for the same session.
    for i in range(3):
        await db.insert_chat_message(pg_test_conn, {
            "id": f"u-msg-{i}", "user_id": "u-1",
            "session_id": "sess-1", "role": "user",
            "content": f"user {i}", "timestamp": 100.0 + i,
        })
    for i in range(2):
        await db.insert_chat_message(pg_test_conn, {
            "id": f"o-msg-{i}", "user_id": "u-1",
            "session_id": "sess-1", "role": "orchestrator",
            "content": f"bot {i}", "timestamp": 200.0 + i,
        })

    count = await db.count_user_turns_in_session(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert count == 3  # only role='user' counted

    # Other tenant sees zero.
    set_tenant_id("t-beta")
    count_b = await db.count_user_turns_in_session(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert count_b == 0


@pytest.mark.asyncio
async def test_set_session_auto_title_is_at_most_once(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    first = await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="First title",
    )
    assert first is True
    second = await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Would-be override",
    )
    # Must NOT overwrite — the field already exists.
    assert second is False
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {"auto_title": "First title"}


@pytest.mark.asyncio
async def test_set_session_auto_title_rejects_empty_title(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    assert await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1", title="",
    ) is False
    assert await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1", title="   ",
    ) is False
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {}


@pytest.mark.asyncio
async def test_list_chat_sessions_orders_by_updated_at_desc(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-old", user_id="u-1", now=100.0,
    )
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-new", user_id="u-1", now=500.0,
    )
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-mid", user_id="u-1", now=300.0,
    )
    rows = await db.list_chat_sessions_for_user(pg_test_conn, "u-1", limit=10)
    assert [r["session_id"] for r in rows] == ["sess-new", "sess-mid", "sess-old"]
    assert all(isinstance(r["metadata"], dict) for r in rows)


# ─── 3-turn trigger + background task ────────────────────────────────

@pytest.mark.asyncio
async def test_maybe_schedule_auto_title_fires_at_exactly_three_turns(
    pg_test_conn, monkeypatch,
):
    """At turn 1 / 2 the trigger must stay quiet. At turn 3 it schedules
    the task. Turn 4+ must not re-fire once auto_title is set.
    """
    from backend.routers import chat as chat_router

    fired: list[tuple[str, str, str]] = []

    async def _fake_generate(*, session_id, user_id, tenant_id):
        fired.append((session_id, user_id, tenant_id))

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)
    chat_router._auto_title_inflight.clear()

    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )

    # Turn 1 — no fire
    await db.insert_chat_message(pg_test_conn, {
        "id": "u-msg-1", "user_id": "u-1",
        "session_id": "sess-1", "role": "user",
        "content": "first", "timestamp": 101.0,
    })
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)  # flush any queued task
    assert fired == []

    # Turn 2 — no fire
    await db.insert_chat_message(pg_test_conn, {
        "id": "u-msg-2", "user_id": "u-1",
        "session_id": "sess-1", "role": "user",
        "content": "second", "timestamp": 102.0,
    })
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)
    assert fired == []

    # Turn 3 — fires exactly once
    await db.insert_chat_message(pg_test_conn, {
        "id": "u-msg-3", "user_id": "u-1",
        "session_id": "sess-1", "role": "user",
        "content": "third", "timestamp": 103.0,
    })
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0] == ("sess-1", "u-1", "t-alpha")

    # In-flight guard prevents a re-fire within the same worker.
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)
    assert len(fired) == 1  # still one

    # Simulate the background task finishing + writing auto_title.
    chat_router._auto_title_inflight.discard(("u-1", "sess-1"))
    await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Already titled",
    )

    # Turn 4 arrives — trigger must NOT re-fire because auto_title exists.
    await db.insert_chat_message(pg_test_conn, {
        "id": "u-msg-4", "user_id": "u-1",
        "session_id": "sess-1", "role": "user",
        "content": "fourth", "timestamp": 104.0,
    })
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_maybe_schedule_auto_title_dedupes_within_worker(
    pg_test_conn, monkeypatch,
):
    """Two overlapping 3-turn triggers in the same worker process must
    only schedule the background task once. (PG's conditional UPDATE
    enforces the cross-worker invariant; this locks the per-worker
    latency optimisation.)"""
    from backend.routers import chat as chat_router

    fired: list[str] = []

    async def _fake_generate(*, session_id, user_id, tenant_id):
        fired.append(session_id)

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)
    chat_router._auto_title_inflight.clear()

    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    for i in range(3):
        await db.insert_chat_message(pg_test_conn, {
            "id": f"u-msg-{i}", "user_id": "u-1",
            "session_id": "sess-1", "role": "user",
            "content": f"turn {i}", "timestamp": 100.0 + i,
        })

    # Fire two trigger checks back-to-back without awaiting the fake
    # task's completion (simulates the dedupe guard's purpose).
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await chat_router._maybe_schedule_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(fired) == 1


# ─── LLM output sanitisation ─────────────────────────────────────────

def test_sanitize_title_strips_quotes_and_prefix():
    from backend.routers.chat import _sanitize_title
    assert _sanitize_title('"Wire up deep link"') == "Wire up deep link"
    assert _sanitize_title("Title: Wire up deep link") == "Wire up deep link"
    assert _sanitize_title("chat: Build the chat") == "Build the chat"
    assert _sanitize_title("line one\nline two") == "line one"
    # 80-char cap + trailing punctuation trimmed.
    long = "x" * 100 + "."
    assert _sanitize_title(long) == "x" * 80
    assert _sanitize_title("   ") == ""


def test_condense_turn_keeps_first_line_and_bounds_length():
    from backend.routers.chat import _condense_turn, _AUTO_TITLE_CONDENSE_CHARS
    assert _condense_turn("one\ntwo") == "one"
    # Long first line is capped.
    blob = "x" * (_AUTO_TITLE_CONDENSE_CHARS + 50)
    assert _condense_turn(blob) == "x" * _AUTO_TITLE_CONDENSE_CHARS
    assert _condense_turn("") == ""
    assert _condense_turn("   \n   ") == ""

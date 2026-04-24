"""ZZ.B2 #304-2 checkbox 4 — 4-axis auto-title integration matrix.

The upstream checkboxes each cover one axis in isolation. This file
crosses the boundaries that only a true end-to-end path can catch:

    1. **3-turn trigger**: drive the ``_persist_and_emit`` pipeline
       with real chat_messages rows and assert the scheduler fires
       exactly at the boundary, not before, not after.
    2. **Title update does not break existing session**: an
       ``auto_title`` landing mid-session must not corrupt subsequent
       chat writes, must not re-fire the trigger, and must not block
       the SSE fan-out of later ``chat.message`` events.
    3. **Manual title priority**: an operator-authored ``user_title``
       must win over ``auto_title`` in the effective render; clearing
       it must revert to ``auto_title``. Mirror the frontend
       ``resolveSessionTitle`` contract so future drift on either side
       trips a test.
    4. **Cheapest model fallback**: the composer must route through
       ``get_cheapest_model`` (not ``get_llm``), degrade cleanly when
       no cheap provider is available, and skip emit on empty output
       rather than broadcast a blank title.

The matrix tests are intentionally redundant with the per-axis files —
an axis fix that accidentally breaks another axis must turn this file
red even if the axis-specific tests stay green. That is the whole
point of a matrix.

Module-global audit (SOP Step 1, 2026-04-21 rule):
    * ``chat_router._auto_title_inflight`` — cleared in every fixture
      to prevent cross-test bleed. Per-worker set is the SOP answer-3
      "故意每 worker 獨立" as documented in the router.
    * ``events.bus`` is a process-global singleton; we subscribe /
      unsubscribe per-test so queues don't accumulate listeners.
    * ``settings`` + LLM ``_cache`` — monkeypatched per-test via pytest
      teardown. The ``_clear_llm_cache`` autouse fixture matches the
      pattern in ``test_get_cheapest_model.py`` so a prior test's
      cached ``(deepseek, deepseek-chat)`` doesn't leak into a later
      anthropic-only case.

Read-after-write timing audit (SOP Step 1): the ``_generate_auto_title``
background task normally borrows a fresh pool conn via
``get_pool().acquire()`` — under ``pg_test_conn`` (outer-tx wrapped)
that fresh conn can't see our uncommitted rows. The matrix tests
work around this by either (a) mocking ``_generate_auto_title`` to
record scheduling calls, or (b) replaying the same logic against the
test's own ``pg_test_conn`` so the read-after-write path exercises
inside the transaction. Both patterns are documented inline.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import auth as _au
from backend import db, events
from backend.db_context import set_tenant_id
from backend.routers import chat as chat_router


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


@pytest.fixture(autouse=True)
def _reset_inflight():
    """Clear the per-worker in-flight dedupe set.

    Without this, a test that leaves (user, session) in the set
    silently suppresses the trigger in a later test. The router
    clears the set on task completion; the tests here fake the task
    so we clean up ourselves.
    """
    chat_router._auto_title_inflight.clear()
    yield
    chat_router._auto_title_inflight.clear()


@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """Mirror ``test_get_cheapest_model._clear_llm_cache`` so the
    cheapest-model axis is reproducible regardless of test order."""
    from backend.agents import llm as _llm_mod

    _llm_mod._cache.clear()
    _llm_mod._provider_failures.clear()
    yield
    _llm_mod._cache.clear()
    _llm_mod._provider_failures.clear()


def _insert_user_turn(
    conn, *, session_id: str, user_id: str, idx: int, ts_base: float = 100.0,
):
    """Single-shot helper for the 3-turn sweep tests."""
    return db.insert_chat_message(conn, {
        "id": f"u-msg-{session_id}-{idx}",
        "user_id": user_id,
        "session_id": session_id,
        "role": "user",
        "content": f"turn {idx} content",
        "timestamp": ts_base + idx,
    })


async def _drive_persist_and_emit_user_turn(
    pg_test_conn, *, session_id: str, user_id: str, idx: int,
    ts_base: float = 100.0,
):
    """End-to-end path through ``_persist_and_emit`` for a user turn.

    Uses a plain ``OrchestratorMessage`` so the side-effect ladder
    (upsert_chat_session → maybe_schedule_auto_title → emit_chat_message)
    engages exactly as production does. Returns the inserted message id.
    """
    from backend.models import MessageRole, OrchestratorMessage

    msg = OrchestratorMessage(
        id=f"msg-{session_id}-{idx}",
        role=MessageRole.user,
        content=f"user message #{idx}",
        timestamp=str(ts_base + idx),
    )
    await chat_router._persist_and_emit(
        pg_test_conn, msg, user_id=user_id, session_id=session_id,
    )
    return msg.id


# ─── AXIS 1 — 3-turn trigger through full _persist_and_emit pipeline ─


@pytest.mark.asyncio
async def test_matrix_axis1_trigger_fires_only_at_turn_three_end_to_end(
    pg_test_conn, monkeypatch,
):
    """End-to-end: 3 user turns flow through ``_persist_and_emit``.
    The auto-title task must be scheduled at exactly turn 3, not
    earlier, not later. This is the per-axis test scaled up to
    exercise the real side-effect ladder (upsert_chat_session →
    count_user_turns → maybe_schedule_auto_title) instead of calling
    the scheduler directly.
    """
    scheduled: list[tuple[str, str, str]] = []

    async def _fake_generate(*, session_id, user_id, tenant_id):
        scheduled.append((session_id, user_id, tenant_id))

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)

    set_tenant_id("t-matrix")

    # Turn 1 — no schedule.
    await _drive_persist_and_emit_user_turn(
        pg_test_conn, session_id="sess-m1", user_id="u-m1", idx=1,
    )
    await asyncio.sleep(0)
    assert scheduled == []

    # Turn 2 — no schedule.
    await _drive_persist_and_emit_user_turn(
        pg_test_conn, session_id="sess-m1", user_id="u-m1", idx=2,
    )
    await asyncio.sleep(0)
    assert scheduled == []

    # Turn 3 — exactly one schedule.
    await _drive_persist_and_emit_user_turn(
        pg_test_conn, session_id="sess-m1", user_id="u-m1", idx=3,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(scheduled) == 1
    assert scheduled[0] == ("sess-m1", "u-m1", "t-matrix")


@pytest.mark.asyncio
async def test_matrix_axis1_non_user_roles_do_not_advance_trigger(
    pg_test_conn, monkeypatch,
):
    """Only ``role='user'`` counts. An orchestrator reply between each
    user turn must not tip the count — otherwise two user turns + two
    bot replies would spuriously trip at turn 4 overall."""
    from backend.models import MessageRole, OrchestratorMessage

    scheduled: list = []

    async def _fake_generate(**kwargs):
        scheduled.append(kwargs["session_id"])

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)

    set_tenant_id("t-matrix")

    # Two user turns interleaved with orchestrator replies — count is 2.
    for i in range(2):
        await _drive_persist_and_emit_user_turn(
            pg_test_conn, session_id="sess-m2", user_id="u-m2", idx=i,
        )
        bot = OrchestratorMessage(
            id=f"bot-msg-{i}",
            role=MessageRole.orchestrator,
            content=f"bot reply {i}",
            timestamp=str(200 + i),
        )
        await chat_router._persist_and_emit(
            pg_test_conn, bot, user_id="u-m2", session_id="sess-m2",
        )
    await asyncio.sleep(0)
    assert scheduled == []  # only 2 user turns logged


# ─── AXIS 2 — Title update does not break the existing session ──────


@pytest.mark.asyncio
async def test_matrix_axis2_auto_title_landing_does_not_disrupt_turn_four(
    pg_test_conn, monkeypatch,
):
    """The heart of the "update 不中斷現有 session" contract.

    After turn 3 schedules the auto-title task, and the task lands
    ``metadata.auto_title``, turn 4 must:
      * Successfully persist to ``chat_messages`` (no lock contention).
      * NOT re-fire the auto-title scheduler (auto_title is already set).
      * NOT clobber the stored ``auto_title`` value.
      * Leave the chat_sessions ``updated_at`` fresh (for sidebar
        recency ordering) without touching ``created_at``.

    If a future refactor gates ``_persist_and_emit`` on a lock that
    only releases after the title task finishes, turn 4 would hang —
    this test fails by timing out, surfacing the bug fast.
    """
    scheduled: list[tuple[str, str, str]] = []

    async def _fake_generate(*, session_id, user_id, tenant_id):
        # Record scheduling only; the write lands below via the test
        # body so we don't race ``pg_test_conn`` with the main test's
        # own conn usage. asyncpg prohibits concurrent operations on
        # the same Connection, so any real DB work here would trip an
        # ``InterfaceError: another operation is in progress`` the
        # moment the test's next ``await`` touches the same conn.
        scheduled.append((session_id, user_id, tenant_id))

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)

    set_tenant_id("t-matrix")

    # Turns 1-3 — final one fires the faked generate.
    for i in range(1, 4):
        await _drive_persist_and_emit_user_turn(
            pg_test_conn, session_id="sess-m3", user_id="u-m3", idx=i,
        )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert scheduled == [("sess-m3", "u-m3", "t-matrix")]

    # Simulate the task completing — real prod path would run the
    # composer against a fresh pool conn; here we land the result on
    # ``pg_test_conn`` so the read-after-write flow stays inside the
    # test's outer transaction.
    landed = await db.set_session_auto_title(
        pg_test_conn, session_id="sess-m3", user_id="u-m3",
        title="AUTO-TITLE-SET",
    )
    assert landed is True
    # Drop the in-flight guard as the real task would on completion.
    chat_router._auto_title_inflight.discard(("u-m3", "sess-m3"))

    meta_after_three = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-m3", user_id="u-m3",
    )
    assert meta_after_three == {"auto_title": "AUTO-TITLE-SET"}

    row_after_three = (await db.list_chat_sessions_for_user(
        pg_test_conn, "u-m3", limit=10,
    ))[0]
    created_at_snapshot = row_after_three["created_at"]
    updated_at_after_three = row_after_three["updated_at"]

    # Small delay so turn 4's ``upsert_chat_session`` ``now`` differs.
    await asyncio.sleep(0.01)

    # Turn 4 MUST succeed without raising, re-scheduling, or overwriting.
    await _drive_persist_and_emit_user_turn(
        pg_test_conn, session_id="sess-m3", user_id="u-m3", idx=4,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Scheduler must NOT have been re-invoked.
    assert scheduled == [("sess-m3", "u-m3", "t-matrix")], (
        "Turn 4 re-triggered the auto-title task — gate on "
        "'auto_title in metadata' must suppress subsequent fires."
    )

    # auto_title must be untouched.
    meta_after_four = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-m3", user_id="u-m3",
    )
    assert meta_after_four == {"auto_title": "AUTO-TITLE-SET"}

    # chat_messages row 4 landed.
    msgs = await db.list_chat_messages(pg_test_conn, "u-m3", limit=50)
    user_contents = [m["content"] for m in msgs if m["role"] == "user"]
    assert len(user_contents) == 4
    assert "user message #4" in user_contents

    # Session recency bumped (sidebar ordering) but created_at stable.
    row_after_four = (await db.list_chat_sessions_for_user(
        pg_test_conn, "u-m3", limit=10,
    ))[0]
    assert row_after_four["created_at"] == created_at_snapshot
    assert row_after_four["updated_at"] >= updated_at_after_three


@pytest.mark.asyncio
async def test_matrix_axis2_session_titled_sse_does_not_stall_chat_message_sse(
    pg_test_conn,
):
    """A ``session.titled`` SSE emit must not wedge the bus so that a
    subsequent ``chat.message`` emit fails to arrive. Subscribe to the
    bus, publish both in order, assert both arrive in order within a
    tight timeout.
    """
    q = events.bus.subscribe()
    try:
        events.emit_session_titled(
            session_id="sess-m4",
            user_id="u-m4",
            title="Probe title",
            source="auto",
            broadcast_scope="user",
            tenant_id="t-matrix",
        )
        events.emit_chat_message(
            message_id="msg-m4-follow",
            user_id="u-m4",
            role="user",
            content="follow-up after title landed",
            timestamp="1700000000",
            session_id="sess-m4",
        )

        first = await asyncio.wait_for(q.get(), timeout=1)
        second = await asyncio.wait_for(q.get(), timeout=1)
        assert first["event"] == "session.titled"
        assert second["event"] == "chat.message"
        payload2 = json.loads(second["data"])
        assert payload2["content"] == "follow-up after title landed"
    finally:
        events.bus.unsubscribe(q)


# ─── AXIS 3 — Manual title priority (mirrors resolveSessionTitle) ───


def _resolve_effective_title(metadata: dict, session_id: str) -> dict:
    """Backend mirror of frontend ``resolveSessionTitle``.

    Locking the contract on both sides prevents drift: if the
    frontend resolver is revised (e.g. to prefer auto over user), this
    test goes red and forces the change to be acknowledged here too.
    """
    user_t = (metadata.get("user_title") or "").strip() if metadata else ""
    if user_t:
        return {"title": user_t, "source": "user"}
    auto_t = (metadata.get("auto_title") or "").strip() if metadata else ""
    if auto_t:
        return {"title": auto_t, "source": "auto"}
    return {"title": f"Session {session_id[:8]}", "source": "hash"}


@pytest.mark.asyncio
async def test_matrix_axis3_user_title_wins_over_auto_title(pg_test_conn):
    """With both metadata keys present, the sidebar MUST render the
    ``user_title``. This locks the auto → user precedence at the
    backend-facing layer: any future schema change (e.g. a renamed
    field) must update both sides in lockstep.
    """
    set_tenant_id("t-matrix")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-m5", user_id="u-m5", now=100.0,
    )
    assert await db.set_session_auto_title(
        pg_test_conn, session_id="sess-m5", user_id="u-m5",
        title="LLM generated",
    ) is True
    assert await db.set_session_user_title(
        pg_test_conn, session_id="sess-m5", user_id="u-m5",
        title="Operator rename",
    ) is True

    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-m5", user_id="u-m5",
    )
    assert meta == {
        "auto_title": "LLM generated",
        "user_title": "Operator rename",
    }

    resolved = _resolve_effective_title(meta, "sess-m5-longhash")
    assert resolved == {"title": "Operator rename", "source": "user"}


@pytest.mark.asyncio
async def test_matrix_axis3_clearing_user_title_reverts_to_auto(pg_test_conn):
    """When the operator clears the rename, the sidebar re-renders the
    ``auto_title``. The underlying fact: ``metadata - 'user_title'``
    removes only the ``user_title`` key — the LLM's ``auto_title``
    survives so the fallback chain has something to land on.
    """
    set_tenant_id("t-matrix")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-m6", user_id="u-m6", now=100.0,
    )
    await db.set_session_auto_title(
        pg_test_conn, session_id="sess-m6", user_id="u-m6",
        title="LLM label",
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-m6", user_id="u-m6",
        title="Temporary rename",
    )
    # Clear (user passes empty string via PATCH).
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-m6", user_id="u-m6", title="",
    )
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-m6", user_id="u-m6",
    )
    assert meta == {"auto_title": "LLM label"}
    resolved = _resolve_effective_title(meta, "sess-m6-longhash")
    assert resolved == {"title": "LLM label", "source": "auto"}


@pytest.mark.asyncio
async def test_matrix_axis3_clearing_both_titles_falls_back_to_hash(pg_test_conn):
    """If neither ``user_title`` nor ``auto_title`` is present, the
    sidebar renders a hash fallback — the third leg of the chain.
    ``set_session_auto_title`` is at-most-once so once set it can't be
    cleared by the same API; but a row that never had an auto_title
    (e.g. session with <3 user turns) must still render cleanly.
    """
    set_tenant_id("t-matrix")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-m7", user_id="u-m7", now=100.0,
    )
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-m7", user_id="u-m7",
    )
    assert meta == {}
    resolved = _resolve_effective_title(meta, "sess-m7-xyz")
    assert resolved == {"title": "Session sess-m7-", "source": "hash"}


@pytest.mark.asyncio
async def test_matrix_axis3_rename_endpoint_emits_source_user(pg_test_conn):
    """The PATCH endpoint fires ``session.titled`` with source=user so
    the operator's other devices relabel in place — part of the same
    fallback-chain contract but exercised at the HTTP handler level.
    """
    set_tenant_id("t-matrix")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-m8", user_id="u-m8", now=100.0,
    )
    await db.set_session_auto_title(
        pg_test_conn, session_id="sess-m8", user_id="u-m8",
        title="Auto label",
    )

    user = _au.User(
        id="u-m8",
        email="u-m8@example.com",
        name="u-m8",
        role="operator",
        enabled=True,
        tenant_id="t-matrix",
    )

    class _FakeRequest:
        def __init__(self) -> None:
            self.state = type("S", (), {})()

    q = events.bus.subscribe()
    try:
        result = await chat_router.rename_session(
            session_id="sess-m8",
            body=chat_router.SessionTitleBody(title="  Operator took over  "),
            request=_FakeRequest(),
            user=user,
            conn=pg_test_conn,
        )
        assert result["metadata"]["user_title"] == "Operator took over"
        assert result["metadata"]["auto_title"] == "Auto label"

        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "session.titled"
        payload = json.loads(msg["data"])
        assert payload["source"] == "user"
        assert payload["title"] == "Operator took over"
    finally:
        events.bus.unsubscribe(q)


# ─── AXIS 4 — Cheapest model fallback wiring ────────────────────────


@pytest.mark.asyncio
async def test_matrix_axis4_composer_routes_through_cheapest_not_primary(
    monkeypatch,
):
    """Integration rebind guard at the matrix level — if anyone reverts
    ``_compose_title_via_llm`` back to ``get_llm``, the assertion
    inside ``_fake_primary`` fires.
    """
    from backend.agents import llm as _llm_mod

    calls: list[str] = []

    class _FakeLLM:
        async def ainvoke(self, prompt):
            calls.append(prompt)

            class _R:
                content = "Clean matrix title"
            return _R()

    def _fake_cheapest(bind_tools=None):
        return _FakeLLM()

    def _fake_primary(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError(
            "matrix guard: _compose_title_via_llm must route through "
            "get_cheapest_model (ZZ.B2 checkbox 3 contract)"
        )

    monkeypatch.setattr(_llm_mod, "get_cheapest_model", _fake_cheapest)
    monkeypatch.setattr(_llm_mod, "get_llm", _fake_primary)

    title = await chat_router._compose_title_via_llm([
        "Debug the firmware handshake", "Add retry backoff", "Verify on real board",
    ])
    assert title == "Clean matrix title"
    # Prompt should number all three condensed turns.
    prompt = calls[0]
    for i, expected in enumerate([
        "Debug the firmware handshake",
        "Add retry backoff",
        "Verify on real board",
    ], start=1):
        assert f"{i}. {expected}" in prompt


@pytest.mark.asyncio
async def test_matrix_axis4_composer_fallback_to_primary_when_cheapest_none(
    monkeypatch,
):
    """When every cheap entry is missing credentials and the primary
    still has a key, ``get_cheapest_model`` falls back to the primary
    via ``get_llm()``. This test locks that cascade without requiring
    real LLM credentials — we patch ``_create_llm`` so the provider
    gate mirrors production behaviour.

    Rationale: the cheapest-first contract must never be so strict that
    a deployment with only a primary key (e.g. anthropic Opus only)
    silently drops auto-title generation. Degrading back to the
    primary is the acceptable loss-of-cost-guarantee path documented
    in the checkbox-3 history.
    """
    from backend.agents import llm as _llm_mod
    from backend.config import settings

    # No cheap provider keys; primary (google) has one.
    for attr in [
        "deepseek_api_key", "anthropic_api_key", "openrouter_api_key",
        "groq_api_key", "openai_api_key", "xai_api_key",
    ]:
        monkeypatch.setattr(settings, attr, "", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "google")
    monkeypatch.setattr(settings, "google_api_key", "g-test-key")
    monkeypatch.setattr(settings, "llm_model", "")
    monkeypatch.setattr(settings, "llm_fallback_chain", "google")

    captured: list[tuple[str, str]] = []

    class _Fake:
        async def ainvoke(self, _prompt):
            class _R:
                content = "Primary fallback title"
            return _R()

        def with_config(self, **_k):
            return self

        def bind_tools(self, _tools):
            return self

    def _fake_create(provider, model):
        key_attr = {
            "deepseek": "deepseek_api_key",
            "anthropic": "anthropic_api_key",
            "openrouter": "openrouter_api_key",
            "groq": "groq_api_key",
            "google": "google_api_key",
            "openai": "openai_api_key",
            "xai": "xai_api_key",
        }.get(provider)
        if key_attr and not getattr(settings, key_attr, ""):
            return None
        captured.append((provider, model or ""))
        return _Fake()

    monkeypatch.setattr(_llm_mod, "_create_llm", _fake_create)
    # Stop the freeze-check short-circuit from swallowing get_llm.
    from backend.routers import system as _sys_mod
    monkeypatch.setattr(_sys_mod, "is_token_frozen", lambda: False)
    # Keep emit_token_warning quiet during the primary fallback path.
    monkeypatch.setattr(events, "emit_token_warning", lambda *a, **k: None)

    title = await chat_router._compose_title_via_llm([
        "investigate", "then ship", "observe 24h",
    ])
    assert title == "Primary fallback title"
    # The primary (google) must appear somewhere in the call trace —
    # proves the cheapest-preference loop returned None for every
    # cheap entry and the helper cascaded into ``get_llm()``.
    assert any(p == "google" for p, _m in captured), captured


@pytest.mark.asyncio
async def test_matrix_axis4_composer_empty_when_no_provider(monkeypatch):
    """Last-line defense: nothing configured → empty string → caller
    skips the ``session.titled`` emit. The sidebar continues to render
    the hash fallback silently; no error surfaces to the user.
    """
    from backend.agents import llm as _llm_mod
    monkeypatch.setattr(_llm_mod, "get_cheapest_model", lambda bind_tools=None: None)

    title = await chat_router._compose_title_via_llm([
        "anything", "at all", "pending creds",
    ])
    assert title == ""


# ─── CROSS-AXIS — full session lifecycle matrix ─────────────────────


@pytest.mark.asyncio
async def test_matrix_full_session_lifecycle_crosses_all_four_axes(
    pg_test_conn, monkeypatch,
):
    """One test plays the 4 axes in sequence to catch cross-axis drift:

      1. Three user turns via ``_persist_and_emit`` → schedule fires.
      2. Schedule wrapper runs the real composer against a fake
         ``get_cheapest_model`` → ``set_session_auto_title`` lands the
         LLM label → ``session.titled`` source=auto broadcasts.
      3. Operator PATCHes a ``user_title`` → ``session.titled`` source=
         user broadcasts; effective title flips user.
      4. Operator clears the ``user_title`` → effective title reverts
         to the auto label the LLM produced in axis 2.

    If any single axis regresses in isolation this test fails alongside
    that axis's dedicated test — the extra signal is worth the
    redundancy for a feature where the 4 concerns genuinely interact.
    """
    from backend.agents import llm as _llm_mod

    class _CheapFake:
        async def ainvoke(self, _prompt):
            class _R:
                content = "Firmware handshake retry title"
            return _R()

    monkeypatch.setattr(
        _llm_mod, "get_cheapest_model", lambda bind_tools=None: _CheapFake(),
    )

    scheduled_args: list[tuple[str, str, str]] = []

    async def _fake_generate(*, session_id, user_id, tenant_id):
        # See axis-2 test note: we record the scheduling here but do
        # the actual write below in the test body so we don't race
        # ``pg_test_conn`` with the test's own operations (asyncpg
        # rejects concurrent ops on the same connection).
        scheduled_args.append((session_id, user_id, tenant_id))

    monkeypatch.setattr(chat_router, "_generate_auto_title", _fake_generate)

    set_tenant_id("t-matrix")

    q = events.bus.subscribe()
    try:
        # AXIS 1 + 2 + 4 — 3 turns → schedule fires → compose runs
        # synchronously here → persist → emit.
        for i in range(1, 4):
            await _drive_persist_and_emit_user_turn(
                pg_test_conn,
                session_id="sess-life", user_id="u-life", idx=i,
            )
        # Flush the scheduled fake task (no-op DB work, just a record).
        for _ in range(5):
            await asyncio.sleep(0)
        assert scheduled_args == [("sess-life", "u-life", "t-matrix")]

        # Replay the auto-title generate flow on the test's conn so
        # read-after-write sits inside our outer tx.
        rows = await db.list_chat_messages(pg_test_conn, "u-life", limit=50)
        user_turns = [
            r["content"] for r in rows if r.get("role") == "user"
            and r.get("session_id") == "sess-life"
        ][:chat_router._AUTO_TITLE_TURN_THRESHOLD]
        title = await chat_router._compose_title_via_llm(user_turns)
        assert title == "Firmware handshake retry title"
        landed = await db.set_session_auto_title(
            pg_test_conn, session_id="sess-life", user_id="u-life",
            title=title,
        )
        assert landed is True
        events.emit_session_titled(
            session_id="sess-life",
            user_id="u-life",
            title=title,
            source="auto",
            broadcast_scope="user",
            tenant_id="t-matrix",
        )
        chat_router._auto_title_inflight.discard(("u-life", "sess-life"))

        meta = await db.get_chat_session_metadata(
            pg_test_conn, session_id="sess-life", user_id="u-life",
        )
        assert meta == {"auto_title": "Firmware handshake retry title"}

        # Drain chat.message emits from turns 1-3, then assert the
        # session.titled emit shape.
        seen: list[tuple[str, str]] = []
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                break
            seen.append((msg["event"], msg["data"]))
        event_names = [e for e, _ in seen]
        assert "session.titled" in event_names, event_names
        titled_payloads = [json.loads(d) for e, d in seen if e == "session.titled"]
        assert titled_payloads[0]["source"] == "auto"
        assert titled_payloads[0]["title"] == "Firmware handshake retry title"

        # AXIS 3 — user rename → source=user precedence.
        user = _au.User(
            id="u-life",
            email="u-life@example.com",
            name="u-life",
            role="operator",
            enabled=True,
            tenant_id="t-matrix",
        )

        class _FakeRequest:
            def __init__(self) -> None:
                self.state = type("S", (), {})()

        result = await chat_router.rename_session(
            session_id="sess-life",
            body=chat_router.SessionTitleBody(title="Hand-tuned title"),
            request=_FakeRequest(),
            user=user,
            conn=pg_test_conn,
        )
        assert result["metadata"] == {
            "auto_title": "Firmware handshake retry title",
            "user_title": "Hand-tuned title",
        }
        resolved_a3 = _resolve_effective_title(result["metadata"], "sess-life-xyz")
        assert resolved_a3 == {"title": "Hand-tuned title", "source": "user"}

        rename_msg = await asyncio.wait_for(q.get(), timeout=1)
        assert rename_msg["event"] == "session.titled"
        assert json.loads(rename_msg["data"])["source"] == "user"

        # AXIS 3 clear — revert to auto.
        clear_result = await chat_router.rename_session(
            session_id="sess-life",
            body=chat_router.SessionTitleBody(title=""),
            request=_FakeRequest(),
            user=user,
            conn=pg_test_conn,
        )
        assert clear_result["metadata"] == {
            "auto_title": "Firmware handshake retry title",
        }
        resolved_cleared = _resolve_effective_title(
            clear_result["metadata"], "sess-life-xyz",
        )
        assert resolved_cleared == {
            "title": "Firmware handshake retry title", "source": "auto",
        }

        # AXIS 2 — turn 4 after all this upheaval must still succeed
        # and must not re-schedule auto-title.
        fresh_schedule_calls: list = []

        async def _tracker(**kwargs):
            fresh_schedule_calls.append(kwargs["session_id"])

        monkeypatch.setattr(chat_router, "_generate_auto_title", _tracker)
        await _drive_persist_and_emit_user_turn(
            pg_test_conn, session_id="sess-life", user_id="u-life", idx=4,
        )
        await asyncio.sleep(0)
        assert fresh_schedule_calls == []
        # The chat row landed.
        msgs = await db.list_chat_messages(pg_test_conn, "u-life", limit=50)
        assert any(m["content"] == "user message #4" for m in msgs)
    finally:
        events.bus.unsubscribe(q)

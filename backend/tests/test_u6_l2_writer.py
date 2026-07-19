"""U6-2b — L2 session-summary store + writer tests.

Offline pure-helper tests (provenance hashing, watermark determinism, session-end
definition, default-OFF gate) always run; the store + writer lifecycle tests use
the real-PG fixture and skip cleanly when ``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend import db
from backend.agents.u6_l2_outcome import OutcomeKind, SessionOutcome
from backend.agents.u6_memory_scope import MemoryScope
from backend.agents.u6_l2_writer import (
    SessionEndReason,
    WriteResult,
    compute_source_message_hashes,
    compute_source_watermark,
    is_session_end,
    l2_write_enabled,
    message_hash,
    write_session_summary,
)


# ── offline: provenance + watermark determinism / collision-freeness ─────────
def test_message_hash_deterministic_and_content_sensitive() -> None:
    a = message_hash("m1", "hello")
    assert a == message_hash("m1", "hello")
    assert a != message_hash("m1", "hello!")   # content change → different hash
    assert a != message_hash("m2", "hello")    # id change → different hash


def test_message_hash_no_framing_collision() -> None:
    # ("m1","2:x") must not alias ("m1\x00..." split differently) — length framing.
    assert message_hash("m1", "2:x") != message_hash("m", "1\x002:x")


def test_watermark_changes_when_a_turn_is_added() -> None:
    base = compute_source_watermark(["m1", "m2"])
    assert base == compute_source_watermark(["m1", "m2"])       # same turns → same wm
    assert base != compute_source_watermark(["m1", "m2", "m3"])  # +1 turn → new wm
    assert base != compute_source_watermark(["m2", "m1"])        # order matters


def test_source_message_hashes_ordered() -> None:
    hs = compute_source_message_hashes([("m1", "a"), ("m2", "b")])
    assert hs == [message_hash("m1", "a"), message_hash("m2", "b")]


# ── offline: session-end is DEFINED (auto-title is not one) ──────────────────
def test_is_session_end_only_for_defined_reasons() -> None:
    assert is_session_end(SessionEndReason.INACTIVITY_TIMEOUT) is True
    assert is_session_end(SessionEndReason.EXPLICIT_CLOSE) is True
    assert is_session_end("auto_title") is False        # the 3-turn event: NOT an end
    assert is_session_end("explicit_close") is False    # raw string is not the enum
    assert is_session_end(None) is False


# ── offline: default-OFF gate ────────────────────────────────────────────────
def test_write_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_U6_L2_WRITE", raising=False)
    assert l2_write_enabled() is False


def test_write_flag_parsing(monkeypatch) -> None:
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("OMNISIGHT_U6_L2_WRITE", on)
        assert l2_write_enabled() is True
    for off in ("0", "", "no", "off"):
        monkeypatch.setenv("OMNISIGHT_U6_L2_WRITE", off)
        assert l2_write_enabled() is False


# ── PG: store round-trip + exactly-once + immutability ───────────────────────
async def _seed_tenant(conn) -> str:
    tenant_id = f"t-l2-{uuid.uuid4().hex}"
    await conn.execute(
        "INSERT INTO tenants (id, name, plan) VALUES ($1, $2, 'free')", tenant_id, tenant_id
    )
    return tenant_id


def _summary_kwargs(tenant_id: str, user_id: str, *, watermark: str, revision: int) -> dict:
    return {
        "summary_id": f"csum-{uuid.uuid4().hex}",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": "sess-1",
        "source_watermark": watermark,
        "source_message_hashes": ["h1", "h2"],
        "summary_outcome": {"schema_version": 1, "outcome_kind": "question_answered"},
        "token_count": 42,
        "model_fingerprint": "srv-abc",
        "classifier_version": 1,
        "renderer_version": 0,
        "revision": revision,
        "session_end_reason": "explicit_close",
    }


@pytest.mark.asyncio
async def test_insert_and_get_round_trip(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    kw = _summary_kwargs(t, "user-a", watermark="wm_1", revision=0)
    assert await db.insert_session_summary(pg_test_conn, **kw) is True
    got = await db.get_session_summary(
        pg_test_conn, tenant_id=t, user_id="user-a", session_id="sess-1", source_watermark="wm_1"
    )
    assert got is not None
    assert got["token_count"] == 42 and got["session_end_reason"] == "explicit_close"


@pytest.mark.asyncio
async def test_exactly_once_idempotent(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    kw = _summary_kwargs(t, "user-a", watermark="wm_1", revision=0)
    assert await db.insert_session_summary(pg_test_conn, **kw) is True
    # same exactly-once key, different surrogate id → still a no-op (idempotent).
    kw2 = dict(kw, summary_id=f"csum-{uuid.uuid4().hex}")
    assert await db.insert_session_summary(pg_test_conn, **kw2) is False


@pytest.mark.asyncio
async def test_new_watermark_is_a_new_row_and_revision(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    assert await db.insert_session_summary(
        pg_test_conn, **_summary_kwargs(t, "user-a", watermark="wm_1", revision=0)
    ) is True
    assert await db.insert_session_summary(
        pg_test_conn, **_summary_kwargs(t, "user-a", watermark="wm_2", revision=1)
    ) is True
    assert await db.latest_revision_for_session(
        pg_test_conn, tenant_id=t, user_id="user-a", session_id="sess-1"
    ) == 1


@pytest.mark.asyncio
async def test_list_is_user_scoped(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    await db.insert_session_summary(pg_test_conn, **_summary_kwargs(t, "user-a", watermark="wm_a", revision=0))
    await db.insert_session_summary(pg_test_conn, **_summary_kwargs(t, "user-b", watermark="wm_b", revision=0))
    a = await db.list_session_summaries(pg_test_conn, tenant_id=t, user_id="user-a")
    assert len(a) == 1 and a[0]["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_row_is_write_once(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    kw = _summary_kwargs(t, "user-a", watermark="wm_1", revision=0)
    await db.insert_session_summary(pg_test_conn, **kw)
    with pytest.raises(Exception):  # noqa: B017 — the BEFORE UPDATE trigger raises
        await pg_test_conn.execute(
            "UPDATE chat_session_summaries SET token_count = 999 WHERE id = $1", kw["summary_id"]
        )


@pytest.mark.asyncio
async def test_latest_revision_minus_one_when_empty(pg_test_conn) -> None:
    t = await _seed_tenant(pg_test_conn)
    assert await db.latest_revision_for_session(
        pg_test_conn, tenant_id=t, user_id="nobody", session_id="none"
    ) == -1


# ── PG: writer lifecycle (idempotent, late-turn new revision, gated) ─────────
@pytest.mark.asyncio
async def test_writer_disabled_no_ops(pg_test_conn, monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_U6_L2_WRITE", raising=False)
    t = await _seed_tenant(pg_test_conn)
    res = await write_session_summary(
        pg_test_conn,
        scope=MemoryScope(t, "user-a"),
        session_id="s1",
        reason=SessionEndReason.EXPLICIT_CLOSE,
        outcome=SessionOutcome(OutcomeKind.QUESTION_ANSWERED, 3, True),
        messages=[("m1", "hi")],
        token_count=10,
        model_fingerprint="srv",
    )
    assert res == WriteResult(False, -1, "", "disabled")


@pytest.mark.asyncio
async def test_writer_idempotent_then_new_revision(pg_test_conn, monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_L2_WRITE", "1")
    t = await _seed_tenant(pg_test_conn)
    scope = MemoryScope(t, "user-a")
    outcome = SessionOutcome(OutcomeKind.QUESTION_ANSWERED, 3, True)

    r0 = await write_session_summary(
        pg_test_conn, scope=scope, session_id="s1", reason=SessionEndReason.EXPLICIT_CLOSE,
        outcome=outcome, messages=[("m1", "hi"), ("m2", "there")],
        token_count=10, model_fingerprint="srv",
    )
    assert r0.written is True and r0.revision == 0

    # same turns → idempotent no-op, same revision.
    r0b = await write_session_summary(
        pg_test_conn, scope=scope, session_id="s1", reason=SessionEndReason.EXPLICIT_CLOSE,
        outcome=outcome, messages=[("m1", "hi"), ("m2", "there")],
        token_count=10, model_fingerprint="srv",
    )
    assert r0b.written is False and r0b.reason == "duplicate_watermark" and r0b.revision == 0

    # a late turn → new watermark → new revision.
    r1 = await write_session_summary(
        pg_test_conn, scope=scope, session_id="s1", reason=SessionEndReason.INACTIVITY_TIMEOUT,
        outcome=outcome, messages=[("m1", "hi"), ("m2", "there"), ("m3", "more")],
        token_count=15, model_fingerprint="srv",
    )
    assert r1.written is True and r1.revision == 1


@pytest.mark.asyncio
async def test_advisory_lock_serializes_concurrent_revisions(pg_test_pool, monkeypatch) -> None:
    # TRUE contention (two INDEPENDENT connections, not the single-conn fixture):
    # a race on the same session with DIFFERENT watermarks must serialize on the
    # per-session advisory lock → revisions {0, 1}, never {0, 0}.
    monkeypatch.setenv("OMNISIGHT_U6_L2_WRITE", "1")
    tenant = f"t-race-{uuid.uuid4().hex}"
    scope = MemoryScope(tenant, "user-race")
    outcome = SessionOutcome(OutcomeKind.QUESTION_ANSWERED, 3, True)
    async with pg_test_pool.acquire() as setup:
        await setup.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", tenant)
    try:
        async def _w(c, msgs):
            return await write_session_summary(
                c, scope=scope, session_id="race", reason=SessionEndReason.EXPLICIT_CLOSE,
                outcome=outcome, messages=msgs, token_count=1, model_fingerprint="s",
            )

        async with pg_test_pool.acquire() as c1, pg_test_pool.acquire() as c2:
            ra, rb = await asyncio.gather(
                _w(c1, [("m1", "a")]),
                _w(c2, [("m1", "a"), ("m2", "b")]),  # a different watermark
            )
        assert ra.written and rb.written
        assert sorted([ra.revision, rb.revision]) == [0, 1]  # serialized, no duplicate revision
    finally:
        async with pg_test_pool.acquire() as cleanup:
            await cleanup.execute("DELETE FROM chat_session_summaries WHERE tenant_id = $1", tenant)
            await cleanup.execute("DELETE FROM tenants WHERE id = $1", tenant)


@pytest.mark.asyncio
async def test_writer_rejects_non_session_end(pg_test_conn, monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_L2_WRITE", "1")
    t = await _seed_tenant(pg_test_conn)
    with pytest.raises(ValueError):
        await write_session_summary(
            pg_test_conn, scope=MemoryScope(t, "u"), session_id="s1",
            reason="auto_title",  # type: ignore[arg-type]  # not a SessionEndReason
            outcome=SessionOutcome(OutcomeKind.NO_OUTCOME, 1, False),
            messages=[("m1", "x")], token_count=1, model_fingerprint="s",
        )

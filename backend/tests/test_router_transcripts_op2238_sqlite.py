"""OP-2238 -- in-process contract tests for the BI0 ingest router.

The sibling ``test_router_transcripts_op2238.py`` runs the full HTTP
path against ``pg_test_pool`` -- which skips when ``OMNI_TEST_PG_URL``
is unset. This module re-exercises the same per-segment correctness
matrix (dedup / supersede / reject / cross-tenant / replay) at the
**handler-function** level by wrapping an in-memory SQLite connection
in an asyncpg-shape adapter. No PG required, so the AC "Exercised"
guarantee holds even on a minimal runner.

The adapter is intentionally tiny -- it only handles the four query
shapes the router actually issues:

  * INSERT INTO meetings ... ON CONFLICT (id) DO NOTHING RETURNING id
  * SELECT ... FROM meetings WHERE id = $1 [AND tenant_id = $2]
  * SELECT id, is_final, text FROM transcript_segments WHERE ...
  * INSERT / UPDATE / SELECT against transcript_segments

Every query uses asyncpg-style ``$N`` placeholders -- the adapter
rewrites them to ``?`` before handing to sqlite3.

Module-global state audit
-------------------------
Each test uses a fresh in-memory sqlite3 connection (no state shared
between tests). The router's whole-batch idempotency cache
(``_idem_cache``) is reset between tests via the autouse fixture.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

import pytest


# ━━ asyncpg-shape adapter over sqlite3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _translate_sql(sql: str, params: tuple) -> tuple[str, list]:
    """asyncpg ``$N`` -> sqlite3 ``?``; expand ``$N`` referenced twice
    into two ``?`` bound to the same value. Coerce bool params to int."""
    out_sql_parts: list[str] = []
    out_params: list[Any] = []
    last = 0
    for m in re.finditer(r"\$(\d+)", sql):
        idx = int(m.group(1)) - 1
        out_sql_parts.append(sql[last:m.start()])
        out_sql_parts.append("?")
        out_params.append(_coerce_param(params[idx]))
        last = m.end()
    out_sql_parts.append(sql[last:])
    rewritten = "".join(out_sql_parts)
    rewritten = re.sub(r"\bTRUE\b", "1", rewritten)
    rewritten = re.sub(r"\bFALSE\b", "0", rewritten)
    return rewritten, out_params


def _coerce_param(p: Any) -> Any:
    if isinstance(p, bool):
        return 1 if p else 0
    return p


class _Record:
    """asyncpg.Record stand-in: dict-like access by key + index."""
    def __init__(self, row: sqlite3.Row) -> None:
        self._row = row

    def __getitem__(self, key):
        return self._row[key]

    def get(self, key, default=None):
        try:
            return self._row[key]
        except (IndexError, KeyError):
            return default


class FakeConn:
    """Tiny asyncpg-shape adapter over a sqlite3 in-memory connection."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        # Schema mirrors alembic 0249 (modulo dialect coercions).
        self._conn.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                retention_until REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE transcript_segments (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                meeting_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                segment_seq INTEGER NOT NULL,
                start_ms INTEGER,
                end_ms INTEGER,
                text TEXT NOT NULL,
                language TEXT,
                confidence REAL,
                is_final INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (tenant_id, meeting_id, session_id, segment_seq)
            );
            """
        )

    async def fetchrow(self, sql: str, *params):
        rewritten, bound = _translate_sql(sql, params)
        cur = self._conn.execute(rewritten, bound)
        row = cur.fetchone()
        return _Record(row) if row else None

    async def fetch(self, sql: str, *params):
        rewritten, bound = _translate_sql(sql, params)
        cur = self._conn.execute(rewritten, bound)
        return [_Record(row) for row in cur.fetchall()]

    async def execute(self, sql: str, *params):
        rewritten, bound = _translate_sql(sql, params)
        self._conn.execute(rewritten, bound)
        self._conn.commit()

    def transaction(self):  # used by audit.log when conn is passed in
        return _NoopTx()


class _NoopTx:
    async def __aenter__(self): return self
    async def __aexit__(self, et, ev, tb): return False


def _user(tenant_id: str = "t-alpha"):
    from backend import auth as _au
    return _au.User(
        id="u-test", email="op2238@test", name="OP-2238",
        role="operator", enabled=True, tenant_id=tenant_id,
    )


@pytest.fixture(autouse=True)
def _reset_idempotency_cache():
    """Wipe the router's whole-batch idempotency cache between tests."""
    from backend.routers import transcripts as r
    r._idem_cache.clear()
    r._idem_order.clear()
    yield
    r._idem_cache.clear()
    r._idem_order.clear()


@pytest.fixture(autouse=True)
def _disable_audit(monkeypatch):
    """The audit chain depends on a PG advisory lock + audit_log table;
    swap it for a no-op so unit tests don't blow up the audit path."""
    from backend import audit

    async def _noop_log(*a, **k):
        return 1

    monkeypatch.setattr(audit, "log", _noop_log)


# ━━ Helpers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _seg(seq: int, text: str, *, final: bool, source: str = "asr",
         lang: str | None = None) -> dict:
    from backend.models import TranscriptSegmentIn
    return TranscriptSegmentIn(
        segment_seq=seq, text=text, is_final=final, source=source,
        language=lang,
    )


async def _ingest(conn, mid: str, session_id: str, segs: list,
                  tenant_id: str = "t-alpha", idem: str | None = None):
    from backend.routers import transcripts as r
    from backend.models import TranscriptIngestRequest
    body = TranscriptIngestRequest(session_id=session_id, segments=segs)
    return await r.ingest_segments(
        meeting_id=mid, body=body, user=_user(tenant_id), conn=conn,
        idempotency_key=idem,
    )


# ━━ Contract tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_duplicate_segment_is_deduped() -> None:
    conn = FakeConn()
    segs = [_seg(0, "hi", final=True)]
    r1 = await _ingest(conn, "mtg-1", "sess-1", segs)
    assert r1.accepted == 1 and r1.deduped == 0
    r2 = await _ingest(conn, "mtg-1", "sess-1", segs)
    assert r2.accepted == 0 and r2.deduped == 1
    assert r2.rejected == []


async def test_out_of_order_arrivals_read_in_seq_order() -> None:
    from backend.routers.transcripts import list_segments
    conn = FakeConn()
    # Post 2, then 0, then 1 -- the dedup index doesn't care about order.
    for seq, text in ((2, "third"), (0, "first"), (1, "second")):
        await _ingest(conn, "mtg-1", "sess-1", [_seg(seq, text, final=True)])
    rows = await list_segments(
        meeting_id="mtg-1", since_seq=None, final_only=False,
        user=_user(), conn=conn,
    )
    assert [r.segment_seq for r in rows] == [0, 1, 2]
    assert [r.text for r in rows] == ["first", "second", "third"]


async def test_partial_then_final_supersedes() -> None:
    from backend.routers.transcripts import list_segments
    conn = FakeConn()
    await _ingest(conn, "mtg-1", "sess-1", [_seg(0, "partial", final=False)])
    result = await _ingest(
        conn, "mtg-1", "sess-1", [_seg(0, "FINAL", final=True)],
    )
    assert result.superseded == 1
    assert result.accepted == 0
    rows = await list_segments(
        meeting_id="mtg-1", since_seq=None, final_only=False,
        user=_user(), conn=conn,
    )
    assert len(rows) == 1
    assert rows[0].text == "FINAL"
    assert rows[0].is_final is True


async def test_final_then_partial_is_rejected() -> None:
    from backend.routers.transcripts import list_segments
    conn = FakeConn()
    await _ingest(conn, "mtg-1", "sess-1", [_seg(0, "FINAL", final=True)])
    result = await _ingest(
        conn, "mtg-1", "sess-1", [_seg(0, "partial-later", final=False)],
    )
    assert result.accepted == 0
    assert result.superseded == 0
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "final_superseded"
    rows = await list_segments(
        meeting_id="mtg-1", since_seq=None, final_only=False,
        user=_user(), conn=conn,
    )
    assert [r.text for r in rows] == ["FINAL"]


async def test_reconnect_replay_is_idempotent() -> None:
    from backend.routers.transcripts import list_segments
    conn = FakeConn()
    segs = [
        _seg(0, "a", final=True),
        _seg(1, "b", final=True),
        _seg(2, "c", final=True),
    ]
    r1 = await _ingest(conn, "mtg-1", "sess-replay", segs)
    assert r1.accepted == 3
    # Full batch replay (same session_id, same seqs)
    r2 = await _ingest(conn, "mtg-1", "sess-replay", segs)
    assert r2.accepted == 0
    assert r2.deduped == 3
    rows = await list_segments(
        meeting_id="mtg-1", since_seq=None, final_only=False,
        user=_user(), conn=conn,
    )
    assert len(rows) == 3


async def test_cross_tenant_meeting_returns_404() -> None:
    """Tenant A creates a meeting; tenant B's ingest must 404."""
    from fastapi import HTTPException
    conn = FakeConn()
    # Tenant A writes first segment -- auto-creates meeting row.
    await _ingest(conn, "mtg-shared", "sess-1",
                  [_seg(0, "hi from A", final=True)],
                  tenant_id="t-alpha")
    # Tenant B attempts the same meeting id.
    with pytest.raises(HTTPException) as ei:
        await _ingest(conn, "mtg-shared", "sess-b",
                      [_seg(0, "hi from B", final=True)],
                      tenant_id="t-beta")
    assert ei.value.status_code == 404


async def test_idempotency_key_returns_cached_response() -> None:
    """Replay with the same Idempotency-Key returns the cached body and
    does NOT re-touch the segments table."""
    conn = FakeConn()
    segs = [_seg(0, "hi", final=True)]
    r1 = await _ingest(
        conn, "mtg-1", "sess-1", segs, idem="op2238-key",
    )
    assert r1.accepted == 1
    r2 = await _ingest(
        conn, "mtg-1", "sess-1", segs, idem="op2238-key",
    )
    # Cached -- same shape as r1; deduped did NOT increment.
    assert r2.model_dump() == r1.model_dump()
    # Storage has exactly one row.
    rows = await conn.fetch(
        "SELECT COUNT(*) AS n FROM transcript_segments",
    )
    assert int(rows[0]["n"]) == 1


async def test_envelope_aggregates_counts_languages_and_ts() -> None:
    from backend.routers.transcripts import get_meeting_envelope
    conn = FakeConn()
    await _ingest(conn, "mtg-env", "sess-1", [
        _seg(0, "hi", final=True, lang="en"),
        _seg(1, "salut", final=False, lang="fr"),
    ])
    # Patch start_ms/end_ms via a follow-up batch -- the helper above
    # always uses None for start_ms/end_ms, so we set them via raw SQL
    # to keep the test focused on the aggregate query shape.
    await conn.execute(
        "UPDATE transcript_segments SET start_ms = 100, end_ms = 500 "
        "WHERE segment_seq = 0",
    )
    await conn.execute(
        "UPDATE transcript_segments SET start_ms = 500, end_ms = 900 "
        "WHERE segment_seq = 1",
    )
    env = await get_meeting_envelope(
        meeting_id="mtg-env", user=_user(), conn=conn,
    )
    assert env.segment_count == 2
    assert env.final_segment_count == 1
    assert sorted(env.languages) == ["en", "fr"]
    assert env.first_segment_ts_ms == 100
    assert env.last_segment_ts_ms == 900


async def test_open_meeting_explicit_then_repeat() -> None:
    """Explicit POST /meetings is idempotent for the same id."""
    from backend.routers.transcripts import open_meeting
    from backend.models import OpenMeetingRequest

    conn = FakeConn()
    r1 = await open_meeting(
        body=OpenMeetingRequest(id="mtg-explicit", title="Daily"),
        user=_user(), conn=conn,
    )
    assert r1.status_code == 201
    r2 = await open_meeting(
        body=OpenMeetingRequest(id="mtg-explicit", title="Daily"),
        user=_user(), conn=conn,
    )
    # Repeat is a 200 with the existing row.
    assert r2.status_code == 200

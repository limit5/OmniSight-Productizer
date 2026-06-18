"""OP-2240 BI1 — meeting summary router contract tests.

The router under test is :mod:`backend.routers.meeting_summary`. We
exercise it at the **handler-function** level (no HTTP, no real LLM,
no PG) by reusing the asyncpg-shape sqlite adapter pattern from
``test_router_transcripts_op2238_sqlite.py``.

The LLM call goes through :func:`backend.llm_adapter.invoke_chat` —
monkeypatched here to return a canned JSON reply so the test stays
deterministic and hermetic. ruff-clean / no live keys required.

Acceptance Criteria coverage
----------------------------
* AC.code     — endpoint reads final segments + invokes LLM
                (``test_summary_uses_final_segments_only``,
                 ``test_summary_returns_structured_shape``).
* AC.flag     — disabled-by-default → 404
                (``test_disabled_flag_returns_404``);
                enabled → non-empty summary
                (``test_summary_returns_structured_shape``).
* AC.tenant   — cross-tenant meeting → 404
                (``test_cross_tenant_meeting_returns_404``).
* AC.exercised — pytest, LLM stubbed (all tests above).
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

import pytest


# ━━ asyncpg-shape adapter (mirrors OP-2238 sibling) ━━━━━━━━━━━━━━━━━━


def _translate_sql(sql: str, params: tuple) -> tuple[str, list]:
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
    """Tiny asyncpg-shape adapter over sqlite3:memory: — mirror of
    the OP-2238 sqlite test adapter so the suites stay in lockstep."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'open',
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


def _user(tenant_id: str = "t-alpha"):
    from backend import auth as _au
    return _au.User(
        id="u-test", email="op2240@test", name="OP-2240",
        role="operator", enabled=True, tenant_id=tenant_id,
    )


# ━━ Fixtures ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def feature_enabled(monkeypatch):
    """Flip the OP-2240 kill-switch on for the duration of one test."""
    from backend.config import settings
    monkeypatch.setattr(settings, "meeting_summary_enabled", True)
    yield


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace ``invoke_chat`` with a deterministic JSON reply.

    Returns the *list of calls* (each entry = the messages list the
    router passed in) so tests can assert prompt assembly without
    needing a real LangChain call.
    """
    calls: list[Any] = []

    def _fake_invoke_chat(messages, **kwargs):
        calls.append({"messages": list(messages), "kwargs": kwargs})
        return (
            '{"tldr": "Team aligned on Q3 priorities.", '
            '"bullet_points": ["Hire two ICs", "Ship onboarding v2",'
            ' "Defer mobile rewrite"]}'
        )

    from backend import llm_adapter
    from backend.routers import meeting_summary as _ms
    monkeypatch.setattr(llm_adapter, "invoke_chat", _fake_invoke_chat)
    # Defensive: in case some loader has already bound the symbol
    # into the router module namespace, patch there too. (Currently
    # the router does a lazy ``from backend.llm_adapter import …``
    # inside the handler, so the module-level monkeypatch above is
    # the load-time path that the handler will pick up.)
    monkeypatch.setattr(
        _ms, "_resolve_model_label", lambda: "stub:test-model",
    )
    return calls


# ━━ Helpers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _seed_meeting(
    conn: FakeConn, meeting_id: str, tenant_id: str = "t-alpha",
    title: str | None = "Daily standup",
) -> None:
    now = time.time()
    await conn.execute(
        "INSERT INTO meetings (id, tenant_id, title, status, "
        "created_at, updated_at) VALUES ($1, $2, $3, 'open', $4, $4)",
        meeting_id, tenant_id, title, now,
    )


async def _seed_segment(
    conn: FakeConn,
    meeting_id: str,
    seq: int,
    text: str,
    *,
    is_final: bool = True,
    tenant_id: str = "t-alpha",
    session_id: str = "sess-1",
) -> None:
    now = time.time()
    await conn.execute(
        "INSERT INTO transcript_segments ("
        "  id, tenant_id, meeting_id, session_id, segment_seq, "
        "  start_ms, end_ms, text, language, confidence, is_final, "
        "  source, created_at, updated_at"
        ") VALUES ($1, $2, $3, $4, $5, NULL, NULL, $6, NULL, NULL, "
        "$7, 'asr', $8, $8)",
        f"seg-{tenant_id}-{seq}",
        tenant_id, meeting_id, session_id, seq, text, is_final, now,
    )


# ━━ Contract tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def test_disabled_flag_returns_404() -> None:
    """AC: with the feature flag OFF (default), the endpoint must 404.

    Off-state must look identical to "meeting not found" so non-
    opted-in tenants do not discover the BI1 surface.
    """
    from fastapi import HTTPException

    from backend.routers.meeting_summary import summarise_meeting

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1")
    await _seed_segment(conn, "mtg-1", 0, "hello")

    with pytest.raises(HTTPException) as ei:
        await summarise_meeting(
            meeting_id="mtg-1", user=_user(), conn=conn,
        )
    assert ei.value.status_code == 404
    assert "feature not enabled" in ei.value.detail


async def test_summary_returns_structured_shape(
    feature_enabled, stub_llm,
) -> None:
    """AC: with the flag ON and seeded segments, returns a non-empty
    structured summary with the contracted MeetingSummary shape."""
    from backend.routers.meeting_summary import (
        MeetingSummary, summarise_meeting,
    )

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1")
    await _seed_segment(conn, "mtg-1", 0, "Welcome to the meeting.")
    await _seed_segment(conn, "mtg-1", 1, "Q3 priorities: hiring and onboarding.")
    await _seed_segment(conn, "mtg-1", 2, "We will defer the mobile rewrite.")

    result = await summarise_meeting(
        meeting_id="mtg-1", user=_user(), conn=conn,
    )

    assert isinstance(result, MeetingSummary)
    assert result.meeting_id == "mtg-1"
    assert result.tldr == "Team aligned on Q3 priorities."
    assert result.bullet_points == [
        "Hire two ICs", "Ship onboarding v2", "Defer mobile rewrite",
    ]
    assert result.model == "stub:test-model"
    assert result.segment_count == 3
    assert result.generated_at > 0
    # The LLM stub recorded one call — assert the system prompt
    # framing reached the provider.
    assert len(stub_llm) == 1
    sys_msg = stub_llm[0]["messages"][0]
    assert "STRICT JSON" in getattr(sys_msg, "content", "")


async def test_summary_uses_final_segments_only(
    feature_enabled, stub_llm,
) -> None:
    """Partial (non-final) segments must NOT reach the LLM prompt."""
    from backend.routers.meeting_summary import summarise_meeting

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1")
    await _seed_segment(conn, "mtg-1", 0, "final-content-A", is_final=True)
    await _seed_segment(
        conn, "mtg-1", 1, "PARTIAL-LEAKED", is_final=False,
    )
    await _seed_segment(conn, "mtg-1", 2, "final-content-B", is_final=True)

    result = await summarise_meeting(
        meeting_id="mtg-1", user=_user(), conn=conn,
    )

    assert result.segment_count == 2  # only finals counted
    human_msg = stub_llm[0]["messages"][1]
    body = getattr(human_msg, "content", "")
    assert "final-content-A" in body
    assert "final-content-B" in body
    assert "PARTIAL-LEAKED" not in body


async def test_meeting_not_found_returns_404(
    feature_enabled, stub_llm,
) -> None:
    """A meeting_id that doesn't exist (or lives in a different
    tenant) must 404 even with the flag enabled."""
    from fastapi import HTTPException

    from backend.routers.meeting_summary import summarise_meeting

    conn = FakeConn()
    # No meeting seeded.
    with pytest.raises(HTTPException) as ei:
        await summarise_meeting(
            meeting_id="mtg-missing", user=_user(), conn=conn,
        )
    assert ei.value.status_code == 404
    # The LLM must NOT have been called for a 404 path.
    assert stub_llm == []


async def test_cross_tenant_meeting_returns_404(
    feature_enabled, stub_llm,
) -> None:
    """Tenant A's meeting is invisible to tenant B (no existence leak)."""
    from fastapi import HTTPException

    from backend.routers.meeting_summary import summarise_meeting

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-shared", tenant_id="t-alpha")
    await _seed_segment(
        conn, "mtg-shared", 0, "secrets from A", tenant_id="t-alpha",
    )

    with pytest.raises(HTTPException) as ei:
        await summarise_meeting(
            meeting_id="mtg-shared", user=_user("t-beta"), conn=conn,
        )
    assert ei.value.status_code == 404
    assert stub_llm == []


async def test_meeting_with_no_final_segments_returns_409(
    feature_enabled, stub_llm,
) -> None:
    """A meeting with only partials must NOT call the LLM — 409."""
    from fastapi import HTTPException

    from backend.routers.meeting_summary import summarise_meeting

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1")
    await _seed_segment(conn, "mtg-1", 0, "partial only", is_final=False)

    with pytest.raises(HTTPException) as ei:
        await summarise_meeting(
            meeting_id="mtg-1", user=_user(), conn=conn,
        )
    assert ei.value.status_code == 409
    assert stub_llm == []


async def test_llm_empty_reply_returns_503(
    feature_enabled, monkeypatch,
) -> None:
    """If the LLM is unconfigured / circuit-open the adapter returns
    an empty string; the router must surface this as 503, not as a
    successful empty MeetingSummary."""
    from fastapi import HTTPException

    from backend import llm_adapter
    from backend.routers.meeting_summary import summarise_meeting

    monkeypatch.setattr(
        llm_adapter, "invoke_chat", lambda messages, **kw: "",
    )

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1")
    await _seed_segment(conn, "mtg-1", 0, "hello")

    with pytest.raises(HTTPException) as ei:
        await summarise_meeting(
            meeting_id="mtg-1", user=_user(), conn=conn,
        )
    assert ei.value.status_code == 503


async def test_llm_reply_with_markdown_fence_is_parsed(
    feature_enabled, monkeypatch,
) -> None:
    """The strict-JSON prompt asks for raw JSON, but providers often
    wrap in markdown fences. Parser must extract the JSON inside."""
    from backend import llm_adapter
    from backend.routers.meeting_summary import summarise_meeting

    reply = (
        "Sure, here's the summary:\n"
        "```json\n"
        '{"tldr": "Quarterly check-in.", '
        '"bullet_points": ["Budget is on track", "Hiring paused"]}\n'
        "```\n"
        "Let me know if you need anything else."
    )
    monkeypatch.setattr(
        llm_adapter, "invoke_chat", lambda messages, **kw: reply,
    )

    conn = FakeConn()
    await _seed_meeting(conn, "mtg-1", title=None)
    await _seed_segment(conn, "mtg-1", 0, "Quarterly notes.")

    result = await summarise_meeting(
        meeting_id="mtg-1", user=_user(), conn=conn,
    )
    assert result.tldr == "Quarterly check-in."
    assert result.bullet_points == [
        "Budget is on track", "Hiring paused",
    ]

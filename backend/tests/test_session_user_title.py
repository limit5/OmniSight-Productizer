"""ZZ.B2 #304-2 checkbox 2 — operator-authored session rename tests.

Locks the fallback-chain contract ``user_title`` → ``auto_title`` →
hash on the backend side. The frontend side is covered by the
``chat-sessions-sidebar-rename.test.tsx`` test file; the two meet at
the ``session.titled`` SSE event with ``source="user"``.

Covers:

1. ``set_session_user_title`` writes ``metadata.user_title`` without
   disturbing an existing ``auto_title`` (fallback chain invariant —
   an operator rename must not invalidate the LLM title that lives
   underneath).
2. ``set_session_user_title`` overwrites the existing user_title
   (unlike auto_title's at-most-once contract — the operator can
   re-rename freely).
3. Empty / whitespace / ``None`` clears the ``user_title`` key so the
   sidebar reverts to ``auto_title`` / hash.
4. ``set_session_user_title`` returns ``False`` when no row matches
   (unknown session id or cross-tenant).
5. Tenant scope — a rename request arriving on a swapped auth token
   cannot mutate another tenant's row.
6. ``PATCH /chat/sessions/{sid}/title`` emits ``session.titled`` with
   ``source="user"`` and carries the effective title (cleaned input,
   or the surviving auto_title when the override is cleared).
7. Pydantic input bound to 120 chars (defensive cap; mirrors
   ``set_session_auto_title`` title_clean).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import auth as _au
from backend import db, events
from backend.db_context import set_tenant_id
from backend.routers import chat as chat_router


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    set_tenant_id(None)
    yield
    set_tenant_id(None)


def _fake_user(user_id: str = "u-1", tenant_id: str = "t-alpha") -> _au.User:
    return _au.User(
        id=user_id,
        email=f"{user_id}@example.com",
        name=user_id,
        role="operator",
        enabled=True,
        tenant_id=tenant_id,
    )


class _FakeRequest:
    """Stand-in Request — ``rename_session`` only uses the Request for
    future hooks (session_id_from_request is not called here); keeping
    the attribute surface tiny makes the test intent obvious."""
    def __init__(self) -> None:
        self.state = type("S", (), {})()


# ─── db helper branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_session_user_title_preserves_auto_title(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    assert await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="LLM title",
    ) is True
    # Now apply an operator rename — auto_title must survive so that
    # clearing the user_title later restores the auto label.
    ok = await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Operator rename",
    )
    assert ok is True
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {
        "auto_title": "LLM title",
        "user_title": "Operator rename",
    }


@pytest.mark.asyncio
async def test_set_session_user_title_allows_overwrite(pg_test_conn):
    """Operator titles are editable — unlike auto_title, a second
    ``set_session_user_title`` call must replace the stored value.
    Correctness gate: a typo rename must be fixable without needing a
    db-level escape hatch."""
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="First rename",
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Second rename",
    )
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {"user_title": "Second rename"}


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_input", ["", "   ", None])
async def test_set_session_user_title_empty_clears_key(pg_test_conn, empty_input):
    """Empty / whitespace / None removes the key so the fallback chain
    drops back to ``auto_title`` / hash."""
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="LLM title",
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Operator rename",
    )
    ok = await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title=empty_input,
    )
    assert ok is True
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    # auto_title survives the clear; user_title is gone.
    assert meta == {"auto_title": "LLM title"}


@pytest.mark.asyncio
async def test_set_session_user_title_unknown_session_returns_false(pg_test_conn):
    set_tenant_id("t-alpha")
    ok = await db.set_session_user_title(
        pg_test_conn, session_id="never-inserted", user_id="u-1",
        title="Anything",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_set_session_user_title_is_tenant_scoped(pg_test_conn):
    """A rename issued under a different tenant must not touch the
    original row — covers the token-swap threat model that the
    sidebar endpoint inherits from ``tenant_where_pg``."""
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Alpha rename",
    )
    # Attacker on t-beta tries to rename the same session id.
    set_tenant_id("t-beta")
    ok = await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Beta would-be override",
    )
    assert ok is False
    # Verify the original tenant's title is unchanged.
    set_tenant_id("t-alpha")
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {"user_title": "Alpha rename"}


@pytest.mark.asyncio
async def test_set_session_user_title_applies_120char_cap(pg_test_conn):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    overflow = "x" * 200
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title=overflow,
    )
    meta = await db.get_chat_session_metadata(
        pg_test_conn, session_id="sess-1", user_id="u-1",
    )
    assert meta == {"user_title": "x" * 120}


# ─── endpoint behaviour (direct function invocation) ─────────────────


@pytest.mark.asyncio
async def test_rename_session_endpoint_emits_session_titled_source_user(
    pg_test_conn,
):
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    q = events.bus.subscribe()
    try:
        result = await chat_router.rename_session(
            session_id="sess-1",
            body=chat_router.SessionTitleBody(title="  Wire up deep link  "),
            request=_FakeRequest(),
            user=_fake_user(tenant_id="t-alpha"),
            conn=pg_test_conn,
        )
        assert result["session_id"] == "sess-1"
        assert result["metadata"] == {"user_title": "Wire up deep link"}
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "session.titled"
        payload = json.loads(msg["data"])
        assert payload["session_id"] == "sess-1"
        assert payload["user_id"] == "u-1"
        assert payload["title"] == "Wire up deep link"
        assert payload["source"] == "user"
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_rename_session_endpoint_empty_falls_back_to_auto_title(
    pg_test_conn,
):
    """Clearing ``user_title`` must fan out the surviving ``auto_title``
    so a second device's sidebar re-renders to the LLM label without
    a refetch. If auto_title were absent the payload title is empty
    and the sidebar resolver would pick hash — that's the fallback
    chain's 3rd step, also tested."""
    set_tenant_id("t-alpha")
    await db.upsert_chat_session(
        pg_test_conn, session_id="sess-1", user_id="u-1", now=100.0,
    )
    await db.set_session_auto_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="LLM label",
    )
    await db.set_session_user_title(
        pg_test_conn, session_id="sess-1", user_id="u-1",
        title="Temporary rename",
    )
    q = events.bus.subscribe()
    try:
        result = await chat_router.rename_session(
            session_id="sess-1",
            body=chat_router.SessionTitleBody(title=""),
            request=_FakeRequest(),
            user=_fake_user(tenant_id="t-alpha"),
            conn=pg_test_conn,
        )
        assert result["metadata"] == {"auto_title": "LLM label"}
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["source"] == "user"
        # The broadcast title is the surviving auto_title so other
        # devices relabel back to the LLM label in-place.
        assert payload["title"] == "LLM label"
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_rename_session_endpoint_404_for_unknown_session(pg_test_conn):
    from fastapi import HTTPException
    set_tenant_id("t-alpha")
    with pytest.raises(HTTPException) as exc_info:
        await chat_router.rename_session(
            session_id="never-inserted",
            body=chat_router.SessionTitleBody(title="Anything"),
            request=_FakeRequest(),
            user=_fake_user(tenant_id="t-alpha"),
            conn=pg_test_conn,
        )
    assert exc_info.value.status_code == 404


def test_session_title_body_enforces_120_char_cap():
    """Pydantic body validation — a malicious caller submitting 5k
    chars must be rejected by FastAPI before the db helper even
    runs. This mirrors the 120-char cap in
    ``set_session_user_title`` so the two fences are explicit."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        chat_router.SessionTitleBody(title="x" * 121)
    # Empty + None are allowed (they mean "clear").
    assert chat_router.SessionTitleBody(title="").title == ""
    assert chat_router.SessionTitleBody(title=None).title is None
    # Exactly 120 chars passes — boundary check.
    ok = chat_router.SessionTitleBody(title="y" * 120)
    assert ok.title == "y" * 120

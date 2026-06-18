"""OP-2243 -- BI4 meeting suggestions router contracts."""

from __future__ import annotations

import inspect
import json

import pytest
from fastapi import HTTPException

from backend import auth as _au
from backend.routers import meeting_suggestions as r
from backend.tests.test_router_transcripts_op2238_sqlite import (
    FakeConn,
    _ingest,
    _seg,
)


def _user(tenant_id: str = "t-alpha") -> _au.User:
    return _au.User(
        id="u-op2243",
        email="op2243@test",
        name="OP-2243",
        role="operator",
        enabled=True,
        tenant_id=tenant_id,
    )


def test_router_is_mounted_on_versioned_app() -> None:
    from pathlib import Path

    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "from backend.routers import meeting_suggestions" in main_src
    assert "_meeting_suggestions_router.router" in main_src


def test_router_prefix_and_operator_gate() -> None:
    assert r.router.prefix == "/meetings"
    src = inspect.getsource(r.suggest_meeting_contributions)
    assert "Depends(_au.require_operator)" in src
    assert "settings.meeting_suggestions_enabled" in src


async def test_disabled_by_default_blocks_suggestions(monkeypatch) -> None:
    conn = FakeConn()
    await _ingest(conn, "mtg-disabled", "sess-1", [_seg(0, "hello", final=True)])
    monkeypatch.setattr(r.settings, "meeting_suggestions_enabled", False)

    with pytest.raises(HTTPException) as exc:
        await r.suggest_meeting_contributions(
            meeting_id="mtg-disabled",
            user=_user(),
            conn=conn,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail == "meeting suggestions disabled"


async def test_flag_on_reads_final_segments_and_returns_stubbed_suggestions(
    monkeypatch,
) -> None:
    conn = FakeConn()
    await _ingest(conn, "mtg-suggest", "sess-1", [
        _seg(0, "Ari covered the launch risk.", final=True),
        _seg(1, "Bo: maybe later", final=False),
        _seg(2, "Chen asked for a customer follow-up.", final=True),
    ])
    monkeypatch.setattr(r.settings, "meeting_suggestions_enabled", True)
    monkeypatch.setattr(r.settings, "llm_model", "stub-model")
    monkeypatch.setattr(r, "get_llm", lambda: object())

    calls: list[dict] = []

    def _fake_invoke_chat(messages, *, llm=None, max_tokens=None):
        calls.append({"messages": messages, "llm": llm, "max_tokens": max_tokens})
        return json.dumps({
            "suggestions": [
                {
                    "kind": "contribution",
                    "text": "Invite Bo to expand on launch blockers.",
                },
            ],
        })

    monkeypatch.setattr(r, "invoke_chat", _fake_invoke_chat)

    out = await r.suggest_meeting_contributions(
        meeting_id="mtg-suggest",
        user=_user(),
        conn=conn,
    )

    assert out.model == "stub-model"
    assert [s.model_dump() for s in out.suggestions] == [
        {
            "kind": "contribution",
            "text": "Invite Bo to expand on launch blockers.",
        },
    ]
    assert calls and calls[0]["max_tokens"] == 512
    prompt = calls[0]["messages"][1][1]
    assert "Ari covered the launch risk." in prompt
    assert "Chen asked for a customer follow-up." in prompt
    assert "Bo: maybe later" not in prompt


async def test_cross_tenant_meeting_is_not_found(monkeypatch) -> None:
    conn = FakeConn()
    await _ingest(
        conn,
        "mtg-tenant",
        "sess-1",
        [_seg(0, "tenant alpha only", final=True)],
        tenant_id="t-alpha",
    )
    monkeypatch.setattr(r.settings, "meeting_suggestions_enabled", True)

    with pytest.raises(HTTPException) as exc:
        await r.suggest_meeting_contributions(
            meeting_id="mtg-tenant",
            user=_user("t-beta"),
            conn=conn,
        )

    assert exc.value.status_code == 404

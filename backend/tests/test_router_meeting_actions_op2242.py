"""OP-2242 -- structured meeting action-items extraction contract."""

from __future__ import annotations

import pytest

from tests.test_router_transcripts_op2238_sqlite import FakeConn, _ingest, _seg, _user


class _FakeLlm:
    model_name = "stub-actions-model"


async def test_action_items_endpoint_disabled_by_default(monkeypatch) -> None:
    from fastapi import HTTPException
    from backend.config import settings
    from backend.routers import meeting_actions as r

    monkeypatch.setattr(settings, "meeting_action_items_enabled", False)

    with pytest.raises(HTTPException) as ei:
        await r.extract_action_items(
            meeting_id="mtg-disabled",
            user=_user(),
            conn=FakeConn(),
        )

    assert ei.value.status_code == 404
    assert ei.value.detail == "meeting action-items disabled"


async def test_action_items_extracts_structured_json_from_final_segments(
    monkeypatch,
) -> None:
    from backend import llm_adapter
    from backend.config import settings
    from backend.routers import meeting_actions as r

    conn = FakeConn()
    await _ingest(
        conn,
        "mtg-actions",
        "sess-partial",
        [_seg(0, "draft thought that should not be extracted", final=False)],
    )
    await _ingest(
        conn,
        "mtg-actions",
        "sess-final",
        [
            _seg(1, "Alice will send the revised launch notes by Friday.", final=True),
            _seg(2, "The team discussed launch risk and rollback criteria.", final=True),
        ],
    )

    captured: dict[str, object] = {}

    def _fake_tool_call(messages, tools, *, llm=None, **_kwargs):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["llm"] = llm
        transcript_prompt = messages[-1][1]
        assert "draft thought" not in transcript_prompt
        assert "Alice will send" in transcript_prompt
        assert "launch risk" in transcript_prompt
        return llm_adapter.AdapterToolResponse(
            text="",
            tool_calls=[
                llm_adapter.AdapterToolCall(
                    name="record_meeting_action_items",
                    arguments={
                        "action_items": [
                            {
                                "text": "Send the revised launch notes.",
                                "owner": "Alice",
                                "due": "Friday",
                            },
                        ],
                        "discussion_points": [
                            {
                                "topic": "Launch risk",
                                "summary": "The team reviewed rollback criteria.",
                            },
                        ],
                    },
                ),
            ],
        )

    monkeypatch.setattr(settings, "meeting_action_items_enabled", True)
    monkeypatch.setattr(r, "get_llm", lambda: _FakeLlm())
    monkeypatch.setattr(r.llm_adapter, "tool_call", _fake_tool_call)

    result = await r.extract_action_items(
        meeting_id="mtg-actions",
        user=_user(),
        conn=conn,
    )

    assert captured["llm"].model_name == "stub-actions-model"
    assert result.model == "stub-actions-model"
    assert result.action_items[0].text == "Send the revised launch notes."
    assert result.action_items[0].owner == "Alice"
    assert result.action_items[0].due == "Friday"
    assert result.discussion_points[0].topic == "Launch risk"
    assert result.discussion_points[0].summary == "The team reviewed rollback criteria."


async def test_action_items_cross_tenant_meeting_returns_404(monkeypatch) -> None:
    from fastapi import HTTPException
    from backend.config import settings
    from backend.routers import meeting_actions as r

    conn = FakeConn()
    await _ingest(
        conn,
        "mtg-shared",
        "sess-1",
        [_seg(0, "Tenant A final segment", final=True)],
        tenant_id="t-alpha",
    )

    monkeypatch.setattr(settings, "meeting_action_items_enabled", True)
    monkeypatch.setattr(r, "get_llm", lambda: _FakeLlm())

    with pytest.raises(HTTPException) as ei:
        await r.extract_action_items(
            meeting_id="mtg-shared",
            user=_user("t-beta"),
            conn=conn,
        )

    assert ei.value.status_code == 404
    assert ei.value.detail == "meeting not found"


def test_meeting_actions_router_registered_in_main() -> None:
    from pathlib import Path

    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "from backend.routers import meeting_actions" in main_src
    assert "_meeting_actions_router.router" in main_src

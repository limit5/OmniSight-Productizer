"""OP-2241 -- BI2 on-demand meeting transcript translation contract."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def test_op2241_module_imports() -> None:
    """Router-local response models import cleanly without Postgres."""
    from backend.routers import meeting_translate as r

    assert hasattr(r, "router")
    assert hasattr(r, "translate_meeting")
    result = r.TranslationResult(
        target_lang="ja",
        segments=[
            r.TranslationSegment(
                id="seg-1",
                segment_seq=1,
                source_lang="en",
                text="hello",
                translated_text="konnichiwa",
            )
        ],
        text="konnichiwa",
        model="stub-model",
    )
    assert result.segments[0].translated_text == "konnichiwa"


def test_op2241_router_registered_in_main() -> None:
    """The meeting translation router is wired into backend/main.py."""
    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "from backend.routers import meeting_translate" in main_src
    assert "_meeting_translate_router.router" in main_src


class _FakeConn:
    def __init__(self) -> None:
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock()


def _user(tenant_id: str = "t-op2241") -> SimpleNamespace:
    return SimpleNamespace(id="u-op2241", email="op2241@test", tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_translate_disabled_by_default(monkeypatch) -> None:
    """Flag-off deployments reject before any DB or LLM work."""
    from fastapi import HTTPException
    from backend.routers import meeting_translate as r

    conn = _FakeConn()
    monkeypatch.setattr(r.settings, "meeting_translate_enabled", False)

    with pytest.raises(HTTPException) as exc:
        await r.translate_meeting("mtg-op2241", "ja", user=_user(), conn=conn)

    assert exc.value.status_code == 404
    assert exc.value.detail == "meeting translation disabled"
    conn.fetchrow.assert_not_called()
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_translate_seeded_segments_returns_target_shape(monkeypatch) -> None:
    """Flag-on seeded final segments are translated through the LLM."""
    from backend.routers import meeting_translate as r

    conn = _FakeConn()
    conn.fetchrow.return_value = {"id": "mtg-op2241"}
    conn.fetch.return_value = [
        {
            "id": "seg-1",
            "segment_seq": 1,
            "text": "hello",
            "language": "en",
        },
        {
            "id": "seg-2",
            "segment_seq": 2,
            "text": "ship it",
            "language": "en-US",
        },
    ]
    llm = SimpleNamespace(model_name="stub-translate")
    invoke = Mock(
        return_value=(
            '{"segments": ['
            '{"id": "seg-1", "segment_seq": 1, "translated_text": "hola"},'
            '{"id": "seg-2", "segment_seq": 2, "translated_text": "enviar"}'
            "]}"
        )
    )
    monkeypatch.setattr(r.settings, "meeting_translate_enabled", True)
    monkeypatch.setattr(r, "get_llm", Mock(return_value=llm))
    monkeypatch.setattr(r, "invoke_chat", invoke)

    result = await r.translate_meeting("mtg-op2241", "es", user=_user(), conn=conn)

    assert result.target_lang == "es"
    assert result.model == "stub-translate"
    assert [s.segment_seq for s in result.segments] == [1, 2]
    assert [s.translated_text for s in result.segments] == ["hola", "enviar"]
    assert result.text == "hola\nenviar"
    conn.fetchrow.assert_called_once_with(
        "SELECT id FROM meetings WHERE id = $1 AND tenant_id = $2",
        "mtg-op2241",
        "t-op2241",
    )
    assert "is_final = TRUE" in conn.fetch.call_args.args[0]
    invoke.assert_called_once()


@pytest.mark.asyncio
async def test_translate_skips_same_language_segments(monkeypatch) -> None:
    """Source language matching target is copied without an LLM call."""
    from backend.routers import meeting_translate as r

    conn = _FakeConn()
    conn.fetchrow.return_value = {"id": "mtg-op2241"}
    conn.fetch.return_value = [
        {
            "id": "seg-1",
            "segment_seq": 1,
            "text": "already japanese",
            "language": "ja-JP",
        }
    ]
    monkeypatch.setattr(r.settings, "meeting_translate_enabled", True)
    get_llm = Mock()
    invoke = Mock()
    monkeypatch.setattr(r, "get_llm", get_llm)
    monkeypatch.setattr(r, "invoke_chat", invoke)

    result = await r.translate_meeting("mtg-op2241", "ja", user=_user(), conn=conn)

    assert result.model is None
    assert result.text == "already japanese"
    assert result.segments[0].skipped is True
    assert result.segments[0].translated_text == "already japanese"
    get_llm.assert_not_called()
    invoke.assert_not_called()

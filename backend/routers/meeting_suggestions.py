"""OP-2243 BI4 -- TEXT-only meeting suggestion nudge.

This endpoint reads final transcript segments for a tenant-scoped
meeting and asks the configured LLM for content-derived speaker /
contribution nudges. It deliberately does not inspect audio, DOA, face
tracking, or diarization signals.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend.agents.llm import get_llm
from backend.config import settings
from backend.db_pool import get_conn
from backend.llm_adapter import invoke_chat

router = APIRouter(prefix="/meetings", tags=["meeting-suggestions"])


class Suggestion(BaseModel):
    kind: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class SuggestionsResult(BaseModel):
    suggestions: list[Suggestion]
    model: str


async def _fetch_meeting(
    conn: asyncpg.Connection,
    tenant_id: str,
    meeting_id: str,
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT id FROM meetings WHERE id = $1 AND tenant_id = $2",
        meeting_id,
        tenant_id,
    )


async def _fetch_final_segments(
    conn: asyncpg.Connection,
    tenant_id: str,
    meeting_id: str,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT segment_seq, text, language "
        "FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2 AND is_final = TRUE "
        "ORDER BY segment_seq ASC",
        tenant_id,
        meeting_id,
    )


def _model_name() -> str:
    model = (getattr(settings, "llm_model", "") or "").strip()
    if model:
        return model
    try:
        return settings.get_model_name()
    except Exception:
        return (
            (getattr(settings, "llm_provider", "") or "default").strip()
            or "default"
        )


def _transcript_text(rows: list[asyncpg.Record]) -> str:
    lines: list[str] = []
    for row in rows:
        text = str(row["text"] or "").strip()
        if not text:
            continue
        lines.append(f"{int(row['segment_seq'])}: {text}")
    return "\n".join(lines)


def _parse_suggestions(raw: str) -> list[Suggestion]:
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="suggestions LLM returned invalid JSON",
        ) from exc
    if isinstance(payload, dict):
        payload = payload.get("suggestions", [])
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=502,
            detail="suggestions LLM returned invalid suggestions shape",
        )
    suggestions: list[Suggestion] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        text = str(item.get("text") or "").strip()
        if kind and text:
            suggestions.append(Suggestion(kind=kind, text=text))
    return suggestions


@router.post("/{meeting_id}/suggestions", response_model=SuggestionsResult)
async def suggest_meeting_contributions(
    meeting_id: str,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> SuggestionsResult:
    if not settings.meeting_suggestions_enabled:
        raise HTTPException(status_code=503, detail="meeting suggestions disabled")

    row = await _fetch_meeting(conn, user.tenant_id, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    segments = await _fetch_final_segments(conn, user.tenant_id, meeting_id)
    transcript = _transcript_text(segments)
    if not transcript:
        return SuggestionsResult(suggestions=[], model=_model_name())

    llm = get_llm()
    raw = invoke_chat(
        [
            (
                "system",
                "You generate content-derived meeting contribution nudges. "
                "Use only transcript text. Do not infer acoustic active-speaker, "
                "face tracking, diarization, or audio cues. Return strict JSON "
                "with key suggestions, an array of objects with string kind and text.",
            ),
            (
                "user",
                "Review these final transcript segments and suggest who may need "
                "a follow-up, who spoke little based only on the text, or useful "
                "contribution prompts. Keep each suggestion concise.\n\n"
                f"{transcript}",
            ),
        ],
        llm=llm,
        max_tokens=512,
    )
    if not raw.strip():
        return SuggestionsResult(suggestions=[], model=_model_name())
    return SuggestionsResult(
        suggestions=_parse_suggestions(raw),
        model=_model_name(),
    )

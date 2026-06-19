"""OP-2242 BI3 — structured meeting action-items extraction."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import llm_adapter
from backend.agents.llm import get_llm
from backend.config import settings
from backend.db_pool import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meeting-actions"])


class ActionItem(BaseModel):
    text: str = Field(min_length=1)
    owner: str | None = None
    due: str | None = None


class DiscussionPoint(BaseModel):
    topic: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ActionItemsResult(BaseModel):
    action_items: list[ActionItem] = Field(default_factory=list)
    discussion_points: list[DiscussionPoint] = Field(default_factory=list)
    model: str


class _StructuredExtraction(BaseModel):
    action_items: list[ActionItem] = Field(default_factory=list)
    discussion_points: list[DiscussionPoint] = Field(default_factory=list)


_ACTION_ITEMS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_meeting_action_items",
        "description": (
            "Return structured action items and discussion points extracted "
            "from the meeting transcript."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "text": {"type": "string"},
                            "owner": {"type": ["string", "null"]},
                            "due": {"type": ["string", "null"]},
                        },
                        "required": ["text"],
                    },
                },
                "discussion_points": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "topic": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["topic", "summary"],
                    },
                },
            },
            "required": ["action_items", "discussion_points"],
        },
    },
}


def _enabled() -> bool:
    return bool(getattr(settings, "meeting_action_items_enabled", False))


def _meeting_model_name(llm: object | None) -> str:
    for attr in ("model_name", "model", "model_id"):
        value = getattr(llm, attr, None)
        if value:
            return str(value)
    return settings.llm_model or settings.llm_provider


async def _fetch_final_segment_texts(
    conn: asyncpg.Connection,
    tenant_id: str,
    meeting_id: str,
) -> list[str]:
    row = await conn.fetchrow(
        "SELECT id FROM meetings WHERE id = $1 AND tenant_id = $2",
        meeting_id,
        tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    rows = await conn.fetch(
        "SELECT text FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2 AND is_final = TRUE "
        "ORDER BY segment_seq ASC",
        tenant_id,
        meeting_id,
    )
    return [str(r["text"]) for r in rows]


def _build_messages(transcript_text: str) -> list[tuple[str, str]]:
    return [
        (
            "system",
            "Extract meeting action items and discussion points. "
            "Return only by calling record_meeting_action_items with JSON "
            "that matches the provided schema.",
        ),
        ("user", f"Meeting transcript:\n\n{transcript_text}"),
    ]


@router.post("/{meeting_id}/action-items", response_model=ActionItemsResult)
async def extract_action_items(
    meeting_id: str,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ActionItemsResult:
    """Extract structured action items from final transcript segments."""
    if not _enabled():
        raise HTTPException(status_code=404, detail="meeting action-items disabled")

    texts = await _fetch_final_segment_texts(conn, user.tenant_id, meeting_id)
    if not texts:
        llm = get_llm()
        return ActionItemsResult(
            action_items=[],
            discussion_points=[],
            model=_meeting_model_name(llm),
        )

    llm = get_llm()
    if llm is None:
        raise HTTPException(status_code=503, detail="action-items unavailable: LLM provider not configured")

    # OP-2267: provider exceptions (e.g. ollama daemon down, anthropic 5xx)
    # used to bubble as 500. Catch and translate to 503 so meeting-intelligence
    # endpoints degrade uniformly with BI1 summary. The ollama-specific
    # fallback inside ``llm_adapter.tool_call`` returns an empty tool_calls
    # response in that case (handled by the ``if not response.tool_calls``
    # branch below — also 503).
    try:
        response = llm_adapter.tool_call(
            _build_messages("\n".join(texts)),
            [_ACTION_ITEMS_TOOL],
            llm=llm,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — provider failures bubble as 503
        logger.warning(
            "meeting_actions: LLM tool_call failed for %s: %s",
            meeting_id, exc,
        )
        raise HTTPException(
            status_code=503,
            detail="action-items unavailable: LLM call failed",
        ) from exc

    if not response.tool_calls:
        raise HTTPException(status_code=503, detail="action-items unavailable: llm returned no structured output")

    call = response.tool_calls[0]
    if call.name and call.name != "record_meeting_action_items":
        raise HTTPException(status_code=503, detail="action-items unavailable: llm returned unexpected tool")

    try:
        extracted = _StructuredExtraction.model_validate(call.arguments)
    except Exception as exc:  # noqa: BLE001 - normalize provider schema drift.
        raise HTTPException(status_code=503, detail="action-items unavailable: invalid structured output") from exc

    return ActionItemsResult(
        action_items=extracted.action_items,
        discussion_points=extracted.discussion_points,
        model=_meeting_model_name(llm),
    )

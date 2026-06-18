"""OP-2240 BI1 — meeting SUMMARY over the final transcript text (LLM).

On-demand only, disabled-by-default: ``POST /meetings/{meeting_id}/summary``
reads the final segments stored by the OP-2238 BI0 ingest API
(``transcript_segments`` from alembic 0249) for the calling tenant,
sends them through ``backend.llm_adapter.invoke_chat`` and returns a
structured :class:`MeetingSummary` payload.

Design anchors
--------------
* No new table, no migration — siblings BI2-BI4 run in parallel; adding
  any 0250+ migration collides with them. This router owns ONE
  endpoint and ONE Pydantic shape; nothing else moves.
* Auth/tenant via :func:`backend.auth.require_operator` +
  ``user.tenant_id``, mirroring the transcripts router.
* DB via :func:`backend.db_pool.get_conn`; SELECTs are tenant-scoped
  so the 404 path for "meeting not found" cannot leak a cross-tenant
  row.
* LLM via :func:`backend.llm_adapter.invoke_chat` (which routes
  through :func:`backend.agents.llm.get_llm` and the failover
  chain, with token / cost tracking auto-wired via
  ``TokenTrackingCallback``).
* Disabled-by-default flag ``settings.meeting_summary_enabled`` —
  off-state returns ``404 {"detail": "feature not enabled"}`` (NOT
  403; the existence of the endpoint must look identical to a
  missing meeting so operators that have never opted in don't leak
  the upcoming BI1 surface to clients).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend.config import settings
from backend.db_pool import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meeting-summary"])


# ──────────────────────────────────────────────────────────────────
# Output schema (lives in this router file per ticket convention —
# editing backend/models.py would collide with sibling BI2-4 work).
# ──────────────────────────────────────────────────────────────────


class MeetingSummary(BaseModel):
    """Structured summary returned by ``POST /meetings/{id}/summary``."""
    meeting_id: str
    tldr: str
    bullet_points: list[str] = Field(default_factory=list)
    generated_at: float
    model: str
    segment_count: int = 0


# Bounded so a 5-hour meeting cannot push a 200K-token prompt at the
# provider — the LLM-tier price ceiling we promised the BI design doc
# §6 was "BI1 prompts stay under a few thousand final segments". The
# truncation strategy is "keep the head", matching how meeting recaps
# are typically about the *opening framing + key decisions*; if the
# tail truly mattered, an operator would run summary on a sliced
# session instead.
_MAX_PROMPT_SEGMENTS = 2000
_MAX_PROMPT_CHARS = 32_000


_SYSTEM_PROMPT = (
    "You summarise meeting transcripts. Reply with STRICT JSON only — "
    'no prose around it — matching this schema: '
    '{"tldr": "<one-paragraph summary>", '
    '"bullet_points": ["<key takeaway>", "..."]}'
    " Use 3-7 bullet points. Keep the tldr under 600 characters."
)


# ──────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────


async def _fetch_meeting(
    conn: asyncpg.Connection, tenant_id: str, meeting_id: str,
) -> Optional[asyncpg.Record]:
    return await conn.fetchrow(
        "SELECT id, tenant_id, title, status FROM meetings "
        "WHERE id = $1 AND tenant_id = $2",
        meeting_id, tenant_id,
    )


async def _fetch_final_segments(
    conn: asyncpg.Connection, tenant_id: str, meeting_id: str,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT segment_seq, text FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2 AND is_final = TRUE "
        "ORDER BY segment_seq ASC",
        tenant_id, meeting_id,
    )


def _build_transcript_text(segments: list[Any]) -> tuple[str, int]:
    """Concatenate ``text`` from each segment with newlines.

    Returns ``(joined_text, segment_count_used)``. Truncates by both
    segment-count and total-char budget — whichever bites first wins.
    """
    used = 0
    parts: list[str] = []
    total_chars = 0
    for row in segments[:_MAX_PROMPT_SEGMENTS]:
        text = (row["text"] or "").strip()
        if not text:
            continue
        if total_chars + len(text) + 1 > _MAX_PROMPT_CHARS:
            break
        parts.append(text)
        total_chars += len(text) + 1
        used += 1
    return "\n".join(parts), used


# ──────────────────────────────────────────────────────────────────
# LLM helpers
# ──────────────────────────────────────────────────────────────────


def _parse_llm_summary(raw: str) -> tuple[str, list[str]]:
    """Parse the LLM reply into ``(tldr, bullet_points)``.

    The system prompt asks for strict JSON, but providers regularly
    wrap it in markdown fences or trailing prose; this parser is
    deliberately forgiving — extract the first JSON object, fall back
    to a "first line is tldr, bullets are dash-prefixed lines"
    heuristic if JSON parsing fails entirely. An empty LLM reply
    (provider unavailable) surfaces as HTTPException so callers see a
    clean 503 rather than an empty payload that *looks* successful.
    """
    if not raw or not raw.strip():
        raise HTTPException(
            status_code=503,
            detail="summary unavailable: LLM returned empty response",
        )

    cleaned = raw.strip()
    # Strip markdown JSON fence if present.
    fence_match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL,
    )
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        # Pull the first {...} block out of the reply.
        brace_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if brace_match:
            cleaned = brace_match.group(0)

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            tldr = str(obj.get("tldr", "")).strip()
            bullets_raw = obj.get("bullet_points") or []
            if isinstance(bullets_raw, list):
                bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
            else:
                bullets = []
            if tldr or bullets:
                return tldr, bullets
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Heuristic fallback — first non-empty line is tldr; dash- /
    # bullet-prefixed lines become bullet points.
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    tldr = lines[0] if lines else ""
    bullets: list[str] = []
    for ln in lines[1:]:
        if ln.startswith(("-", "*", "•")):
            bullets.append(ln.lstrip("-*• ").strip())
    return tldr, bullets


def _resolve_model_label() -> str:
    """Best-effort label for the LLM that produced the reply.

    The adapter doesn't surface a "what did you actually use" signal,
    so we report the configured primary (provider:model) — operators
    asking "what billed this" cross-reference the LLM cost log. When
    settings aren't loaded yet (defensive), we report ``unknown``.
    """
    try:
        provider = getattr(settings, "llm_provider", "") or "unknown"
        model = getattr(settings, "llm_model", "") or ""
        if model:
            return f"{provider}:{model}"
        return provider
    except Exception:  # noqa: BLE001 — defensive, never block summary
        return "unknown"


# ──────────────────────────────────────────────────────────────────
# Public endpoint
# ──────────────────────────────────────────────────────────────────


@router.post("/{meeting_id}/summary", response_model=MeetingSummary)
async def summarise_meeting(
    meeting_id: str,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> MeetingSummary:
    """Generate a structured summary over the meeting's final segments.

    Failure modes:

      * ``settings.meeting_summary_enabled`` False ⇒ 404 ``feature not
        enabled`` (looks identical to "meeting not found" so a
        not-opted-in operator never surfaces the BI1 endpoint).
      * Meeting absent (or in another tenant) ⇒ 404 ``meeting not
        found``.
      * No final segments yet ⇒ 409 ``no final segments`` — the LLM
        would otherwise be asked to summarise the empty string.
      * LLM returns empty (no provider configured / circuit open) ⇒
        503 ``summary unavailable``.
    """
    if not getattr(settings, "meeting_summary_enabled", False):
        # Off-by-default — same 404 shape as "meeting not found" so
        # the endpoint's presence stays invisible to non-opted-in
        # tenants.
        raise HTTPException(
            status_code=404, detail="feature not enabled",
        )

    row = await _fetch_meeting(conn, user.tenant_id, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    segments = await _fetch_final_segments(
        conn, user.tenant_id, meeting_id,
    )
    if not segments:
        raise HTTPException(
            status_code=409, detail="no final segments to summarise",
        )

    transcript_text, used = _build_transcript_text(segments)
    if not transcript_text:
        raise HTTPException(
            status_code=409, detail="no final segments to summarise",
        )

    # Lazy import — keeps the router importable in unit tests that
    # never touch the LLM path (and avoids paying the langchain
    # import cost at backend boot).
    from backend.llm_adapter import HumanMessage, SystemMessage, invoke_chat

    title_line = (
        f"Meeting title: {row['title']}\n" if row["title"] else ""
    )
    user_prompt = (
        f"{title_line}"
        f"Final transcript ({used} segments):\n"
        f"---\n{transcript_text}\n---\n"
        "Return ONLY the JSON object — no fences, no commentary."
    )

    try:
        raw_reply = invoke_chat(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ],
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — provider failures bubble as 503
        logger.warning(
            "meeting_summary: LLM invoke failed for %s: %s",
            meeting_id, exc,
        )
        raise HTTPException(
            status_code=503,
            detail="summary unavailable: LLM call failed",
        ) from exc

    tldr, bullet_points = _parse_llm_summary(raw_reply)

    return MeetingSummary(
        meeting_id=meeting_id,
        tldr=tldr,
        bullet_points=bullet_points,
        generated_at=time.time(),
        model=_resolve_model_label(),
        segment_count=used,
    )

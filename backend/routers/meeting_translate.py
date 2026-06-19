"""OP-2241 BI2 -- on-demand meeting transcript translation.

Reads final transcript segments for one tenant-scoped meeting and asks the
configured LLM to translate only segments whose language differs from the
requested BCP-47 target. Disabled by default via
``OMNISIGHT_MEETING_TRANSLATE_ENABLED``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend import auth as _au
from backend.agents.llm import get_llm
from backend.config import settings
from backend.db_pool import get_conn
from backend.llm_adapter import invoke_chat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meeting-translate"])


class TranslationSegment(BaseModel):
    """Single translated transcript segment projection."""

    id: str
    segment_seq: int
    source_lang: Optional[str] = None
    text: str
    translated_text: str
    skipped: bool = False


class TranslationResult(BaseModel):
    """On-demand translation response."""

    target_lang: str
    segments: list[TranslationSegment]
    text: str
    model: Optional[str] = None


def _normalise_lang(lang: str | None) -> str:
    return (lang or "").strip().lower().replace("_", "-")


def _same_language(segment_lang: str | None, target_lang: str) -> bool:
    source = _normalise_lang(segment_lang)
    target = _normalise_lang(target_lang)
    return bool(
        source
        and (
            source == target
            or source.split("-", 1)[0] == target.split("-", 1)[0]
        )
    )


def _model_name(llm: Any) -> Optional[str]:
    for attr in ("model_name", "model", "model_id"):
        value = getattr(llm, attr, None)
        if value:
            return str(value)
    return None


def _translation_prompt(target_lang: str, rows: list[asyncpg.Record]) -> list[tuple[str, str]]:
    payload = [
        {
            "id": r["id"],
            "segment_seq": int(r["segment_seq"]),
            "source_lang": r["language"],
            "text": r["text"],
        }
        for r in rows
    ]
    return [
        (
            "system",
            "Translate meeting transcript segments. Return only JSON with a "
            '"segments" array. Each item must include id, segment_seq, and '
            "translated_text. Do not summarize or add commentary.",
        ),
        (
            "user",
            "Target language: "
            f"{target_lang}\nSegments JSON:\n{json.dumps(payload, ensure_ascii=False)}",
        ),
    ]


def _parse_translations(raw: str, rows: list[asyncpg.Record]) -> dict[str, str]:
    # OP-2267: empty / malformed / partial provider replies degrade to 503
    # "translation unavailable" to match the BI1 summary contract (the four
    # meeting-intelligence endpoints must fail uniformly when the LLM is
    # unhealthy — anything 5xx other than 503 trips paging alerts).
    if not raw.strip():
        raise HTTPException(status_code=503, detail="translation unavailable: llm returned empty response")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail="translation unavailable: llm returned invalid json",
        ) from exc
    items = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(status_code=503, detail="translation unavailable: llm returned invalid shape")
    by_id: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        seg_id = str(item.get("id") or "")
        translated = item.get("translated_text")
        if seg_id and isinstance(translated, str):
            by_id[seg_id] = translated
    missing = [r["id"] for r in rows if r["id"] not in by_id]
    if missing:
        raise HTTPException(status_code=503, detail="translation unavailable: llm omitted segments")
    return by_id


def _segment_result(
    row: asyncpg.Record,
    target_lang: str,
    translations: dict[str, str],
) -> TranslationSegment:
    skipped = _same_language(row["language"], target_lang)
    return TranslationSegment(
        id=row["id"],
        segment_seq=int(row["segment_seq"]),
        source_lang=row["language"],
        text=row["text"],
        translated_text=(row["text"] if skipped else translations[row["id"]]),
        skipped=skipped,
    )


@router.post("/{meeting_id}/translate", response_model=TranslationResult)
async def translate_meeting(
    meeting_id: str,
    target_lang: str = Query(..., min_length=2, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"),
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> TranslationResult:
    """Translate final transcript segments for one tenant-scoped meeting."""
    if not settings.meeting_translate_enabled:
        raise HTTPException(status_code=404, detail="meeting translation disabled")

    target = target_lang.strip()
    row = await conn.fetchrow(
        "SELECT id FROM meetings WHERE id = $1 AND tenant_id = $2",
        meeting_id, user.tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")

    rows = await conn.fetch(
        "SELECT id, segment_seq, text, language "
        "FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2 AND is_final = TRUE "
        "ORDER BY segment_seq ASC",
        user.tenant_id, meeting_id,
    )
    llm_rows = [r for r in rows if not _same_language(r["language"], target)]
    translations: dict[str, str] = {}
    model: Optional[str] = None
    if llm_rows:
        # OP-2267: LLM-unavailable / provider-exception must surface as 503
        # (matching BI1 summary), not a 500/502 paged-alert. ``get_llm()``
        # returns ``None`` when no provider is configured; the adapter would
        # then return "" and the parser would already raise 503 — but a
        # configured-but-broken provider (e.g. ollama daemon refusing
        # connections) raises from invoke_chat, so wrap it here.
        llm = get_llm()
        if llm is None:
            raise HTTPException(
                status_code=503,
                detail="translation unavailable: LLM provider not configured",
            )
        model = _model_name(llm)
        try:
            raw = invoke_chat(_translation_prompt(target, llm_rows), llm=llm)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — provider failures bubble as 503
            logger.warning(
                "meeting_translate: LLM invoke failed for %s: %s",
                meeting_id, exc,
            )
            raise HTTPException(
                status_code=503,
                detail="translation unavailable: LLM call failed",
            ) from exc
        translations = _parse_translations(raw, llm_rows)

    segments = [_segment_result(r, target, translations) for r in rows]
    return TranslationResult(
        target_lang=target,
        segments=segments,
        text="\n".join(seg.translated_text for seg in segments),
        model=model,
    )

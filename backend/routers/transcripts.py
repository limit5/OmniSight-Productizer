"""OP-2238 BI0 — TEXT-only transcript ingest API.

On-board MI lane pushes ASR segments here; BI1-4 (summary / translation
/ action-items / suggestions) read the segment list and meeting envelope
back. **TEXT-only**: no audio field, no audio endpoint. Summaries /
translation / extraction stay strictly out of this router (those are
BI1-4 territory).

The contract (see ticket description + alembic 0249):

* ``POST /meetings/{meeting_id}/segments`` — batch ingest, idempotent.
* ``GET  /meetings/{meeting_id}/segments`` — ordered read (segment_seq
  ascending), tenant-scoped.
* ``GET  /meetings/{meeting_id}`` — envelope (counts / languages /
  first-last segment timestamps).
* ``POST /meetings`` — explicit open. Idempotent under
  ``ON CONFLICT (id) DO NOTHING``. The ingest endpoint also
  auto-creates the meeting row on first segment arrival, so callers
  may skip this endpoint entirely.

Idempotency / replay invariants
-------------------------------
* INSERT ... ON CONFLICT ``(tenant_id, meeting_id, session_id,
  segment_seq)`` DO UPDATE SET ... WHERE NOT
  ``transcript_segments.is_final`` — a final supersedes a partial in
  place; a duplicate (same is_final state, same text) is a no-op.
* A partial arriving AFTER a final (same key) is rejected with reason
  ``final_superseded`` — we never downgrade.
* Reads are ordered by ``segment_seq`` so an out-of-order arrival is
  still consistent on the way out.
* Optional ``Idempotency-Key`` header lets a caller retry the whole
  batch; cached response is returned on the second hit.

Cross-tenant invariant
----------------------
A meeting row lives in exactly one tenant. A read or write from a
different tenant resolves to 404 (NOT 403 — the existence of the
meeting must not leak across tenants).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from backend import audit
from backend import auth as _au
from backend import transcripts_retention as _retention
from backend.db_pool import get_conn
from backend.models import (
    MeetingEnvelope,
    OpenMeetingRequest,
    TranscriptIngestRejection,
    TranscriptIngestRequest,
    TranscriptIngestResult,
    TranscriptSegmentOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["transcripts"])


# Whole-batch idempotency cache. The on-board MI lane retries the same
# POST body with the same Idempotency-Key on transient network failures;
# we return the cached response rather than re-running the batch.
#
# Module-global state audit: a per-worker dict, bounded by size to a few
# thousand entries (FIFO eviction). The correctness story is owned by
# the PG UNIQUE constraint — the in-process cache is a latency /
# determinism optimisation. A miss caused by a cold worker still ends
# up with the same DB state (the UNIQUE-based dedup still fires) and
# the same response body shape (recomputed from the same inputs).
_IDEM_MAX = 4096
_idem_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
_idem_order: list[tuple[str, str, str]] = []


def _cache_put(key: tuple[str, str, str], value: dict[str, Any]) -> None:
    if key in _idem_cache:
        return
    if len(_idem_order) >= _IDEM_MAX:
        evicted = _idem_order.pop(0)
        _idem_cache.pop(evicted, None)
    _idem_cache[key] = value
    _idem_order.append(key)


def _new_meeting_id() -> str:
    return f"mtg-{uuid.uuid4().hex[:12]}"


def _new_segment_id() -> str:
    return f"seg-{uuid.uuid4().hex[:12]}"


async def _fetch_meeting(
    conn: asyncpg.Connection, tenant_id: str, meeting_id: str,
) -> Optional[asyncpg.Record]:
    """Tenant-scoped probe; returns the row or ``None``.

    Used by every endpoint that takes ``{meeting_id}`` — a None return
    is the 404 path (the existence of a cross-tenant meeting must NOT
    leak; we treat it as not-found).
    """
    return await conn.fetchrow(
        "SELECT id, tenant_id, title, status, created_at, updated_at "
        "FROM meetings WHERE id = $1 AND tenant_id = $2",
        meeting_id, tenant_id,
    )


async def _ensure_meeting_row(
    conn: asyncpg.Connection, tenant_id: str, meeting_id: str,
    *, actor: str,
) -> str:
    """Auto-create a meetings row on first segment arrival.

    Returns one of:
      ``"existing"``   — row already lives in this tenant.
      ``"created"``    — row didn't exist anywhere; we inserted it.

    Raises ``HTTPException(404)`` when the meeting_id already exists in
    a *different* tenant (the existence must not leak; same 404 as a
    bare ``GET`` would return).
    """
    row = await conn.fetchrow(
        "SELECT tenant_id FROM meetings WHERE id = $1", meeting_id,
    )
    if row is not None:
        if row["tenant_id"] != tenant_id:
            raise HTTPException(status_code=404, detail="meeting not found")
        return "existing"
    now = time.time()
    # OP-2239 BI0b -- stamp the retention deadline at create time so the
    # row carries its own expiry independent of later config changes.
    retention_until = _retention.compute_retention_until(now)
    inserted = await conn.fetchrow(
        "INSERT INTO meetings (id, tenant_id, status, retention_until, "
        "created_at, updated_at) "
        "VALUES ($1, $2, 'open', $3, $4, $4) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        meeting_id, tenant_id, retention_until, now,
    )
    if inserted is None:
        # Lost the race against a concurrent writer — re-probe to confirm
        # the winner sits in our tenant.
        rerow = await conn.fetchrow(
            "SELECT tenant_id FROM meetings WHERE id = $1", meeting_id,
        )
        if rerow is None or rerow["tenant_id"] != tenant_id:
            raise HTTPException(status_code=404, detail="meeting not found")
        return "existing"
    try:
        await audit.log(
            "meeting.create", "meeting", meeting_id,
            before=None,
            after={"id": meeting_id, "tenant_id": tenant_id, "auto": True},
            actor=actor, conn=conn,
        )
    except Exception as exc:
        logger.debug("audit.log meeting.create failed: %s", exc)
    return "created"


@router.post("", status_code=201)
async def open_meeting(
    body: OpenMeetingRequest,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> JSONResponse:
    """Explicit meeting open. Idempotent on repeated ``id``.

    Returns 201 on first open; 200 with the existing row on a repeated
    open of the same id (callers that pass a deterministic id can use
    this to convert "open if not exists" into a single round-trip).
    Cross-tenant collision on ``id`` => 409.
    """
    tenant_id = user.tenant_id
    meeting_id = (body.id or "").strip() or _new_meeting_id()
    now = time.time()
    # OP-2239 BI0b -- stamp the retention deadline at create time so the
    # row carries its own expiry independent of later config changes.
    retention_until = _retention.compute_retention_until(now)
    inserted = await conn.fetchrow(
        "INSERT INTO meetings (id, tenant_id, title, status, "
        "retention_until, created_at, updated_at) "
        "VALUES ($1, $2, $3, 'open', $4, $5, $5) "
        "ON CONFLICT (id) DO NOTHING "
        "RETURNING id, tenant_id, title, status, created_at, updated_at",
        meeting_id, tenant_id, body.title, retention_until, now,
    )
    if inserted is None:
        existing = await conn.fetchrow(
            "SELECT id, tenant_id, title, status, created_at, updated_at "
            "FROM meetings WHERE id = $1", meeting_id,
        )
        if existing is None or existing["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=409,
                detail="meeting id collision — pick a different id",
            )
        return JSONResponse(status_code=200, content=_meeting_row_dict(existing))
    try:
        await audit.log(
            "meeting.create", "meeting", meeting_id,
            before=None,
            after={
                "id": meeting_id, "tenant_id": tenant_id,
                "title": body.title, "auto": False,
            },
            actor=user.email or user.id, conn=conn,
        )
    except Exception as exc:
        logger.debug("audit.log meeting.create failed: %s", exc)
    return JSONResponse(status_code=201, content=_meeting_row_dict(inserted))


def _meeting_row_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/{meeting_id}", response_model=MeetingEnvelope)
async def get_meeting_envelope(
    meeting_id: str,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> MeetingEnvelope:
    """Aggregate envelope: row + segment counts / languages / ts span."""
    row = await _fetch_meeting(conn, user.tenant_id, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    agg = await conn.fetchrow(
        "SELECT COUNT(*) AS segment_count, "
        "       SUM(CASE WHEN is_final THEN 1 ELSE 0 END) AS final_count, "
        "       MIN(start_ms) AS first_ts, "
        "       MAX(end_ms) AS last_ts "
        "FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2",
        user.tenant_id, meeting_id,
    )
    langs = await conn.fetch(
        "SELECT DISTINCT language FROM transcript_segments "
        "WHERE tenant_id = $1 AND meeting_id = $2 AND language IS NOT NULL "
        "ORDER BY language",
        user.tenant_id, meeting_id,
    )
    seg_count = int(agg["segment_count"] or 0) if agg else 0
    final_count = int(agg["final_count"] or 0) if agg else 0
    return MeetingEnvelope(
        id=row["id"],
        tenant_id=row["tenant_id"],
        title=row["title"],
        status=row["status"],
        segment_count=seg_count,
        final_segment_count=final_count,
        languages=[r["language"] for r in langs],
        first_segment_ts_ms=(int(agg["first_ts"]) if agg and agg["first_ts"] is not None else None),
        last_segment_ts_ms=(int(agg["last_ts"]) if agg and agg["last_ts"] is not None else None),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get(
    "/{meeting_id}/segments",
    response_model=list[TranscriptSegmentOut],
)
async def list_segments(
    meeting_id: str,
    since_seq: Optional[int] = None,
    final_only: bool = False,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[TranscriptSegmentOut]:
    """Ordered read (ASC by segment_seq). Tenant-scoped 404 path.

    ``since_seq`` is exclusive (only rows strictly greater than the
    given seq), matching the typical "incremental tail since I last
    polled" caller. ``final_only`` filters to finalised segments, which
    is what BI1-4 want when sizing a summarisation pass.
    """
    row = await _fetch_meeting(conn, user.tenant_id, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="meeting not found")
    where = ["tenant_id = $1", "meeting_id = $2"]
    params: list[Any] = [user.tenant_id, meeting_id]
    if since_seq is not None:
        where.append(f"segment_seq > ${len(params) + 1}")
        params.append(int(since_seq))
    if final_only:
        where.append("is_final = TRUE")
    sql = (
        "SELECT id, meeting_id, session_id, segment_seq, text, is_final, "
        "       source, start_ms, end_ms, language, confidence "
        "FROM transcript_segments "
        "WHERE " + " AND ".join(where) + " ORDER BY segment_seq ASC"
    )
    rows = await conn.fetch(sql, *params)
    return [
        TranscriptSegmentOut(
            id=r["id"],
            meeting_id=r["meeting_id"],
            session_id=r["session_id"],
            segment_seq=int(r["segment_seq"]),
            text=r["text"],
            is_final=bool(r["is_final"]),
            source=r["source"],
            start_ms=(int(r["start_ms"]) if r["start_ms"] is not None else None),
            end_ms=(int(r["end_ms"]) if r["end_ms"] is not None else None),
            language=r["language"],
            confidence=(float(r["confidence"]) if r["confidence"] is not None else None),
        )
        for r in rows
    ]


@router.post(
    "/{meeting_id}/segments",
    status_code=201,
    response_model=TranscriptIngestResult,
)
async def ingest_segments(
    meeting_id: str,
    body: TranscriptIngestRequest,
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> TranscriptIngestResult:
    """Idempotent batch ingest.

    Semantics per segment within the batch:

      * **dedup** — same ``(tenant_id, meeting_id, session_id,
        segment_seq)``, both stored row and incoming row are already
        finalised (or both are partial with identical text): no-op.
      * **supersede** — same key, stored row is a partial and incoming
        row is final: in-place UPDATE keeps the row id stable, flips
        ``is_final`` and refreshes ``text`` / ``end_ms`` / ``confidence``.
      * **reject** — same key, stored row is final and incoming is
        partial: never downgrade. ``rejected[]`` carries the seq +
        reason ``"final_superseded"``.
      * **accept** — first arrival for that key.

    Cross-tenant meeting_id => 404. Meeting auto-created on first
    segment arrival (router picks auto-create; the explicit POST
    /meetings remains available for callers that want it).
    """
    tenant_id = user.tenant_id

    idem_key: Optional[tuple[str, str, str]] = None
    if idempotency_key:
        idem_key = (tenant_id, meeting_id, idempotency_key)
        cached = _idem_cache.get(idem_key)
        if cached is not None:
            return TranscriptIngestResult(**cached)

    await _ensure_meeting_row(
        conn, tenant_id, meeting_id, actor=user.email or user.id,
    )

    accepted = 0
    deduped = 0
    superseded = 0
    rejected: list[TranscriptIngestRejection] = []

    now = time.time()
    for seg in body.segments:
        existing = await conn.fetchrow(
            "SELECT id, is_final, text FROM transcript_segments "
            "WHERE tenant_id = $1 AND meeting_id = $2 "
            "AND session_id = $3 AND segment_seq = $4",
            tenant_id, meeting_id, body.session_id, seg.segment_seq,
        )
        if existing is None:
            new_id = _new_segment_id()
            await conn.execute(
                "INSERT INTO transcript_segments ("
                "  id, tenant_id, meeting_id, session_id, segment_seq, "
                "  start_ms, end_ms, text, language, confidence, is_final, "
                "  source, created_at, updated_at"
                ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13)",
                new_id, tenant_id, meeting_id, body.session_id, seg.segment_seq,
                seg.start_ms, seg.end_ms, seg.text, seg.language,
                seg.confidence, seg.is_final, seg.source, now,
            )
            accepted += 1
            try:
                await audit.log(
                    "transcript_segment.insert", "transcript_segment", new_id,
                    before=None,
                    after={
                        "tenant_id": tenant_id, "meeting_id": meeting_id,
                        "session_id": body.session_id,
                        "segment_seq": seg.segment_seq,
                        "is_final": seg.is_final, "source": seg.source,
                    },
                    actor=user.email or user.id, conn=conn,
                )
            except Exception as exc:
                logger.debug("audit.log segment.insert failed: %s", exc)
            continue
        if existing["is_final"]:
            if seg.is_final and existing["text"] == seg.text:
                deduped += 1
            elif not seg.is_final:
                rejected.append(TranscriptIngestRejection(
                    segment_seq=seg.segment_seq, reason="final_superseded",
                ))
            else:
                # final-on-final with different text: keep stored final,
                # treat as deduped (we do not rewrite a final we already
                # accepted -- BI1-4 read the first-stored final).
                deduped += 1
            continue
        # existing is partial.
        if seg.is_final:
            await conn.execute(
                "UPDATE transcript_segments SET "
                "  text = $1, is_final = TRUE, "
                "  start_ms = COALESCE($2, start_ms), "
                "  end_ms = COALESCE($3, end_ms), "
                "  language = COALESCE($4, language), "
                "  confidence = COALESCE($5, confidence), "
                "  updated_at = $6 "
                "WHERE id = $7",
                seg.text, seg.start_ms, seg.end_ms, seg.language,
                seg.confidence, now, existing["id"],
            )
            superseded += 1
            try:
                await audit.log(
                    "transcript_segment.supersede", "transcript_segment",
                    existing["id"],
                    before={"is_final": False, "text": existing["text"]},
                    after={
                        "is_final": True, "text": seg.text,
                        "segment_seq": seg.segment_seq,
                    },
                    actor=user.email or user.id, conn=conn,
                )
            except Exception as exc:
                logger.debug("audit.log segment.supersede failed: %s", exc)
            continue
        # partial-on-partial: dedupe-by-text is the simplest contract.
        if existing["text"] == seg.text:
            deduped += 1
        else:
            await conn.execute(
                "UPDATE transcript_segments SET "
                "  text = $1, "
                "  start_ms = COALESCE($2, start_ms), "
                "  end_ms = COALESCE($3, end_ms), "
                "  language = COALESCE($4, language), "
                "  confidence = COALESCE($5, confidence), "
                "  updated_at = $6 "
                "WHERE id = $7",
                seg.text, seg.start_ms, seg.end_ms, seg.language,
                seg.confidence, now, existing["id"],
            )
            superseded += 1
            try:
                await audit.log(
                    "transcript_segment.supersede",
                    "transcript_segment", existing["id"],
                    before={"is_final": False, "text": existing["text"]},
                    after={"is_final": False, "text": seg.text},
                    actor=user.email or user.id, conn=conn,
                )
            except Exception as exc:
                logger.debug("audit.log segment.supersede failed: %s", exc)

    if accepted or superseded:
        await conn.execute(
            "UPDATE meetings SET updated_at = $1 WHERE id = $2 AND tenant_id = $3",
            now, meeting_id, tenant_id,
        )

    result = TranscriptIngestResult(
        accepted=accepted, deduped=deduped, superseded=superseded,
        rejected=rejected,
    )

    if idem_key is not None:
        # Cache the JSON-serialisable shape so a retry returns the same
        # body bytes the first call did.
        _cache_put(idem_key, json.loads(result.model_dump_json()))

    return result

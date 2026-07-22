"""γ-0 leg-3 — Claude-memory bulk ingest (quarantine-only, service bearer).

The governed WRITE path for Claude's file store. Files remain the write
surface (the harness auto-memory writer is unredirectable); this endpoint
INGESTS them — every landing is QUARANTINED; publication is the γ-1 human
gate. Mirrors the ``/memories/propose`` discipline: server-built identity,
per-item 422-style gate reasons, no client-supplied scope.

Identity: an ``omni_`` api-key bearer minted with ``scopes=["claude-memories"]``
(NEVER ``["*"]``) — the K6 middleware pre-gates the path; the handler builds
``for_service("claude-code", authorization_source="api_key")``. The kernel
invariant is untouched: rows never authorize anything.

Upsert semantics (wiring-audit F2/F4 + integrity-audit MAJOR-4):
  * slug = filename stem (client sends it; server validates shape) —
    ``name`` frontmatter is NEVER identity.
  * new slug → version rev=1 + head(state=quarantined) + event.
  * same body_sha256 → ``unchanged`` (touch last_seen_at only).
  * changed body → NEW version rev+1, head_version_id advances,
    ``published_version_id`` and ``state`` are NEVER touched here — an
    edit to a published slug lands as a quarantined revision pending
    re-publish (published revisions are immutable; the ledger triggers
    enforce it).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend import auth as _au

_log = logging.getLogger(__name__)

router = APIRouter(tags=["claude-memories"])

_SLUG_RE = re.compile(r"^[a-z0-9_]{1,120}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MEM_TYPES = {"project", "feedback", "reference", "user"}
_MAX_BATCH = 32
_MAX_BODY_B = 1_048_576
_PROJECT_ID = "omnisight-productizer"  # server-set; never client-supplied


class IngestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    description: str = ""
    mem_type: str
    body: str
    links: list[str] = Field(default_factory=list)
    origin_session_id: str | None = None
    rank: int | None = None
    hook: str | None = None
    last_seen_iso: str | None = None


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[IngestItem]


def _reject(reasons: list[str], slug: str) -> dict[str, Any]:
    return {"slug": slug, "action": "rejected", "reasons": reasons}


async def _ingest_one(conn, item: IngestItem, *, actor: str) -> dict[str, Any]:
    reasons: list[str] = []
    if not _SLUG_RE.match(item.slug):
        reasons.append("bad_slug")
    if item.mem_type not in _MEM_TYPES:
        reasons.append("bad_mem_type")
    if not item.title.strip():
        reasons.append("empty_title")
    if not item.body.strip():
        reasons.append("empty_body")
    if len(item.body.encode("utf-8", "ignore")) > _MAX_BODY_B:
        reasons.append("oversize_body_hard")
    if reasons:
        return _reject(reasons, item.slug)

    sha = hashlib.sha256(item.body.encode("utf-8")).hexdigest()
    assert _SHA_RE.match(sha)
    head = await conn.fetchrow(
        "SELECT s.head_version_id, s.state, v.body_sha256 "
        "FROM claude_memory_state s "
        "JOIN claude_memory_versions v ON v.id = s.head_version_id "
        "WHERE s.project_id = $1 AND s.slug = $2 FOR UPDATE OF s",
        _PROJECT_ID, item.slug,
    )
    now_touch = (
        "UPDATE claude_memory_state SET last_seen_at = now(), "
        "rank = COALESCE($3, rank), hook = COALESCE($4, hook), "
        "updated_at = now() WHERE project_id = $1 AND slug = $2"
    )
    if head is not None and head["body_sha256"] == sha:
        await conn.execute(now_touch, _PROJECT_ID, item.slug, item.rank, item.hook)
        return {"slug": item.slug, "action": "unchanged", "reasons": []}

    version_id = str(uuid.uuid4())
    if head is None:
        revision = 1
    else:
        revision = await conn.fetchval(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM claude_memory_versions "
            "WHERE project_id = $1 AND slug = $2",
            _PROJECT_ID, item.slug,
        )
    await conn.execute(
        "INSERT INTO claude_memory_versions "
        "(id, project_id, slug, revision, title, description, mem_type, "
        " body, body_sha256, links, origin_session_id, created_by) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
        version_id, _PROJECT_ID, item.slug, revision, item.title.strip(),
        item.description, item.mem_type, item.body, sha,
        json.dumps(item.links), item.origin_session_id, actor,
    )
    if head is None:
        await conn.execute(
            "INSERT INTO claude_memory_state "
            "(project_id, slug, head_version_id, state, rank, hook, last_seen_at) "
            "VALUES ($1, $2, $3, 'quarantined', $4, $5, now())",
            _PROJECT_ID, item.slug, version_id, item.rank, item.hook,
        )
        action = "created"
    else:
        # Head advances; state/published_version UNTOUCHED (a published
        # slug's edit awaits re-publish — integrity-audit MAJOR-4).
        await conn.execute(
            "UPDATE claude_memory_state SET head_version_id = $3, "
            "rank = COALESCE($4, rank), hook = COALESCE($5, hook), "
            "last_seen_at = now(), updated_at = now() "
            "WHERE project_id = $1 AND slug = $2",
            _PROJECT_ID, item.slug, version_id, item.rank, item.hook,
        )
        action = "revised"
    await conn.execute(
        "INSERT INTO claude_memory_transition_events "
        "(id, project_id, slug, version_id, from_state, to_state, actor, reason) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        str(uuid.uuid4()), _PROJECT_ID, item.slug, version_id,
        (head or {}).get("state"), "quarantined", actor,
        f"ingest:{action}:rev{revision}",
    )
    return {"slug": item.slug, "action": action, "reasons": []}


@router.post("/claude-memories/ingest")
async def ingest_claude_memories(
    batch: IngestBatch,
    user: _au.User = Depends(_au.current_user),
) -> dict[str, Any]:
    if len(batch.items) > _MAX_BATCH:
        raise HTTPException(status_code=422, detail=f"batch>{_MAX_BATCH}")
    from backend.db_pool import get_pool  # lazy

    actor = f"claude-code:{user.id}"
    results: list[dict[str, Any]] = []
    async with get_pool().acquire() as conn:
        for item in batch.items:
            try:
                async with conn.transaction():
                    results.append(await _ingest_one(conn, item, actor=actor))
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                _log.warning("claude-memory ingest failed for %s: %s",
                             item.slug, exc)
                results.append(_reject([f"error:{type(exc).__name__}"], item.slug))
    from backend import metrics

    for r in results:
        metrics.claude_memory_ingest_total.labels(action=r["action"]).inc()
    return {"results": results}

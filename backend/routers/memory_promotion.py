"""OP-2568 U4-D — dedicated HUMAN-ONLY memory-promotion approval.

The ONLY sanctioned way an approval row is created. Approval does NOT
publish — U4-C revalidates and publishes later. All DB work delegates
to the approvals writer module (zero ledger table names here).

Handler order is load-bearing: the human-principal assertion runs
UNCONDITIONALLY and FIRST — in "open" auth mode the anonymous synthetic
principal carries role=super_admin and would pass BOTH role gates, so
only the human check stops it. Never move it behind a role shortcut.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend import auth as _au
from backend.learned_item_approval import (
    ApprovalValidationError,
    assert_human_principal,
    get_version_audience,
    record_memory_approval,
)

router = APIRouter(tags=["memory-promotions"])


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str
    live_set_hash: str
    digest_id: str | None = None


async def handle_approve(
    user: Any, conn: Any, version_id: str, body: ApproveBody
) -> dict[str, Any]:
    """Plain handler body — tests drive it directly with fakes (no app
    boot, no pool init)."""
    assert_human_principal(user)  # BEFORE any DB access — see module doc
    audience = await get_version_audience(conn, version_id)
    # β-3a (L6 closure at APPROVAL time): a fix reverted since the eval ran
    # must not be approvable. Distinct 409 (vs 422 validation) + a terminal
    # reject row so the version drops out of the pending scan. Marked with
    # reason=reverted_later so the pending card renders revert-forced, never
    # inferred from stats.
    name_row = await conn.fetchrow(
        "SELECT name FROM learned_item_versions WHERE id = $1", version_id
    )
    from backend.agents.memory_promotion_scheduler import (
        _write_terminal_run,
        current_live_set_hash,
        ticket_from_version_name,
    )
    from backend.agents.worker_loop_distiller import reverted_later
    from backend.learned_item_publication import publication_scope_key
    from datetime import datetime, timezone

    ticket = ticket_from_version_name((name_row or {}).get("name") or "")
    if ticket and await reverted_later(conn, ticket):
        tenant_row = await conn.fetchrow(
            "SELECT tenant_id FROM learned_item_versions WHERE id = $1",
            version_id,
        )
        scope_key = publication_scope_key(
            audience=audience,
            tenant_id=(tenant_row or {}).get("tenant_id"),
        )
        await _write_terminal_run(
            conn, version_id=version_id, decision="reject",
            reason="reverted_later",
            live_set_hash=await current_live_set_hash(conn, scope_key=scope_key),
            now=datetime.now(timezone.utc).isoformat(),
            extra={"ticket": ticket, "at": "approval"},
        )
        raise HTTPException(
            status_code=409,
            detail=f"reverted_later: {ticket} was reverted after the eval ran",
        )
    # Freeze G6: global-audience items need super_admin; tenant-level
    # needs admin (already enforced by the route dependency). Audience
    # is server-derived — a body-supplied value is never trusted.
    if audience == "global" and not _au.role_at_least(user.role, "super_admin"):
        raise HTTPException(
            status_code=403,
            detail="global-audience memory promotions require super_admin",
        )
    approval_id = str(uuid4())
    await record_memory_approval(
        conn,
        approval_id=approval_id,
        version_id=version_id,
        eval_run_id=body.eval_run_id,
        live_set_hash=body.live_set_hash,
        approved_by=user.email,  # the REAL authenticated identity
        digest_id=body.digest_id,
    )
    return {"approval_id": approval_id}


_PENDING_SQL = """
SELECT v.id AS version_id, v.kind, v.audience, v.tenant_id, v.name,
       v.created_by, v.renderer_version,
       left(v.rendered_payload, 400) AS rendered_preview,
       r.id AS eval_run_id, r.decision, r.suite_sha256, r.model_fingerprint,
       r.live_set_hash, r.stat_summary, r.ran_at,
       COALESCE(e.kinds, '{}') AS evidence_kinds
FROM learned_item_versions v
JOIN LATERAL (
    SELECT * FROM memory_eval_runs r
     WHERE r.version_id = v.id
     ORDER BY r.ran_at DESC, r.id DESC LIMIT 1
) r ON true
LEFT JOIN LATERAL (
    SELECT array_agg(DISTINCT le.ground_truth_kind) AS kinds
      FROM learned_item_evidence le WHERE le.version_id = v.id
) e ON true
WHERE NOT EXISTS (SELECT 1 FROM memory_approvals a WHERE a.version_id = v.id)
  AND NOT EXISTS (SELECT 1 FROM memory_publications p WHERE p.version_id = v.id)
ORDER BY r.ran_at DESC LIMIT $1 OFFSET $2
"""


def _card_verdict(decision: str, stat_summary: Any) -> dict[str, Any]:
    """β-3a: 'reject (neg-control forced / reverted)' comes from the CATCH
    recorded in stat_summary — NEVER inferred from stats (a forced reject
    can carry promote-shaped numbers)."""
    import json as _json

    stats = stat_summary
    if isinstance(stats, str):
        try:
            stats = _json.loads(stats)
        except ValueError:
            stats = {}
    stats = stats if isinstance(stats, dict) else {}
    catches = stats.get("neg_control_catches") or []
    reason = stats.get("reason") or ""
    label = decision
    if decision == "reject" and catches:
        label = "reject (neg-control forced)"
    elif decision == "reject" and reason == "reverted_later":
        label = "reject (reverted)"
    return {
        "decision": decision,
        "verdict_label": label,
        "neg_control_catches": catches,
        "reason": reason or None,
    }


@router.get("/memory-promotions/pending")
async def list_pending_memory_promotions(
    limit: int = 20,
    offset: int = 0,
    user: _au.User = Depends(_au.require_admin),
) -> dict[str, Any]:
    """β-3a human review lane: quarantined versions + their LATEST eval
    verdict card (suite/model fingerprints surfaced — gate-audit F5)."""
    assert_human_principal(user)
    from backend.db_pool import get_pool  # lazy

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(_PENDING_SQL, limit, offset)
    items = []
    for row in rows:
        d = dict(row)
        items.append({
            "version_id": str(d["version_id"]),
            "name": d["name"],
            "kind": d["kind"],
            "audience": d["audience"],
            "tenant_id": d["tenant_id"],
            "created_by": d["created_by"],
            "renderer_version": d["renderer_version"],
            "rendered_preview": d["rendered_preview"],
            "eval": {
                "eval_run_id": str(d["eval_run_id"]),
                **_card_verdict(d["decision"], d["stat_summary"]),
                "suite_sha256": d["suite_sha256"],
                "model_fingerprint": d["model_fingerprint"],
                "live_set_hash": d["live_set_hash"],
                "ran_at": str(d["ran_at"]),
            },
            "evidence_kinds": list(d["evidence_kinds"] or []),
        })
    return {"items": items, "limit": limit, "offset": offset}


@router.post("/memory-promotions/{version_id}/approve")
async def approve_memory_promotion(
    version_id: str,
    body: ApproveBody,
    user: _au.User = Depends(_au.require_admin),
) -> dict[str, Any]:
    # Human check before the pool is even touched.
    assert_human_principal(user)
    from backend.db_pool import get_pool  # lazy — import must stay side-effect-free

    async with get_pool().acquire() as conn:
        try:
            return await handle_approve(user, conn, version_id, body)
        except ApprovalValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.reason)

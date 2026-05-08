"""OP-735 R5 -- batch-merge candidate dashboard backend.

Two endpoints, both gated to admin+:

  GET  /admin/batch-merge
       List PSes the AI Reviewer auto-tagged
       ``runner-batch-merge-candidate`` along with the snapshot of
       gates / verdict / trust score that produced the tag. The
       operator UI (components/admin/batch-merge-page.tsx) renders
       this as a sortable / filterable table.

  POST /admin/batch-merge/approve
       Bulk-+2 N selected changes. For each, posts Code-Review +2 to
       Gerrit via ``gerrit_client.post_review`` and writes one
       ``batch_merge_approve`` audit row per change so /admin/audit?
       action=batch_merge_approve enumerates the operator's history.

Module-global state audit
-------------------------
Stateless router. Reads pull from the in-memory candidate registry
maintained by ``backend.agents.ai_reviewer`` (process-local) plus the
PG ``trust_scores`` table; the +2 path goes out over the existing
``gerrit_client`` SSH connection pool. Audit rows go through
``backend.audit.log`` which already advisory-locks per tenant.

Read-after-write timing audit
-----------------------------
Each +2 commits its own audit row via the audit module's pool-scoped
transaction; the response is built strictly from per-change apply
results, so the client never reads back stale state.

CLAUDE.md L1 invariant preservation
-----------------------------------
The +2 score is posted from an *operator-authenticated* request with
``actor=operator.email``; the AI Reviewer only ever produces +1, never
+2. The audit row records the operator's email + the AI verdict
snapshot at the time the candidate was tagged so retrospective review
can verify the AI didn't usurp the human +2.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.agents.ai_reviewer import BATCH_MERGE_HASHTAG


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/batch-merge", tags=["admin", "batch-merge"])


# ── In-memory candidate registry ─────────────────────────────────────


@dataclass
class BatchMergeCandidate:
    """Snapshot the AI Reviewer captures when it auto-tags a PS.

    Stored in-process (per-worker). Cross-worker visibility is not
    critical here because the AI Reviewer webhook handler runs on a
    single worker per patchset event; if the dashboard worker happens
    to be different, the operator simply won't see it for one
    refresh-cycle. The trust_scores table + the Gerrit hashtag remain
    the durable source of truth.
    """

    change_id: str
    project: str
    bot: str
    file_class: str
    insertions: int
    deletions: int
    files: list[str]
    ai_summary: str
    tagged_at: float
    revision: str = ""
    subject: str = ""
    agent_class: str = ""
    tier: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


_candidates: dict[str, BatchMergeCandidate] = {}


def register_candidate(candidate: BatchMergeCandidate) -> None:
    """Called by the AI Reviewer auto-+1 path after Gerrit accepts the
    hashtag + +1 vote. Idempotent on ``change_id`` (later registration
    overwrites earlier — the latest snapshot wins so re-uploads of a
    patchset don't leave stale data on the dashboard)."""
    _candidates[candidate.change_id] = candidate


def remove_candidate(change_id: str) -> BatchMergeCandidate | None:
    """Drop a candidate from the dashboard (after operator +2 or
    after Gerrit reports the change is no longer mergeable / merged /
    abandoned). Returns the popped row for audit-trail callers."""
    return _candidates.pop(change_id, None)


def list_candidates() -> list[BatchMergeCandidate]:
    return list(_candidates.values())


def _reset_for_tests() -> None:
    """Tests-only helper: clear the in-process registry between cases."""
    _candidates.clear()


# ── GET /admin/batch-merge ───────────────────────────────────────────


@router.get("")
async def list_batch_merge_candidates(
    _request: Request,
    actor: auth.User = Depends(auth.require_admin),
    agent_class: str | None = None,
    tier: str | None = None,
    file_glob: str | None = None,
) -> JSONResponse:
    """Return the current AI-auto-tagged batch-merge candidates.

    Filters mirror the OP-735 spec (agent_class / tier / file_glob).
    ``file_glob`` is a substring match on each candidate's file list
    -- intentionally simple; the dashboard's value comes from the +2
    bulk action, not from the filter expressiveness.
    """
    rows = list_candidates()
    if agent_class:
        rows = [r for r in rows if r.agent_class == agent_class]
    if tier:
        rows = [r for r in rows if r.tier == tier]
    if file_glob:
        needle = file_glob.strip()
        rows = [r for r in rows if any(needle in f for f in r.files)]

    rows.sort(key=lambda r: r.tagged_at, reverse=True)

    return JSONResponse(
        status_code=200,
        content={
            "candidates": [r.to_payload() for r in rows],
            "hashtag": BATCH_MERGE_HASHTAG,
            "operator": actor.email,
            "fetched_at": time.time(),
        },
    )


# ── POST /admin/batch-merge/approve ──────────────────────────────────


class BatchApproveRequest(BaseModel):
    change_ids: list[str] = Field(
        min_length=1,
        max_length=100,
        description=(
            "Gerrit Change-Id values to bulk-+2. Capped at 100 per call "
            "so a single operator click can't accidentally vote on an "
            "entire backlog."
        ),
    )
    message: str = Field(
        default="Operator batch-approved per AI verdict.",
        max_length=400,
    )


@dataclass
class BatchApproveResult:
    change_id: str
    ok: bool
    reason: str = ""


async def _post_plus_two(
    change_id: str, message: str, project: str = "",
) -> tuple[bool, str]:
    """Wrap ``gerrit_client.post_review`` with the +2 vote + audit
    string. Returns ``(ok, error_message)``."""
    from backend.gerrit import gerrit_client

    result = await gerrit_client.post_review(
        commit=change_id,
        message=message,
        labels={"Code-Review": 2},
        project=project,
    )
    if isinstance(result, dict) and "error" in result:
        return False, str(result["error"])
    return True, ""


@router.post("/approve")
async def approve_batch_merge_candidates(
    req: BatchApproveRequest,
    request: Request,
    actor: auth.User = Depends(auth.require_admin),
) -> JSONResponse:
    """Bulk-+2 the requested change ids. Per-change audit logging."""
    from backend import audit as _audit

    results: list[BatchApproveResult] = []
    for change_id in req.change_ids:
        candidate = _candidates.get(change_id)
        if candidate is None:
            results.append(BatchApproveResult(
                change_id=change_id, ok=False,
                reason="not a current batch-merge candidate",
            ))
            continue

        ok, err = await _post_plus_two(
            change_id, req.message, project=candidate.project,
        )
        if not ok:
            results.append(BatchApproveResult(
                change_id=change_id, ok=False,
                reason=f"gerrit: {err}",
            ))
            continue

        try:
            await _audit.log(
                action="batch_merge_approve",
                entity_kind="gerrit_change",
                entity_id=change_id,
                before={"hashtag": BATCH_MERGE_HASHTAG},
                after={
                    "code_review": 2,
                    "ai_summary": candidate.ai_summary,
                    "bot": candidate.bot,
                    "file_class": candidate.file_class,
                    "insertions": candidate.insertions,
                    "deletions": candidate.deletions,
                    "operator_message": req.message,
                },
                actor=actor.email,
            )
        except Exception as exc:
            # ``audit.log`` already swallows internally; this is a
            # defensive belt so the +2 path keeps the same best-effort
            # audit posture as existing admin routers.
            logger.warning("batch_merge audit failed: %s", exc)

        # Drop from the dashboard registry so operators don't double-+2.
        remove_candidate(change_id)

        results.append(BatchApproveResult(change_id=change_id, ok=True))

    succeeded = sum(1 for r in results if r.ok)
    return JSONResponse(
        status_code=200,
        content={
            "results": [asdict(r) for r in results],
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "operator": actor.email,
        },
    )

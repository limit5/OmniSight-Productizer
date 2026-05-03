"""O4 (#267) — Orchestrator Gateway HTTP surface.

FastAPI shim over ``backend.orchestrator_gateway``:

  * POST /orchestrator/intake        — Jira webhook intake
  * POST /orchestrator/replan        — PM-approved replan
  * GET  /orchestrator/status/{tid}  — DAG / CATC / Gerrit state

Auth model matches the rest of the enterprise surface:

  * Intake + replan require ``require_operator`` (write).
  * Status requires ``require_viewer`` (read-only).

Webhook signature check: we re-use the same HMAC shared secret as
``/webhooks/jira`` (``settings.jira_webhook_secret``) so a single Jira
automation rule can hit either path.  When the secret is unset, the
path is still available for operator-driven testing behind the normal
``require_operator`` auth — Jira itself is locked out.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import merge_arbiter as _arb
from backend import orchestrator_gateway as og
from backend.config import settings
from backend.queue_backend import PriorityLevel
from backend.submit_rule import ReviewerVote, evaluate_submit_rule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request / response shapes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class IntakeRequest(BaseModel):
    """Direct JSON-body shape for manual / CLI invocation.  Jira
    webhooks hit the raw-body variant at the top of ``intake()`` so
    signature verification runs on the untouched bytes."""

    issue: dict[str, Any] | None = Field(default=None)
    jira_ticket: str | None = Field(default=None)
    summary: str | None = Field(default=None)
    description: str | None = Field(default=None)
    priority: str = Field(default=PriorityLevel.P2.value)
    token_budget: int | None = Field(default=None)
    forbidden_globs: list[str] | None = Field(default=None)

    model_config = {"extra": "allow"}


class ReplanRequest(BaseModel):
    jira_ticket: str
    approver: str
    new_story: str | None = None
    priority: str = Field(default=PriorityLevel.P2.value)
    token_budget: int | None = None
    forbidden_globs: list[str] | None = None
    override_human_review: bool = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _coerce_priority(raw: str) -> PriorityLevel:
    try:
        return PriorityLevel(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid priority {raw!r}; expected one of "
                   f"{[p.value for p in PriorityLevel]}",
        )


def _verify_jira_signature(request: Request, raw_body: bytes) -> bool:
    """Return True if the request carried a valid Bearer token matching
    ``settings.jira_webhook_secret``, or False if no secret is configured
    (operator-only path — require_operator auth still applies).

    Raises HTTPException(401) on an explicit bad token.
    """
    secret = getattr(settings, "jira_webhook_secret", "") or ""
    if not secret:
        return False
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer ") and hmac.compare_digest(
        header[len("Bearer "):], secret,
    ):
        return True
    # Also accept a dedicated ``X-Jira-Webhook-Secret`` header for
    # automations that can't set Authorization.
    alt = request.headers.get("X-Jira-Webhook-Secret", "")
    if alt and hmac.compare_digest(alt, secret):
        return True
    raise HTTPException(status_code=401, detail="Invalid Jira webhook secret")


def _error_response(exc: og.IntakeError) -> JSONResponse:
    status = 400
    if exc.reason is og.IntakeRejectReason.llm_unavailable:
        status = 503
    if exc.reason is og.IntakeRejectReason.token_budget_exceeded:
        status = 402
    if exc.reason is og.IntakeRejectReason.llm_firewall_blocked:
        status = 403
    if exc.reason is og.IntakeRejectReason.pending_human_review:
        status = 409
    return JSONResponse(
        status_code=status,
        content={
            "ok": False,
            "reason": exc.reason.value,
            "detail": exc.detail,
            "context": exc.context,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/intake")
async def intake_endpoint(
    request: Request,
    _user=Depends(_au.require_operator),
) -> Any:
    """Jira webhook intake.  Reads raw body so signature verification
    works, then drives the ``orchestrator_gateway.intake`` pipeline.

    On success: 200 JSON with the intake outcome (``state`` tells the
    caller whether CATCs were queued or held for PM review).
    On failure: 4xx/5xx JSON with ``{reason, detail, context}``.
    """
    raw = await request.body()
    if len(raw) > 1_048_576:
        raise HTTPException(status_code=413, detail="Payload too large")
    _verify_jira_signature(request, raw)

    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        body = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400,
                            detail="Body must be a JSON object")

    # Parse via the Pydantic model for type safety, but pass the raw
    # dict to ``parse_jira_webhook`` so Jira's nested issue.fields shape
    # still works.
    try:
        req_model = IntakeRequest.model_validate(body)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"Invalid intake request: {exc}") from exc

    priority = _coerce_priority(req_model.priority)

    try:
        from backend.db_context import current_tenant_id
        tenant_id = current_tenant_id()
    except Exception:
        tenant_id = None

    try:
        outcome = await og.intake(
            body,
            token_budget=req_model.token_budget,
            priority=priority,
            forbidden_globs=req_model.forbidden_globs,
            tenant_id=tenant_id,
        )
    except og.IntakeError as exc:
        return _error_response(exc)

    # Audit trail — best-effort.
    try:
        from backend import audit
        await audit.log(
            action="orchestrator_intake",
            entity_kind="jira_ticket",
            entity_id=outcome.jira_ticket,
            after=outcome.to_dict(),
            actor=getattr(_user, "email", "system"),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("orchestrator intake audit.log failed: %s", exc)

    return {"ok": True, **outcome.to_dict()}


@router.post("/replan")
async def replan_endpoint(
    payload: ReplanRequest,
    _user=Depends(_au.require_operator),
) -> Any:
    """PM-approved replan for a previously-intaken ticket."""
    priority = _coerce_priority(payload.priority)
    try:
        outcome = await og.replan(
            payload.jira_ticket,
            approver=payload.approver,
            new_story=payload.new_story,
            token_budget=payload.token_budget,
            priority=priority,
            forbidden_globs=payload.forbidden_globs,
            override_human_review=payload.override_human_review,
        )
    except og.IntakeError as exc:
        return _error_response(exc)

    try:
        from backend import audit
        await audit.log(
            action="orchestrator_replan",
            entity_kind="jira_ticket",
            entity_id=payload.jira_ticket,
            after={
                **outcome.to_dict(),
                "approver": payload.approver,
                "override_human_review": payload.override_human_review,
            },
            actor=getattr(_user, "email", "system"),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("orchestrator replan audit.log failed: %s", exc)

    return {"ok": True, **outcome.to_dict()}


@router.get("/status/{jira_ticket}")
async def status_endpoint(
    jira_ticket: str,
    _user=Depends(_au.require_viewer),
) -> Any:
    """Return the DAG + CATC + Gerrit snapshot for a Jira ticket."""
    snapshot = og.get_status(jira_ticket)
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"no intake session for {jira_ticket!r}",
        )
    return snapshot


@router.get("/status")
async def list_status_endpoint(
    _user=Depends(_au.require_viewer),
) -> Any:
    """Operator surface — every intake session in this process."""
    return {"sessions": og.list_sessions()}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  O7 (#270) — Merge-conflict webhook + human-vote reconciliation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MergeConflictRequest(BaseModel):
    """Gerrit / GitHub webhook → arbiter intake shape."""

    change_id: str
    project: str
    file_path: str
    conflict_text: str = ""
    head_commit_message: str = ""
    incoming_commit_message: str = ""
    file_context: str = ""
    patchset_revision: str = ""
    workspace: str | None = None
    additional_files: list[str] = Field(default_factory=list)
    jira_ticket: str = ""
    catc_owner: str = ""

    model_config = {"extra": "allow"}


class HumanVotePayload(BaseModel):
    """Per-vote shape for reconciliation calls.  ``groups`` comes from
    the Gerrit account look-up; orchestrator-side cache."""

    voter: str
    groups: list[str]
    score: int


class HumanVoteRequest(BaseModel):
    change_id: str
    project: str
    commit: str
    votes: list[HumanVotePayload]


@router.post("/merge-conflict")
async def merge_conflict_endpoint(
    request: Request,
    _user=Depends(_au.require_operator),
) -> Any:
    """Gerrit webhook entry: a merge conflict was detected on a change.

    Body shape matches :class:`MergeConflictRequest`.  Gerrit's native
    webhooks plugin posts this payload after the orchestrator's webhook
    mapper normalises the event.  We re-use the Jira HMAC secret for
    signature verification so operators only maintain one secret.
    """
    raw = await request.body()
    if len(raw) > 1_048_576:
        raise HTTPException(status_code=413, detail="Payload too large")
    _verify_jira_signature(request, raw)

    if not raw:
        raise HTTPException(status_code=400, detail="Empty body")
    try:
        body = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"Invalid JSON: {exc}") from exc
    try:
        model = MergeConflictRequest.model_validate(body)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid merge-conflict payload: {exc}",
        ) from exc

    task = _arb.MergeConflictTask(
        change_id=model.change_id,
        project=model.project,
        file_path=model.file_path,
        conflict_text=model.conflict_text,
        head_commit_message=model.head_commit_message,
        incoming_commit_message=model.incoming_commit_message,
        file_context=model.file_context,
        patchset_revision=model.patchset_revision,
        workspace=model.workspace,
        additional_files=list(model.additional_files),
        jira_ticket=model.jira_ticket,
        catc_owner=model.catc_owner,
    )
    outcome = await _arb.on_merge_conflict_webhook(task)

    try:
        from backend import audit
        await audit.log(
            action="arbiter_merge_conflict",
            entity_kind="gerrit_change",
            entity_id=model.change_id,
            after=outcome.to_dict(),
            actor=getattr(_user, "email", "system"),
        )
    except Exception as exc:                             # pragma: no cover
        logger.debug("arbiter audit.log failed: %s", exc)

    return {"ok": True, **outcome.to_dict()}


@router.post("/human-vote")
async def human_vote_endpoint(
    payload: HumanVoteRequest,
    _user=Depends(_au.require_operator),
) -> Any:
    """Gerrit webhook entry: a human (or AI bot) cast a Code-Review.

    We re-evaluate the submit-rule + drive submit / withdraw flows.
    """
    votes = [
        ReviewerVote(
            voter=v.voter,
            groups=frozenset(v.groups),
            score=v.score,
        )
        for v in payload.votes
    ]
    outcome = await _arb.on_human_vote_recorded(
        change_id=payload.change_id,
        project=payload.project,
        commit=payload.commit,
        votes=votes,
    )
    try:
        from backend import audit
        await audit.log(
            action="arbiter_human_vote",
            entity_kind="gerrit_change",
            entity_id=payload.change_id,
            after=outcome.to_dict(),
            actor=getattr(_user, "email", "system"),
        )
    except Exception as exc:                             # pragma: no cover
        logger.debug("arbiter human-vote audit.log failed: %s", exc)

    return {"ok": True, **outcome.to_dict()}


@router.post("/check-change-ready")
async def check_change_ready_endpoint(
    payload: HumanVoteRequest,
    _user=Depends(_au.require_viewer),
) -> Any:
    """Pure query: evaluate the submit-rule for a given vote set.

    No side-effects; useful from the UI / CLI / observability layer.
    """
    votes = [
        ReviewerVote(
            voter=v.voter,
            groups=frozenset(v.groups),
            score=v.score,
        )
        for v in payload.votes
    ]
    return {"ok": True, **evaluate_submit_rule(votes).to_dict()}

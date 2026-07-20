"""U6-6 — the ``/memories`` user-lane route (L3 confirm / revoke / erase).

The frozen design's §2.F consent surface + the U6-6 DEFERRED DoD:
**the ExecutionContext and MemoryScope are built from the SERVER-AUTHENTICATED
session, never from client input.** A caller can only ever act on THEIR OWN
memory — identity comes from ``require_operator``'s ``User`` (the same session
principal the chat route trusts), and every downstream call
(``confirm_and_publish`` / ``revoke_fact`` / ``hard_erase_user``) re-checks the
human-owner gate (``ctx.principal_type == 'human'`` AND ``ctx.actor_id ==
scope.user_id`` AND tenant) — defence in depth, not a substitute for building
the scope server-side.

This is the READ-of-candidates + WRITE-of-consent surface. It does NOT flip
injection on: L3 facts reach a prompt only when ``OMNISIGHT_SORA_L3_READ`` is
set (U6-7 loader) AND a fact has been promoted here. Listing is always allowed
(a user may review their own memory regardless of the read flag).

§2.F enforcement status (L3c): the per-user WRITE rate limit
(anti-rubber-stamp, ``_memories_write_limit``) and the explicit
sensitive-category acknowledgment (428 without ``acknowledge_sensitive``) are
enforced SERVER-SIDE here. Still deferred: keyset pagination of the listings
(today a single ``_LIST_CAP`` page + per-row decrypt; add real paging when the
write path produces volume).

Endpoints (all scoped to the authenticated user):
  GET  /memories          → live (promoted) facts, exact rendered bytes
  GET  /memories/pending  → quarantined candidates awaiting confirm (+ eval id)
  POST /memories/{id}/confirm → promote a candidate (server looks up its eval)
  POST /memories/{id}/revoke  → unpublish a live fact
  POST /memories/erase        → crypto-shred ALL of the user's memory
"""

from __future__ import annotations

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import auth as _au
from backend.agents import execution_context as _ec
from backend.agents.u6_fact_schema import FactType, Sensitivity, render_fact
from backend.agents.u6_l3_producer import produce_quarantine_and_record
from backend.agents.u6_l3_confirm import (
    L3ConfirmError,
    confirm_and_publish,
    discard_fact,
    hard_erase_user,
    revoke_fact,
)
from backend.agents.u6_l3_eval_adapter import L3EvalError, latest_promote_eval_for_fact
from backend.agents.u6_l3_store import L3StoreError, StoredFact, list_facts
from backend.agents.u6_memory_scope import MemoryScope
from backend.db_pool import get_conn

# A confirm/revoke can lose a race (double-click, concurrent /erase) so a
# state-transition failure from the store/eval layer is a normal 409, not a
# 500. All three are caught wherever a write is attempted.
_L3_WRITE_ERRORS = (L3ConfirmError, L3StoreError, L3EvalError)

# §2.F anti-rubber-stamp: a per-user cap on memory WRITE actions
# (propose/confirm/revoke/erase share one bucket). 60/h is far above any
# honest one-at-a-time review pace and far below a scripted bulk run.
# Reads are uncapped (reviewing is always allowed).
_WRITE_CAP_PER_HOUR = 60


async def _memories_write_limit(
    user: _au.User = Depends(_au.require_operator),
) -> _au.User:
    """Rate-limit dependency for the WRITE endpoints. Composes on top of
    ``require_operator`` (auth + role + CSRF), then applies the shared I9
    limiter (Redis-backed in prod, per-replica in-memory fallback) — the same
    infra ``check_llm_quota`` uses."""
    from backend.rate_limit import get_limiter

    allowed, retry_after = get_limiter().allow(
        key=f"mem:write:{user.id}",
        capacity=_WRITE_CAP_PER_HOUR,
        window_seconds=3600.0,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"memory-write rate limit exceeded ({_WRITE_CAP_PER_HOUR}/h); "
                f"retry in {int(retry_after)}s"
            ),
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )
    return user

# Bound a single listing response (the producer is dormant today, but the
# view decrypts per row — never return an unbounded page once writes land).
_LIST_CAP = 500

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memories", tags=["memories"])


def _scope_and_ctx(user: _au.User) -> tuple[MemoryScope, _ec.ExecutionContext]:
    """Build BOTH the scope and the human ctx from the authenticated session
    ONLY — the client never supplies tenant/user identity. request_id is fresh
    per call; message_id is None (not a chat turn)."""
    scope = MemoryScope(tenant_id=user.tenant_id, user_id=user.id)
    ctx = _ec.for_human(
        user=user,
        tenant_id=user.tenant_id,
        session_id=None,
        request_id=uuid.uuid4().hex,
        message_id=None,
        authorization_source="memories_ui",
    )
    return scope, ctx


class ProposeMemoryBody(BaseModel):
    """A user-EXPLICIT candidate proposal. The user states their OWN
    preference/profile/project fact; the MODEL is deliberately NOT in this
    write path (no distiller in v1 — that keeps the poisoning surface a human
    action). The body carries only DATA fields; identity is server-side and
    ``source_span`` is server-set (never client text)."""

    fact_type: str = Field(max_length=32)
    predicate: str = Field(max_length=64)
    value: str = Field(max_length=64)
    declared_sensitivity: str = Field(default="normal", max_length=16)


class ConfirmMemoryBody(BaseModel):
    """§2.F sensitive-category acknowledgment. Confirming a SENSITIVE
    candidate requires the explicit flag (server-enforced 428 otherwise);
    a normal candidate needs no body at all."""

    acknowledge_sensitive: bool = False


def _fact_view(sf: StoredFact) -> dict:
    """A safe, content-bounded view: the EXACT rendered bytes the guard would
    inject (§2.F "show exact rendered bytes") + non-authoritative metadata."""
    return {
        "fact_id": sf.id,
        "rendered": render_fact(sf.fact),
        "fact_type": sf.fact.fact_type.value,
        "predicate": sf.fact.predicate,
        "sensitivity": sf.fact.sensitivity.value,
        # §2.F "source context": the inert provenance span (grammar-bounded,
        # no free text) so the user confirms with informed consent.
        "source_span": sf.fact.source_span,
        "valid_from": sf.fact.valid_from,
        "valid_until": sf.fact.valid_until,
        "revision": sf.revision,
    }


@router.get("")
async def list_live_memories(
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """The user's LIVE (promoted) facts — what U6-7 would inject for them."""
    scope, _ctx = _scope_and_ctx(user)
    facts = await list_facts(conn, scope, state="promoted")
    return {"memories": [_fact_view(sf) for sf in facts[:_LIST_CAP]]}


@router.get("/pending")
async def list_pending_memories(
    user: _au.User = Depends(_au.require_operator),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """Quarantined candidates awaiting the user's confirm. Each carries the id
    of its passing safety eval so ``confirm`` needs no client-supplied eval id.
    A candidate with no ``promote`` eval is NOT surfaced (it can't be
    confirmed) — it should not exist via the producer, but we fail safe."""
    scope, _ctx = _scope_and_ctx(user)
    facts = await list_facts(conn, scope, state="quarantined")
    out = []
    for sf in facts[:_LIST_CAP]:
        eval_run_id = await latest_promote_eval_for_fact(conn, scope, sf.id)
        if eval_run_id is None:
            continue
        out.append({**_fact_view(sf), "eval_run_id": eval_run_id})
    return {"pending": out}


@router.post("/propose")
async def propose_memory(
    body: ProposeMemoryBody,
    user: _au.User = Depends(_memories_write_limit),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """Propose a candidate from the user's OWN explicit statement. Runs the
    FULL producer gate (triage → closed-schema → memory-safety eval) and lands
    QUARANTINED (never live) with its promote eval recorded — the user then
    reviews it under /pending and confirms. A rejected candidate returns 422
    with the gate reason; nothing is stored. The model is NOT in this path."""
    scope, _ctx = _scope_and_ctx(user)
    try:
        fact_type = FactType(body.fact_type)
        sensitivity = Sensitivity(body.declared_sensitivity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid enum: {exc}") from exc
    # source_span is SERVER-SET (bounded grammar), never client free text.
    source_span = f"memories_ui:{uuid.uuid4().hex[:16]}"
    outcome, fact_id, _eval = await produce_quarantine_and_record(
        conn, scope,
        fact_type=fact_type,
        subject="user",
        predicate=body.predicate,
        value=body.value,
        source_span=source_span,
        declared_sensitivity=sensitivity,
    )
    if not outcome.accepted or fact_id is None:
        # A gate rejected it — 422 with the reason (triage/schema/safety). The
        # reason is a bounded gate label, never echoed user content.
        raise HTTPException(status_code=422, detail=f"rejected: {outcome.reason}")
    logger.info("u6_memories propose tenant=%s fact=%s", scope.tenant_id, fact_id)
    return {
        "proposed": True,
        "fact_id": fact_id,
        "rendered": render_fact(outcome.fact),
        "status": "quarantined",
    }


@router.post("/{fact_id}/confirm")
async def confirm_memory(
    fact_id: str,
    body: ConfirmMemoryBody | None = None,
    user: _au.User = Depends(_memories_write_limit),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """Confirm + publish ONE candidate. The eval id is resolved SERVER-SIDE
    from the fact (never trusted from the client), so a user cannot bind a
    confirm to some other fact's eval. A SENSITIVE candidate additionally
    requires the explicit §2.F acknowledgment (server-enforced, 428)."""
    scope, ctx = _scope_and_ctx(user)
    eval_run_id = await latest_promote_eval_for_fact(conn, scope, fact_id)
    if eval_run_id is None:
        raise HTTPException(status_code=404, detail="no confirmable candidate for that id")
    # §2.F sensitive-category acknowledgment — enforced on the SERVER (the
    # frontend checkbox is UX, not the gate). Scoped lookup of the candidate.
    candidates = await list_facts(conn, scope, state="quarantined")
    target = next((sf for sf in candidates if sf.id == fact_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="no confirmable candidate for that id")
    if target.fact.sensitivity is Sensitivity.SENSITIVE and not (
        body is not None and body.acknowledge_sensitive
    ):
        raise HTTPException(
            status_code=428,
            detail="sensitive_ack_required: this memory is marked sensitive — "
                   "confirming it requires explicit acknowledgment",
        )
    try:
        result = await confirm_and_publish(
            conn, ctx, scope, eval_run_id=eval_run_id, fact_id=fact_id
        )
    except _L3_WRITE_ERRORS as exc:
        # A double-confirm or a race with /erase makes the state transition
        # illegal — a 409 conflict, not a 500. (assert_human_owner failures
        # also land here; both are safe to surface as the caller's fault.)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info("u6_memories confirm tenant=%s fact=%s", scope.tenant_id, fact_id)
    return {"confirmed": True, "fact_id": result.fact_id, "revision": result.revision}


@router.post("/{fact_id}/discard")
async def discard_memory(
    fact_id: str,
    user: _au.User = Depends(_memories_write_limit),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """Discard a QUARANTINED candidate the user chose not to confirm (reject +
    key-shred). Distinct from revoke (which unpublishes a LIVE fact)."""
    scope, ctx = _scope_and_ctx(user)
    try:
        discarded = await discard_fact(conn, ctx, scope, fact_id)
    except _L3_WRITE_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not discarded:
        raise HTTPException(status_code=404, detail="no pending candidate with that id")
    logger.info("u6_memories discard tenant=%s fact=%s", scope.tenant_id, fact_id)
    return {"discarded": True, "fact_id": fact_id}


@router.post("/{fact_id}/revoke")
async def revoke_memory(
    fact_id: str,
    user: _au.User = Depends(_memories_write_limit),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """Revoke (unpublish) a live fact — it leaves the injectable set."""
    scope, ctx = _scope_and_ctx(user)
    try:
        revoked = await revoke_fact(conn, ctx, scope, fact_id)
    except _L3_WRITE_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not revoked:
        raise HTTPException(status_code=404, detail="no live fact with that id")
    logger.info("u6_memories revoke tenant=%s fact=%s", scope.tenant_id, fact_id)
    return {"revoked": True, "fact_id": fact_id}


@router.post("/erase")
async def erase_memories(
    user: _au.User = Depends(_memories_write_limit),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    """"Delete ALL my memory": crypto-shred every fact + the eval ledger."""
    scope, ctx = _scope_and_ctx(user)
    try:
        result = await hard_erase_user(conn, ctx, scope)
    except _L3_WRITE_ERRORS as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "u6_memories erase tenant=%s facts=%d", scope.tenant_id, result.facts
    )
    return {
        "erased": True,
        "facts": result.facts,
        "eval_runs": result.eval_runs,
        "approvals": result.approvals,
    }

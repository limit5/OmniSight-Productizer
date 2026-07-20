"""U6-6 — L3 /memories confirm / publish / revoke / hard-erase (DORMANT).

The user-lane publication path (frozen design §2.F / §3 L3). A per-user HUMAN gate:
only a human principal may confirm, and only their OWN memory
(``ctx.principal_type == 'human'`` AND ``ctx.actor_id == scope.user_id``) — distinct
from the global super-admin gate. On confirm, ATOMICALLY (one transaction):
record the user approval (U6-5b eval-gate: only a ``promote`` eval) THEN promote the
quarantined fact to live (U6-4 ``promote_fact`` = one-current-value). Revoke
supersedes a live fact (removes it from the injectable set). Hard-erase runs BOTH
the U6-4 fact crypto-shred AND the U6-5b eval-ledger delete.

Ships ``OMNISIGHT_SORA_L3_READ`` **default OFF** — U6-7 is the ONLY increment that
turns memory injection on; nothing reaches the prompt before the guard exists.

Composes the already-merged U6-4/U6-5b pieces. DORMANT: no route/endpoint wires the
``/memories`` UX yet (that surface is U6-6's frontend follow-up); this is the
backend orchestration + the human gate. Not the read path (U6-7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.agents.execution_context import ExecutionContext
from backend.agents.u6_l3_eval_adapter import erase_user_evals, record_approval
from backend.agents.u6_l3_store import erase_user, promote_fact
from backend.agents.u6_memory_scope import MemoryScope

_L3_READ_FLAG = "OMNISIGHT_SORA_L3_READ"


class L3ConfirmError(Exception):
    """A confirm/publish invariant was violated — fail closed."""


def l3_read_enabled() -> bool:
    """The L3 read/injection kill-switch — DEFAULT OFF. U6-7 gates the read path on
    it; until U6-7 lands + an operator flips it, no L3 fact ever reaches a prompt."""
    return os.environ.get(_L3_READ_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def assert_human_owner(ctx: ExecutionContext, scope: MemoryScope) -> None:
    """The per-user human gate: a HUMAN principal confirming their OWN memory.
    A bot/service/machine principal, or a human acting on another user's scope, is
    rejected — a user may only confirm their own facts."""
    if not isinstance(ctx, ExecutionContext):
        raise L3ConfirmError("ctx must be a server-constructed ExecutionContext")
    if not isinstance(scope, MemoryScope):
        raise L3ConfirmError("scope must be a MemoryScope")
    if ctx.principal_type != "human":
        raise L3ConfirmError("only a human principal may confirm memory")
    if ctx.tenant_id != scope.tenant_id:
        raise L3ConfirmError("tenant mismatch")
    if ctx.actor_id != scope.user_id:
        raise L3ConfirmError("a user may only confirm their OWN memory")


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    approval_id: str
    fact_id: str
    revision: int


async def confirm_and_publish(
    conn,
    ctx: ExecutionContext,
    scope: MemoryScope,
    *,
    eval_run_id: str,
    fact_id: str,
) -> ConfirmResult:
    """The user confirms + publishes one candidate fact. ATOMIC: record the approval
    (U6-5b eval-gate) then promote it to live (U6-4). Human-owner gated."""
    assert_human_owner(ctx, scope)
    async with conn.transaction():  # outer txn → the two steps commit together
        approval_id = await record_approval(
            conn, scope, eval_run_id=eval_run_id, fact_id=fact_id, approved_by=ctx.actor_id
        )
        revision = await promote_fact(conn, scope, fact_id)
    return ConfirmResult(approval_id=approval_id, fact_id=fact_id, revision=revision)


async def revoke_fact(conn, ctx: ExecutionContext, scope: MemoryScope, fact_id: str) -> bool:
    """Revoke (unpublish) a live fact — supersede it so it leaves the injectable
    set. Human-owner gated. Returns True if a promoted row was superseded.
    (Full per-fact crypto-shred is ``hard_erase_user`` / a later per-fact shred.)"""
    assert_human_owner(ctx, scope)
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", scope.tenant_id)
        await conn.execute("SELECT set_config('app.user_id', $1, true)", scope.user_id)
        row = await conn.fetchrow(
            "UPDATE l3_facts SET state = 'superseded' "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = $3 AND state = 'promoted' RETURNING id",
            fact_id, scope.tenant_id, scope.user_id,
        )
    return row is not None


async def discard_fact(conn, ctx: ExecutionContext, scope: MemoryScope, fact_id: str) -> bool:
    """Discard a QUARANTINED candidate the user chose not to confirm — reject it
    (+ crypto-shred its key, mirroring the U6-8 TTL-decay posture: a never-
    confirmed candidate keeps no live key). Human-owner gated. Returns True if a
    quarantined row was rejected. This is the /pending discard control; ``revoke``
    is for already-promoted (live) facts."""
    assert_human_owner(ctx, scope)
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", scope.tenant_id)
        await conn.execute("SELECT set_config('app.user_id', $1, true)", scope.user_id)
        row = await conn.fetchrow(
            "UPDATE l3_facts SET state = 'rejected', dek_ref = '{}'::jsonb "
            "WHERE id = $1 AND tenant_id = $2 AND user_id = $3 AND state = 'quarantined' RETURNING id",
            fact_id, scope.tenant_id, scope.user_id,
        )
    return row is not None


@dataclass(frozen=True, slots=True)
class EraseResult:
    facts: int
    eval_runs: int
    approvals: int


async def hard_erase_user(conn, ctx: ExecutionContext, scope: MemoryScope) -> EraseResult:
    """"Delete ALL my memory": crypto-shred every L3 fact (U6-4) AND delete the
    eval/approval ledger (U6-5b) — BOTH, per the U6-5b audit DoD (else eval rows
    with raw user_id survive). Human-owner gated. ATOMIC: the two stores run in ONE
    outer transaction (nested savepoints), so a mid-erase failure rolls BOTH back —
    the erase is all-or-nothing and safely retryable, never a partial residue."""
    assert_human_owner(ctx, scope)
    async with conn.transaction():
        facts = await erase_user(conn, scope)
        eval_runs, approvals = await erase_user_evals(conn, scope)
    return EraseResult(facts=facts, eval_runs=eval_runs, approvals=approvals)

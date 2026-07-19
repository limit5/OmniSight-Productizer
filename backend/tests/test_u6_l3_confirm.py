"""U6-6 — L3 confirm / publish / revoke / hard-erase tests (offline + PG-gated).

Pins the per-user HUMAN gate (only a human may confirm their OWN memory), the
atomic confirm→publish (eval-gate approval → promote), revoke, the both-stores
hard-erase, and the L3_READ kill-switch default OFF.
"""
from __future__ import annotations

import uuid

import pytest

from backend.agents.execution_context import ExecutionContext
from backend.agents.u6_fact_schema import Fact, FactType
from backend.agents.u6_l3_confirm import (
    L3ConfirmError,
    assert_human_owner,
    confirm_and_publish,
    hard_erase_user,
    l3_read_enabled,
    revoke_fact,
)
from backend.agents.u6_l3_eval_adapter import record_eval
from backend.agents.u6_l3_store import insert_fact, list_facts
from backend.agents.u6_memory_safety_eval import MemorySafetyDecision
from backend.agents.u6_memory_scope import MemoryScope


def _ctx(tenant: str, user: str, principal: str = "human") -> ExecutionContext:
    return ExecutionContext(
        principal_type=principal, tenant_id=tenant, actor_id=user, roles=("user",),
        session_id=None, request_id="r", message_id=None, authorization_source="chat_confirm",
    )


def _fact(value="named_pipes") -> Fact:
    return Fact(FactType.PREFERENCE, "user", "preferred_ipc", value, source_span="chat:m1")


# ── the per-user human gate ──────────────────────────────────────────────────
def test_human_owner_ok() -> None:
    assert_human_owner(_ctx("t", "u"), MemoryScope("t", "u"))  # no raise


@pytest.mark.parametrize(
    "ctx,scope",
    [
        (_ctx("t", "u", "machine"), MemoryScope("t", "u")),   # not a human
        (_ctx("t", "u", "service"), MemoryScope("t", "u")),   # a bot
        (_ctx("t", "other"), MemoryScope("t", "u")),          # human, but not the owner
        (_ctx("other", "u"), MemoryScope("t", "u")),          # tenant mismatch
    ],
)
def test_human_gate_rejects(ctx, scope) -> None:
    with pytest.raises(L3ConfirmError):
        assert_human_owner(ctx, scope)


def test_human_gate_type_guards() -> None:
    with pytest.raises(L3ConfirmError):
        assert_human_owner("not a ctx", MemoryScope("t", "u"))  # type: ignore[arg-type]


# ── the L3 read kill-switch is default OFF ───────────────────────────────────
def test_l3_read_default_off(monkeypatch) -> None:
    monkeypatch.delenv("OMNISIGHT_SORA_L3_READ", raising=False)
    assert l3_read_enabled() is False
    monkeypatch.setenv("OMNISIGHT_SORA_L3_READ", "1")
    assert l3_read_enabled() is True


# ── PG: confirm → publish (atomic, eval-gated, human-owner) ──────────────────
async def _seed(pool) -> str:
    t = f"t-cf-{uuid.uuid4().hex}"
    async with pool.acquire() as c:
        await c.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", t)
    return t


async def _cleanup(pool, tenant, *users) -> None:
    async with pool.acquire() as c:
        for u in users:
            await hard_erase_user(c, _ctx(tenant, u), MemoryScope(tenant, u))
            await c.execute("DELETE FROM l3_erasure_audit WHERE tenant_id=$1", tenant)
        await c.execute("DELETE FROM tenants WHERE id=$1", tenant)


@pytest.mark.asyncio
async def test_confirm_publishes_the_fact(pg_test_pool) -> None:
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            eid = await record_eval(c, scope, fact_id=fid, decision=MemorySafetyDecision("promote", ()))
            res = await confirm_and_publish(c, _ctx(t, "u"), scope, eval_run_id=eid, fact_id=fid)
            assert res.fact_id == fid and res.revision == 0
            live = await list_facts(c, scope, state="promoted")
            assert len(live) == 1 and live[0].id == fid   # now LIVE
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_confirm_rejects_non_owner_before_any_write(pg_test_pool) -> None:
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            eid = await record_eval(c, scope, fact_id=fid, decision=MemorySafetyDecision("promote", ()))
            with pytest.raises(L3ConfirmError):  # a different user cannot confirm
                await confirm_and_publish(c, _ctx(t, "attacker"), scope, eval_run_id=eid, fact_id=fid)
            assert await list_facts(c, scope, state="promoted") == []  # still quarantined
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_confirm_on_a_reject_eval_rolls_back(pg_test_pool) -> None:
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            reject_eid = await record_eval(
                c, scope, fact_id=fid, decision=MemorySafetyDecision("reject", ("action_influence:x",))
            )
            with pytest.raises(Exception):  # noqa: B017 — the eval-gate rejects; the txn rolls back
                await confirm_and_publish(c, _ctx(t, "u"), scope, eval_run_id=reject_eid, fact_id=fid)
            assert await list_facts(c, scope, state="promoted") == []   # not promoted
            assert len(await list_facts(c, scope, state="quarantined")) == 1
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_confirm_rolls_back_approval_when_promote_fails(pg_test_pool) -> None:
    # if the approval INSERT succeeds but promote raises (fact already promoted),
    # the whole txn rolls back — NO orphan approval survives.
    from backend.agents.u6_l3_store import promote_fact
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            eid = await record_eval(c, scope, fact_id=fid, decision=MemorySafetyDecision("promote", ()))
            await promote_fact(c, scope, fid)  # pre-promote → confirm's promote will raise
            with pytest.raises(Exception):  # noqa: B017 — promote_fact raises; the txn rolls back
                await confirm_and_publish(c, _ctx(t, "u"), scope, eval_run_id=eid, fact_id=fid)
            n = await c.fetchval(
                "SELECT count(*) FROM l3_approvals WHERE tenant_id=$1 AND user_id=$2 AND fact_id=$3",
                t, "u", fid,
            )
            assert n == 0   # approval rolled back with the failed promote
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_revoke_supersedes_a_live_fact(pg_test_pool) -> None:
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            eid = await record_eval(c, scope, fact_id=fid, decision=MemorySafetyDecision("promote", ()))
            await confirm_and_publish(c, _ctx(t, "u"), scope, eval_run_id=eid, fact_id=fid)
            assert await revoke_fact(c, _ctx(t, "u"), scope, fid) is True
            assert await list_facts(c, scope, state="promoted") == []   # removed from live
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_hard_erase_clears_facts_and_eval_ledger(pg_test_pool) -> None:
    from backend.agents.u6_l3_eval_adapter import get_eval_run
    t = await _seed(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            eid = await record_eval(c, scope, fact_id=fid, decision=MemorySafetyDecision("promote", ()))
            res = await hard_erase_user(c, _ctx(t, "u"), scope)
            assert res.facts == 1 and res.eval_runs == 1
            assert await list_facts(c, scope, state="quarantined") == []   # facts gone
            assert await get_eval_run(c, scope, eid) is None               # ledger gone
    finally:
        await _cleanup(pg_test_pool, t, "u")

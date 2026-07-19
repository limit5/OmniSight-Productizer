"""U6-5b — per-user L3 eval/approval ledger adapter tests (offline + PG-gated).

Pins the persistence of a U6-5a memory-safety decision + the eval-gate (an approval
only for a ``promote`` eval), per-user isolation, and erase.
"""
from __future__ import annotations

import uuid

import pytest

from backend.agents.u6_l3_eval_adapter import (
    L3EvalError,
    erase_user_evals,
    get_eval_run,
    record_approval,
    record_eval,
)
from backend.agents.u6_memory_safety_eval import MemorySafetyDecision
from backend.agents.u6_memory_scope import MemoryScope

_PROMOTE = MemorySafetyDecision("promote", ())
_REJECT = MemorySafetyDecision("reject", ("action_influence:write_file",))


# ── offline guards ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_record_eval_rejects_bad_decision() -> None:
    with pytest.raises(L3EvalError):
        await record_eval(None, MemoryScope("t", "u"), fact_id="l3f-1", decision="promote")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_record_eval_rejects_empty_fact_id() -> None:
    with pytest.raises(L3EvalError):
        await record_eval(None, MemoryScope("t", "u"), fact_id="", decision=_PROMOTE)


# ── PG helpers ───────────────────────────────────────────────────────────────
async def _seed_tenant(pool) -> str:
    t = f"t-ev-{uuid.uuid4().hex}"
    async with pool.acquire() as c:
        await c.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", t)
    return t


async def _cleanup(pool, tenant: str, *users: str) -> None:
    async with pool.acquire() as c:
        for user in users:
            await erase_user_evals(c, MemoryScope(tenant, user))
        await c.execute("DELETE FROM tenants WHERE id = $1", tenant)


# ── PG: record + round-trip ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_record_eval_round_trip(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            eid = await record_eval(c, scope, fact_id="l3f-x", decision=_REJECT)
            got = await get_eval_run(c, scope, eid)
            assert got and got["decision"] == "reject" and got["fact_id"] == "l3f-x"
            assert got["eval_kind"] == "memory_safety"
    finally:
        await _cleanup(pg_test_pool, t, "u")


# ── PG: the eval-gate — approval only for a promote eval ─────────────────────
@pytest.mark.asyncio
async def test_approval_requires_a_promote_eval(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            reject_eid = await record_eval(c, scope, fact_id="l3f-r", decision=_REJECT)
            with pytest.raises(L3EvalError):  # cannot approve a rejected eval
                await record_approval(c, scope, eval_run_id=reject_eid, fact_id="l3f-r", approved_by="u")

            promote_eid = await record_eval(c, scope, fact_id="l3f-p", decision=_PROMOTE)
            aid = await record_approval(c, scope, eval_run_id=promote_eid, fact_id="l3f-p", approved_by="u")
            assert aid.startswith("l3ap-")

            with pytest.raises(L3EvalError):  # fact_id must match the eval run
                await record_approval(c, scope, eval_run_id=promote_eid, fact_id="l3f-OTHER", approved_by="u")
    finally:
        await _cleanup(pg_test_pool, t, "u")


# ── PG: per-user isolation + erase ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_eval_runs_are_user_scoped(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    a, b = MemoryScope(t, "user-a"), MemoryScope(t, "user-b")
    try:
        async with pg_test_pool.acquire() as c:
            eid = await record_eval(c, a, fact_id="l3f-a", decision=_PROMOTE)
            assert await get_eval_run(c, b, eid) is None      # B can't read A's eval run
            assert await get_eval_run(c, a, eid) is not None
    finally:
        await _cleanup(pg_test_pool, t, "user-a", "user-b")


@pytest.mark.asyncio
async def test_rls_defense_in_depth_denies_predicate_free_read(pg_test_pool) -> None:
    # DEFENSE-IN-DEPTH: as a NON-SUPERUSER (superusers bypass RLS), a predicate-free
    # read scoped to B returns only B's eval runs — the FORCED policy blocks a
    # cross-user read even if an app predicate is ever omitted.
    t = await _seed_tenant(pg_test_pool)
    try:
        async with pg_test_pool.acquire() as c:
            await record_eval(c, MemoryScope(t, "u-a"), fact_id="l3f-a", decision=_PROMOTE)
            await record_eval(c, MemoryScope(t, "u-b"), fact_id="l3f-b", decision=_PROMOTE)
            await c.execute("DROP ROLE IF EXISTS l3ev_rls_probe")
            await c.execute("CREATE ROLE l3ev_rls_probe NOSUPERUSER")
            await c.execute("GRANT USAGE ON SCHEMA public TO l3ev_rls_probe")
            await c.execute("GRANT SELECT ON l3_eval_runs TO l3ev_rls_probe")
            try:
                await c.execute("SET ROLE l3ev_rls_probe")
                async with c.transaction():
                    await c.execute("SELECT set_config('app.tenant_id', $1, true)", t)
                    await c.execute("SELECT set_config('app.user_id', $1, true)", "u-b")
                    seen = await c.fetch("SELECT user_id FROM l3_eval_runs")  # NO predicate
                assert seen and all(r["user_id"] == "u-b" for r in seen), \
                    f"RLS leak: {[r['user_id'] for r in seen]}"
                async with c.transaction():
                    unset = await c.fetch("SELECT id FROM l3_eval_runs")  # unset → fail-closed
                assert unset == []
            finally:
                await c.execute("RESET ROLE")
                for stmt in (
                    "REVOKE ALL ON SCHEMA public FROM l3ev_rls_probe",
                    "REVOKE ALL ON l3_eval_runs FROM l3ev_rls_probe",
                ):
                    try:
                        await c.execute(stmt)
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        await _cleanup(pg_test_pool, t, "u-a", "u-b")
        async with pg_test_pool.acquire() as c:
            try:
                await c.execute("DROP ROLE IF EXISTS l3ev_rls_probe")
            except Exception:  # noqa: BLE001
                pass


@pytest.mark.asyncio
async def test_erase_user_evals_clears_runs_and_approvals(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            eid = await record_eval(c, scope, fact_id="l3f-1", decision=_PROMOTE)
            await record_approval(c, scope, eval_run_id=eid, fact_id="l3f-1", approved_by="u")
            runs, approvals = await erase_user_evals(c, scope)
            assert runs == 1 and approvals == 1
            assert await get_eval_run(c, scope, eid) is None
    finally:
        await _cleanup(pg_test_pool, t, "u")

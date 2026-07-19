"""U6-5a — memory-safety eval + L3 producer tests (offline + PG-gated).

Pins the RB3 action-influence negative control (a mutating decision through the
REAL guard is byte-identical with/without the candidate injected), that the eval
has TEETH (rejects when a fact WOULD move a verdict), and the producer write-gate
pipeline (triage → schema → safety → quarantine).
"""
from __future__ import annotations

import types
import uuid

import pytest

from backend.agents.u6_fact_schema import FactType, Sensitivity
from backend.agents.u6_l3_producer import produce, produce_and_quarantine
from backend.agents.u6_l3_store import list_facts
from backend.agents.u6_memory_safety_eval import (
    action_influence_divergences,
    evaluate_memory_safety,
)
from backend.agents.u6_memory_scope import MemoryScope

_SCOPE = MemoryScope("omnisight-self", "user-1")


def _draft(value="named_pipes", **kw):
    base = dict(
        fact_type=FactType.PREFERENCE, subject="user", predicate="preferred_ipc",
        value=value, source_span="chat:m1",
    )
    base.update(kw)
    return base


# ── the negative control against the REAL guard ──────────────────────────────
def test_valid_fact_is_action_inert_and_promotes() -> None:
    outcome = produce(_SCOPE, **_draft())
    assert outcome.accepted is True and outcome.fact is not None
    d = evaluate_memory_safety(outcome.fact, _SCOPE)
    assert d.promoted is True and d.reasons == ()
    # zero probe tools diverged with vs without the candidate injected.
    assert action_influence_divergences(outcome.fact, _SCOPE) == ()


@pytest.mark.parametrize(
    "value,predicate,ftype,subject",
    [
        ("named_pipes", "preferred_ipc", FactType.PREFERENCE, "user"),
        ("en-US", "preferred_language", FactType.PREFERENCE, "user"),
        ("Asia/Taipei", "timezone", FactType.PROFILE, "user"),
        ("develop", "default_branch", FactType.PROJECT_CONTEXT, "repo:omnisight"),
    ],
)
def test_every_valid_fact_is_inert(value, predicate, ftype, subject) -> None:
    o = produce(_SCOPE, fact_type=ftype, subject=subject, predicate=predicate,
                value=value, source_span="s")
    assert o.accepted and evaluate_memory_safety(o.fact, _SCOPE).promoted


def test_eval_has_teeth_rejects_when_a_fact_moves_a_verdict(monkeypatch) -> None:
    # simulate a broken kernel where injecting memory FLIPS a mutating op to allow;
    # the eval MUST reject (this is the property the negative control guards).
    def _biased(ctx, req, ids):
        return types.SimpleNamespace(
            verdict="allow" if ids else "requires_grant", reason="x",
        )

    monkeypatch.setattr("backend.agents.u6_memory_safety_eval.authorize_action", _biased)
    o = produce(_SCOPE, **_draft())  # produce runs before the patch on its own fact...
    # build the fact directly to evaluate under the patched guard:
    from backend.agents.u6_fact_schema import Fact
    fact = Fact(FactType.PREFERENCE, "user", "preferred_ipc", "tcp", source_span="s")
    d = evaluate_memory_safety(fact, _SCOPE)
    assert d.promoted is False
    assert d.reasons and d.reasons[0].startswith("action_influence:")
    assert o  # (produce above ran against the real guard; unused beyond smoke)


def test_eval_type_guards() -> None:
    with pytest.raises(TypeError):
        action_influence_divergences("not a fact", _SCOPE)  # type: ignore[arg-type]


# ── producer write-gate pipeline ─────────────────────────────────────────────
def test_producer_rejects_junk_value() -> None:
    o = produce(_SCOPE, **_draft(value="none", predicate="preferred_editor"))
    assert o.accepted is False and o.reason.startswith("triage_reject")


def test_producer_rejects_authority_predicate() -> None:
    o = produce(_SCOPE, **_draft(predicate="skips_reviews", value="true"))
    assert o.accepted is False and o.reason.startswith(("triage_reject", "schema_reject"))


def test_producer_rejects_out_of_enum_value() -> None:
    o = produce(_SCOPE, **_draft(value="carrier_pigeon"))
    assert o.accepted is False


def test_producer_preserves_declared_sensitivity() -> None:
    o = produce(_SCOPE, **_draft(value="tcp"), declared_sensitivity=Sensitivity.SENSITIVE)
    assert o.accepted and o.fact.sensitivity is Sensitivity.SENSITIVE


# ── PG: produce_and_quarantine lands QUARANTINED (not live) ──────────────────
async def _seed_tenant(pool) -> str:
    t = f"t-prod-{uuid.uuid4().hex}"
    async with pool.acquire() as c:
        await c.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", t)
    return t


@pytest.mark.asyncio
async def test_produce_and_quarantine_lands_quarantined(pg_test_pool) -> None:
    from backend.agents.u6_l3_store import erase_user
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            outcome, fid = await produce_and_quarantine(c, scope, **_draft())
            assert outcome.accepted and fid is not None
            assert await list_facts(c, scope, state="promoted") == []   # NOT live
            q = await list_facts(c, scope, state="quarantined")
            assert len(q) == 1 and q[0].id == fid
    finally:
        async with pg_test_pool.acquire() as c:
            await erase_user(c, MemoryScope(t, "u"))
            await c.execute("DELETE FROM l3_erasure_audit WHERE tenant_id=$1", t)
            await c.execute("DELETE FROM tenants WHERE id=$1", t)


@pytest.mark.asyncio
async def test_rejected_candidate_is_not_inserted(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            outcome, fid = await produce_and_quarantine(c, scope, **_draft(value="none", predicate="preferred_editor"))
            assert outcome.accepted is False and fid is None
            assert await list_facts(c, scope, state="quarantined") == []
    finally:
        async with pg_test_pool.acquire() as c:
            await c.execute("DELETE FROM tenants WHERE id=$1", t)

"""U6-4 — L3 per-user erasable store tests.

Offline crypto tests (seal/open round-trip, crypto-shred unrecoverability) always
run; the store / RLS / hard-erase tests use the real-PG fixture and skip cleanly
when ``OMNI_TEST_PG_URL`` is unset.
"""
from __future__ import annotations

import uuid

import pytest

from backend.agents.u6_fact_schema import Fact, FactType, Sensitivity
from backend.agents.u6_l3_store import (
    L3StoreError,
    StoredFact,
    _new_fact_id,
    _open_to_fact,
    _seal_payload,
    erase_user,
    insert_fact,
    list_facts,
    promote_fact,
)
from backend.agents.u6_memory_scope import MemoryScope


def _fact(value: str = "named_pipes") -> Fact:
    return Fact(
        FactType.PREFERENCE, "user", "preferred_ipc", value,
        source_span="chat:m1", sensitivity=Sensitivity.SENSITIVE,
    )


# ── offline: crypto seal/open + shred unrecoverability ───────────────────────
def test_fact_id_prefixed() -> None:
    assert _new_fact_id().startswith("l3f-")


def test_seal_open_round_trip_reconstructs_fact() -> None:
    scope = MemoryScope("t", "u")
    sealed = _seal_payload(scope, _fact("tcp"))
    row = {
        "sealed_ciphertext": sealed.ciphertext, "dek_ref": sealed.dek_ref,
        "fact_type": "preference", "subject": "user", "predicate": "preferred_ipc",
        "sensitivity": "sensitive", "valid_from": None, "valid_until": None,
    }
    got = _open_to_fact(row, scope)
    assert got.value == "tcp" and got.source_span == "chat:m1"
    assert got.sensitivity is Sensitivity.SENSITIVE


def test_crypto_shred_makes_value_unrecoverable() -> None:
    scope = MemoryScope("t", "u")
    sealed = _seal_payload(scope, _fact())
    shredded = {
        "sealed_ciphertext": sealed.ciphertext, "dek_ref": {},  # dek_ref destroyed
        "fact_type": "preference", "subject": "user", "predicate": "preferred_ipc",
        "sensitivity": "normal", "valid_from": None, "valid_until": None,
    }
    with pytest.raises(Exception):  # noqa: B017 — any failure; the point is it CANNOT open
        _open_to_fact(shredded, scope)


def test_open_rejects_clear_semantic_key_tamper() -> None:
    # the sealed key is preferred_ipc; a swapped CLEAR predicate is caught at open.
    scope = MemoryScope("t", "u")
    sealed = _seal_payload(scope, _fact("named_pipes"))
    tampered = {
        "sealed_ciphertext": sealed.ciphertext, "dek_ref": sealed.dek_ref,
        "fact_type": "preference", "subject": "user", "predicate": "preferred_editor",
        "sensitivity": "normal", "valid_from": None, "valid_until": None,
    }
    with pytest.raises(L3StoreError):
        _open_to_fact(tampered, scope)


# ── PG helpers ───────────────────────────────────────────────────────────────
async def _seed_tenant(pool) -> str:
    t = f"t-l3-{uuid.uuid4().hex}"
    async with pool.acquire() as c:
        await c.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", t)
    return t


async def _cleanup(pool, tenant: str, *users: str) -> None:
    async with pool.acquire() as c:
        for user in users:
            await erase_user(c, MemoryScope(tenant, user))
        await c.execute("DELETE FROM l3_erasure_audit WHERE tenant_id = $1", tenant)
        await c.execute("DELETE FROM tenants WHERE id = $1", tenant)


# ── PG: store CRUD + one-current-value ───────────────────────────────────────
@pytest.mark.asyncio
async def test_insert_is_quarantined_not_live(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            fid = await insert_fact(c, scope, _fact())
            assert await list_facts(c, scope, state="promoted") == []   # not live
            q = await list_facts(c, scope, state="quarantined")
            assert len(q) == 1 and q[0].id == fid and q[0].fact.value == "named_pipes"
            assert isinstance(q[0], StoredFact)
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_promote_supersedes_one_current_value(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            f1 = await insert_fact(c, scope, _fact("named_pipes"))
            f2 = await insert_fact(c, scope, _fact("tcp"))  # same semantic key
            assert await promote_fact(c, scope, f1) == 0
            assert await promote_fact(c, scope, f2) == 1    # supersedes f1
            live = await list_facts(c, scope, state="promoted")
            assert len(live) == 1 and live[0].fact.value == "tcp"   # one current value
            assert len(await list_facts(c, scope, state="superseded")) == 1
    finally:
        await _cleanup(pg_test_pool, t, "u")


# ── PG: cross-user isolation — app-predicate (primary) + RLS (defense-in-depth) ─
@pytest.mark.asyncio
async def test_app_predicate_cross_user_isolation(pg_test_pool) -> None:
    # PRIMARY isolation: every store query carries explicit tenant+user predicates,
    # so B's scoped read returns NONE of A's rows regardless of the RLS layer.
    t = await _seed_tenant(pg_test_pool)
    a, b = MemoryScope(t, "user-a"), MemoryScope(t, "user-b")
    try:
        async with pg_test_pool.acquire() as c:
            await insert_fact(c, a, _fact())
            assert await list_facts(c, b, state="quarantined") == []      # none of A's
            assert len(await list_facts(c, a, state="quarantined")) == 1  # A sees its own
    finally:
        await _cleanup(pg_test_pool, t, "user-a", "user-b")


@pytest.mark.asyncio
async def test_rls_defense_in_depth_denies_predicate_free_read(pg_test_pool) -> None:
    # DEFENSE-IN-DEPTH: as a NON-SUPERUSER (superusers bypass RLS), a PREDICATE-FREE
    # read scoped to B must return only B's rows — the FORCED RLS policy blocks a
    # cross-user read even if an application predicate is ever omitted. This is the
    # U6-0b deferred "RLS cross-user-read-denied" DoD.
    t = await _seed_tenant(pg_test_pool)
    try:
        async with pg_test_pool.acquire() as c:
            await insert_fact(c, MemoryScope(t, "u-a"), _fact())
            await insert_fact(c, MemoryScope(t, "u-b"), _fact("tcp"))
            await c.execute("DROP ROLE IF EXISTS l3_rls_probe")
            await c.execute("CREATE ROLE l3_rls_probe NOSUPERUSER")
            await c.execute("GRANT USAGE ON SCHEMA public TO l3_rls_probe")
            await c.execute("GRANT SELECT ON l3_facts TO l3_rls_probe")
            try:
                await c.execute("SET ROLE l3_rls_probe")
                async with c.transaction():
                    await c.execute("SELECT set_config('app.tenant_id', $1, true)", t)
                    await c.execute("SELECT set_config('app.user_id', $1, true)", "u-b")
                    seen = await c.fetch("SELECT user_id FROM l3_facts")  # NO predicate → RLS only
                assert seen and all(r["user_id"] == "u-b" for r in seen), \
                    f"RLS leak: {[r['user_id'] for r in seen]}"
                async with c.transaction():
                    # NO app.* set → current_setting(...,true)=NULL → fail-closed (0 rows, not all)
                    unset = await c.fetch("SELECT id FROM l3_facts")
                assert unset == [], f"RLS fail-OPEN when session unset: saw {len(unset)} rows"
            finally:
                await c.execute("RESET ROLE")
                for stmt in (
                    "REVOKE ALL ON SCHEMA public FROM l3_rls_probe",
                    "REVOKE ALL ON l3_facts FROM l3_rls_probe",
                ):
                    try:
                        await c.execute(stmt)
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        await _cleanup(pg_test_pool, t, "u-a", "u-b")
        async with pg_test_pool.acquire() as c:
            try:
                await c.execute("DROP ROLE IF EXISTS l3_rls_probe")
            except Exception:  # noqa: BLE001
                pass


# ── PG: hard-erase (crypto-shred) + tombstone + idempotent + RLS-scoped ──────
@pytest.mark.asyncio
async def test_erase_shreds_tombstones_and_is_idempotent(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    scope = MemoryScope(t, "u")
    try:
        async with pg_test_pool.acquire() as c:
            await insert_fact(c, scope, _fact("named_pipes"))
            await insert_fact(c, scope, _fact("tcp"))
            assert await erase_user(c, scope) == 2
            for st in ("quarantined", "promoted", "superseded"):
                assert await list_facts(c, scope, state=st) == []      # gone
            tomb = await c.fetchrow(
                "SELECT fact_count, user_ref FROM l3_erasure_audit WHERE tenant_id = $1", t
            )
            assert tomb["fact_count"] == 2                              # content-free tombstone
            assert tomb["user_ref"] != "u" and len(tomb["user_ref"]) == 64  # pseudonym, not raw id
            assert await erase_user(c, scope) == 0                      # idempotent
    finally:
        await _cleanup(pg_test_pool, t, "u")


@pytest.mark.asyncio
async def test_erase_is_user_scoped(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    a, b = MemoryScope(t, "user-a"), MemoryScope(t, "user-b")
    try:
        async with pg_test_pool.acquire() as c:
            await insert_fact(c, a, _fact())
            await insert_fact(c, b, _fact("tcp"))
            assert await erase_user(c, a) == 1                          # only A
            assert await list_facts(c, a, state="quarantined") == []
            assert len(await list_facts(c, b, state="quarantined")) == 1  # B intact
    finally:
        await _cleanup(pg_test_pool, t, "user-a", "user-b")

"""U6-6 — /memories route tests (server-authed identity + owner isolation).

The endpoints are called directly (like test_preferences) with a stub ``User``
and a real ``pg_test_pool`` connection, so the server-side scope/ctx
construction + the human-owner gate are exercised end to end without HTTP.
"""

from __future__ import annotations

import uuid

import pytest

from backend.agents.u6_l3_eval_adapter import erase_user_evals
from backend.agents.u6_l3_producer import produce_quarantine_and_record
from backend.agents.u6_l3_store import erase_user, list_facts
from backend.agents.u6_memory_scope import MemoryScope
from backend.auth import User
from backend.routers import memories as m
from fastapi import HTTPException

_DRAFT = dict(
    fact_type=__import__("backend.agents.u6_fact_schema", fromlist=["FactType"]).FactType.PREFERENCE,
    subject="user", predicate="preferred_ipc", value="named_pipes", source_span="chat:s:m",
)


def _user(uid: str, tenant: str) -> User:
    return User(id=uid, email=f"{uid}@x", name=uid, role="operator", tenant_id=tenant)


async def _seed_tenant(pool) -> str:
    t = f"t-mem-{uuid.uuid4().hex}"
    async with pool.acquire() as c:
        await c.execute("INSERT INTO tenants (id, name, plan) VALUES ($1, $1, 'free')", t)
    return t


async def _cleanup(pool, tenant, *users) -> None:
    async with pool.acquire() as c:
        for u in users:
            await erase_user(c, MemoryScope(tenant, u))
            await erase_user_evals(c, MemoryScope(tenant, u))  # tenant FK on l3_eval_runs
        await c.execute("DELETE FROM l3_erasure_audit WHERE tenant_id=$1", tenant)
        await c.execute("DELETE FROM tenants WHERE id=$1", tenant)


# ── scope/ctx are built from the session, never the client ───────────────────
def test_scope_and_ctx_from_session_only() -> None:
    scope, ctx = m._scope_and_ctx(_user("alice", "t-1"))
    assert scope.tenant_id == "t-1" and scope.user_id == "alice"
    assert ctx.principal_type == "human"
    assert ctx.actor_id == "alice" and ctx.tenant_id == "t-1"
    assert ctx.authorization_source == "memories_ui"


# ── L3b: propose (user-explicit candidate ingress) ───────────────────────────
@pytest.mark.asyncio
async def test_propose_lands_quarantined_and_shows_in_pending(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            body = m.ProposeMemoryBody(
                fact_type="preference", predicate="preferred_ipc", value="named_pipes"
            )
            res = await m.propose_memory(body, user=u, conn=c)
            assert res["proposed"] and res["status"] == "quarantined"
            assert res["rendered"] == "user preferred_ipc named_pipes"
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []
            pending = await m.list_pending_memories(user=u, conn=c)
            assert [p["fact_id"] for p in pending["pending"]] == [res["fact_id"]]
            # confirmable end-to-end (propose recorded the eval)
            await m.confirm_memory(res["fact_id"], user=u, conn=c)
            live = await m.list_live_memories(user=u, conn=c)
            assert [x["fact_id"] for x in live["memories"]] == [res["fact_id"]]
    finally:
        await _cleanup(pg_test_pool, t, "owner")


@pytest.mark.asyncio
async def test_propose_rejects_unregistered_predicate_422(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            body = m.ProposeMemoryBody(
                fact_type="preference", predicate="skips_reviews", value="always"
            )
            with pytest.raises(HTTPException) as ei:
                await m.propose_memory(body, user=u, conn=c)
            assert ei.value.status_code == 422
            assert (await m.list_pending_memories(user=u, conn=c))["pending"] == []
    finally:
        await _cleanup(pg_test_pool, t, "owner")


@pytest.mark.asyncio
async def test_propose_invalid_fact_type_422(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            body = m.ProposeMemoryBody(
                fact_type="authority", predicate="preferred_ipc", value="named_pipes"
            )
            with pytest.raises(HTTPException) as ei:
                await m.propose_memory(body, user=u, conn=c)
            assert ei.value.status_code == 422
    finally:
        await _cleanup(pg_test_pool, t, "owner")


def test_propose_body_has_no_client_source_span() -> None:
    # The client cannot supply source_span — it is server-set.
    assert "source_span" not in m.ProposeMemoryBody.model_fields


@pytest.mark.asyncio
async def test_discard_rejects_pending_candidate(pg_test_pool) -> None:
    # The /pending discard control: a quarantined candidate the user chose not
    # to confirm is rejected (+ key-shred), leaving pending, never reaching live.
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            body = m.ProposeMemoryBody(
                fact_type="preference", predicate="preferred_ipc", value="named_pipes"
            )
            res = await m.propose_memory(body, user=u, conn=c)
            fid = res["fact_id"]
            out = await m.discard_memory(fid, user=u, conn=c)
            assert out["discarded"] and out["fact_id"] == fid
            assert (await m.list_pending_memories(user=u, conn=c))["pending"] == []
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []
            # a second discard (or a live/absent id) → 404, never 500
            with pytest.raises(HTTPException) as ei:
                await m.discard_memory(fid, user=u, conn=c)
            assert ei.value.status_code == 404
    finally:
        await _cleanup(pg_test_pool, t, "owner")


# ── L3c: §2.F server-side gates (rate limit + sensitive ack) ─────────────────
@pytest.mark.asyncio
async def test_write_limit_dependency_429_when_limiter_denies(monkeypatch) -> None:
    class _DenyLimiter:
        def allow(self, *, key, capacity, window_seconds):
            assert key.startswith("mem:write:")
            return False, 42.0

    import backend.rate_limit as rl

    monkeypatch.setattr(rl, "get_limiter", lambda: _DenyLimiter())
    with pytest.raises(HTTPException) as ei:
        await m._memories_write_limit(user=_user("u", "t"))
    assert ei.value.status_code == 429
    assert ei.value.headers["Retry-After"] == "42"


@pytest.mark.asyncio
async def test_write_limit_dependency_passes_user_through(monkeypatch) -> None:
    class _AllowLimiter:
        def allow(self, *, key, capacity, window_seconds):
            return True, 0.0

    import backend.rate_limit as rl

    monkeypatch.setattr(rl, "get_limiter", lambda: _AllowLimiter())
    u = _user("u", "t")
    assert await m._memories_write_limit(user=u) is u


@pytest.mark.asyncio
async def test_sensitive_confirm_requires_explicit_ack(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            body = m.ProposeMemoryBody(
                fact_type="preference", predicate="preferred_ipc",
                value="named_pipes", declared_sensitivity="sensitive",
            )
            res = await m.propose_memory(body, user=u, conn=c)
            fid = res["fact_id"]
            # no ack → 428 Precondition Required; still quarantined
            with pytest.raises(HTTPException) as ei:
                await m.confirm_memory(fid, user=u, conn=c)
            assert ei.value.status_code == 428
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []
            # explicit ack → confirmed
            ok = await m.confirm_memory(
                fid, body=m.ConfirmMemoryBody(acknowledge_sensitive=True), user=u, conn=c
            )
            assert ok["confirmed"]
    finally:
        await _cleanup(pg_test_pool, t, "owner")


# ── full lifecycle: pending → confirm → live → revoke ────────────────────────
@pytest.mark.asyncio
async def test_pending_confirm_live_revoke_roundtrip(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            _o, fid, _e = await produce_quarantine_and_record(c, MemoryScope(t, "owner"), **_DRAFT)

            pending = await m.list_pending_memories(user=u, conn=c)
            assert [p["fact_id"] for p in pending["pending"]] == [fid]
            assert pending["pending"][0]["rendered"] == "user preferred_ipc named_pipes"
            assert pending["pending"][0]["eval_run_id"]  # server-resolved
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []

            confirmed = await m.confirm_memory(fid, user=u, conn=c)
            assert confirmed["confirmed"] and confirmed["fact_id"] == fid

            live = await m.list_live_memories(user=u, conn=c)
            assert [x["fact_id"] for x in live["memories"]] == [fid]
            assert (await m.list_pending_memories(user=u, conn=c))["pending"] == []

            revoked = await m.revoke_memory(fid, user=u, conn=c)
            assert revoked["revoked"] and revoked["fact_id"] == fid
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []
    finally:
        await _cleanup(pg_test_pool, t, "owner")


# ── a user can only act on THEIR OWN memory ──────────────────────────────────
@pytest.mark.asyncio
async def test_another_user_cannot_see_or_confirm(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    intruder = _user("intruder", t)
    try:
        async with pg_test_pool.acquire() as c:
            _o, fid, _e = await produce_quarantine_and_record(c, MemoryScope(t, "owner"), **_DRAFT)

            # the intruder's scoped listing sees nothing of the owner's
            assert (await m.list_pending_memories(user=intruder, conn=c))["pending"] == []
            # and cannot confirm the owner's fact — server resolves NO eval in
            # the intruder's scope ⇒ 404 (never reaches the owner's row)
            with pytest.raises(HTTPException) as ei:
                await m.confirm_memory(fid, user=intruder, conn=c)
            assert ei.value.status_code == 404
            # the owner's candidate is untouched
            assert len(await list_facts(c, MemoryScope(t, "owner"), state="quarantined")) == 1
    finally:
        await _cleanup(pg_test_pool, t, "owner", "intruder")


@pytest.mark.asyncio
async def test_double_confirm_is_client_error_not_500(pg_test_pool) -> None:
    # A second confirm (double-click / retry) must be a CLIENT error, never an
    # uncaught 500 (audit MAJOR-1). Deterministically it's now 404 (the fact
    # left the quarantined set, so the L3c candidate lookup refuses first); a
    # mid-flight race that passes the lookup but loses at promote_fact still
    # lands in the 409 handler.
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            _o, fid, _e = await produce_quarantine_and_record(c, MemoryScope(t, "owner"), **_DRAFT)
            first = await m.confirm_memory(fid, user=u, conn=c)
            assert first["confirmed"]
            with pytest.raises(HTTPException) as ei:
                await m.confirm_memory(fid, user=u, conn=c)
            assert ei.value.status_code in (404, 409)
    finally:
        await _cleanup(pg_test_pool, t, "owner")


@pytest.mark.asyncio
async def test_confirm_unknown_fact_404(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            with pytest.raises(HTTPException) as ei:
                await m.confirm_memory("l3f-nope", user=u, conn=c)
            assert ei.value.status_code == 404
    finally:
        await _cleanup(pg_test_pool, t, "owner")


@pytest.mark.asyncio
async def test_revoke_unknown_fact_404(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            with pytest.raises(HTTPException) as ei:
                await m.revoke_memory("l3f-nope", user=u, conn=c)
            assert ei.value.status_code == 404
    finally:
        await _cleanup(pg_test_pool, t, "owner")


@pytest.mark.asyncio
async def test_erase_clears_everything(pg_test_pool) -> None:
    t = await _seed_tenant(pg_test_pool)
    u = _user("owner", t)
    try:
        async with pg_test_pool.acquire() as c:
            _o, fid, _e = await produce_quarantine_and_record(c, MemoryScope(t, "owner"), **_DRAFT)
            await m.confirm_memory(fid, user=u, conn=c)
            res = await m.erase_memories(user=u, conn=c)
            assert res["erased"] and res["facts"] >= 1
            assert (await m.list_live_memories(user=u, conn=c))["memories"] == []
            assert (await m.list_pending_memories(user=u, conn=c))["pending"] == []
    finally:
        await _cleanup(pg_test_pool, t, "owner")

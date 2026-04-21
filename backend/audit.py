"""Phase 53 / I8 — audit & compliance layer with per-tenant hash chains.

Append-only hash-chained log of every state-changing operation worth
auditing. Each tenant maintains an independent hash chain starting from
its own genesis row (empty prev_hash). This prevents cross-tenant chain
interference and enables per-tenant integrity verification.

Hash chain: each row's `curr_hash` is `sha256(prev_hash || canonical(row))`.
A tampered row breaks the chain from that point onward. `verify_chain()`
re-walks and reports the first divergence.

Persistence is best-effort with respect to the calling main flow:
audit failures log a warning and let the caller proceed (Phase 50-Fix
philosophy: don't kill the train because the receipt printer ran out
of paper).

Public API:
    await audit.log(action, entity_kind, entity_id, before, after, actor=...)
    await audit.query(since=..., actor=..., entity_kind=..., limit=...)
    await audit.verify_chain(tenant_id=...)  # per-tenant chain verify
    await audit.verify_all_chains()          # all tenants at once

CLI:
    python -m backend.audit verify [--tenant TENANT_ID]
    python -m backend.audit verify-all
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Optional

from backend.db_context import tenant_insert_value, tenant_where_pg

logger = logging.getLogger(__name__)

# Phase-3-Runtime-v2 SP-4.1 (2026-04-20): ported to native asyncpg
# + pool. Chain write serialisation is handled ENTIRELY by PG's
# ``pg_advisory_xact_lock(hashtext(tenant_id))`` inside ``_log_impl``
# — that lock covers both cross-connection races (two pool borrows
# reading the same prev_hash) and cross-process races (`uvicorn
# --workers N`), which are the real concurrency hazards for a hash
# chain.
#
# SP-9.2 (2026-04-21, task #82): the previous module-level
# ``asyncio.Lock`` has been removed. It was redundant on top of the
# PG advisory lock, AND it was a latent multi-event-loop bug — the
# Lock binds to whichever event loop first touches it, so once
# pytest-asyncio closes that loop (function-scoped) subsequent
# tests on fresh loops hit ``RuntimeError: <Lock> is bound to a
# different event loop``. This surfaced in the SP-9.2 multi-tenant
# isolation suite where several ``audit.log`` calls in the second-
# to-run test raised silently (swallowed by the outer ``except
# Exception`` in ``log``) and lost audit rows. Removing the Lock
# is safe because every call path into ``_log_impl`` is wrapped in
# ``conn.transaction()`` + ``pg_advisory_xact_lock(...)`` already.


def _canonical(row: dict[str, Any]) -> str:
    """Deterministic JSON serialisation for hashing. Sorted keys, no
    whitespace, ensure_ascii=False so CJK doesn't escape."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(prev_hash: str, payload_canon: str) -> str:
    return hashlib.sha256((prev_hash + payload_canon).encode("utf-8")).hexdigest()


async def _last_hash_for_tenant(conn, tenant_id: str) -> str:
    """Get the last hash in a specific tenant's chain.

    Caller must be inside ``pg_advisory_xact_lock`` for the same
    tenant (otherwise the read can be stale against a concurrent
    writer). Used only from ``_log_impl`` which holds the lock.
    """
    row = await conn.fetchrow(
        "SELECT curr_hash FROM audit_log WHERE tenant_id = $1 "
        "ORDER BY id DESC LIMIT 1",
        tenant_id,
    )
    return row["curr_hash"] if row else ""


async def _log_impl(
    conn,
    action: str,
    entity_kind: str,
    entity_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str,
    session_id: str | None,
) -> Optional[int]:
    """Core chain-write path. Must be called INSIDE ``conn.transaction()``.

    Acquires a PG advisory lock scoped to the current tenant so concurrent
    writers across different pool connections (and across multiple worker
    processes) serialise on the chain append. The lock is
    transaction-scoped — PG releases it automatically on COMMIT/ROLLBACK.
    """
    before_d = before or {}
    after_d = after or {}
    payload = {
        "action": action,
        "entity_kind": entity_kind,
        "entity_id": entity_id or "",
        "before": before_d,
        "after": after_d,
        "actor": actor,
    }
    payload_canon = _canonical(payload)
    ts = time.time()
    tid = tenant_insert_value()

    # Advisory lock keyed on the tenant chain. hashtext returns int4;
    # the key space is effectively per-tenant so concurrent chains
    # (different tenants) still append in parallel.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1))",
        f"audit-chain-{tid}",
    )
    prev = await _last_hash_for_tenant(conn, tid)
    curr = _hash(prev, payload_canon + str(round(ts, 6)))
    row = await conn.fetchrow(
        "INSERT INTO audit_log "
        "(ts, actor, action, entity_kind, entity_id, before_json, "
        " after_json, prev_hash, curr_hash, session_id, tenant_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
        "RETURNING id",
        ts, actor, action, entity_kind, entity_id or "",
        json.dumps(before_d, ensure_ascii=False),
        json.dumps(after_d, ensure_ascii=False),
        prev, curr, session_id, tid,
    )
    return row["id"] if row else None


async def log(
    action: str,
    entity_kind: str,
    entity_id: str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "system",
    session_id: str | None = None,
    conn=None,
) -> Optional[int]:
    """Append a single row. Returns the new row id, or None on failure
    (logged at warning, never raises). Chains the new row to the prior
    one's curr_hash so any post-write tampering breaks ``verify_chain``.

    ``conn`` is polymorphic — request handlers pass their
    ``Depends(get_conn)`` conn; background workers and the 18+ existing
    callers call without conn and this function borrows one from the
    pool. Either way, the append runs inside a fresh transaction with
    a tenant-scoped advisory lock.
    """
    try:
        if conn is None:
            from backend.db_pool import get_pool
            async with get_pool().acquire() as owned_conn:
                async with owned_conn.transaction():
                    return await _log_impl(
                        owned_conn, action, entity_kind, entity_id,
                        before, after, actor, session_id,
                    )
        else:
            # Nested transaction → PG savepoint; advisory lock is
            # still scoped to the outer tx (released on its commit).
            async with conn.transaction():
                return await _log_impl(
                    conn, action, entity_kind, entity_id,
                    before, after, actor, session_id,
                )
    except Exception as exc:
        logger.warning("audit.log failed (%s on %s): %s", action, entity_kind, exc)
        return None


def log_sync(action: str, entity_kind: str, entity_id: str | None,
             before: dict[str, Any] | None = None,
             after: dict[str, Any] | None = None,
             actor: str = "system",
             session_id: str | None = None) -> None:
    """Fire-and-forget wrapper for callers that aren't on an async stack
    (e.g. decision_engine.set_mode is sync). Schedules log() on the
    running loop; if there's no loop, drops with a debug message
    (typically only happens in unit-test setup paths)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("audit.log_sync skipped (no running loop): %s/%s", action, entity_kind)
        return
    loop.create_task(log(action, entity_kind, entity_id, before, after, actor, session_id))


async def _query_impl(
    conn,
    *,
    since: float | None = None,
    actor: str | None = None,
    entity_kind: str | None = None,
    session_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    tenant_where_pg(where, params, table_alias="a")
    if since is not None:
        where.append(f"a.ts >= ${len(params) + 1}")
        params.append(since)
    if actor:
        where.append(f"a.actor = ${len(params) + 1}")
        params.append(actor)
    if entity_kind:
        where.append(f"a.entity_kind = ${len(params) + 1}")
        params.append(entity_kind)
    if session_id:
        where.append(f"a.session_id = ${len(params) + 1}")
        params.append(session_id)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    sql = (
        "SELECT a.id, a.ts, a.actor, a.action, a.entity_kind, a.entity_id, "
        "a.before_json, a.after_json, a.prev_hash, a.curr_hash, a.session_id, "
        "s.ip AS session_ip, s.user_agent AS session_ua "
        "FROM audit_log a LEFT JOIN sessions s ON a.session_id = s.token"
        + where_sql
        + f" ORDER BY a.id DESC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [
        {
            "id": r["id"], "ts": r["ts"], "actor": r["actor"],
            "action": r["action"], "entity_kind": r["entity_kind"],
            "entity_id": r["entity_id"],
            "before": json.loads(r["before_json"] or "{}"),
            "after": json.loads(r["after_json"] or "{}"),
            "prev_hash": r["prev_hash"],
            "curr_hash": r["curr_hash"],
            "session_id": r["session_id"],
            "session_ip": r["session_ip"],
            "session_ua": r["session_ua"],
        }
        for r in rows
    ]


async def query(
    *,
    since: float | None = None,
    actor: str | None = None,
    entity_kind: str | None = None,
    session_id: str | None = None,
    limit: int = 200,
    conn=None,
) -> list[dict[str, Any]]:
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _query_impl(
                owned_conn, since=since, actor=actor,
                entity_kind=entity_kind, session_id=session_id,
                limit=limit,
            )
    return await _query_impl(
        conn, since=since, actor=actor,
        entity_kind=entity_kind, session_id=session_id, limit=limit,
    )


async def write_audit(request, action: str, entity_kind: str,
                      entity_id: str | None = None,
                      before: dict[str, Any] | None = None,
                      after: dict[str, Any] | None = None,
                      actor: str | None = None) -> Optional[int]:
    """Convenience wrapper that auto-extracts session_id and actor from
    the current request context (set by ``auth.current_user``)."""
    sess = getattr(getattr(request, "state", None), "session", None)
    sid = sess.token if sess else None
    if actor is None:
        user = getattr(getattr(request, "state", None), "user", None)
        if user:
            actor = getattr(user, "email", "system")
        else:
            actor = "system"
    return await log(action, entity_kind, entity_id, before, after, actor, session_id=sid)


async def _verify_chain_impl(
    conn, tenant_id: str,
) -> tuple[bool, Optional[int]]:
    prev_hash = ""
    rows = await conn.fetch(
        "SELECT id, ts, actor, action, entity_kind, entity_id, "
        "before_json, after_json, prev_hash, curr_hash "
        "FROM audit_log WHERE tenant_id = $1 ORDER BY id ASC",
        tenant_id,
    )
    for r in rows:
        payload = {
            "action": r["action"],
            "entity_kind": r["entity_kind"],
            "entity_id": r["entity_id"] or "",
            "before": json.loads(r["before_json"] or "{}"),
            "after": json.loads(r["after_json"] or "{}"),
            "actor": r["actor"],
        }
        recomputed = _hash(
            prev_hash,
            _canonical(payload) + str(round(r["ts"], 6)),
        )
        if r["prev_hash"] != prev_hash or r["curr_hash"] != recomputed:
            return (False, r["id"])
        prev_hash = r["curr_hash"]
    return (True, None)


async def verify_chain(
    tenant_id: str | None = None, conn=None,
) -> tuple[bool, Optional[int]]:
    """Walk a single tenant's chain in id order. Returns (True, None)
    if intact, otherwise (False, first_bad_id).

    If *tenant_id* is None, uses the current context tenant (falls back
    to "t-default")."""
    from backend.db_context import current_tenant_id
    tid = tenant_id or current_tenant_id() or "t-default"
    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            return await _verify_chain_impl(owned_conn, tid)
    return await _verify_chain_impl(conn, tid)


async def verify_all_chains(conn=None) -> dict[str, tuple[bool, Optional[int]]]:
    """Verify every tenant's chain independently. Returns a dict mapping
    tenant_id → (ok, first_bad_id_or_None)."""
    from backend.db_pool import get_pool
    if conn is None:
        async with get_pool().acquire() as owned_conn:
            tenants = [
                r["tenant_id"] for r in await owned_conn.fetch(
                    "SELECT DISTINCT tenant_id FROM audit_log "
                    "ORDER BY tenant_id"
                )
            ]
            results: dict[str, tuple[bool, Optional[int]]] = {}
            for tid in tenants:
                results[tid] = await _verify_chain_impl(owned_conn, tid)
            return results
    tenants = [
        r["tenant_id"] for r in await conn.fetch(
            "SELECT DISTINCT tenant_id FROM audit_log ORDER BY tenant_id"
        )
    ]
    results = {}
    for tid in tenants:
        results[tid] = await _verify_chain_impl(conn, tid)
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI: `python -m backend.audit verify`
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cli_main(argv: list[str]) -> int:
    import sys
    if len(argv) < 1 or argv[0] not in {"verify", "verify-all", "tail"}:
        print("usage: python -m backend.audit verify [--tenant TID] | verify-all | tail [N]",
              file=sys.stderr)
        return 2

    async def _run() -> int:
        from backend import db
        await db.init()
        try:
            if argv[0] == "verify":
                tid: str | None = None
                if "--tenant" in argv:
                    idx = argv.index("--tenant")
                    if idx + 1 < len(argv):
                        tid = argv[idx + 1]
                    else:
                        print("--tenant requires a value", file=sys.stderr)
                        return 2
                if tid:
                    ok, bad = await verify_chain(tenant_id=tid)
                    label = f"[{tid}] "
                else:
                    from backend.db_context import set_tenant_id
                    set_tenant_id("t-default")
                    ok, bad = await verify_chain()
                    label = "[t-default] "
                if ok:
                    print(f"audit chain {label}OK")
                    return 0
                print(f"audit chain {label}BROKEN at row id={bad}", file=sys.stderr)
                return 1

            if argv[0] == "verify-all":
                results = await verify_all_chains()
                if not results:
                    print("no audit entries found")
                    return 0
                failed = False
                for tid, (ok, bad) in sorted(results.items()):
                    if ok:
                        print(f"  [{tid}] OK")
                    else:
                        print(f"  [{tid}] BROKEN at row id={bad}", file=sys.stderr)
                        failed = True
                return 1 if failed else 0

            n = int(argv[1]) if len(argv) > 1 else 20
            rows = await query(limit=n)
            for r in rows:
                print(f"#{r['id']:<5} {r['ts']:.0f} [{r['actor']}] "
                      f"{r['action']} {r['entity_kind']}/{r['entity_id']}")
            return 0
        finally:
            await db.close()

    return asyncio.run(_run())


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv[1:]))

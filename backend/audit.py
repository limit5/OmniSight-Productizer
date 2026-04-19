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

from backend.db_context import tenant_insert_value, tenant_where

logger = logging.getLogger(__name__)

# Single-process serialiser for the chain — a fan-in of concurrent
# writers would race the prev_hash read otherwise. SQLite's BEGIN
# IMMEDIATE provides the cross-process variant in CI.
_chain_lock = asyncio.Lock()


def _canonical(row: dict[str, Any]) -> str:
    """Deterministic JSON serialisation for hashing. Sorted keys, no
    whitespace, ensure_ascii=False so CJK doesn't escape."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(prev_hash: str, payload_canon: str) -> str:
    return hashlib.sha256((prev_hash + payload_canon).encode("utf-8")).hexdigest()


async def _conn():
    from backend import db
    return db._conn()


async def _last_hash_for_tenant(tenant_id: str) -> str:
    """Get the last hash in a specific tenant's chain."""
    conn = await _conn()
    async with conn.execute(
        "SELECT curr_hash FROM audit_log WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["curr_hash"] if row else ""


async def log(action: str, entity_kind: str, entity_id: str | None,
              before: dict[str, Any] | None = None,
              after: dict[str, Any] | None = None,
              actor: str = "system",
              session_id: str | None = None) -> Optional[int]:
    """Append a single row. Returns the new row id, or None on failure
    (logged at warning, never raises). Chains the new row to the prior
    one's curr_hash so any post-write tampering breaks `verify_chain`."""
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

    try:
        conn = await _conn()
        tid = tenant_insert_value()
        async with _chain_lock:
            prev = await _last_hash_for_tenant(tid)
            curr = _hash(prev, payload_canon + str(round(ts, 6)))
            # Phase-3 PG compat: use RETURNING instead of cur.lastrowid.
            # asyncpg does not surface a lastrowid; RETURNING is dialect-
            # neutral (SQLite 3.35+, Postgres) and avoids a second round
            # trip for ``SELECT MAX(id)``.
            async with conn.execute(
                "INSERT INTO audit_log "
                "(ts, actor, action, entity_kind, entity_id, before_json, after_json, "
                "prev_hash, curr_hash, session_id, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (ts, actor, action, entity_kind, entity_id or "",
                 json.dumps(before_d, ensure_ascii=False),
                 json.dumps(after_d, ensure_ascii=False),
                 prev, curr, session_id, tid),
            ) as cur:
                row = await cur.fetchone()
            await conn.commit()
            new_id = row[0] if row else None
        return new_id
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


async def query(*, since: float | None = None, actor: str | None = None,
                entity_kind: str | None = None, session_id: str | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
    conn = await _conn()
    where: list[str] = []
    params: list[Any] = []
    tenant_where(where, params, table_alias="a")
    if since is not None:
        where.append("a.ts >= ?"); params.append(since)
    if actor:
        where.append("a.actor = ?"); params.append(actor)
    if entity_kind:
        where.append("a.entity_kind = ?"); params.append(entity_kind)
    if session_id:
        where.append("a.session_id = ?"); params.append(session_id)
    sql = ("SELECT a.id, a.ts, a.actor, a.action, a.entity_kind, a.entity_id, "
           "a.before_json, a.after_json, a.prev_hash, a.curr_hash, a.session_id, "
           "s.ip AS session_ip, s.user_agent AS session_ua "
           "FROM audit_log a LEFT JOIN sessions s ON a.session_id = s.token")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(int(limit))
    async with conn.execute(sql, tuple(params)) as cur:
        rows = await cur.fetchall()
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


async def verify_chain(tenant_id: str | None = None) -> tuple[bool, Optional[int]]:
    """Walk a single tenant's chain in id order. Returns (True, None)
    if intact, otherwise (False, first_bad_id).

    If *tenant_id* is None, uses the current context tenant (falls back
    to "t-default")."""
    from backend.db_context import current_tenant_id
    tid = tenant_id or current_tenant_id() or "t-default"
    conn = await _conn()
    prev_hash = ""
    async with conn.execute(
        "SELECT id, ts, actor, action, entity_kind, entity_id, "
        "before_json, after_json, prev_hash, curr_hash "
        "FROM audit_log WHERE tenant_id = ? ORDER BY id ASC",
        (tid,),
    ) as cur:
        async for r in cur:
            payload = {
                "action": r["action"],
                "entity_kind": r["entity_kind"],
                "entity_id": r["entity_id"] or "",
                "before": json.loads(r["before_json"] or "{}"),
                "after": json.loads(r["after_json"] or "{}"),
                "actor": r["actor"],
            }
            recomputed = _hash(prev_hash, _canonical(payload) + str(round(r["ts"], 6)))
            if r["prev_hash"] != prev_hash or r["curr_hash"] != recomputed:
                return (False, r["id"])
            prev_hash = r["curr_hash"]
    return (True, None)


async def verify_all_chains() -> dict[str, tuple[bool, Optional[int]]]:
    """Verify every tenant's chain independently. Returns a dict mapping
    tenant_id → (ok, first_bad_id_or_None)."""
    conn = await _conn()
    async with conn.execute(
        "SELECT DISTINCT tenant_id FROM audit_log ORDER BY tenant_id"
    ) as cur:
        tenants = [r["tenant_id"] for r in await cur.fetchall()]
    results: dict[str, tuple[bool, Optional[int]]] = {}
    for tid in tenants:
        results[tid] = await verify_chain(tenant_id=tid)
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

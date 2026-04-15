"""Phase 53 — audit & compliance layer.

Append-only hash-chained log of every state-changing operation worth
auditing. Implementation is intentionally minimal — one table, one
write path, one read path, one verify path. Entry points hooked from
`decision_engine.set_mode/set_strategy/resolve` and any callers that
go through `audit.log()`.

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
    await audit.verify_chain()  # returns (ok, first_bad_id_or_None)

CLI:
    python -m backend.audit verify
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Optional

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


async def _last_hash() -> str:
    conn = await _conn()
    async with conn.execute(
        "SELECT curr_hash FROM audit_log ORDER BY id DESC LIMIT 1"
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
        async with _chain_lock:
            prev = await _last_hash()
            curr = _hash(prev, payload_canon + str(round(ts, 6)))
            cur = await conn.execute(
                "INSERT INTO audit_log "
                "(ts, actor, action, entity_kind, entity_id, before_json, after_json, "
                "prev_hash, curr_hash, session_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, entity_kind, entity_id or "",
                 json.dumps(before_d, ensure_ascii=False),
                 json.dumps(after_d, ensure_ascii=False),
                 prev, curr, session_id),
            )
            await conn.commit()
            new_id = cur.lastrowid
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
                entity_kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    conn = await _conn()
    where = []
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?"); params.append(since)
    if actor:
        where.append("actor = ?"); params.append(actor)
    if entity_kind:
        where.append("entity_kind = ?"); params.append(entity_kind)
    sql = ("SELECT id, ts, actor, action, entity_kind, entity_id, "
           "before_json, after_json, prev_hash, curr_hash, session_id FROM audit_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
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


async def verify_chain() -> tuple[bool, Optional[int]]:
    """Walk the chain in id order. Returns (True, None) if the chain
    is intact, otherwise (False, first_bad_id). first_bad_id is the
    earliest row whose curr_hash doesn't match the recomputed value
    given the prior row's curr_hash."""
    conn = await _conn()
    prev_hash = ""
    async with conn.execute(
        "SELECT id, ts, actor, action, entity_kind, entity_id, "
        "before_json, after_json, prev_hash, curr_hash "
        "FROM audit_log ORDER BY id ASC"
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI: `python -m backend.audit verify`
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _cli_main(argv: list[str]) -> int:
    import sys
    if len(argv) < 1 or argv[0] not in {"verify", "tail"}:
        print("usage: python -m backend.audit verify | tail [N]", file=sys.stderr)
        return 2

    async def _run() -> int:
        from backend import db
        await db.init()
        try:
            if argv[0] == "verify":
                ok, bad = await verify_chain()
                if ok:
                    print("audit chain OK")
                    return 0
                print(f"audit chain BROKEN at row id={bad}", file=sys.stderr)
                return 1
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

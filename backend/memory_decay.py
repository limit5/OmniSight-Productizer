"""Phase 63-E — episodic memory quality decay.

Locked design rule: **never delete memory rows, only down-weight them**.
A stale solution might still be the right one for a long-tail edge
case; deleting it is irreversible. Decay lets `decayed_score` slide
toward zero so the FTS5 search ranks fresher / more-used solutions
above stale ones, but the row stays around for an admin to revive.

What this module owns:

  * `touch(memory_id)`           — caller (RAG pre-fetch, manual
                                    lookup) bumps `last_used_at` so
                                    the row resets its decay clock.
  * `decay_unused(*, ttl_s, factor)` — nightly worker. For every row
                                       not touched in `ttl_s`, multiply
                                       `decayed_score` by `factor`.
  * `restore(memory_id)`         — admin endpoint: copy quality_score
                                    back into decayed_score.
  * `run_decay_loop`             — singleton background loop, opt-in
                                    via OMNISIGHT_SELF_IMPROVE_LEVEL
                                    containing l3 (memory tracks the
                                    intelligence subsystem).

Tunables (env, with sane defaults):
  OMNISIGHT_MEMORY_DECAY_TTL_S=7776000   # 90 days
  OMNISIGHT_MEMORY_DECAY_FACTOR=0.9      # 10% per pass
  OMNISIGHT_MEMORY_DECAY_INTERVAL_S=86400  # daily
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 90 * 86400
DEFAULT_FACTOR = 0.9
DEFAULT_INTERVAL_S = 86400.0


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        return default


def is_enabled() -> bool:
    """Decay is gated on L3 (intelligence track) — same opt-in domain
    as IIS / prompt registry."""
    level = (os.environ.get("OMNISIGHT_SELF_IMPROVE_LEVEL")
             or "off").strip().lower()
    if level in {"off", ""}:
        return False
    return "l3" in level or level == "all"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Touch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def touch(memory_id: str, *, now: float | None = None) -> bool:
    """Caller hook: mark this memory as recently used. Resets the
    decay timer (the next decay pass treats this row as 'fresh').

    Phase-3 Step A.2 (2026-04-21): ported to pool + RETURNING id to
    detect matched-vs-missed (asyncpg Pool.execute doesn't expose
    the rowcount the compat wrapper did).
    """
    if not memory_id:
        return False
    now_iso = _ts_iso(now)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE episodic_memory SET last_used_at = $1 "
            "WHERE id = $2 RETURNING id",
            now_iso, memory_id,
        )
    return row is not None


def _ts_iso(now: float | None = None) -> str:
    """ISO-8601 in UTC for SQL TEXT comparisons (matches the
    `datetime('now')` defaults already in the table).

    `utcfromtimestamp` was deprecated in 3.12 and is removed in 3.13;
    use the tz-aware form then drop the tz suffix so sqlite's naïve
    `datetime('now')` strings still lexically compare."""
    import datetime as _dt
    t = now if now is not None else time.time()
    return _dt.datetime.fromtimestamp(t, _dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decay pass
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class DecayResult:
    scanned: int = 0
    decayed: int = 0
    skipped_recent: int = 0


async def decay_unused(
    *,
    ttl_s: float | None = None,
    factor: float | None = None,
    now: float | None = None,
) -> DecayResult:
    """Walk `episodic_memory`. Any row whose `last_used_at` is older
    than `ttl_s` (or NULL — never touched after migration) gets its
    `decayed_score` multiplied by `factor`. Caller usually invokes
    this from the nightly loop; tests pass `now` for determinism."""
    ttl = ttl_s if ttl_s is not None else _env_float(
        "OMNISIGHT_MEMORY_DECAY_TTL_S", DEFAULT_TTL_S)
    f = factor if factor is not None else _env_float(
        "OMNISIGHT_MEMORY_DECAY_FACTOR", DEFAULT_FACTOR)
    f = max(0.0, min(1.0, f))  # clamp; >1 would inflate forever

    cutoff_iso = _ts_iso((now if now is not None else time.time()) - ttl)
    from backend.db_pool import get_pool

    # Count fresh rows separately so ``scanned`` still reflects total
    # table size (tests assert on it). The WHERE filters the decay
    # loop to just stale / never-touched rows — index on last_used_at
    # keeps this O(log n) instead of O(n) as the table grows.
    #
    # Phase-3 Step A.2 (2026-04-21): entire pipeline (count + scan +
    # N× UPDATE) now runs under one pool acquire + tx. Previously a
    # crash mid-loop left some rows decayed and others not, with the
    # decay-total metric reporting a higher count than actually
    # committed to the table. Tx wrap makes the "scanned this many,
    # committed this many, metric reports this many" trio consistent.
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            fresh_n = int(await conn.fetchval(
                "SELECT COUNT(*) FROM episodic_memory "
                "WHERE last_used_at IS NOT NULL "
                "AND last_used_at >= $1",
                cutoff_iso,
            ) or 0)
            rows = await conn.fetch(
                "SELECT id, last_used_at, decayed_score, quality_score "
                "FROM episodic_memory "
                "WHERE last_used_at IS NULL OR last_used_at < $1",
                cutoff_iso,
            )
            res = DecayResult(scanned=len(rows) + fresh_n,
                              skipped_recent=fresh_n)
            for r in rows:
                # First-ever pass for a row migrated in:
                # decayed_score may be 0 if the column was added
                # before quality_score was copied. Initialise it
                # from quality_score in that case.
                current = r["decayed_score"] or 0.0
                if current == 0.0:
                    current = r["quality_score"] or 0.0
                new_score = current * f
                await conn.execute(
                    "UPDATE episodic_memory SET decayed_score = $1 "
                    "WHERE id = $2",
                    new_score, r["id"],
                )
                res.decayed += 1

    try:
        from backend import metrics as _m
        _m.memory_decay_total.labels(action="decayed").inc(res.decayed)
        _m.memory_decay_total.labels(action="skipped_recent").inc(
            res.skipped_recent,
        )
    except Exception as exc:
        logger.debug("memory_decay metric failed: %s", exc)

    logger.info(
        "memory_decay: scanned=%d decayed=%d skipped_recent=%d "
        "ttl=%.0fs factor=%.2f",
        res.scanned, res.decayed, res.skipped_recent, ttl, f,
    )
    return res


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Restore (admin endpoint)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def restore(memory_id: str) -> Optional[float]:
    """Reset decayed_score back to quality_score AND mark touched.
    Returns the restored score, or None if the row doesn't exist.

    Phase-3 Step A.2: single pool acquire shared across the SELECT +
    UPDATE so a concurrent writer can't modify quality_score between
    our read and the restore-to-it write.
    """
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        # fetchrow (not fetchval) — distinguishes "no row" from
        # "row with quality_score IS NULL". Original semantics: row
        # missing → return None; row present with NULL quality →
        # restore to 0.0.
        row = await conn.fetchrow(
            "SELECT quality_score FROM episodic_memory WHERE id = $1",
            memory_id,
        )
        if row is None:
            return None
        q = row["quality_score"] or 0.0
        await conn.execute(
            "UPDATE episodic_memory SET decayed_score = $1, "
            "last_used_at = $2 WHERE id = $3",
            q, _ts_iso(), memory_id,
        )
    try:
        from backend import metrics as _m
        _m.memory_decay_total.labels(action="restored").inc()
    except Exception as exc:
        logger.debug("memory_decay metric failed: %s", exc)
    logger.info("memory_decay: restored %s → %.3f", memory_id, q)
    return q


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_LOOP_RUNNING = False


async def run_decay_loop(*, interval_s: float | None = None) -> None:
    """Singleton background coroutine. Skips ticks while opt-in is off
    so flipping the env doesn't require restart. Mirrors the Phase 52
    DLQ / Phase 47 sweep convention."""
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True

    interval = interval_s if interval_s is not None else _env_float(
        "OMNISIGHT_MEMORY_DECAY_INTERVAL_S", DEFAULT_INTERVAL_S)
    try:
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            if not is_enabled():
                logger.debug("memory_decay loop: opt-in off, skipping tick")
                continue
            try:
                await decay_unused()
            except Exception as exc:
                logger.warning("memory_decay tick failed: %s", exc)
    except asyncio.CancelledError:
        pass
    finally:
        _LOOP_RUNNING = False

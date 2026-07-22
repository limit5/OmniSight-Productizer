"""β-3c (leg-2) — per-worker learned-item cache refresh loop.

The loader cache is a PER-PROCESS module dict, and prod runs multiple
uvicorn workers × two containers — so this loop is deliberately
per-worker (the cmek_revoke_detector idiom), NEVER leader-elected: a
leader-gated refresh would fill exactly one process's cache and every
other worker would serve ``empty_degraded`` forever (β-3c audit C1).

Gated on the loader's dual-flag ``_read_enabled()`` — read-OFF ⇒ 0 ticks,
no pool lookup (inert on SQLite/no-DSN startups). Scope set per tick
(audit C3: the self-tenant scope must be pre-warmed or a tenant cold miss
withholds GLOBAL items too): ``global:-`` ∪ ``tenant:omnisight-self`` ∪
``SELECT DISTINCT scope_key FROM learned_item_snapshots``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from backend import db_pool

_log = logging.getLogger(__name__)

_INTERVAL_ENV = "OMNISIGHT_LEARNED_ITEM_REFRESH_INTERVAL_S"
_DEFAULT_INTERVAL_S = 60.0
_SELF_SCOPE = "tenant:omnisight-self"


def _interval_s() -> float:
    raw = os.environ.get(_INTERVAL_ENV, "").strip()
    try:
        return max(float(raw), 5.0) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        return _DEFAULT_INTERVAL_S


async def refresh_once(pool) -> int:
    """Refresh every known scope into THIS process's cache. Returns the
    scope count. Cost per scope = 1 fetchrow + <=tens of member reads."""
    from backend import learned_item_loader as loader

    scopes = {"global:-", _SELF_SCOPE}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT scope_key FROM learned_item_snapshots"
        )
        scopes.update(r["scope_key"] for r in rows)
        for scope_key in sorted(scopes):
            await loader.refresh_scope(conn, scope_key)
    return len(scopes)


async def run_learned_item_refresh_loop(
    *,
    get_pool=db_pool.get_pool,
    interval_s: "float | None" = None,
    should_continue=lambda: True,
    max_ticks: "int | None" = None,
    sleep=asyncio.sleep,
) -> int:
    """Per-worker loop: inert (0 ticks, no pool) while the read flag is
    OFF; polls the flag so an operator flip enables refresh without a
    restart. Cancellable at shutdown."""
    from backend import learned_item_loader as loader

    if interval_s is None:
        interval_s = _interval_s()
    ticks = 0
    while should_continue() and (max_ticks is None or ticks < max_ticks):
        await asyncio.sleep(0)  # real yield -> always cancellable
        if loader._read_enabled():
            try:
                n = await refresh_once(get_pool())
                _log.debug("learned_item_refresh tick: %d scopes", n)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — keep looping
                _log.warning("learned_item_refresh tick failed", exc_info=True)
        # Count ALL iterations (flag-off included) — else max_ticks never
        # terminates a read-OFF loop (test-hang bug caught 2026-07-22).
        ticks += 1
        await sleep(interval_s)
    return ticks

"""U6-0 T9/T10 GAP-6b: DB-derived U6 observability -- a gated, read-only metrics refresh loop (dormant by default).

The U6 two-stage action-grant mechanism runs largely in STANDALONE processes (the resume loop, the expiry sweeper),
whose in-process prometheus counters are NOT scrape-visible unless PROMETHEUS_MULTIPROC_DIR is set (it is not, by
default -- see docs/operations/prometheus-multiprocess-runbook.md).  So instead of per-producer counters this derives
the metrics from the SHARED DB: a loop in the API process periodically runs READ-ONLY aggregation over the U6 tables and
sets Gauges.  Cross-process-correct by construction (a standalone producer's durable row state IS reflected), and it
touches NO governed producer -- it CANNOT perturb the governed path (read-only, gauges only, all faults swallowed).

DEFAULT-OFF: unless OMNISIGHT_U6_METRICS_ENABLED is truthy the loop returns immediately WITHOUT any pool lookup (so the
lifespan create_task is inert on SQLite/no-DSN startup, where there is no pool to get).  Table-missing-safe: each
aggregation is its own auto-committed statement (no shared transaction), so a not-yet-migrated U6 table (they are
release-pinned, absent in prod until the next cut) degrades to an 'error' outcome without poisoning the other queries or
crashing the loop.  The DB-count Gauges are point-in-time SNAPSHOTS -- never rate() them; rate() only the Counter.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from collections.abc import Callable

from backend import db_pool
from backend import metrics

_log = logging.getLogger(__name__)

_ENABLE_ENV = "OMNISIGHT_U6_METRICS_ENABLED"

# Closed state vocabularies (must match the CHECK constraints: 0264 challenges, 0269 action_grants, 0265 resume_jobs).
_CHALLENGE_STATES = ("pending", "confirmed", "rejected", "expired")
_GRANT_STATES = ("pending", "executing", "consumed", "expired", "failed", "manual")
_RESUME_STATES = ("queued", "claimed", "done", "failed", "manual")


def u6_metrics_enabled() -> bool:
    """The metrics-refresh switch (default OFF; same predicate as the other U6 flags)."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


async def _count_by_state(conn, table: str, gauge, states: "tuple[str, ...]") -> None:
    """Set ``gauge{state}`` from ``SELECT state, count(*) GROUP BY state``, writing EVERY closed label (0 if absent)."""
    rows = await conn.fetch(f"SELECT state, count(*) AS n FROM {table} GROUP BY state")  # noqa: S608 -- const table
    counts = {row["state"]: int(row["n"]) for row in rows}
    for state in states:
        gauge.labels(state=state).set(counts.get(state, 0))


async def _row_count(conn, table: str, gauge) -> None:
    gauge.set(int(await conn.fetchval(f"SELECT count(*) AS n FROM {table}")))  # noqa: S608 -- table is a const


async def _oldest_pending_age(conn, table: str, gauge) -> None:
    """Set ``gauge`` to the age (s) of the OLDEST pending row, 0 when none, clamped non-negative."""
    age = await conn.fetchval(
        f"SELECT COALESCE(EXTRACT(EPOCH FROM (clock_timestamp() - min(created_at))), 0) "  # noqa: S608 -- const table
        f"FROM {table} WHERE state = 'pending'"
    )
    gauge.set(max(0.0, float(age)))


async def refresh_u6_metrics_once(pool) -> str:
    """Read-only: refresh every U6 gauge.  Returns 'ok' if ALL aggregations succeeded, else 'error'.

    Each aggregation is independently guarded: a missing/broken table leaves its gauge at the LAST value (never zeroed
    before a query that may fail) and marks the tick 'error'; it never raises or poisons the next query.
    """
    ok = True
    async with pool.acquire() as conn:
        for coro in (
            lambda: _count_by_state(conn, "challenges", metrics.u6_challenge_count, _CHALLENGE_STATES),
            lambda: _count_by_state(conn, "action_grants", metrics.u6_grant_count, _GRANT_STATES),
            lambda: _count_by_state(conn, "resume_jobs", metrics.u6_resume_count, _RESUME_STATES),
            lambda: _row_count(conn, "execution_results", metrics.u6_execution_results_count),
            lambda: _row_count(conn, "execution_attempts", metrics.u6_execution_attempts_count),
            lambda: _oldest_pending_age(conn, "challenges", metrics.u6_oldest_pending_challenge_age_seconds),
            lambda: _oldest_pending_age(conn, "action_grants", metrics.u6_oldest_pending_grant_age_seconds),
        ):
            try:
                await coro()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a missing/broken table must not abort the refresh or the loop
                ok = False
                _log.warning("u6 metrics refresh aggregation failed", exc_info=True)
    if ok:
        metrics.u6_metrics_last_success_timestamp_seconds.set(time.time())
    return "ok" if ok else "error"


async def run_u6_metrics_refresh_loop(
    *,
    get_pool: Callable[[], object] = db_pool.get_pool,
    interval_s: float = 30.0,
    should_continue: Callable[[], bool] = lambda: True,
    max_ticks: "int | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Run the read-only refresh loop IFF OMNISIGHT_U6_METRICS_ENABLED is set; else an immediate inert return (0 ticks).

    Ticks IMMEDIATELY then sleeps (so startup does not expose a false zero for one interval).  ``get_pool`` is called
    LAZILY per tick (never at construction) so a disabled task never triggers ``db_pool.get_pool()`` on a SQLite/no-DSN
    startup where no pool exists.  Cancellable at shutdown (CancelledError propagates).  Returns the tick count.
    """
    if not u6_metrics_enabled():
        return 0
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    if max_ticks is not None and max_ticks < 0:
        raise ValueError("max_ticks must be >= 0")

    ticks = 0
    while should_continue() and (max_ticks is None or ticks < max_ticks):
        await asyncio.sleep(0)  # real yield -> always cancellable, never starves the loop
        try:
            pool = get_pool()
            outcome = await refresh_u6_metrics_once(pool)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- no pool yet / unexpected fault: record + keep looping, never crash lifespan
            outcome = "error"
            _log.warning("u6 metrics refresh tick failed", exc_info=True)
        metrics.u6_metrics_refresh_total.labels(outcome=outcome).inc()
        ticks += 1
        await sleep(interval_s)
    return ticks

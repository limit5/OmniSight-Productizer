"""Q.6 #300 checkbox 3 — dedicated 24 h GC sweep for ``user_drafts``.

Complements the opportunistic GC already running inside the PUT
handler (``backend/routers/drafts.py::put_user_draft``). The PUT-time
sweep handles the case where a user is actively typing, but idle
workers never fire it, so drafts written just before the user walks
away could theoretically linger past their 24 h TTL if no other PUT
ever arrives.

This module provides a lifespan-scoped background loop (registered
in ``backend/main.py``) that calls :func:`db.prune_user_drafts` on a
fixed cadence (default 1 h) regardless of HTTP traffic. The sweep
itself is a cheap indexed DELETE (``idx_user_drafts_updated_at`` from
alembic 0022) and is a no-op on a clean table.

Module-global audit (SOP Step 1, 2026-04-21 rule)
--------------------------------------------------
The only module-global state is ``_LOOP_RUNNING`` — a singleton guard
that prevents the loop from starting twice in the same worker
process. This is *intentionally* per-worker (acceptable answer #3
under the SOP's three valid patterns): every uvicorn worker runs its
own GC loop, multiple workers firing the same DELETE at the same
time just race harmlessly because the DELETE is idempotent under
PG's read-committed isolation. The underlying durable state lives in
PG, not in the Python process. Same pattern as
``notifications._DLQ_RUNNING`` / ``memory_decay._LOOP_RUNNING``.

Read-after-write timing (SOP Step 1, 2026-04-21 rule)
------------------------------------------------------
N/A — this loop only performs DELETEs. It does not race with any
read-after-write test assertion; the "draft was just written, is it
visible?" contract is covered by the PUT-time opportunistic sweep
and the existing ``test_q6_user_drafts.py`` fixtures, not by this
periodic loop.

Tuning knob
-----------
``OMNISIGHT_DRAFT_GC_SWEEP_S`` (default 3600.0) — seconds between
sweeps. Lower bound is soft: the sweep itself is cheap, but running
more often than once per minute offers no benefit because the TTL
is 24 h and sub-minute staleness is noise.
"""
from __future__ import annotations

import asyncio
import logging
import os

from backend import db as db_helpers

logger = logging.getLogger(__name__)


SWEEP_INTERVAL_S: float = float(
    os.environ.get("OMNISIGHT_DRAFT_GC_SWEEP_S", "3600.0")  # 1 hour
)


_LOOP_RUNNING = False


async def sweep_once() -> int:
    """One shot of the retention sweep. Acquires a pool connection,
    calls :func:`db.prune_user_drafts`, and returns the number of
    rows deleted.

    Exposed as a public entry point so tests can exercise the sweep
    without spinning the background loop, and so an operator can
    trigger a manual sweep via an admin handler if ever needed.
    """
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        return await db_helpers.prune_user_drafts(conn)


async def run_gc_loop(*, interval_s: float | None = None) -> None:
    """Background coroutine: sweep every ``interval_s`` seconds.

    Singleton-guarded so that test fixtures reusing the same process
    don't end up with two loops fighting for the pool. Exits cleanly
    on ``CancelledError`` (the lifespan shutdown path cancels it as
    part of the Step 6 drain).
    """
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True

    interval = float(interval_s) if interval_s is not None else SWEEP_INTERVAL_S
    # Stagger the first run so startup doesn't pile every background
    # loop onto the pool at once. Mirrors ``run_quota_sweep_loop``.
    try:
        await asyncio.sleep(min(60.0, interval / 2))
    except asyncio.CancelledError:
        _LOOP_RUNNING = False
        return

    try:
        while True:
            try:
                deleted = await sweep_once()
                if deleted:
                    logger.info(
                        "user_drafts GC sweep: pruned %d stale row(s)",
                        deleted,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("user_drafts GC sweep failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
    finally:
        _LOOP_RUNNING = False


def _reset_for_tests() -> None:
    """Clear the singleton flag between tests so each case starts
    from a known state. Matches the ``tenant_quota._reset_for_tests``
    convention.
    """
    global _LOOP_RUNNING
    _LOOP_RUNNING = False

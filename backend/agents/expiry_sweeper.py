"""U6-0 T9/T10 GAP-6a: dormant, default-OFF supervised expire-stale sweeper (operator-run only).

Periodically calls ``db.expire_stale(conn)`` to expire ABANDONED pending challenges/grants past wall time -- the hygiene
that claim-time expiry (GAP-5a) does not cover: claim-time only touches a grant that is actually claimed, so
never-claimed grants and abandoned challenges otherwise sit ``pending`` forever.  ``expire_stale`` is a state-only
UPDATE (permitted by the 0270 identity-freeze trigger) that owns its OWN transaction; this loop only SCHEDULES it and
never changes its semantics -- it is pure hygiene and (like the whole U6 mechanism) executes no governed side effect.

DEFAULT-OFF: unless ``OMNISIGHT_U6_EXPIRY_SWEEP_ENABLED`` is truthy the loop returns immediately WITHOUT touching the
pool.  Fixed-interval: exactly ONE set-wide sweep per ``interval_s`` (never a queue-drain hot-loop).  NOTHING auto-runs
this: only the operator-run ``scripts/run_u6_expiry_sweep.py`` calls it (no lifespan hook, no timer, no cron), mirroring
the shipped P5 'manual, NO timer' posture.

Failure contract (mirrors ``resume_driver.drive_resume_jobs``): a sweep fault (any ``Exception``) is caught, logged,
counted, backed off, and circuit-broken after ``max_consecutive_errors`` consecutive raises -- it never crashes the
caller.  ``asyncio.CancelledError`` and other ``BaseException`` propagate.  ``ValueError`` is raised for invalid config
BEFORE any sweep (only when enabled).  ``should_continue`` / ``sleep`` / ``on_sweep`` are trusted caller contracts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass

from backend import db

_log = logging.getLogger(__name__)

_ENABLE_ENV = "OMNISIGHT_U6_EXPIRY_SWEEP_ENABLED"
_DISABLED = "sweep_disabled"
_STOPPED = "stop_predicate"
_MAX_TICKS = "max_ticks"
_CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True)
class SweepStats:
    """An immutable summary of one supervised sweeper run."""

    ticks: int
    challenges_expired: int
    grants_expired: int
    errors: int
    stopped_reason: str


def expiry_sweep_enabled() -> bool:
    """The sweep-enable switch (default OFF; same predicate as resume_loop.resume_loop_enabled)."""
    return os.environ.get(_ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


async def run_expiry_sweep_loop(
    pool,
    *,
    interval_s: float,
    error_backoff_s: float,
    max_consecutive_errors: int,
    should_continue: Callable[[], bool],
    max_ticks: "int | None" = None,
    on_sweep: "Callable[[int, int], None] | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SweepStats:
    """Run the supervised expiry sweep IFF ``OMNISIGHT_U6_EXPIRY_SWEEP_ENABLED`` is set; else a no-op SweepStats.

    Disabled: NO side effect (no pool use).  Enabled: each tick acquires ONE pooled connection and calls
    ``db.expire_stale`` (which owns its own transaction), accumulates the expired counts, calls ``on_sweep(c, g)``
    best-effort, then paces a fixed ``interval_s`` before the next sweep (applied at the loop TOP after the stop checks,
    so a stopping run -- incl ``max_ticks`` -- never wastes a final sleep).  The injected ``sleep`` is the shutdown
    seam: the operator script injects an interruptible sleep so SIGINT/SIGTERM wakes it.  A real ``asyncio.sleep(0)``
    every iteration keeps the loop cancellable.  ``max_ticks=1`` = one sweep then exit (drain).  Dormant: no caller.
    """
    if not expiry_sweep_enabled():
        return SweepStats(0, 0, 0, 0, _DISABLED)
    if interval_s <= 0:
        raise ValueError("interval_s must be > 0")
    if error_backoff_s <= 0:
        raise ValueError("error_backoff_s must be > 0")
    if max_consecutive_errors < 1:
        raise ValueError("max_consecutive_errors must be >= 1")
    if max_ticks is not None and max_ticks < 0:
        raise ValueError("max_ticks must be >= 0")

    ticks = challenges = grants = errors = 0
    consecutive_errors = 0
    backoff = 0.0

    def _done(reason: str) -> SweepStats:
        return SweepStats(ticks, challenges, grants, errors, reason)

    while True:
        if not should_continue():
            return _done(_STOPPED)
        if max_ticks is not None and ticks >= max_ticks:
            return _done(_MAX_TICKS)

        # A real (non-injected) yield every iteration: the loop can never starve the event loop or its own canceller.
        # The injected sleep below is only for the meaningful interval/backoff waits (and is the interruptibility seam).
        await asyncio.sleep(0)
        if backoff:
            await sleep(backoff)
            backoff = 0.0

        ticks += 1
        try:
            async with pool.acquire() as conn:
                counts = await db.expire_stale(conn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a supervisor must not crash on a sweep fault
            errors += 1
            consecutive_errors += 1
            _log.warning("u6 expiry sweep tick raised (consecutive=%d)", consecutive_errors, exc_info=True)
            if consecutive_errors >= max_consecutive_errors:
                return _done(_CIRCUIT_OPEN)
            backoff = error_backoff_s
            continue

        consecutive_errors = 0
        c = int(counts.get("challenges", 0))
        g = int(counts.get("grants", 0))
        challenges += c
        grants += g
        if on_sweep is not None:
            try:
                on_sweep(c, g)
            except Exception:  # noqa: BLE001 -- an observability hook must not break the loop
                _log.warning("u6 expiry sweep on_sweep hook raised", exc_info=True)

        # Fixed-interval pacing: wait interval_s before the next sweep, deferred to the loop top so the stop/max_ticks
        # checks run first (a stopping run never sleeps needlessly).
        backoff = interval_s

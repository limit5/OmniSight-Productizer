"""U6-0 T9/T10 GAP-5c-loop sub-leaf A: a supervised driver loop over an abstract one-shot driver (dormant).

``drive_resume_jobs`` repeatedly awaits an injected ``drive_once()`` -- which drives ONE resume job and returns a status
string -- until a stop predicate says stop, the queue drains (optional), a tick cap is hit, or too many consecutive
errors trip a circuit breaker.  It has NO dependency on PostgreSQL, the executor, the resolver, or ``run_resume_job``'s
signature: the caller (sub-leaf C) binds ``drive_once`` to ``run_resume_job(...)`` with all its wired dependencies.
Whatever this loop causes is exactly what ``drive_once`` causes, which downstream is authorizer-gated (default-deny), so
a running loop executes nothing until execution is separately enabled.

Failure contract: a ``drive_once`` fault (any ``Exception``) is caught, logged, counted, backed off, and circuit-broken
-- it never crashes the caller.  ``asyncio.CancelledError`` and other ``BaseException`` (``KeyboardInterrupt`` /
``SystemExit`` / ``GeneratorExit``) intentionally propagate.  ``ValueError`` is raised for invalid configuration before
any drive.  ``should_continue`` and ``sleep`` are trusted caller contracts: if they raise, that propagates as a caller
programming error (it is not masked as a fake circuit-break).  ``drive_once`` must return ``str`` per its contract.

Boundedness: ``max_ticks`` caps the number of drive *starts*, not wall-clock time -- a slow ``drive_once`` / ``sleep`` /
``on_outcome`` can still take arbitrarily long.  Daemon mode (``drain_when_idle=False`` with an always-true
predicate and no ``max_ticks``) is intentionally unbounded; a real ``asyncio.sleep(0)`` each iteration yields
so the loop can never starve the event loop and always remains cancellable.  Dormant: no production caller.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from types import MappingProxyType

_log = logging.getLogger(__name__)

# The status a one-shot driver returns.  "idle" means the queue had no runnable job; anything else is a driven outcome
# (run_resume_job returns "done" / "failed" / "manual" / "queued" / "lease_lost").  The loop treats only "idle" special.
DriveOnce = Callable[[], Awaitable[str]]

_STOP_PREDICATE = "stop_predicate"
_IDLE_DRAINED = "idle_drained"
_MAX_TICKS = "max_ticks"
_CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True)
class DriveStats:
    """An immutable summary of one supervised run.  ``ticks == driven + idle + errors``; outcomes sum to ``driven``."""

    ticks: int
    driven: int
    idle: int
    errors: int
    stopped_reason: str
    outcomes: Mapping[str, int] = field(default_factory=dict)


async def drive_resume_jobs(
    drive_once: DriveOnce,
    *,
    poll_interval_s: float,
    error_backoff_s: float,
    max_consecutive_errors: int,
    drain_when_idle: bool,
    should_continue: Callable[[], bool],
    max_ticks: "int | None" = None,
    on_outcome: "Callable[[str], None] | None" = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DriveStats:
    """Supervise repeated one-shot drives.  Bounded, stoppable, error-resilient; see the module failure contract.

    Stops when: ``should_continue()`` is False (``stop_predicate``); ``drain_when_idle`` and ``drive_once`` returned
    ``"idle"`` (``idle_drained``); ``max_ticks`` drive-starts reached (``max_ticks``); or ``max_consecutive_errors``
    consecutive raises (``circuit_open``).  A pending backoff is applied at the loop top AFTER the stop checks, so a run
    that is about to stop never wastes a final sleep.
    """
    if max_consecutive_errors < 1:
        raise ValueError("max_consecutive_errors must be >= 1")

    ticks = driven = idle = errors = 0
    consecutive_errors = 0
    outcomes: dict[str, int] = {}
    backoff = 0.0

    def _done(reason: str) -> DriveStats:
        return DriveStats(ticks, driven, idle, errors, reason, MappingProxyType(dict(outcomes)))

    while True:
        if not should_continue():
            return _done(_STOP_PREDICATE)
        if max_ticks is not None and ticks >= max_ticks:
            return _done(_MAX_TICKS)

        # A real (non-injected) yield every iteration: the loop can never starve the event loop or its own canceller,
        # even if drive_once completes synchronously.  The injected sleep below is only for the meaningful backoffs.
        await asyncio.sleep(0)
        if backoff:
            await sleep(backoff)
            backoff = 0.0

        ticks += 1
        try:
            status = await drive_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a supervisor must not crash on a driver fault
            errors += 1
            consecutive_errors += 1
            _log.warning("resume driver tick raised (consecutive=%d)", consecutive_errors, exc_info=True)
            if consecutive_errors >= max_consecutive_errors:
                return _done(_CIRCUIT_OPEN)
            backoff = error_backoff_s
            continue

        consecutive_errors = 0
        if status == "idle":
            idle += 1
            if drain_when_idle:
                return _done(_IDLE_DRAINED)
            backoff = poll_interval_s
            continue

        driven += 1
        outcomes[status] = outcomes.get(status, 0) + 1
        if on_outcome is not None:
            try:
                on_outcome(status)
            except Exception:  # noqa: BLE001 -- an observability hook must not break the loop
                _log.warning("resume driver on_outcome hook raised", exc_info=True)

"""OP-947 H2 — Event-router worker (FIFO pull + retry + DLQ).

Loops over the ``release_events`` queue:

1. Claim the oldest ``pending`` row whose ``next_retry_at <= now``
   (atomic UPDATE … WHERE status='pending'; concurrent workers race
   on the WHERE clause and the loser tries again — AC "worker
   concurrent safe").
2. Dispatch via :mod:`backend.release_conductor.event_handlers`.
3. On success — mark ``done`` + cache ``handler_result_json`` (AC #5
   idempotent replay).
4. On retryable failure — bump ``attempt_count``, schedule
   ``next_retry_at`` with exponential backoff (AC #4: 3 attempts
   total). After the 3rd failure, status moves to ``failed`` (dead-
   letter) and the worker emits a :class:`DeadLetterAlert` for the
   operator (AC #4 / error catalog).
5. On non-retryable failure (``UnknownEventType``) — park the row in
   ``failed`` immediately; retrying an unknown event 3 times helps no
   one.

The worker is intentionally **sync + single-tick** at the public API
level: :func:`process_one_event` claims and dispatches one row and
returns. The "loop forever" version (:func:`run_forever`) is a thin
wrapper that polls; tests call ``process_one_event`` directly so they
never block.

Why sync
========
The release-events channel is low volume (≤ hundreds of events per
release per day per ADR-0018 §Consequences). A sync worker keeps the
atomic claim+dispatch path easy to reason about: the dispatcher is
sync, the state-machine helpers are sync, and adding asyncio only
buys us throughput we don't need.

Concurrency safety
==================
The atomic claim in :func:`event_router.claim_next_event` is the only
critical section. Once a row is leased (``status=in_progress``), no
other worker can pick it up — the worker then has exclusive ownership
until it calls ``mark_done`` / ``mark_retry`` / ``mark_dead_letter_
immediately``. If the worker process crashes mid-dispatch the row
stays in ``in_progress`` and the H4 cron fallback re-leases it after
the lease timeout. The lease timeout lives in H4 (not here) because
the matrix's eventual-consistency contract owns the recovery story.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from backend.release_conductor import event_handlers, event_router


logger = logging.getLogger(__name__)


class WorkerResult:
    """Lightweight result-shape for one worker tick.

    Tests inspect this rather than reading the DB so the assertions
    stay focused on dispatcher behaviour.
    """

    def __init__(
        self,
        *,
        claimed: bool,
        row_id: int | None,
        outcome: str,
        handler_result: dict[str, Any] | None = None,
        error: str | None = None,
        dead_letter: bool = False,
    ) -> None:
        self.claimed = claimed
        self.row_id = row_id
        self.outcome = outcome
        self.handler_result = handler_result
        self.error = error
        self.dead_letter = dead_letter

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"WorkerResult(claimed={self.claimed}, row_id={self.row_id}, "
            f"outcome={self.outcome!r}, dead_letter={self.dead_letter})"
        )


def process_one_event(
    *,
    alert_on_dead_letter: bool = True,
) -> WorkerResult:
    """Claim + dispatch one event. Returns a :class:`WorkerResult`.

    If no ``pending`` row is ready (queue empty / all in backoff), the
    returned ``claimed`` is False and the worker should sleep before
    polling again.

    ``alert_on_dead_letter`` controls whether the worker raises a
    :class:`event_router.DeadLetterAlert` when an event hits the
    terminal failed state. Production wants True (the operator must
    be paged); tests pass False so they can assert the DB state
    without an exception side effect.
    """
    leased = event_router.claim_next_event()
    if leased is None:
        return WorkerResult(claimed=False, row_id=None, outcome="empty_queue")

    row_id = leased["id"]
    source = leased["source"]
    event_type = leased["event_type"]
    payload = leased["payload"]

    # Idempotent replay short-circuit (AC #5) — if the same row was
    # already dispatched (handler_result cached, status==done) we
    # never reach here because claim_next_event filters on pending.
    # But if the row was retried after a transient failure and a
    # cached handler_result exists, return it directly. (This is rare:
    # only happens if mark_retry runs after a partial mark_done, which
    # shouldn't occur — but the defensive read keeps the contract
    # tight.)
    cached = leased.get("handler_result")
    if cached:
        event_router.mark_done(row_id=row_id, handler_result=cached)
        return WorkerResult(
            claimed=True,
            row_id=row_id,
            outcome="cached_replay",
            handler_result=cached,
        )

    try:
        result = event_handlers.dispatch(source, event_type, payload)
    except event_handlers.UnknownEventType as exc:
        logger.warning(
            "release_conductor.worker.unknown_event row_id=%s source=%s "
            "event_type=%s",
            row_id,
            source,
            event_type,
        )
        event_router.mark_dead_letter_immediately(
            row_id=row_id, error=f"UnknownEventType: {exc}"
        )
        wr = WorkerResult(
            claimed=True,
            row_id=row_id,
            outcome="unknown_event",
            error=str(exc),
            dead_letter=True,
        )
        if alert_on_dead_letter:
            raise event_router.DeadLetterAlert(
                f"row {row_id} ({source}/{event_type}): {exc}"
            )
        return wr
    except Exception as exc:
        # Anything else (handler-raised + bubbled) is treated as
        # retryable per AC #4. The dispatcher wraps the raw exception
        # repr into ``last_error`` for operator triage.
        logger.warning(
            "release_conductor.worker.dispatch_failed row_id=%s source=%s "
            "event_type=%s err=%r",
            row_id,
            source,
            event_type,
            exc,
        )
        retry_state = event_router.mark_retry(
            row_id=row_id, error=f"{type(exc).__name__}: {exc}"
        )
        if retry_state["dead_letter"]:
            wr = WorkerResult(
                claimed=True,
                row_id=row_id,
                outcome="dead_letter",
                error=str(exc),
                dead_letter=True,
            )
            if alert_on_dead_letter:
                raise event_router.DeadLetterAlert(
                    f"row {row_id} ({source}/{event_type}) failed after "
                    f"{retry_state['attempt_count']} attempts: {exc}"
                )
            return wr
        return WorkerResult(
            claimed=True,
            row_id=row_id,
            outcome="retry_scheduled",
            error=str(exc),
            dead_letter=False,
        )

    event_router.mark_done(row_id=row_id, handler_result=result)
    return WorkerResult(
        claimed=True,
        row_id=row_id,
        outcome="dispatched",
        handler_result=result,
    )


# ─── "Loop forever" wrapper for the prod process ─────────────────────
_stop_event = threading.Event()


def request_stop() -> None:
    """Signal :func:`run_forever` to exit on its next idle tick."""
    _stop_event.set()


def reset_stop() -> None:
    """Test-only — clear the stop signal between runs."""
    _stop_event.clear()


def run_forever(
    *,
    idle_sleep_seconds: float = 1.0,
    alert_on_dead_letter: bool = True,
) -> None:  # pragma: no cover - main loop, exercised by integration tests
    """Process events until :func:`request_stop` is called.

    Sleeps ``idle_sleep_seconds`` when the queue is empty. On
    :class:`event_router.DeadLetterAlert` the loop swallows the
    exception (after logging) so a single bad event doesn't crash the
    worker — but the alert side effect (logger.error, future
    pagerduty hook) has already fired before we get here.
    """
    logger.info("release_conductor.worker.run_forever starting")
    while not _stop_event.is_set():
        try:
            result = process_one_event(
                alert_on_dead_letter=alert_on_dead_letter
            )
        except event_router.DeadLetterAlert as exc:
            logger.error(
                "release_conductor.worker.dead_letter_alert: %s", exc
            )
            continue
        if not result.claimed:
            time.sleep(idle_sleep_seconds)
    logger.info("release_conductor.worker.run_forever stopped")


__all__ = [
    "WorkerResult",
    "process_one_event",
    "request_stop",
    "reset_stop",
    "run_forever",
]

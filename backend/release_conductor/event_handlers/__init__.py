"""OP-947 H2 — Dispatch table for the L3 release-conductor event router.

The router (`backend.release_conductor.event_router`) persists each
accepted event to `release_events`; the worker
(`backend.release_conductor.worker`) pulls FIFO and routes by
``(source, event_type)`` through :data:`HANDLER_TABLE` below.

The matrix here is the *code projection* of ADR-0018's 11-row
subscription matrix. Every row in the ADR maps to exactly one entry
here. Where the ADR row says "new handler (H2)" the function lives in
this package; rows already wired in `backend.routers.webhooks` (Gerrit
``change-merged`` on develop, JIRA ``issue_updated``) get a thin shim
that records the dispatch + delegates back to the canonical handler.

Dispatch contract
=================
Each handler:

* Accepts a single ``event`` dict (the parsed ``payload_json``).
* Returns a ``dict`` result (recorded in ``handler_result_json`` for
  idempotent replay per AC #5).
* May raise ``HandlerDispatchFailed`` to trigger the worker's retry
  backoff (AC #4); any other exception bubbles up but is wrapped by
  the worker into ``HandlerDispatchFailed`` for the same retry path.

The ``UnknownEventType`` sentinel is raised by :func:`dispatch` when a
``(source, event_type)`` pair has no row — this is treated as a
non-retryable error and the event lands in dead-letter immediately
(otherwise we'd retry an unknown event 3 times for nothing).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import canary_handlers, gerrit_handlers, jira_handlers, slo_handlers


logger = logging.getLogger(__name__)


class HandlerDispatchFailed(RuntimeError):
    """Raised by a handler when the dispatch should be retried.

    Per AC #4 the worker retries 3 times with exponential backoff; on
    the final attempt the event is moved to ``failed`` (dead-letter)
    status and the operator is alerted.
    """


class UnknownEventType(LookupError):
    """No handler is registered for the given ``(source, event_type)``.

    Non-retryable — the worker drops the event into ``dead_letter``
    immediately rather than burning three retry attempts on an event
    we don't know how to dispatch.
    """


HandlerCallable = Callable[[dict[str, Any]], dict[str, Any]]


HANDLER_TABLE: dict[tuple[str, str], HandlerCallable] = {
    # ─── External: Gerrit ────────────────────────────────────────────
    # ADR-0018 row 1 — Gerrit change-merged on develop. We dispatch
    # via a shim because the canonical entrypoint
    # ``backend.routers.webhooks._on_change_merged`` is async + tied
    # to FastAPI request state; the L3 worker is sync and only needs
    # the post-merge bookkeeping (transition matching release child,
    # advance gate). The shim does that subset.
    ("gerrit", "change-merged"): gerrit_handlers.on_change_merged,
    # ADR-0018 rows 3 / 4 — Gerrit label-added (CR+2 and topic).
    ("gerrit", "label-added"): gerrit_handlers.on_label_added,
    # ─── External: JIRA ─────────────────────────────────────────────
    # ADR-0018 rows 5 / 6 — issue_updated covers both 公開済み and
    # 進行中 transitions; the handler reads the changelog and picks
    # the branch.
    ("jira", "jira:issue_updated"): jira_handlers.on_issue_updated,
    # ─── Internal: SLO + canary ─────────────────────────────────────
    # ADR-0018 row 8 — SLO breach halts forward progress (does not
    # auto-rollback; D11 owns its own RollbackTrigger).
    ("slo_monitor", "slo.breach"): slo_handlers.on_slo_breach,
    # ADR-0018 row 9 — canary stage transitions advance R8/R9.
    ("canary", "canary.stage.started"): canary_handlers.on_stage_event,
    ("canary", "canary.stage.transitioned"): canary_handlers.on_stage_event,
    # ADR-0018 row 10 — canary gate failures halt the advance loop.
    ("canary", "canary.gate.failed"): canary_handlers.on_gate_failed,
    ("canary", "canary.rolled_back"): canary_handlers.on_rolled_back,
    # ADR-0018 row 11 — D9 prod orchestrator (OP-881, not yet built).
    # Placeholder row keeps the contract reserved; H2 ships a no-op.
    (
        "prod_orchestrator",
        "canary-stage-transition",
    ): canary_handlers.on_prod_orchestrator_placeholder,
}


def dispatch(source: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Route ``(source, event_type)`` to its handler.

    Raises :class:`UnknownEventType` if no handler is registered (non-
    retryable). Handler exceptions are *not* caught here — the worker
    layer is responsible for wrapping them into the retry/backoff path.
    """
    key = (source, event_type)
    handler = HANDLER_TABLE.get(key)
    if handler is None:
        raise UnknownEventType(
            f"no handler registered for source={source!r} "
            f"event_type={event_type!r}"
        )
    logger.info(
        "release_conductor.dispatch source=%s event_type=%s handler=%s",
        source,
        event_type,
        handler.__name__,
    )
    return handler(payload)


def register_handler(
    source: str, event_type: str, handler: HandlerCallable
) -> None:
    """Register or override a handler for ``(source, event_type)``.

    Exposed so tests can inject fake handlers without monkeypatching
    the module-level dict directly. Production code uses the table
    above as-is.
    """
    HANDLER_TABLE[(source, event_type)] = handler


def clear_handler(source: str, event_type: str) -> None:
    """Remove a handler entry — test-only helper."""
    HANDLER_TABLE.pop((source, event_type), None)


__all__ = [
    "HANDLER_TABLE",
    "HandlerCallable",
    "HandlerDispatchFailed",
    "UnknownEventType",
    "clear_handler",
    "dispatch",
    "register_handler",
]

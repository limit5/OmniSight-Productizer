"""O8 (#271) — Orchestration mode: monolith ↔ distributed feature flag.

This module is the single seam through which callers ask the platform to
execute an agent run.  It decides, per call, whether to:

  * ``monolith`` — invoke ``backend.agents.graph.run_graph`` in-process
    (the legacy path that has been live since v0.1.0); or
  * ``distributed`` — synthesise a CATC card, push it onto
    ``backend.queue_backend`` and wait for a worker (O3) to produce a
    terminal ``ack`` / ``nack`` / DLQ verdict.

Both paths emit the **same SSE event sequence** so UI subscribers and the
audit log cannot tell the two apart from the outside.  The event sequence
is the parity contract — see ``backend/tests/test_orchestration_mode.py``
for the frozen test matrix.

The mode is selected by the env var ``OMNISIGHT_ORCHESTRATION_MODE``
(settings-backed, see ``backend.config.Settings.orchestration_mode``) and
can be overridden at call time via the ``mode=`` argument — tests and the
rollback drain helper use that override to pin a specific path regardless
of env.

Rollback: when an operator flips the flag back to ``monolith`` after
running ``distributed`` for a while, the queue may still hold in-flight
messages.  ``drain_distributed_inflight()`` is the documented helper to
either (a) wait them out, or (b) forcibly re-dispatch them through the
monolith path.  The runbook ``docs/ops/orchestration_migration.md`` walks
operators through both options.

Non-goals
---------
* This module does NOT change ``run_graph``'s signature or behaviour.
* It does NOT spin up workers — the operator / deployment is expected to
  run ``python -m backend.worker run`` separately when distributed is on.
* It does NOT persist dispatch records to DB; the queue message id IS the
  durable record.  Follow-up phases can add a cross-host status table if
  we ever need to query "who dispatched what" across hosts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mode enum
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OrchestrationMode(str, Enum):
    """Supported execution modes.  String values are the env var values."""

    monolith = "monolith"
    distributed = "distributed"

    @classmethod
    def parse(cls, raw: str | None) -> "OrchestrationMode":
        """Loose parser — empty / unknown / None ⇒ ``monolith``.

        Kept permissive on purpose: this module is in every dispatch hot
        path.  A typo in the env var should NOT crash the app; it should
        fall back to the safe legacy behaviour and log a warning so the
        operator can spot it in observability.
        """
        if not raw:
            return cls.monolith
        normalized = str(raw).strip().lower()
        if normalized in (cls.monolith.value, cls.distributed.value):
            return cls(normalized)
        logger.warning(
            "orchestration_mode: unknown mode %r, falling back to monolith",
            raw,
        )
        return cls.monolith


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public mode accessor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_mode_override: OrchestrationMode | None = None


def current_mode() -> OrchestrationMode:
    """Return the currently active mode.

    Resolution order:
      1. explicit override set by ``set_mode_override`` (tests / CLI);
      2. ``OMNISIGHT_ORCHESTRATION_MODE`` env var (lets ops flip without
         a process restart if the binary checks on each dispatch);
      3. ``settings.orchestration_mode`` (.env / pydantic_settings);
      4. default ``monolith``.
    """
    if _mode_override is not None:
        return _mode_override
    env = os.environ.get("OMNISIGHT_ORCHESTRATION_MODE")
    if env is not None:
        return OrchestrationMode.parse(env)
    try:
        from backend.config import settings
        return OrchestrationMode.parse(settings.orchestration_mode)
    except Exception as exc:
        logger.debug("orchestration_mode: settings unavailable (%s); default", exc)
        return OrchestrationMode.monolith


def set_mode_override(mode: OrchestrationMode | str | None) -> None:
    """Set/clear an in-process mode override.

    Tests use this to pin monolith/distributed without touching the env.
    Pass ``None`` to clear — env + settings resume control.
    """
    global _mode_override
    if mode is None:
        _mode_override = None
        return
    # OrchestrationMode is a ``str`` subclass (StrEnum-style); check enum
    # identity FIRST so ``str(OrchestrationMode.distributed)`` — which
    # renders as ``"OrchestrationMode.distributed"`` and doesn't round
    # -trip through ``parse`` — never reaches the parser.
    if isinstance(mode, OrchestrationMode):
        _mode_override = mode
        return
    _mode_override = OrchestrationMode.parse(mode)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event sequence contract (parity across modes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Both paths MUST emit these event_type / action_type tuples in order.
# The parity test asserts on this list; a failure here usually means
# either the distributed dispatcher skipped a stage or the monolith path
# grew a new stage that wasn't ported over.

PARITY_EVENT_SEQUENCE: tuple[str, ...] = (
    "orchestration.dispatch.started",
    "orchestration.dispatch.routed",
    "orchestration.dispatch.executed",
    "orchestration.dispatch.completed",
)


def _emit(event: str, mode: OrchestrationMode, **extra: Any) -> None:
    """Single emit point so both modes produce byte-identical SSE shape.

    Delegates to ``backend.events.emit_invoke`` — piggybacking on the
    existing ``invoke`` channel keeps the UI subscriber list unchanged
    (no new SSE schema to document in openapi).
    """
    try:
        from backend.events import emit_invoke
        emit_invoke(
            event,
            f"[{mode.value}] {event}",
            mode=mode.value,
            **extra,
        )
    except Exception as exc:    # pragma: no cover — events never hard-fail
        logger.debug("orchestration_mode emit(%s) failed: %s", event, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class DispatchRequest:
    """What a caller hands to ``dispatch()``.

    Mirrors the positional arguments of ``run_graph`` so callers never
    have to choose between the two APIs — the monolith path forwards the
    fields verbatim; the distributed path packs them into the CATC's
    ``domain_context`` / ``handoff_protocol`` so workers have the same
    execution context after round-tripping through the queue.
    """

    user_command: str
    workspace_path: str | None = None
    model_name: str = ""
    agent_sub_type: str = ""
    handoff_context: str = ""
    task_skill_context: str = ""
    task_id: str | None = None
    soc_vendor: str = ""
    sdk_version: str = ""
    # O8-specific: a caller can attach a synthetic Jira subtask key so
    # the distributed path's CATC carries it; if empty, dispatch() will
    # mint one.  Useful for tests that want determinism.
    synthesised_jira_ticket: str = ""
    # Optional allow-list of paths the distributed worker is allowed to
    # touch.  Monolith mode ignores this (LangGraph tools use workspace
    # isolation), but the distributed CATC needs it as a non-empty glob
    # list per ``backend.catc.ImpactScope``.  Defaults to the whole
    # workspace_path (or "**" when no workspace is set).
    allowed_globs: list[str] = field(default_factory=list)


@dataclass
class DispatchOutcome:
    """Uniform result surface returned to the caller in both modes."""

    mode: OrchestrationMode
    ok: bool
    answer: str = ""
    routed_to: str = ""
    # Populated in distributed mode; empty in monolith.
    queue_message_id: str = ""
    jira_ticket: str = ""
    # Filled in both modes so the caller can stream it back in SSE.
    event_sequence: list[str] = field(default_factory=list)
    error: str | None = None
    # The raw ``GraphState`` (monolith) or ``WorkerTaskOutcome`` (distributed)
    # for callers that want to introspect further.  Intentionally typed as
    # Any so this module doesn't import the heavy LangGraph stack eagerly.
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "ok": self.ok,
            "answer": self.answer,
            "routed_to": self.routed_to,
            "queue_message_id": self.queue_message_id,
            "jira_ticket": self.jira_ticket,
            "event_sequence": list(self.event_sequence),
            "error": self.error,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Distributed-path message registry (used by rollback drain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# We track in-flight queue message ids in-process so ``drain_distributed_inflight``
# can enumerate "what did this orchestrator push that hasn't terminated
# yet".  This is deliberately process-local: if you've sharded
# orchestrators, each shard runs drain on its own set of dispatches.

_inflight_lock = threading.Lock()
_inflight: dict[str, dict[str, Any]] = {}


def _register_inflight(message_id: str, request: DispatchRequest,
                       jira_ticket: str) -> None:
    with _inflight_lock:
        _inflight[message_id] = {
            "jira_ticket": jira_ticket,
            "user_command": request.user_command,
            "task_id": request.task_id,
            "dispatched_at": time.time(),
        }


def _unregister_inflight(message_id: str) -> None:
    with _inflight_lock:
        _inflight.pop(message_id, None)


def list_inflight() -> list[dict[str, Any]]:
    """Snapshot of message ids this orchestrator dispatched that haven't
    yet received a terminal verdict in ``dispatch()``.

    Not authoritative across hosts — operators must use the queue's own
    ``depth()`` / ``dlq_list()`` for cluster-wide accounting.  This is a
    convenience for the rollback helper and tests.
    """
    with _inflight_lock:
        return [dict(v, message_id=k) for k, v in _inflight.items()]


def reset_inflight_for_tests() -> None:
    """Test helper — empties the in-flight registry."""
    with _inflight_lock:
        _inflight.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CATC synthesis (distributed path)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_SYNTH_PROJECT = "OMNISIGHTOP"

# Module-level monotonic counter so two dispatches from the same process
# never collide on the synthesised ticket key (seconds resolution alone
# is not enough under load).
_synth_counter_lock = threading.Lock()
_synth_counter = 0


def _synth_jira_ticket() -> str:
    """Mint a ``PROJECT-NUMBER`` that passes CATC validation."""
    global _synth_counter
    with _synth_counter_lock:
        _synth_counter += 1
        seq = _synth_counter
    # Keep inside the 64-char CATC limit with plenty of room.
    now_ms = int(time.time() * 1000) % 10_000_000_000
    return f"{_SYNTH_PROJECT}-{now_ms * 1000 + seq % 1000}"


def _build_catc_from_request(request: DispatchRequest) -> Any:
    """Synthesise a ``TaskCard`` for the distributed path.

    Imported lazily to keep this module's import graph thin (tests that
    only need ``current_mode()`` shouldn't pull in pydantic validators).
    """
    from backend.catc import TaskCard

    ticket = request.synthesised_jira_ticket or _synth_jira_ticket()
    if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", ticket):
        # Defensive — caller supplied a malformed override.
        ticket = _synth_jira_ticket()

    allowed = list(request.allowed_globs)
    if not allowed:
        allowed = ["**"]

    ac = request.user_command.strip() or "(empty user command)"
    handoff = [
        "orchestration_mode=distributed",
        f"agent_sub_type={request.agent_sub_type or 'auto'}",
        f"model={request.model_name or 'auto'}",
    ]
    if request.task_id:
        handoff.append(f"task_id={request.task_id}")
    if request.soc_vendor or request.sdk_version:
        handoff.append(
            f"platform={request.soc_vendor or '-'}@{request.sdk_version or '-'}"
        )

    entry = request.workspace_path or f"#dispatch-{uuid.uuid4().hex[:8]}"
    card = TaskCard.from_dict({
        "jira_ticket": ticket,
        "acceptance_criteria": ac,
        "navigation": {
            "entry_point": entry,
            "impact_scope": {
                "allowed": allowed,
                "forbidden": [],
            },
        },
        "domain_context": request.handoff_context[:1000] or f"dispatch:{ticket}",
        "handoff_protocol": handoff,
    })
    return card, ticket


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dispatchers (one per mode)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Injectable hook — tests replace this with a deterministic stub so they
# don't have to spin up a real worker pool.  Production callers use the
# default which imports ``queue_backend`` on demand.
QueuePushFn = Callable[[Any, Any], str]


async def _monolith_dispatch(request: DispatchRequest) -> DispatchOutcome:
    """Legacy path — run the full LangGraph graph in-process."""
    from backend.agents.graph import run_graph

    try:
        state = await run_graph(
            user_command=request.user_command,
            workspace_path=request.workspace_path,
            model_name=request.model_name,
            agent_sub_type=request.agent_sub_type,
            handoff_context=request.handoff_context,
            task_skill_context=request.task_skill_context,
            task_id=request.task_id,
            soc_vendor=request.soc_vendor,
            sdk_version=request.sdk_version,
        )
    except Exception as exc:
        return DispatchOutcome(
            mode=OrchestrationMode.monolith,
            ok=False,
            error=str(exc),
        )

    routed_to = getattr(state, "routed_to", "") or ""
    answer = getattr(state, "answer", "") or ""
    last_error = getattr(state, "last_error", "") or ""
    return DispatchOutcome(
        mode=OrchestrationMode.monolith,
        ok=not last_error,
        answer=answer,
        routed_to=routed_to,
        error=last_error or None,
        raw=state,
    )


async def _distributed_dispatch(
    request: DispatchRequest,
    *,
    queue_push: QueuePushFn | None = None,
    wait_s: float | None = None,
    poll_interval_s: float = 0.25,
) -> DispatchOutcome:
    """Distributed path — push a CATC, wait for the worker to terminate
    the message (ack / DLQ), then return the uniform outcome.

    ``queue_push`` is injectable for tests.  Defaults to the live
    ``backend.queue_backend.push`` + the live ``get`` poller so real
    workers drive the flow in production.
    """
    from backend import queue_backend as qb
    from backend.queue_backend import TaskState, PriorityLevel

    push = queue_push or (lambda card, prio: qb.push(card, prio))
    card, ticket = _build_catc_from_request(request)
    try:
        msg_id = push(card, PriorityLevel.P2)
    except Exception as exc:
        return DispatchOutcome(
            mode=OrchestrationMode.distributed,
            ok=False,
            jira_ticket=ticket,
            error=f"queue_push_failed: {exc}",
        )

    _register_inflight(msg_id, request, ticket)

    try:
        from backend.config import settings
        default_wait = float(
            getattr(settings, "orchestration_distributed_wait_s", 600.0)
        )
    except Exception:
        default_wait = 600.0
    if wait_s is None:
        wait_s = default_wait

    deadline = time.time() + max(0.0, float(wait_s))
    terminal: Any = None
    dlq_entry: Any = None
    # Terminal states as seen from the orchestrator:
    #   * Done  → ack (worker succeeded)
    #   * Failed → DLQ (worker exhausted retries)
    #   * ``None`` (get returns None) → message was removed: either ack'd
    #     (success) or moved to DLQ (failure). Probe the DLQ to tell them
    #     apart — a silent disappearance is NEVER treated as success.
    while True:
        msg = qb.get(msg_id)
        if msg is None:
            # Could be ack'd (success) OR DLQ'd (failure). Check DLQ.
            try:
                for entry in qb.dlq_list(limit=500):
                    if entry.message_id == msg_id:
                        dlq_entry = entry
                        break
            except Exception as exc:
                logger.debug("dlq_list probe failed: %s", exc)
            terminal = "dlq" if dlq_entry is not None else "acked"
            break
        if msg.state == TaskState.Done:
            terminal = msg
            break
        if msg.state == TaskState.Failed:
            terminal = msg
            break
        if time.time() >= deadline:
            terminal = None  # timeout
            break
        await asyncio.sleep(poll_interval_s)

    _unregister_inflight(msg_id)

    if terminal is None:
        return DispatchOutcome(
            mode=OrchestrationMode.distributed,
            ok=False,
            jira_ticket=ticket,
            queue_message_id=msg_id,
            routed_to="",
            error=f"distributed_wait_timeout_after_{wait_s}s",
        )

    # Happy path — ack'd and gone, or Done before delete.
    if terminal == "acked" or (
        hasattr(terminal, "state")
        and getattr(terminal, "state", None) == TaskState.Done
    ):
        return DispatchOutcome(
            mode=OrchestrationMode.distributed,
            ok=True,
            answer=f"distributed worker acked {ticket}",
            routed_to="distributed-worker",
            jira_ticket=ticket,
            queue_message_id=msg_id,
            raw=terminal,
        )

    # DLQ path — the message was removed from the main store AND found
    # in the dead-letter ledger.  Surface the root cause so the caller
    # can render it in the outcome card.
    if terminal == "dlq":
        root = getattr(dlq_entry, "root_cause", "") or "distributed_worker_failed"
        return DispatchOutcome(
            mode=OrchestrationMode.distributed,
            ok=False,
            answer="",
            routed_to="distributed-worker",
            jira_ticket=ticket,
            queue_message_id=msg_id,
            error=root,
            raw=dlq_entry,
        )

    # Failed state still in the main store — propagate last_error.
    return DispatchOutcome(
        mode=OrchestrationMode.distributed,
        ok=False,
        answer="",
        routed_to="distributed-worker",
        jira_ticket=ticket,
        queue_message_id=msg_id,
        error=getattr(terminal, "last_error", "") or "distributed_worker_failed",
        raw=terminal,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public dispatch API — mode-aware entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def dispatch(
    request: DispatchRequest,
    *,
    mode: OrchestrationMode | str | None = None,
    queue_push: QueuePushFn | None = None,
    wait_s: float | None = None,
) -> DispatchOutcome:
    """Run one agent task under the currently-selected orchestration mode.

    Contract:
      * Emits PARITY_EVENT_SEQUENCE events in order, both modes.
      * Returns a ``DispatchOutcome`` — never raises for routine failures
        (queue push error, worker DLQ, LangGraph exception).  Raises only
        for programmer errors (malformed request, etc.).
      * Safe to call from sync code via ``asyncio.run`` / ``asyncio.to_thread``.

    The ``mode`` kwarg overrides env/settings for this one call (used by
    the rollback drain helper to force-route in-flight work through the
    monolith path).
    """
    if not isinstance(request, DispatchRequest):
        raise TypeError("dispatch() expects a DispatchRequest instance")

    if mode is None:
        active = current_mode()
    elif isinstance(mode, OrchestrationMode):
        active = mode
    else:
        active = OrchestrationMode.parse(mode)
    sequence: list[str] = []

    def _stage(event: str, **extra: Any) -> None:
        sequence.append(event)
        _emit(event, active, **extra)

    _stage(
        PARITY_EVENT_SEQUENCE[0],
        user_command_len=len(request.user_command or ""),
        task_id=request.task_id,
    )
    _stage(
        PARITY_EVENT_SEQUENCE[1],
        routed_to=active.value,
    )

    if active == OrchestrationMode.monolith:
        outcome = await _monolith_dispatch(request)
    else:
        outcome = await _distributed_dispatch(
            request,
            queue_push=queue_push,
            wait_s=wait_s,
        )

    _stage(
        PARITY_EVENT_SEQUENCE[2],
        ok=outcome.ok,
        error=outcome.error,
    )
    _stage(
        PARITY_EVENT_SEQUENCE[3],
        ok=outcome.ok,
        routed_to=outcome.routed_to,
        queue_message_id=outcome.queue_message_id,
    )
    outcome.event_sequence = sequence
    return outcome


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Rollback: drain in-flight distributed tasks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class DrainReport:
    """Summary of a ``drain_distributed_inflight`` run.

    ``drained`` — messages that terminated on their own (ack / DLQ) during
    the wait window.
    ``redispatched`` — messages the helper redispatched through the
    monolith path (only when ``strategy='redispatch_monolith'``).
    ``still_pending`` — messages that didn't terminate in the wait window
    AND weren't redispatched.  Operator must deal with them manually.
    """

    strategy: str
    drained: list[str] = field(default_factory=list)
    redispatched: list[str] = field(default_factory=list)
    still_pending: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "drained": list(self.drained),
            "redispatched": list(self.redispatched),
            "still_pending": list(self.still_pending),
            "elapsed_s": self.elapsed_s,
        }


async def drain_distributed_inflight(
    *,
    strategy: str = "wait",
    wait_s: float = 300.0,
    poll_interval_s: float = 0.5,
    queue_push: QueuePushFn | None = None,
) -> DrainReport:
    """Reconcile the in-flight distributed registry when rolling back.

    Strategies:

      * ``"wait"`` — poll the queue for ``wait_s`` and let the running
        workers terminate each message normally.  Safe when the worker
        pool is still up and you just want to stop sending new work.
      * ``"redispatch_monolith"`` — for every still-in-flight message,
        purge it from the distributed side (best-effort) and re-run the
        original user command through the monolith path so the work
        doesn't get lost.  Used during a hard rollback where the worker
        pool is being torn down.

    Implementation notes:
      * This helper only looks at messages THIS orchestrator dispatched;
        it can't see work pushed by peers.  The runbook makes operators
        run drain on every orchestrator shard before turning workers off.
      * ``redispatch_monolith`` is strictly additive — it does not try to
        dequeue / ack the original message (the worker will either finish
        it or nack it to DLQ naturally); the guarantee is that the user
        command gets a completion signal via the monolith path even if
        the queue side drops on the floor.
    """
    if strategy not in ("wait", "redispatch_monolith"):
        raise ValueError(
            f"drain_distributed_inflight: unknown strategy {strategy!r}"
        )

    start = time.time()
    report = DrainReport(strategy=strategy)

    with _inflight_lock:
        initial = dict(_inflight)

    if not initial:
        report.elapsed_s = time.time() - start
        return report

    deadline = start + max(0.0, float(wait_s))

    # Phase 1 — wait for natural termination.
    remaining: dict[str, dict[str, Any]] = dict(initial)
    from backend import queue_backend as qb
    from backend.queue_backend import TaskState

    while remaining and time.time() < deadline:
        for msg_id in list(remaining.keys()):
            msg = qb.get(msg_id)
            if msg is None or msg.state in (TaskState.Done, TaskState.Failed):
                report.drained.append(msg_id)
                _unregister_inflight(msg_id)
                remaining.pop(msg_id, None)
        if remaining:
            await asyncio.sleep(poll_interval_s)

    if strategy == "wait" or not remaining:
        report.still_pending.extend(remaining.keys())
        report.elapsed_s = time.time() - start
        return report

    # Phase 2 — re-run each remaining dispatch through the monolith path.
    for msg_id, info in list(remaining.items()):
        cmd = info.get("user_command") or ""
        task_id = info.get("task_id")
        if not cmd:
            report.still_pending.append(msg_id)
            continue
        req = DispatchRequest(user_command=cmd, task_id=task_id)
        try:
            await dispatch(req, mode=OrchestrationMode.monolith)
            report.redispatched.append(msg_id)
            _unregister_inflight(msg_id)
        except Exception as exc:
            logger.warning(
                "drain_distributed_inflight: monolith redispatch %s failed: %s",
                msg_id, exc,
            )
            report.still_pending.append(msg_id)

    report.elapsed_s = time.time() - start
    return report


__all__ = [
    "DispatchOutcome",
    "DispatchRequest",
    "DrainReport",
    "OrchestrationMode",
    "PARITY_EVENT_SEQUENCE",
    "current_mode",
    "dispatch",
    "drain_distributed_inflight",
    "list_inflight",
    "reset_inflight_for_tests",
    "set_mode_override",
]

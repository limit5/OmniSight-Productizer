"""Autonomous Decision Engine (Phase 47).

Central brain that decides — given the current Operation Mode — whether an
action should execute immediately, ask for approval, or be auto-resolved
after a timeout. Other modules (`invoke`, `pipeline`, `nodes`) publish
"decision points" here; this module owns the mode, the queue, the loop,
and the SSE contract.

Scope of 47A (this file): mode + decision queue + SSE skeleton. Stuck
detection (47B), ambiguity options (47C), and the 30 s scan loop + full
API (47D) land in follow-up sub-phases.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Operation Mode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OperationMode(str, Enum):
    """How aggressively the engine acts without human approval.

    - **manual**: every non-trivial decision needs explicit approval.
    - **supervised**: common decisions auto-execute; risky ones queue
      for approval. Parallelism capped at 2.
    - **full_auto**: everything auto-executes except destructive/irreversible
      decisions. Parallelism 4.
    - **turbo**: everything auto-executes, including destructive ones, after
      a short countdown (user can still cancel). Parallelism 8.
    """

    manual = "manual"
    supervised = "supervised"
    full_auto = "full_auto"
    turbo = "turbo"


# Concurrency budget per mode (how many INVOKE runs in parallel).
_PARALLEL_BUDGET: dict[OperationMode, int] = {
    OperationMode.manual: 1,
    OperationMode.supervised: 2,
    OperationMode.full_auto: 4,
    OperationMode.turbo: 8,
}

# Decision severities the user can see + filter on.
class DecisionSeverity(str, Enum):
    info = "info"          # observational — auto-OK in any mode
    routine = "routine"    # auto-execute in supervised+
    risky = "risky"        # auto-execute only in full_auto+
    destructive = "destructive"  # auto-execute only in turbo (with countdown)


class DecisionStatus(str, Enum):
    pending = "pending"
    auto_executed = "auto_executed"
    approved = "approved"
    rejected = "rejected"
    undone = "undone"
    timeout_default = "timeout_default"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  State
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_state_lock = threading.Lock()
_current_mode: OperationMode = OperationMode.supervised


@dataclass
class Decision:
    id: str
    kind: str            # semantic tag, e.g. "spawn_agent", "change_model"
    severity: DecisionSeverity
    title: str
    detail: str
    options: list[dict[str, Any]]  # each: {id, label, description, default?}
    default_option_id: str | None
    status: DecisionStatus
    created_at: float
    deadline_at: float | None   # auto-decide after this unix timestamp
    resolved_at: float | None = None
    chosen_option_id: str | None = None
    resolver: str | None = None  # "auto" | "user" | "timeout"
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["status"] = self.status.value
        return d


# Queue of unresolved decisions + bounded history.
_pending: dict[str, Decision] = {}
_history: list[Decision] = []
_HISTORY_MAX = 500
# N7: pending queue cap — refuse new proposals when full to prevent memory
# DoS from a runaway producer. Configurable via env for ops.
import os as _os
_PENDING_MAX = int(_os.environ.get("OMNISIGHT_DECISION_PENDING_MAX", "256"))


class DecisionQueueFull(Exception):
    """Raised when the pending-decision queue is at capacity."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Parallelism budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# N4: replaced bare Semaphore with an explicit counter + condvar so we can
# atomically enforce the *current* cap on every acquire, regardless of how
# many acquires are already in flight. Rebuilding the Semaphore on mode
# change let existing holders keep the old cap.
_parallel_lock = threading.Lock()
_parallel_in_flight: int = 0
_parallel_async_cond: asyncio.Condition | None = None


class _ModeSlot:
    """Async context manager that acquires a slot under the *current* cap.

    Unlike a fixed-cap Semaphore, this reads `_PARALLEL_BUDGET[get_mode()]`
    at `__aenter__` time, so a mode switch immediately tightens or loosens
    the limit for new acquirers. Existing holders retain their slot until
    they release (preserves in-flight safety without killing them).
    """

    async def __aenter__(self) -> "_ModeSlot":
        global _parallel_in_flight, _parallel_async_cond
        if _parallel_async_cond is None:
            _parallel_async_cond = asyncio.Condition()
        while True:
            async with _parallel_async_cond:
                cap = _PARALLEL_BUDGET[get_mode()]
                with _parallel_lock:
                    if _parallel_in_flight < cap:
                        _parallel_in_flight += 1
                        return self
                await _parallel_async_cond.wait()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        global _parallel_in_flight, _parallel_async_cond
        with _parallel_lock:
            _parallel_in_flight = max(0, _parallel_in_flight - 1)
        if _parallel_async_cond is not None:
            async with _parallel_async_cond:
                _parallel_async_cond.notify_all()

    # Back-compat methods so callers that did `.locked()` / `.acquire()` on
    # the previous Semaphore interface keep working. `locked()` means
    # "saturated", matching Semaphore semantics.
    def locked(self) -> bool:
        with _parallel_lock:
            return _parallel_in_flight >= _PARALLEL_BUDGET[get_mode()]

    async def acquire(self) -> bool:
        await self.__aenter__()
        return True

    def release(self) -> None:
        global _parallel_in_flight
        with _parallel_lock:
            _parallel_in_flight = max(0, _parallel_in_flight - 1)


_mode_slot_singleton = _ModeSlot()


def parallel_slot() -> _ModeSlot:
    """Acquire this in invoke/pipeline runs to respect mode parallelism.

    Use as::

        async with decision_engine.parallel_slot():
            ...real work...

    The cap is re-read on every acquire; switching mode mid-flight takes
    effect immediately for *new* acquirers without revoking existing ones.
    """
    return _mode_slot_singleton


def parallel_in_flight() -> int:
    """Current number of held slots (observational / SSE telemetry)."""
    with _parallel_lock:
        return _parallel_in_flight


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mode API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_mode() -> OperationMode:
    with _state_lock:
        return _current_mode


def set_mode(mode: OperationMode | str) -> OperationMode:
    """Switch the operation mode. Emits SSE `mode_changed`."""
    global _current_mode
    if isinstance(mode, str):
        try:
            mode = OperationMode(mode)
        except ValueError as exc:
            raise ValueError(f"unknown mode: {mode}") from exc
    with _state_lock:
        prev = _current_mode
        _current_mode = mode
    # N4: we no longer rebuild a Semaphore on mode change — _ModeSlot reads
    # the cap at acquire time. But if the new cap is *lower* than the
    # in-flight count, surface a warning via SSE so operators know some
    # requests are still running above the nominal cap until they drain.
    cur_inflight = parallel_in_flight()
    new_cap = _PARALLEL_BUDGET[mode]
    try:
        from backend.events import bus as _bus
        payload = {
            "mode": mode.value,
            "previous": prev.value,
            "parallel_cap": new_cap,
            "in_flight": cur_inflight,
            "over_cap": max(0, cur_inflight - new_cap),
        }
        _bus.publish("mode_changed", payload)
    except Exception as _exc:
        # L#44: log at warning — mode changes are audit-relevant; silent
        # swallow hid broken event bus wiring in the past.
        logger.warning("mode_changed publish failed: %s", _exc)
    logger.info("OperationMode: %s → %s", prev.value, mode.value)
    # Phase 53: tamper-evident audit. Fire-and-forget; failures
    # logged but do not block the mode change.
    try:
        from backend import audit as _audit
        _audit.log_sync(
            action="mode_change", entity_kind="operation_mode", entity_id="global",
            before={"mode": prev.value}, after={"mode": mode.value, "parallel_cap": new_cap},
        )
    except Exception:
        pass
    return mode


def should_auto_execute(severity: DecisionSeverity | str) -> bool:
    """Would a decision of *severity* auto-execute under the current mode?"""
    if isinstance(severity, str):
        severity = DecisionSeverity(severity)
    mode = get_mode()
    if severity == DecisionSeverity.info:
        return True
    if severity == DecisionSeverity.routine:
        return mode in (OperationMode.supervised, OperationMode.full_auto, OperationMode.turbo)
    if severity == DecisionSeverity.risky:
        return mode in (OperationMode.full_auto, OperationMode.turbo)
    if severity == DecisionSeverity.destructive:
        return mode == OperationMode.turbo
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Decision registration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def propose(
    kind: str,
    title: str,
    detail: str = "",
    options: list[dict[str, Any]] | None = None,
    default_option_id: str | None = None,
    severity: DecisionSeverity | str = DecisionSeverity.routine,
    timeout_s: float | None = 60.0,
    source: dict[str, Any] | None = None,
) -> Decision:
    """Register a decision point.

    If the current mode permits auto-execution for this severity, the
    decision is immediately resolved to `default_option_id` (or the first
    option) and returned with status=auto_executed. Otherwise it goes to
    the pending queue and an SSE `decision_pending` event is emitted.

    `timeout_s` sets `deadline_at`; the 30 s loop (47D) will auto-resolve
    to `default_option_id` on timeout.
    """
    if isinstance(severity, str):
        severity = DecisionSeverity(severity)
    opts = list(options or [])
    if not opts:
        opts = [{"id": "ok", "label": "OK", "description": ""}]
    # R2-#27: validate options — each must have a non-empty string id;
    # ids must be unique. Garbage-in → garbage-out otherwise (the SSE
    # subscriber picks an option by id later and has no way to recover
    # from duplicates).
    seen: set[str] = set()
    for o in opts:
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            raise ValueError(f"option.id must be a non-empty string, got {oid!r}")
        if oid in seen:
            raise ValueError(f"duplicate option id: {oid!r}")
        seen.add(oid)
    if default_option_id is None:
        default_option_id = opts[0]["id"]
    elif default_option_id not in seen:
        raise ValueError(f"default_option_id {default_option_id!r} not in options")

    # Phase 50B: a matching rule can force severity / default / auto-exec
    # ahead of the normal mode × severity policy. Imported lazily to avoid
    # an import cycle.
    try:
        from backend import decision_rules as _rules
        severity, default_option_id, matched_rule, rule_forces_auto = _rules.apply(
            kind, severity, default_option_id, get_mode(),
        )
        rule_engine_error: str | None = None
    except Exception as _exc:
        logger.warning(
            "decision_rules.apply failed (%s) — falling back to mode/severity policy",
            _exc,
        )
        matched_rule, rule_forces_auto = None, False
        rule_engine_error = str(_exc)[:200]

    now = time.time()
    deadline = (now + timeout_s) if (timeout_s and timeout_s > 0) else None
    dec = Decision(
        id=f"dec-{uuid.uuid4().hex[:10]}",
        kind=kind,
        severity=severity,
        title=title,
        detail=detail,
        options=opts,
        default_option_id=default_option_id,
        status=DecisionStatus.pending,
        created_at=now,
        deadline_at=deadline,
        source=dict(
            {"rule_id": matched_rule["id"]} if matched_rule else {},
            **({"rule_engine_error": rule_engine_error} if rule_engine_error else {}),
            **(source or {}),
        ),
    )

    # Phase 58: smart defaults + profile gate. Rule already had its
    # chance above; if no rule fired AND we're in a profile that
    # accepts auto-resolution by confidence, ask the chooser.
    profile_chosen_id: str | None = None
    profile_confidence: float = 0.0
    profile_rationale: str = ""
    profile_id_used: str = ""
    if not rule_forces_auto:
        try:
            from backend import decision_defaults as _dd
            from backend import decision_profiles as _dp
            from backend import host_native as _hn
            hn_ctx = _hn.context_dict()
            ctx = _dd.Context(
                kind=kind, severity=severity.value,
                options=opts, default_option_id=default_option_id,
                is_host_native=(source or {}).get("is_host_native", hn_ctx["is_host_native"]),
                project_track=(source or {}).get("project_track", hn_ctx["project_track"]),
            )
            chosen = _dd.consult(ctx)
            prof = _dp.get_profile()
            profile_id_used = prof.id
            if chosen is not None:
                # Critical-kind allow-list always queues unless profile
                # explicitly opted in (only GHOST does).
                is_critical = kind in _dp.CRITICAL_KINDS
                threshold = (
                    prof.threshold_destructive
                    if severity == DecisionSeverity.destructive
                    else prof.threshold_risky
                )
                allow_auto = (
                    chosen.confidence >= threshold
                    and (not is_critical or prof.auto_critical)
                )
                if allow_auto:
                    profile_chosen_id = chosen.option_id
                    profile_confidence = chosen.confidence
                    profile_rationale = chosen.rationale
        except Exception as exc:
            logger.warning("decision_defaults/profile gate failed: %s", exc)

    if rule_forces_auto or should_auto_execute(severity) or profile_chosen_id is not None:
        dec.status = DecisionStatus.auto_executed
        dec.resolved_at = now
        dec.chosen_option_id = profile_chosen_id or default_option_id
        dec.resolver = "auto"
        if profile_chosen_id:
            dec.source["chooser_confidence"] = profile_confidence
            dec.source["chooser_rationale"] = profile_rationale
            dec.source["profile_id"] = profile_id_used
            try:
                from backend import db
                loop = asyncio.get_event_loop()
                loop.create_task(_log_auto_decision(
                    decision_id=dec.id, kind=kind, severity=severity.value,
                    chosen_option=dec.chosen_option_id, confidence=profile_confidence,
                    rationale=profile_rationale, profile_id=profile_id_used,
                    auto_executed_at=now,
                ))
            except Exception:
                pass
        _archive(dec)
        _emit("decision_auto_executed", dec)
        return dec

    with _state_lock:
        # N7: bound the pending queue. If callers exceed the cap, surface
        # a distinct exception instead of silently growing memory.
        if len(_pending) >= _PENDING_MAX:
            logger.error(
                "DecisionEngine pending queue full (%d) — refusing %s",
                _PENDING_MAX, kind,
            )
            raise DecisionQueueFull(
                f"pending queue full ({_PENDING_MAX}); "
                "resolve outstanding decisions before submitting more"
            )
        _pending[dec.id] = dec
    _emit("decision_pending", dec)
    return dec


def list_pending() -> list[Decision]:
    with _state_lock:
        return list(_pending.values())


def list_history(limit: int = 100) -> list[Decision]:
    with _state_lock:
        return list(_history[-limit:])


def get(decision_id: str) -> Decision | None:
    with _state_lock:
        if decision_id in _pending:
            return _pending[decision_id]
        for d in reversed(_history):
            if d.id == decision_id:
                return d
    return None


def resolve(
    decision_id: str,
    option_id: str,
    resolver: str = "user",
    status: DecisionStatus = DecisionStatus.approved,
) -> Decision | None:
    """Resolve a pending decision and emit `decision_resolved`.

    Pop + mutate + archive happen inside a single lock acquisition so
    concurrent sweep / user-approval cannot double-resolve (N5/N6 fix).
    The SSE emit runs outside the lock to avoid holding it across I/O.
    """
    with _state_lock:
        dec = _pending.pop(decision_id, None)
        if dec is None:
            return None
        dec.status = status
        dec.chosen_option_id = option_id
        dec.resolved_at = time.time()
        dec.resolver = resolver
        _archive_locked(dec)
    _emit("decision_resolved", dec)
    # Phase 53 audit
    try:
        from backend import audit as _audit
        _audit.log_sync(
            action="decision_resolve", entity_kind="decision", entity_id=dec.id,
            before={"status": "pending", "kind": dec.kind, "severity": dec.severity.value},
            after={"status": dec.status.value, "chosen_option_id": dec.chosen_option_id,
                   "resolver": dec.resolver},
            actor=resolver,
        )
    except Exception:
        pass
    return dec


def undo(decision_id: str) -> Decision | None:
    """Mark a resolved decision as undone (audit only — caller reverses effect)."""
    with _state_lock:
        for d in reversed(_history):
            if d.id == decision_id and d.status in (
                DecisionStatus.approved,
                DecisionStatus.auto_executed,
                DecisionStatus.timeout_default,
            ):
                prev_status = d.status.value
                d.status = DecisionStatus.undone
                d.resolved_at = time.time()
                _emit("decision_undone", d)
                try:
                    from backend import audit as _audit
                    _audit.log_sync(
                        action="decision_undo", entity_kind="decision", entity_id=d.id,
                        before={"status": prev_status},
                        after={"status": "undone"},
                    )
                except Exception:
                    pass
                return d
    return None


def _archive(dec: Decision) -> None:
    """Public archive — takes the lock itself (for standalone callers)."""
    with _state_lock:
        _archive_locked(dec)


async def _log_auto_decision(*, decision_id: str, kind: str, severity: str,
                             chosen_option: str, confidence: float, rationale: str,
                             profile_id: str, auto_executed_at: float) -> None:
    """Phase 58: write to auto_decision_log so the postmortem UI can
    list / bulk-undo. Best-effort; failures logged at warning."""
    try:
        from backend import db
        await db._conn().execute(
            "INSERT INTO auto_decision_log "
            "(decision_id, kind, severity, chosen_option, confidence, rationale, "
            " profile_id, auto_executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, kind, severity, chosen_option, confidence,
             rationale[:240], profile_id, auto_executed_at),
        )
        await db._conn().commit()
    except Exception as exc:
        logger.warning("auto_decision_log insert failed for %s: %s", decision_id, exc)


def _archive_locked(dec: Decision) -> None:
    """Archive while the caller already holds _state_lock."""
    _history.append(dec)
    if len(_history) > _HISTORY_MAX:
        del _history[: len(_history) - _HISTORY_MAX]


def _emit(event: str, dec: Decision) -> None:
    try:
        from backend.events import bus as _bus
        _bus.publish(event, dec.to_dict())
    except Exception as exc:
        logger.debug("decision SSE emit failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test / reset hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _reset_for_tests() -> None:
    """Clear global state between tests. Not for production use."""
    global _current_mode, _parallel_in_flight, _parallel_async_cond
    with _state_lock:
        _pending.clear()
        _history.clear()
        _current_mode = OperationMode.supervised
    _parallel_in_flight = 0
    _parallel_async_cond = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  47D: periodic timeout sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


SWEEP_INTERVAL_S = int(_os.environ.get("OMNISIGHT_DECISION_SWEEP_INTERVAL_S", "10"))


def sweep_timeouts(now: float | None = None) -> list[Decision]:
    """Resolve any pending decision whose deadline has passed.

    N5 fix: snapshot + pop + mutate + archive are all done inside a single
    lock acquisition. A concurrent user-approve can either win the pop
    (and we skip it) or lose (and we archive). No more "resolve called
    with ID that just vanished" window. SSE emission runs outside the
    lock since it is I/O-ish.
    """
    now = now if now is not None else time.time()
    to_emit: list[Decision] = []
    with _state_lock:
        expired_ids = [
            d.id for d in _pending.values()
            if d.deadline_at is not None and d.deadline_at <= now
        ]
        for did in expired_ids:
            dec = _pending.pop(did, None)
            if dec is None:
                continue  # user raced us — their resolve() already handled it
            dec.status = DecisionStatus.timeout_default
            dec.chosen_option_id = dec.default_option_id  # safe default
            dec.resolved_at = now
            dec.resolver = "timeout"
            _archive_locked(dec)
            to_emit.append(dec)
    for dec in to_emit:
        _emit("decision_resolved", dec)
    return to_emit


async def run_sweep_loop(interval_s: float = SWEEP_INTERVAL_S) -> None:
    """Background task: periodically call sweep_timeouts."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            count = len(sweep_timeouts())
            if count:
                logger.info("DecisionEngine sweep: resolved %d timed-out decision(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("DecisionEngine sweep error: %s", exc)

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Parallelism budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_parallel_sema: asyncio.Semaphore | None = None
_parallel_cap_for_sema: int = 0


def _ensure_parallel_sema() -> asyncio.Semaphore:
    """Lazily build the semaphore inside a running loop, resizing on mode change.

    Returns a semaphore whose slot count matches the current mode's budget.
    We can't mutate `asyncio.Semaphore._value` safely, so we rebuild when the
    cap changes. Existing holders keep their reference; new callers see the
    new cap.
    """
    global _parallel_sema, _parallel_cap_for_sema
    cap = _PARALLEL_BUDGET[get_mode()]
    if _parallel_sema is None or _parallel_cap_for_sema != cap:
        _parallel_sema = asyncio.Semaphore(cap)
        _parallel_cap_for_sema = cap
    return _parallel_sema


def parallel_slot() -> asyncio.Semaphore:
    """Acquire this in invoke/pipeline runs to respect mode parallelism.

    Use as::

        async with decision_engine.parallel_slot():
            ...real work...
    """
    return _ensure_parallel_sema()


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
    # Rebuild semaphore on next acquire (lazy).
    global _parallel_sema, _parallel_cap_for_sema
    _parallel_cap_for_sema = -1
    _parallel_sema = None
    try:
        from backend.events import bus as _bus
        _bus.publish("mode_changed", {
            "mode": mode.value,
            "previous": prev.value,
            "parallel_cap": _PARALLEL_BUDGET[mode],
        })
    except Exception:
        pass
    logger.info("OperationMode: %s → %s", prev.value, mode.value)
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
    if default_option_id is None:
        default_option_id = opts[0]["id"]

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
        source=dict(source or {}),
    )

    if should_auto_execute(severity):
        dec.status = DecisionStatus.auto_executed
        dec.resolved_at = now
        dec.chosen_option_id = default_option_id
        dec.resolver = "auto"
        _archive(dec)
        _emit("decision_auto_executed", dec)
        return dec

    with _state_lock:
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
    """Resolve a pending decision and emit `decision_resolved`."""
    with _state_lock:
        dec = _pending.pop(decision_id, None)
    if dec is None:
        return None
    dec.status = status
    dec.chosen_option_id = option_id
    dec.resolved_at = time.time()
    dec.resolver = resolver
    _archive(dec)
    _emit("decision_resolved", dec)
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
                d.status = DecisionStatus.undone
                d.resolved_at = time.time()
                _emit("decision_undone", d)
                return d
    return None


def _archive(dec: Decision) -> None:
    with _state_lock:
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
    global _current_mode, _parallel_sema, _parallel_cap_for_sema
    with _state_lock:
        _pending.clear()
        _history.clear()
        _current_mode = OperationMode.supervised
    _parallel_sema = None
    _parallel_cap_for_sema = 0

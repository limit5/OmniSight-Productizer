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
from backend import audit as _audit

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


# Fix-B B7: threading.Lock is intentional — the holders (propose/resolve/
# list_pending/get/sweep_timeouts) are all synchronous, DB/SSE `await`s
# happen in wrappers *outside* the lock. Any async helper entering this
# module MUST do its awaits before/after the lock. Verified by
# `scripts/check_lock_await.py`.
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


class CapacityExhausted(Exception):
    """Raised when DRF per-tenant capacity cannot be acquired."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H2: host-load-aware precondition
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Before a _ModeSlot grants a slot we check the freshest host metrics
# snapshot (populated by backend.host_metrics.run_host_sampling_loop at
# 5s cadence). When CPU%, mem% or the whole-daemon running-container
# count is at/above threshold we refuse to grant the slot — the
# coordinator would otherwise keep stacking new agents onto an already
# saturated host and make the pressure worse.
#
# When the precondition is violated the waiter does NOT hold a slot —
# _shared_parallel is untouched during the wait. Each failed attempt
# emits a `sandbox.deferred` audit event + SSE with the breaching reason
# code, then sleeps for an exponentially growing interval capped at
# H2_BACKOFF_CAP_S (default 30s). The pattern is 1s, 2s, 4s, 8s, 16s,
# 30s, 30s, … so a transient spike burns cheap retries while a
# sustained high-pressure window doesn't spam the audit log.
#
# When no snapshot exists yet (cold-start grace window before the
# sampler has produced its first tick) we treat the host as "unknown"
# and allow the acquire to proceed, matching the convention already
# used by host_metrics.is_host_high_pressure.

H2_CPU_HIGH_PCT: float = float(_os.environ.get("OMNISIGHT_H2_CPU_HIGH_PCT", "85.0"))
H2_MEM_HIGH_PCT: float = float(_os.environ.get("OMNISIGHT_H2_MEM_HIGH_PCT", "85.0"))
# K — per-host running-container cap. Default 64 ≈ 4× the 16-core
# baseline so the precondition only trips when Docker itself is
# saturated, not during normal multi-tenant operation.
H2_CONTAINER_CAP: int = int(_os.environ.get("OMNISIGHT_H2_CONTAINER_CAP", "64"))

# Reason codes surfaced on the sandbox.deferred audit event and to
# tests that introspect *why* a precondition failed.
H2_REASON_CPU = "host_cpu_high"
H2_REASON_MEM = "host_mem_high"
H2_REASON_CONTAINER = "container_cap"

# H4a row 2581: queue-wait reasons. Emitted when the slot cannot be
# granted because the per-mode cap (composed with the AIMD-shaped
# budget) is saturated, or because the DRF per-tenant bucket is full.
# Unlike the H2 reasons above — which describe physical host pressure —
# these describe logical admission-control pressure and are fired once
# per acquire on entry to the queue wait (not per wakeup).
H4A_REASON_MODE_CAP = "mode_cap_saturated"
H4A_REASON_DRF = "drf_saturated"

# Exponential backoff schedule while the precondition is breached.
# Starts at H2_BACKOFF_BASE_S, doubles on every failed attempt, caps
# at H2_BACKOFF_CAP_S. Overridable via env for ops tuning + tests.
H2_BACKOFF_BASE_S: float = float(_os.environ.get("OMNISIGHT_H2_BACKOFF_BASE_S", "1.0"))
H2_BACKOFF_CAP_S: float = float(_os.environ.get("OMNISIGHT_H2_BACKOFF_CAP_S", "30.0"))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  H2 row 1513: Turbo auto-derate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# When the host CPU stays above H2_TURBO_DERATE_CPU_PCT for at least
# H2_TURBO_DERATE_SUSTAIN_S seconds, a session running in turbo mode is
# temporarily served the *supervised* parallel budget (2) instead of the
# turbo budget (8). The lower ceiling shrinks new-acquirer capacity
# without revoking in-flight holders, matching the existing "mode switch
# only affects fresh acquires" contract.
#
# Recovery is automatic once the CPU drops below the threshold AND the
# low-pressure condition persists for H2_TURBO_RECOVER_COOLDOWN_S
# seconds. A CPU spike mid-cooldown interrupts the cooldown — the system
# stays derated until a full cooldown window passes below-threshold.
#
# The state machine advances in two places:
#   1. The host sampling loop (5s cadence) calls evaluate_turbo_derate()
#      once per tick to progress sustained / cooldown timers naturally.
#   2. Each _ModeSlot.acquire() also calls it so the precondition is
#      re-checked even in test runners where the sampling loop isn't
#      running.

H2_TURBO_DERATE_CPU_PCT: float = float(
    _os.environ.get("OMNISIGHT_H2_TURBO_DERATE_CPU_PCT", "80.0")
)
H2_TURBO_DERATE_SUSTAIN_S: float = float(
    _os.environ.get("OMNISIGHT_H2_TURBO_DERATE_SUSTAIN_S", "30.0")
)
H2_TURBO_RECOVER_COOLDOWN_S: float = float(
    _os.environ.get("OMNISIGHT_H2_TURBO_RECOVER_COOLDOWN_S", "120.0")
)


class TurboConfirmRequired(Exception):
    """Raised when switching to turbo mode while h2_auto_derate=false
    without an explicit confirm flag. See `is_auto_derate_enabled`."""


def is_auto_derate_enabled() -> bool:
    """Return whether the turbo auto-derate safety net is engaged.

    Read live from ``backend.config.settings`` so tests / ops can flip
    the flag at runtime (``settings.h2_auto_derate = False``) without
    re-importing this module.
    """
    try:
        from backend.config import settings as _settings
        return bool(getattr(_settings, "h2_auto_derate", True))
    except Exception:
        return True


@dataclass
class _TurboDerateState:
    derate_active: bool = False
    # First time CPU crossed above threshold while not yet derated.
    # Reset to None on every below-threshold sample.
    high_cpu_since: float | None = None
    # First time CPU dropped at/below threshold after derate started.
    # Reset to None on any above-threshold sample so a spike interrupts
    # the cooldown (the 2-min cooldown must be continuous).
    low_cpu_since: float | None = None
    # Wall-clock timestamp of the last derate/recover transition.
    # Surfaced on the coordinator.turbo_derate / .turbo_recover events
    # and read by tests.
    last_transition_at: float | None = None


_turbo_derate_state = _TurboDerateState()


def _emit_turbo_transition(event: str, payload: dict[str, Any]) -> None:
    """Best-effort SSE emit + Phase-53 hash-chain audit for turbo_derate /
    turbo_recover transitions.

    The SSE bus broadcast lights up the live coordinator panel; the audit
    row gives operators an after-the-fact reconstruction of *why* a turbo
    session was throttled (CPU sample, sustain elapsed, the budget swap)
    — chained into the per-tenant Phase-53 audit log so any post-hoc
    tampering breaks ``audit.verify_chain``.
    """
    logger.info("coordinator turbo transition: %s %s", event, payload)
    try:
        from backend.events import bus as _bus
        _bus.publish(event, payload)
    except Exception as exc:
        logger.debug("%s SSE publish failed: %s", event, exc)

    # Phase 53 hash-chain audit: every derate / recover decision is a
    # state-changing operation worth persisting. Mirrors the
    # `sandbox.deferred` audit pattern used by the H2 precondition path.
    is_derate = event.endswith("turbo_derate")
    direction = "engaged" if is_derate else "recovered"
    try:
        _audit.log_sync(
            action=event,
            entity_kind="turbo_derate",
            entity_id=direction,
            before={"derate_active": not is_derate},
            after={"derate_active": is_derate, **payload},
        )
    except Exception as exc:
        logger.debug("%s audit log failed: %s", event, exc)


def evaluate_turbo_derate(
    *,
    now: float | None = None,
    cpu_percent: float | None = None,
) -> bool:
    """Advance the turbo auto-derate state machine and return derate_active.

    When *cpu_percent* is None the function reads the latest
    HostSnapshot from ``backend.host_metrics``. When no snapshot
    exists (cold start), the state is left unchanged — we neither
    derate nor recover on unknown host pressure.

    Safe to call from sync contexts (sampling loop, _ModeSlot.acquire).
    State mutation is protected by _state_lock. Transitions (derate
    engaged / recovered) emit coordinator.turbo_derate /
    coordinator.turbo_recover SSE events while the lock is NOT held.

    When ``settings.h2_auto_derate`` is False the safety net is off: we
    neither engage nor recover on our own, and leave the state machine
    frozen at whatever it last observed. A caller that disabled the
    switch *while* derate was active stays derated — they have to flip
    the flag back on (or call ``clear_turbo_derate()``) to recover.
    """
    t = now if now is not None else time.time()

    if not is_auto_derate_enabled():
        with _state_lock:
            return _turbo_derate_state.derate_active

    if cpu_percent is None:
        try:
            from backend import host_metrics as _hm
            snap = _hm.get_latest_host_snapshot()
        except Exception:
            snap = None
        if snap is None:
            with _state_lock:
                return _turbo_derate_state.derate_active
        cpu_percent = float(snap.host.cpu_percent)

    transition_event: str | None = None
    transition_payload: dict[str, Any] | None = None
    with _state_lock:
        state = _turbo_derate_state
        above = cpu_percent > H2_TURBO_DERATE_CPU_PCT
        if above:
            # CPU spike — cancel any in-progress cooldown.
            state.low_cpu_since = None
            if not state.derate_active:
                if state.high_cpu_since is None:
                    state.high_cpu_since = t
                if t - state.high_cpu_since >= H2_TURBO_DERATE_SUSTAIN_S:
                    state.derate_active = True
                    state.last_transition_at = t
                    transition_event = "coordinator.turbo_derate"
                    transition_payload = {
                        "cpu_percent": cpu_percent,
                        "threshold_pct": H2_TURBO_DERATE_CPU_PCT,
                        "sustained_s": t - state.high_cpu_since,
                        "sustain_required_s": H2_TURBO_DERATE_SUSTAIN_S,
                        "derated_to_budget": _PARALLEL_BUDGET[OperationMode.supervised],
                        "from_budget": _PARALLEL_BUDGET[OperationMode.turbo],
                        "at": t,
                    }
                    state.high_cpu_since = None
        else:
            state.high_cpu_since = None
            if state.derate_active:
                if state.low_cpu_since is None:
                    state.low_cpu_since = t
                if t - state.low_cpu_since >= H2_TURBO_RECOVER_COOLDOWN_S:
                    state.derate_active = False
                    cooldown_elapsed = t - state.low_cpu_since
                    state.low_cpu_since = None
                    state.last_transition_at = t
                    transition_event = "coordinator.turbo_recover"
                    transition_payload = {
                        "cpu_percent": cpu_percent,
                        "threshold_pct": H2_TURBO_DERATE_CPU_PCT,
                        "cooldown_s": cooldown_elapsed,
                        "cooldown_required_s": H2_TURBO_RECOVER_COOLDOWN_S,
                        "restored_to_budget": _PARALLEL_BUDGET[OperationMode.turbo],
                        "at": t,
                    }
        active = state.derate_active

    if transition_event is not None and transition_payload is not None:
        _emit_turbo_transition(transition_event, transition_payload)
    return active


def is_turbo_derated() -> bool:
    """Read-only accessor for the current derate flag (observational)."""
    with _state_lock:
        return _turbo_derate_state.derate_active


def turbo_derate_snapshot() -> dict[str, Any]:
    """Expose the current state machine for tests + UI telemetry."""
    with _state_lock:
        s = _turbo_derate_state
        return {
            "derate_active": s.derate_active,
            "high_cpu_since": s.high_cpu_since,
            "low_cpu_since": s.low_cpu_since,
            "last_transition_at": s.last_transition_at,
            "threshold_pct": H2_TURBO_DERATE_CPU_PCT,
            "sustain_required_s": H2_TURBO_DERATE_SUSTAIN_S,
            "cooldown_required_s": H2_TURBO_RECOVER_COOLDOWN_S,
            "auto_derate_enabled": is_auto_derate_enabled(),
        }


def clear_turbo_derate() -> bool:
    """Force-clear an active derate state. Used by operators who flipped
    ``h2_auto_derate=false`` while derate was engaged and want the cap
    lifted back to turbo budget without waiting for the 2-min cooldown
    (which is disabled along with the auto-engage path). Returns True
    if a transition occurred, False if already inactive.
    """
    changed = False
    with _state_lock:
        if _turbo_derate_state.derate_active:
            _turbo_derate_state.derate_active = False
            _turbo_derate_state.last_transition_at = time.time()
            _turbo_derate_state.high_cpu_since = None
            _turbo_derate_state.low_cpu_since = None
            changed = True
    if changed:
        _emit_turbo_transition(
            "coordinator.turbo_recover",
            {
                "cpu_percent": None,
                "threshold_pct": H2_TURBO_DERATE_CPU_PCT,
                "cooldown_s": 0.0,
                "cooldown_required_s": H2_TURBO_RECOVER_COOLDOWN_S,
                "restored_to_budget": _PARALLEL_BUDGET[OperationMode.turbo],
                "manual_clear": True,
                "at": time.time(),
            },
        )
    return changed


def _effective_budget(mode: OperationMode) -> int:
    """Parallel budget with the turbo-derate override applied.

    Only turbo is affected — supervised / full_auto / manual keep
    their static budgets. When derate is active, a turbo session is
    served the supervised budget (2) until the cooldown completes.
    """
    if mode == OperationMode.turbo:
        if is_turbo_derated():
            return _PARALLEL_BUDGET[OperationMode.supervised]
    return _PARALLEL_BUDGET[mode]


def _compose_effective_cap(mode: OperationMode) -> int:
    """H4a row 2581 — compose the mode cap with the AIMD-shaped budget.

    The mode-cap path honours three ceilings at once:

    * ``_effective_budget(mode)`` — the static per-mode parallelism
      budget, with an active turbo-derate override.
    * :func:`backend.adaptive_budget.effective_budget` — the mode's
      multiplier (row 2580) against ``CAPACITY_MAX``, further floored
      by the live AIMD budget (row 2575).

    ``min()`` of the two is the admission ceiling. Whichever governor
    is tighter right now wins — typically the static ``_PARALLEL_BUDGET``
    on a quiet host (supervised=2, turbo=8), but the AIMD-derived
    ``effective_budget`` takes over after sustained CPU/mem pressure
    halves the global budget. Floored at 1 so the tightest composition
    still grants at least one slot (matches the anti-deadlock floor in
    ``sandbox_capacity._effective_capacity_max_locked`` and
    ``adaptive_budget.effective_budget``).
    """
    mode_cap = _effective_budget(mode)
    try:
        from backend import adaptive_budget as _ab
        aimd_cap = _ab.effective_budget(mode)
    except Exception as exc:
        logger.debug("adaptive_budget unavailable, using mode cap: %s", exc)
        return mode_cap
    return max(1, min(mode_cap, aimd_cap))


def _h2_backoff_delay(attempt: int) -> float:
    """Exponential backoff: base * 2^(attempt-1), capped at H2_BACKOFF_CAP_S.

    Attempt numbers start at 1 (first defer). Returned value is always
    ``>= 0``; callers may pass the attempt to ``asyncio.sleep``.
    """
    if attempt < 1:
        return 0.0
    delay = H2_BACKOFF_BASE_S * (2 ** (attempt - 1))
    return min(delay, H2_BACKOFF_CAP_S)


def _emit_sandbox_deferred(
    reason: str,
    *,
    attempt: int,
    delay_s: float,
    tenant_id: str | None,
    session_token: str | None,
    snapshot_summary: dict[str, Any] | None,
) -> None:
    """Emit both the SSE `sandbox.deferred` event and an audit row.

    Best-effort: neither failure prevents the caller from continuing
    its backoff loop — the precondition itself is the source of truth
    for whether to proceed, the audit is just the paper trail.
    """
    payload = {
        "reason": reason,
        "attempt": attempt,
        "delay_s": delay_s,
        "backoff_cap_s": H2_BACKOFF_CAP_S,
        "tenant_id": tenant_id or "",
        "session_id": session_token[:8] if session_token else "",
        "host_snapshot": snapshot_summary or {},
    }
    try:
        from backend.events import bus as _bus
        _bus.publish("sandbox.deferred", payload, tenant_id=tenant_id)
    except Exception as exc:
        logger.debug("sandbox.deferred SSE publish failed: %s", exc)
    try:
        _audit.log_sync(
            action="sandbox.deferred",
            entity_kind="sandbox_slot",
            entity_id=reason,
            before=None,
            after=payload,
            session_id=session_token,
        )
    except Exception as exc:
        logger.debug("sandbox.deferred audit log failed: %s", exc)


def _emit_capacity_deferred(
    reason: str,
    *,
    cost: int,
    cap: int,
    in_flight: int,
    tenant_id: str | None,
    session_token: str | None,
) -> None:
    """H4a row 2581 — emit ``sandbox.deferred`` when a token-based acquire
    is queued behind admission-control (mode-cap or DRF), as distinct
    from the H2 host-precondition path (:func:`_emit_sandbox_deferred`).

    Fires once on entry to the wait — *not* on every condvar wakeup —
    so the audit trail records "caller A blocked for C tokens because
    the pool had U/cap" without thundering-herd spam when many waiters
    exist.

    Reuses the ``sandbox.deferred`` SSE event name so existing
    consumers (ops dashboard, UI toast) surface mode-cap/DRF waits
    alongside host-pressure waits — callers discriminate via
    ``payload["reason"]``.
    """
    payload = {
        "reason": reason,
        "cost": int(cost),
        "cap": int(cap),
        "in_flight": int(in_flight),
        "tenant_id": tenant_id or "",
        "session_id": session_token[:8] if session_token else "",
    }
    try:
        from backend.events import bus as _bus
        _bus.publish("sandbox.deferred", payload, tenant_id=tenant_id)
    except Exception as exc:
        logger.debug("sandbox.deferred (capacity) publish failed: %s", exc)
    try:
        _audit.log_sync(
            action="sandbox.deferred",
            entity_kind="sandbox_slot",
            entity_id=reason,
            before=None,
            after=payload,
            session_id=session_token,
        )
    except Exception as exc:
        logger.debug("sandbox.deferred (capacity) audit failed: %s", exc)


def _host_snapshot_summary() -> dict[str, Any] | None:
    """Return a compact snapshot summary for inclusion in the deferred
    event, or ``None`` if host_metrics is unavailable / empty."""
    try:
        from backend import host_metrics as _hm
        snap = _hm.get_latest_host_snapshot()
    except Exception:
        return None
    if snap is None:
        return None
    return {
        "cpu_percent": snap.host.cpu_percent,
        "mem_percent": snap.host.mem_percent,
        "container_count": snap.docker.container_count,
    }


def _host_precondition_reason() -> str | None:
    """Return None if the host has headroom, else a reason code.

    Reads the latest ``HostSnapshot`` from ``backend.host_metrics``.
    Returns ``None`` (allow acquire) when:
      * no snapshot has landed yet (cold start), or
      * the host_metrics module is unimportable (dev/test env without
        psutil); failing open here avoids deadlocking fresh test
        runners that never boot the sampling loop.

    Returns one of ``H2_REASON_*`` when a threshold is breached.
    CPU is checked first, then memory, then container count — the
    order matches the TODO spec so reason-code precedence is stable
    across callers.
    """
    try:
        from backend import host_metrics as _hm
        snap = _hm.get_latest_host_snapshot()
    except Exception as exc:
        logger.debug("H2 precondition: host_metrics unavailable: %s", exc)
        return None
    if snap is None:
        return None
    if snap.host.cpu_percent >= H2_CPU_HIGH_PCT:
        return H2_REASON_CPU
    if snap.host.mem_percent >= H2_MEM_HIGH_PCT:
        return H2_REASON_MEM
    if snap.docker.container_count >= H2_CONTAINER_CAP:
        return H2_REASON_CONTAINER
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Parallelism budget
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# N4: replaced bare Semaphore with an explicit counter + condvar so we can
# atomically enforce the *current* cap on every acquire, regardless of how
# many acquires are already in flight. Rebuilding the Semaphore on mode
# change let existing holders keep the old cap.
#
# I10: _parallel_in_flight is now shared across workers via Redis counter.
# The local _parallel_async_cond still coordinates within a single worker's
# event loop; cross-worker coordination uses the Redis counter as the
# source of truth for the global slot count.
_parallel_lock = threading.Lock()
_parallel_async_cond: asyncio.Condition | None = None

from backend.shared_state import SharedCounter as _SharedCounter, SharedKV as _SharedKV
_shared_parallel = _SharedCounter("parallel_in_flight")
_shared_mode = _SharedKV("decision_engine")


class _ModeSlot:
    """Async context manager that acquires a slot under the *current* cap.

    Unlike a fixed-cap Semaphore, this reads the cap at ``__aenter__``
    time from the per-session mode (J5) or global fallback, so a mode
    switch immediately tightens or loosens the limit for new acquirers.
    Existing holders retain their slot until they release.

    I6: when *tenant_id* is set, delegates to sandbox_capacity for
    DRF-based per-tenant token budgeting. The mode cap still applies
    as a parallel session-level ceiling, but the global token pool is
    managed by the DRF module.

    H4a row 2581: the mode-cap path is now *token-based* — :attr:`_cost`
    is the number of slots this acquire consumes against the shared
    counter, and the cap is composed with
    :func:`backend.adaptive_budget.effective_budget` so AIMD host-load
    shaping bites even on non-tenant acquires. When the first peek shows
    the request would block, a ``sandbox.deferred`` event fires (reason
    ``mode_cap_saturated`` / ``drf_saturated``) so the ops dashboard can
    surface admission queueing alongside H2 host-pressure deferrals.
    """

    def __init__(
        self,
        session_token: str | None = None,
        tenant_id: str | None = None,
        cost: int | float = 1,
    ) -> None:
        self._session_token = session_token
        self._tenant_id = tenant_id
        self._cost = cost
        self._drf_acquired = False
        # H4a row 2581: remember the per-acquire integer token counts
        # actually charged to ``_shared_parallel`` so every ``release``
        # decrements by the same amount even if the caller mutated
        # ``_cost`` between acquire and release. A stack (not a scalar)
        # because ``_mode_slot_singleton`` is re-entered by multiple
        # callers in the non-tenant path — concurrent/overlapping
        # acquires must each reserve and un-reserve their own cost
        # without trampling each other.
        self._reservations: list[int] = []

    def _mode_cap_tokens(self) -> int:
        """Integer token count this acquire consumes on the mode-cap path.

        Floors at 1 (an acquire must consume at least one token or it's
        a no-op; and ``ssh_remote=0.5`` would otherwise round to 0),
        and integer-casts fractional costs up so a 0.5-cost acquire
        still accounts for one token on the per-worker shared counter.
        """
        c = self._cost
        if c is None:
            return 1
        if isinstance(c, float):
            # Round up — half a token still occupies one shared-counter
            # slot because the counter is integer and the cost < 1
            # weights exist to under-charge the DRF float pool, not the
            # per-worker parallel-slot gauge.
            return max(1, int(c + 0.5)) if c > 0 else 1
        return max(1, int(c))

    def _get_cap(self) -> int:
        """Return the *mode-level* admission cap (turbo-derate aware).

        Does **not** fold in the AIMD-shaped ceiling from
        :mod:`backend.adaptive_budget` — that composition happens only
        inside :meth:`__aenter__`'s admission loop so external callers
        (tests, ops UI) can still read "what does the mode allow"
        independently of the live host-pressure shaping.
        """
        mode = get_session_mode(self._session_token) if self._session_token else get_mode()
        # Refresh the turbo-derate state machine so a sustained high-CPU
        # window tightens the cap even if the sampling loop hasn't had
        # a chance to tick yet (tests, cold-start).
        evaluate_turbo_derate()
        return _effective_budget(mode)

    async def _get_cap_async(self) -> int:
        """Async variant of :meth:`_get_cap` — same mode-level-only
        semantics. The H4a composition with ``adaptive_budget`` happens
        inside :meth:`__aenter__` on the shared-cap path."""
        if self._session_token:
            mode = await get_session_mode_async(self._session_token)
        else:
            mode = get_mode()
        evaluate_turbo_derate()
        return _effective_budget(mode)

    async def _admission_cap_async(self) -> int:
        """Composed admission ceiling — ``min(mode-cap, AIMD budget)``.

        Called inside :meth:`__aenter__` to compute the effective
        token-bucket ceiling. H4a row 2581 widens this beyond the
        static mode budget by narrowing further with
        :func:`backend.adaptive_budget.effective_budget` so AIMD
        pressure tightens admission without touching the turbo-derate
        state machine (which has its own cooldown semantics).
        """
        if self._session_token:
            mode = await get_session_mode_async(self._session_token)
        else:
            mode = get_mode()
        evaluate_turbo_derate()
        return _compose_effective_cap(mode)

    def _is_turbo(self) -> bool:
        mode = get_session_mode(self._session_token) if self._session_token else get_mode()
        return mode == OperationMode.turbo

    async def __aenter__(self) -> "_ModeSlot":
        global _parallel_async_cond
        if _parallel_async_cond is None:
            _parallel_async_cond = asyncio.Condition()

        # H2 precondition: don't grant a slot while the host is above
        # CPU/mem/container thresholds. Applies to BOTH the DRF and
        # the mode-cap path — host pressure is a property of the
        # physical box, not of the tenant bucket. Each breach emits a
        # sandbox.deferred audit event + SSE and sleeps with exponential
        # backoff (cap H2_BACKOFF_CAP_S). The slot counter is NOT
        # incremented during the wait.
        defer_attempt = 0
        while True:
            reason = _host_precondition_reason()
            if reason is None:
                break
            defer_attempt += 1
            delay = _h2_backoff_delay(defer_attempt)
            _emit_sandbox_deferred(
                reason,
                attempt=defer_attempt,
                delay_s=delay,
                tenant_id=self._tenant_id,
                session_token=self._session_token,
                snapshot_summary=_host_snapshot_summary(),
            )
            await asyncio.sleep(delay)

        if self._tenant_id is not None:
            from backend import sandbox_capacity as _sc
            # H4a row 2581: peek once so we can emit sandbox.deferred
            # exactly on entry to the DRF wait, without polluting the
            # audit log for instant-success acquires.
            if not _sc.try_acquire(self._tenant_id, self._cost, self._is_turbo()):
                _emit_capacity_deferred(
                    H4A_REASON_DRF,
                    cost=int(self._cost) if isinstance(self._cost, int) else max(1, int(float(self._cost) + 0.5)),
                    cap=int(_sc.effective_capacity_max()),
                    in_flight=_shared_parallel.get(),
                    tenant_id=self._tenant_id,
                    session_token=self._session_token,
                )
                ok = await _sc.acquire_with_reclaim(
                    tenant_id=self._tenant_id,
                    cost=self._cost,
                    is_turbo=self._is_turbo(),
                    timeout_s=60.0,
                )
                if not ok:
                    raise CapacityExhausted(
                        f"DRF capacity exhausted for tenant {self._tenant_id}"
                    )
            self._drf_acquired = True
            # The DRF path tracks slot-sessions on the cross-worker
            # counter at cost=1 (tokens are already enforced by
            # sandbox_capacity._buckets). The mode-cap path uses the
            # counter as the token-bucket itself — see below.
            _shared_parallel.increment()
            self._reservations.append(1)
            return self

        # ─── H4a row 2581: token-based mode-cap admission ────
        # Token cost for this acquire; clamp to the current cap so a
        # cost > cap can't deadlock (classic anti-deadlock rule for
        # fixed-window schedulers — an oversize request is treated as
        # "take the whole cap" rather than "wait forever").
        tokens = self._mode_cap_tokens()
        deferred_emitted = False
        while True:
            async with _parallel_async_cond:
                cap = await self._admission_cap_async()
                tokens_clamped = max(1, min(tokens, cap))
                current = _shared_parallel.get()
                if current + tokens_clamped <= cap:
                    _shared_parallel.increment(tokens_clamped)
                    self._reservations.append(tokens_clamped)
                    return self
                if not deferred_emitted:
                    _emit_capacity_deferred(
                        H4A_REASON_MODE_CAP,
                        cost=tokens_clamped,
                        cap=cap,
                        in_flight=current,
                        tenant_id=self._tenant_id,
                        session_token=self._session_token,
                    )
                    deferred_emitted = True
                await _parallel_async_cond.wait()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        global _parallel_async_cond

        if self._drf_acquired:
            from backend import sandbox_capacity as _sc
            _sc.release(self._tenant_id, self._cost)
            self._drf_acquired = False

        if self._reservations:
            _shared_parallel.decrement(self._reservations.pop())
        if _parallel_async_cond is not None:
            async with _parallel_async_cond:
                _parallel_async_cond.notify_all()

    def locked(self) -> bool:
        if self._tenant_id is not None:
            from backend import sandbox_capacity as _sc
            # Non-destructive probe: try_acquire mutates the bucket on
            # success, so we roll back immediately — tests + UI read
            # `locked()` without taking a slot.
            if _sc.try_acquire(self._tenant_id, self._cost, self._is_turbo()):
                _sc.release(self._tenant_id, self._cost)
                return False
            return True
        # ``locked()`` is a peek for callers (UI / pre-check) — reflect
        # the same composed admission ceiling as ``__aenter__`` so
        # "not locked" really means "acquire will not queue".
        mode = get_session_mode(self._session_token) if self._session_token else get_mode()
        evaluate_turbo_derate()
        cap = _compose_effective_cap(mode)
        tokens = self._mode_cap_tokens()
        return _shared_parallel.get() + max(1, min(tokens, cap)) > cap

    async def acquire(self, cost: int | None = None) -> bool:
        """H4a row 2581 — token-based acquire.

        When *cost* is given, overrides the constructor-supplied cost
        for this single acquire/release cycle so callers who know the
        per-invocation workload size (e.g. ``4`` tokens for a
        local-compile sandbox) can size their admission without
        re-instantiating the slot. The cost the caller asked for is
        clamped to the current mode/AIMD cap inside ``__aenter__`` to
        prevent a cost>cap deadlock.
        """
        if cost is not None:
            self._cost = cost
        await self.__aenter__()
        return True

    def release(self) -> None:
        if self._drf_acquired:
            from backend import sandbox_capacity as _sc
            _sc.release(self._tenant_id, self._cost)
            self._drf_acquired = False
        if self._reservations:
            _shared_parallel.decrement(self._reservations.pop())


_mode_slot_singleton = _ModeSlot()


def parallel_slot(
    session_token: str | None = None,
    tenant_id: str | None = None,
    cost: float = 1.0,
) -> _ModeSlot:
    """Acquire this in invoke/pipeline runs to respect mode parallelism.

    Use as::

        async with decision_engine.parallel_slot():
            ...real work...

    Pass *session_token* to use the per-session mode cap (J5).
    Pass *tenant_id* + *cost* to use DRF per-tenant capacity (I6).
    The cap is re-read on every acquire; switching mode mid-flight takes
    effect immediately for *new* acquirers without revoking existing ones.
    """
    if session_token or tenant_id:
        return _ModeSlot(
            session_token=session_token,
            tenant_id=tenant_id,
            cost=cost,
        )
    return _mode_slot_singleton


def parallel_in_flight() -> int:
    """Current number of held slots (observational / SSE telemetry)."""
    return _shared_parallel.get()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Mode API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def get_mode() -> OperationMode:
    """Return the global (fallback) operation mode."""
    stored = _shared_mode.get("current_mode")
    if stored:
        try:
            return OperationMode(stored)
        except ValueError:
            pass
    with _state_lock:
        return _current_mode


def get_session_mode(session_token: str | None) -> OperationMode:
    """Return the operation mode for a specific session.

    Reads from sessions.metadata.operation_mode; falls back to the
    global mode if the session has no per-session override.
    """
    if not session_token:
        return get_mode()
    try:
        import asyncio
        from backend import auth as _auth
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                sess = pool.submit(asyncio.run, _auth.get_session(session_token)).result()
        else:
            sess = loop.run_until_complete(_auth.get_session(session_token))
        if sess:
            meta = _auth.get_session_metadata(sess)
            sm = meta.get("operation_mode")
            if sm:
                try:
                    return OperationMode(sm)
                except ValueError:
                    pass
    except Exception as exc:
        logger.debug("get_session_mode failed: %s", exc)
    return get_mode()


async def get_session_mode_async(session_token: str | None) -> OperationMode:
    """Async variant of get_session_mode."""
    if not session_token:
        return get_mode()
    try:
        from backend import auth as _auth
        sess = await _auth.get_session(session_token)
        if sess:
            meta = _auth.get_session_metadata(sess)
            sm = meta.get("operation_mode")
            if sm:
                try:
                    return OperationMode(sm)
                except ValueError:
                    pass
    except Exception as exc:
        logger.debug("get_session_mode_async failed: %s", exc)
    return get_mode()


async def set_session_mode(
    session_token: str,
    mode: OperationMode | str,
    *,
    confirm_turbo: bool = False,
) -> OperationMode:
    """Set the operation mode for a specific session. Emits SSE `mode_changed`.

    When ``settings.h2_auto_derate`` is False the turbo safety net is
    disabled. In that configuration, switching **into** turbo requires
    ``confirm_turbo=True`` — otherwise ``TurboConfirmRequired`` is
    raised so the caller explicitly acknowledges they're running
    without the auto-derate backstop.
    """
    if isinstance(mode, str):
        try:
            mode = OperationMode(mode)
        except ValueError as exc:
            raise ValueError(f"unknown mode: {mode}") from exc
    if (
        mode == OperationMode.turbo
        and not is_auto_derate_enabled()
        and not confirm_turbo
    ):
        raise TurboConfirmRequired(
            "h2_auto_derate is disabled; switching to turbo requires "
            "explicit confirm_turbo=True (the host has no auto-shrink "
            "safety net under sustained CPU pressure)."
        )
    from backend import auth as _auth
    prev_mode = await get_session_mode_async(session_token)
    await _auth.update_session_metadata(session_token, {"operation_mode": mode.value})
    new_cap = _PARALLEL_BUDGET[mode]
    cur_inflight = parallel_in_flight()
    try:
        from backend.events import bus as _bus
        payload = {
            "mode": mode.value,
            "previous": prev_mode.value,
            "parallel_cap": new_cap,
            "in_flight": cur_inflight,
            "over_cap": max(0, cur_inflight - new_cap),
            "session_scoped": True,
        }
        _bus.publish("mode_changed", payload)
    except Exception as _exc:
        logger.warning("mode_changed publish failed: %s", _exc)
    logger.info("OperationMode (session): %s → %s", prev_mode.value, mode.value)
    try:
        _audit.log_sync(
            action="mode_change", entity_kind="operation_mode",
            entity_id=f"session:{session_token[:8]}",
            before={"mode": prev_mode.value},
            after={"mode": mode.value, "parallel_cap": new_cap},
        )
    except Exception as exc:
        logger.debug("mode_change audit failed (non-fatal): %s", exc)
    return mode


def set_mode(
    mode: OperationMode | str,
    *,
    confirm_turbo: bool = False,
) -> OperationMode:
    """Switch the global (fallback) operation mode. Emits SSE `mode_changed`.

    When ``settings.h2_auto_derate`` is False, switching *into* turbo
    requires ``confirm_turbo=True`` — see :class:`TurboConfirmRequired`
    for the rationale.
    """
    global _current_mode
    if isinstance(mode, str):
        try:
            mode = OperationMode(mode)
        except ValueError as exc:
            raise ValueError(f"unknown mode: {mode}") from exc
    if (
        mode == OperationMode.turbo
        and not is_auto_derate_enabled()
        and not confirm_turbo
    ):
        raise TurboConfirmRequired(
            "h2_auto_derate is disabled; switching to turbo requires "
            "explicit confirm_turbo=True (the host has no auto-shrink "
            "safety net under sustained CPU pressure)."
        )
    with _state_lock:
        prev = _current_mode
        _current_mode = mode
    _shared_mode.set("current_mode", mode.value)
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
        logger.warning("mode_changed publish failed: %s", _exc)
    logger.info("OperationMode: %s → %s", prev.value, mode.value)
    try:
        _audit.log_sync(
            action="mode_change", entity_kind="operation_mode", entity_id="global",
            before={"mode": prev.value}, after={"mode": mode.value, "parallel_cap": new_cap},
        )
    except Exception as exc:
        logger.debug("mode_change audit failed (non-fatal): %s", exc)
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
                loop = asyncio.get_event_loop()
                loop.create_task(_log_auto_decision(
                    decision_id=dec.id, kind=kind, severity=severity.value,
                    chosen_option=dec.chosen_option_id, confidence=profile_confidence,
                    rationale=profile_rationale, profile_id=profile_id_used,
                    auto_executed_at=now,
                ))
            except Exception as exc:
                logger.debug("auto_decision log schedule failed: %s", exc)
        _archive(dec)
        _emit("decision_auto_executed", dec)
        # Phase 52 metric: count + record resolve duration
        try:
            from backend import metrics as _m
            _m.decision_total.labels(
                kind=kind, severity=severity.value, status=dec.status.value
            ).inc()
            _m.decision_resolve_seconds.labels(
                kind=kind, severity=severity.value, resolver="auto"
            ).observe(0.0)  # auto resolved instantly
        except Exception as exc:
            logger.debug("decision metrics failed: %s", exc)
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
    try:
        from backend import metrics as _m
        _m.decision_total.labels(
            kind=kind, severity=severity.value, status="pending"
        ).inc()
    except Exception as exc:
        logger.debug("decision pending metric failed: %s", exc)
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
        _audit.log_sync(
            action="decision_resolve", entity_kind="decision", entity_id=dec.id,
            before={"status": "pending", "kind": dec.kind, "severity": dec.severity.value},
            after={"status": dec.status.value, "chosen_option_id": dec.chosen_option_id,
                   "resolver": dec.resolver},
            actor=resolver,
        )
    except Exception as exc:
        logger.debug("decision resolve audit failed: %s", exc)
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
                    _audit.log_sync(
                        action="decision_undo", entity_kind="decision", entity_id=d.id,
                        before={"status": prev_status},
                        after={"status": "undone"},
                    )
                except Exception as exc:
                    logger.debug("decision undo audit failed: %s", exc)
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
    list / bulk-undo. Best-effort; failures logged at warning.

    SP-5.6b (2026-04-21): ported to pool. The insert is single-
    statement so no tx wrap needed; asyncpg auto-commits on
    statement exit.
    """
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO auto_decision_log "
                "(decision_id, kind, severity, chosen_option, confidence, "
                " rationale, profile_id, auto_executed_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                decision_id, kind, severity, chosen_option, confidence,
                rationale[:240], profile_id, auto_executed_at,
            )
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
    global _current_mode, _parallel_async_cond
    with _state_lock:
        _pending.clear()
        _history.clear()
        _current_mode = OperationMode.supervised
        _turbo_derate_state.derate_active = False
        _turbo_derate_state.high_cpu_since = None
        _turbo_derate_state.low_cpu_since = None
        _turbo_derate_state.last_transition_at = None
    _shared_parallel.set(0)
    _shared_mode.set("current_mode", "supervised")
    _parallel_async_cond = None
    # H4a row 2581: the singleton carries per-acquire token state
    # across tests that re-use ``parallel_slot()`` without a fresh
    # instance; clear it so a prior test's abort can't leak a reserved
    # cost into the next test's shared-counter view.
    _mode_slot_singleton._reservations.clear()
    _mode_slot_singleton._drf_acquired = False
    _mode_slot_singleton._cost = 1


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

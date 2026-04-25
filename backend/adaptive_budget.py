"""H4a — Global AIMD controller for the sandbox concurrency budget.

Distinct from :mod:`backend.tenant_aimd` (M4):
* ``tenant_aimd`` → per-tenant multiplicative-decrease that picks an
  outlier tenant when the host is hot; punishes the noisy neighbour
  rather than flat-rate everybody.
* ``adaptive_budget`` (this module) → host-level concurrency BUDGET that
  AIMD-shapes against the global host CPU/mem signal. Treats
  :data:`backend.sandbox_capacity.CAPACITY_MAX` as the hard ceiling;
  the AIMD-derived budget never exceeds it. Downstream consumers
  (H4a row 2580 mode multiplier + row 2581 token-based
  ``_ModeSlot.acquire``) compose this with the per-mode multiplier
  to get an effective per-tick admission cap.

Control law — TODO H4a row 2575 spec:

* ``Init budget = 6`` (≈ ``CAPACITY_MAX / 2``, "safe boot"). On hosts
  smaller than the 16c/64GB reference rig the seed is clamped to
  ``min(CAPACITY_MAX, INIT_BUDGET)`` so a 1c/1GB dev box doesn't try to
  start above its physical ceiling.
* **Additive increase**: every ``AI_INTERVAL_S`` (30s) wall-clock if
  ``cpu_percent < CPU_AI_THRESHOLD_PCT`` (70) AND
  ``mem_percent < MEM_AI_THRESHOLD_PCT`` (70) AND
  ``deferred_count == 0`` → ``budget += 1``.
* **Multiplicative decrease**: ``cpu_percent > CPU_MD_THRESHOLD_PCT``
  (85) OR ``mem_percent > MEM_MD_THRESHOLD_PCT`` (85) sustained for
  ``MD_PERSISTENCE_S`` (10s) → ``budget = max(FLOOR_BUDGET, budget // 2)``.
* **Hard cap**: ``budget`` is always clamped to
  ``[FLOOR_BUDGET, CAPACITY_MAX]``.

The controller is *pure with respect to its host-metric inputs*: it
never reads psutil itself — callers pass already-sampled CPU%, mem%,
and ``deferred_count`` so tests can drive the control loop with
synthetic time. :func:`evaluate_from_host_snapshot` is the convenience
wiring helper for the production sampling loop.

Module-global state audit (SOP Step 1):
``_state`` is a module-level dataclass instance — same pattern as
:mod:`backend.sandbox_capacity._buckets` and
:mod:`backend.tenant_aimd._state`. AIMD budget is intentionally
**per-uvicorn-worker**: every worker observes the same host CPU/mem
via psutil so AI/MD decisions converge naturally (qualifying answer #1
from ``docs/sop/implement_phase_step.md`` — "不共享，因為每 worker 從
同樣來源推導出同樣的值"). Cross-worker budget coordination is **not**
needed because each worker also runs its own
``sandbox_capacity._buckets`` admission state — the per-worker AIMD
ceiling shapes that worker's own queue.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from backend.sandbox_capacity import CAPACITY_MAX

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Knobs (env-overridable for ops tuning)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INIT_BUDGET: int = int(os.environ.get("OMNISIGHT_AIMD_INIT_BUDGET", "6"))
"""H4a 'Init budget = 6 (≈ CAPACITY_MAX/2 safe boot)'.

Default 6 matches the 16c/64GB reference rig (CAPACITY_MAX=12 → 50%).
Operators can override via env to seed the cold-start higher / lower;
:func:`reset` clamps the value to ``[FLOOR_BUDGET, CAPACITY_MAX]`` so
an over-aggressive override on a small host still produces a safe
budget."""

FLOOR_BUDGET: int = int(os.environ.get("OMNISIGHT_AIMD_FLOOR", "2"))
"""H4a ``budget = max(floor=2, budget//2)`` — hard floor so an MD spiral
can never collapse the host to zero concurrency. Even one bursty
sandbox needs to keep flowing through the system or operators lose
visibility into recovery."""

CPU_AI_THRESHOLD_PCT: float = float(
    os.environ.get("OMNISIGHT_AIMD_CPU_AI", "70.0")
)
MEM_AI_THRESHOLD_PCT: float = float(
    os.environ.get("OMNISIGHT_AIMD_MEM_AI", "70.0")
)
"""H4a additive-increase trigger: ``cpu < 70`` AND ``mem < 70``
AND ``deferred == 0``. Strict ``<`` — exactly 70.0 is *not* green
enough to grow."""

CPU_MD_THRESHOLD_PCT: float = float(
    os.environ.get("OMNISIGHT_AIMD_CPU_MD", "85.0")
)
MEM_MD_THRESHOLD_PCT: float = float(
    os.environ.get("OMNISIGHT_AIMD_MEM_MD", "85.0")
)
"""H4a multiplicative-decrease trigger: ``cpu > 85`` OR ``mem > 85``.
Strict ``>`` — exactly 85.0 is the boundary, not yet hot."""

AI_INTERVAL_S: float = float(os.environ.get("OMNISIGHT_AIMD_AI_S", "30.0"))
"""H4a 'every 30s if green → budget += 1' — AI cooldown."""

MD_PERSISTENCE_S: float = float(os.environ.get("OMNISIGHT_AIMD_MD_S", "10.0"))
"""H4a 'sustained 10s → halve' — MD persistence requirement so a single
noisy 5s tick can't shrink the budget."""

TRACE_WINDOW_S: float = 300.0
"""5-minute rolling history window for the UI (TODO H4a row 2583).
Bounds the trace deque so a long-running process doesn't accumulate
unbounded entries."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AdjustReason(str, Enum):
    """One-cycle outcome label — drives trace + future Prom counter."""

    INIT = "init"
    AI = "additive_increase"
    MD = "multiplicative_decrease"
    HOLD = "hold"
    CAP = "hard_cap"
    FLOOR = "floor"


@dataclass(frozen=True)
class BudgetTraceEntry:
    """One trace record — what the budget changed to and why.

    The UI renders the deque chronologically as a "budget over time"
    sparkline; ``reason`` colours the dot (red for MD, green for AI,
    grey for HOLD/CAP/FLOOR).
    """

    timestamp: float
    budget: int
    reason: AdjustReason
    cpu_percent: float
    mem_percent: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Module state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _State:
    budget: int = 0
    last_ai_at: float = 0.0
    pressure_first_seen: float | None = None
    last_reason: AdjustReason = AdjustReason.INIT
    trace: deque[BudgetTraceEntry] = field(default_factory=deque)


_lock = threading.Lock()
_state = _State()


def _now() -> float:
    return time.time()


def _trim_trace_locked(now: float) -> None:
    cutoff = now - TRACE_WINDOW_S
    while _state.trace and _state.trace[0].timestamp < cutoff:
        _state.trace.popleft()


def _append_trace_locked(
    now: float,
    reason: AdjustReason,
    cpu: float,
    mem: float,
) -> None:
    _state.trace.append(
        BudgetTraceEntry(
            timestamp=now,
            budget=_state.budget,
            reason=reason,
            cpu_percent=cpu,
            mem_percent=mem,
        )
    )
    _trim_trace_locked(now)


def _clamp(value: int) -> int:
    """Apply the hard ``[FLOOR_BUDGET, CAPACITY_MAX]`` envelope."""
    return max(FLOOR_BUDGET, min(CAPACITY_MAX, value))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def reset(initial_budget: int | None = None, *, now: float | None = None) -> None:
    """Reset the controller to its cold-start state.

    Called once at module import (warm boot of the process), and again
    by :func:`_reset_for_tests`. Future H4a row 2582 (last-known-good
    budget persisted to DB) will pass the recovered value as
    ``initial_budget`` so a restart doesn't lose calibration.
    """
    with _lock:
        seed = initial_budget if initial_budget is not None else INIT_BUDGET
        t = now if now is not None else _now()
        _state.budget = _clamp(seed)
        _state.last_ai_at = t
        _state.pressure_first_seen = None
        _state.last_reason = AdjustReason.INIT
        _state.trace.clear()
        _append_trace_locked(t, AdjustReason.INIT, 0.0, 0.0)


def current_budget() -> int:
    """Current adaptive budget in tokens (always within
    ``[FLOOR_BUDGET, CAPACITY_MAX]``)."""
    with _lock:
        return _state.budget


def tick(
    cpu_percent: float,
    mem_percent: float,
    deferred_count: int,
    *,
    now: float | None = None,
) -> AdjustReason:
    """Advance the AIMD state machine by one control cycle.

    Designed to be called every host-metrics tick (5s under H1's
    ``SAMPLE_INTERVAL_S``). Pure with respect to host signals — caller
    sources ``cpu_percent`` / ``mem_percent`` from
    :mod:`backend.host_metrics` and ``deferred_count`` from
    :func:`backend.sandbox_capacity.deferred_count_recent`.

    Returns the cycle's :class:`AdjustReason`.
    """
    t = now if now is not None else _now()
    hot = (
        cpu_percent > CPU_MD_THRESHOLD_PCT
        or mem_percent > MEM_MD_THRESHOLD_PCT
    )
    cool = (
        cpu_percent < CPU_AI_THRESHOLD_PCT
        and mem_percent < MEM_AI_THRESHOLD_PCT
    )

    with _lock:
        if hot:
            # MD path — start / advance the persistence clock.
            if _state.pressure_first_seen is None:
                _state.pressure_first_seen = t
                _state.last_reason = AdjustReason.HOLD
                return AdjustReason.HOLD

            if t - _state.pressure_first_seen >= MD_PERSISTENCE_S:
                old = _state.budget
                halved = max(FLOOR_BUDGET, _state.budget // 2)
                if halved < old:
                    _state.budget = halved
                    reason = AdjustReason.MD
                else:
                    reason = AdjustReason.FLOOR
                # Reset both clocks so the next halving requires a
                # fresh full ``MD_PERSISTENCE_S`` of pressure (classic
                # exponential-backoff AIMD), and AI doesn't fire
                # immediately after recovery.
                _state.pressure_first_seen = t
                _state.last_ai_at = t
                _state.last_reason = reason
                _append_trace_locked(t, reason, cpu_percent, mem_percent)
                return reason

            _state.last_reason = AdjustReason.HOLD
            return AdjustReason.HOLD

        # Not hot — clear the MD persistence clock so a brief spike
        # doesn't accumulate across an otherwise calm window.
        _state.pressure_first_seen = None

        if cool and deferred_count == 0 and t - _state.last_ai_at >= AI_INTERVAL_S:
            _state.last_ai_at = t
            if _state.budget >= CAPACITY_MAX:
                _state.last_reason = AdjustReason.CAP
                _append_trace_locked(t, AdjustReason.CAP, cpu_percent, mem_percent)
                return AdjustReason.CAP
            _state.budget = min(CAPACITY_MAX, _state.budget + 1)
            _state.last_reason = AdjustReason.AI
            _append_trace_locked(t, AdjustReason.AI, cpu_percent, mem_percent)
            return AdjustReason.AI

        _state.last_reason = AdjustReason.HOLD
        return AdjustReason.HOLD


def trace(*, now: float | None = None) -> list[BudgetTraceEntry]:
    """Return the (trimmed) trace, oldest first.

    ``now`` overrides the trim cutoff for tests; production callers
    omit it and trim is taken from the wall clock.
    """
    t = now if now is not None else _now()
    with _lock:
        _trim_trace_locked(t)
        return list(_state.trace)


def snapshot(*, now: float | None = None) -> dict:
    """JSON-serialisable view for ``GET /api/v1/ops/summary`` + SSE."""
    t = now if now is not None else _now()
    with _lock:
        _trim_trace_locked(t)
        return {
            "budget": _state.budget,
            "capacity_max": CAPACITY_MAX,
            "floor": FLOOR_BUDGET,
            "init_budget": INIT_BUDGET,
            "last_reason": _state.last_reason.value,
            "last_ai_at": _state.last_ai_at,
            "pressure_clock_started_at": _state.pressure_first_seen,
            "thresholds": {
                "cpu_ai_pct": CPU_AI_THRESHOLD_PCT,
                "mem_ai_pct": MEM_AI_THRESHOLD_PCT,
                "cpu_md_pct": CPU_MD_THRESHOLD_PCT,
                "mem_md_pct": MEM_MD_THRESHOLD_PCT,
                "ai_interval_s": AI_INTERVAL_S,
                "md_persistence_s": MD_PERSISTENCE_S,
            },
            "trace": [
                {
                    "timestamp": e.timestamp,
                    "budget": e.budget,
                    "reason": e.reason.value,
                    "cpu_percent": e.cpu_percent,
                    "mem_percent": e.mem_percent,
                }
                for e in _state.trace
            ],
        }


def evaluate_from_host_snapshot(snap=None, *, now: float | None = None) -> AdjustReason:
    """Convenience wrapper for the host sampling loop.

    Pulls CPU/mem from the freshest :class:`HostSnapshot` and
    ``deferred_count`` from
    :func:`backend.sandbox_capacity.deferred_count_recent`. Returns
    :data:`AdjustReason.HOLD` and logs at debug level if no snapshot
    has landed yet (cold-start grace).
    """
    if snap is None:
        try:
            from backend import host_metrics
        except Exception as exc:
            logger.debug("adaptive_budget: host_metrics import failed: %s", exc)
            return AdjustReason.HOLD
        snap = host_metrics.get_latest_host_snapshot()
    if snap is None:
        return AdjustReason.HOLD

    try:
        from backend import sandbox_capacity
        deferred = sandbox_capacity.deferred_count_recent()
    except Exception as exc:
        logger.debug("adaptive_budget: deferred-count read failed: %s", exc)
        deferred = 0

    return tick(
        cpu_percent=snap.host.cpu_percent,
        mem_percent=snap.host.mem_percent,
        deferred_count=deferred,
        now=now,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reset_for_tests() -> None:
    """Restore module state to cold-start defaults. Not for production."""
    reset()


# Cold-start prime so callers that ``import current_budget()`` before
# any tick still see the seed value rather than the dataclass's 0.
reset()

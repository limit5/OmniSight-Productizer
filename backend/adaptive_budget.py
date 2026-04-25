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
#  Mode multiplier (TODO H4a row 2580)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MODE_MULTIPLIER: dict[str, float] = {
    "turbo": 1.0,
    "full_auto": 0.7,
    "supervised": 0.4,
    "manual": 0.15,
}
"""H4a row 2580 — per-mode fraction of ``CAPACITY_MAX`` allowed.

Composes with the AIMD-shaped budget: the effective per-cycle admission
ceiling is ``min(floor(MODE_MULTIPLIER[mode] * CAPACITY_MAX),
current_budget())``. The intuition:

* **turbo (1.0)** — power-user opt-in to use the full host envelope; the
  AIMD controller is the only governor.
* **full_auto (0.7)** — keep ~30% of host headroom unspent so an
  unattended session leaves room for ad-hoc operator tasks.
* **supervised (0.4)** — visible session, half-throttle so a runaway
  decision can't eat the box before the human notices.
* **manual (0.15)** — every action is human-approved; concurrency is
  almost always 1–2 anyway, so the cap is symbolic but prevents
  pathological queue depth in case of a misconfigured tenant.

Keyed by string (matches :class:`backend.decision_engine.OperationMode`'s
``str, Enum`` value) so this module doesn't import ``decision_engine``
— preserves the ``adaptive_budget`` → ``sandbox_capacity`` direction
(downstream consumers wire upward, not the reverse). ``OperationMode``
members can still be passed directly because their ``.value`` matches
the dict key.
"""


def mode_multiplier(mode: str) -> float:
    """Return the per-mode capacity fraction for *mode*.

    Falls back to the ``supervised`` multiplier (0.4) for unknown modes
    — defensive default that keeps a typo'd / future-mode session
    bounded rather than silently granting full capacity.
    """
    key = mode.value if hasattr(mode, "value") else str(mode)
    return MODE_MULTIPLIER.get(key, MODE_MULTIPLIER["supervised"])


def effective_budget(
    mode: str,
    *,
    aimd_budget: int | None = None,
) -> int:
    """Compose the per-mode ceiling with the AIMD-shaped budget.

    ``effective = min(floor(MODE_MULTIPLIER[mode] * CAPACITY_MAX),
    aimd_budget)``.

    * ``aimd_budget`` defaults to :func:`current_budget` so callers in
      the production path don't need to pass it; tests pass it
      explicitly to drive the composition deterministically.
    * Floored at ``1`` so the most-throttled mode (``manual`` × small
      ``CAPACITY_MAX``) still grants at least one slot — matches the
      same anti-deadlock floor enforced by
      :func:`backend.sandbox_capacity._effective_capacity_max_locked`.

    H4a row 2581 wires this into ``_ModeSlot.acquire(cost)`` so the
    token-based admission path consults the composed ceiling on every
    fresh acquire.
    """
    mode_cap = max(1, int(mode_multiplier(mode) * CAPACITY_MAX))
    aimd = aimd_budget if aimd_budget is not None else current_budget()
    return max(1, min(mode_cap, aimd))


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
    #: Set True whenever ``tick()`` actually mutates ``budget`` (AI with
    #: room / MD that shrinks). Cleared by
    #: :func:`persist_current_budget_if_dirty` after a successful write.
    #: HOLD / CAP / no-op FLOOR leave this False — we only hit the DB on
    #: real state changes. H4a row 2582.
    dirty: bool = False


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
        # Reset never triggers a DB write — either we just loaded from
        # DB (dirty would overwrite a fresh read with the same value)
        # or we are in a test / cold-start default (no persistence
        # desired for a transient seed). H4a row 2582.
        _state.dirty = False
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
                    _state.dirty = True  # persist the new last-known-good
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
            _state.dirty = True  # persist the new last-known-good
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
#  Last-known-good persistence (TODO H4a row 2582)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def load_last_known_good() -> int | None:
    """Return the last-persisted budget, or None if unavailable.

    Best-effort read from the ``adaptive_budget_state`` singleton row
    (see alembic 0030 + :func:`backend.db.load_adaptive_budget_state`).
    Swallows any DB error (pool not up, table empty on first boot,
    SQLite dev mode with no pool) and returns None — the caller
    falls back to the static ``INIT_BUDGET`` default.
    """
    try:
        from backend.db_pool import get_pool
        from backend import db
        async with get_pool().acquire() as conn:
            row = await db.load_adaptive_budget_state(conn)
    except Exception as exc:
        logger.debug("adaptive_budget: load_last_known_good failed: %s", exc)
        return None
    if row is None:
        return None
    # Always re-clamp through the live envelope — the row may be
    # from a different-sized host (operator moved the database) or
    # from before the operator tightened ``CAPACITY_MAX``. Out-of-
    # envelope seeds are clamped, never rejected.
    return _clamp(int(row["budget"]))


async def prime_from_db() -> int | None:
    """Bootstrap the controller from the persisted last-known-good.

    Called once at lifespan startup (see ``backend/main.py`` after
    ``db_pool.init_pool`` opens the pool). On success, returns the
    loaded budget (which has also been applied via :func:`reset`);
    on any failure returns None and leaves the existing cold-start
    default in place.

    Idempotent: safe to call twice. The second call either reloads
    the same row or hits the same DB error and returns None. Tests
    call :func:`_reset_for_tests` to reset between cases.
    """
    loaded = await load_last_known_good()
    if loaded is None:
        return None
    reset(initial_budget=loaded)
    logger.info(
        "adaptive_budget: primed from DB — last-known-good budget=%d "
        "(replaces INIT_BUDGET=%d)",
        loaded,
        INIT_BUDGET,
    )
    return loaded


async def persist_current_budget_if_dirty() -> bool:
    """Best-effort upsert of the current budget if it changed since
    the last persist.

    Returns True on a successful write, False if nothing was dirty
    or the DB write failed (both outcomes are non-fatal — the in-
    memory budget is the source of truth for the live system, the
    DB row is only load-bearing at cold start).

    Called from the host sampling loop after each
    :func:`evaluate_from_host_snapshot` so that AI / MD transitions
    carry over a restart. HOLD / CAP / no-op FLOOR leave the dirty
    flag untouched so we do not hit the DB every 5 s on an idle host.
    """
    # Read + clear the dirty flag under the lock so we never miss or
    # double-write a concurrent ``tick()``. If the DB write fails we
    # leave the flag cleared — the next AI / MD will re-arm it, and
    # writing a stale "still 7" on top of a subsequent "still 7" is
    # a no-op anyway.
    with _lock:
        if not _state.dirty:
            return False
        snapshot_budget = _state.budget
        snapshot_reason = _state.last_reason.value
        _state.dirty = False

    try:
        from backend.db_pool import get_pool
        from backend import db
        async with get_pool().acquire() as conn:
            await db.save_adaptive_budget_state(
                conn,
                budget=snapshot_budget,
                last_reason=snapshot_reason,
                updated_at=_now(),
            )
    except Exception as exc:
        logger.debug(
            "adaptive_budget: persist_current_budget_if_dirty failed: %s",
            exc,
        )
        return False
    return True


async def evaluate_and_persist_from_host_snapshot(
    snap=None, *, now: float | None = None,
) -> AdjustReason:
    """Wire helper: run one control cycle, then persist if it changed.

    Mirrors :func:`evaluate_from_host_snapshot` but adds the
    best-effort DB write. Intended for the production host sampling
    loop; tests keep the cheaper sync ``evaluate_from_host_snapshot``
    + ``tick`` for deterministic timing.
    """
    reason = evaluate_from_host_snapshot(snap=snap, now=now)
    await persist_current_budget_if_dirty()
    return reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reset_for_tests() -> None:
    """Restore module state to cold-start defaults. Not for production."""
    reset()


# Cold-start prime so callers that ``import current_budget()`` before
# any tick still see the seed value rather than the dataclass's 0.
reset()

"""I6 — DRF per-tenant sandbox capacity with weighted token bucket.

Implements Dominant Resource Fairness for sandbox concurrency:
- Global CAPACITY_MAX = 12 tokens (configurable via env)
- Per-tenant guaranteed minimum = CAPACITY_MAX / active_tenant_count
- Idle capacity borrowing: tenants can use unused tokens from others
- Grace period: borrowed tokens must be released within 30s when the
  owner tenant needs them back
- Per-tenant turbo cap prevents single-tenant monopoly

Builds on H4a design (weighted token bucket) but scoped per-tenant
from day one. The `_ModeSlot` in decision_engine.py delegates to
this module for token-cost-based acquire/release.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _detect_cpu_cores() -> int:
    """Logical CPU cores visible to this process (defaults to 1 if unknown)."""
    return os.cpu_count() or 1


def _detect_mem_gb() -> float:
    """Total system memory in GiB (0.0 if undetectable)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) / (1024 ** 3)
    except (OSError, ValueError):
        return 0.0


def _compute_capacity_max(
    cpu_cores: int | None = None,
    mem_gb: float | None = None,
) -> int:
    """H4a derived global token budget.

    ``CAPACITY_MAX = floor(min(cpu_cores * 0.8, mem_gb / 2))``

    Reference rig 16c / 64 GiB → ``min(12.8, 32) = 12`` tokens. The 0.8
    CPU factor leaves ~20% headroom for the host (kernel, frontend,
    coordinator); the ``mem_gb / 2`` factor assumes the heaviest single
    sandbox class peaks at ~512 MiB per token (matches
    :class:`SandboxCostWeight` lightweight envelope and worst-case
    compile bursts).

    Falls back to a floor of 1 so even a tiny dev VM (1c/1GB) can run
    one sandbox; the env var ``OMNISIGHT_CAPACITY_MAX`` overrides the
    formula entirely (operator opt-out for known-tuned hosts).
    """
    cores = cpu_cores if cpu_cores is not None else _detect_cpu_cores()
    mem = mem_gb if mem_gb is not None else _detect_mem_gb()
    cpu_budget = cores * 0.8
    mem_budget = mem / 2.0
    return max(1, int(min(cpu_budget, mem_budget)))


_env_capacity = os.environ.get("OMNISIGHT_CAPACITY_MAX")
CAPACITY_MAX: int = (
    int(_env_capacity) if _env_capacity else _compute_capacity_max()
)

GRACE_PERIOD_S: float = float(os.environ.get("OMNISIGHT_DRF_GRACE_S", "30.0"))

TURBO_TENANT_CAP_RATIO: float = float(
    os.environ.get("OMNISIGHT_TURBO_TENANT_CAP_RATIO", "0.75")
)


class SandboxCostWeight(float, Enum):
    """H4a initial cost weights (DRF tokens) per sandbox class.

    1 token ≈ 1 CPU core × 512 MiB RAM (see ``backend/container.py`` M1
    mapping). The values here are the first-cut estimates agreed in the
    H4a design; H4b will replace them with `configs/sandbox_cost_weights.yaml`
    derived from real sandbox telemetry (see ``scripts/calibrate_sandbox_cost.py``).

    Per-class resource envelopes — kept in sync with ``COST_WEIGHT_ESTIMATES``
    below so downstream code can look up memory/core hints by enum member.
    """

    gvisor_lightweight = 1.0
    docker_t2_networked = 2.0
    phase64c_local_compile = 4.0
    phase64c_qemu_aarch64 = 3.0
    phase64c_ssh_remote = 0.5


@dataclass(frozen=True)
class CostEstimate:
    """Initial-estimate metadata for a :class:`SandboxCostWeight` member.

    * ``tokens`` — DRF tokens (matches the enum's float value).
    * ``memory_mb`` — expected peak RSS in MiB (used by ``container.py``
      to derive ``--memory`` when the caller passes a weight).
    * ``cpu_cores`` — expected CPU envelope in cores.
    * ``burst`` — ``True`` for short bursty workloads (unit tests, lint),
      ``False`` for sustained workloads (compile, QEMU). Sustained
      workloads are what the AIMD controller throttles first on host
      pressure because they dominate CPU×time.
    * ``use_case`` — one-line human summary for UI tooltips / audit.
    """

    tokens: float
    memory_mb: int
    cpu_cores: float
    burst: bool
    use_case: str


# H4a initial estimates — 1 token ≈ 1 core × 512 MiB. Values mirror the
# TODO.md H4a row for SandboxCostWeight. Keep the two tables consistent:
# any change here must also update the enum member's float value (and
# vice versa); the ``test_weight_metadata_matches_enum_values`` guard
# test fails loudly if they drift.
COST_WEIGHT_ESTIMATES: dict[SandboxCostWeight, CostEstimate] = {
    SandboxCostWeight.gvisor_lightweight: CostEstimate(
        tokens=1.0,
        memory_mb=512,
        cpu_cores=1.0,
        burst=True,
        use_case="unit test / lint",
    ),
    SandboxCostWeight.docker_t2_networked: CostEstimate(
        tokens=2.0,
        memory_mb=1536,  # ~1.5 GiB
        cpu_cores=2.0,
        burst=False,
        use_case="integration test with network",
    ),
    SandboxCostWeight.phase64c_local_compile: CostEstimate(
        tokens=4.0,
        memory_mb=2048,  # ~2 GiB
        cpu_cores=4.0,
        burst=False,
        use_case="make -j4 local compile (sustained)",
    ),
    SandboxCostWeight.phase64c_qemu_aarch64: CostEstimate(
        tokens=3.0,
        memory_mb=2048,  # ~2 GiB
        cpu_cores=2.0,
        burst=False,
        use_case="aarch64 cross-compile under qemu",
    ),
    SandboxCostWeight.phase64c_ssh_remote: CostEstimate(
        tokens=0.5,
        memory_mb=256,
        cpu_cores=0.5,
        burst=True,
        use_case="ssh remote (compute on far side, local is just client)",
    ),
}


DEFAULT_COST = SandboxCostWeight.gvisor_lightweight


def cost_estimate(weight: SandboxCostWeight) -> CostEstimate:
    """Return the :class:`CostEstimate` metadata for *weight*.

    Convenience accessor so callers don't need to import the dict.
    Raises ``KeyError`` if a new enum member is added without a matching
    ``COST_WEIGHT_ESTIMATES`` row — caught by the drift-guard test.
    """
    return COST_WEIGHT_ESTIMATES[weight]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-tenant state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _TenantBucket:
    tenant_id: str
    used: float = 0.0
    guaranteed: float = 0.0
    borrowed: float = 0.0
    last_active: float = field(default_factory=time.time)
    grants: list[_Grant] = field(default_factory=list)


@dataclass
class _Grant:
    cost: float
    acquired_at: float
    is_borrowed: bool = False
    grace_deadline: float | None = None


_lock = threading.Lock()
_buckets: dict[str, _TenantBucket] = {}
_async_cond: asyncio.Condition | None = None

_DEFAULT_TENANT = "t-default"

# H3 row 1524: Coordinator transparency — queue depth, deferred-5m,
# effective budget (derate). `_waiters` counts tasks currently blocked
# in `acquire()` waiting for a free slot; `_deferred_events` is a
# rolling timestamp deque for tasks that had to wait for *any* slot
# in the last DEFERRED_WINDOW_S; `_derate_ratio` shrinks the effective
# budget when the coordinator decides the host is under pressure.
DEFERRED_WINDOW_S: float = 300.0  # 5-minute rolling window
_waiters: int = 0
_deferred_events: deque[float] = deque()
_derate_ratio: float = 1.0
_derate_reason: str | None = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ensure_bucket(tid: str) -> _TenantBucket:
    if tid not in _buckets:
        _buckets[tid] = _TenantBucket(tenant_id=tid)
    return _buckets[tid]


def _active_tenants() -> list[_TenantBucket]:
    return [b for b in _buckets.values() if b.used > 0 or b.grants]


def _active_tenant_count() -> int:
    active = _active_tenants()
    return max(1, len(active)) if active else max(1, len(_buckets))


def _recalc_guarantees() -> None:
    count = max(1, len(_buckets)) if _buckets else 1
    per_tenant = CAPACITY_MAX / count
    for b in _buckets.values():
        b.guaranteed = per_tenant


def _total_used() -> float:
    return sum(b.used for b in _buckets.values())


def _tenant_turbo_cap(tid: str) -> float:
    return CAPACITY_MAX * TURBO_TENANT_CAP_RATIO


def _available_for_tenant(bucket: _TenantBucket) -> float:
    total_used = _total_used()
    global_free = CAPACITY_MAX - total_used
    own_free = bucket.guaranteed - bucket.used
    if own_free >= 0:
        return own_free + max(0, global_free - own_free)
    return max(0, global_free)


def _has_grace_expired_grants(bucket: _TenantBucket, now: float) -> list[_Grant]:
    return [
        g for g in bucket.grants
        if g.is_borrowed and g.grace_deadline is not None and g.grace_deadline <= now
    ]


def _effective_capacity_max_locked() -> float:
    """Effective concurrency budget after derate — must be called with _lock held."""
    # Floor at 1.0 so a full derate can't produce a zero-capacity deadlock.
    return max(1.0, CAPACITY_MAX * _derate_ratio)


def _trim_deferred_events_locked(now: float) -> None:
    cutoff = now - DEFERRED_WINDOW_S
    while _deferred_events and _deferred_events[0] < cutoff:
        _deferred_events.popleft()


def _record_deferral() -> None:
    now = time.time()
    with _lock:
        _deferred_events.append(now)
        _trim_deferred_events_locked(now)


def _waiters_inc() -> None:
    global _waiters
    with _lock:
        _waiters += 1


def _waiters_dec() -> None:
    global _waiters
    with _lock:
        _waiters = max(0, _waiters - 1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def try_acquire(
    tenant_id: str | None = None,
    cost: float = 1.0,
    is_turbo: bool = False,
) -> bool:
    """Try to acquire *cost* tokens for *tenant_id*. Non-blocking.

    Returns True if acquired, False if capacity not available.
    When *is_turbo* is True, the per-tenant turbo cap is enforced.
    """
    tid = tenant_id or _DEFAULT_TENANT
    now = time.time()

    with _lock:
        bucket = _ensure_bucket(tid)
        _recalc_guarantees()

        if is_turbo and bucket.used + cost > _tenant_turbo_cap(tid):
            return False

        total_used = _total_used()
        if total_used + cost > _effective_capacity_max_locked():
            return False

        is_borrowed = bucket.used + cost > bucket.guaranteed
        bucket.used += cost
        bucket.last_active = now
        bucket.grants.append(_Grant(
            cost=cost,
            acquired_at=now,
            is_borrowed=is_borrowed,
        ))
        return True


async def acquire(
    tenant_id: str | None = None,
    cost: float = 1.0,
    is_turbo: bool = False,
    timeout_s: float | None = None,
) -> bool:
    """Async acquire — blocks until capacity is available or timeout.

    Returns True if acquired, False on timeout.
    """
    global _async_cond
    if _async_cond is None:
        _async_cond = asyncio.Condition()

    # Fast path: slot available immediately → no deferral, no queue bump.
    if try_acquire(tenant_id, cost, is_turbo):
        return True

    # Slow path: record a deferral (5-min rolling counter) and bump the
    # queue-depth gauge for the duration of the wait so the ops panel
    # can surface "N tasks are actually stuck waiting for a slot".
    _record_deferral()
    _waiters_inc()

    deadline = (time.time() + timeout_s) if timeout_s else None

    try:
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False

            async with _async_cond:
                try:
                    await asyncio.wait_for(
                        _async_cond.wait(),
                        timeout=min(remaining, 1.0) if remaining else 1.0,
                    )
                except asyncio.TimeoutError:
                    pass

            if try_acquire(tenant_id, cost, is_turbo):
                return True

            if deadline is not None and time.time() >= deadline:
                return False
    finally:
        _waiters_dec()


def release(tenant_id: str | None = None, cost: float = 1.0) -> None:
    """Release *cost* tokens back to the pool for *tenant_id*."""
    tid = tenant_id or _DEFAULT_TENANT

    with _lock:
        bucket = _buckets.get(tid)
        if bucket is None:
            return

        remaining = cost
        to_remove = []
        for i, g in enumerate(bucket.grants):
            if remaining <= 0:
                break
            take = min(g.cost, remaining)
            g.cost -= take
            remaining -= take
            if g.cost <= 0:
                to_remove.append(i)

        for i in reversed(to_remove):
            bucket.grants.pop(i)

        bucket.used = max(0, bucket.used - cost)
        if bucket.borrowed > 0:
            bucket.borrowed = max(0, bucket.borrowed - cost)

    _notify_waiters()


def reclaim_borrowed(requesting_tid: str) -> list[tuple[str, float]]:
    """Trigger grace period on tenants using more than their guaranteed share.

    Called when a tenant needs capacity but others hold tokens beyond
    their current guaranteed minimum. Sets grace deadlines on excess
    grants (newest first). Returns list of (tenant_id, cost) that will
    free up after the grace period expires.
    """
    now = time.time()
    reclaims: list[tuple[str, float]] = []

    with _lock:
        _recalc_guarantees()
        requesting = _ensure_bucket(requesting_tid)

        if requesting.used >= requesting.guaranteed:
            return []

        needed = requesting.guaranteed - requesting.used
        for b in _buckets.values():
            if b.tenant_id == requesting_tid:
                continue
            if b.used <= b.guaranteed:
                continue

            over = b.used - b.guaranteed
            reclaimable = min(over, needed)

            for g in reversed(b.grants):
                if reclaimable <= 0:
                    break
                if g.grace_deadline is not None:
                    continue
                g.is_borrowed = True
                g.grace_deadline = now + GRACE_PERIOD_S
                take = min(g.cost, reclaimable)
                reclaimable -= take
                reclaims.append((b.tenant_id, take))
                needed -= take

    _try_emit_reclaim_event(requesting_tid, reclaims)
    return reclaims


async def acquire_with_reclaim(
    tenant_id: str | None = None,
    cost: float = 1.0,
    is_turbo: bool = False,
    timeout_s: float | None = None,
) -> bool:
    """Acquire with automatic reclaim of borrowed capacity.

    If direct acquire fails, triggers reclaim on borrowers and waits
    up to GRACE_PERIOD_S + timeout_s for capacity to free up.
    """
    tid = tenant_id or _DEFAULT_TENANT

    if try_acquire(tid, cost, is_turbo):
        return True

    reclaim_borrowed(tid)

    effective_timeout = (timeout_s or 0) + GRACE_PERIOD_S
    return await acquire(tid, cost, is_turbo, timeout_s=effective_timeout)


def enforce_grace_deadlines() -> list[tuple[str, float]]:
    """Force-release grants whose grace deadline has expired.

    Called periodically by a sweep loop. Returns list of
    (tenant_id, released_cost) for logging/audit.
    """
    now = time.time()
    released: list[tuple[str, float]] = []

    with _lock:
        for b in list(_buckets.values()):
            expired = _has_grace_expired_grants(b, now)
            for g in expired:
                b.used = max(0, b.used - g.cost)
                b.borrowed = max(0, b.borrowed - g.cost)
                released.append((b.tenant_id, g.cost))
                b.grants.remove(g)

    if released:
        _notify_waiters()
        _try_emit_grace_enforced(released)

    return released


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Observability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def snapshot() -> dict[str, Any]:
    """Return current capacity state for API / SSE telemetry."""
    with _lock:
        _recalc_guarantees()
        now = time.time()
        _trim_deferred_events_locked(now)
        tenants = {}
        for b in _buckets.values():
            tenants[b.tenant_id] = {
                "used": b.used,
                "guaranteed": b.guaranteed,
                "borrowed": b.borrowed,
                "grant_count": len(b.grants),
                "turbo_cap": _tenant_turbo_cap(b.tenant_id),
            }
        effective = _effective_capacity_max_locked()
        return {
            "capacity_max": CAPACITY_MAX,
            "effective_capacity_max": effective,
            "derated": _derate_ratio < 1.0,
            "derate_ratio": _derate_ratio,
            "derate_reason": _derate_reason,
            "queue_depth": _waiters,
            "deferred_5m": len(_deferred_events),
            "total_used": _total_used(),
            "total_free": effective - _total_used(),
            "active_tenants": len([b for b in _buckets.values() if b.used > 0]),
            "registered_tenants": len(_buckets),
            "tenants": tenants,
        }


def queue_depth() -> int:
    """Number of tasks currently blocked in `acquire()` waiting for a slot."""
    with _lock:
        return _waiters


def deferred_count_recent() -> int:
    """Deferred-task count in the last DEFERRED_WINDOW_S (5 min)."""
    with _lock:
        _trim_deferred_events_locked(time.time())
        return len(_deferred_events)


def effective_capacity_max() -> float:
    """Effective budget tokens after derate — may be < CAPACITY_MAX."""
    with _lock:
        return _effective_capacity_max_locked()


def set_derate(ratio: float, reason: str | None = None) -> None:
    """Set the derate multiplier (0 < ratio <= 1).

    Called by the Coordinator when host pressure crosses a threshold to
    shrink the effective budget below CAPACITY_MAX. Wakes any waiters
    so they re-check capacity against the new ceiling.
    """
    global _derate_ratio, _derate_reason
    ratio = max(0.0, min(1.0, float(ratio)))
    with _lock:
        _derate_ratio = ratio
        _derate_reason = reason if ratio < 1.0 else None
    _notify_waiters()


def tenant_usage(tenant_id: str) -> dict[str, Any]:
    """Return usage for a specific tenant."""
    with _lock:
        _recalc_guarantees()
        b = _buckets.get(tenant_id or _DEFAULT_TENANT)
        if b is None:
            return {"used": 0, "guaranteed": 0, "borrowed": 0, "grant_count": 0}
        return {
            "used": b.used,
            "guaranteed": b.guaranteed,
            "borrowed": b.borrowed,
            "grant_count": len(b.grants),
            "turbo_cap": _tenant_turbo_cap(b.tenant_id),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SWEEP_INTERVAL_S: float = float(
    os.environ.get("OMNISIGHT_DRF_SWEEP_S", "5.0")
)


async def run_sweep_loop(interval_s: float = SWEEP_INTERVAL_S) -> None:
    """Background task: enforce grace deadlines periodically."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            released = enforce_grace_deadlines()
            if released:
                logger.info(
                    "DRF grace sweep: released %d grants from %s",
                    len(released),
                    [r[0] for r in released],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("DRF sweep error: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SSE helpers (best-effort)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _notify_waiters() -> None:
    global _async_cond
    if _async_cond is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_do_notify())
        except RuntimeError:
            pass


async def _do_notify() -> None:
    global _async_cond
    if _async_cond is not None:
        async with _async_cond:
            _async_cond.notify_all()


def _try_emit_reclaim_event(
    requesting_tid: str,
    reclaims: list[tuple[str, float]],
) -> None:
    if not reclaims:
        return
    try:
        from backend.events import bus
        bus.publish("sandbox_capacity_reclaim", {
            "requesting_tenant": requesting_tid,
            "reclaims": [
                {"tenant_id": tid, "cost": cost} for tid, cost in reclaims
            ],
            "grace_period_s": GRACE_PERIOD_S,
        })
    except Exception as exc:
        logger.debug("reclaim event publish failed: %s", exc)


def _try_emit_grace_enforced(
    released: list[tuple[str, float]],
) -> None:
    try:
        from backend.events import bus
        bus.publish("sandbox_capacity_grace_enforced", {
            "released": [
                {"tenant_id": tid, "cost": cost} for tid, cost in released
            ],
        })
    except Exception as exc:
        logger.debug("grace_enforced event publish failed: %s", exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test / reset
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reset_for_tests() -> None:
    """Clear all state. Not for production use."""
    global _async_cond, _waiters, _derate_ratio, _derate_reason
    with _lock:
        _buckets.clear()
        _deferred_events.clear()
        _waiters = 0
        _derate_ratio = 1.0
        _derate_reason = None
    _async_cond = None

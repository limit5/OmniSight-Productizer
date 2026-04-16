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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CAPACITY_MAX: int = int(os.environ.get("OMNISIGHT_CAPACITY_MAX", "12"))

GRACE_PERIOD_S: float = float(os.environ.get("OMNISIGHT_DRF_GRACE_S", "30.0"))

TURBO_TENANT_CAP_RATIO: float = float(
    os.environ.get("OMNISIGHT_TURBO_TENANT_CAP_RATIO", "0.75")
)


class SandboxCostWeight(float, Enum):
    gvisor_lightweight = 1.0
    docker_t2_networked = 2.0
    phase64c_local_compile = 4.0
    phase64c_qemu_aarch64 = 3.0
    phase64c_ssh_remote = 0.5


DEFAULT_COST = SandboxCostWeight.gvisor_lightweight


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
        if total_used + cost > CAPACITY_MAX:
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

    deadline = (time.time() + timeout_s) if timeout_s else None

    while True:
        if try_acquire(tenant_id, cost, is_turbo):
            return True

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

        if deadline is not None and time.time() >= deadline:
            if try_acquire(tenant_id, cost, is_turbo):
                return True
            return False


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
        tenants = {}
        for b in _buckets.values():
            tenants[b.tenant_id] = {
                "used": b.used,
                "guaranteed": b.guaranteed,
                "borrowed": b.borrowed,
                "grant_count": len(b.grants),
                "turbo_cap": _tenant_turbo_cap(b.tenant_id),
            }
        return {
            "capacity_max": CAPACITY_MAX,
            "total_used": _total_used(),
            "total_free": CAPACITY_MAX - _total_used(),
            "active_tenants": len([b for b in _buckets.values() if b.used > 0]),
            "registered_tenants": len(_buckets),
            "tenants": tenants,
        }


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
    global _async_cond
    with _lock:
        _buckets.clear()
    _async_cond = None

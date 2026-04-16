"""Phase 67-C S1 — speculative container pre-warm for DAG dispatch.
M5 — multi-tenant safety: per-tenant bucketing + forced /tmp cleanup.

Engine 3 from `lossless-agent-acceleration.md`: while the orchestrator
is thinking about the next turn, we can already be pulling the
Tier-1 docker image and starting a container for whichever
in-degree-0 task is queued up next. On consume, the caller hands off
the pre-warmed `ContainerInfo` instead of waiting 1-3s for
`start_container` in the hot path.

Hard invariants (enforced by tests):

  * Never bypass Phase 64-A image trust. We literally call
    `container.start_container` — the trust check happens inside it.
  * Never exceed Phase 64-D lifetime cap. Pre-warmed containers are
    registered in the same `_containers` dict, so the existing
    `_lifetime_killswitch` watchdog covers them for free.
  * In-degree ≠ 0 tasks are NEVER pre-warmed. A task depending on an
    upstream output has nothing useful a pre-warmed container can
    do yet.
  * Only `depth` most-ready tasks are pre-warmed. The design locked
    this at 2: more is pre-warmed waste, 1 is insufficient when
    dispatch fans out.
  * Cancel releases immediately. Pre-warmed containers that never
    get consumed (e.g., DAG mutation) are stopped right away to
    free the lifetime budget for the replanned run.
  * M5: A pre-warmed container for tenant A cannot be consumed by
    tenant B when policy="per_tenant". The `/tmp` tenant namespace
    is force-cleared on every consume regardless of policy so that
    no cross-run filesystem residue leaks into the real task.

This module does NOT handle workspace preparation — caller passes a
workspace path when pre-warming, same as `start_container(agent_id,
workspace_path)`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from backend.dag_schema import DAG, Task

if TYPE_CHECKING:
    from backend.container import ContainerInfo

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Policy + depth config
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# M5: virtual tenant id used when policy="shared" so all slots share
# the same bucket. Cannot collide with a real tenant id because
# tenant ids are never prefixed with "_".
_SHARED_BUCKET = "_shared"

_VALID_POLICIES = ("disabled", "shared", "per_tenant")


def _prewarm_depth() -> int:
    """Locked default 2. Env override for ops experimentation."""
    raw = (os.environ.get("OMNISIGHT_PREWARM_DEPTH") or "2").strip()
    try:
        return max(0, min(8, int(raw)))
    except ValueError:
        return 2


def get_policy() -> str:
    """Return the current prewarm policy: disabled | shared | per_tenant.

    Reads from settings each call so test monkeypatching works without
    module reimport. Unknown values fall back to ``per_tenant`` so a
    typo never silently degrades tenant isolation.
    """
    try:
        from backend.config import settings as _settings
        raw = (getattr(_settings, "prewarm_policy", "per_tenant") or "").strip().lower()
    except Exception:
        raw = "per_tenant"
    if raw not in _VALID_POLICIES:
        return "per_tenant"
    return raw


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry (per-tenant bucketed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PrewarmSlot:
    """One pre-warmed container awaiting consumption."""
    task_id: str
    agent_id: str  # synthesised: "prewarm-<task_id>-<short>"
    info: "ContainerInfo"
    tenant_id: str = "t-default"  # M5: which tenant owns this slot


# Module-level registry: { tenant_id: { task_id: slot } }. A consume()
# pops from the matching tenant bucket only; cancel_all() stops every
# container in one or all buckets.
_prewarmed_by_tenant: dict[str, dict[str, PrewarmSlot]] = {}
_lock = asyncio.Lock()


def _reset_for_tests() -> None:
    _prewarmed_by_tenant.clear()


def _bucket_key(tenant_id: Optional[str]) -> str:
    """Resolve the dict key for a given tenant under the active policy.

    - ``disabled``: callers should never reach this; we still normalise
      to a stable key.
    - ``shared``: every call maps to ``_SHARED_BUCKET`` so A and B slots
      co-mingle (legacy behaviour).
    - ``per_tenant``: uses the tenant id itself (or "t-default" if
      context is empty).
    """
    policy = get_policy()
    if policy == "shared":
        return _SHARED_BUCKET
    return (tenant_id or "t-default")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DAG walk — in-degree 0 task picker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def pick_prewarm_candidates(dag: DAG, *, depth: int | None = None) -> list[Task]:
    """Return up to `depth` Tier-1 tasks with in-degree 0, in DAG
    declaration order so the selection is deterministic (stable
    replay during incident review)."""
    n = depth if depth is not None else _prewarm_depth()
    if n <= 0:
        return []
    # Count in-degree.
    indeg: dict[str, int] = {t.task_id: 0 for t in dag.tasks}
    for t in dag.tasks:
        for d in t.depends_on:
            if t.task_id in indeg:  # self-referential guard
                indeg[t.task_id] = indeg.get(t.task_id, 0) + (1 if d in indeg else 0)
    ready = [t for t in dag.tasks if indeg.get(t.task_id, 0) == 0]
    # Only Tier-1 benefits from the Docker pre-warm; networked/t3
    # paths have different start-up cost profiles we haven't modelled.
    ready = [t for t in ready if t.required_tier == "t1"]
    return ready[:n]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  prewarm_for — launch containers speculatively
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def prewarm_for(
    dag: DAG,
    workspace_path: Path,
    *,
    depth: int | None = None,
    starter=None,  # injectable for tests; None → use container.start_container
    tenant_id: Optional[str] = None,  # M5: bucket owner
) -> list[PrewarmSlot]:
    """Launch up to `depth` pre-warmed containers for the in-degree-0
    Tier-1 tasks of `dag`. Runs sequentially so a failure early
    doesn't pre-fire metrics for later starts.

    Failures during start (e.g., image trust rejection, bridge
    unavailable) are LOGGED and SWALLOWED — pre-warm is an
    optimisation, not a correctness requirement. The normal
    dispatch path still works.

    M5: ``tenant_id`` is the owning tenant. Under ``per_tenant`` policy,
    each tenant gets its own bucket (depth applies per bucket). Under
    ``shared``, every call lands in a single ``_shared`` bucket. Under
    ``disabled``, this function short-circuits immediately.
    """
    policy = get_policy()
    if policy == "disabled":
        logger.debug("prewarm: policy=disabled — skipping prewarm_for(%s)", dag.dag_id)
        return []

    if starter is None:
        from backend.container import start_container as starter  # type: ignore

    bucket = _bucket_key(tenant_id)
    effective_tid = tenant_id or "t-default"
    candidates = pick_prewarm_candidates(dag, depth=depth)
    out: list[PrewarmSlot] = []
    # M5: mix the tenant id into the agent_id suffix so two tenants
    # prewarming the same DAG don't collide in container.py's
    # _containers registry (single shared dict keyed by agent_id).
    suffix = _short_id(f"{dag.dag_id}|{effective_tid}")
    for t in candidates:
        agent_id = f"prewarm-{t.task_id}-{suffix}"
        async with _lock:
            tenant_slots = _prewarmed_by_tenant.setdefault(bucket, {})
            if t.task_id in tenant_slots:
                logger.debug(
                    "prewarm: %s already slotted under bucket=%s",
                    t.task_id, bucket,
                )
                out.append(tenant_slots[t.task_id])
                continue
        try:
            # Pass tenant_id through to start_container so cgroup labels
            # + OOM attribution + quota gate all use the right tenant.
            info = await _call_starter(starter, agent_id, workspace_path, effective_tid)
        except Exception as exc:
            logger.warning(
                "prewarm_for: start_container for %s failed: %s", t.task_id, exc,
            )
            try:
                from backend import metrics as _m
                _m.prewarm_consumed_total.labels(result="start_error").inc()
            except Exception as exc:
                logger.debug("prewarm metric bump failed: %s", exc)
            continue
        slot = PrewarmSlot(
            task_id=t.task_id, agent_id=agent_id, info=info,
            tenant_id=effective_tid,
        )
        async with _lock:
            _prewarmed_by_tenant.setdefault(bucket, {})[t.task_id] = slot
        out.append(slot)
        try:
            from backend import metrics as _m
            _m.prewarm_started_total.inc()
        except Exception as exc:
            logger.debug("prewarm metric bump failed: %s", exc)
        logger.info(
            "prewarm: started container for %s (agent_id=%s, tenant=%s, policy=%s)",
            t.task_id, agent_id, effective_tid, policy,
        )
    return out


async def _call_starter(starter, agent_id: str, workspace_path: Path, tenant_id: str):
    """Invoke the injected starter — pass ``tenant_id`` kw if it accepts
    one (production ``start_container``), else fall back to the 2-arg
    call (test starters).
    """
    import inspect
    try:
        sig = inspect.signature(starter)
        if "tenant_id" in sig.parameters:
            return await starter(agent_id, workspace_path, tenant_id=tenant_id)
    except (TypeError, ValueError):
        pass
    return await starter(agent_id, workspace_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  consume — hand off a pre-warmed container to the real task
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def consume(
    task_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[PrewarmSlot]:
    """Pop and return the pre-warmed slot for `task_id`, or None on
    miss. Consumers MUST call this instead of inspecting the registry
    directly — we need the lock + the metric bump.

    M5: the returned slot is guaranteed to belong to ``tenant_id``
    under ``per_tenant`` policy. Under ``shared``, all callers draw
    from the same bucket. Under ``disabled``, always returns None.

    Also force-clears the tenant's ``/tmp`` namespace before handing
    off the container so no scratch-file residue from the speculative
    workspace mount leaks into the real task. This runs on every
    consume, even under ``shared`` policy, because residue risk exists
    regardless of bucketing.
    """
    policy = get_policy()
    if policy == "disabled":
        return None

    bucket = _bucket_key(tenant_id)
    async with _lock:
        tenant_slots = _prewarmed_by_tenant.get(bucket)
        slot = tenant_slots.pop(task_id, None) if tenant_slots else None
        # Clean up empty bucket dict to keep snapshot() tidy.
        if tenant_slots is not None and not tenant_slots:
            _prewarmed_by_tenant.pop(bucket, None)

    # M5: force-clear /tmp before handoff regardless of policy. Best
    # effort — never let cleanup failure void a hit.
    effective_tid = slot.tenant_id if slot else (tenant_id or "t-default")
    try:
        from backend import tenant_quota as _tq
        _tq.cleanup_tenant_tmp(effective_tid)
    except Exception as exc:
        logger.debug("prewarm consume: /tmp cleanup for %s failed: %s",
                     effective_tid, exc)

    try:
        from backend import metrics as _m
        _m.prewarm_consumed_total.labels(
            result="hit" if slot else "miss",
        ).inc()
    except Exception as exc:
        logger.debug("prewarm consume metric bump failed: %s", exc)
    if slot:
        logger.info(
            "prewarm: consumed %s (agent_id=%s, tenant=%s)",
            task_id, slot.agent_id, slot.tenant_id,
        )
    return slot


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  cancel_all — DAG mutation / dispatch abort
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def cancel_all(
    *,
    stopper=None,
    reason: str = "dag_mutated",
    tenant_id: Optional[str] = None,
) -> int:
    """Stop every un-consumed pre-warmed container. Returns the count.
    Called on DAG mutation or dispatch abort so the 45-min lifetime
    budget isn't burned by idle containers.

    `stopper` is injectable for tests.

    M5: if ``tenant_id`` is provided, only that tenant's bucket is
    cleared (and under ``shared`` policy, that maps to the shared
    bucket). If omitted, every bucket is cleared — used when the
    whole app is shutting down or a global mutation invalidates all
    speculation.
    """
    if stopper is None:
        from backend.container import stop_container as stopper  # type: ignore

    async with _lock:
        if tenant_id is None:
            slots: list[PrewarmSlot] = []
            for bucket_slots in _prewarmed_by_tenant.values():
                slots.extend(bucket_slots.values())
            _prewarmed_by_tenant.clear()
        else:
            bucket = _bucket_key(tenant_id)
            bucket_slots = _prewarmed_by_tenant.pop(bucket, {})
            slots = list(bucket_slots.values())

    count = 0
    for slot in slots:
        try:
            await stopper(slot.agent_id)
        except Exception as exc:
            logger.warning(
                "prewarm cancel: stop_container for %s failed: %s",
                slot.agent_id, exc,
            )
            continue
        count += 1
        try:
            from backend import metrics as _m
            _m.prewarm_consumed_total.labels(result="cancelled").inc()
        except Exception as exc:
            logger.debug("prewarm metric bump failed: %s", exc)
    if count:
        logger.info(
            "prewarm: cancelled %d container(s) (%s, tenant=%s)",
            count, reason, tenant_id or "ALL",
        )
    return count


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _short_id(s: str) -> str:
    """8-char stable hash snippet for agent_id suffix."""
    import hashlib
    return hashlib.blake2b(s.encode("utf-8"), digest_size=4).hexdigest()


def snapshot() -> dict[str, str]:
    """Read-only view of what's currently pre-warmed (flat view for
    /healthz + debug). Maps task_id → agent_id across all tenant
    buckets; ambiguity is not a concern because task_ids are DAG-
    scoped and prewarm DAGs are distinct runs. For per-tenant
    inspection, use ``snapshot_by_tenant``.
    """
    out: dict[str, str] = {}
    for bucket_slots in _prewarmed_by_tenant.values():
        for tid, slot in bucket_slots.items():
            out[tid] = slot.agent_id
    return out


def snapshot_by_tenant() -> dict[str, dict[str, str]]:
    """Per-tenant read-only view: { tenant_id: { task_id: agent_id } }.
    Intended for admin debug endpoints and the M5 isolation tests."""
    return {
        bucket: {tid: slot.agent_id for tid, slot in slots.items()}
        for bucket, slots in _prewarmed_by_tenant.items()
    }

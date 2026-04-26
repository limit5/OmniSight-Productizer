"""Y6 #282 row 6 — Background workspace GC reaper.

Hourly async task (lifespan-scoped via ``backend/main.py``) that walks
the row-1 five-layer workspace hierarchy under ``_WORKSPACES_ROOT`` and
reclaims disk from agents whose worktrees outlived their owning agent.
Without this loop the only path that ever frees a workspace is
``backend.workspace.cleanup(agent_id)`` — an explicit call from the
agent finalize pipeline. Crashed runs, abandoned long-running shells,
operator-cancelled retries: each leaks one workspace dir that nothing
else reaps. Across weeks of production the disk cost is real (the
audit row 6 was opened to close).

Lifecycle per sweep
-------------------
1. **Stale leaf scan**: walk ``_WORKSPACES_ROOT`` excluding the
   ``_trash/`` sidecar. For each leaf workspace dir (``.git`` entry
   present at depth 5 under root, per row-1 layout) check:

   * ``mtime`` strictly older than
     ``settings.keep_recent_workspaces_stale_days`` — fresh dirs are
     left alone regardless of registry state.
   * ``agent_id`` (parent dir of the leaf hash) **NOT** in the
     in-process active registry ``backend.workspace._workspaces`` —
     "the agent has ended". This is per-worker in-memory state, so a
     crash that took the whole process down lets the next process
     observe an empty registry and reap freely; cross-worker case is
     handled by the same singleton-guard pattern as
     ``cleanup_orphan_worktrees`` (the GC reaper only reaps; it never
     resurrects state, so two workers reaping the same dir is
     idempotent — see module-global audit below).
   * ``.git/index.lock`` either missing or older than 60s — fresh
     locks indicate an in-flight git op even if the agent_id isn't
     in our registry (e.g. a CLI invocation outside the FastAPI
     lifespan). 60s mirrors ``backend.workspace.cleanup_stale_locks``.

   Matching candidates are moved to
   ``{_WORKSPACES_ROOT}/_trash/{tenant_id}/{timestamp}-{leaf_name}/``.
   We use ``rename`` (atomic, single-fs) so the leaf disappears from
   its tenant slice immediately — the tenant_quota measurement
   (row 5) sees the recovery on its next sweep without waiting for
   the trash-purge step. Falls back to ``shutil.move`` on cross-FS
   environments (test fixtures sometimes mount tmpfs).

2. **Trash purge**: walk ``{_WORKSPACES_ROOT}/_trash/`` and hard-
   delete any entry whose own ``mtime`` (set by the move) is older
   than ``settings.workspace_gc_trash_ttl_days``. The move-time mtime
   gives operators a stable "soft delete" window to recover an
   accidentally trashed workspace by ``mv``-ing it back.

3. **Per-tenant LRU eviction (optional)**: when
   ``backend.tenant_quota.measure_tenant_usage`` reports a tenant
   over hard, this sweep evicts older workspaces to trash *first*
   (per-project LRU, oldest mtime first) before deferring to the
   workflow-runs LRU in ``tenant_quota.lru_cleanup``. Spec-quote:
   "遇 tenant hard quota 超標時，優先刪舊的 workspace
   （per-project LRU）而非新的". The pre-emptive eviction is
   idempotent w.r.t. step 1 — a leaf evicted under quota pressure
   takes the same path through ``_trash/`` so the trash-purge step
   reclaims it on the same TTL.

4. **Telemetry**: SSE ``workspace_gc`` events on ``trashed`` and
   ``purged`` actions; audit rows
   ``workspace.gc_trashed`` / ``workspace.gc_purged`` /
   ``workspace.gc_quota_evicted`` capture the same payload for
   forensics.

Module-global state audit (per implement_phase_step.md Step 1,
2026-04-21 rule)
----------------------------------------------------------------
* ``_LOOP_RUNNING`` — singleton guard preventing the loop from
  starting twice in the same worker. Type-3 "intentionally per-worker"
  answer. Multiple workers running their own GC loops is harmless:
  ``rename(stale_leaf, trash_path)`` either wins (the loser's
  follow-up call sees ENOENT and quietly drops the candidate) or
  loses (same outcome). ``shutil.rmtree`` on a missing path is a
  no-op. The underlying durable state lives on the filesystem, not
  in the Python process. Same pattern as
  ``backend.user_drafts_gc._LOOP_RUNNING`` /
  ``backend.notifications._DLQ_RUNNING``.

Read-after-write timing-visible downstream tests
------------------------------------------------
N/A — the loop only deletes / moves files. There is no read-after-
write contract tied to its timing. The "workspace was just provisioned,
is it visible in measure_tenant_usage" contract is covered by row 5's
synchronous gate in ``provision()``, not by this periodic sweep.

Tuning knobs
------------
``OMNISIGHT_KEEP_RECENT_WORKSPACES_STALE_DAYS`` (default 30) — age
threshold for the leaf scan.

``OMNISIGHT_WORKSPACE_GC_TRASH_TTL_DAYS`` (default 7) — how long
trashed entries linger before hard delete.

``OMNISIGHT_WORKSPACE_GC_INTERVAL_S`` (default 3600.0) — seconds
between sweeps. Lower bound is soft.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Singleton loop guard (see module docstring's audit section for why
# this is intentionally per-worker).
_LOOP_RUNNING = False


# Sidecar dirname under ``_WORKSPACES_ROOT`` that holds soft-deleted
# workspaces. Underscore prefix matches the convention enforced by
# ``backend.workspace.cleanup_orphan_worktrees`` (which already skips
# ``_*`` dirs at the top level — designed for exactly this kind of
# operational sidecar). The sub-tree shape is
# ``_trash/{tenant_id}/{timestamp}-{leaf_name}/`` so per-tenant
# accounting and trash inspection both stay readable.
_TRASH_DIRNAME = "_trash"

# Stale lock window. Mirrors ``backend.workspace.cleanup_stale_locks``
# (60s) — a fresh lock indicates an in-flight git op we must not
# trample.
_FRESH_LOCK_AGE_S = 60.0

# Limit on how many leaves we evict per sweep when reacting to a
# quota breach. Bounds the wall-clock cost of a single sweep so a
# pathological tenant cannot starve the rest of the loop.
_MAX_QUOTA_EVICTIONS_PER_SWEEP = 20


@dataclass
class GCSummary:
    """Per-sweep telemetry returned to the loop logger and tests."""

    trashed: list[dict] = field(default_factory=list)
    purged: list[dict] = field(default_factory=list)
    quota_evicted: list[dict] = field(default_factory=list)
    skipped_busy: list[str] = field(default_factory=list)
    skipped_fresh: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "trashed": list(self.trashed),
            "purged": list(self.purged),
            "quota_evicted": list(self.quota_evicted),
            "skipped_busy": list(self.skipped_busy),
            "skipped_fresh": list(self.skipped_fresh),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _trash_root() -> Path:
    """Return ``{_WORKSPACES_ROOT}/_trash`` (lazy import to keep this
    module load-order independent of ``backend.workspace``)."""
    from backend.workspace import _WORKSPACES_ROOT
    return _WORKSPACES_ROOT / _TRASH_DIRNAME


def _is_workspace_busy(leaf: Path, active_paths: set[Path]) -> tuple[bool, str]:
    """Return ``(busy, reason)``. Busy means do-not-touch.

    Two reasons a leaf is "busy":

    1. It's in the in-process active registry (the owning agent is
       still executing). ``active_paths`` is a snapshot of resolved
       paths in ``_workspaces`` — caller passes it so we don't
       re-import / re-walk per leaf.
    2. ``.git/index.lock`` is present and younger than
       ``_FRESH_LOCK_AGE_S``. A fresh lock means a CLI / chatops
       process is mid-git-op even if no agent registry entry covers
       it.
    """
    try:
        resolved = leaf.resolve()
    except OSError:
        resolved = leaf
    if resolved in active_paths:
        return True, "registry"

    lock = leaf / ".git" / "index.lock"
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0.0
        if age < _FRESH_LOCK_AGE_S:
            return True, f"index.lock fresh ({age:.0f}s)"
    return False, ""


def _iter_workspace_leaves_for_gc():
    """Yield ``(tenant_id, leaf_path)`` for every workspace leaf under
    ``_WORKSPACES_ROOT`` excluding the ``_trash/`` sidecar.

    Reuses ``backend.workspace._iter_workspace_leaves`` so the layout
    knowledge stays in one place (the workspace module owns the row-1
    hierarchy). The generator filters out top-level ``_*`` operational
    sidecars (only ``_trash`` exists today, but the convention matches
    ``cleanup_orphan_worktrees``'s skip rule).
    """
    from backend.workspace import _WORKSPACES_ROOT, _iter_workspace_leaves
    if not _WORKSPACES_ROOT.exists():
        return
    for top in _WORKSPACES_ROOT.iterdir():
        if not top.is_dir():
            continue
        if top.name.startswith("_"):
            continue
        for leaf in _iter_workspace_leaves(top):
            yield top.name, leaf


def _move_to_trash(leaf: Path, tenant_id: str) -> Path:
    """Rename ``leaf`` into ``_trash/{tenant_id}/{ts}-{name}/`` and
    return the destination path.

    Uses ``Path.rename`` for atomicity on the same filesystem; falls
    back to ``shutil.move`` for cross-FS test fixtures (the tmpfs
    mount in CI is on a different device than the staging dir).

    The destination's ``mtime`` is set explicitly to "now" so the
    trash-purge TTL is anchored to the move time rather than the
    leaf's pre-move mtime (which is by construction old — that's
    why we're trashing it).
    """
    trash_dir = _trash_root() / tenant_id
    trash_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = trash_dir / f"{ts}-{leaf.name}"
    # Disambiguate if a same-second collision happens (rare but
    # possible under quota eviction storms).
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = trash_dir / f"{ts}-{leaf.name}-{suffix}"
    try:
        leaf.rename(dest)
    except OSError:
        # Cross-FS or other rename failure — fall back to copy+delete.
        shutil.move(str(leaf), str(dest))
    # Anchor the TTL to "now" — mtime on the trashed dir is the
    # move time, decoupled from the leaf's original last-touched
    # time (which is days old by definition).
    try:
        now = time.time()
        import os as _os
        _os.utime(dest, (now, now))
    except OSError:
        pass
    return dest


def _emit_gc_event(
    *, action: str, leaf_or_trash: Path, tenant_id: str | None,
    extra: dict | None = None,
) -> None:
    """SSE ``workspace_gc`` event. Best-effort — never raises."""
    try:
        from backend.events import bus
        bus.publish(
            "workspace_gc",
            {
                "action": action,
                "path": str(leaf_or_trash),
                "tenant_id": tenant_id,
                **(extra or {}),
            },
            broadcast_scope="tenant",
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.debug("workspace_gc SSE publish failed: %s", exc)


async def _emit_gc_audit(
    *, action: str, entity_id: str, before: dict, after: dict,
) -> None:
    """Audit row. Best-effort — same pattern as the rest of the
    workspace module's audit calls. Lazy import keeps this module's
    import-time graph clean.
    """
    try:
        from backend import audit as _audit
        await _audit.log(
            action=action,
            entity_kind="workspace",
            entity_id=entity_id,
            before=before,
            after=after,
            actor="system:workspace-gc",
        )
    except Exception as exc:
        logger.debug("workspace_gc audit log failed (%s): %s", action, exc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sweep — stale leaf scan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _sweep_stale_leaves(
    *, stale_seconds: float, summary: GCSummary,
) -> None:
    from backend.workspace import _workspaces

    active_paths: set[Path] = set()
    for info in _workspaces.values():
        try:
            if info.path.exists():
                active_paths.add(info.path.resolve())
        except OSError:
            continue

    cutoff = time.time() - stale_seconds

    for tenant_id, leaf in _iter_workspace_leaves_for_gc():
        try:
            mtime = leaf.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            summary.skipped_fresh.append(str(leaf))
            continue
        busy, reason = _is_workspace_busy(leaf, active_paths)
        if busy:
            summary.skipped_busy.append(f"{leaf} [{reason}]")
            continue

        # Capture the agent_id surface for audit / SSE before the
        # rename destroys the path. Under the row-1 layout the
        # agent_id is the parent of the leaf hash dir.
        agent_id = leaf.parent.name
        try:
            dest = _move_to_trash(leaf, tenant_id)
        except OSError as exc:
            logger.warning(
                "workspace_gc: trash move failed for %s: %s", leaf, exc,
            )
            continue

        record = {
            "leaf": str(leaf),
            "trash_path": str(dest),
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "mtime": mtime,
        }
        summary.trashed.append(record)
        _emit_gc_event(
            action="trashed", leaf_or_trash=dest, tenant_id=tenant_id,
            extra={"agent_id": agent_id, "leaf": str(leaf)},
        )
        await _emit_gc_audit(
            action="workspace.gc_trashed",
            entity_id=agent_id,
            before={"path": str(leaf), "mtime": mtime,
                    "tenant_id": tenant_id},
            after={"trash_path": str(dest), "reason": "stale"},
        )
        logger.info(
            "workspace_gc: trashed stale workspace %s → %s "
            "(tenant=%s, agent=%s, age=%.0fd)",
            leaf, dest, tenant_id, agent_id,
            (time.time() - mtime) / 86400,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sweep — trash purge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _sweep_trash_purge(
    *, ttl_seconds: float, summary: GCSummary,
) -> None:
    trash = _trash_root()
    if not trash.is_dir():
        return
    cutoff = time.time() - ttl_seconds
    for tenant_dir in trash.iterdir():
        if not tenant_dir.is_dir():
            continue
        for entry in tenant_dir.iterdir():
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            size = 0
            try:
                # Best-effort size accounting for telemetry.
                size = sum(
                    f.stat().st_size for f in entry.rglob("*") if f.is_file()
                )
            except OSError:
                pass
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "workspace_gc: purge failed for %s: %s", entry, exc,
                )
                continue
            record = {
                "trash_path": str(entry),
                "tenant_id": tenant_dir.name,
                "freed_bytes": size,
                "trashed_at": mtime,
            }
            summary.purged.append(record)
            _emit_gc_event(
                action="purged", leaf_or_trash=entry,
                tenant_id=tenant_dir.name,
                extra={"freed_bytes": size},
            )
            await _emit_gc_audit(
                action="workspace.gc_purged",
                entity_id=entry.name,
                before={"trash_path": str(entry), "mtime": mtime,
                        "tenant_id": tenant_dir.name,
                        "freed_bytes": size},
                after={"status": "deleted"},
            )
            logger.info(
                "workspace_gc: purged %s (tenant=%s, freed=%d bytes)",
                entry, tenant_dir.name, size,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Sweep — quota-driven LRU eviction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _list_tenants_with_workspaces() -> list[str]:
    """Top-level dirs under ``_WORKSPACES_ROOT`` that look like a
    tenant slice (i.e. excluding the ``_trash`` sidecar)."""
    from backend.workspace import _WORKSPACES_ROOT
    if not _WORKSPACES_ROOT.is_dir():
        return []
    out: list[str] = []
    for child in _WORKSPACES_ROOT.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        out.append(child.name)
    return out


def _project_lru_workspaces(tenant_id: str) -> list[tuple[Path, str]]:
    """Per-project LRU order — return ``(leaf, project_id)`` for every
    leaf under the tenant's slice, sorted oldest-first by mtime within
    each project group, then projects round-robin so we don't drain
    any single project entirely.

    Layout reminder (row 1):
    ``{root}/{tid}/{product_line}/{project_id}/{agent_id}/{hash}/``
    """
    from backend.workspace import _WORKSPACES_ROOT, _iter_workspace_leaves
    top = _WORKSPACES_ROOT / tenant_id
    if not top.is_dir():
        return []
    leaves = _iter_workspace_leaves(top)
    by_project: dict[str, list[tuple[Path, float]]] = {}
    for leaf in leaves:
        try:
            rel = leaf.relative_to(_WORKSPACES_ROOT).parts
        except ValueError:
            continue
        # rel = (tid, product_line, project_id, agent_id, hash)
        project_id = rel[2] if len(rel) >= 3 else "default"
        try:
            mtime = leaf.stat().st_mtime
        except OSError:
            continue
        by_project.setdefault(project_id, []).append((leaf, mtime))
    # Sort each project's bucket oldest-first.
    for bucket in by_project.values():
        bucket.sort(key=lambda t: t[1])
    # Round-robin merge so a single project with thousands of stale
    # workspaces doesn't monopolise the eviction quota.
    out: list[tuple[Path, str]] = []
    while any(by_project.values()):
        for project_id in list(by_project.keys()):
            bucket = by_project[project_id]
            if not bucket:
                del by_project[project_id]
                continue
            leaf, _ = bucket.pop(0)
            out.append((leaf, project_id))
    return out


async def _sweep_quota_evict(
    *, summary: GCSummary,
) -> None:
    """For each tenant currently over hard quota, trash older
    workspaces (per-project LRU) until the breach clears or we hit
    ``_MAX_QUOTA_EVICTIONS_PER_SWEEP``.

    The eviction path is the same ``_move_to_trash`` used by the
    stale scan — a quota-evicted workspace still gets the cool-down
    window before hard-delete, so an operator who realises a tenant
    was unfairly throttled can ``mv`` it back from ``_trash/`` within
    the TTL.
    """
    try:
        from backend import tenant_quota as _tq
        from backend.workspace import _workspaces
    except Exception as exc:
        logger.debug("workspace_gc: quota module import failed: %s", exc)
        return

    active_paths: set[Path] = set()
    for info in _workspaces.values():
        try:
            if info.path.exists():
                active_paths.add(info.path.resolve())
        except OSError:
            continue

    for tenant_id in _list_tenants_with_workspaces():
        try:
            quota = _tq.load_quota(tenant_id)
            usage = _tq.measure_tenant_usage(tenant_id)
        except Exception as exc:
            logger.debug(
                "workspace_gc: quota probe failed for %s: %s",
                tenant_id, exc,
            )
            continue
        if usage["total_bytes"] < quota.hard_bytes:
            continue

        evicted_here = 0
        for leaf, project_id in _project_lru_workspaces(tenant_id):
            if evicted_here >= _MAX_QUOTA_EVICTIONS_PER_SWEEP:
                break
            busy, reason = _is_workspace_busy(leaf, active_paths)
            if busy:
                summary.skipped_busy.append(f"{leaf} [{reason}]")
                continue
            try:
                size = sum(
                    f.stat().st_size for f in leaf.rglob("*") if f.is_file()
                )
            except OSError:
                size = 0
            agent_id = leaf.parent.name
            try:
                dest = _move_to_trash(leaf, tenant_id)
            except OSError as exc:
                logger.warning(
                    "workspace_gc: quota eviction move failed for %s: %s",
                    leaf, exc,
                )
                continue

            record = {
                "leaf": str(leaf),
                "trash_path": str(dest),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "agent_id": agent_id,
                "freed_bytes": size,
            }
            summary.quota_evicted.append(record)
            evicted_here += 1
            _emit_gc_event(
                action="quota_evicted", leaf_or_trash=dest,
                tenant_id=tenant_id,
                extra={
                    "project_id": project_id,
                    "agent_id": agent_id,
                    "freed_bytes": size,
                },
            )
            await _emit_gc_audit(
                action="workspace.gc_quota_evicted",
                entity_id=agent_id,
                before={
                    "path": str(leaf), "tenant_id": tenant_id,
                    "project_id": project_id, "freed_bytes": size,
                    "hard_bytes": quota.hard_bytes,
                    "used_bytes": usage["total_bytes"],
                },
                after={"trash_path": str(dest), "reason": "quota_hard"},
            )
            logger.warning(
                "workspace_gc: quota-evicted %s → %s "
                "(tenant=%s, project=%s, freed=%d bytes)",
                leaf, dest, tenant_id, project_id, size,
            )

            # Re-probe usage so we stop as soon as we drop under
            # hard. Cheap relative to the cost of an unnecessary
            # eviction.
            try:
                usage = _tq.measure_tenant_usage(tenant_id)
            except Exception:
                break
            if usage["total_bytes"] < quota.hard_bytes:
                break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y9 #285 row 3 — workspace-GB-hour billing sample
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# PG advisory-lock key for the workspace-GB-hour billing sampler.
# ``pg_try_advisory_xact_lock`` returns False if any other transaction
# already holds the lock; we use that to guarantee at most one worker
# emits the per-sweep samples even when N uvicorn workers each have
# their own GC loop running. The numeric key is hashed from a fixed
# string to avoid collision with ad-hoc advisory-lock users.
#
# Module-global state audit (per implement_phase_step.md Step 1):
# the constant is immutable — every uvicorn worker derives the same
# value (audit answer #1). The lock itself is a PG-side serialisation
# mechanism that is shared across all workers (audit answer #2 —
# coordinated via PG).
_WORKSPACE_GB_HOUR_LOCK_KEY = "billing-workspace-gb-hour-sampler"


def _list_projects_per_tenant() -> dict[str, list[tuple[str, float]]]:
    """Walk the workspace tree and return ``{tenant_id: [(project_id,
    size_bytes), ...]}``.

    The size is the sum of leaf bytes under each
    ``{root}/{tid}/{product_line}/{project_id}/`` slice (per the row 1
    five-layer layout). Best-effort: per-leaf ``stat()`` errors are
    swallowed and that leaf's bytes are dropped from the sum — the
    billing sample is meant to be approximate, not byte-exact.
    """
    from backend.workspace import _WORKSPACES_ROOT
    out: dict[str, list[tuple[str, float]]] = {}
    if not _WORKSPACES_ROOT.is_dir():
        return out
    for tenant_dir in _WORKSPACES_ROOT.iterdir():
        if not tenant_dir.is_dir() or tenant_dir.name.startswith("_"):
            continue
        tid = tenant_dir.name
        # Per-project size accumulator. Layout:
        # {root}/{tid}/{product_line}/{project_id}/{agent_id}/{hash}/
        per_project: dict[str, float] = {}
        for product_line_dir in tenant_dir.iterdir():
            if not product_line_dir.is_dir():
                continue
            for project_dir in product_line_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                pid = project_dir.name
                size = 0.0
                try:
                    for f in project_dir.rglob("*"):
                        try:
                            if f.is_file():
                                size += float(f.stat().st_size)
                        except OSError:
                            continue
                except OSError:
                    pass
                per_project[pid] = per_project.get(pid, 0.0) + size
        if per_project:
            out[tid] = sorted(per_project.items())
    return out


async def _emit_workspace_gb_hour_samples(
    *, sample_window_s: float,
) -> None:
    """Emit one ``billing_usage_events.workspace_gb_hour`` row per
    ``(tenant_id, project_id)`` slice with non-zero disk usage.

    Cross-worker dedupe
    ───────────────────
    Wraps the emit loop in ``pg_try_advisory_xact_lock`` keyed on
    ``billing-workspace-gb-hour-sampler``. If the lock is already held
    (another uvicorn worker's GC loop is in the same window) we silently
    skip the emit — this guarantees exactly-one-sample-per-sweep across
    workers without depending on per-worker process state. Same pattern
    as the audit-chain advisory lock in ``backend.audit._log_impl``.

    GB-hour computation
    ───────────────────
    Each emit row carries ``gb_hours = current_gb × hours_in_window``
    where ``hours_in_window = sample_window_s / 3600``. Caller passes
    ``sample_window_s = settings.workspace_gc_interval_s`` so a
    sweep-per-hour cadence yields one row per hour with
    ``gb_hours ≈ current_gb``. T4 sums these across the billing period.
    """
    per_tenant = _list_projects_per_tenant()
    if not per_tenant:
        return
    hours = max(0.0, float(sample_window_s) / 3600.0)
    if hours == 0.0:
        return

    from backend.db_pool import get_pool
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                got = await conn.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext($1))",
                    _WORKSPACE_GB_HOUR_LOCK_KEY,
                )
                if not got:
                    logger.debug(
                        "workspace_gb_hour sampler: lock already held by "
                        "another worker, skipping emit this sweep"
                    )
                    return
                # Hold the lock for the duration of the emit loop. The
                # billing emitter borrows its own conn from the pool
                # for each row; that's intentional so a slow row can't
                # block lock release on the long-held conn we hold here.
                from backend import billing_usage as _billing
                for tid, projects in per_tenant.items():
                    for pid, size_bytes in projects:
                        gb = float(size_bytes) / (1024.0 ** 3)
                        gb_hours = round(gb * hours, 6)
                        if gb_hours <= 0.0:
                            continue
                        await _billing.record_workspace_gb_hour(
                            gb_hours=gb_hours,
                            tenant_id=tid,
                            project_id=pid,
                            metadata={
                                "sample_window_s": float(sample_window_s),
                                "size_bytes": float(size_bytes),
                            },
                        )
    except Exception as exc:
        logger.warning(
            "workspace_gb_hour sampler outer transaction failed: %s", exc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public sweep entry + loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def sweep_once(
    *,
    stale_days: int | None = None,
    trash_ttl_days: int | None = None,
) -> GCSummary:
    """One full sweep: stale-leaf scan → trash purge → quota eviction.

    Returns a :class:`GCSummary` with counts + paths for telemetry.
    Exposed as a public entry point so tests can exercise the sweep
    without spinning the loop, and so an operator could trigger a
    manual sweep through an admin handler if ever needed.

    Argument overrides exist for tests; production reads from the
    Settings object.
    """
    from backend.config import settings
    if stale_days is None:
        stale_days = int(settings.keep_recent_workspaces_stale_days)
    if trash_ttl_days is None:
        trash_ttl_days = int(settings.workspace_gc_trash_ttl_days)
    stale_seconds = max(0, stale_days) * 86400.0
    ttl_seconds = max(0, trash_ttl_days) * 86400.0

    summary = GCSummary()
    try:
        await _sweep_stale_leaves(
            stale_seconds=stale_seconds, summary=summary,
        )
    except Exception as exc:
        logger.warning("workspace_gc: stale-leaf scan failed: %s", exc)
    try:
        await _sweep_trash_purge(
            ttl_seconds=ttl_seconds, summary=summary,
        )
    except Exception as exc:
        logger.warning("workspace_gc: trash purge failed: %s", exc)
    try:
        await _sweep_quota_evict(summary=summary)
    except Exception as exc:
        logger.warning("workspace_gc: quota eviction failed: %s", exc)

    # Y9 #285 row 1 — aggregate sweep-completion audit row. Per-leaf
    # ``workspace.gc_trashed`` / ``workspace.gc_purged`` /
    # ``workspace.gc_quota_evicted`` rows continue to fire alongside;
    # this single ``workspace.gc_executed`` row gives downstream
    # consumers (T-series billing rollup, ops dashboard) one record per
    # sweep without scanning thousands of per-leaf rows. Best-effort —
    # the sweep itself has already happened, so an audit failure must
    # not regress the on-disk state.
    try:
        from backend import audit_events as _audit_events
        await _audit_events.emit_workspace_gc_executed(
            summary=summary.as_dict(),
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.debug(
            "workspace.gc_executed audit emit failed: %s", exc,
        )

    # Y9 #285 row 3 — per-(tenant_id, project_id) workspace-GB-hour
    # billing sample fan-out. Best-effort — billing emit failure must
    # never regress the on-disk GC state. See
    # ``_emit_workspace_gb_hour_samples`` for the multi-worker dedupe
    # contract (PG advisory-lock, exactly-one-emitter-per-sweep).
    try:
        from backend.config import settings
        sweep_interval_s = float(getattr(
            settings, "workspace_gc_interval_s", 3600.0,
        ))
        await _emit_workspace_gb_hour_samples(
            sample_window_s=sweep_interval_s,
        )
    except Exception as exc:  # pragma: no cover — billing already swallows
        logger.debug(
            "workspace_gb_hour billing emit failed: %s", exc,
        )

    return summary


async def run_gc_loop(*, interval_s: float | None = None) -> None:
    """Background coroutine: ``sweep_once`` every ``interval_s`` seconds.

    Singleton-guarded so that test fixtures reusing the same process
    don't end up with two loops fighting on the same on-disk state.
    Exits cleanly on ``CancelledError`` (the lifespan shutdown path
    cancels it as part of the drain).
    """
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True
    try:
        from backend.config import settings
        interval = (
            float(interval_s)
            if interval_s is not None
            else float(settings.workspace_gc_interval_s)
        )
        # Stagger the first run so startup doesn't pile every
        # background loop onto the pool at once. Mirrors the
        # convention in user_drafts_gc / tenant_quota.
        try:
            await asyncio.sleep(min(60.0, interval / 2))
        except asyncio.CancelledError:
            return

        while True:
            try:
                summary = await sweep_once()
                if (summary.trashed or summary.purged
                        or summary.quota_evicted):
                    logger.info(
                        "workspace_gc sweep: trashed=%d purged=%d "
                        "quota_evicted=%d skipped_busy=%d "
                        "skipped_fresh=%d",
                        len(summary.trashed), len(summary.purged),
                        len(summary.quota_evicted),
                        len(summary.skipped_busy),
                        len(summary.skipped_fresh),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "workspace_gc sweep loop error: %s", exc,
                )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
    finally:
        _LOOP_RUNNING = False


def _reset_for_tests() -> None:
    """Clear the singleton flag between tests so each case starts
    from a known state. Matches the
    ``user_drafts_gc._reset_for_tests`` convention.
    """
    global _LOOP_RUNNING
    _LOOP_RUNNING = False

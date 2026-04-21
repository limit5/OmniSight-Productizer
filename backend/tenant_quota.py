"""M2 — Per-tenant disk quota + LRU cleanup.

Companion to ``backend/quota.py`` (which only handles request rate-limit
budgets). This module owns *disk* budget: per-tenant ``quota.yaml`` with
``soft_bytes`` / ``hard_bytes`` thresholds, computation of current
on-disk usage, soft-warning SSE emission, hard-fail enforcement (used by
sandbox create to return 507 Insufficient Storage), and an LRU cleanup
helper that prunes the oldest completed workflow_run artifacts while
preserving anything the operator marked ``keep=true``.

quota.yaml schema (per ``data/tenants/<tid>/quota.yaml``)::

    soft_bytes: 5368709120        # 5 GiB
    hard_bytes: 10737418240       # 10 GiB
    keep_recent_runs: 5           # never LRU-prune the N newest runs
    plan: free                    # mirrors tenants.plan when materialised

If the file does not exist, the plan→quota table below is consulted with
the tenant's plan from the ``tenants`` row (default ``free``). The file
is materialised the first time the sweep observes the tenant so that
operators can hand-edit the file to override the plan default.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from backend.tenant_fs import (
    tenant_artifacts_root,
    tenant_data_root,
    tenant_ingest_root,
    tenant_workflow_runs_root,
    tenants_root,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Plan defaults
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GIB = 1024 ** 3


@dataclass(frozen=True)
class DiskQuota:
    soft_bytes: int
    hard_bytes: int
    keep_recent_runs: int = 5

    def as_dict(self) -> dict:
        return asdict(self)


PLAN_DISK_QUOTAS: dict[str, DiskQuota] = {
    "free": DiskQuota(soft_bytes=5 * GIB, hard_bytes=10 * GIB, keep_recent_runs=5),
    "starter": DiskQuota(soft_bytes=20 * GIB, hard_bytes=40 * GIB, keep_recent_runs=10),
    "pro": DiskQuota(soft_bytes=100 * GIB, hard_bytes=200 * GIB, keep_recent_runs=20),
    "enterprise": DiskQuota(soft_bytes=500 * GIB, hard_bytes=1000 * GIB, keep_recent_runs=50),
}

DEFAULT_PLAN = "free"

QUOTA_FILENAME = "quota.yaml"
KEEP_MARKER = ".keep"  # presence of this sidecar file inside an artifact / run dir
                       # protects it from LRU cleanup. Operators can `touch` it
                       # via the manual cleanup UI.


def quota_for_plan(plan: str | None) -> DiskQuota:
    return PLAN_DISK_QUOTAS.get(plan or DEFAULT_PLAN, PLAN_DISK_QUOTAS[DEFAULT_PLAN])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quota file load / write
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def quota_file_path(tenant_id: str) -> Path:
    return tenant_data_root(tenant_id) / QUOTA_FILENAME


def load_quota(tenant_id: str, plan: str | None = None) -> DiskQuota:
    """Load tenant's quota.yaml, falling back to plan defaults."""
    fp = quota_file_path(tenant_id)
    if fp.is_file():
        try:
            data = yaml.safe_load(fp.read_text()) or {}
            return DiskQuota(
                soft_bytes=int(data.get("soft_bytes", quota_for_plan(plan).soft_bytes)),
                hard_bytes=int(data.get("hard_bytes", quota_for_plan(plan).hard_bytes)),
                keep_recent_runs=int(data.get("keep_recent_runs", quota_for_plan(plan).keep_recent_runs)),
            )
        except Exception as exc:
            logger.warning("quota.yaml parse failed for %s: %s — using plan default", tenant_id, exc)
    return quota_for_plan(plan)


def write_quota(tenant_id: str, quota: DiskQuota, plan: str | None = None) -> Path:
    """Materialise quota.yaml so operators can hand-edit / inspect it."""
    fp = quota_file_path(tenant_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = quota.as_dict()
    if plan:
        payload["plan"] = plan
    fp.write_text(yaml.safe_dump(payload, sort_keys=True))
    return fp


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Disk usage measurement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _dir_size_bytes(p: Path) -> int:
    """Recursive sum of file sizes (follows no symlinks)."""
    if not p.exists():
        return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(p, followlinks=False):
            for name in files:
                fp = Path(root) / name
                try:
                    st = fp.lstat()
                    if not (st.st_mode & 0o170000) == 0o120000:  # skip symlinks
                        total += st.st_size
                except OSError:
                    continue
    except OSError as exc:
        logger.debug("dir walk failed for %s: %s", p, exc)
    return total


def measure_tenant_usage(tenant_id: str) -> dict[str, int]:
    """Per-subdir byte usage. Includes both the tenant data root and /tmp."""
    artifacts = _dir_size_bytes(tenant_artifacts_root(tenant_id))
    workflow_runs = _dir_size_bytes(tenant_workflow_runs_root(tenant_id))
    backups = _dir_size_bytes(tenant_data_root(tenant_id) / "backups")
    ingest_tmp = _dir_size_bytes(tenant_ingest_root(tenant_id))
    total = artifacts + workflow_runs + backups + ingest_tmp
    return {
        "artifacts_bytes": artifacts,
        "workflow_runs_bytes": workflow_runs,
        "backups_bytes": backups,
        "ingest_tmp_bytes": ingest_tmp,
        "total_bytes": total,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Quota enforcement (write-path)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class QuotaExceeded(Exception):
    """Raised when a write would push a tenant past its hard quota.

    The HTTP layer should translate this into a 507 Insufficient Storage
    (RFC 4918) so clients can distinguish it from generic 5xx errors.
    """

    def __init__(self, tenant_id: str, used: int, hard: int):
        self.tenant_id = tenant_id
        self.used = used
        self.hard = hard
        super().__init__(
            f"Tenant {tenant_id} disk quota exceeded: {used} / {hard} bytes"
        )


def check_hard_quota(tenant_id: str, plan: str | None = None,
                     usage: dict[str, int] | None = None) -> None:
    """Raise ``QuotaExceeded`` if tenant is at/over hard quota.

    Callers (sandbox create, artifact write, workflow run create, …)
    should call this *before* allocating new storage. ``usage`` may be
    pre-computed by the sweep loop to avoid a redundant ``du``.
    """
    quota = load_quota(tenant_id, plan)
    if usage is None:
        usage = measure_tenant_usage(tenant_id)
    if usage["total_bytes"] >= quota.hard_bytes:
        raise QuotaExceeded(tenant_id, usage["total_bytes"], quota.hard_bytes)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LRU cleanup
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_kept(path: Path) -> bool:
    """A directory is kept if it (or any ancestor up to the run root)
    contains a ``.keep`` marker file. We only check the immediate dir
    so callers can mark individual runs without touching siblings."""
    return (path / KEEP_MARKER).is_file()


def _completed_runs(tenant_id: str) -> list[Path]:
    """Return all top-level workflow_run dirs sorted oldest-first by mtime.

    A workflow_run is considered "completed" if its dir has no
    ``.in_progress`` sentinel. This avoids deleting a run that is still
    being written to.
    """
    root = tenant_workflow_runs_root(tenant_id)
    if not root.is_dir():
        return []
    runs: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if (child / ".in_progress").is_file():
            continue
        runs.append(child)
    runs.sort(key=lambda p: p.stat().st_mtime)
    return runs


def lru_cleanup(tenant_id: str, plan: str | None = None,
                target_bytes: int | None = None) -> dict:
    """Delete oldest completed workflow_run dirs (and their artifacts) until
    usage drops below ``target_bytes`` (default = soft quota * 0.9).

    Always preserves the most-recent ``keep_recent_runs`` runs and any
    run with a ``.keep`` marker. Returns a summary dict.
    """
    quota = load_quota(tenant_id, plan)
    if target_bytes is None:
        target_bytes = int(quota.soft_bytes * 0.9)

    runs = _completed_runs(tenant_id)
    # Reserve the newest N (regardless of keep marker — those are
    # protected anyway, but the "recent N" rule applies first).
    reserved = set(p.resolve() for p in runs[-quota.keep_recent_runs:])

    usage_before = measure_tenant_usage(tenant_id)
    deleted: list[dict] = []
    skipped_keep: list[str] = []
    skipped_recent: list[str] = []

    if usage_before["total_bytes"] <= target_bytes:
        return {
            "tenant_id": tenant_id,
            "usage_before_bytes": usage_before["total_bytes"],
            "usage_after_bytes": usage_before["total_bytes"],
            "target_bytes": target_bytes,
            "deleted": deleted,
            "skipped_keep": skipped_keep,
            "skipped_recent": skipped_recent,
        }

    # Walk oldest-first, stop once we're below target.
    current_total = usage_before["total_bytes"]
    for run_dir in runs:
        if current_total <= target_bytes:
            break
        if run_dir.resolve() in reserved:
            skipped_recent.append(run_dir.name)
            continue
        if _is_kept(run_dir):
            skipped_keep.append(run_dir.name)
            continue
        size = _dir_size_bytes(run_dir)
        try:
            shutil.rmtree(run_dir)
            deleted.append({"run_id": run_dir.name, "freed_bytes": size})
            current_total -= size
        except OSError as exc:
            logger.warning("LRU rmtree failed for %s: %s", run_dir, exc)

    usage_after = measure_tenant_usage(tenant_id)
    return {
        "tenant_id": tenant_id,
        "usage_before_bytes": usage_before["total_bytes"],
        "usage_after_bytes": usage_after["total_bytes"],
        "target_bytes": target_bytes,
        "deleted": deleted,
        "skipped_keep": skipped_keep,
        "skipped_recent": skipped_recent,
    }


def cleanup_tenant_tmp(tenant_id: str) -> int:
    """Force-clear the tenant's /tmp/omnisight_ingest/<tid>/ namespace.

    Called from ``stop_container`` so a sandbox's scratch space is
    wiped at end-of-run regardless of how the sandbox exits. Returns
    the number of bytes freed.
    """
    root = tenant_ingest_root(tenant_id)
    freed = _dir_size_bytes(root)
    if not root.is_dir():
        return 0
    for child in root.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("tmp cleanup skipped %s: %s", child, exc)
    return freed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background sweep
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SWEEP_INTERVAL_S: float = float(
    os.environ.get("OMNISIGHT_QUOTA_SWEEP_S", "300.0")  # 5 minutes
)

# Cooldown so a tenant that stays over soft doesn't spam SSE every sweep.
_warning_cooldown_s: float = float(
    os.environ.get("OMNISIGHT_QUOTA_WARN_COOLDOWN_S", "1800.0")  # 30 min
)
_last_warning_at: dict[str, float] = {}


def _list_tenants_on_disk() -> list[str]:
    root = tenants_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


async def _resolve_plan(tenant_id: str) -> str:
    """Best-effort lookup of tenants.plan. DB unavailable → ``free``."""
    try:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as conn:
            plan = await conn.fetchval(
                "SELECT plan FROM tenants WHERE id = $1", tenant_id,
            )
        if plan:
            return plan
    except Exception as exc:
        logger.debug("tenant plan lookup failed for %s: %s", tenant_id, exc)
    return DEFAULT_PLAN


def _emit_warning(tenant_id: str, usage: dict, quota: DiskQuota,
                  level: str) -> None:
    """Push an SSE ``tenant_storage_warning`` so the UI can flash a banner.

    ``level`` is one of ``"soft"`` / ``"hard"``. Best-effort — never
    raises out of the sweep loop.
    """
    try:
        from backend.events import bus
        bus.publish("tenant_storage_warning", {
            "tenant_id": tenant_id,
            "level": level,
            "used_bytes": usage["total_bytes"],
            "soft_bytes": quota.soft_bytes,
            "hard_bytes": quota.hard_bytes,
            "breakdown": {
                "artifacts": usage["artifacts_bytes"],
                "workflow_runs": usage["workflow_runs_bytes"],
                "backups": usage["backups_bytes"],
                "ingest_tmp": usage["ingest_tmp_bytes"],
            },
        }, broadcast_scope="tenant", tenant_id=tenant_id)
    except Exception as exc:
        logger.debug("storage warning publish failed: %s", exc)
    try:
        from backend import audit as _audit
        # audit.log is async; schedule fire-and-forget
        loop = asyncio.get_event_loop()
        loop.create_task(_audit.log(
            action="tenant_storage_warning",
            entity_kind="tenant",
            entity_id=tenant_id,
            after={
                "level": level,
                "used_bytes": usage["total_bytes"],
                "soft_bytes": quota.soft_bytes,
                "hard_bytes": quota.hard_bytes,
            },
            actor="system:quota-sweep",
        ))
    except Exception as exc:
        logger.debug("audit log of storage warning failed: %s", exc)


async def sweep_tenant(tenant_id: str) -> dict:
    """Single-tenant pass: measure, materialise quota.yaml, emit warning,
    auto-LRU when over soft.

    Returns the per-tenant summary dict (used by the sweep loop and the
    REST API).
    """
    plan = await _resolve_plan(tenant_id)
    quota = load_quota(tenant_id, plan)

    # Materialise quota.yaml the first time we see a tenant. Operators
    # can then hand-edit to override.
    if not quota_file_path(tenant_id).is_file():
        try:
            write_quota(tenant_id, quota, plan=plan)
        except OSError as exc:
            logger.debug("quota.yaml materialise failed for %s: %s", tenant_id, exc)

    usage = measure_tenant_usage(tenant_id)
    over_hard = usage["total_bytes"] >= quota.hard_bytes
    over_soft = usage["total_bytes"] >= quota.soft_bytes
    cleanup_summary: dict | None = None

    now = time.time()
    last = _last_warning_at.get(tenant_id, 0.0)

    if over_hard:
        _emit_warning(tenant_id, usage, quota, level="hard")
        _last_warning_at[tenant_id] = now
        # Also try to claw some space back so subsequent writes can
        # proceed once the operator either bumps the quota or marks
        # runs for keep.
        cleanup_summary = lru_cleanup(tenant_id, plan=plan)
    elif over_soft:
        if now - last >= _warning_cooldown_s:
            _emit_warning(tenant_id, usage, quota, level="soft")
            _last_warning_at[tenant_id] = now
        cleanup_summary = lru_cleanup(tenant_id, plan=plan)
    else:
        # Reset cooldown so the next breach fires immediately.
        _last_warning_at.pop(tenant_id, None)

    return {
        "tenant_id": tenant_id,
        "plan": plan,
        "quota": quota.as_dict(),
        "usage": usage,
        "over_soft": over_soft,
        "over_hard": over_hard,
        "cleanup": cleanup_summary,
    }


async def sweep_all_tenants() -> list[dict]:
    """Iterate every tenant directory on disk and sweep each in turn.

    Sequential rather than concurrent — the work is I/O on the same
    block device, so parallelism doesn't help and a slow tenant can't
    starve the others if they're all serialised.
    """
    summaries: list[dict] = []
    for tid in _list_tenants_on_disk():
        try:
            summaries.append(await sweep_tenant(tid))
        except Exception as exc:
            logger.warning("quota sweep failed for %s: %s", tid, exc)
    return summaries


async def run_quota_sweep_loop(interval_s: float = SWEEP_INTERVAL_S) -> None:
    """Background task: sweep every tenant on a fixed cadence."""
    # Stagger the first run so we don't pile on with the other startup loops.
    await asyncio.sleep(min(30.0, interval_s / 2))
    while True:
        try:
            summaries = await sweep_all_tenants()
            breaches = [s for s in summaries if s["over_soft"] or s["over_hard"]]
            if breaches:
                logger.info(
                    "quota sweep: %d tenant(s) over threshold: %s",
                    len(breaches),
                    [s["tenant_id"] for s in breaches],
                )
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("quota sweep loop error: %s", exc)
            await asyncio.sleep(interval_s)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _reset_for_tests() -> None:
    _last_warning_at.clear()

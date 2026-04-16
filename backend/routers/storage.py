"""M2 — Per-tenant disk quota REST API.

Surface for the Settings → Storage UI panel. All endpoints are
tenant-scoped: a viewer sees their own tenant's usage; admins can
override via ``?tenant_id=`` to inspect another tenant.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import auth as _au
from backend import tenant_quota as _tq

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage", tags=["storage"])


def _resolve_tenant(user: _au.User, tenant_id: str | None) -> str:
    """Pick the tenant to operate on. Admins may override via query;
    everyone else is locked to their own tenant."""
    if tenant_id and tenant_id != user.tenant_id:
        if not _au.role_at_least(user.role, "admin"):
            raise HTTPException(
                status_code=403,
                detail="Only admins may inspect another tenant's storage",
            )
        return tenant_id
    return user.tenant_id or "t-default"


@router.get("/usage")
async def get_storage_usage(
    tenant_id: str | None = Query(default=None),
    user: _au.User = Depends(_au.require_viewer),
):
    """Current usage breakdown + quota for a tenant.

    Returns the dict shape consumed by the Storage settings UI:
    plan / quota / usage breakdown / over_soft / over_hard.
    """
    tid = _resolve_tenant(user, tenant_id)
    plan = await _tq._resolve_plan(tid)
    quota = _tq.load_quota(tid, plan)
    usage = _tq.measure_tenant_usage(tid)
    return {
        "tenant_id": tid,
        "plan": plan,
        "quota": quota.as_dict(),
        "usage": usage,
        "over_soft": usage["total_bytes"] >= quota.soft_bytes,
        "over_hard": usage["total_bytes"] >= quota.hard_bytes,
    }


@router.post("/cleanup")
async def trigger_cleanup(
    tenant_id: str | None = Query(default=None),
    target_bytes: int | None = Query(default=None),
    user: _au.User = Depends(_au.require_operator),
):
    """Operator-triggered manual LRU pass.

    By default cleans down to 90 % of soft quota; operators can pass an
    explicit ``target_bytes`` to free more aggressively.
    """
    tid = _resolve_tenant(user, tenant_id)
    plan = await _tq._resolve_plan(tid)
    summary = _tq.lru_cleanup(tid, plan=plan, target_bytes=target_bytes)
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_storage_cleanup",
            entity_kind="tenant",
            entity_id=tid,
            after={
                "deleted_count": len(summary.get("deleted", [])),
                "freed_bytes": (
                    summary["usage_before_bytes"] - summary["usage_after_bytes"]
                ),
                "target_bytes": summary["target_bytes"],
            },
            actor=f"user:{user.email}",
        )
    except Exception as exc:
        logger.debug("audit log of manual cleanup failed: %s", exc)
    return summary


@router.post("/sweep")
async def trigger_sweep(
    tenant_id: str | None = Query(default=None),
    user: _au.User = Depends(_au.require_operator),
):
    """On-demand quota sweep (otherwise runs on the 5-min background loop)."""
    tid = _resolve_tenant(user, tenant_id)
    return await _tq.sweep_tenant(tid)

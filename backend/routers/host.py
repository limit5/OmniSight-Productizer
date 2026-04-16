"""M4 — Host-level per-tenant metrics endpoints.

    GET  /api/v1/host/metrics                  → all tenants (admin only)
    GET  /api/v1/host/metrics?tenant_id=<tid>  → one tenant (admin any, user self)
    GET  /api/v1/host/metrics/me               → current user's tenant
    GET  /api/v1/host/accounting               → cumulative billing (admin only)

The shape matches ``host_metrics.TenantUsage`` so the UI can render the
same object whether it's looking at a single tenant or iterating over
the admin list.

ACL rules:
  * ``admin`` may pass any ``tenant_id`` or omit it (= all tenants).
  * ``viewer`` / ``operator`` may only ever read their *own* tenant.
    Requests with a different ``tenant_id`` get 403. Omitted
    ``tenant_id`` is silently rewritten to the user's ``tenant_id``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from backend import auth as _au
from backend import host_metrics as _hm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/host", tags=["host"])


def _usage_to_dict(u: _hm.TenantUsage) -> dict:
    return {
        "tenant_id": u.tenant_id,
        "cpu_percent": round(u.cpu_percent, 2),
        "mem_used_gb": round(u.mem_used_gb, 3),
        "disk_used_gb": round(u.disk_used_gb, 3),
        "sandbox_count": u.sandbox_count,
    }


@router.get("/metrics")
async def get_host_metrics(
    tenant_id: str | None = Query(default=None),
    user: _au.User = Depends(_au.current_user),
) -> dict:
    """Return per-tenant resource usage.

    * ``tenant_id`` omitted:
        - admin → full list of all tenants with running sandboxes
        - non-admin → auto-scoped to the caller's own tenant
    * ``tenant_id`` set:
        - admin → any tenant
        - non-admin → only their own, else 403
    """
    is_admin = user.role == "admin"
    if tenant_id is None:
        if is_admin:
            return {"tenants": [_usage_to_dict(u) for u in _hm.get_all_tenant_usage()]}
        usage = _hm.get_tenant_usage(user.tenant_id)
        return {"tenant": _usage_to_dict(usage)}
    if not is_admin and tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot read another tenant's metrics")
    usage = _hm.get_tenant_usage(tenant_id)
    return {"tenant": _usage_to_dict(usage)}


@router.get("/metrics/me")
async def get_my_tenant_metrics(user: _au.User = Depends(_au.current_user)) -> dict:
    """Shortcut for the UI's "current tenant" bar — same shape as
    ``/metrics?tenant_id=<self>`` but with no query string."""
    return {"tenant": _usage_to_dict(_hm.get_tenant_usage(user.tenant_id))}


@router.get("/accounting")
async def get_accounting(_user: _au.User = Depends(_au.require_admin)) -> dict:
    """Cumulative cpu_seconds / mem_gb_seconds per tenant (billing feed).

    Admin-only because this is the primary invoicing signal.
    """
    rows = _hm.snapshot_accounting()
    return {
        "tenants": [
            {
                "tenant_id": a.tenant_id,
                "cpu_seconds_total": round(a.cpu_seconds_total, 3),
                "mem_gb_seconds_total": round(a.mem_gb_seconds_total, 3),
                "last_updated": a.last_updated,
            }
            for a in rows
        ],
    }

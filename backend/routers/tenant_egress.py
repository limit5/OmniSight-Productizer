"""M6 — Per-tenant egress allow-list REST surface.

ACL summary
-----------

  GET    /tenants/me/egress                   any authenticated user
  GET    /tenants/{tid}/egress                admin
  GET    /tenants/egress                      admin (list every tenant)
  PUT    /tenants/{tid}/egress                admin

  POST   /tenants/me/egress/requests          viewer / operator
  GET    /tenants/me/egress/requests          viewer / operator (own only)
  GET    /tenants/egress/requests             admin (any tenant)
  POST   /tenants/egress/requests/{rid}/approve   admin
  POST   /tenants/egress/requests/{rid}/reject    admin

  POST   /tenants/{tid}/egress/dns-cache/reset    admin (force re-resolve)

The router intentionally rejects cross-tenant reads/writes from
non-admin callers — even reading another tenant's policy leaks
business intelligence (which provider keys they call out to).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from backend import auth as _au
from backend import tenant_egress as _te

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants", tags=["tenant_egress"])


def _policy_dict(p: _te.EgressPolicy) -> dict:
    d = p.to_dict()
    return d


def _request_dict(r: _te.EgressRequest) -> dict:
    return r.to_dict()


@router.get("/me/egress")
async def get_my_egress(user: _au.User = Depends(_au.current_user)) -> dict:
    pol = await _te.get_policy(user.tenant_id)
    return {"policy": _policy_dict(pol)}


@router.get("/egress")
async def list_egress_policies(
    _admin: _au.User = Depends(_au.require_admin),
) -> dict:
    pols = await _te.list_policies()
    return {"policies": [_policy_dict(p) for p in pols]}


@router.get("/{tid}/egress")
async def get_egress(
    tid: str,
    _admin: _au.User = Depends(_au.require_admin),
) -> dict:
    pol = await _te.get_policy(tid)
    return {"policy": _policy_dict(pol)}


@router.put("/{tid}/egress")
async def put_egress(
    tid: str,
    body: dict = Body(...),
    admin: _au.User = Depends(_au.require_admin),
) -> dict:
    """Admin-only direct edit. Body keys (all optional):
       allowed_hosts, allowed_cidrs, default_action.
    Omitted keys preserve the existing values."""
    try:
        pol = await _te.upsert_policy(
            tid,
            allowed_hosts=body.get("allowed_hosts"),
            allowed_cidrs=body.get("allowed_cidrs"),
            default_action=body.get("default_action"),
            actor=f"user:{admin.email}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"policy": _policy_dict(pol)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Approval workflow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("/me/egress/requests", status_code=201)
async def submit_my_egress_request(
    body: dict = Body(...),
    user: _au.User = Depends(_au.require_viewer),
) -> dict:
    kind = body.get("kind")
    value = body.get("value")
    justification = body.get("justification") or ""
    if not kind or not value:
        raise HTTPException(status_code=400, detail="kind and value required")
    try:
        req = await _te.submit_request(
            user.tenant_id,
            requested_by=f"user:{user.email}",
            kind=str(kind),
            value=str(value),
            justification=str(justification),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"request": _request_dict(req)}


@router.get("/me/egress/requests")
async def list_my_egress_requests(
    user: _au.User = Depends(_au.require_viewer),
    status: str | None = None,
) -> dict:
    try:
        reqs = await _te.list_requests(tenant_id=user.tenant_id, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"requests": [_request_dict(r) for r in reqs]}


@router.get("/egress/requests")
async def list_all_egress_requests(
    _admin: _au.User = Depends(_au.require_admin),
    tenant_id: str | None = None,
    status: str | None = None,
) -> dict:
    try:
        reqs = await _te.list_requests(tenant_id=tenant_id, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"requests": [_request_dict(r) for r in reqs]}


@router.post("/egress/requests/{rid}/approve")
async def approve_egress_request(
    rid: str,
    body: dict | None = Body(default=None),
    admin: _au.User = Depends(_au.require_admin),
) -> dict:
    note = (body or {}).get("note") or ""
    try:
        req, pol = await _te.approve_request(
            rid, actor=f"user:{admin.email}", note=str(note),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown request: {rid}")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"request": _request_dict(req), "policy": _policy_dict(pol)}


@router.post("/egress/requests/{rid}/reject")
async def reject_egress_request(
    rid: str,
    body: dict | None = Body(default=None),
    admin: _au.User = Depends(_au.require_admin),
) -> dict:
    note = (body or {}).get("note") or ""
    try:
        req = await _te.reject_request(
            rid, actor=f"user:{admin.email}", note=str(note),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown request: {rid}")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"request": _request_dict(req)}


@router.post("/{tid}/egress/dns-cache/reset")
async def reset_dns_cache(
    tid: str,
    _admin: _au.User = Depends(_au.require_admin),
) -> dict:
    """Force a re-resolve on the next launch — useful when a host
    rotates IPs faster than the 5-min TTL."""
    _te._reset_dns_cache_for_tests()
    pol = await _te.get_policy(tid)
    resolved = await _te.resolve_allow_targets(pol)
    return {
        "tenant_id": tid,
        "resolved": {h: ips for h, ips in resolved.items()},
    }

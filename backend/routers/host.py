"""M4 + H1 — Host-level metrics endpoints.

    GET  /api/v1/host/metrics                  → tenants + whole-host snapshot
    GET  /api/v1/host/metrics?tenant_id=<tid>  → one tenant (admin any, user self)
    GET  /api/v1/host/metrics/me               → current user's tenant
    GET  /api/v1/host/accounting               → cumulative billing (admin only)

The ``host`` field on ``/host/metrics`` carries the H1 whole-host view
(``HostSnapshot.current`` + ``HostSnapshot.history``) that the
host-device panel renders. It is computed once and attached to every
shape so the UI does not need a second round-trip: admins iterating the
tenants list and single-tenant users both see the same ``host`` block.

ACL rules:
  * Per-tenant views:
      - ``admin`` may pass any ``tenant_id`` or omit it (= all tenants).
      - ``viewer`` / ``operator`` may only ever read their *own* tenant.
        Requests with a different ``tenant_id`` get 403. Omitted
        ``tenant_id`` is silently rewritten to the user's ``tenant_id``.
  * Whole-host view (``host`` field): always present, same to every
    authenticated user — the numbers are aggregate machine counters
    (CPU / mem / disk / loadavg / running-container count) and do not
    leak any per-tenant state.
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


def _host_sample_to_dict(s: _hm.HostSample) -> dict:
    return {
        "cpu_percent": round(s.cpu_percent, 2),
        "mem_percent": round(s.mem_percent, 2),
        "mem_used_gb": round(s.mem_used_gb, 3),
        "mem_total_gb": round(s.mem_total_gb, 3),
        "disk_percent": round(s.disk_percent, 2),
        "disk_used_gb": round(s.disk_used_gb, 3),
        "disk_total_gb": round(s.disk_total_gb, 3),
        "loadavg_1m": round(s.loadavg_1m, 3),
        "loadavg_5m": round(s.loadavg_5m, 3),
        "loadavg_15m": round(s.loadavg_15m, 3),
        "sampled_at": s.sampled_at,
    }


def _docker_sample_to_dict(d: _hm.DockerSample) -> dict:
    return {
        "container_count": d.container_count,
        "total_mem_reservation_bytes": d.total_mem_reservation_bytes,
        "source": d.source,
        "sampled_at": d.sampled_at,
    }


def _snapshot_to_dict(snap: _hm.HostSnapshot) -> dict:
    return {
        "host": _host_sample_to_dict(snap.host),
        "docker": _docker_sample_to_dict(snap.docker),
        "sampled_at": snap.sampled_at,
    }


def _host_block() -> dict:
    """Assemble the H1 whole-host block: baseline + current + history.

    ``current`` is ``None`` on cold start (sampler hasn't produced a tick
    yet); ``history`` is the oldest→newest list from the ring buffer and
    is capped at ``HOST_HISTORY_SIZE`` (60 entries = 5 minutes at the
    5s cadence), so the payload fits comfortably in a single SSE frame.
    """
    latest = _hm.get_latest_host_snapshot()
    history = [_snapshot_to_dict(s) for s in _hm.get_host_history()]
    return {
        "baseline": {
            "cpu_cores": _hm.HOST_BASELINE.cpu_cores,
            "mem_total_gb": _hm.HOST_BASELINE.mem_total_gb,
            "disk_total_gb": _hm.HOST_BASELINE.disk_total_gb,
            "cpu_model": _hm.HOST_BASELINE.cpu_model,
        },
        "current": _snapshot_to_dict(latest) if latest is not None else None,
        "history": history,
        "interval_s": _hm.SAMPLE_INTERVAL_S,
        "history_size": _hm.HOST_HISTORY_SIZE,
    }


@router.get("/metrics")
async def get_host_metrics(
    tenant_id: str | None = Query(default=None),
    user: _au.User = Depends(_au.current_user),
) -> dict:
    """Return per-tenant resource usage plus the H1 whole-host block.

    Per-tenant half:
      * ``tenant_id`` omitted:
          - admin → full list of all tenants with running sandboxes
          - non-admin → auto-scoped to the caller's own tenant
      * ``tenant_id`` set:
          - admin → any tenant
          - non-admin → only their own, else 403

    Whole-host half (``host`` field): always included, same shape for
    every authenticated caller — baseline (HOST_BASELINE), the most
    recent ring-buffer snapshot (``current``), and the full history list
    (``history``, oldest first, up to ``HOST_HISTORY_SIZE`` entries).
    """
    is_admin = user.role == "admin"
    host = _host_block()
    if tenant_id is None:
        if is_admin:
            return {
                "tenants": [_usage_to_dict(u) for u in _hm.get_all_tenant_usage()],
                "host": host,
            }
        usage = _hm.get_tenant_usage(user.tenant_id)
        return {"tenant": _usage_to_dict(usage), "host": host}
    if not is_admin and tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot read another tenant's metrics")
    usage = _hm.get_tenant_usage(tenant_id)
    return {"tenant": _usage_to_dict(usage), "host": host}


@router.get("/metrics/me")
async def get_my_tenant_metrics(user: _au.User = Depends(_au.current_user)) -> dict:
    """Shortcut for the UI's "current tenant" bar — same shape as
    ``/metrics?tenant_id=<self>`` but with no query string. Also
    carries the H1 ``host`` block so the panel can render the whole
    machine view in the same request."""
    return {
        "tenant": _usage_to_dict(_hm.get_tenant_usage(user.tenant_id)),
        "host": _host_block(),
    }


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

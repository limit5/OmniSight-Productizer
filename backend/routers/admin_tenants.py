"""Y2 (#278) — Admin REST API for tenant CRUD.

Replaces the legacy "operator runs raw SQL against the tenants table"
hack workflow with a proper super-admin-gated REST surface. Currently
implemented:

  POST /api/v1/admin/tenants — create a new tenant.
  GET  /api/v1/admin/tenants — list tenants + aggregated usage metrics.

Subsequent rows (GET detail / PATCH / DELETE) extend this router;
they are out of scope here and will be added by their own TODO rows.

Auth model (Y2 spec)
────────────────────
Every endpoint below is gated by ``auth.require_super_admin``. Tenant
admins (``role='admin'``) get 403; only platform-tier ``super_admin``
users may mutate the tenants table. The ``super_admin`` role is added
to ``auth.ROLES`` in this commit; Y3 (#279) will land the user-facing
POST /admin/super-admins bootstrap. Until Y3, super_admin can be
acquired only by:

  * direct DB row update by an operator who already has shell access
    (the same trust boundary the legacy hack already crossed), OR
  * the ``OMNISIGHT_AUTH_MODE=open`` dev fallback (synthetic
    anonymous user, see ``auth._ANON_ADMIN``).

Tenant ID format
────────────────
The id pattern ``^t-[a-z0-9][a-z0-9-]{2,62}$`` enforces:

  * mandatory ``t-`` prefix (matches the seeded ``t-default``)
  * leading char is a-z or 0-9 (no leading hyphen → no double-dash
    URLs, no ambiguity with shell flags)
  * 2-62 trailing chars from [a-z0-9-]
  * total max 65 chars — comfortably below DNS label / filesystem /
    PG identifier limits

The validator is enforced at the Pydantic layer so malformed ids
return 422 *before* reaching the DB.

Module-global state
───────────────────
None introduced. All writes go through the asyncpg pool
(``backend.db_pool.get_pool()``) which is shared across uvicorn
workers via PG. Audit chain integrity is preserved by the existing
``pg_advisory_xact_lock`` inside ``audit._log_impl``. No new
process-local caches.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.tenant_quota import PLAN_DISK_QUOTAS, measure_tenant_usage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-tenants"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Y2 spec: ``^t-[a-z0-9][a-z0-9-]{2,62}$``. ``t-default`` (13 chars
# inside the bracket class after the ``t-``) matches because ``d``
# is in [a-z0-9] and ``efault`` (6 chars) is in {2,62}.
TENANT_ID_PATTERN = r"^t-[a-z0-9][a-z0-9-]{2,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)

VALID_PLANS = tuple(PLAN_DISK_QUOTAS.keys())  # ("free","starter","pro","enterprise")


def _is_valid_tenant_id(tid: str) -> bool:
    return bool(tid) and bool(_TENANT_ID_RE.match(tid))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CreateTenantRequest(BaseModel):
    id: str = Field(
        pattern=TENANT_ID_PATTERN,
        min_length=5,   # "t-" + 1 leading char + 2 trailing chars
        max_length=65,  # "t-" + 1 leading char + 62 trailing chars
        description="Tenant id; must match ^t-[a-z0-9][a-z0-9-]{2,62}$",
    )
    name: str = Field(min_length=1, max_length=200)
    plan: Literal["free", "starter", "pro", "enterprise"] = "free"
    enabled: bool = True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/tenants", status_code=201)
async def create_tenant(
    req: CreateTenantRequest,
    _request: Request,
    actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Create a new tenant.

    Returns 201 with the created tenant row on success, 409 if the id
    already exists (including the seeded ``t-default``), 422 if the
    body is malformed (handled by FastAPI/Pydantic before this body
    runs), and 403 if the caller is not a super-admin.
    """
    # Defensive belt-and-braces — Pydantic should already have rejected
    # this, but guard against any future regex drift in the schema.
    if not _is_valid_tenant_id(req.id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {req.id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if req.plan not in VALID_PLANS:
        return JSONResponse(
            status_code=422,
            content={"detail": f"unknown plan: {req.plan!r}; "
                               f"must be one of {list(VALID_PLANS)}"},
        )

    from backend.db_pool import get_pool
    enabled_int = 1 if req.enabled else 0

    # ON CONFLICT DO NOTHING + RETURNING gives us atomic
    # "insert-or-detect-duplicate" semantics: the RETURNING row is
    # only present when the INSERT actually wrote, so a None result
    # unambiguously means "id already taken". This sidesteps the
    # classic INSERT/SELECT TOCTOU race that would otherwise let two
    # concurrent super-admins both think they created the same id.
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (id) DO NOTHING "
            "RETURNING id, name, plan, enabled, created_at",
            req.id, req.name, req.plan, enabled_int,
        )

    if row is None:
        return JSONResponse(
            status_code=409,
            content={"detail": f"tenant id already exists: {req.id!r}"},
        )

    # Best-effort audit. ``audit.log`` swallows its own failures so the
    # request still succeeds even if the chain is briefly unavailable —
    # the row is in the DB regardless.
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_created",
            entity_kind="tenant",
            entity_id=row["id"],
            before=None,
            after={
                "id": row["id"],
                "name": row["name"],
                "plan": row["plan"],
                "enabled": bool(row["enabled"]),
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant_created audit emit failed: %s", exc)

    return JSONResponse(
        status_code=201,
        content={
            "id": row["id"],
            "name": row["name"],
            "plan": row["plan"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /admin/tenants — list + aggregated usage  (Y2 #278 row 2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Single-pass aggregated query. ``users`` and ``projects`` JOIN counts
# are scoped to active rows only (``users.enabled = 1`` and
# ``projects.archived_at IS NULL``) — disabled / archived rows would
# otherwise inflate the operator-facing usage view. ``last_activity_at``
# is taken from the most recent ``audit_log.ts`` for the tenant; this
# is the single most reliable cross-feature activity signal because
# every mutation in the codebase fans out through ``audit.log``.
# ``llm_tokens_30d`` reads from the ``event_log`` ``turn.complete``
# stream which carries ``tokens_used`` in its ``data_json`` JSONB
# payload (mirrors the existing /runtime/tokens/burn-rate query in
# routers/system.py).
#
# Why subqueries instead of LEFT JOIN + GROUP BY for tokens / activity:
# the JOIN-then-GROUP approach over event_log fans the row count out
# by N_events_per_tenant which on a busy tenant can be 100k+ rows
# before grouping. Scalar subqueries collapse the same work tenant-
# side, letting PG use the (event_type, tenant_id) and (tenant_id, ts)
# composite indexes directly.
_LIST_TENANTS_SQL = """
SELECT
    t.id,
    t.name,
    t.plan,
    t.enabled,
    t.created_at,
    COALESCE(uc.user_count, 0)       AS user_count,
    COALESCE(pc.project_count, 0)    AS project_count,
    COALESCE(tk.tokens_30d, 0)       AS llm_tokens_30d,
    la.last_activity_at              AS last_activity_at
FROM tenants t
LEFT JOIN (
    SELECT tenant_id, COUNT(*) AS user_count
    FROM users
    WHERE enabled = 1
    GROUP BY tenant_id
) uc ON uc.tenant_id = t.id
LEFT JOIN (
    SELECT tenant_id, COUNT(*) AS project_count
    FROM projects
    WHERE archived_at IS NULL
    GROUP BY tenant_id
) pc ON pc.tenant_id = t.id
LEFT JOIN (
    SELECT
        tenant_id,
        SUM(COALESCE((data_json::jsonb->>'tokens_used')::bigint, 0))
            AS tokens_30d
    FROM event_log
    WHERE event_type = 'turn.complete'
      AND to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS')
            >= NOW() - INTERVAL '30 days'
    GROUP BY tenant_id
) tk ON tk.tenant_id = t.id
LEFT JOIN (
    SELECT tenant_id, MAX(ts) AS last_activity_at
    FROM audit_log
    GROUP BY tenant_id
) la ON la.tenant_id = t.id
ORDER BY t.created_at ASC, t.id ASC
"""


def _measure_disk_safely(tenant_id: str) -> int:
    """Wrap ``measure_tenant_usage`` so a missing tenant data dir
    (common in fresh prod / CI) maps to 0 instead of raising. The
    helper itself already tolerates missing dirs; this layer also
    swallows OS errors (permission, transient ENOENT) so one bad
    tenant can't poison the whole list response."""
    try:
        return int(measure_tenant_usage(tenant_id).get("total_bytes", 0))
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "disk usage measurement failed for tenant=%s: %s",
            tenant_id, exc,
        )
        return 0


@router.get("/tenants")
async def list_tenants(
    _request: Request,
    _actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """List every tenant with aggregated usage metrics.

    Per-row payload::

        {
            "id": "t-acme",
            "name": "Acme Corp",
            "plan": "pro",
            "enabled": true,
            "created_at": "2026-01-15 12:34:56",
            "usage": {
                "user_count": 7,
                "project_count": 3,
                "disk_used_bytes": 1234567,
                "llm_tokens_30d": 4500000,
                "rate_limit_hits_7d": 0,
                "last_activity_at": 1745580000.0
            }
        }

    Returned envelope is ``{"tenants": [...]}`` so future fields
    (pagination cursor, server timestamp) can be added without
    breaking clients.

    Metric notes
    ────────────
    * ``user_count`` — only enabled users.
    * ``project_count`` — only non-archived projects.
    * ``disk_used_bytes`` — filesystem walk over
      ``data/tenants/<id>/{artifacts,workflow_runs,backups,ingest_tmp}``
      via ``backend.tenant_quota.measure_tenant_usage``. The measurement
      is *not* cached: the operator-facing view should be live.
    * ``llm_tokens_30d`` — ``SUM(data_json->>'tokens_used')`` from
      ``event_log`` ``turn.complete`` rows in the last 30 days.
    * ``rate_limit_hits_7d`` — currently 0; the rate-limiter is
      Redis / in-memory only and we don't yet persist hit events.
      Reserved here as an explicit field so the contract is stable
      once persistent rate-limit logging lands.
    * ``last_activity_at`` — UNIX timestamp of the most recent
      ``audit_log`` row for this tenant (``ts`` column, REAL). NULL
      for tenants that have never recorded an audit row.
    """
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(_LIST_TENANTS_SQL)

    # Disk measurement is filesystem I/O; offload to a thread so we
    # don't stall the event loop when listing many tenants. The walk
    # is deliberately not parallelised across tenants — they typically
    # share one block device, so concurrent ``du`` only thrashes the
    # disk without actually reducing wall time.
    def _measure_all(ids: list[str]) -> dict[str, int]:
        return {tid: _measure_disk_safely(tid) for tid in ids}

    tenant_ids = [r["id"] for r in rows]
    disk_by_tenant = await asyncio.to_thread(_measure_all, tenant_ids)

    tenants: list[dict[str, Any]] = []
    for r in rows:
        tid = r["id"]
        tenants.append({
            "id": tid,
            "name": r["name"],
            "plan": r["plan"],
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
            "usage": {
                "user_count": int(r["user_count"]),
                "project_count": int(r["project_count"]),
                "disk_used_bytes": disk_by_tenant.get(tid, 0),
                "llm_tokens_30d": int(r["llm_tokens_30d"]),
                # No persistent rate-limit log yet — explicit zero
                # rather than null keeps the field shape stable.
                "rate_limit_hits_7d": 0,
                "last_activity_at": (
                    float(r["last_activity_at"])
                    if r["last_activity_at"] is not None
                    else None
                ),
            },
        })

    return JSONResponse(
        status_code=200,
        content={"tenants": tenants},
    )

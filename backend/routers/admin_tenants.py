"""Y2 (#278) — Admin REST API for tenant CRUD.

Replaces the legacy "operator runs raw SQL against the tenants table"
hack workflow with a proper super-admin-gated REST surface. Currently
implemented:

  POST   /api/v1/admin/tenants       — create a new tenant.
  GET    /api/v1/admin/tenants       — list tenants + aggregated usage.
  GET    /api/v1/admin/tenants/{id}  — single-tenant detail (plan, quota
                                       usage, members, projects, recent
                                       audit events).
  PATCH  /api/v1/admin/tenants/{id}  — partial update (rename, change
                                       plan, enable / disable). Plan
                                       downgrade is refused with 409 if
                                       the tenant's current disk usage
                                       would exceed the new plan's hard
                                       quota — never silently force-
                                       deletes data.
  DELETE /api/v1/admin/tenants/{id}  — cascade delete every row /
                                       artifact owned by the tenant.
                                       Requires ``?confirm=<tenant_id>``
                                       second-handshake; ``t-default``
                                       is protected. Runs in the
                                       background and emits SSE
                                       ``tenant_delete_progress`` per
                                       phase.

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
import shutil
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.tenant_quota import (
    PLAN_DISK_QUOTAS,
    load_quota,
    measure_tenant_usage,
)

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


class PatchTenantRequest(BaseModel):
    """Partial update: every field optional, but at least one required.

    PATCH semantics: ``None`` means "leave this column alone", a present
    value (incl. ``False`` for ``enabled``) means "set the column to
    this". Empty body / all-None body is a 422 — operator probably
    meant something else and an empty UPDATE wastes an audit row.
    """
    name: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="New display name; omit to keep current.",
    )
    plan: Literal["free", "starter", "pro", "enterprise"] | None = Field(
        default=None,
        description="New plan tier; omit to keep current. A downgrade is "
                    "refused (409) if current disk usage exceeds the new "
                    "plan's hard quota.",
    )
    enabled: bool | None = Field(
        default=None,
        description="True → enable, False → disable, omit → keep current.",
    )

    def has_any_field(self) -> bool:
        return any(
            v is not None
            for v in (self.name, self.plan, self.enabled)
        )


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

    # Y9 #285 row 1 — canonical dot-notation event. Fires alongside the
    # legacy ``tenant_created`` row for backward-compat with existing
    # readers; new readers (T-series billing aggregator, Y9 audit
    # query surface) key on ``tenant.created``.
    try:
        from backend import audit_events as _audit_events
        await _audit_events.emit_tenant_created(
            tenant_id=row["id"],
            name=row["name"],
            plan=row["plan"],
            enabled=bool(row["enabled"]),
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant.created audit emit failed: %s", exc)

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /admin/tenants/{tenant_id} — single-tenant detail (Y2 #278 row 3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Single-row tenant fetch. Same canonical column projection as
# ``_LIST_TENANTS_SQL`` but parameterised on ``id`` so PG can use the
# primary-key index directly. Exists as a separate constant (rather
# than reusing the LIST SQL with a WHERE filter) because the four
# scalar subqueries in the LIST query are correlated *over the entire
# tenants table* — adding a top-level WHERE doesn't push that filter
# into the subqueries the way you'd want, so PG ends up scanning every
# tenant's audit_log / event_log range only to throw N-1 of them away.
# A direct one-row variant keeps the detail endpoint cheap.
_GET_TENANT_SQL = """
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
    SELECT COUNT(*) AS user_count
    FROM users
    WHERE enabled = 1 AND tenant_id = $1
) uc ON TRUE
LEFT JOIN (
    SELECT COUNT(*) AS project_count
    FROM projects
    WHERE archived_at IS NULL AND tenant_id = $1
) pc ON TRUE
LEFT JOIN (
    SELECT
        SUM(COALESCE((data_json::jsonb->>'tokens_used')::bigint, 0))
            AS tokens_30d
    FROM event_log
    WHERE event_type = 'turn.complete'
      AND tenant_id = $1
      AND to_timestamp(created_at, 'YYYY-MM-DD HH24:MI:SS')
            >= NOW() - INTERVAL '30 days'
) tk ON TRUE
LEFT JOIN (
    SELECT MAX(ts) AS last_activity_at
    FROM audit_log
    WHERE tenant_id = $1
) la ON TRUE
WHERE t.id = $1
"""

# Members listing — joins ``user_tenant_memberships`` (the Y1 N-to-M
# authoritative source) with the ``users`` row so the operator sees
# email / name / role-on-this-tenant in one shot. Excludes disabled
# users to mirror the list-endpoint user_count semantics, but we
# DO surface the user's enabled flag so the operator can audit who
# is suspended without paging back to the user-management endpoint.
# ``role`` from the membership row (per-tenant role) takes precedence
# over the cached ``users.role`` (account-tier role).
_LIST_MEMBERS_SQL = """
SELECT
    u.id              AS user_id,
    u.email           AS email,
    u.name            AS name,
    m.role            AS role,
    m.status          AS status,
    u.enabled         AS user_enabled,
    m.created_at      AS joined_at,
    m.last_active_at  AS last_active_at
FROM user_tenant_memberships m
JOIN users u ON u.id = m.user_id
WHERE m.tenant_id = $1
ORDER BY m.created_at ASC, u.email ASC
"""

# Projects listing — every non-archived project under the tenant plus
# its slug / product_line composite, archived_at flag, and an indication
# of whether the project overrides the tenant plan. ``parent_id`` is
# included so the operator can tell sub-project tree shape at a glance.
_LIST_PROJECTS_SQL = """
SELECT
    p.id              AS id,
    p.name            AS name,
    p.slug            AS slug,
    p.product_line    AS product_line,
    p.parent_id       AS parent_id,
    p.plan_override   AS plan_override,
    p.disk_budget_bytes AS disk_budget_bytes,
    p.llm_budget_tokens AS llm_budget_tokens,
    p.created_at      AS created_at,
    p.archived_at     AS archived_at
FROM projects p
WHERE p.tenant_id = $1
ORDER BY
    CASE WHEN p.archived_at IS NULL THEN 0 ELSE 1 END,
    p.created_at ASC,
    p.id ASC
"""

# Recent audit events. The audit_log row is denormalised already
# (actor / entity_kind / entity_id / before_json / after_json) so the
# operator-facing detail page can render the event timeline directly
# without joining out. ``before_json`` / ``after_json`` are TEXT JSON
# blobs; we hand them back as raw strings — clients that want to
# inspect them do their own JSON.parse. (Pre-parsing here would force
# us to handle malformed legacy rows, and the chain integrity test
# already enforces well-formed JSON for new writes.)
_LIST_AUDIT_EVENTS_SQL = """
SELECT
    id,
    ts,
    actor,
    action,
    entity_kind,
    entity_id,
    before_json,
    after_json
FROM audit_log
WHERE tenant_id = $1
ORDER BY ts DESC, id DESC
LIMIT $2
"""

# Cap returned audit events. The detail endpoint is operator-facing
# UI; 50 rows is enough for "recent" without paging. A future row
# can add ``?cursor=`` for full pagination — keep the field stable
# now to avoid breaking the client when that lands.
_AUDIT_EVENT_LIMIT = 50


@router.get("/tenants/{tenant_id}")
async def get_tenant_detail(
    tenant_id: str,
    _request: Request,
    _actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Return the per-tenant detail panel.

    Payload::

        {
            "id": "t-acme",
            "name": "Acme Corp",
            "plan": "pro",
            "enabled": true,
            "created_at": "2026-01-15 12:34:56",
            "quota": {
                "soft_bytes": 107374182400,
                "hard_bytes": 214748364800,
                "keep_recent_runs": 20
            },
            "usage": {
                "user_count": 7,
                "project_count": 3,
                "disk_used_bytes": 1234567,
                "disk_used_pct_of_hard": 0.0057,
                "llm_tokens_30d": 4500000,
                "rate_limit_hits_7d": 0,
                "last_activity_at": 1745580000.0
            },
            "members": [
                {
                    "user_id": "u-...",
                    "email": "alice@acme.example",
                    "name": "Alice",
                    "role": "owner",
                    "status": "active",
                    "user_enabled": true,
                    "joined_at": "2026-02-01 09:00:00",
                    "last_active_at": null
                },
                ...
            ],
            "projects": [
                {
                    "id": "p-acme-default",
                    "name": "Default",
                    "slug": "default",
                    "product_line": "default",
                    "parent_id": null,
                    "plan_override": null,
                    "disk_budget_bytes": null,
                    "llm_budget_tokens": null,
                    "created_at": "2026-02-01 09:00:00",
                    "archived_at": null
                },
                ...
            ],
            "recent_audit_events": [
                {
                    "id": 9876,
                    "ts": 1745580000.0,
                    "actor": "alice@acme.example",
                    "action": "tenant_updated",
                    "entity_kind": "tenant",
                    "entity_id": "t-acme",
                    "before_json": "{...}",
                    "after_json": "{...}"
                },
                ...
            ]
        }

    Errors
    ──────
    * 404 — tenant id does not exist
    * 422 — id fails ``TENANT_ID_PATTERN`` (validated *before* DB hit
      to avoid leaking ill-formed values into the query)
    * 403 — caller is not a super-admin (handled by dependency)

    Recent audit events are capped at ``_AUDIT_EVENT_LIMIT`` rows,
    newest first. ``before_json`` / ``after_json`` are returned as
    raw JSON strings — clients that need structured access call
    ``JSON.parse`` themselves.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_GET_TENANT_SQL, tenant_id)
        if tenant_row is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"tenant not found: {tenant_id!r}"},
            )
        member_rows = await conn.fetch(_LIST_MEMBERS_SQL, tenant_id)
        project_rows = await conn.fetch(_LIST_PROJECTS_SQL, tenant_id)
        audit_rows = await conn.fetch(
            _LIST_AUDIT_EVENTS_SQL, tenant_id, _AUDIT_EVENT_LIMIT,
        )

    # Disk measurement on a single tenant — no parallelism needed, but
    # still offload to a thread to keep the event loop responsive on
    # tenants with very large data dirs.
    disk_used = await asyncio.to_thread(_measure_disk_safely, tenant_id)

    # Quota is plan-derived (or yaml-overridden); we surface both the
    # absolute bytes and a ratio-of-hard-quota so the UI doesn't have
    # to compute it client-side. The yaml fallback path inside
    # ``load_quota`` does file I/O — same ``to_thread`` reasoning as
    # the disk measurement above.
    quota = await asyncio.to_thread(load_quota, tenant_id, tenant_row["plan"])
    pct_of_hard = (
        (disk_used / quota.hard_bytes) if quota.hard_bytes > 0 else 0.0
    )

    members = [
        {
            "user_id": r["user_id"],
            "email": r["email"],
            "name": r["name"],
            "role": r["role"],
            "status": r["status"],
            "user_enabled": bool(r["user_enabled"]),
            "joined_at": r["joined_at"],
            "last_active_at": r["last_active_at"],
        }
        for r in member_rows
    ]

    projects = [
        {
            "id": r["id"],
            "name": r["name"],
            "slug": r["slug"],
            "product_line": r["product_line"],
            "parent_id": r["parent_id"],
            "plan_override": r["plan_override"],
            "disk_budget_bytes": (
                int(r["disk_budget_bytes"])
                if r["disk_budget_bytes"] is not None else None
            ),
            "llm_budget_tokens": (
                int(r["llm_budget_tokens"])
                if r["llm_budget_tokens"] is not None else None
            ),
            "created_at": r["created_at"],
            "archived_at": r["archived_at"],
        }
        for r in project_rows
    ]

    recent_audit_events = [
        {
            "id": int(r["id"]),
            "ts": float(r["ts"]) if r["ts"] is not None else None,
            "actor": r["actor"],
            "action": r["action"],
            "entity_kind": r["entity_kind"],
            "entity_id": r["entity_id"],
            "before_json": r["before_json"],
            "after_json": r["after_json"],
        }
        for r in audit_rows
    ]

    return JSONResponse(
        status_code=200,
        content={
            "id": tenant_row["id"],
            "name": tenant_row["name"],
            "plan": tenant_row["plan"],
            "enabled": bool(tenant_row["enabled"]),
            "created_at": tenant_row["created_at"],
            "quota": {
                "soft_bytes": int(quota.soft_bytes),
                "hard_bytes": int(quota.hard_bytes),
                "keep_recent_runs": int(quota.keep_recent_runs),
            },
            "usage": {
                "user_count": int(tenant_row["user_count"]),
                "project_count": int(tenant_row["project_count"]),
                "disk_used_bytes": int(disk_used),
                "disk_used_pct_of_hard": float(pct_of_hard),
                "llm_tokens_30d": int(tenant_row["llm_tokens_30d"]),
                # No persistent rate-limit log yet — explicit zero
                # rather than null keeps the field shape stable.
                "rate_limit_hits_7d": 0,
                "last_activity_at": (
                    float(tenant_row["last_activity_at"])
                    if tenant_row["last_activity_at"] is not None
                    else None
                ),
            },
            "members": members,
            "projects": projects,
            "recent_audit_events": recent_audit_events,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATCH /admin/tenants/{tenant_id} — rename / change plan / toggle
#  enabled  (Y2 #278 row 4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Read the current row before mutating so we can:
#   (a) decide whether the plan field is actually changing (skip the
#       expensive disk-usage walk if it isn't), and
#   (b) hand the audit log a faithful ``before`` snapshot.
# Not wrapped in FOR UPDATE: two concurrent super-admins racing on the
# same tenant is benign — last-writer-wins, both events land in audit
# in commit order, and the disk-quota check protects each writer from
# the only outcome we actually care about (data loss from forced
# eviction).
_FETCH_TENANT_FOR_PATCH_SQL = """
SELECT id, name, plan, enabled, created_at
FROM tenants
WHERE id = $1
"""

# Single-statement partial UPDATE. ``COALESCE($N, col)`` keeps the
# existing column value when the parameter is NULL — i.e. "field
# omitted from the PATCH body". Crucially this also lets us pass
# ``enabled`` as ``None`` for "no change" while still distinguishing
# from ``0`` ("disable"): None → COALESCE keeps current, 0 → write 0.
# RETURNING gives us the post-update row in one round-trip so the
# response body and the audit ``after`` payload share a single source
# of truth.
_PATCH_TENANT_SQL = """
UPDATE tenants
SET name    = COALESCE($2, name),
    plan    = COALESCE($3, plan),
    enabled = COALESCE($4, enabled)
WHERE id = $1
RETURNING id, name, plan, enabled, created_at
"""


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(
    tenant_id: str,
    body: PatchTenantRequest,
    _request: Request,
    actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Partial-update a tenant.

    Body accepts any subset of ``{name, plan, enabled}``; at least one
    field must be present. Returns 200 with the updated tenant row::

        {
            "id": "t-acme",
            "name": "Acme Corp (renamed)",
            "plan": "starter",
            "enabled": false,
            "created_at": "2026-01-15 12:34:56"
        }

    Status codes
    ────────────
    * 200 — applied successfully.
    * 403 — caller is not a super-admin (handled by dependency).
    * 404 — well-formed id but no such tenant.
    * 409 — plan downgrade refused because current ``disk_used_bytes``
      exceeds the new plan's ``hard_bytes``. The response includes
      ``current_plan`` / ``requested_plan`` / ``disk_used_bytes`` /
      ``new_hard_bytes`` so the operator (or UI) can render the gap
      directly. **No data is force-deleted** — the spec is explicit
      that downgrading must never silently reclaim storage; the
      operator must run an LRU sweep, mark-keep, or pick a higher
      plan.
    * 422 — id fails ``TENANT_ID_PATTERN``, body has no settable
      field, or any field violates its Pydantic constraints.

    Plan-downgrade quota guard
    ──────────────────────────
    If ``plan`` is in the body AND it differs from the current plan,
    the handler measures live disk usage (filesystem walk; same
    helper as the LIST / GET handlers) and compares it against the
    *new* plan's default ``hard_bytes`` from ``PLAN_DISK_QUOTAS``.
    The yaml override file (``data/tenants/<id>/quota.yaml``) is
    intentionally not consulted here — it represents the *current*
    operator-granted budget and a plan change implies that override
    will be re-materialised by the next sweep with the new plan's
    defaults. Comparing against the override would let an over-
    provisioned tenant sneak through a downgrade that would then
    immediately violate its own plan.

    Module-global state
    ───────────────────
    None introduced. SQL constants are module-level immutable
    strings (each worker derives the same value); the asyncpg pool
    is shared via PG; the audit chain serialises through
    ``pg_advisory_xact_lock`` inside ``audit._log_impl``.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if not body.has_any_field():
        return JSONResponse(
            status_code=422,
            content={"detail": "PATCH body must include at least one of "
                               "'name', 'plan', or 'enabled'."},
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        cur_row = await conn.fetchrow(
            _FETCH_TENANT_FOR_PATCH_SQL, tenant_id,
        )
        if cur_row is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"tenant not found: {tenant_id!r}"},
            )

        # Plan-change quota guard. Run *before* the UPDATE so a doomed
        # downgrade leaves the row untouched (no half-applied state to
        # roll back). We only walk the filesystem when the plan field
        # is actually changing — a no-op plan PATCH (e.g. rename only,
        # or rename + plan=current_plan) skips the I/O.
        if body.plan is not None and body.plan != cur_row["plan"]:
            new_quota = PLAN_DISK_QUOTAS[body.plan]
            disk_used = await asyncio.to_thread(
                _measure_disk_safely, tenant_id,
            )
            if disk_used > new_quota.hard_bytes:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            f"plan change refused: tenant {tenant_id!r} "
                            f"is currently using {disk_used} bytes "
                            f"which exceeds the requested plan "
                            f"{body.plan!r} hard quota of "
                            f"{new_quota.hard_bytes} bytes. Free up "
                            f"storage or pick a higher plan; this "
                            f"endpoint never force-deletes tenant data."
                        ),
                        "tenant_id": tenant_id,
                        "current_plan": cur_row["plan"],
                        "requested_plan": body.plan,
                        "disk_used_bytes": int(disk_used),
                        "new_hard_bytes": int(new_quota.hard_bytes),
                    },
                )

        # ``enabled`` is stored as INTEGER (0/1). Translate the tri-state
        # (None / True / False) → (None / 1 / 0) so the UPDATE COALESCE
        # can distinguish "leave alone" from "set to false".
        enabled_int = (
            None if body.enabled is None
            else (1 if body.enabled else 0)
        )

        new_row = await conn.fetchrow(
            _PATCH_TENANT_SQL,
            tenant_id, body.name, body.plan, enabled_int,
        )

    if new_row is None:
        # Race: tenant was deleted between the read and the UPDATE.
        # 404 keeps the contract honest — the resource does not exist
        # at the moment the caller wanted it patched.
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    before = {
        "id": cur_row["id"],
        "name": cur_row["name"],
        "plan": cur_row["plan"],
        "enabled": bool(cur_row["enabled"]),
    }
    after = {
        "id": new_row["id"],
        "name": new_row["name"],
        "plan": new_row["plan"],
        "enabled": bool(new_row["enabled"]),
    }
    # Best-effort audit; ``audit.log`` swallows its own failures so the
    # caller still sees the successful 200 even if the chain is briefly
    # unavailable — the row is in the DB regardless.
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_updated",
            entity_kind="tenant",
            entity_id=new_row["id"],
            before=before,
            after=after,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant_updated audit emit failed: %s", exc)

    # Y9 #285 row 1 — derived dot-notation events. A single PATCH may
    # change plan AND/OR enabled; fan out to one canonical event per
    # field-level transition. Re-enabling does NOT fire ``tenant.disabled``
    # (only the disable transition is event-worthy; re-enable is covered
    # by the legacy ``tenant_updated`` row alongside).
    try:
        from backend import audit_events as _audit_events
        if before["plan"] != after["plan"]:
            await _audit_events.emit_tenant_plan_changed(
                tenant_id=new_row["id"],
                old_plan=before["plan"],
                new_plan=after["plan"],
                actor=actor.email,
            )
        if before["enabled"] is True and after["enabled"] is False:
            await _audit_events.emit_tenant_disabled(
                tenant_id=new_row["id"],
                actor=actor.email,
            )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant.plan_changed/disabled audit emit failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "id": new_row["id"],
            "name": new_row["name"],
            "plan": new_row["plan"],
            "enabled": bool(new_row["enabled"]),
            "created_at": new_row["created_at"],
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DELETE /admin/tenants/{tenant_id} — cascade delete  (Y2 #278 row 5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenants that the platform itself depends on and which must NEVER be
# deleted, even by a super-admin. ``t-default`` is the seed row that
# every default-tenant write path falls back to (db.py seeds it on
# init); removing it would break every subsequent INSERT that omits
# ``tenant_id`` and rely on the column DEFAULT, plus the artifact /
# audit / event_log columns that point at it. Treated as a hard policy
# — returns 403 (not 404) so the operator can see exactly why.
PROTECTED_TENANT_IDS: frozenset[str] = frozenset({"t-default"})

# Single-row fetch used by the DELETE handler to (a) decide between
# 404 and 202, and (b) snapshot the tenant for the audit log before
# the row is gone. Read-only.
_FETCH_TENANT_FOR_DELETE_SQL = """
SELECT id, name, plan, enabled, created_at
FROM tenants
WHERE id = $1
"""

# Cascade DELETE phases, in dependency-safe order: rows in tables that
# REFERENCE ``tenants(id)`` *without* ``ON DELETE CASCADE`` must be
# removed before the tenants row itself, otherwise the final delete
# trips an FK violation. The list mirrors every ``REFERENCES tenants``
# call site that lacks ``ON DELETE CASCADE`` in ``backend/db.py`` plus
# the three FK-less ``tenant_id`` columns (chat_messages, chat_sessions,
# user_drafts) — when one of these tables is added in the future the
# drift guard test ``test_delete_phases_cover_all_tenant_id_tables``
# will fail at module-load until the new table is appended here.
#
# Tables that already have ``ON DELETE CASCADE`` on their tenants FK
# (``user_tenant_memberships`` / ``projects`` / ``tenant_invites`` /
# ``project_shares.guest_tenant_id`` / ``git_accounts`` /
# ``llm_credentials``) and the user-scoped child rows that hang off
# ``users`` (``sessions`` / ``user_mfa`` / ``mfa_backup_codes`` /
# ``password_history`` — all CASCADE on ``users.id``) fall away on
# their own when the ``users`` and final ``tenants`` deletes fire.
_DELETE_PHASES_PG: tuple[tuple[str, str], ...] = (
    ("artifacts",
        "DELETE FROM artifacts WHERE tenant_id = $1"),
    ("event_log",
        "DELETE FROM event_log WHERE tenant_id = $1"),
    ("debug_findings",
        "DELETE FROM debug_findings WHERE tenant_id = $1"),
    ("decision_rules",
        "DELETE FROM decision_rules WHERE tenant_id = $1"),
    ("workflow_runs",
        "DELETE FROM workflow_runs WHERE tenant_id = $1"),
    ("user_preferences",
        "DELETE FROM user_preferences WHERE tenant_id = $1"),
    ("tenant_secrets",
        "DELETE FROM tenant_secrets WHERE tenant_id = $1"),
    ("tenant_egress_requests",
        "DELETE FROM tenant_egress_requests WHERE tenant_id = $1"),
    ("tenant_egress_policies",
        "DELETE FROM tenant_egress_policies WHERE tenant_id = $1"),
    ("chat_messages",
        "DELETE FROM chat_messages WHERE tenant_id = $1"),
    ("chat_sessions",
        "DELETE FROM chat_sessions WHERE tenant_id = $1"),
    ("user_drafts",
        "DELETE FROM user_drafts WHERE tenant_id = $1"),
    # audit_log is the deleted tenant's audit chain. The super-admin's
    # chain (typically ``t-default``) is untouched because audit row
    # ``tenant_id`` is set from the request context, not from the
    # entity being acted upon (see ``audit._log_impl``).
    ("audit_log",
        "DELETE FROM audit_log WHERE tenant_id = $1"),
    # ``users`` last among the explicit deletes so the user-scoped
    # CASCADE children (sessions, mfa, password_history) are still in
    # place when audit_log rows referencing them are removed above.
    ("users",
        "DELETE FROM users WHERE tenant_id = $1"),
    # Final row removal. PG cascades fan out to user_tenant_memberships,
    # projects (+ project_members + project_shares), tenant_invites,
    # project_shares.guest_tenant_id, git_accounts, llm_credentials.
    ("tenants",
        "DELETE FROM tenants WHERE id = $1"),
)

# Filesystem cleanup is the (N+1)-th phase — emitted after the SQL
# DELETEs so subscribers see a single linear stream of phases ending
# with ``filesystem``.
DELETE_TOTAL_PHASES = len(_DELETE_PHASES_PG) + 1

# SSE event type used for both per-phase progress and terminal
# done / failed events. A single event type with a ``status`` field
# (rather than three event types) keeps SSE subscribers simple.
DELETE_PROGRESS_EVENT = "tenant_delete_progress"

# Public phase-name list — used by the frontend to render a progress
# bar with stable labels and by the drift-guard tests to assert phase
# coverage. Defined once at module scope so both the response payload
# and the test surface read the same source of truth.
DELETE_PHASE_NAMES: tuple[str, ...] = tuple(
    name for name, _sql in _DELETE_PHASES_PG
) + ("filesystem",)

# Strong references to in-flight cascade tasks. ``asyncio.create_task``
# only weakly references its task — without this set, a pending task
# can be garbage-collected mid-flight, silently aborting the cascade.
# Tests use this set to ``await`` the bg task before asserting
# post-state. Module-global state #3 answer ("intentionally per-worker"):
# each uvicorn worker owns its own in-flight tasks, exactly the same
# scope as the asyncio event loop running them.
_pending_delete_tasks: set[asyncio.Task] = set()


def _emit_delete_progress(
    tenant_id: str,
    phase: str,
    status: str,
    **extra: Any,
) -> None:
    """Push one ``tenant_delete_progress`` SSE event to all subscribers.

    Status values: ``started`` / ``running`` / ``done`` / ``completed``
    / ``failed``. ``broadcast_scope='global'`` because the event is for
    super-admins watching the global admin pane — it must NOT be scoped
    to the tenant being deleted (subscribers of that tenant's stream
    are about to vanish).
    """
    try:
        from backend.events import bus
        bus.publish(
            DELETE_PROGRESS_EVENT,
            {
                "tenant_id": tenant_id,
                "phase": phase,
                "status": status,
                **extra,
            },
            broadcast_scope="global",
            tenant_id=None,
        )
    except Exception as exc:  # pragma: no cover — best-effort SSE
        logger.warning(
            "tenant_delete_progress emit failed (tenant=%s phase=%s): %s",
            tenant_id, phase, exc,
        )


def _delete_tenant_filesystem_sync(tenant_id: str) -> int:
    """Remove the tenant's on-disk data dirs synchronously. Returns the
    pre-delete byte total (best-effort; 0 on measurement / removal
    failure of any subdir). Runs inside ``asyncio.to_thread``.

    Two roots are removed:
      * ``data/tenants/<tenant_id>/`` — artifacts / workflow_runs /
        backups / quota.yaml
      * ``/tmp/omnisight_ingest/<tenant_id>/`` — staged ingest payloads

    ``ignore_errors=True`` on rmtree because partial cleanup is better
    than an exception that aborts the cascade — orphaned bytes are
    cosmetic, an aborted cascade leaves the operator with half-deleted
    DB state. Any per-tree failure is logged at warning so an operator
    can manually finish the sweep.
    """
    from backend.tenant_fs import tenant_data_root, tenant_ingest_root
    bytes_freed = 0
    for path_fn in (tenant_data_root, tenant_ingest_root):
        try:
            p = path_fn(tenant_id)
            if not p.exists():
                continue
            try:
                bytes_freed += sum(
                    f.stat().st_size for f in p.rglob("*") if f.is_file()
                )
            except Exception as exc:
                logger.debug(
                    "filesystem size measurement failed for %s: %s", p, exc,
                )
            shutil.rmtree(p, ignore_errors=True)
        except Exception as exc:
            logger.warning(
                "filesystem delete failed for tenant=%s helper=%s: %s",
                tenant_id, path_fn.__name__, exc,
            )
    return bytes_freed


async def _run_tenant_cascade_delete(
    tenant_id: str,
    before_snapshot: dict[str, Any],
    actor_email: str,
) -> dict[str, Any]:
    """Background worker — execute the cascade and emit SSE per phase.

    Runs OUTSIDE the request lifecycle (no request-scoped tenant
    contextvar; ``audit.log`` falls back to ``t-default`` for the
    audit chain, which is correct: the audit row belongs to the
    super-admin's chain, not the chain we just wiped).

    Returns the per-phase row counts so callers / tests can introspect
    the cascade outcome without re-querying.
    """
    from backend import audit as _audit
    from backend.db_pool import get_pool

    deleted_counts: dict[str, int] = {}
    started = time.time()
    try:
        async with get_pool().acquire() as conn:
            for idx, (table, sql) in enumerate(_DELETE_PHASES_PG, start=1):
                _emit_delete_progress(
                    tenant_id, phase=table, status="running",
                    step=idx, total=DELETE_TOTAL_PHASES,
                )
                tag = await conn.execute(sql, tenant_id)
                # asyncpg returns 'DELETE <n>'; parse defensively.
                count = 0
                try:
                    count = int(tag.rsplit(" ", 1)[-1])
                except (ValueError, IndexError):
                    pass
                deleted_counts[table] = count
                _emit_delete_progress(
                    tenant_id, phase=table, status="done",
                    step=idx, total=DELETE_TOTAL_PHASES,
                    rows_deleted=count,
                )

        _emit_delete_progress(
            tenant_id, phase="filesystem", status="running",
            step=DELETE_TOTAL_PHASES, total=DELETE_TOTAL_PHASES,
        )
        bytes_freed = await asyncio.to_thread(
            _delete_tenant_filesystem_sync, tenant_id,
        )
        _emit_delete_progress(
            tenant_id, phase="filesystem", status="done",
            step=DELETE_TOTAL_PHASES, total=DELETE_TOTAL_PHASES,
            bytes_freed=int(bytes_freed),
        )

        # Final summary audit row written under the super-admin's chain
        # (``audit.tenant_insert_value`` falls back to ``t-default`` for
        # contexts without a tenant — exactly what we want here).
        try:
            await _audit.log(
                action="tenant_deleted",
                entity_kind="tenant",
                entity_id=tenant_id,
                before=before_snapshot,
                after=None,
                actor=actor_email,
            )
        except Exception as exc:  # pragma: no cover — audit swallows
            logger.warning("tenant_deleted audit emit failed: %s", exc)

        elapsed = time.time() - started
        _emit_delete_progress(
            tenant_id, phase="all", status="completed",
            elapsed_seconds=round(elapsed, 3),
            deleted_counts=deleted_counts,
            bytes_freed=int(bytes_freed),
        )
        return {
            "tenant_id": tenant_id,
            "status": "completed",
            "deleted_counts": deleted_counts,
            "bytes_freed": int(bytes_freed),
            "elapsed_seconds": round(elapsed, 3),
        }
    except Exception as exc:
        logger.exception(
            "tenant cascade delete failed for tenant=%s", tenant_id,
        )
        _emit_delete_progress(
            tenant_id, phase="all", status="failed",
            error=str(exc),
            deleted_counts=deleted_counts,
        )
        try:
            await _audit.log(
                action="tenant_delete_failed",
                entity_kind="tenant",
                entity_id=tenant_id,
                before=before_snapshot,
                after={"error": str(exc), "deleted_counts": deleted_counts},
                actor=actor_email,
            )
        except Exception as audit_exc:  # pragma: no cover
            logger.warning(
                "tenant_delete_failed audit emit failed: %s", audit_exc,
            )
        return {
            "tenant_id": tenant_id,
            "status": "failed",
            "error": str(exc),
            "deleted_counts": deleted_counts,
        }


@router.delete("/tenants/{tenant_id}", status_code=202)
async def delete_tenant(
    tenant_id: str,
    _request: Request,
    confirm: str | None = Query(
        default=None,
        description="Must equal the path tenant_id; second-handshake "
                    "guard so a stray DELETE URL cannot wipe a tenant.",
    ),
    actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Cascade-delete a tenant and every owned row / artifact.

    The actual delete runs in the background (``asyncio.create_task``)
    and emits one ``tenant_delete_progress`` SSE event per phase via
    the global event bus. The HTTP response returns ``202 Accepted``
    immediately so the caller doesn't block on a tenant with millions
    of rows / many GiB of artifacts.

    Status codes
    ────────────
    * 202 — accepted; cascade started, watch SSE for progress.
    * 403 — caller is not a super-admin (handled by dependency), OR
      ``tenant_id`` is in ``PROTECTED_TENANT_IDS`` (``t-default``).
    * 404 — well-formed id but no such tenant.
    * 422 — id fails ``TENANT_ID_PATTERN``, OR the ``?confirm=`` query
      param is missing or doesn't match the path id.

    Confirm handshake
    ─────────────────
    A misconfigured client / shell history replay must not be able to
    delete a tenant by accidentally re-sending a stored URL. The caller
    is required to echo the tenant id in ``?confirm=<id>`` — same kind
    of two-step guard GitHub uses for ``DELETE`` of repos / orgs. The
    check is exact-equal, case-sensitive.

    SSE channel
    ───────────
    Event type: ``tenant_delete_progress``. Per-event payload::

        {
            "tenant_id": "t-acme",
            "phase": "<table_name|filesystem|all>",
            "status": "started|running|done|completed|failed",
            "step": <int>,                  # 1..N (per-phase)
            "total": <int>,                 # N (constant)
            "rows_deleted": <int>,          # only on table phases
            "bytes_freed": <int>,           # only on filesystem
            "elapsed_seconds": <float>,     # only on completed
            "deleted_counts": {table:int},  # only on completed/failed
            "error": "...",                 # only on failed
            "timestamp": "...iso..."
        }

    Audit
    ─────
    Three audit actions are written under the super-admin's chain:
      * ``tenant_delete_requested`` — synchronous, before kickoff
      * ``tenant_deleted`` — after successful cascade
      * ``tenant_delete_failed`` — if the cascade aborts mid-flight

    Module-global state
    ───────────────────
    SQL constants are module-level immutable strings (each worker
    derives the same value). The asyncpg pool is shared via PG.
    ``_pending_delete_tasks`` is intentionally per-worker (see its
    docstring) — each uvicorn worker tracks only the cascades it
    started, which matches the asyncio event-loop ownership semantics.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if confirm is None or confirm != tenant_id:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "DELETE requires ?confirm=<tenant_id> matching the "
                    "path id. This second handshake prevents accidental "
                    "deletion via a replayed URL."
                ),
                "tenant_id": tenant_id,
                "confirm_received": confirm,
            },
        )
    if tenant_id in PROTECTED_TENANT_IDS:
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    f"tenant {tenant_id!r} is protected and cannot be "
                    f"deleted. The platform default tenant backs every "
                    f"un-tenanted write path and the seeded audit chain."
                ),
                "tenant_id": tenant_id,
            },
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        cur_row = await conn.fetchrow(
            _FETCH_TENANT_FOR_DELETE_SQL, tenant_id,
        )
    if cur_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    before = {
        "id": cur_row["id"],
        "name": cur_row["name"],
        "plan": cur_row["plan"],
        "enabled": bool(cur_row["enabled"]),
    }

    # Best-effort synchronous audit BEFORE we kick off the cascade —
    # we want a record of "operator X requested delete of tenant Y at
    # time T" even if the cascade later aborts. ``audit.log`` swallows
    # its own failures so a transient chain outage doesn't 5xx the
    # caller (the cascade still runs).
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_delete_requested",
            entity_kind="tenant",
            entity_id=tenant_id,
            before=before,
            after=None,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant_delete_requested audit emit failed: %s", exc)

    # Emit the "started" event BEFORE kickoff so that subscribers who
    # connect after the 202 response can still see at least one event
    # confirming the cascade is in flight.
    started_at = time.time()
    _emit_delete_progress(
        tenant_id, phase="all", status="started",
        step=0, total=DELETE_TOTAL_PHASES,
        actor=actor.email,
    )

    task = asyncio.create_task(
        _run_tenant_cascade_delete(tenant_id, before, actor.email),
    )
    _pending_delete_tasks.add(task)
    task.add_done_callback(_pending_delete_tasks.discard)

    return JSONResponse(
        status_code=202,
        content={
            "tenant_id": tenant_id,
            "status": "deleting",
            "started_at": started_at,
            "sse_event": DELETE_PROGRESS_EVENT,
            "total_phases": DELETE_TOTAL_PHASES,
            "phases": list(DELETE_PHASE_NAMES),
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /admin/audit/tenants/{tenant_id} — per-tenant audit query
#  (Y9 #285 row 2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Why this endpoint exists
# ────────────────────────
# Y9 row 1 fanned 10 dot-notation event types into the audit chain
# (``tenant.created`` etc.). Operators now need a way to read the
# per-tenant slice of that stream without granting full ``role=admin``
# unfiltered access to every row in ``audit_log`` (which the existing
# /api/v1/audit endpoint gives — but only for the caller's own
# tenant_id contextvar).
#
# The /admin/audit/tenants/{tid} surface targets two roles:
#   * ``super_admin`` — may query ANY tenant's chain (cross-tenant
#     forensic read).
#   * tenant ``admin`` / ``owner`` — may query ONLY their own
#     tenant's chain (i.e. the tenant where they hold an active
#     membership row with role ∈ {owner, admin}). The legacy
#     ``users.role='admin'`` cache is intentionally NOT consulted —
#     a user who is admin on tenant A must not be able to read
#     tenant B's audit just because their primary-tenant role is
#     high. The Y3 / Y4 admin-tier helpers established this contract;
#     this endpoint follows the same rule.
#
# Every successful query writes one ``audit.queried`` audit row INTO
# THE QUERIED TENANT'S CHAIN so the queried tenant's own audit pane
# carries a record of cross-tenant inspection by super-admins. This
# is the "log 記 who-queried-which" requirement.
#
# Module-global state (SOP Step 1)
# ────────────────────────────────
# Two new module-level immutables: ``_AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES``
# (frozenset) + ``_QUERY_TENANT_AUDIT_SQL_BASE`` (str). Each uvicorn
# worker derives the same value from this source file (audit answer #1).
# The ContextVar swap inside the audit-row write follows the same
# save-and-restore pattern as ``backend.audit_events._emit_single_chain``
# — never mutates module state, restores on exception.
#
# Read-after-write timing (SOP Step 1)
# ────────────────────────────────────
# Two reads (tenants existence + audit_log fetch) followed by one
# best-effort ``audit.log`` write. The audit write holds a
# ``pg_advisory_xact_lock`` on its tenant chain; concurrent queries
# on the same tenant serialise on the chain append. The query reads
# do NOT take a lock — a concurrent writer's row may or may not be
# visible depending on commit ordering, which matches the
# "newest-first / id-descending / cursor=id<X" pagination contract
# (cursor is monotone in id).

# Membership roles that may query their own tenant's audit log.
# Mirrors the Y3 / Y4 admin-tier helpers
# (_INVITE_ALLOWED_MEMBERSHIP_ROLES, _user_can_manage_members) — if
# Y rolls out a new "auditor" role in the future, the row goes here.
_AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})

# Hard cap so a buggy / hostile caller can't request a million rows.
# Mirrors the cap on ``backend.routers._pagination.Limit`` defaults
# used by the I8 audit endpoint (max_cap=500).
_AUDIT_QUERY_HARD_MAX = 500
_AUDIT_QUERY_DEFAULT_LIMIT = 200

# Per-tenant audit fetch base. Filters / cursor / limit are appended
# at request time via PG ``$N`` placeholders — every variable goes
# through asyncpg parameter binding, never string-formatted into the
# SQL body, so this is injection-safe even though the SQL string is
# assembled dynamically.
_QUERY_TENANT_AUDIT_SQL_BASE = (
    "SELECT id, ts, actor, action, entity_kind, entity_id, "
    "before_json, after_json, prev_hash, curr_hash, session_id, "
    "tenant_id "
    "FROM audit_log "
    "WHERE tenant_id = $1"
)


async def _user_can_query_tenant_audit(
    user: auth.User,
    tenant_id: str,
) -> bool:
    """True iff ``user`` may query ``tenant_id``'s audit log.

    Resolution order (cheap → expensive):
      1. Platform ``super_admin`` — always allowed (cross-tenant).
      2. Caller has an *active* ``user_tenant_memberships`` row with
         role ∈ {owner, admin} on the target tenant. Membership row
         is per-tenant; that is the correct authoritative.

    The legacy ``users.role`` cache is **not** consulted: a user who
    is "admin on their primary tenant" must not be allowed to query
    a *different* tenant just because their primary-tenant role is
    high.
    """
    if auth.role_at_least(user.role, "super_admin"):
        return True

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user.id, tenant_id,
        )
    if row is None:
        return False
    if row["status"] != "active":
        return False
    return row["role"] in _AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES


def _build_audit_query_sql(
    *,
    has_since: bool,
    has_until: bool,
    has_actor: bool,
    has_action: bool,
    has_entity_kind: bool,
    has_cursor: bool,
) -> tuple[str, list[str]]:
    """Construct WHERE conditions and matching ``$N`` placeholders.

    Returns the assembled SQL string plus a list of slot names so the
    caller can hand each value to asyncpg in the right order. The
    only two table-shape inputs are the boolean flags; every actual
    *value* still goes through a parameter slot.
    """
    sql = _QUERY_TENANT_AUDIT_SQL_BASE
    slots: list[str] = ["tenant_id"]
    nxt = 2
    if has_since:
        sql += f" AND ts >= ${nxt}"
        slots.append("since")
        nxt += 1
    if has_until:
        sql += f" AND ts <= ${nxt}"
        slots.append("until")
        nxt += 1
    if has_actor:
        sql += f" AND actor = ${nxt}"
        slots.append("actor")
        nxt += 1
    if has_action:
        sql += f" AND action = ${nxt}"
        slots.append("action")
        nxt += 1
    if has_entity_kind:
        sql += f" AND entity_kind = ${nxt}"
        slots.append("entity_kind")
        nxt += 1
    if has_cursor:
        sql += f" AND id < ${nxt}"
        slots.append("cursor")
        nxt += 1
    sql += f" ORDER BY id DESC LIMIT ${nxt}"
    slots.append("limit")
    return sql, slots


@router.get("/audit/tenants/{tenant_id}")
async def get_tenant_audit_events(
    tenant_id: str,
    _request: Request,
    since: float | None = Query(
        default=None,
        description="Lower bound (inclusive) on audit_log.ts (UNIX seconds).",
    ),
    until: float | None = Query(
        default=None,
        description="Upper bound (inclusive) on audit_log.ts (UNIX seconds).",
    ),
    actor: str | None = Query(
        default=None,
        description="Exact-match filter on audit_log.actor (typically email).",
    ),
    action: str | None = Query(
        default=None,
        description="Exact-match filter on audit_log.action — pass one of "
                    "the canonical event types from "
                    "``backend.audit_events.ALL_EVENT_TYPES`` or any legacy "
                    "snake_case action.",
    ),
    entity_kind: str | None = Query(
        default=None,
        description="Exact-match filter on audit_log.entity_kind "
                    "(``tenant`` / ``project`` / ``tenant_invite`` / ...).",
    ),
    limit: int = Query(
        default=_AUDIT_QUERY_DEFAULT_LIMIT,
        ge=1, le=_AUDIT_QUERY_HARD_MAX,
        description="Max rows to return; hard-capped at 500.",
    ),
    cursor: int | None = Query(
        default=None, ge=0,
        description="Pagination cursor: only return rows with "
                    "audit_log.id strictly less than this value. Use the "
                    "smallest id in the previous page to fetch the next.",
    ),
    user: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List audit events scoped to a single tenant.

    Authorisation
    ─────────────
    * ``super_admin`` may query any tenant.
    * Tenant ``owner`` / ``admin`` may query their own tenant only,
      where "own" means an *active* ``user_tenant_memberships`` row
      with role ∈ {owner, admin} on the path-param tenant.
    * Anything else → 403.

    Status codes
    ────────────
    * 200 — payload below.
    * 403 — caller cannot query this tenant. Body includes the
      caller's role + the queried tenant id so the operator UI can
      render an explanation.
    * 404 — tenant id is well-formed but does not exist. Returned
      *after* the authz check passes so a non-super-admin probing
      arbitrary IDs cannot enumerate which tenants they would have
      been allowed to query.
    * 422 — ``tenant_id`` fails ``TENANT_ID_PATTERN``, or any query
      param violates its Pydantic constraint (limit out of range,
      negative cursor, ...).

    Payload
    ───────
    ::

        {
            "tenant_id": "t-acme",
            "items": [
                {
                    "id": 9876,
                    "ts": 1745580000.0,
                    "actor": "alice@acme.example",
                    "action": "tenant.created",
                    "entity_kind": "tenant",
                    "entity_id": "t-acme",
                    "before_json": "{...}",
                    "after_json": "{...}",
                    "prev_hash": "...",
                    "curr_hash": "...",
                    "session_id": "...",
                    "tenant_id": "t-acme"
                },
                ...
            ],
            "count": 200,
            "limit": 200,
            "cursor": null,
            "next_cursor": 9711,
            "filtered_to_self": false
        }

    Pagination
    ──────────
    Rows come back newest-first (``ORDER BY id DESC``). To page,
    pass the smallest ``id`` from the previous page as ``cursor``;
    the next call returns rows with ``id < cursor``. ``next_cursor``
    is the smallest id in the current response, or ``null`` if the
    response was shorter than ``limit`` (i.e. end of stream).

    Forensic audit row
    ──────────────────
    Every successful query (200) writes one ``audit.queried`` row
    INTO THE QUERIED TENANT'S CHAIN so the queried tenant's own audit
    pane carries a record of cross-tenant inspection by super-admins.
    The row records actor (querier email), querier role, querier home
    tenant, the ``cross_tenant`` flag, the filter shape, and the
    result count. Best-effort: a chain-write failure logs at warning
    but never 5xx's the read.
    """
    # Path-param shape check first — fail fast before authz / DB I/O.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    if not await _user_can_query_tenant_audit(user, tenant_id):
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "audit query forbidden: super_admin may query any "
                    "tenant; tenant owner / admin may query their own "
                    "tenant only."
                ),
                "tenant_id": tenant_id,
                "your_role": user.role,
                "your_home_tenant": user.tenant_id,
            },
        )

    from backend.db_pool import get_pool

    # Tenant-existence probe. Done AFTER authz so a non-super-admin
    # probing arbitrary ids cannot enumerate which tenants exist via
    # 404-vs-403 timing.
    async with get_pool().acquire() as conn:
        exists = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1", tenant_id,
        )
    if exists is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    sql, slot_names = _build_audit_query_sql(
        has_since=since is not None,
        has_until=until is not None,
        has_actor=actor is not None,
        has_action=action is not None,
        has_entity_kind=entity_kind is not None,
        has_cursor=cursor is not None,
    )
    slot_values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "since": since,
        "until": until,
        "actor": actor,
        "action": action,
        "entity_kind": entity_kind,
        "cursor": cursor,
        "limit": int(limit),
    }
    params = [slot_values[name] for name in slot_names]

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)

    items = [
        {
            "id": int(r["id"]),
            "ts": float(r["ts"]) if r["ts"] is not None else None,
            "actor": r["actor"],
            "action": r["action"],
            "entity_kind": r["entity_kind"],
            "entity_id": r["entity_id"],
            "before_json": r["before_json"],
            "after_json": r["after_json"],
            "prev_hash": r["prev_hash"],
            "curr_hash": r["curr_hash"],
            "session_id": r["session_id"],
            "tenant_id": r["tenant_id"],
        }
        for r in rows
    ]

    next_cursor = items[-1]["id"] if len(items) == int(limit) else None
    is_cross_tenant = (user.tenant_id != tenant_id)

    # Forensic "who queried which tenant" audit row. Goes into the
    # QUERIED tenant's chain so the queried tenant's own audit pane
    # carries the record. Pattern mirrors
    # ``audit_events._emit_single_chain``: save the prior contextvar,
    # set the override, write, restore on finally so an unrelated code
    # path on the same task cannot inherit the override even on
    # exception.
    try:
        from backend import audit as _audit
        from backend.db_context import (
            current_tenant_id as _ctv,
            set_tenant_id as _stv,
        )
        saved = _ctv()
        try:
            _stv(tenant_id)
            await _audit.log(
                action="audit.queried",
                entity_kind="tenant",
                entity_id=tenant_id,
                before=None,
                after={
                    "queried_tenant": tenant_id,
                    "queried_by_user_id": user.id,
                    "queried_by_role": user.role,
                    "querier_home_tenant": user.tenant_id,
                    "cross_tenant": is_cross_tenant,
                    "filters": {
                        "since": since,
                        "until": until,
                        "actor": actor,
                        "action": action,
                        "entity_kind": entity_kind,
                        "cursor": cursor,
                        "limit": int(limit),
                    },
                    "result_count": len(items),
                },
                actor=user.email,
            )
        finally:
            _stv(saved)
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("audit.queried emit failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "items": items,
            "count": len(items),
            "limit": int(limit),
            "cursor": cursor,
            "next_cursor": next_cursor,
            "filtered_to_self": not is_cross_tenant,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y9 #285 row 3 — per-(tenant_id, project_id) usage breakdown for T6
#  pricing page.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Reads ``billing_usage_events`` (alembic 0039) and returns one
# row per ``project_id`` for the requested tenant + time window. The
# T6 pricing page renders the breakdown table; the contract is the
# read-side of the Y9 row 3 ``(tenant_id, project_id)`` tuple plumbing
# (LLM call / workflow_run / workspace-GB-hour all tagged at write
# time by ``backend.billing_usage``).
#
# Authorisation mirrors GET /admin/audit/tenants/{tid}: super-admin
# may query any tenant; tenant owner / admin may query their own
# tenant only; everyone else 403. We reuse
# :func:`_user_can_query_tenant_audit` since the auth contract is
# identical (admin-tier members of the target tenant see their own
# data; cross-tenant view requires super-admin).
#
# Module-global state (SOP Step 1)
# ────────────────────────────────
# No new module-level state in this row. Re-uses the immutable
# ``_AUDIT_QUERY_ALLOWED_MEMBERSHIP_ROLES`` frozenset already defined
# above for the audit-query helper. Each uvicorn worker derives the
# same value (audit answer #1).
#
# Read-after-write timing (SOP Step 1)
# ────────────────────────────────────
# Two reads (tenants existence + breakdown SUM/GROUP BY). No write.
# Concurrent emitters (LLM callback / workflow.finish / workspace-GC
# sweep) write rows that may or may not be visible depending on
# commit ordering — the GROUP BY reads whatever has committed at
# query time, which matches the eventual-consistency contract of
# any append-only billing fact table.


@router.get("/usage/breakdown")
async def get_usage_breakdown_by_project(
    _request: Request,
    tenant_id: str = Query(
        ...,
        description="Tenant id to compute the breakdown for. Must "
                    "match TENANT_ID_PATTERN.",
    ),
    since: float | None = Query(
        default=None,
        description="Lower bound (inclusive) on "
                    "billing_usage_events.occurred_at (UNIX seconds).",
    ),
    until: float | None = Query(
        default=None,
        description="Upper bound (inclusive) on "
                    "billing_usage_events.occurred_at (UNIX seconds).",
    ),
    user: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Per-(``tenant_id``, ``project_id``) usage breakdown — T6 pricing
    page data source.

    Authorisation
    ─────────────
    * ``super_admin`` may query any tenant.
    * Tenant ``owner`` / ``admin`` may query their own tenant only,
      where "own" means an *active* ``user_tenant_memberships`` row
      with role ∈ {owner, admin} on the queried tenant.
    * Anything else → 403.

    Status codes
    ────────────
    * 200 — payload below.
    * 403 — caller cannot query this tenant.
    * 404 — tenant not found (returned *after* authz so non-super-admin
      cannot enumerate via 404-vs-403 timing).
    * 422 — ``tenant_id`` shape invalid.

    Payload
    ───────
    ::

        {
            "tenant_id": "t-acme",
            "since": 1745500000.0,
            "until": null,
            "breakdown": [
                {
                    "project_id": "p-acme-firmware",
                    "llm_calls": 1024,
                    "llm_input_tokens": 245000,
                    "llm_output_tokens": 91000,
                    "llm_cost_usd": 14.32,
                    "workflow_runs": 53,
                    "workspace_gb_hours": 17.6
                },
                ...
            ],
            "totals": {
                "llm_calls": 4096,
                "llm_input_tokens": 980000,
                "llm_output_tokens": 364000,
                "llm_cost_usd": 57.28,
                "workflow_runs": 212,
                "workspace_gb_hours": 70.4
            }
        }

    Sort order
    ──────────
    Rows come back ordered by ``llm_cost_usd DESC`` then ``project_id
    ASC`` (the spend hot-spots first) — matches what the T6 pricing
    page wants for its breakdown table.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    if not await _user_can_query_tenant_audit(user, tenant_id):
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "usage breakdown forbidden: super_admin may query "
                    "any tenant; tenant owner / admin may query their "
                    "own tenant only."
                ),
                "tenant_id": tenant_id,
                "your_role": user.role,
                "your_home_tenant": user.tenant_id,
            },
        )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        exists = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1", tenant_id,
        )
    if exists is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    from backend import billing_usage as _billing
    breakdown = await _billing.breakdown_by_project(
        tenant_id=tenant_id,
        since=since,
        until=until,
    )

    totals = {
        "llm_calls": sum(int(r["llm_calls"]) for r in breakdown),
        "llm_input_tokens": sum(
            int(r["llm_input_tokens"]) for r in breakdown
        ),
        "llm_output_tokens": sum(
            int(r["llm_output_tokens"]) for r in breakdown
        ),
        "llm_cost_usd": round(
            sum(float(r["llm_cost_usd"]) for r in breakdown), 6,
        ),
        "workflow_runs": sum(int(r["workflow_runs"]) for r in breakdown),
        "workspace_gb_hours": round(
            sum(float(r["workspace_gb_hours"]) for r in breakdown), 6,
        ),
    }

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "since": since,
            "until": until,
            "breakdown": breakdown,
            "totals": totals,
        },
    )

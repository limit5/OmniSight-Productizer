"""Y2 (#278) — Admin REST API for tenant CRUD.

Replaces the legacy "operator runs raw SQL against the tenants table"
hack workflow with a proper super-admin-gated REST surface. This file
ships the FIRST row of the Y2 plan only:

  POST /api/v1/admin/tenants — create a new tenant.

Subsequent rows (GET list / GET detail / PATCH / DELETE) extend this
router; they are out of scope here and will be added by their own
TODO rows.

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

import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.tenant_quota import PLAN_DISK_QUOTAS

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

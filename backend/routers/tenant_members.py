"""Y3 (#279) row 6 — GET / PATCH / DELETE /api/v1/tenants/{tid}/members.

Tenant-scoped membership management for the admin console:

  * GET    /api/v1/tenants/{tid}/members[?status=&limit=]
        list every membership row for the target tenant joined onto the
        users row so the operator sees email / display name / per-tenant
        role / status / join date / last-active in one shot.

  * PATCH  /api/v1/tenants/{tid}/members/{user_id}
        partial update of role and/or status. Body accepts ``role`` and
        ``status``; at least one must be present. Idempotent — re-PATCH
        to the same state still returns 200 with ``no_change=True`` and
        does NOT emit audit.

  * DELETE /api/v1/tenants/{tid}/members/{user_id}
        soft-delete: sets ``status='suspended'`` while preserving the
        membership row + audit trail. Hard delete only happens when the
        tenant itself is dropped (PG cascade — see Y2 admin_tenants
        DELETE handler). Idempotent — re-DELETE on an already-suspended
        row returns 200 with ``already_suspended=True`` and emits no
        audit row.

Endpoint contracts at a glance
──────────────────────────────
::

    GET /api/v1/tenants/{tid}/members
    auth   : tenant admin or above on the target tenant or platform
             super_admin (same trust boundary as the invite surface)
    out    : 200 {tenant_id, status_filter, count, members:[…]}
    errors : 403 RBAC · 404 unknown tenant · 422 malformed id /
             unknown status / oversized limit

    PATCH /api/v1/tenants/{tid}/members/{user_id}
    body   : {"role"?: "owner|admin|member|viewer",
              "status"?: "active|suspended"}
    out    : 200 {tenant_id, user_id, role, status, no_change?, …}
    errors : 400 empty body · 403 RBAC · 404 unknown tenant /
             unknown membership · 409 last-admin floor would be
             breached · 422 malformed ids / unknown enum value

    DELETE /api/v1/tenants/{tid}/members/{user_id}
    out    : 200 {tenant_id, user_id, status:'suspended',
                  already_suspended, role, …}
    errors : 403 RBAC · 404 unknown tenant / unknown membership ·
             409 last-admin floor would be breached · 422 malformed id

Last-admin floor invariant
──────────────────────────
Every tenant must always have at least ONE active admin-tier member —
either ``owner`` or ``admin`` whose ``status='active'`` and whose
backing ``users.enabled=1``. Suspending the last such member, or
demoting them out of the admin tier, would leave the tenant orphaned
(no one can manage invites / projects / further role changes).

The floor check counts *other* enabled-active admin-tier members
excluding the target. If the count is zero AND the target is itself
admin-tier-active-enabled, the operation is refused with 409.

Suspending or demoting a *member* / *viewer* never trips the floor
because their state has no bearing on the admin-tier count. Suspending
an already-disabled user doesn't reduce the live admin count either,
so it's allowed even if it would otherwise be the last admin row.

Concurrent demote race protection
─────────────────────────────────
Two admins suspending each other simultaneously would each see the
*other* as still-active and both commit — leaving zero. To serialise
those operations we take a per-tenant ``pg_advisory_xact_lock`` on
``hashtext('omnisight_membership_demote:' || tenant_id)`` at the start
of every PATCH/DELETE transaction. The lock is per-tenant (not
platform-wide like super-admin demote) so traffic across tenants does
not collide. The loser blocks until the winner commits, then
re-counts and either proceeds or 409s.

Audit emission
──────────────
Single action ``tenant_member_updated`` covers PATCH + DELETE. The
before / after blob captures both ``role`` and ``status`` so the
audit trail tells the full story regardless of which field(s) the
admin actually changed:

    before = {"role": "<prev_role>", "status": "<prev_status>"}
    after  = {"role": "<new_role>",  "status": "<new_status>"}

No-op responses (PATCH with the same values, DELETE on already-
suspended) emit no audit row. The 409 floor-block path also emits no
audit (no mutation occurred).

Module-global state audit (SOP Step 1)
──────────────────────────────────────
None introduced. ``USER_ID_PATTERN`` / ``_USER_ID_RE`` /
``MEMBERSHIP_ROLE_ENUM`` / ``MEMBERSHIP_STATUS_ENUM`` /
``LISTABLE_MEMBERSHIP_STATUSES`` / ``_TENANT_ADMIN_TIER_ROLES`` /
``_MEMBERSHIP_DEMOTE_LOCK_PREFIX`` / 9 SQL constants are all
module-level immutable; each uvicorn worker derives the same values
from source. The asyncpg pool is shared via PG. Per-tenant advisory
lock is PG-coordinated across workers (qualifying answer #2).

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
GET is a pure read — no write. PATCH / DELETE both wrap the
SELECT-FOR-UPDATE → floor-count → UPDATE … RETURNING sequence in a
single transaction guarded by the per-tenant advisory lock so
concurrent demotes serialise without leaking the floor.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from backend import auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tenant-members"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant id — same source-of-truth as Y2 admin_tenants / Y3 row 1.
TENANT_ID_PATTERN = r"^t-[a-z0-9][a-z0-9-]{2,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)

# User id — same shape as Y3 row 5 admin_super_admins.
USER_ID_PATTERN = r"^u-[a-z0-9]{4,64}$"
_USER_ID_RE = re.compile(USER_ID_PATTERN)


def _is_valid_tenant_id(tid: str) -> bool:
    return bool(tid) and bool(_TENANT_ID_RE.match(tid))


def _is_valid_user_id(uid: str) -> bool:
    return bool(uid) and bool(_USER_ID_RE.match(uid))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Mirrors the DB CHECK on ``user_tenant_memberships.role``.
MEMBERSHIP_ROLE_ENUM = ("owner", "admin", "member", "viewer")

# Mirrors the DB CHECK on ``user_tenant_memberships.status``.
MEMBERSHIP_STATUS_ENUM = ("active", "suspended")

# Filter values for ``GET ?status=`` — DB enum + sentinel ``all`` that
# skips the WHERE filter entirely. Default is ``active`` so the admin
# console's primary tab shows live members only.
LISTABLE_MEMBERSHIP_STATUSES = ("active", "suspended", "all")

# Cap on rows returned by GET. Keeps response bounded under tenants
# with thousands of members. Admin UI paginates client-side; default
# matches Y3 row 2 invite list endpoint convention.
LIST_MEMBERS_DEFAULT_LIMIT = 100
LIST_MEMBERS_MAX_LIMIT = 500

# Roles that count toward the last-admin floor. Owner outranks admin
# functionally for some operations (only owner can transfer the tenant
# in a future row), but for the floor we treat both as admin-tier
# because either can manage invites / projects / role changes.
_TENANT_ADMIN_TIER_ROLES = frozenset({"owner", "admin"})

# Prefix for the per-tenant ``pg_advisory_xact_lock`` key. Concatenated
# with the tenant id and run through ``hashtext`` inside the SQL itself
# to produce a stable bigint per tenant.
_MEMBERSHIP_DEMOTE_LOCK_PREFIX = "omnisight_membership_demote:"

# Membership roles whose holders may issue invite / member-management
# calls for a tenant. Identical to the invite router's allow-list —
# tenant admin or above OR platform super_admin.
_MEMBER_MGMT_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PatchMemberRequest(BaseModel):
    """Body for ``PATCH /api/v1/tenants/{tid}/members/{user_id}``.

    Both fields are optional individually but at least one must be
    supplied — an empty body is rejected at the schema layer to avoid
    a wasteful round-trip to PG only to discover the caller had
    nothing to change.
    """

    role: Literal["owner", "admin", "member", "viewer"] | None = Field(
        default=None,
        description=(
            "New tenant-scope role for the membership row. Must be "
            "one of (owner, admin, member, viewer). Pydantic returns "
            "422 on anything else before the handler runs. Demoting "
            "the last admin-tier member of the tenant is refused with "
            "409 — the tenant must always have at least one active "
            "owner / admin."
        ),
    )
    status: Literal["active", "suspended"] | None = Field(
        default=None,
        description=(
            "New membership status. ``suspended`` keeps the row + "
            "audit trail but disables tenant access; ``active`` "
            "reactivates a previously-suspended membership. "
            "Suspending the last admin-tier member is refused with "
            "409."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "PatchMemberRequest":
        if self.role is None and self.status is None:
            raise ValueError(
                "PATCH body must include at least one of "
                "'role' or 'status'"
            )
        return self


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorisation — tenant admin / owner on the target tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _user_can_manage_members(
    user: auth.User,
    tenant_id: str,
) -> bool:
    """True iff ``user`` may list / modify / suspend memberships on
    ``tenant_id``.

    Order of checks (cheap → expensive):
      1. Platform ``super_admin`` — always allowed (matches Y2 / Y3
         row 1 trust boundary; super-admin manages cross-tenant state).
      2. Active membership row with role ∈ {owner, admin} on the
         target tenant — DB lookup against
         ``user_tenant_memberships`` (the Y1 N-to-M source-of-truth).

    Pure tenant-account-tier ``users.role='admin'`` is NOT consulted —
    a user who is admin on tenant A must not be allowed to manage
    members of tenant B unless they are explicitly admin on B too.
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
    return row["role"] in _MEMBER_MGMT_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant existence probe — used by every endpoint to surface a clean
# 404 before doing membership work.
_FETCH_TENANT_SQL = "SELECT id FROM tenants WHERE id = $1"

# List active members. Joined onto ``users`` so the operator sees
# email / name / account-tier-enabled flag without a second round-trip.
# ``token_hash`` style PII (password_hash / oidc_subject) is NOT
# projected — cleaner separation of concerns and zero leak surface.
_LIST_MEMBERS_ACTIVE_SQL = """
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
  AND m.status = 'active'
ORDER BY m.created_at ASC, u.email ASC
LIMIT $2
"""

_LIST_MEMBERS_BY_STATUS_SQL = """
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
  AND m.status = $2
ORDER BY m.created_at ASC, u.email ASC
LIMIT $3
"""

_LIST_MEMBERS_ALL_SQL = """
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
LIMIT $2
"""

# Read-only fetch of a single membership row (no row lock). Used to
# distinguish 404 / no-op / happy-path branches from outside a write
# transaction (PATCH / DELETE re-fetch with FOR UPDATE inside the tx).
_FETCH_MEMBERSHIP_SQL = """
SELECT
    m.user_id, m.tenant_id, m.role, m.status,
    m.created_at, m.last_active_at,
    u.email, u.name, u.enabled AS user_enabled
FROM user_tenant_memberships m
JOIN users u ON u.id = m.user_id
WHERE m.user_id = $1 AND m.tenant_id = $2
"""

# Same projection but with FOR UPDATE so concurrent PATCH / DELETE
# attempts on the same membership row serialise. Used inside the
# write transaction.
_FETCH_MEMBERSHIP_FOR_UPDATE_SQL = """
SELECT
    m.user_id, m.tenant_id, m.role, m.status,
    m.created_at, m.last_active_at,
    u.email, u.name, u.enabled AS user_enabled
FROM user_tenant_memberships m
JOIN users u ON u.id = m.user_id
WHERE m.user_id = $1 AND m.tenant_id = $2
FOR UPDATE OF m
"""

# Atomic role-only update. ``RETURNING`` projects the post-update row
# so the response body and the audit ``after`` payload share a single
# source of truth. ``WHERE`` is keyed by composite PK only — the FOR
# UPDATE row lock acquired earlier in the same tx serialises any
# concurrent writer.
_UPDATE_MEMBERSHIP_ROLE_SQL = """
UPDATE user_tenant_memberships
SET role = $3
WHERE user_id = $1 AND tenant_id = $2
RETURNING user_id, tenant_id, role, status, created_at, last_active_at
"""

# Atomic status-only update.
_UPDATE_MEMBERSHIP_STATUS_SQL = """
UPDATE user_tenant_memberships
SET status = $3
WHERE user_id = $1 AND tenant_id = $2
RETURNING user_id, tenant_id, role, status, created_at, last_active_at
"""

# Atomic role + status update — single round-trip when both fields
# change in the same PATCH.
_UPDATE_MEMBERSHIP_ROLE_AND_STATUS_SQL = """
UPDATE user_tenant_memberships
SET role = $3, status = $4
WHERE user_id = $1 AND tenant_id = $2
RETURNING user_id, tenant_id, role, status, created_at, last_active_at
"""

# Last-admin floor check. Counts OTHER enabled-active admin-tier
# members of the tenant (excluding the target). Used by PATCH (when
# demoting an admin-tier member out of the tier OR suspending one)
# and by DELETE (always — DELETE always drops to suspended). Disabled
# users are excluded — a user who can't log in doesn't preserve admin
# reach. The target's own ``users.enabled`` flag is checked separately
# in the handler so the disabled-target-bypasses-floor optimisation
# matches the super-admin pattern.
_COUNT_OTHER_ACTIVE_ADMINS_SQL = """
SELECT COUNT(*) AS n
FROM user_tenant_memberships m
JOIN users u ON u.id = m.user_id
WHERE m.tenant_id = $1
  AND m.user_id <> $2
  AND m.status = 'active'
  AND m.role IN ('owner', 'admin')
  AND u.enabled = 1
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _row_to_member_dict(row) -> dict:
    """Project the joined membership+user row for response bodies."""
    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "status": row["status"],
        "user_enabled": bool(row["user_enabled"]),
        "joined_at": row["joined_at"],
        "last_active_at": row["last_active_at"],
    }


def _is_admin_tier_active_enabled(role: str, status: str, enabled: int) -> bool:
    """True iff this membership row counts toward the floor — i.e.
    role ∈ {owner, admin} AND status='active' AND user.enabled=1.
    Disabled users contribute zero to the live admin-tier count."""
    return (
        role in _TENANT_ADMIN_TIER_ROLES
        and status == "active"
        and bool(enabled)
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /tenants/{tid}/members
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/tenants/{tenant_id}/members")
async def list_members(
    tenant_id: str,
    _request: Request,
    status: str = Query(
        default="active",
        description=(
            "Filter by membership status. One of "
            "(active, suspended, all). Default is 'active' — the "
            "live members tab. 'all' returns every status."
        ),
    ),
    limit: int = Query(
        default=LIST_MEMBERS_DEFAULT_LIMIT,
        ge=1,
        le=LIST_MEMBERS_MAX_LIMIT,
        description=(
            f"Max rows to return (1..{LIST_MEMBERS_MAX_LIMIT}). "
            f"Default {LIST_MEMBERS_DEFAULT_LIMIT}."
        ),
    ),
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List memberships for a tenant.

    Returns 200 with::

        {
            "tenant_id": "t-acme",
            "status_filter": "active",
            "count": 4,
            "members": [
                {
                    "user_id": "u-...",
                    "email": "alice@example.com",
                    "name": "Alice",
                    "role": "admin",
                    "status": "active",
                    "user_enabled": true,
                    "joined_at": "2026-04-25 12:00:00",
                    "last_active_at": "2026-04-25 13:14:15"
                },
                ...
            ]
        }
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    if status not in LISTABLE_MEMBERSHIP_STATUSES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid status filter: {status!r}; must be one of "
                    f"{LISTABLE_MEMBERSHIP_STATUSES}"
                ),
            },
        )

    # RBAC before existence — same trust boundary as Y3 invite surface.
    if not await _user_can_manage_members(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        if status == "active":
            rows = await conn.fetch(
                _LIST_MEMBERS_ACTIVE_SQL, tenant_id, limit,
            )
        elif status == "all":
            rows = await conn.fetch(
                _LIST_MEMBERS_ALL_SQL, tenant_id, limit,
            )
        else:
            rows = await conn.fetch(
                _LIST_MEMBERS_BY_STATUS_SQL, tenant_id, status, limit,
            )

    members = [_row_to_member_dict(r) for r in rows]
    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "status_filter": status,
            "count": len(members),
            "members": members,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATCH /tenants/{tid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.patch("/tenants/{tenant_id}/members/{user_id}")
async def patch_member(
    tenant_id: str,
    user_id: str,
    body: PatchMemberRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Update a membership's role and/or status.

    Returns 200 with the post-update row state plus a ``no_change``
    flag for callers that PATCH'd the row to its current values.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if not _is_valid_user_id(user_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid user id: {user_id!r}; "
                               f"must match {USER_ID_PATTERN}"},
        )

    if not await _user_can_manage_members(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    new_row, before_state, no_change, floor_block = await _apply_membership_change(
        tenant_id=tenant_id,
        user_id=user_id,
        target_role=body.role,
        target_status=body.status,
    )

    if floor_block is not None:
        return JSONResponse(status_code=409, content=floor_block)

    if before_state is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"membership not found: user={user_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    if no_change:
        # Same-state PATCH — return current state, no audit row.
        return JSONResponse(
            status_code=200,
            content={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": before_state["role"],
                "status": before_state["status"],
                "no_change": True,
                "email": before_state["email"],
                "name": before_state["name"],
                "user_enabled": bool(before_state["user_enabled"]),
                "joined_at": before_state["created_at"],
                "last_active_at": before_state["last_active_at"],
            },
        )

    assert new_row is not None  # non-no-change branches always update
    await _emit_member_updated_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        before=before_state,
        after=new_row,
        actor=actor,
    )
    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": new_row["role"],
            "status": new_row["status"],
            "no_change": False,
            "email": before_state["email"],
            "name": before_state["name"],
            "user_enabled": bool(before_state["user_enabled"]),
            "joined_at": new_row["created_at"],
            "last_active_at": new_row["last_active_at"],
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DELETE /tenants/{tid}/members/{user_id}  — soft delete (suspend)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.delete("/tenants/{tenant_id}/members/{user_id}")
async def delete_member(
    tenant_id: str,
    user_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Soft-delete a membership by flipping ``status`` to
    ``suspended``. The row + audit history is preserved; hard delete
    only happens when the tenant itself is dropped (PG cascade).

    Idempotent on already-suspended rows (200 + ``already_suspended=
    True``, no audit). Refuses with 409 if the demotion would leave
    the tenant with zero active admin-tier members.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if not _is_valid_user_id(user_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid user id: {user_id!r}; "
                               f"must match {USER_ID_PATTERN}"},
        )

    if not await _user_can_manage_members(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    new_row, before_state, no_change, floor_block = await _apply_membership_change(
        tenant_id=tenant_id,
        user_id=user_id,
        target_role=None,
        target_status="suspended",
    )

    if floor_block is not None:
        return JSONResponse(status_code=409, content=floor_block)

    if before_state is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"membership not found: user={user_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    if no_change:
        # Already suspended — idempotent.
        return JSONResponse(
            status_code=200,
            content={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": before_state["role"],
                "status": before_state["status"],
                "already_suspended": True,
                "email": before_state["email"],
                "name": before_state["name"],
                "user_enabled": bool(before_state["user_enabled"]),
                "joined_at": before_state["created_at"],
                "last_active_at": before_state["last_active_at"],
            },
        )

    assert new_row is not None
    await _emit_member_updated_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        before=before_state,
        after=new_row,
        actor=actor,
    )
    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": new_row["role"],
            "status": new_row["status"],
            "already_suspended": False,
            "email": before_state["email"],
            "name": before_state["name"],
            "user_enabled": bool(before_state["user_enabled"]),
            "joined_at": new_row["created_at"],
            "last_active_at": new_row["last_active_at"],
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared write path — used by both PATCH and DELETE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _apply_membership_change(
    *,
    tenant_id: str,
    user_id: str,
    target_role: str | None,
    target_status: str | None,
):
    """Atomically apply an optional role/status change with floor
    protection.

    Returns a 4-tuple ``(new_row, before_state, no_change, floor_block)``:

      * ``new_row`` — the post-update row dict (None on no-change /
        404 / floor-block paths)
      * ``before_state`` — the pre-update row dict (None on 404 path)
      * ``no_change`` — True when the request was a same-state no-op
      * ``floor_block`` — None if the change was permitted; else a
        dict suitable for the 409 response body explaining which
        last-admin invariant would be breached.

    The whole sequence runs in a single transaction guarded by a
    per-tenant ``pg_advisory_xact_lock`` so concurrent demote attempts
    on different members of the same tenant serialise (the worst-case
    A-suspends-B-while-B-suspends-A race that would otherwise floor
    the count without either tx noticing).
    """
    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Per-tenant advisory lock — released automatically on tx
            # commit/rollback. Keyed on the tenant id so traffic on
            # different tenants does not collide.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _MEMBERSHIP_DEMOTE_LOCK_PREFIX + tenant_id,
            )

            cur = await conn.fetchrow(
                _FETCH_MEMBERSHIP_FOR_UPDATE_SQL, user_id, tenant_id,
            )
            if cur is None:
                return None, None, False, None

            before_state = {
                "user_id": cur["user_id"],
                "tenant_id": cur["tenant_id"],
                "role": cur["role"],
                "status": cur["status"],
                "created_at": cur["created_at"],
                "last_active_at": cur["last_active_at"],
                "email": cur["email"],
                "name": cur["name"],
                "user_enabled": cur["user_enabled"],
            }

            new_role = target_role if target_role is not None else cur["role"]
            new_status = (
                target_status if target_status is not None else cur["status"]
            )

            if new_role == cur["role"] and new_status == cur["status"]:
                # Same-state no-op — short-circuit before touching the
                # row or the floor counter.
                return None, before_state, True, None

            # Floor protection — invoked when the *target* row is the
            # one being demoted out of admin-tier-active-enabled.
            target_was_admin_tier = _is_admin_tier_active_enabled(
                cur["role"], cur["status"], cur["user_enabled"],
            )
            target_will_be_admin_tier = (
                new_role in _TENANT_ADMIN_TIER_ROLES
                and new_status == "active"
                and bool(cur["user_enabled"])
            )
            if target_was_admin_tier and not target_will_be_admin_tier:
                other_count = int(await conn.fetchval(
                    _COUNT_OTHER_ACTIVE_ADMINS_SQL, tenant_id, user_id,
                ))
                if other_count == 0:
                    floor_block = {
                        "detail": (
                            f"refusing to demote / suspend membership "
                            f"{user_id!r} on tenant {tenant_id!r}: "
                            f"this is the last active admin-tier member "
                            f"on the tenant and the change would leave "
                            f"zero owners/admins able to manage invites "
                            f"and member roles. Promote another member "
                            f"first, then retry."
                        ),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "would_leave_zero_admin_members": True,
                        "other_active_admin_member_count": 0,
                    }
                    return None, before_state, False, floor_block

            # Issue the right UPDATE shape based on which fields the
            # caller wanted to flip. ``RETURNING`` gives us the new row
            # in one round-trip.
            if target_role is not None and target_status is not None:
                new = await conn.fetchrow(
                    _UPDATE_MEMBERSHIP_ROLE_AND_STATUS_SQL,
                    user_id, tenant_id, new_role, new_status,
                )
            elif target_role is not None:
                new = await conn.fetchrow(
                    _UPDATE_MEMBERSHIP_ROLE_SQL,
                    user_id, tenant_id, new_role,
                )
            else:
                new = await conn.fetchrow(
                    _UPDATE_MEMBERSHIP_STATUS_SQL,
                    user_id, tenant_id, new_status,
                )

            new_row = {
                "user_id": new["user_id"],
                "tenant_id": new["tenant_id"],
                "role": new["role"],
                "status": new["status"],
                "created_at": new["created_at"],
                "last_active_at": new["last_active_at"],
            }
            return new_row, before_state, False, None


async def _emit_member_updated_audit(
    *,
    tenant_id: str,
    user_id: str,
    before,
    after,
    actor: auth.User,
) -> None:
    """Fire a ``tenant_member_updated`` audit row capturing the full
    role/status delta. Best-effort — failures are logged at warning
    and never raise (matches every other audit.log callsite)."""
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_member_updated",
            entity_kind="tenant_membership",
            entity_id=user_id,
            before={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": before["role"],
                "status": before["status"],
            },
            after={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": after["role"],
                "status": after["status"],
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_member_updated audit emit failed (tenant=%s "
            "user=%s): %s", tenant_id, user_id, exc,
        )

"""Y3 (#279) row 5 — POST / DELETE /api/v1/admin/super-admins.

Platform-operator self-service for promoting / revoking the
``users.role = 'super_admin'`` flag. Only an existing super-admin may
call these endpoints; the very first super-admin is seeded by the
bootstrap wizard (Y7 #283) or by direct DB INSERT during the deploy
runbook.

Endpoint contracts
──────────────────
::

    POST   /api/v1/admin/super-admins
    body   : {"user_id": "u-..."}
    auth   : require_super_admin (existing super-admin only)
    out    : 200 {user_id, email, role:'super_admin', already_super_admin}
             — handler is idempotent. ``already_super_admin=True`` is
             returned when the target was already platform-tier; UI
             clients use this to suppress a duplicate "promoted" toast.
    errors : 403 caller not super-admin · 404 user not found ·
             409 user disabled (cannot promote a disabled account) ·
             422 malformed body / user_id pattern.

    DELETE /api/v1/admin/super-admins/{user_id}
    auth   : require_super_admin
    out    : 200 {user_id, email, role:'admin', already_revoked}
             — handler is idempotent. ``already_revoked=True`` is
             returned when the target was already not super-admin; the
             new role on a happy-path demotion is always ``'admin'``
             (one tier below ``super_admin`` on ``auth.ROLES``) so the
             demoted operator keeps tenant-admin capability without
             platform-tier reach.
    errors : 403 caller not super-admin · 404 user not found ·
             409 would leave zero enabled super-admins (last-super-
             admin protection — the platform must always have at least
             one operator who can manage tenants / promote others) ·
             422 malformed user_id pattern.

Last-super-admin invariant
──────────────────────────
The DELETE handler refuses any demotion that would bring the count of
*enabled* super-admins to zero. Disabled super-admins are not counted
toward the floor — they can't log in, so they don't preserve operator
reach. The check + UPDATE is wrapped in a transaction with
``pg_advisory_xact_lock`` keyed on the constant string
``omnisight_super_admin_demote`` so two operators racing each other
("A demotes B while B demotes A" — both would individually see one
remaining super-admin and both would commit, leaving zero) are
strictly serialised: the second tx blocks until the first commits and
then re-counts before deciding.

Demotion target role
────────────────────
A revoked super-admin lands on ``role='admin'`` (one rank below
``super_admin`` in ``auth.ROLES``). This preserves the operator's
tenant-admin reach (an existing tenant-admin should not be silently
demoted to ``viewer`` just because they were also a super-admin) while
removing platform-tier authority. The membership rows
(``user_tenant_memberships``) are untouched — those are per-tenant
scopes; this endpoint only mutates the account-tier role flag on
``users``.

Promotion preconditions
───────────────────────
Promoting a *disabled* user is refused (409). A disabled account
cannot log in, so promoting it would create a phantom super-admin —
either operator error (typo in the user_id) or an attempt to plant a
dormant super-admin row for later activation. Re-enable the user
first, then promote.

Audit emission
──────────────
Every state-changing call emits one audit row under the actor's
chain:

  * ``super_admin_granted`` — before ``{role:<prev>}``, after
    ``{role:'super_admin'}``
  * ``super_admin_revoked`` — before ``{role:'super_admin'}``, after
    ``{role:'admin'}``

Idempotent no-op responses (already_super_admin / already_revoked) do
NOT emit audit — there was no state change to record. The 409 last-
super-admin path also does not emit audit (no mutation occurred).

Module-global state audit (SOP Step 1)
──────────────────────────────────────
None introduced. ``USER_ID_PATTERN`` / ``_USER_ID_RE`` /
``_DEMOTE_LOCK_KEY_NAME`` / 5 SQL constants / ``DEMOTION_TARGET_ROLE``
are all module-level immutable; each uvicorn worker derives the same
values from source. The asyncpg pool is shared via PG. The advisory
lock is PG-coordinated across workers (qualifying answer #2).

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Both endpoints read-then-write inside a single transaction. The
DELETE path additionally takes ``pg_advisory_xact_lock`` so concurrent
demotions serialise — losers re-count after winners commit and either
proceed or 409. The POST path's race surface is benign: two operators
promoting the same target collapse into a single state (both see
``role='super_admin'`` after either commit), so an explicit lock is
unnecessary.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-super-admins"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ``users.id`` is generated by ``auth.create_user`` as
# ``f"u-{uuid.uuid4().hex[:10]}"`` — i.e. ``u-`` + 10 lowercase hex
# chars. The accept handler also produces ``u-<token_hex(5)>`` (10
# lowercase hex) for invite-anonymous users. Pattern is permissive on
# length (4..64 trailing) so future schemes (longer uuids, prefixed
# external ids) keep working without code edits, but strictly anchored
# + lowercase to refuse uppercase / unanchored / control-char ids
# before they hit PG.
USER_ID_PATTERN = r"^u-[a-z0-9]{4,64}$"
_USER_ID_RE = re.compile(USER_ID_PATTERN)


def _is_valid_user_id(uid: str) -> bool:
    return bool(uid) and bool(_USER_ID_RE.match(uid))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Role a revoked super-admin lands on. ``admin`` is the next rank
# down on ``auth.ROLES`` — preserves tenant-admin reach (i.e. the
# operator can still manage their primary tenant if they were also a
# tenant-admin elsewhere) while stripping platform-tier authority.
DEMOTION_TARGET_ROLE = "admin"

# Identifier for the platform-wide pg_advisory_xact_lock taken during
# every demote tx. Stored as a string and converted via PG's
# ``hashtext`` builtin to a stable bigint inside the SQL itself.
_DEMOTE_LOCK_KEY_NAME = "omnisight_super_admin_demote"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PromoteSuperAdminRequest(BaseModel):
    """Body for ``POST /api/v1/admin/super-admins``."""

    user_id: str = Field(
        pattern=USER_ID_PATTERN,
        min_length=6,
        max_length=66,
        description="Target ``users.id``. Must match the standard "
                    "``^u-[a-z0-9]{4,64}$`` shape; pydantic returns "
                    "422 on anything else before the handler runs.",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Read-only fetch of the target user. Used by both endpoints to
# distinguish 404 / no-op / happy-path branches before the UPDATE.
# ``FOR UPDATE`` is added on the DELETE path inside the transactional
# wrapper (see ``_FETCH_USER_FOR_DEMOTE_SQL``) — POST does not need
# row-level locking because two concurrent promotions collapse to the
# same end state.
_FETCH_USER_SQL = """
SELECT id, email, name, role, enabled
FROM users
WHERE id = $1
"""

# Same projection but with a row lock. Used by the DELETE handler so
# concurrent demotes of the same target serialise.
_FETCH_USER_FOR_DEMOTE_SQL = """
SELECT id, email, name, role, enabled
FROM users
WHERE id = $1
FOR UPDATE
"""

# Atomic promotion. ``WHERE role != 'super_admin'`` makes the no-op
# path silent (no row touched, RETURNING empty) — the handler treats
# RETURNING None as "already super-admin" and returns the idempotent
# response. RETURNING projects the post-update row so the response
# body and the audit ``after`` payload share a single source of truth.
_PROMOTE_USER_SQL = """
UPDATE users
SET role = 'super_admin'
WHERE id = $1 AND role != 'super_admin'
RETURNING id, email, name, role, enabled
"""

# Atomic demotion to ``DEMOTION_TARGET_ROLE``. ``WHERE role = 'super_admin'``
# narrows so the no-op path (target already not super-admin) is silent.
_DEMOTE_USER_SQL = """
UPDATE users
SET role = $2
WHERE id = $1 AND role = 'super_admin'
RETURNING id, email, name, role, enabled
"""

# Count *enabled* super-admins other than the target. Disabled
# super-admins are ignored — they can't log in, so they don't preserve
# operator reach. Used by the last-super-admin floor check.
_COUNT_OTHER_ENABLED_SUPER_ADMINS_SQL = """
SELECT COUNT(*) AS n
FROM users
WHERE role = 'super_admin'
  AND enabled = 1
  AND id <> $1
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Endpoints
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/super-admins", status_code=200)
async def promote_super_admin(
    body: PromoteSuperAdminRequest,
    _request: Request,
    actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Promote a user to ``role='super_admin'``.

    Idempotent: if the target is already platform-tier the response
    has ``already_super_admin=True`` and no audit row is written.
    """
    # Defensive belt-and-braces — pydantic should already have rejected
    # this, but guard against future regex drift in the schema.
    if not _is_valid_user_id(body.user_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid user id: {body.user_id!r}; "
                               f"must match {USER_ID_PATTERN}"},
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchrow(_FETCH_USER_SQL, body.user_id)
            if cur is None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"user not found: {body.user_id!r}"},
                )

            if cur["role"] == "super_admin":
                # Idempotent no-op — surface state, do NOT emit audit.
                return JSONResponse(
                    status_code=200,
                    content={
                        "user_id": cur["id"],
                        "email": cur["email"],
                        "name": cur["name"],
                        "role": cur["role"],
                        "enabled": bool(cur["enabled"]),
                        "already_super_admin": True,
                    },
                )

            # Refuse to promote a disabled account. A super-admin row
            # that can't log in is a footgun: either operator typo or
            # an attempt to plant a dormant platform-tier account.
            if not bool(cur["enabled"]):
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            f"cannot promote disabled user "
                            f"{body.user_id!r} to super-admin; "
                            f"re-enable the account first."
                        ),
                        "user_id": body.user_id,
                        "enabled": False,
                    },
                )

            new_row = await conn.fetchrow(_PROMOTE_USER_SQL, body.user_id)

    if new_row is None:
        # Race: row vanished or flipped to super_admin between the
        # SELECT and UPDATE. Re-read to disambiguate.
        async with get_pool().acquire() as conn:
            still = await conn.fetchrow(_FETCH_USER_SQL, body.user_id)
        if still is None:
            return JSONResponse(
                status_code=404,
                content={"detail": f"user not found: {body.user_id!r}"},
            )
        return JSONResponse(
            status_code=200,
            content={
                "user_id": still["id"],
                "email": still["email"],
                "name": still["name"],
                "role": still["role"],
                "enabled": bool(still["enabled"]),
                "already_super_admin": still["role"] == "super_admin",
            },
        )

    before = {"role": cur["role"]}
    after = {"role": new_row["role"]}
    try:
        from backend import audit as _audit
        await _audit.log(
            action="super_admin_granted",
            entity_kind="user",
            entity_id=new_row["id"],
            before=before,
            after=after,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("super_admin_granted audit emit failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "user_id": new_row["id"],
            "email": new_row["email"],
            "name": new_row["name"],
            "role": new_row["role"],
            "enabled": bool(new_row["enabled"]),
            "already_super_admin": False,
        },
    )


@router.delete("/super-admins/{user_id}", status_code=200)
async def revoke_super_admin(
    user_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.require_super_admin),
) -> JSONResponse:
    """Demote a super-admin back to ``role='admin'``.

    Idempotent on a target that is already not super-admin (returns
    200 + ``already_revoked=True``). Refuses with 409 if the demotion
    would leave zero enabled super-admins (last-super-admin
    protection). Concurrent demotions are serialised by a platform-
    wide ``pg_advisory_xact_lock`` so two operators racing each other
    cannot collectively floor the count.
    """
    if not _is_valid_user_id(user_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid user id: {user_id!r}; "
                               f"must match {USER_ID_PATTERN}"},
        )

    from backend.db_pool import get_pool

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Platform-wide serialisation of demote attempts. Without
            # this, two concurrent demotes of distinct super-admins
            # both pass independent "other_count > 0" checks and both
            # commit, leaving zero. ``pg_advisory_xact_lock`` releases
            # automatically on tx commit/rollback.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _DEMOTE_LOCK_KEY_NAME,
            )

            cur = await conn.fetchrow(
                _FETCH_USER_FOR_DEMOTE_SQL, user_id,
            )
            if cur is None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"user not found: {user_id!r}"},
                )

            if cur["role"] != "super_admin":
                # Idempotent no-op — operator clicked revoke on a row
                # that was already not platform-tier. No audit row.
                return JSONResponse(
                    status_code=200,
                    content={
                        "user_id": cur["id"],
                        "email": cur["email"],
                        "name": cur["name"],
                        "role": cur["role"],
                        "enabled": bool(cur["enabled"]),
                        "already_revoked": True,
                    },
                )

            # Last-super-admin floor check. Only enforced when the
            # target itself is *enabled* — demoting a disabled
            # super-admin doesn't reduce the live operator count, so
            # it can proceed even if it would otherwise hit the floor.
            if bool(cur["enabled"]):
                other_count = int(await conn.fetchval(
                    _COUNT_OTHER_ENABLED_SUPER_ADMINS_SQL, user_id,
                ))
                if other_count == 0:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": (
                                f"refusing to revoke super-admin "
                                f"from {user_id!r}: this is the last "
                                f"enabled super-admin on the platform "
                                f"and demoting it would leave zero "
                                f"operators able to manage tenants. "
                                f"Promote another user first, then "
                                f"retry."
                            ),
                            "user_id": user_id,
                            "would_leave_zero_super_admins": True,
                            "other_enabled_super_admin_count": 0,
                        },
                    )

            new_row = await conn.fetchrow(
                _DEMOTE_USER_SQL, user_id, DEMOTION_TARGET_ROLE,
            )

    if new_row is None:
        # Race: target flipped out of super_admin between the
        # SELECT FOR UPDATE and the UPDATE — should not be reachable
        # because the row lock is held until tx commit, but treat as
        # idempotent no-op rather than 5xx.
        return JSONResponse(
            status_code=200,
            content={
                "user_id": cur["id"],
                "email": cur["email"],
                "name": cur["name"],
                "role": cur["role"],
                "enabled": bool(cur["enabled"]),
                "already_revoked": True,
            },
        )

    before = {"role": "super_admin"}
    after = {"role": new_row["role"]}
    try:
        from backend import audit as _audit
        await _audit.log(
            action="super_admin_revoked",
            entity_kind="user",
            entity_id=new_row["id"],
            before=before,
            after=after,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("super_admin_revoked audit emit failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "user_id": new_row["id"],
            "email": new_row["email"],
            "name": new_row["name"],
            "role": new_row["role"],
            "enabled": bool(new_row["enabled"]),
            "already_revoked": False,
        },
    )

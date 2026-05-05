"""WP.9.2 -- generic share permalink slug helpers.

``shareable_objects.share_id`` doubles as the external permalink slug.
This module keeps slug minting and collision-safe row creation in one
small helper so later WP.9 ACL / expiry / redaction rows can reuse it
without reimplementing permalink entropy.

Module-global state audit (SOP Step 1): constants, regex objects, and SQL
strings are immutable policy data.  No singleton, in-memory cache, or
cross-worker mutable state is introduced; every worker mints request-local
random slugs and PG arbitrates collisions via the primary key.

Read-after-write timing audit (SOP Step 1): row creation is one atomic
``INSERT ... ON CONFLICT DO NOTHING RETURNING``.  A collision retries with
a fresh slug in the same request; no downstream serial timing assumption
changes.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping


SHARE_SLUG_PREFIX = "sh-"
SHARE_SLUG_BYTES = 16
SHARE_SLUG_MAX_ATTEMPTS = 5
SHARE_SLUG_PATTERN = r"^sh-[A-Za-z0-9_-]{22}$"
_SHARE_SLUG_RE = re.compile(SHARE_SLUG_PATTERN)

OBJECT_KIND_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_OBJECT_KIND_RE = re.compile(OBJECT_KIND_PATTERN)

SHARE_VISIBILITIES = ("private", "team", "tenant", "public")
_SHARE_VISIBILITIES_SET = frozenset(SHARE_VISIBILITIES)

# ``team`` is the tenant's working set (owner/admin/member). ``tenant``
# additionally includes viewer, matching the existing tenant membership
# enum while still keeping the two share tiers distinct.
_TEAM_VISIBILITY_MEMBERSHIP_ROLES = frozenset({"owner", "admin", "member"})
_TENANT_VISIBILITY_MEMBERSHIP_ROLES = frozenset(
    {"owner", "admin", "member", "viewer"}
)


_INSERT_SHAREABLE_OBJECT_SQL = """
INSERT INTO shareable_objects (
    share_id, object_kind, object_id, tenant_id, owner_user_id,
    visibility, redaction_applied
) VALUES (
    $1, $2, $3, $4, $5, $6, $7::jsonb
)
ON CONFLICT (share_id) DO NOTHING
RETURNING share_id, object_kind, object_id, tenant_id, owner_user_id,
          visibility, expires_at, redaction_applied, created_at
"""

_FETCH_USER_TENANT_MEMBERSHIP_SQL = (
    "SELECT role, status FROM user_tenant_memberships "
    "WHERE user_id = $1 AND tenant_id = $2"
)


class ShareSlugCollisionError(RuntimeError):
    """Raised when random slug minting exhausts the retry budget."""


@dataclass(frozen=True)
class ShareableObject:
    """Created ``shareable_objects`` row projected for API callers."""

    share_id: str
    object_kind: str
    object_id: str
    tenant_id: str
    owner_user_id: str
    visibility: str
    expires_at: Any
    redaction_applied: Any
    created_at: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "share_id": self.share_id,
            "object_kind": self.object_kind,
            "object_id": self.object_id,
            "tenant_id": self.tenant_id,
            "owner_user_id": self.owner_user_id,
            "visibility": self.visibility,
            "expires_at": self.expires_at,
            "redaction_applied": self.redaction_applied,
            "created_at": self.created_at,
        }


def is_valid_share_slug(value: str) -> bool:
    """Return whether ``value`` matches the WP.9 permalink slug shape."""

    return bool(value) and bool(_SHARE_SLUG_RE.fullmatch(value))


def mint_share_slug() -> str:
    """Return ``sh-`` + 128 bits encoded as URL-safe base64.

    ``secrets.token_urlsafe(16)`` yields 22 URL-safe ASCII chars with no
    padding, keeping permalinks short while leaving guessing attacks far
    outside the practical search space.
    """

    return f"{SHARE_SLUG_PREFIX}{secrets.token_urlsafe(SHARE_SLUG_BYTES)}"


def _validate_object_kind(object_kind: str) -> None:
    if not _OBJECT_KIND_RE.fullmatch(object_kind):
        raise ValueError(
            "object_kind must match ^[a-z][a-z0-9_.-]{0,63}$",
        )


def validate_visibility(visibility: str) -> None:
    if visibility not in _SHARE_VISIBILITIES_SET:
        raise ValueError(
            "visibility must be one of private, team, tenant, public",
        )


def _share_field(share: ShareableObject | Mapping[str, Any], key: str) -> Any:
    if isinstance(share, ShareableObject):
        return getattr(share, key)
    return share[key]


def _row_to_shareable_object(row) -> ShareableObject:
    return ShareableObject(
        share_id=row["share_id"],
        object_kind=row["object_kind"],
        object_id=row["object_id"],
        tenant_id=row["tenant_id"],
        owner_user_id=row["owner_user_id"],
        visibility=row["visibility"],
        expires_at=row["expires_at"],
        redaction_applied=row["redaction_applied"],
        created_at=row["created_at"],
    )


async def create_shareable_object(
    conn,
    *,
    object_kind: str,
    object_id: str,
    tenant_id: str,
    owner_user_id: str,
    visibility: str = "private",
    redaction_applied: Mapping[str, Any] | None = None,
    max_attempts: int = SHARE_SLUG_MAX_ATTEMPTS,
) -> ShareableObject:
    """Insert a share row with a collision-checked permalink slug.

    The helper accepts only the four WP.9 ACL tiers; expiry and redaction
    policy decisions remain owned by WP.9.4-WP.9.5.  It mints a
    non-guessable slug and lets the database primary key atomically accept
    or reject it.
    """

    _validate_object_kind(object_kind)
    validate_visibility(visibility)
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    redaction_json = json.dumps(
        dict(redaction_applied or {}),
        sort_keys=True,
        separators=(",", ":"),
    )
    for _ in range(max_attempts):
        share_id = mint_share_slug()
        row = await conn.fetchrow(
            _INSERT_SHAREABLE_OBJECT_SQL,
            share_id,
            object_kind,
            object_id,
            tenant_id,
            owner_user_id,
            visibility,
            redaction_json,
        )
        if row is not None:
            return _row_to_shareable_object(row)

    raise ShareSlugCollisionError(
        "share_id collision retry budget exhausted; retry the request",
    )


async def user_can_access_shareable_object(
    conn,
    share: ShareableObject | Mapping[str, Any],
    *,
    caller_user_id: str | None,
    caller_role: str = "",
) -> bool:
    """Return whether the caller may read a share row.

    ACL source of truth mirrors the tenant-project visibility helpers:
    ``users.tenant_id`` is not trusted for access; non-public tiers read
    the active ``user_tenant_memberships`` row from PG.  The four levels
    are:

    * ``private`` -- owner only, plus platform super-admin.
    * ``team`` -- owner, super-admin, or active owner/admin/member.
    * ``tenant`` -- owner, super-admin, or any active tenant member.
    * ``public`` -- anyone with the permalink slug.
    """

    visibility = _share_field(share, "visibility")
    validate_visibility(visibility)
    if visibility == "public":
        return True

    if caller_role == "super_admin":
        return True
    if not caller_user_id:
        return False
    if caller_user_id == _share_field(share, "owner_user_id"):
        return True
    if visibility == "private":
        return False

    row = await conn.fetchrow(
        _FETCH_USER_TENANT_MEMBERSHIP_SQL,
        caller_user_id,
        _share_field(share, "tenant_id"),
    )
    if row is None or row["status"] != "active":
        return False

    if visibility == "team":
        return row["role"] in _TEAM_VISIBILITY_MEMBERSHIP_ROLES
    return row["role"] in _TENANT_VISIBILITY_MEMBERSHIP_ROLES

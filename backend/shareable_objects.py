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


_INSERT_SHAREABLE_OBJECT_SQL = """
INSERT INTO shareable_objects (
    share_id, object_kind, object_id, tenant_id, owner_user_id,
    redaction_applied
) VALUES (
    $1, $2, $3, $4, $5, $6::jsonb
)
ON CONFLICT (share_id) DO NOTHING
RETURNING share_id, object_kind, object_id, tenant_id, owner_user_id,
          visibility, expires_at, redaction_applied, created_at
"""


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
    redaction_applied: Mapping[str, Any] | None = None,
    max_attempts: int = SHARE_SLUG_MAX_ATTEMPTS,
) -> ShareableObject:
    """Insert a private share row with a collision-checked permalink slug.

    The helper deliberately leaves ACL level, expiry, and redaction policy
    decisions to WP.9.3-WP.9.5.  It only mints a non-guessable slug and
    lets the database primary key atomically accept or reject it.
    """

    _validate_object_kind(object_kind)
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
            redaction_json,
        )
        if row is not None:
            return _row_to_shareable_object(row)

    raise ShareSlugCollisionError(
        "share_id collision retry budget exhausted; retry the request",
    )

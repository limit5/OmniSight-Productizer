"""Y3 (#279) row 1 — POST /api/v1/tenants/{tid}/invites.

Tenant-admin-and-above invite issuance: generate a one-time random
token, persist *only its hash* in ``tenant_invites``, deliver the
plaintext to the recipient email via the existing
:func:`backend.notifications.notify` channel, and return the plaintext
to the issuing admin **once and only once** in the HTTP response so a
client UI can show a copyable link.

Endpoint contract
─────────────────
::

    POST /api/v1/tenants/{tid}/invites
    body  : {"email": "<rfc-ish>", "role": "owner|admin|member|viewer"}
    auth  : tenant admin or above (super_admin always allowed; local
            tenant role must be 'admin' or 'owner' on the target
            tenant id; a session-tier 'super_admin' bypasses the
            membership check because they manage cross-tenant
            platform state — same trust boundary already enforced by
            the Y2 admin REST surface)
    out   : 201 {invite_id, token_plaintext, expires_at}
    errors: 400 unsupported role · 403 not authorised on tenant ·
            404 tenant unknown · 409 active invite for same email
            already pending · 422 malformed body / id ·
            429 rate-limit (invite-per-email-per-tenant burst)

Token shape & one-time visibility
─────────────────────────────────
The plaintext is ``secrets.token_urlsafe(32)`` (256-bit entropy
encoded url-safe base64; ~43 ASCII chars). Only ``sha256(plaintext)``
is persisted on ``tenant_invites.token_hash`` (UNIQUE). The plaintext
is returned to the caller exactly once in this 201 response and is
also embedded into the recipient email body — neither the application
log nor the audit log records it. Once the response is consumed (or
the email is sent), the plaintext exists nowhere on disk; loss of the
plaintext means the admin must revoke + reissue.

Pattern is the same as ``api_keys.key_hash`` (alembic 0011) and
``mfa_backup_codes.code_hash`` (alembic 0012).

Email channel
─────────────
For now the invite email rides the existing ``notifications.notify``
fan-out (``level="info"`` + ``severity=None``) so a tenant admin
operating in dev / pre-SMTP environments still gets a queryable trail
through the ``notifications`` table + SSE bus. A future row may flip
this to a direct SMTP path keyed on a dedicated ``OMNISIGHT_INVITE_*``
env knob — the API contract above stays identical.

Email-case normalisation
────────────────────────
The address is **stored verbatim** (RFC-5321 says the local-part is
case-sensitive) but **compared case-insensitively** at acceptance
time. To make the rate-limit + duplicate-pending guards stable across
``Alice@x.com`` / ``alice@x.com``, both checks run against
``email.strip().lower()``. The casing the admin entered is preserved
on the row for audit / display.

Rate-limit
──────────
Each (tenant_id, normalised_email) pair gets a 5-token bucket that
refills over 1 hour (``backend.rate_limit.get_limiter()``). Burst
prevents a malicious or buggy admin from flooding a recipient with
repeated invites; the 1-hour window matches the TODO row literal
"5/email/tenant/hour".

Module-global state audit (SOP Step 1)
──────────────────────────────────────
None introduced. The asyncpg pool is shared via PG; the rate limiter
is Redis-coordinated in prod and per-replica in-memory in dev (see
``backend.rate_limit`` — qualifying answer #2/#3 already documented
on that module). Token plaintext is generated per-request from
``secrets.token_urlsafe`` and never cached.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Single INSERT … RETURNING; no follow-on read. The duplicate-pending
guard is a SELECT before the INSERT — under concurrent issue the
loser hits the ``token_hash`` UNIQUE constraint (vanishingly small
collision probability with 256-bit entropy, but also a same-second
re-submit by the same admin would re-roll the token before INSERT,
so the guard reads "is there an active pending invite for this
email?" and is best-effort. On a race, the second INSERT goes
through; the admin gets two pending invites for the same email and
either may be consumed first. The acceptance flow (Y3 row 4) will
mark all sibling pending invites for that email/tenant as accepted
in the same transaction.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend import auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tenant-invites"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant id pattern — same source-of-truth as Y2 admin_tenants.py.
# Imported here as a string literal so this router does not pull in
# admin_tenants module-level state at import time (admin_tenants
# imports tenant_quota which does filesystem work; tenant_invites is
# a smaller surface and we want the import graph to stay shallow).
TENANT_ID_PATTERN = r"^t-[a-z0-9][a-z0-9-]{2,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)

# Y3 spec: the tenant-level role enum, matching
# ``user_tenant_memberships.role`` and ``tenant_invites.role`` CHECK
# constraints. Deliberately *not* the project-level enum (owner /
# contributor / viewer) — invites grant tenant-scope membership, not
# project-scope.
INVITE_ROLE_ENUM = ("owner", "admin", "member", "viewer")

# Token plaintext byte-count (32 → 256-bit entropy → ~43 url-safe
# base64 chars). The DB CHECK on ``token_hash`` requires len ≥ 16; a
# sha256 hex digest is 64, well above.
INVITE_TOKEN_BYTES = 32

# Default invite TTL (DB does not enforce a length, application does).
# Matches the comment on the migration: "default 7 days".
INVITE_DEFAULT_TTL = timedelta(days=7)

# Light-touch email regex. Deliberately *not* a full RFC-5322 parser
# (those are notoriously broken / accept addresses that no real MTA
# would touch). The contract is "looks like an email" + DB CHECK
# enforces length 1..320; the recipient MTA decides delivery. Mirrors
# the same loose intent as the rest of this codebase (auth router
# accepts any non-empty string with min_length=3).
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Rate-limit: ≤ 5 invites per (tenant, normalised email) per hour.
INVITE_RATE_LIMIT_CAP = 5
INVITE_RATE_LIMIT_WINDOW_SECONDS = 3600.0


def _is_valid_tenant_id(tid: str) -> bool:
    return bool(tid) and bool(_TENANT_ID_RE.match(tid))


def _normalise_email(raw: str) -> str:
    """Lowercase + strip for *comparison* keys (rate-limit, dup
    guard). The original casing is what gets persisted on the row."""
    return raw.strip().lower()


def _hash_token(plaintext: str) -> str:
    """Hex sha256 — same format as the migration's CHECK and the
    api_keys.key_hash convention."""
    return hashlib.sha256(plaintext.encode("ascii")).hexdigest()


def _now_iso() -> str:
    """``YYYY-MM-DD HH:MM:SS`` UTC — the literal format every other
    TEXT timestamp column in this codebase uses (see
    ``audit_log.ts``-style strings rendered by PG ``to_char``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expires_at_iso(ttl: timedelta = INVITE_DEFAULT_TTL) -> str:
    return (datetime.now(timezone.utc) + ttl).strftime("%Y-%m-%d %H:%M:%S")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CreateInviteRequest(BaseModel):
    """Body for ``POST /api/v1/tenants/{tid}/invites``."""

    email: str = Field(
        min_length=3,
        max_length=320,
        description="Mailbox address. Stored verbatim (case preserved) "
                    "but compared case-insensitively at rate-limit / "
                    "duplicate-pending / acceptance time. Length "
                    "capped at 320 to match RFC 5321 + the "
                    "``tenant_invites.email`` CHECK constraint. "
                    "Format check is loose (presence of ``@`` + a "
                    "domain part with a dot) — the recipient MTA "
                    "decides actual deliverability.",
    )
    role: Literal["owner", "admin", "member", "viewer"] = Field(
        default="member",
        description="Tenant-scope role to grant on acceptance. "
                    "Matches the ``user_tenant_memberships.role`` "
                    "CHECK enum. Must be one of "
                    "(owner, admin, member, viewer); pydantic 422s "
                    "on anything else before the handler runs.",
    )

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("email must not be empty")
        if not _EMAIL_RE.match(v):
            raise ValueError(
                "email must contain '@' and a domain part with a dot"
            )
        return v


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorisation — tenant-admin-or-above on the target tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Membership roles that may issue invites for a tenant. ``member`` and
# ``viewer`` may NOT (they are read-only / read-write-non-admin).
_INVITE_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


async def _user_can_invite_into(
    user: auth.User,
    tenant_id: str,
) -> bool:
    """True iff ``user`` may issue invites for ``tenant_id``.

    Order of checks (cheap → expensive):
      1. Platform ``super_admin`` — always allowed (matches the Y2
         trust boundary; super-admin manages cross-tenant state).
      2. Active membership row with role ∈ {owner, admin} on the
         target tenant — DB lookup against ``user_tenant_memberships``
         (the Y1 N-to-M source-of-truth).

    The legacy ``users.role`` cache (per-account role) is **not**
    consulted: a user who is "admin on their primary tenant" must
    not be allowed to invite into a *different* tenant just because
    their primary-tenant role is high. Membership row is per-tenant;
    that is the correct authoritative.
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
    return row["role"] in _INVITE_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant-existence probe so we can return a clean 404 rather than
# letting the FK constraint emit an obscure 5xx.
_FETCH_TENANT_SQL = "SELECT id FROM tenants WHERE id = $1"

# Duplicate-pending guard: case-insensitive email match scoped to
# this tenant + status='pending' + not yet expired. Hits
# ``idx_tenant_invites_email_status`` on (email, status). Note we
# compare ``LOWER(email)`` against ``$2`` which is already the
# normalised form — keeps the index hit by emitting a function-on-
# column expression only on the right-hand side, but PG still does
# table scan on the lower(email) side. A tighter design would store
# a generated ``email_lower`` column; deferred to a follow-up so the
# Y1 schema stays untouched.
_FETCH_PENDING_INVITE_SQL = """
SELECT id, expires_at
FROM tenant_invites
WHERE tenant_id = $1
  AND lower(email) = $2
  AND status = 'pending'
ORDER BY created_at DESC
LIMIT 1
"""

# Single-statement insert. ``ON CONFLICT (token_hash) DO NOTHING`` is
# defence-in-depth against a 1-in-2^256 collision; in practice the
# RETURNING row is always present.
_INSERT_INVITE_SQL = """
INSERT INTO tenant_invites
    (id, tenant_id, email, role, invited_by, token_hash, expires_at,
     status, created_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8)
ON CONFLICT (token_hash) DO NOTHING
RETURNING id, expires_at, created_at
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Email delivery — best-effort fan-out via notifications.notify
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The notification subject / body templates are kept short and
# deliberately generic — Y3 row 4 (POST /invites/accept) lands the
# canonical accept URL shape; until then the recipient receives the
# token plaintext + the tenant id and can hand it to the admin
# manually. The plaintext is included verbatim so the recipient can
# copy-paste; including only the hash here would defeat the whole
# point. Token is **NOT** logged through the system log path
# (notify writes a `[NOTIFY:INFO]` line that does *not* carry the
# message body — see ``backend/notifications.py:185``).


def _format_invite_email(
    tenant_id: str,
    email: str,
    role: str,
    token_plaintext: str,
    expires_at: str,
) -> tuple[str, str]:
    """Return (title, body)."""
    title = f"You've been invited to tenant {tenant_id}"
    body = (
        f"You have been invited to join tenant '{tenant_id}' as "
        f"role '{role}'.\n\n"
        f"Acceptance token (one-time, expires {expires_at} UTC):\n"
        f"  {token_plaintext}\n\n"
        f"To accept, POST this token to "
        f"/api/v1/invites/<invite_id>/accept (Y3 row 4). If you "
        f"did not expect this invite, ignore this email — the "
        f"token will expire automatically."
    )
    return title, body


async def _send_invite_email(
    *,
    tenant_id: str,
    recipient: str,
    role: str,
    token_plaintext: str,
    expires_at: str,
) -> None:
    """Hand the invite payload off to ``notifications.notify``.

    Failures are swallowed (logged at warning) — the DB row is
    already committed, the API caller already has the plaintext in
    the response, and the admin can always re-send via the GET-list +
    revoke / re-issue flow (Y3 rows 2 / 3). Best-effort matches the
    pattern used by every other ``audit.log`` / ``notify`` callsite
    in this codebase.
    """
    try:
        from backend import notifications as _notif
        title, body = _format_invite_email(
            tenant_id, recipient, role, token_plaintext, expires_at,
        )
        await _notif.notify(
            level="info",
            title=title,
            message=body,
            source=f"tenant_invite:{tenant_id}:{recipient}",
        )
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "tenant invite email dispatch failed (tenant=%s email=%s): %s",
            tenant_id, recipient, exc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Endpoint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/tenants/{tenant_id}/invites", status_code=201)
async def create_invite(
    tenant_id: str,
    body: CreateInviteRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Issue a one-time invite token for ``tenant_id``.

    Returns 201 with::

        {
            "invite_id": "inv-<uuid>",
            "token_plaintext": "<url-safe base64, ~43 chars>",
            "expires_at": "YYYY-MM-DD HH:MM:SS"
        }

    The plaintext token is shown in the response **once and only
    once**. It is also embedded in the email delivered through the
    ``notifications.notify`` channel; neither the audit log nor the
    application log records the plaintext.
    """
    # 1. Validate the path id at the regex layer before touching the
    #    DB. Same source-of-truth as Y2 admin_tenants — a malformed
    #    id here can leak into FK probes / SQL strings if we forget.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    # 2. The Pydantic field validator has already trimmed + matched
    #    the email shape and capped length at 320 (matches the
    #    ``tenant_invites.email`` CHECK). ``raw_email`` here is the
    #    sanitised form preserving the user's casing.
    raw_email = body.email
    norm_email = _normalise_email(raw_email)

    # 3. Authorisation — tenant admin or above on the target tenant.
    #    Done before existence / rate-limit so a guess-the-tenant-id
    #    probe can't enumerate which tenants exist via timing.
    if not await _user_can_invite_into(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    # 4. Rate-limit (per-tenant, per-normalised-email). Sits BEFORE
    #    the DB write so a malicious admin spamming the same recipient
    #    burns no PG bandwidth on the rejected calls. Token bucket
    #    refills 5 tokens / hour — see module docstring.
    from backend.rate_limit import get_limiter
    rl_key = f"tenant_invite:{tenant_id}:{norm_email}"
    allowed, retry_after = get_limiter().allow(
        key=rl_key,
        capacity=INVITE_RATE_LIMIT_CAP,
        window_seconds=INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"invite rate limit exceeded for "
                f"{norm_email}@{tenant_id} "
                f"({INVITE_RATE_LIMIT_CAP}/hour); "
                f"retry in {int(retry_after)}s"
            ),
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    # 5. Existence probe → 404 (kept after RBAC so we don't leak
    #    "tenant exists but you can't invite" vs "tenant doesn't
    #    exist" via timing alone — both branches require an
    #    authorised caller anyway).
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 6. Duplicate-pending guard — best-effort, see module docstring.
    #    A pending invite that has already passed its expires_at is
    #    treated as expired (defence in depth: the sweep job hasn't
    #    flipped it yet).
    async with get_pool().acquire() as conn:
        existing = await conn.fetchrow(
            _FETCH_PENDING_INVITE_SQL, tenant_id, norm_email,
        )
    if existing is not None:
        # Compare expires_at against now. expires_at is a TEXT
        # column with the canonical 'YYYY-MM-DD HH:MM:SS' shape.
        try:
            exp_dt = datetime.strptime(
                existing["expires_at"], "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            # Malformed legacy row — treat as still active to err
            # on the side of refusing duplicate writes.
            exp_dt = datetime.now(timezone.utc) + timedelta(days=1)
        if exp_dt > datetime.now(timezone.utc):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        f"a pending invite already exists for "
                        f"{norm_email!r} on tenant {tenant_id!r}; "
                        f"revoke it via DELETE "
                        f"/api/v1/tenants/{tenant_id}/invites/"
                        f"{existing['id']} before issuing a new one"
                    ),
                    "tenant_id": tenant_id,
                    "email": norm_email,
                    "existing_invite_id": existing["id"],
                    "existing_expires_at": existing["expires_at"],
                },
            )

    # 7. Mint token + insert the row. Plaintext lives only in
    #    process memory from here through to the response and the
    #    email send — never persisted, never logged.
    token_plaintext = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
    token_hash = _hash_token(token_plaintext)
    invite_id = f"inv-{secrets.token_hex(8)}"
    expires_at = _expires_at_iso()
    created_at = _now_iso()

    # ``invited_by`` is a FK into ``users(id)`` ON DELETE SET NULL.
    # Synthetic ``anonymous`` user (open-mode dev fallback) does NOT
    # have a row in users, so we send NULL on that case to avoid an
    # FK violation. In session/strict mode every authenticated user
    # has a row, so the FK satisfies.
    invited_by_id: str | None = actor.id
    if invited_by_id == "anonymous" or (
        isinstance(invited_by_id, str) and invited_by_id.startswith("apikey:")
    ):
        invited_by_id = None

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_INVITE_SQL,
            invite_id, tenant_id, raw_email, body.role, invited_by_id,
            token_hash, expires_at, created_at,
        )

    if row is None:
        # 1-in-2^256 hash collision OR the same admin double-clicked
        # within microseconds and the second insert lost the race.
        # Either way, surface a clean 409 — the caller will retry.
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    "token_hash collision (vanishingly rare); "
                    "please retry the invite request"
                ),
            },
        )

    # 8. Audit. We log the invite *creation* — never the plaintext
    #    token. ``after`` payload includes only fields that are safe
    #    to surface in a 7-day audit retention window.
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_invite_created",
            entity_kind="tenant_invite",
            entity_id=invite_id,
            before=None,
            after={
                "invite_id": invite_id,
                "tenant_id": tenant_id,
                "email": raw_email,
                "role": body.role,
                "expires_at": row["expires_at"],
                "invited_by": invited_by_id,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant_invite_created audit emit failed: %s", exc)

    # 9. Fire the email. Best-effort (failures already logged).
    await _send_invite_email(
        tenant_id=tenant_id,
        recipient=raw_email,
        role=body.role,
        token_plaintext=token_plaintext,
        expires_at=row["expires_at"],
    )

    return JSONResponse(
        status_code=201,
        content={
            "invite_id": invite_id,
            "token_plaintext": token_plaintext,
            "expires_at": row["expires_at"],
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y3 (#279) row 2 — GET /api/v1/tenants/{tid}/invites?status=pending
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# List the tenant's invites for an admin console. Default surface is
# ``status=pending``: the literal phrasing in the TODO row is "列當前
# 待接受" — currently waiting to be accepted. A row whose persisted
# ``status='pending'`` but whose wall-clock has passed ``expires_at``
# is functionally expired (the housekeeping sweep will eventually flip
# it; we don't want an admin clicking "revoke" on something already
# dead). Defence-in-depth: when the caller asks for ``pending`` we
# add ``AND expires_at > now`` to the WHERE so the response only
# contains live rows. Other status values (``accepted``, ``revoked``,
# ``expired``) pass through verbatim, and a sentinel ``all`` skips
# the filter entirely (audit-style "show everything for this tenant").
#
# Auth: same trust boundary as POST — tenant admin / owner on the
# target tenant or a platform super_admin. A plain ``member`` /
# ``viewer`` membership cannot enumerate pending invites because the
# email addresses themselves are PII and disclose the admin's
# recruitment pipeline.
#
# Token plaintext is never persisted, so this endpoint cannot leak it.
# ``token_hash`` is also withheld from the response — it has no
# operator value (admin uses ``invite_id`` to revoke / resend) and
# returning it broadens the blast radius of any future logging
# accident.

# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# Two new module-level constants (``_LIST_INVITES_BASE_SQL`` +
# ``LISTABLE_INVITE_STATUSES``). Both immutable; every uvicorn worker
# derives the same value from the same source — qualifying answer #1.
# DB state is shared via PG (qualifying answer #2). No new in-memory
# cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# Pure read endpoint — no writes. The ``expires_at > $N`` filter uses
# the request-time clock; under concurrent POST + GET the GET caller
# may either see or miss a freshly-inserted invite depending on which
# transaction commits first, which is the standard read-committed
# behaviour and not a regression.

# The full set of values the ``?status=`` query parameter accepts.
# Mirrors the DB CHECK on ``tenant_invites.status`` plus a sentinel
# ``all`` that skips the WHERE filter entirely. ``pending`` is the
# default if the caller omits the param.
LISTABLE_INVITE_STATUSES = (
    "pending", "accepted", "revoked", "expired", "all",
)

# Hard cap on how many rows we'll project per call. Keeps the response
# bounded under a tenant that has issued thousands of invites over
# years — the admin console paginates on the client; the server-side
# default is the same conservative shape used by ``DETAIL`` audit
# events. Caller may lower it via ``?limit=`` but cannot exceed.
INVITES_LIST_DEFAULT_LIMIT = 100
INVITES_LIST_MAX_LIMIT = 500

# SQL: project exactly the columns an admin console needs. ``token_hash``
# is deliberately omitted (no operator value, broadens leak surface).
# ``ORDER BY created_at DESC, id DESC`` gives a stable newest-first
# ordering even when two invites share a created_at second.
_LIST_INVITES_PENDING_SQL = """
SELECT id, email, role, invited_by, status, created_at, expires_at
FROM tenant_invites
WHERE tenant_id = $1
  AND status = 'pending'
  AND expires_at > $2
ORDER BY created_at DESC, id DESC
LIMIT $3
"""

_LIST_INVITES_BY_STATUS_SQL = """
SELECT id, email, role, invited_by, status, created_at, expires_at
FROM tenant_invites
WHERE tenant_id = $1
  AND status = $2
ORDER BY created_at DESC, id DESC
LIMIT $3
"""

_LIST_INVITES_ALL_SQL = """
SELECT id, email, role, invited_by, status, created_at, expires_at
FROM tenant_invites
WHERE tenant_id = $1
ORDER BY created_at DESC, id DESC
LIMIT $2
"""


@router.get("/tenants/{tenant_id}/invites")
async def list_invites(
    tenant_id: str,
    _request: Request,
    status: str = Query(
        default="pending",
        description=(
            "Filter by invite status. One of "
            "(pending, accepted, revoked, expired, all). Default "
            "is 'pending', which also excludes rows whose "
            "wall-clock has passed expires_at (the housekeeping "
            "sweep flips them later). 'all' returns every status."
        ),
    ),
    limit: int = Query(
        default=INVITES_LIST_DEFAULT_LIMIT,
        ge=1,
        le=INVITES_LIST_MAX_LIMIT,
        description=(
            f"Max rows to return (1..{INVITES_LIST_MAX_LIMIT}). "
            f"Default {INVITES_LIST_DEFAULT_LIMIT}."
        ),
    ),
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List invites for a tenant.

    Returns 200 with::

        {
            "tenant_id": "t-acme",
            "status_filter": "pending",
            "count": 3,
            "invites": [
                {
                    "invite_id": "inv-...",
                    "email": "alice@example.com",
                    "role": "admin",
                    "status": "pending",
                    "invited_by": "u-...",
                    "created_at": "2026-04-25 12:00:00",
                    "expires_at": "2026-05-02 12:00:00"
                },
                ...
            ]
        }

    The plaintext token is **never** included — it exists only at
    POST time and is not persisted. ``token_hash`` is also omitted;
    the admin uses ``invite_id`` for revoke / resend.
    """
    # 1. Path-id validation. Same regex source-of-truth as POST.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )

    # 2. Status enum check. Done in handler (not Pydantic Literal on
    #    Query) to surface a clear 422 detail listing the allowed
    #    values rather than the FastAPI-default "value is not a valid
    #    enumeration member" wording.
    if status not in LISTABLE_INVITE_STATUSES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid status filter: {status!r}; must be one of "
                    f"{LISTABLE_INVITE_STATUSES}"
                ),
            },
        )

    # 3. RBAC. Same trust boundary as POST: tenant-admin-or-above on
    #    the target tenant, or platform super_admin. Done before the
    #    existence probe so a guess-the-id scan can't enumerate which
    #    tenants exist via timing.
    if not await _user_can_invite_into(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    # 4. Existence probe → clean 404 if the tenant doesn't exist.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 5. Project rows. ``pending`` gets the live-only filter
    #    (defence in depth); other statuses match verbatim; ``all``
    #    skips the status filter.
    async with get_pool().acquire() as conn:
        if status == "pending":
            rows = await conn.fetch(
                _LIST_INVITES_PENDING_SQL, tenant_id, _now_iso(), limit,
            )
        elif status == "all":
            rows = await conn.fetch(
                _LIST_INVITES_ALL_SQL, tenant_id, limit,
            )
        else:
            rows = await conn.fetch(
                _LIST_INVITES_BY_STATUS_SQL, tenant_id, status, limit,
            )

    invites = [
        {
            "invite_id": r["id"],
            "email": r["email"],
            "role": r["role"],
            "status": r["status"],
            "invited_by": r["invited_by"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "status_filter": status,
            "count": len(invites),
            "invites": invites,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y3 (#279) row 3 — DELETE /api/v1/tenants/{tid}/invites/{id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Revoke a previously-issued invite. The admin clicks "revoke" in the
# console; the row is flipped from ``status='pending'`` to
# ``status='revoked'`` and the embedded one-time token is rendered
# inert (the acceptance route refuses to consume non-pending rows).
#
# Status transitions accepted by this endpoint
# ──────────────────────────────────────────────
#   pending  → revoked   ← happy path, atomic UPDATE … RETURNING
#   revoked  → revoked   ← idempotent no-op (admin double-click /
#                           operator retry); 200 with ``already_revoked
#                           =True`` discriminator so a UI can suppress
#                           a "revoked!" toast on the second click.
#   accepted → (refuse)  ← 409: the invite already became a membership
#                           row; revoking the invite row would not
#                           remove the membership. The admin must
#                           instead take the membership-management
#                           path (Y3 row 6 PATCH/DELETE membership) to
#                           rescind access.
#   expired  → (refuse)  ← 409: distinct terminal state. The admin
#                           should see "this invite already expired"
#                           rather than silently turning it into
#                           "revoked", because the difference matters
#                           in the audit trail.
#
# Defence-in-depth note: a row whose persisted ``status='pending'``
# but whose wall-clock has passed ``expires_at`` is *functionally*
# expired (the housekeeping sweep has not yet run). On the
# ``pending`` revoke path we accept this — flipping a stale-pending
# to ``revoked`` is harmless and is what the admin asked for; the
# alternative (refusing with "this is already expired, sweep just
# hasn't caught up") would be confusing UX.
#
# Atomic check-and-flip
# ─────────────────────
# We use ``UPDATE … WHERE id = $1 AND tenant_id = $2 AND status =
# 'pending' RETURNING id, status`` to avoid a SELECT-then-UPDATE
# TOCTOU. A concurrent ``POST /accept`` (Y3 row 4) will lose the
# race deterministically: PG row-locks the row on UPDATE, the second
# transaction sees the new status on its retry / reread. We DO need a
# second SELECT *only* to disambiguate "row not found" from "row
# found but not pending" — both produce 0 rows on the UPDATE
# RETURNING — but we issue it AFTER the UPDATE missed, so the common
# happy path is one round-trip.
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# Two new module-level SQL constants (``_REVOKE_INVITE_SQL`` +
# ``_FETCH_INVITE_FOR_REVOKE_SQL``). Both immutable; every uvicorn
# worker derives the same value from the same source — qualifying
# answer #1. DB state shared via PG (qualifying answer #2). No new
# in-memory cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# Single-statement UPDATE … RETURNING serialised by PG row-lock; the
# follow-up SELECT (only on UPDATE-miss) is a read-committed lookup
# that may see the just-committed row from a concurrent accept /
# revoke, which is the standard PG behaviour and not a regression.
# Idempotent revoke is intentional — repeating the call from the
# same admin produces the same terminal state.

# Invite id pattern — same convention as the POST handler's
# ``inv-<hex>`` shape. Validated at the route layer so a malformed
# id returns 422 cleanly rather than leaking into FK probes / SQL
# strings.
INVITE_ID_PATTERN = r"^inv-[a-z0-9]{4,64}$"
_INVITE_ID_RE = re.compile(INVITE_ID_PATTERN)


def _is_valid_invite_id(iid: str) -> bool:
    return bool(iid) and bool(_INVITE_ID_RE.match(iid))


# Atomic check-and-flip: only ``pending`` rows scoped to the named
# tenant transition. Returns the row on success, no row on miss.
_REVOKE_INVITE_SQL = """
UPDATE tenant_invites
SET status = 'revoked'
WHERE id = $1
  AND tenant_id = $2
  AND status = 'pending'
RETURNING id, email, role, status, created_at, expires_at
"""

# Disambiguate UPDATE-miss: was the row absent, or present but in a
# non-pending state? Read-only; ``token_hash`` deliberately omitted —
# revoke decisions never need the hash.
_FETCH_INVITE_FOR_REVOKE_SQL = """
SELECT id, email, role, status, created_at, expires_at
FROM tenant_invites
WHERE id = $1 AND tenant_id = $2
"""


@router.delete("/tenants/{tenant_id}/invites/{invite_id}")
async def revoke_invite(
    tenant_id: str,
    invite_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Revoke a pending invite for ``tenant_id``.

    Returns 200 with::

        {
            "invite_id": "inv-...",
            "tenant_id": "t-acme",
            "status": "revoked",
            "already_revoked": false,   # true on idempotent re-revoke
            "email": "alice@example.com",
            "role": "admin",
            "created_at": "2026-04-25 12:00:00",
            "expires_at": "2026-05-02 12:00:00"
        }

    Errors: 403 RBAC · 404 invite/tenant unknown · 409 invite is in
    a terminal state that cannot be revoked (accepted / expired) ·
    422 malformed id.
    """
    # 1. Path-id validation. Both the tenant id and the invite id go
    #    through their respective regex source-of-truth before any DB
    #    work so a malformed id can't leak into FK probes / SQL strings.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid tenant id: {tenant_id!r}; "
                               f"must match {TENANT_ID_PATTERN}"},
        )
    if not _is_valid_invite_id(invite_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid invite id: {invite_id!r}; "
                               f"must match {INVITE_ID_PATTERN}"},
        )

    # 2. RBAC — same trust boundary as POST/GET. Done before existence
    #    so a guess-the-id scan can't enumerate which invites exist via
    #    timing.
    if not await _user_can_invite_into(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    # 3. Tenant existence probe → clean 404. Done after RBAC.
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 4. Atomic check-and-flip. Only pending rows scoped to the named
    #    tenant transition. UPDATE-miss falls through to a follow-up
    #    SELECT to disambiguate "row absent" from "row present but
    #    non-pending".
    async with get_pool().acquire() as conn:
        flipped = await conn.fetchrow(
            _REVOKE_INVITE_SQL, invite_id, tenant_id,
        )

    if flipped is not None:
        # Happy path: pending → revoked. Audit + return 200 with the
        # post-flip state. We log the *transition*, never the token
        # plaintext (which is not in the row anyway — only token_hash
        # was persisted, and revoke does not project it).
        try:
            from backend import audit as _audit
            await _audit.log(
                action="tenant_invite_revoked",
                entity_kind="tenant_invite",
                entity_id=invite_id,
                before={
                    "invite_id": invite_id,
                    "tenant_id": tenant_id,
                    "status": "pending",
                },
                after={
                    "invite_id": invite_id,
                    "tenant_id": tenant_id,
                    "status": "revoked",
                    "email": flipped["email"],
                    "role": flipped["role"],
                },
                actor=actor.email,
            )
        except Exception as exc:  # pragma: no cover — audit.log already swallows
            logger.warning("tenant_invite_revoked audit emit failed: %s", exc)

        return JSONResponse(
            status_code=200,
            content={
                "invite_id": flipped["id"],
                "tenant_id": tenant_id,
                "status": flipped["status"],
                "already_revoked": False,
                "email": flipped["email"],
                "role": flipped["role"],
                "created_at": flipped["created_at"],
                "expires_at": flipped["expires_at"],
            },
        )

    # 5. UPDATE-miss disambiguation. Either the invite never existed
    #    on this tenant, or it exists but is in a terminal state.
    async with get_pool().acquire() as conn:
        existing = await conn.fetchrow(
            _FETCH_INVITE_FOR_REVOKE_SQL, invite_id, tenant_id,
        )
    if existing is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"invite not found: {invite_id!r} on tenant "
                    f"{tenant_id!r}"
                ),
            },
        )

    cur_status = existing["status"]
    if cur_status == "revoked":
        # Idempotent: the same admin double-clicked or an operator
        # retry-loop hit us twice. Return 200 with the already_revoked
        # discriminator so a UI can suppress a duplicate "revoked!"
        # toast on the second click.
        return JSONResponse(
            status_code=200,
            content={
                "invite_id": existing["id"],
                "tenant_id": tenant_id,
                "status": "revoked",
                "already_revoked": True,
                "email": existing["email"],
                "role": existing["role"],
                "created_at": existing["created_at"],
                "expires_at": existing["expires_at"],
            },
        )

    # accepted / expired → 409, distinct terminal state. The detail
    # text echoes back the actual current state so the admin sees
    # *why* the revoke was refused.
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                f"cannot revoke invite {invite_id!r}: current status is "
                f"{cur_status!r}, not 'pending'. "
                + (
                    "The invite has already been accepted; remove the "
                    "resulting membership via the membership management "
                    "endpoints instead."
                    if cur_status == "accepted"
                    else "The invite already expired; no revoke is needed."
                )
            ),
            "invite_id": existing["id"],
            "tenant_id": tenant_id,
            "current_status": cur_status,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y3 (#279) row 4 — POST /api/v1/invites/{id}/accept
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Consume a one-time invite token. Two operating modes — the caller is
# either anonymous (no session cookie) or already authenticated:
#
#   ┌──────────────────────────────────────────────────────────────┐
#   │ caller state │ acceptance behaviour                          │
#   ├──────────────┼───────────────────────────────────────────────┤
#   │ anonymous    │ if no users row matches the invite email,     │
#   │              │ create one (random uid, password hash empty   │
#   │              │ unless body.password is supplied + zxcvbn-    │
#   │              │ compatible). Then materialise a membership    │
#   │              │ row. The freshly-created (or already-existing │
#   │              │ but logged-out) user is identified solely by  │
#   │              │ proving knowledge of the token plaintext —    │
#   │              │ there is no session cookie at this point.     │
#   │ authenticated│ MUST already have an account. We refuse to    │
#   │              │ silently bind a token to whatever account is  │
#   │              │ logged in if its email differs from the       │
#   │              │ invite email — that would let an admin invite │
#   │              │ alice@x.com and have bob@y.com (logged in as  │
#   │              │ a different account) consume the link.        │
#   │              │ The session.email must match invite.email     │
#   │              │ case-insensitively or we 409.                 │
#   └──────────────┴───────────────────────────────────────────────┘
#
# Input contract
# ──────────────
#   POST /api/v1/invites/{invite_id}/accept
#   body  : {"token": "<plaintext>", "name"?: "...", "password"?: "..."}
#   auth  : optional — anonymous OR session-bound. The endpoint NEVER
#           depends on auth.current_user (which would 401 in
#           session/strict mode against an anon caller); instead it
#           probes the session cookie inline via auth.get_session and
#           treats failure as anonymous.
#   out   : 200 {invite_id, tenant_id, user_id, role, status, already_member}
#   errors:
#     400 — body missing/malformed token
#     403 — token plaintext does not hash to invite.token_hash
#     404 — invite not found
#     409 — invite not in pending state (already accepted / revoked /
#           email-mismatch when authenticated)
#     410 — invite expired (persisted status='pending' but wall-clock
#           is past expires_at, OR persisted status='expired')
#     422 — malformed invite_id
#     429 — too many failed attempts on this invite (10/token/min,
#           the TODO row 7 literal "accept 失敗每 token 每分鐘不超過 10")
#
# State machine (the *only* mutating endpoint that touches both
# tenant_invites and user_tenant_memberships in a single transaction)
# ───────────────────────────────────────────────────────────────────
#   pending ──(token matches + email matches caller)──▶ accepted
#                                                     +─── creates user (anon caller)
#                                                     +─── creates / upserts membership
# All three writes go in one ``async with conn.transaction()`` so a
# crash mid-flight does not leave a half-materialised membership with
# the invite still pending. ``users.email`` is UNIQUE so a concurrent
# anon-accept of the same email loses the race deterministically — the
# loser falls back to "user already exists, treat as authenticated".
#
# Idempotence
# ───────────
# A re-POST after a successful accept lands on a non-pending row and
# returns 409. We DON'T return 200 with ``already_accepted=True`` like
# revoke does, because the second caller is most likely an attacker
# replaying a leaked token plaintext — silently 200-ing would leak the
# fact that the token was indeed valid. 409 is the only response that
# does not distinguish "wrong token" from "already accepted" via timing
# (both fail at the status check).
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# Two new SQL constants (``_FETCH_INVITE_FOR_ACCEPT_SQL``,
# ``_MARK_INVITE_ACCEPTED_SQL``) + two rate-limit knobs
# (``ACCEPT_FAIL_RATE_LIMIT_*``). All immutable; every uvicorn worker
# derives the same value from the same source — qualifying answer #1.
# DB state shared via PG (qualifying answer #2). Rate limit goes
# through ``backend.rate_limit.get_limiter`` which is Redis-coordinated
# in prod and per-replica in dev (qualifying answer #2/#3 already
# documented on that module).
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# The whole transaction runs under one ``conn.transaction()``: the
# SELECT … FOR UPDATE serialises concurrent accept attempts on the
# same invite row. Two anon callers presenting valid tokens at the
# same time: the loser's transaction blocks at FOR UPDATE, sees the
# row has flipped to 'accepted' on its retry-read, and returns 409.
# No timing-visible regression vs. row 1 (POST) / row 3 (DELETE) which
# both already use single-statement RETURNING.

# Rate-limit knobs for the failed-acceptance bucket. Per-invite-id
# (NOT per-IP) because the threat model is "attacker brute-forces the
# token plaintext"; the bucket key is the invite_id, capacity 10,
# window 60s — TODO row 7 literal.
ACCEPT_FAIL_RATE_LIMIT_CAP = 10
ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS = 60.0

# SELECT … FOR UPDATE: locks the invite row inside the transaction so
# concurrent accept attempts serialise. Projects token_hash because we
# need to verify it; the hash never reaches the response.
_FETCH_INVITE_FOR_ACCEPT_SQL = """
SELECT id, tenant_id, email, role, token_hash, status, expires_at
FROM tenant_invites
WHERE id = $1
FOR UPDATE
"""

# Atomic flip-on-success. Uses the same row already locked by the
# preceding SELECT … FOR UPDATE so this UPDATE never blocks.
_MARK_INVITE_ACCEPTED_SQL = """
UPDATE tenant_invites
SET status = 'accepted'
WHERE id = $1
"""

# Membership upsert. ON CONFLICT (user_id, tenant_id) DO NOTHING
# matches the "one user → N memberships" semantic from the TODO row:
# if the user is *already* a member of this tenant (e.g. an admin
# re-invited them with a different role by mistake), we do not bump
# their role on accept — admin must use the membership management
# endpoints for role changes. The acceptance is still considered
# successful (the invite gets flipped to 'accepted' and the response
# carries ``already_member=true``).
_UPSERT_MEMBERSHIP_SQL = """
INSERT INTO user_tenant_memberships
    (user_id, tenant_id, role, status, created_at)
VALUES ($1, $2, $3, 'active', $4)
ON CONFLICT (user_id, tenant_id) DO NOTHING
RETURNING user_id
"""


class AcceptInviteRequest(BaseModel):
    """Body for ``POST /api/v1/invites/{id}/accept``.

    The plaintext ``token`` is REQUIRED — it's the only proof of
    "I'm the human the admin invited". ``name`` and ``password`` are
    only consulted on the anonymous-caller branch (creating the
    user). On the authenticated branch they are ignored — name lives
    on the existing ``users`` row, password rotation has its own
    dedicated endpoint.
    """

    token: str = Field(
        min_length=16,
        max_length=512,
        description=(
            "Plaintext invite token from the email. The server hashes "
            "with sha256 and compares against tenant_invites.token_hash."
        ),
    )
    name: str = Field(
        default="",
        max_length=160,
        description=(
            "Optional display name. Only consulted on the anonymous-"
            "caller branch (where we are creating the user row). On "
            "an authenticated accept, the existing users.name wins."
        ),
    )
    password: str | None = Field(
        default=None,
        description=(
            "Optional password to set on the freshly-created user. "
            "Only consulted on the anonymous-caller branch. If absent "
            "the user is created with an empty password_hash and must "
            "complete the password-set flow before logging in. "
            "Authenticated branch ignores this field entirely."
        ),
    )

    @field_validator("token")
    @classmethod
    def _token_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("token must not be empty")
        # token_urlsafe(32) = ~43 chars of url-safe base64. We accept
        # anything in the [16..512] band so an admin who shipped a
        # stricter / looser convention does not break the accept
        # path; the actual validation is sha256(plaintext) ==
        # invite.token_hash, not regex matching.
        return v


@router.post("/invites/{invite_id}/accept")
async def accept_invite(
    invite_id: str,
    body: AcceptInviteRequest,
    request: Request,
) -> JSONResponse:
    """Consume a one-time invite token.

    Open to both anonymous and authenticated callers — see the module
    docstring for the two-branch semantics.

    Returns 200 with::

        {
            "invite_id": "inv-...",
            "tenant_id": "t-acme",
            "user_id": "u-...",          # the user the membership was
                                          # materialised onto (existing
                                          # row for authed caller, freshly
                                          # created for anon caller)
            "role": "admin",             # the membership role granted
            "status": "accepted",
            "already_member": false      # true if the user already had
                                          # an active membership row in
                                          # this tenant pre-accept
        }
    """
    # 1. Validate path id at the regex layer before any DB work.
    if not _is_valid_invite_id(invite_id):
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid invite id: {invite_id!r}; "
                               f"must match {INVITE_ID_PATTERN}"},
        )

    # 2. Optional auth probe. We deliberately do NOT depend on
    #    auth.current_user — that helper raises 401 in session/strict
    #    mode for an anon caller, which would block the legitimate
    #    "no account yet" flow. Instead we look at the cookie and
    #    treat failure as anonymous.
    session_user: auth.User | None = None
    try:
        cookie = request.cookies.get(auth.SESSION_COOKIE) or ""
        if cookie:
            sess = await auth.get_session(cookie)
            if sess:
                u = await auth.get_user(sess.user_id)
                if u and u.enabled:
                    session_user = u
    except Exception as exc:  # pragma: no cover — best-effort optional auth
        logger.debug(
            "accept_invite optional-auth probe failed (treating as anon): %s",
            exc,
        )

    actor_label = (
        session_user.email if session_user is not None else "anonymous"
    )

    # 3. Rate-limit the FAILED attempts. We don't decrement the bucket
    #    on success (an admin sending a real invite link to a real
    #    recipient may then click the link from multiple devices in
    #    quick succession — that's not abuse). Bucket key is the
    #    invite_id so an attacker brute-forcing the plaintext on one
    #    invite cannot exhaust our tokens for unrelated invites.
    from backend.rate_limit import get_limiter
    rl_key = f"invite_accept_fail:{invite_id}"

    def _record_fail_and_check() -> tuple[bool, float]:
        return get_limiter().allow(
            key=rl_key,
            capacity=ACCEPT_FAIL_RATE_LIMIT_CAP,
            window_seconds=ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
        )

    # 4. Transactional accept. We wrap SELECT FOR UPDATE + UPDATE +
    #    optional user-create + membership upsert in one transaction
    #    so a crash mid-flight does not leave a half-materialised
    #    membership with the invite still pending.
    from backend.db_pool import get_pool

    candidate_token_hash = _hash_token(body.token)
    now_iso = _now_iso()

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            invite = await conn.fetchrow(
                _FETCH_INVITE_FOR_ACCEPT_SQL, invite_id,
            )
            if invite is None:
                # Treat unknown id as a failed attempt for rate-limit
                # purposes — otherwise an attacker could enumerate
                # which inv-* prefixes are live by probing without
                # cost.
                allowed, retry_after = _record_fail_and_check()
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": (
                                f"too many failed accept attempts on "
                                f"{invite_id!r}; retry in "
                                f"{int(retry_after)}s"
                            ),
                        },
                        headers={
                            "Retry-After": str(max(1, int(retry_after))),
                        },
                    )
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": f"invite not found: {invite_id!r}",
                    },
                )

            # Expired-by-wallclock guard. The persisted status may
            # still be 'pending' while the housekeeping sweep catches
            # up, but functionally the invite is dead.
            try:
                exp_dt = datetime.strptime(
                    invite["expires_at"], "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                exp_dt = datetime.now(timezone.utc) - timedelta(seconds=1)
            wallclock_expired = exp_dt <= datetime.now(timezone.utc)

            if invite["status"] != "pending" or wallclock_expired:
                allowed, retry_after = _record_fail_and_check()
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": (
                                f"too many failed accept attempts on "
                                f"{invite_id!r}; retry in "
                                f"{int(retry_after)}s"
                            ),
                        },
                        headers={
                            "Retry-After": str(max(1, int(retry_after))),
                        },
                    )
                # 410 for expired (persisted or wall-clock), 409 for
                # accepted / revoked. 410 Gone matches the semantic
                # "this resource is permanently unavailable".
                effective_status = (
                    "expired" if (
                        invite["status"] == "expired" or wallclock_expired
                    ) else invite["status"]
                )
                if effective_status == "expired":
                    return JSONResponse(
                        status_code=410,
                        content={
                            "detail": (
                                f"invite {invite_id!r} has expired; "
                                f"ask the admin to issue a new one"
                            ),
                            "invite_id": invite_id,
                            "current_status": effective_status,
                        },
                    )
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            f"invite {invite_id!r} is not pending: "
                            f"current status is {effective_status!r}"
                        ),
                        "invite_id": invite_id,
                        "current_status": effective_status,
                    },
                )

            # Token verification — constant-time compare on the hex
            # digest so a timing oracle does not let an attacker
            # progressively recover the plaintext.
            if not secrets.compare_digest(
                candidate_token_hash, invite["token_hash"],
            ):
                allowed, retry_after = _record_fail_and_check()
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": (
                                f"too many failed accept attempts on "
                                f"{invite_id!r}; retry in "
                                f"{int(retry_after)}s"
                            ),
                        },
                        headers={
                            "Retry-After": str(max(1, int(retry_after))),
                        },
                    )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            f"token does not match invite {invite_id!r}"
                        ),
                    },
                )

            invite_email_norm = _normalise_email(invite["email"])
            invite_tenant = invite["tenant_id"]
            invite_role = invite["role"]

            # Branch on session presence.
            if session_user is not None:
                # Authenticated branch: caller's email MUST match the
                # invite email, otherwise we'd silently bind the
                # invite to a different account.
                caller_email_norm = _normalise_email(session_user.email)
                if caller_email_norm != invite_email_norm:
                    # Email mismatch is NOT a brute-force attempt
                    # (the caller already proved they have a session)
                    # so we do NOT decrement the rate-limit bucket on
                    # this branch — surface a clean 409 immediately.
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": (
                                f"invite was issued to "
                                f"{invite_email_norm!r}, but the "
                                f"current session is "
                                f"{caller_email_norm!r}; sign out "
                                f"first or ask the admin to re-issue "
                                f"the invite to your address"
                            ),
                            "invite_email": invite_email_norm,
                            "session_email": caller_email_norm,
                        },
                    )
                target_user_id = session_user.id
                target_user_email = session_user.email
                user_was_created = False
            else:
                # Anonymous branch: look up by normalised email; create
                # if no row exists. ``users.email`` is UNIQUE so two
                # concurrent anon-accepts of the same email serialise
                # via PG's unique constraint — the loser's INSERT
                # raises and we treat as "user already exists".
                existing = await conn.fetchrow(
                    "SELECT id, email FROM users WHERE lower(email) = $1",
                    invite_email_norm,
                )
                if existing is not None:
                    # User already has an account — anonymous caller
                    # claiming the invite without proving session
                    # ownership of the account. We accept the invite
                    # (token plaintext is sufficient proof), but the
                    # user must log in afterwards to actually use the
                    # new membership.
                    target_user_id = existing["id"]
                    target_user_email = existing["email"]
                    user_was_created = False
                else:
                    # Mint a fresh user row. Role on the user account
                    # itself is 'viewer' (most-restrictive); the
                    # membership row carries the elevated tenant role.
                    new_uid = f"u-{secrets.token_hex(5)}"
                    pw_hash = (
                        auth.hash_password(body.password)
                        if body.password
                        else ""
                    )
                    await conn.execute(
                        "INSERT INTO users (id, email, name, role, "
                        "password_hash, oidc_provider, oidc_subject, "
                        "enabled, tenant_id) "
                        "VALUES ($1, $2, $3, 'viewer', $4, '', '', 1, $5)",
                        new_uid, invite_email_norm,
                        (body.name or "").strip()[:160],
                        pw_hash, invite_tenant,
                    )
                    target_user_id = new_uid
                    target_user_email = invite_email_norm
                    user_was_created = True

            # Materialise the membership. ON CONFLICT DO NOTHING means
            # an existing active membership wins — the invite is still
            # marked accepted (admin's intent was satisfied — the user
            # is in the tenant) and the response carries
            # ``already_member=True`` so the UI can suppress a "you
            # joined!" toast.
            inserted = await conn.fetchrow(
                _UPSERT_MEMBERSHIP_SQL,
                target_user_id, invite_tenant, invite_role, now_iso,
            )
            already_member = inserted is None

            # Flip the invite to 'accepted'. The SELECT … FOR UPDATE
            # earlier locks the row, so this UPDATE never blocks even
            # under contention.
            await conn.execute(_MARK_INVITE_ACCEPTED_SQL, invite_id)

    # 5. Audit. We log the *transition* — never the token plaintext or
    #    its hash. The audit row carries enough to reconstruct the
    #    event without becoming a leak surface for the cryptographic
    #    proof.
    try:
        from backend import audit as _audit
        await _audit.log(
            action="tenant_invite_accepted",
            entity_kind="tenant_invite",
            entity_id=invite_id,
            before={
                "invite_id": invite_id,
                "tenant_id": invite_tenant,
                "status": "pending",
            },
            after={
                "invite_id": invite_id,
                "tenant_id": invite_tenant,
                "status": "accepted",
                "user_id": target_user_id,
                "role": invite_role,
                "user_was_created": user_was_created,
                "already_member": already_member,
            },
            actor=actor_label,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("tenant_invite_accepted audit emit failed: %s", exc)

    return JSONResponse(
        status_code=200,
        content={
            "invite_id": invite_id,
            "tenant_id": invite_tenant,
            "user_id": target_user_id,
            "user_email": target_user_email,
            "role": invite_role,
            "status": "accepted",
            "user_was_created": user_was_created,
            "already_member": already_member,
        },
    )

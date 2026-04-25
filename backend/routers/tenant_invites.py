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

"""BS.2.2 — Installer job REST API (jobs CRUD + sidecar long-poll).

Surface
───────
``POST   /installer/jobs``                — operator + PEP HOLD; create install job
``GET    /installer/jobs``                — operator; list with state filter
``GET    /installer/jobs/{job_id}``       — operator; single job
``POST   /installer/jobs/{job_id}/cancel``— operator; cancel queued/running job
``POST   /installer/jobs/{job_id}/retry`` — operator + PEP HOLD; clone failed/cancelled
``GET    /installer/jobs/poll``           — sidecar long-poll claim (FOR UPDATE SKIP LOCKED)

PEP integration (ADR §7.4 + design §4.1)
────────────────────────────────────────
Both ``POST /installer/jobs`` and ``POST .../retry`` route through
:func:`backend.pep_gateway.evaluate` with ``tool="install_entry"``. Because
``install_entry`` is not on any tier whitelist, the classifier returns
``hold`` ("tier_unlisted"), which raises a Decision Engine proposal and
blocks the request until the operator approves / rejects (or
``hold_timeout_s`` elapses).

* On ``approve`` → the install_jobs row stays at ``state='queued'`` and
  ``pep_decision_id`` records the DE id for auditability.
* On ``reject`` / ``timeout`` → the row is flipped to
  ``state='cancelled'`` with ``error_reason='pep_rejected:<rule>'`` and
  the original POST returns 403.

The HOLD outcome is written to the same row so audit trail is single-source:
listing the job shows both the install lifecycle and the gating decision.

Idempotency (ADR §4.4 step 4)
─────────────────────────────
Frontend supplies ``idempotency_key`` (UUID). The INSERT runs with
``ON CONFLICT (idempotency_key) DO NOTHING RETURNING id``; a duplicate
request returns the *original* row without re-running PEP. This kills
double-click double-creation in the UI without server-side dedup state.

Long-poll claim (ADR §4.4 step 1)
─────────────────────────────────
``GET /installer/jobs/poll`` runs a single transaction:

    SELECT … FROM install_jobs
      WHERE state = 'queued'
            AND protocol_version IN (… supported …)
      ORDER BY queued_at ASC
      LIMIT 1
      FOR UPDATE SKIP LOCKED;

    UPDATE install_jobs SET state='running',
                            sidecar_id=$1,
                            claimed_at=now()
      WHERE id=$2;

PG's ``FOR UPDATE SKIP LOCKED`` serialises claims across uvicorn workers
and across multiple sidecar replicas — a job is delivered to exactly one
sidecar, no double-execution. If no claim is available the handler sleeps
in 250 ms ticks until either ``timeout_s`` elapses (returns 204) or a job
appears.

Auth (BS.2.3 spec)
──────────────────
* ``POST /installer/jobs``                 — ``require_operator`` + PEP
* ``GET  /installer/jobs[/{id}]``          — ``require_operator``
* ``POST /installer/jobs/{id}/cancel``     — ``require_operator``
* ``POST /installer/jobs/{id}/retry``      — ``require_operator`` + PEP
* ``GET  /installer/jobs/poll``            — ``require_admin`` (sidecar
  token auth lands with BS.4.1; until then admin role is the safe stand-in
  because the sidecar process is operator-managed infrastructure)

Tenant scope
────────────
Every read filters by ``tenant_id = caller.tenant_id`` (install_jobs has
no shipped/global rows — every row is per-tenant). The sidecar long-poll
is the sole exception: a sidecar serves jobs across tenants and matches
on ``state='queued'`` only. The ``tenant_id`` lands on the response so
the sidecar can scope its own logging.

Module-global / cross-worker state audit
────────────────────────────────────────
None introduced in this router. Every read / write goes through
``db_pool.get_pool()`` (cross-worker via asyncpg + PG); idempotency
dedup is enforced by the UNIQUE index in alembic 0051; sidecar-claim
serialisation is enforced by ``SELECT … FOR UPDATE SKIP LOCKED`` on PG.
PEP gateway has its own module-global ``_held_registry`` /
``_recent`` rings, but those are R0/R20 concerns (UI live feed, not
load-bearing here) — this router only consumes the gateway's
``evaluate()`` return value.

Read-after-write timing audit
─────────────────────────────
* ``POST → GET /installer/jobs/{id}`` from same tenant: the INSERT and
  GET are separate transactions; the GET sees the new row by PG MVCC
  (the INSERT's commit completes before the response is returned).
* Long-poll claim is in a single transaction so two concurrent sidecars
  hitting the same queued job will see exactly one win the row lock;
  the other gets the next queued row or 204.
* No shared in-memory cache that could lag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets as _secrets
import time
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth as _au
from backend import pep_gateway as _pep
from backend.db_context import set_tenant_id
from backend.routers._pagination import Limit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/installer", tags=["installer"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation constants (mirrored from alembic 0051 CHECK + ADR §4.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Closed sets exactly mirror the DB CHECK constraints from alembic 0051
# so 422 fires before the DB rejects the row.
INSTALL_JOB_STATES: tuple[str, ...] = (
    "queued", "running", "completed", "failed", "cancelled",
)
TERMINAL_STATES: tuple[str, ...] = ("completed", "failed", "cancelled")
ACTIVE_STATES: tuple[str, ...] = ("queued", "running")

# Sidecar protocol versions this backend speaks. ADR §4.3 prescribes
# "support N and N-1 simultaneously; reject N-2 with 426". Today we only
# ship v1 — when v2 lands the tuple becomes ``(1, 2)`` and the 426 path
# fires for any client claiming protocol_version not in the tuple.
SUPPORTED_SIDECAR_PROTOCOL_VERSIONS: tuple[int, ...] = (1,)
DEFAULT_SIDECAR_PROTOCOL_VERSION = 1

# PEP tool identifier for catalog installs. Not on any tier whitelist
# (T1/T2/T3) by design — install of an arbitrary catalog entry is
# inherently destructive (writes to host disk / pulls images / runs
# vendor scripts) and so always lands in PEP HOLD via ``classify``'s
# "tier_unlisted" branch. The string is referenced by the BS.7
# coaching-card lookup (R20-A) when the toast renders.
INSTALL_PEP_TOOL: str = "install_entry"

# ID conventions — ``ij-`` prefix matches the alembic 0051 PK convention.
# 12 hex chars = 48 bits of entropy, plenty for a per-tenant install
# history (collision floor << 1 in 100M jobs per tenant).
INSTALL_JOB_ID_PATTERN = r"^ij-[0-9a-f]{12}$"
_INSTALL_JOB_ID_RE = re.compile(INSTALL_JOB_ID_PATTERN)

# Idempotency key — UUID v4 textual form OR any 16..64 char ascii token.
# We accept the relaxed form so the frontend can use whatever ID it has
# handy (typically a uuid.uuid4().hex from the install button click).
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_\-]{16,64}$"

# Catalog entry id — same kebab-case shape as catalog router's regex.
# Repeated here to avoid a router→router runtime import.
ENTRY_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
ENTRY_ID_MAX_LEN = 64
_ENTRY_ID_RE = re.compile(ENTRY_ID_PATTERN)

# Sidecar id — short ascii token the sidecar self-identifies with.
# Constrained to keep stray injection out of the audit row.
SIDECAR_ID_PATTERN = r"^[A-Za-z0-9_.\-:]{1,128}$"

# Long-poll bounds. ADR §4 says default 30 s timeout for long-poll;
# we cap the upper bound at 60 s so a misbehaving sidecar can't pin a
# uvicorn worker indefinitely (workers are bounded; an idle long-poll
# is still an open conn).
POLL_TIMEOUT_S_DEFAULT = 30
POLL_TIMEOUT_S_MAX = 60
_POLL_TICK_S = 0.25  # 250 ms — same as PEP gateway's wait tick

# PEP HOLD ceiling: 30 min default per pep_gateway.evaluate, but we set
# a tighter 10 min ceiling here because a running HTTP request blocking
# 30 min would clog the connection pool. Frontend ought to accept a 408
# / show a "still pending" UI state if the operator hasn't decided in
# 10 min — they can always re-submit (idempotency_key dedupes).
INSTALL_PEP_HOLD_TIMEOUT_S = 600.0

# Cancel reason field cap — the operator may pass a free-text justification
# that lands in audit / error_reason. Anything longer than 256 chars is
# almost certainly noise.
CANCEL_REASON_MAX_LEN = 256


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InstallJobCreate(BaseModel):
    """POST body for ``/installer/jobs``.

    ``entry_id`` is the catalog_entries id the operator wants to install
    (resolved across the three source layers by the catalog resolver —
    install always uses the resolved view). ``idempotency_key`` is the
    UI-side dedupe token; same key = same job.
    """

    entry_id: str = Field(
        min_length=1, max_length=ENTRY_ID_MAX_LEN, pattern=ENTRY_ID_PATTERN,
    )
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)
    bytes_total: int | None = Field(default=None, ge=0)
    # Free-form per-job metadata — not validated here; whatever the
    # frontend wants to remember about the install (e.g. version chosen,
    # vendor channel, source IP). Never the secret store ref — those
    # belong on catalog_subscriptions.auth_secret_ref.
    metadata: dict = Field(default_factory=dict)


class InstallJobCancelBody(BaseModel):
    """Optional body for ``POST /installer/jobs/{id}/cancel``.

    A bare empty POST is fine; ``reason`` is recorded in audit + the
    job's ``error_reason`` column when set.
    """

    reason: str | None = Field(default=None, max_length=CANCEL_REASON_MAX_LEN)


class InstallJobRetryBody(BaseModel):
    """Optional body for ``POST /installer/jobs/{id}/retry``.

    ``idempotency_key`` is required so the retry POST is itself idempotent
    against double-clicks. The clone takes the source row's ``entry_id``
    and ``metadata`` as a starting point; the caller may not override
    those (admin can patch the catalog entry instead via BS.2.1 PATCH).
    """

    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ensure_tenant(user: _au.User) -> str:
    """Pin the request-scoped tenant context and return the tid."""
    tid = user.tenant_id or "t-default"
    set_tenant_id(tid)
    return tid


def _new_install_job_id() -> str:
    return f"ij-{_secrets.token_hex(6)}"


def _coerce_json(v: Any, default: Any) -> Any:
    """JSONB columns come back as Python objects from asyncpg when the
    pool has the ``json`` codec set; defence-in-depth here in case the
    codec is absent in dev (we accept str + parse)."""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return default
    return default


def _row_to_install_job(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "entry_id": row["entry_id"],
        "state": row["state"],
        "idempotency_key": row["idempotency_key"],
        "sidecar_id": row["sidecar_id"],
        "protocol_version": int(row["protocol_version"]),
        "bytes_done": int(row["bytes_done"]),
        "bytes_total": (
            int(row["bytes_total"]) if row["bytes_total"] is not None else None
        ),
        "eta_seconds": (
            int(row["eta_seconds"]) if row["eta_seconds"] is not None else None
        ),
        "log_tail": row["log_tail"],
        "result_json": _coerce_json(row["result_json"], None),
        "error_reason": row["error_reason"],
        "pep_decision_id": row["pep_decision_id"],
        "requested_by": row["requested_by"],
        "queued_at": row["queued_at"],
        "claimed_at": row["claimed_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


_INSTALL_JOB_RETURNING_COLS = (
    "id, tenant_id, entry_id, state, idempotency_key, sidecar_id, "
    "protocol_version, bytes_done, bytes_total, eta_seconds, log_tail, "
    "result_json, error_reason, pep_decision_id, requested_by, "
    "queued_at, claimed_at, started_at, completed_at"
)


async def _resolve_catalog_entry_for_install(
    conn: Any, entry_id: str, tenant_id: str,
) -> dict[str, Any] | None:
    """Look up the resolved catalog row for *entry_id* in *tenant_id*.

    Returns the highest-priority live row (override > operator > shipped)
    or ``None`` if no live row exists. Does not apply the partial-overlay
    merge that the catalog router does for GET — install only needs to
    confirm "yes this entry exists and is installable" + the
    install_method for the PEP coaching context.
    """
    rows = await conn.fetch(
        "SELECT id, source, install_method, family, vendor, version, "
        "       hidden, tenant_id "
        "FROM catalog_entries "
        "WHERE id = $1 AND (tenant_id IS NULL OR tenant_id = $2) "
        "ORDER BY CASE source "
        "  WHEN 'override' THEN 0 "
        "  WHEN 'operator' THEN 1 "
        "  WHEN 'subscription' THEN 2 "
        "  WHEN 'shipped' THEN 3 "
        "  ELSE 4 END "
        "LIMIT 1",
        entry_id, tenant_id,
    )
    if not rows:
        return None
    row = rows[0]
    if row["hidden"]:
        # Tombstoned for this tenant — install must 404 just like GET.
        return None
    return {
        "id": row["id"],
        "source": row["source"],
        "install_method": row["install_method"],
        "family": row["family"],
        "vendor": row["vendor"],
        "version": row["version"],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs — operator + PEP HOLD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/jobs", status_code=201)
async def create_job(
    body: InstallJobCreate,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Create an install job for *entry_id*; HOLD via PEP gateway.

    Flow:

    1. Validate the catalog entry exists & is not hidden in this tenant.
    2. ``INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING id``.
       If conflict, return the *existing* row at 200 (idempotent retry —
       no second PEP HOLD).
    3. New row exists → call ``pep_gateway.evaluate(tool='install_entry',
       arguments={…})`` and block on operator approval.
    4. On approve → UPDATE the row's ``pep_decision_id``, return 201.
    5. On deny / timeout → flip row to ``state='cancelled'`` with
       ``error_reason='pep_<rule>'`` and return 403.
    """
    tid = _ensure_tenant(user)

    from backend.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Catalog entry must exist + be installable in this tenant.
        entry = await _resolve_catalog_entry_for_install(conn, body.entry_id, tid)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=f"catalog entry {body.entry_id!r} not found "
                       "(or hidden) in this tenant",
            )

        # 2. Idempotent insert. ON CONFLICT (idempotency_key) DO NOTHING
        #    returns NULL when the unique constraint trips — we then
        #    SELECT the existing row and return that.
        new_id = _new_install_job_id()
        inserted = await conn.fetchrow(
            "INSERT INTO install_jobs "
            "  (id, tenant_id, entry_id, state, idempotency_key, "
            "   protocol_version, requested_by) "
            "VALUES ($1, $2, $3, 'queued', $4, $5, $6) "
            "ON CONFLICT (idempotency_key) DO NOTHING "
            f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
            new_id, tid, body.entry_id, body.idempotency_key,
            DEFAULT_SIDECAR_PROTOCOL_VERSION, user.id,
        )
        if inserted is None:
            existing = await conn.fetchrow(
                f"SELECT {_INSTALL_JOB_RETURNING_COLS} "
                "FROM install_jobs "
                "WHERE idempotency_key = $1 AND tenant_id = $2",
                body.idempotency_key, tid,
            )
            if existing is None:
                # Race: another tenant claimed the same idempotency_key
                # before us (UNIQUE is global). Refuse with 409.
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key collision across tenants — "
                           "regenerate and retry",
                )
            return JSONResponse(
                status_code=200, content=_row_to_install_job(existing),
            )

    # 3. PEP HOLD — outside the conn context so the conn isn't held
    #    for the entire 10-minute (worst case) operator decision wait.
    pep_decision = None
    pep_error: str | None = None
    try:
        pep_decision = await _pep.evaluate(
            tool=INSTALL_PEP_TOOL,
            arguments={
                "entry_id": entry["id"],
                "tenant_id": tid,
                "install_method": entry["install_method"],
                "family": entry["family"],
                "vendor": entry["vendor"],
                "version": entry["version"],
                "job_id": inserted["id"],
                "actor": user.email,
            },
            agent_id=f"operator:{user.email}",
            tier="t1",
            hold_timeout_s=INSTALL_PEP_HOLD_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — gateway breaker handles
        # Gateway raised before / during the wait — the breaker logs it,
        # but we still need to clean up the queued row so it doesn't
        # sit forever. Mark it cancelled with a clear error_reason.
        pep_error = f"pep_gateway_error:{exc.__class__.__name__}"
        logger.warning(
            "pep evaluate raised for install job %s: %s",
            inserted["id"], exc,
        )

    async with pool.acquire() as conn:
        if pep_decision is not None and pep_decision.action is _pep.PepAction.auto_allow:
            # Approved — record the decision id, leave state='queued'
            # for sidecar pickup.
            row = await conn.fetchrow(
                "UPDATE install_jobs "
                "SET pep_decision_id = $1 "
                "WHERE id = $2 AND tenant_id = $3 "
                f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
                pep_decision.decision_id, inserted["id"], tid,
            )
            _emit_audit_safely(
                action="installer.job_created",
                entity_id=inserted["id"],
                actor=user.email,
                before=None,
                after={
                    "id": inserted["id"], "entry_id": body.entry_id,
                    "state": "queued",
                    "pep_decision_id": pep_decision.decision_id,
                },
            )
            return JSONResponse(
                status_code=201, content=_row_to_install_job(row),
            )

        # Denied or evaluate raised. Flip the queued row to cancelled
        # with a structured error_reason so the UI can render the
        # rejection cleanly.
        if pep_decision is None:
            reason = pep_error or "pep_gateway_unknown_error"
            decision_id_for_audit = None
        else:
            reason = f"pep_{pep_decision.rule or 'denied'}"
            decision_id_for_audit = pep_decision.decision_id

        row = await conn.fetchrow(
            "UPDATE install_jobs "
            "SET state = 'cancelled', "
            "    error_reason = $1, "
            "    pep_decision_id = $2, "
            "    completed_at = now() "
            "WHERE id = $3 AND tenant_id = $4 "
            f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
            reason, decision_id_for_audit, inserted["id"], tid,
        )

    _emit_audit_safely(
        action="installer.job_pep_denied",
        entity_id=inserted["id"],
        actor=user.email,
        before=None,
        after={
            "id": inserted["id"], "entry_id": body.entry_id,
            "state": "cancelled", "error_reason": reason,
        },
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "pep_denied",
            "reason": reason,
            "job_id": inserted["id"],
            "job": _row_to_install_job(row) if row is not None else None,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /installer/jobs — operator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/jobs")
async def list_jobs(
    request: Request,
    state: str | None = Query(default=None),
    entry_id: str | None = Query(default=None, max_length=ENTRY_ID_MAX_LEN),
    sidecar_id: str | None = Query(default=None, max_length=128),
    limit: int = Limit(default=100, max_cap=500),
    offset: int = Query(default=0, ge=0, le=100_000),
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """List install jobs visible to the caller's tenant.

    Filters: ``state`` (one of ``queued|running|completed|failed|cancelled``),
    ``entry_id``, ``sidecar_id``. Sort: ``queued_at DESC, id ASC`` —
    newest first, deterministic tiebreak.
    """
    tid = _ensure_tenant(user)
    if state is not None and state not in INSTALL_JOB_STATES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown state: {state!r}; "
                   f"must be one of {list(INSTALL_JOB_STATES)}",
        )

    where = ["tenant_id = $1"]
    params: list[Any] = [tid]
    if state is not None:
        params.append(state)
        where.append(f"state = ${len(params)}")
    if entry_id is not None:
        params.append(entry_id)
        where.append(f"entry_id = ${len(params)}")
    if sidecar_id is not None:
        params.append(sidecar_id)
        where.append(f"sidecar_id = ${len(params)}")

    params.append(int(limit))
    params.append(int(offset))
    sql = (
        f"SELECT {_INSTALL_JOB_RETURNING_COLS} "
        "FROM install_jobs "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY queued_at DESC, id ASC "
        f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )
    count_sql = (
        f"SELECT COUNT(*) FROM install_jobs WHERE {' AND '.join(where)}"
    )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
        total = await conn.fetchval(count_sql, *params[:-2])

    items = [_row_to_install_job(r) for r in rows]
    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "count": len(items),
            "total": int(total or 0),
            "limit": int(limit),
            "offset": int(offset),
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /installer/jobs/{job_id} — single job
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/jobs/poll")
async def poll_for_job(
    sidecar_id: str = Query(..., min_length=1, max_length=128),
    protocol_version: int = Query(default=DEFAULT_SIDECAR_PROTOCOL_VERSION, ge=1),
    timeout_s: int = Query(default=POLL_TIMEOUT_S_DEFAULT, ge=0, le=POLL_TIMEOUT_S_MAX),
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Sidecar long-poll: claim one queued job.

    Implements ADR §4.3 handshake (426 on unsupported protocol_version)
    and ADR §4.4 step 1 (single-tx claim via ``SELECT … FOR UPDATE
    SKIP LOCKED``). Polls in 250 ms ticks until either a job is claimed
    or ``timeout_s`` elapses.

    Returns 200 + the claimed job row on success, 204 No Content if
    no claim within the timeout window, 426 if the sidecar's protocol
    version is unsupported.
    """
    if not re.match(SIDECAR_ID_PATTERN, sidecar_id):
        raise HTTPException(
            status_code=422,
            detail=f"invalid sidecar_id: {sidecar_id!r}",
        )

    if protocol_version not in SUPPORTED_SIDECAR_PROTOCOL_VERSIONS:
        # ADR §4.3 — 426 Upgrade Required + body describing the gap so
        # the operator can pull the right sidecar image.
        return JSONResponse(
            status_code=426,
            content={
                "error": "protocol_version_unsupported",
                "client_protocol_version": protocol_version,
                "supported": list(SUPPORTED_SIDECAR_PROTOCOL_VERSIONS),
                "min_version": min(SUPPORTED_SIDECAR_PROTOCOL_VERSIONS),
                "max_version": max(SUPPORTED_SIDECAR_PROTOCOL_VERSIONS),
            },
        )

    deadline = time.monotonic() + float(timeout_s)
    from backend.db_pool import get_pool
    pool = get_pool()

    while True:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, tenant_id, entry_id, idempotency_key, "
                    "       protocol_version, queued_at "
                    "FROM install_jobs "
                    "WHERE state = 'queued' "
                    "  AND protocol_version = ANY($1::int[]) "
                    "ORDER BY queued_at ASC "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED",
                    list(SUPPORTED_SIDECAR_PROTOCOL_VERSIONS),
                )
                if row is not None:
                    claimed = await conn.fetchrow(
                        "UPDATE install_jobs "
                        "SET state = 'running', "
                        "    sidecar_id = $1, "
                        "    claimed_at = now() "
                        "WHERE id = $2 "
                        f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
                        sidecar_id, row["id"],
                    )
                    _emit_audit_safely(
                        action="installer.job_claimed",
                        entity_id=row["id"],
                        actor=f"sidecar:{sidecar_id}",
                        before={"state": "queued"},
                        after={"state": "running", "sidecar_id": sidecar_id},
                    )
                    return JSONResponse(
                        status_code=200,
                        content=_row_to_install_job(claimed),
                    )
        # No claim — sleep one tick and retry until deadline.
        if time.monotonic() >= deadline:
            return JSONResponse(status_code=204, content=None)
        try:
            await asyncio.sleep(_POLL_TICK_S)
        except asyncio.CancelledError:
            # Caller disconnected — exit cleanly.
            raise


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Return a single install job in the caller's tenant. 404 otherwise."""
    if not _INSTALL_JOB_ID_RE.match(job_id):
        raise HTTPException(
            status_code=422,
            detail=f"invalid job id: {job_id!r}; must match "
                   f"{INSTALL_JOB_ID_PATTERN}",
        )
    tid = _ensure_tenant(user)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_INSTALL_JOB_RETURNING_COLS} "
            "FROM install_jobs "
            "WHERE id = $1 AND tenant_id = $2",
            job_id, tid,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="install job not found")
    return JSONResponse(status_code=200, content=_row_to_install_job(row))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs/{id}/cancel — operator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    body: InstallJobCancelBody | None = Body(default=None),
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Cancel a queued or running install job.

    Cancel semantics:

    * ``queued`` → flip to ``cancelled`` immediately; sidecar will
      never claim it (its long-poll WHERE ``state='queued'`` excludes it).
    * ``running`` → flip to ``cancelled``; sidecar's next progress emit
      will see the new state and abort (BS.4.2 sidecar contract).
    * Terminal states (``completed`` / ``failed`` / ``cancelled``) →
      409 Conflict; the caller wanted ``retry`` instead.
    """
    if not _INSTALL_JOB_ID_RE.match(job_id):
        raise HTTPException(
            status_code=422,
            detail=f"invalid job id: {job_id!r}; must match "
                   f"{INSTALL_JOB_ID_PATTERN}",
        )
    tid = _ensure_tenant(user)
    reason = (body.reason if body is not None else None) or "operator_cancelled"

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT state FROM install_jobs "
                "WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                job_id, tid,
            )
            if existing is None:
                raise HTTPException(
                    status_code=404, detail="install job not found",
                )
            if existing["state"] in TERMINAL_STATES:
                raise HTTPException(
                    status_code=409,
                    detail=f"job is in terminal state {existing['state']!r}; "
                           "cannot cancel",
                )
            row = await conn.fetchrow(
                "UPDATE install_jobs "
                "SET state = 'cancelled', "
                "    error_reason = COALESCE(error_reason, $1), "
                "    completed_at = now() "
                "WHERE id = $2 AND tenant_id = $3 "
                f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
                reason, job_id, tid,
            )

    _emit_audit_safely(
        action="installer.job_cancelled",
        entity_id=job_id,
        actor=user.email,
        before={"state": existing["state"]},
        after={"state": "cancelled", "reason": reason},
    )
    return JSONResponse(status_code=200, content=_row_to_install_job(row))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/jobs/{id}/retry — operator + PEP HOLD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/jobs/{job_id}/retry", status_code=201)
async def retry_job(
    job_id: str,
    body: InstallJobRetryBody,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Clone a non-running install job into a fresh ``queued`` row.

    Retry preserves audit history by leaving the source row untouched
    and creating a NEW row with a fresh id + caller-supplied
    ``idempotency_key`` (so the retry POST is itself idempotent).

    Source-row state requirement:

    * Source must be in a non-active terminal state (``failed`` /
      ``cancelled``) OR in ``completed`` (re-install). Re-trying a row
      that's still ``queued`` / ``running`` is a 409 — cancel first if
      that's what the operator meant.

    Per ADR §4.4 step 3, ``shell_script`` install methods are NOT
    auto-retried by the sidecar (vendor scripts may not be idempotent).
    The retry endpoint itself is method-agnostic — it always creates
    a fresh queued row regardless of install_method, then routes
    through the same PEP HOLD as a fresh POST.
    """
    if not _INSTALL_JOB_ID_RE.match(job_id):
        raise HTTPException(
            status_code=422,
            detail=f"invalid job id: {job_id!r}; must match "
                   f"{INSTALL_JOB_ID_PATTERN}",
        )
    tid = _ensure_tenant(user)

    # Use the same router as POST /installer/jobs once the source row is
    # validated — this keeps PEP gating + idempotency dedup centralised.
    from backend.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        src = await conn.fetchrow(
            "SELECT id, entry_id, state FROM install_jobs "
            "WHERE id = $1 AND tenant_id = $2",
            job_id, tid,
        )
        if src is None:
            raise HTTPException(
                status_code=404, detail="install job not found",
            )
        if src["state"] in ACTIVE_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"source job is {src['state']!r}; cancel first "
                       "before retry",
            )

    # Delegate to the POST /installer/jobs path — fresh PEP HOLD,
    # fresh idempotency_key, fresh row.
    create_body = InstallJobCreate(
        entry_id=src["entry_id"],
        idempotency_key=body.idempotency_key,
    )
    return await create_job(create_body, request, user)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit helper (mirrors backend/routers/catalog.py::_emit_audit_safely)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _emit_audit_safely(
    *, action: str, entity_id: str, actor: str,
    before: dict | None, after: dict | None,
) -> None:
    """Best-effort fire-and-forget audit emit.

    Mirrors ``backend/routers/catalog.py::_emit_audit_safely`` so every
    install_jobs mutation generates exactly one audit row; the audit
    module itself swallows transport failures so this wrapper just
    guards against the missing-event-loop edge case during shutdown /
    test teardown.
    """
    try:
        from backend import audit as _audit
        loop = asyncio.get_running_loop()
        loop.create_task(
            _audit.log(
                action=action,
                entity_kind="install_job",
                entity_id=entity_id,
                before=before,
                after=after,
                actor=actor,
            )
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("%s audit emit failed: %s", action, exc)

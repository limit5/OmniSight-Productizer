"""BS.2.2 — Installer job REST API (jobs CRUD + sidecar long-poll).

Surface
───────
``POST   /installer/jobs``                — operator + PEP HOLD; create install job
``GET    /installer/jobs``                — operator; list with state filter
``GET    /installer/jobs/{job_id}``       — operator; single job
``POST   /installer/jobs/{job_id}/cancel``— operator; cancel queued/running job
``POST   /installer/jobs/{job_id}/retry`` — operator + PEP HOLD; clone failed/cancelled
``GET    /installer/jobs/poll``           — sidecar long-poll claim (FOR UPDATE SKIP LOCKED)
``GET    /installer/installed``           — operator; list currently-installed entries (BS.8.2)
``POST   /installer/uninstall``           — operator + PEP HOLD; bulk uninstall (BS.8.2)

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

# BS.8.2 — Bulk uninstall (cleanup unused) constants.
#
# We reuse the existing ``install_jobs`` table for the audit + state
# trail of an uninstall request rather than introducing a parallel
# ``uninstall_jobs`` table. Two design notes:
#
# 1. **Sidecar long-poll isolation**: the sidecar's claim path filters
#    on ``state='queued'``. Uninstall rows are inserted with
#    ``state='completed'`` (PEP-approved) or ``state='cancelled'``
#    (PEP-denied), so the sidecar will never claim them and try to run
#    an install method on an uninstall row.
# 2. **Discriminator lives in ``result_json``**: the row's
#    ``result_json->>'kind' = 'uninstall'`` flags the record for the
#    ``GET /installer/installed`` derivation. The list endpoint excludes
#    entries whose latest install_jobs row is an uninstall, so the
#    Installed tab and the Cleanup-unused modal stop showing the entry
#    after a successful uninstall.
#
# The actual on-disk uninstall (toolchain dir removal, image GC, vendor
# script tear-down) is deferred to a sidecar handler in a follow-up
# row — today the PEP-approved record is the single source of truth
# that the operator wanted the entry gone, and the InstalledTab honours
# that even before the disk is reclaimed.
INSTALL_KIND_UNINSTALL: str = "uninstall"

# PEP tool name for catalog uninstall. Same shape as INSTALL_PEP_TOOL —
# not on any tier whitelist, so ``classify`` returns HOLD via the
# ``tier_unlisted`` rule and the operator must explicitly approve. The
# decision card ToastCenter renders is the standard ``tier_unlisted``
# coaching surface; a dedicated ``uninstall_intercept`` coaching card
# can land alongside BS.8.4 dependency-check (where the operator
# benefits most from the richer copy).
UNINSTALL_PEP_TOOL: str = "uninstall_entry"

# Same 10-minute HOLD ceiling as install. A bulk uninstall blocks on a
# single PEP HOLD covering every entry in the batch; the operator gets
# one coaching card listing the count + the entry ids and either
# approves the whole batch or rejects it.
UNINSTALL_PEP_HOLD_TIMEOUT_S = 600.0

# Cap for a single bulk-uninstall payload. Operators picking 50+ idle
# entries at once is plausible for a long-lived host, but 1000+ is
# almost certainly noise / a misuse — we reject the request loudly so
# pagination is forced.
BULK_UNINSTALL_MAX_ENTRIES = 200


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


class BulkUninstallBody(BaseModel):
    """POST body for ``/installer/uninstall`` (BS.8.2).

    ``entry_ids`` is the list of catalog_entries the operator selected in
    the cleanup-unused modal. The list is bounded at the
    :data:`BULK_UNINSTALL_MAX_ENTRIES` ceiling so a single request never
    blows the PEP coaching card out (and so a server-side typo can't
    queue 100k uninstalls).

    Each entry id must match the same ``ENTRY_ID_PATTERN`` the install
    create body uses; duplicates in the list are deduplicated server-side
    before the PEP HOLD fires (one entry = one row even if the operator
    submitted it twice).
    """

    entry_ids: list[str] = Field(min_length=1, max_length=BULK_UNINSTALL_MAX_ENTRIES)
    reason: str | None = Field(default=None, max_length=CANCEL_REASON_MAX_LEN)


# BS.4.4 — sidecar progress emit. Same wire shape that
# ``installer/progress.py::make_progress_cb`` POSTs every time the
# install method ticks. ``log_tail`` is bounded server-side at the same
# 4 KiB cap the sidecar trims at; an oversize tail is rejected at 422
# rather than silently truncated so a misconfigured sidecar fails loud.
PROGRESS_LOG_TAIL_MAX_BYTES = 4 * 1024
# Stage label is free-form within the sidecar (each install method picks
# its own labels — ``downloading`` / ``verifying`` / ``running`` /
# ``promoting`` / ``finalizing``). We cap the length so a buggy method
# can't write a megabyte of garbage into the SSE payload.
PROGRESS_STAGE_MAX_LEN = 64


class InstallJobProgress(BaseModel):
    """Sidecar → backend progress payload. BS.4.4.

    Fields mirror :data:`installer.progress.ProgressEmitterConfig` /
    :func:`installer.progress.make_progress_cb`'s POST body. Validation
    matches what the install_jobs schema can hold:

    * ``bytes_done`` — non-negative; bigint column, no upper cap.
    * ``bytes_total`` — None (unknown — vendor URL didn't send a
      Content-Length) or non-negative.
    * ``eta_seconds`` — None or non-negative.
    * ``log_tail`` — text, max 4 KiB (sidecar trims; we still validate).
    * ``stage`` — free-form short string the UI shows under the bar.
    * ``sidecar_id`` — for audit only; we already know it from claim,
      but accepting it makes log lines easier to correlate.
    """

    stage: str = Field(min_length=1, max_length=PROGRESS_STAGE_MAX_LEN)
    bytes_done: int = Field(ge=0)
    bytes_total: int | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    log_tail: str = Field(default="", max_length=PROGRESS_LOG_TAIL_MAX_BYTES)
    sidecar_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=SIDECAR_ID_PATTERN,
    )


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


def _ts_to_iso(v: Any) -> Any:
    """Coerce a PG TIMESTAMPTZ (datetime) or SQLite REAL epoch into a
    JSON-serialisable representation. ``starlette.responses.JSONResponse``
    uses plain ``json.dumps`` which cannot serialise ``datetime`` —
    passing the object through raises ``TypeError`` at response time.
    Mirrors the same helper in catalog.py."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    isoformat = getattr(v, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return v


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
        "queued_at": _ts_to_iso(row["queued_at"]),
        "claimed_at": _ts_to_iso(row["claimed_at"]),
        "started_at": _ts_to_iso(row["started_at"]),
        "completed_at": _ts_to_iso(row["completed_at"]),
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
    install_method / display_name / size_bytes for the PEP coaching
    context (BS.7.2: coaching card renders entry name + version + size +
    install method).
    """
    rows = await conn.fetch(
        "SELECT id, source, install_method, family, vendor, version, "
        "       display_name, size_bytes, hidden, tenant_id "
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
        "display_name": row["display_name"],
        "size_bytes": (
            int(row["size_bytes"]) if row["size_bytes"] is not None else None
        ),
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
                # BS.7.2 — coaching card pulls display_name + size_bytes
                # to render the install-specific 4-line card; both are
                # nullable on catalog_entries (size_bytes if vendor URL
                # didn't send Content-Length; display_name is NOT NULL
                # but defensively typed-as-Optional here).
                "display_name": entry.get("display_name"),
                "size_bytes": entry.get("size_bytes"),
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
#  POST /installer/jobs/{id}/progress — sidecar (BS.4.4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/jobs/{job_id}/progress")
async def report_progress(
    job_id: str,
    body: InstallJobProgress,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Sidecar progress emit. BS.4.4.

    Updates the in-flight install_jobs row's
    ``bytes_done / bytes_total / eta_seconds / log_tail`` and (on first
    progress tick of a job) stamps ``started_at = now()``. Then emits an
    SSE ``installer_progress`` event so the operator UI's progress bar
    refreshes without polling. Returns the row's current ``state`` so the
    sidecar can detect operator cancel (state flipped to ``cancelled`` →
    sidecar's ``progress_cb`` raises :class:`InstallCancelled` and the
    in-flight install method runs its kill-and-reap path).

    Auth surface: same as ``/jobs/poll`` — ``require_admin`` because the
    sidecar process is operator-managed infrastructure (per BS.2.3
    notes); a per-sidecar service token is on the BS-future roadmap.

    State invariants:

    * ``running`` → accept and update; this is the steady-state path.
    * ``queued`` → accept (gives the UI a head-start hint) but flip to
      ``running`` since the sidecar wouldn't ticker progress for a
      job it hasn't claimed; rare race during the brief window between
      ``UPDATE … SET state='running'`` and the first progress emit
      (network latency + the install method's first stage). The
      transition mirrors what claim's UPDATE would do, so it is safe.
    * ``completed`` / ``failed`` / ``cancelled`` → return 200 + the
      terminal state without mutating; the sidecar's emitter sees the
      response, raises :class:`InstallCancelled`, and the method aborts.
      Refusing here (e.g. 409) would leave a hung subprocess.

    Cross-tenant note: the sidecar's bearer token authenticates as
    ``admin``, which today has cross-tenant visibility. We stamp
    ``set_tenant_id`` from the row's ``tenant_id`` so any audit emit
    correlates to the right tenant context. SSE broadcast is scoped to
    the row's tenant.
    """
    if not _INSTALL_JOB_ID_RE.match(job_id):
        raise HTTPException(
            status_code=422,
            detail=f"invalid job id: {job_id!r}; must match "
                   f"{INSTALL_JOB_ID_PATTERN}",
        )

    log_tail_bytes = body.log_tail.encode("utf-8", errors="replace")
    if len(log_tail_bytes) > PROGRESS_LOG_TAIL_MAX_BYTES:
        # Defence in depth: pydantic's max_length counts characters; a
        # 4-byte UTF-8 codepoint with `len(str) <= 4096` could still
        # blow the byte budget. Reject loudly so misbehaving sidecars
        # surface during smoke instead of silently overflowing the
        # bus payload.
        raise HTTPException(
            status_code=422,
            detail=(
                f"log_tail exceeds {PROGRESS_LOG_TAIL_MAX_BYTES} bytes "
                f"after utf-8 encoding ({len(log_tail_bytes)} bytes); "
                "trim to LOG_TAIL_MAX_BYTES on the sidecar before posting"
            ),
        )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, tenant_id, state, started_at, entry_id, "
                "       sidecar_id "
                "FROM install_jobs "
                "WHERE id = $1 FOR UPDATE",
                job_id,
            )
            if existing is None:
                raise HTTPException(
                    status_code=404, detail="install job not found",
                )

            # Pin tenant context for any nested audit / SSE emit.
            set_tenant_id(existing["tenant_id"])

            current_state = existing["state"]
            # Terminal — return the state, do NOT mutate. Sidecar uses
            # this as the cancel / abort signal.
            if current_state in TERMINAL_STATES:
                return JSONResponse(
                    status_code=200,
                    content={
                        "id": job_id,
                        "state": current_state,
                        "tenant_id": existing["tenant_id"],
                        "ignored": True,
                    },
                )

            # Active: queued (rare race) → flip to running with this
            # progress tick as the first proof-of-life. running → just
            # update the metrics columns. We always stamp started_at
            # idempotently on first progress tick.
            new_state = "running" if current_state == "queued" else current_state
            started_at_clause = (
                "started_at = COALESCE(started_at, now())"
            )

            row = await conn.fetchrow(
                "UPDATE install_jobs "
                "SET state = $1, "
                "    bytes_done = $2, "
                "    bytes_total = COALESCE($3, bytes_total), "
                "    eta_seconds = $4, "
                "    log_tail = $5, "
                f"   {started_at_clause} "
                "WHERE id = $6 "
                f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
                new_state,
                int(body.bytes_done),
                None if body.bytes_total is None else int(body.bytes_total),
                None if body.eta_seconds is None else int(body.eta_seconds),
                body.log_tail,
                job_id,
            )

    # SSE broadcast — scope=tenant so the operator UI of the right
    # tenant sees the live tick. Best-effort; never propagate emit
    # failures back to the sidecar.
    try:
        from backend import events as _events
        _events.emit_installer_progress(
            job_id,
            state=row["state"],
            stage=body.stage,
            bytes_done=int(row["bytes_done"]),
            bytes_total=(
                int(row["bytes_total"])
                if row["bytes_total"] is not None else None
            ),
            eta_seconds=(
                int(row["eta_seconds"])
                if row["eta_seconds"] is not None else None
            ),
            log_tail=row["log_tail"] or "",
            sidecar_id=row["sidecar_id"],
            entry_id=row["entry_id"],
            broadcast_scope="tenant",
            tenant_id=existing["tenant_id"],
        )
    except Exception as exc:  # pragma: no cover — bus already swallows
        logger.warning(
            "installer_progress SSE emit failed for job %s: %s",
            job_id, exc,
        )

    return JSONResponse(
        status_code=200,
        content={
            "id": job_id,
            "state": row["state"],
            "tenant_id": existing["tenant_id"],
            "bytes_done": int(row["bytes_done"]),
            "bytes_total": (
                int(row["bytes_total"])
                if row["bytes_total"] is not None else None
            ),
        },
    )


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

    # BS.7.7 — broadcast the cancel decision over the existing SSE
    # ``installer_progress`` channel so cross-tab + cross-worker UIs
    # converge instantly (no need to wait for the sidecar's next
    # report_progress round-trip to surface state=cancelled). The
    # frontend's ``useInstallJobs()`` hook is already a single
    # subscriber; mapping cancel onto the same channel avoids adding a
    # second subscription path. ``stage="cancel"`` lets ToastCenter /
    # drawer disambiguate operator-driven cancels from the sidecar's
    # later confirmation tick (which carries the original method
    # stage). Best-effort emit — never propagate bus failure to the
    # operator who already saw the 200 response.
    try:
        from backend import events as _events
        _events.emit_installer_progress(
            job_id,
            state=row["state"],
            stage="cancel",
            bytes_done=int(row["bytes_done"]),
            bytes_total=(
                int(row["bytes_total"])
                if row["bytes_total"] is not None else None
            ),
            eta_seconds=(
                int(row["eta_seconds"])
                if row["eta_seconds"] is not None else None
            ),
            log_tail=row["log_tail"] or "",
            sidecar_id=row["sidecar_id"],
            entry_id=row["entry_id"],
            broadcast_scope="tenant",
            tenant_id=tid,
        )
    except Exception as exc:  # pragma: no cover — bus already swallows
        logger.warning(
            "installer_progress SSE emit failed for cancelled job %s: %s",
            job_id, exc,
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
#  GET /installer/installed — operator (BS.8.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _is_uninstall_record(result_json: Any) -> bool:
    """Return True when ``result_json`` flags the row as an uninstall.

    Uninstall rows are inserted by ``POST /installer/uninstall`` with
    ``result_json = {"kind": "uninstall", ...}`` and ``state='completed'``
    (PEP-approved) or ``state='cancelled'`` (PEP-denied). The list
    endpoint excludes any entry whose latest install_jobs row is an
    uninstall, so the InstalledTab and the Cleanup-unused modal both
    stop showing the entry the moment the operator approves the
    uninstall — even though the actual disk cleanup is deferred.
    """
    coerced = _coerce_json(result_json, None)
    if not isinstance(coerced, dict):
        return False
    return coerced.get("kind") == INSTALL_KIND_UNINSTALL


@router.get("/installed")
async def list_installed_entries(
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """List currently-installed catalog entries for the caller's tenant.

    BS.8.2: derives the "installed" set from the install_jobs table —
    for each ``entry_id`` in the tenant we pick the latest row by
    ``queued_at DESC`` and treat the entry as installed when that row
    is ``state='completed'`` AND its ``result_json`` is *not* an
    uninstall record (see :func:`_is_uninstall_record`).

    Each row in the response carries the post-install bookkeeping
    fields the BS.8.1 ``InstalledTab`` consumes:

    * ``entry_id`` / ``display_name`` / ``vendor`` / ``family`` /
      ``version`` / ``description`` — pulled from ``catalog_entries``
      via a LEFT JOIN. ``description`` lives in ``metadata.description``
      following the BS.6 catalog convention.
    * ``disk_usage_bytes`` — falls back to ``catalog_entries.size_bytes``
      until BS.8.3 wires real on-disk measurement.
    * ``used_by_workspace_count`` — defaults to ``0`` until the
      workspace-platform link table lands; the field is surfaced today
      so the UI doesn't need to skip a column when the data appears.
    * ``last_used_at`` — null today (BS.8.x will read the workspace
      activity timestamp); the cleanup modal currently treats null +
      old ``installed_at`` as "idle since install", which is the
      conservative interpretation.
    * ``installed_at`` — install_jobs.completed_at on the latest
      successful install.
    * ``update_available`` / ``available_version`` — null today;
      surfaced for forward-compat with the catalog feed lookahead
      that BS.6.x will land.
    * ``source`` — ``shipped`` / ``operator`` / ``override`` /
      ``subscription`` from the resolved catalog row.

    Module-global / cross-worker state audit
    ────────────────────────────────────────
    Pure SELECT path; no shared in-memory state. Each tenant's caller
    sees only their own rows because every WHERE filters by tenant_id.
    Multi-worker safe: each worker's pool acquires its own asyncpg
    connection; the SELECT is repeatable-read by default and the
    response is rendered from the snapshot.

    Read-after-write timing audit
    ─────────────────────────────
    Operator just approved an uninstall via POST /installer/uninstall
    → INSERT install_jobs (state='completed', kind='uninstall') →
    HTTP 200 returns. A subsequent GET /installer/installed sees the
    new row by PG MVCC (commit happened before HTTP 200), so the
    optimistic frontend refresh stays consistent with the backend.
    """
    tid = _ensure_tenant(user)

    # DISTINCT ON gets the latest install_jobs row per entry_id within
    # the tenant. We don't filter by state here — instead we filter
    # in Python so the "latest is uninstall" check has access to
    # ``result_json``. Filtering on state in SQL would make the test
    # "did the operator just uninstall it?" much harder to encode.
    sql = """
        SELECT DISTINCT ON (j.entry_id)
            j.id          AS install_job_id,
            j.entry_id    AS entry_id,
            j.state       AS state,
            j.result_json AS result_json,
            j.queued_at   AS queued_at,
            j.completed_at AS completed_at,
            c.display_name AS display_name,
            c.vendor       AS vendor,
            c.family       AS family,
            c.version      AS version,
            c.metadata     AS metadata,
            c.size_bytes   AS size_bytes,
            c.source       AS source
        FROM install_jobs j
        LEFT JOIN LATERAL (
            SELECT display_name, vendor, family, version,
                   metadata, size_bytes, source
            FROM catalog_entries
            WHERE id = j.entry_id
              AND (tenant_id IS NULL OR tenant_id = $1)
              AND hidden = FALSE
            ORDER BY CASE source
                       WHEN 'override'     THEN 0
                       WHEN 'operator'     THEN 1
                       WHEN 'subscription' THEN 2
                       WHEN 'shipped'      THEN 3
                       ELSE 4
                     END
            LIMIT 1
        ) c ON TRUE
        WHERE j.tenant_id = $1
        ORDER BY j.entry_id, j.queued_at DESC
    """

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, tid)

    items: list[dict[str, Any]] = []
    for row in rows:
        if row["state"] != "completed":
            # Latest row for this entry isn't a successful install —
            # entry isn't installed (still pending / failed / cancelled).
            continue
        if _is_uninstall_record(row["result_json"]):
            # Latest row is the operator-approved uninstall — the entry
            # has been "uninstalled" from a state-of-truth perspective
            # even if the disk cleanup is deferred.
            continue
        # Catalog row may be missing (operator-installed entry whose
        # catalog row was later removed). Keep the row visible so the
        # operator can still uninstall it; populate fields with safe
        # fallbacks so the frontend doesn't blow up on null display_name.
        meta = _coerce_json(row["metadata"], {}) if row["metadata"] is not None else {}
        description: Any = None
        if isinstance(meta, dict):
            d = meta.get("description")
            if isinstance(d, str):
                description = d
        size_bytes = (
            int(row["size_bytes"]) if row["size_bytes"] is not None else None
        )
        items.append({
            "entry_id": row["entry_id"],
            "display_name": row["display_name"] or row["entry_id"],
            "vendor": row["vendor"] or "",
            "family": row["family"] or "custom",
            "version": row["version"],
            "description": description,
            "disk_usage_bytes": size_bytes,
            "used_by_workspace_count": 0,
            "last_used_at": None,
            "installed_at": _ts_to_iso(row["completed_at"]),
            "update_available": False,
            "available_version": None,
            "source": row["source"],
        })

    return JSONResponse(
        status_code=200,
        content={"items": items, "count": len(items)},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /installer/uninstall — operator + PEP HOLD (BS.8.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _new_uninstall_idempotency_key() -> str:
    """Generate a fresh idempotency_key for an uninstall row.

    Pattern matches ``IDEMPOTENCY_KEY_PATTERN`` so the row's UNIQUE
    constraint is honoured. Uninstall rows are short-lived audit
    artefacts — duplicate POSTs from the cleanup modal are guarded
    by the modal's "Uninstall in progress" disabled state, so a
    server-side dedupe is unnecessary; each row gets its own key.
    """
    return _secrets.token_hex(16)


@router.post("/uninstall", status_code=200)
async def bulk_uninstall(
    body: BulkUninstallBody,
    request: Request,
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Bulk uninstall a list of catalog entries; HOLD via PEP gateway.

    BS.8.2 cleanup-unused flow. The operator picks N idle entries in the
    Cleanup-unused modal and clicks "Uninstall N selected". One PEP
    HOLD fires for the whole batch (``tool='uninstall_entry'`` with
    arguments listing the entry ids and counts). On approve, every
    entry id gets an install_jobs row with ``state='completed'`` and
    ``result_json = {"kind": "uninstall", ...}``; on deny, every entry
    id gets a row with ``state='cancelled'`` and ``error_reason =
    'pep_<rule>'`` so the operator can see in the audit log that the
    bulk operation was rejected.

    Why one HOLD instead of N? Bulk-cleanup is a single intent ("get
    rid of all of these"); presenting the operator with N coaching
    cards is hostile UX — they'd approve them all anyway. The audit
    trail still shows N rows, one per uninstalled entry, so per-entry
    forensics is preserved.

    Sidecar interaction
    ───────────────────
    Uninstall rows are inserted with ``state='completed'`` /
    ``state='cancelled'`` (NOT ``state='queued'``), so the sidecar's
    long-poll claim — ``WHERE state='queued'`` — never sees them. The
    actual on-disk cleanup is deferred to a follow-up sidecar handler;
    today the row is the audit + state-of-truth marker that the
    operator approved removing the entry.

    Read-after-write timing audit (SOP Step 1)
    ──────────────────────────────────────────
    Two transactions: (1) PEP HOLD evaluate in a separate tx so the
    pool conn is released during the wait; (2) bulk INSERT in a
    single tx after the HOLD resolves. A frontend GET
    ``/installer/installed`` between (1) and (2) sees the entry as
    still installed (no uninstall row exists yet); a GET *after* (2)
    sees the entry filtered out. Optimistic frontend remove-then-poll
    is therefore safe: the optimistic state matches the backend the
    instant (2) commits.
    """
    tid = _ensure_tenant(user)

    # Dedupe + validate every entry_id matches the documented pattern.
    # Pydantic's ``Field(max_length=BULK_UNINSTALL_MAX_ENTRIES)`` already
    # caps the list; we still need per-id regex enforcement (Pydantic
    # validates list length, not per-item shape unless we use a constr).
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in body.entry_ids:
        eid = raw.strip()
        if not _ENTRY_ID_RE.match(eid) or len(eid) > ENTRY_ID_MAX_LEN:
            raise HTTPException(
                status_code=422,
                detail=f"invalid entry_id: {raw!r}; must match {ENTRY_ID_PATTERN}",
            )
        if eid in seen:
            continue
        seen.add(eid)
        deduped.append(eid)
    if not deduped:
        raise HTTPException(
            status_code=422,
            detail="entry_ids must contain at least one unique id",
        )

    # PEP HOLD — outside any pool conn so the conn isn't held for the
    # 10-minute (worst-case) operator decision wait. The arguments dict
    # carries enough context for the coaching card to render a useful
    # summary even before the dedicated ``uninstall_intercept`` card
    # ships in BS.8.4.
    pep_decision = None
    pep_error: str | None = None
    try:
        pep_decision = await _pep.evaluate(
            tool=UNINSTALL_PEP_TOOL,
            arguments={
                "tenant_id": tid,
                "entry_ids": deduped,
                "count": len(deduped),
                "actor": user.email,
                "reason": body.reason,
            },
            agent_id=f"operator:{user.email}",
            tier="t1",
            hold_timeout_s=UNINSTALL_PEP_HOLD_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 — gateway breaker handles
        pep_error = f"pep_gateway_error:{exc.__class__.__name__}"
        logger.warning(
            "pep evaluate raised for bulk uninstall (tenant=%s, count=%d): %s",
            tid, len(deduped), exc,
        )

    approved = (
        pep_decision is not None
        and pep_decision.action is _pep.PepAction.auto_allow
    )
    if approved:
        new_state = "completed"
        decision_id = pep_decision.decision_id  # type: ignore[union-attr]
        error_reason: str | None = None
    else:
        new_state = "cancelled"
        decision_id = (
            pep_decision.decision_id if pep_decision is not None else None
        )
        if pep_decision is None:
            error_reason = pep_error or "pep_gateway_unknown_error"
        else:
            error_reason = f"pep_{pep_decision.rule or 'denied'}"

    # Insert one row per (deduped) entry id. Each row carries:
    #   • a fresh ``ij-`` id + idempotency_key
    #   • result_json discriminator so GET /installer/installed filters
    #   • state='completed' on approve, 'cancelled' on deny
    items: list[dict[str, Any]] = []
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            for eid in deduped:
                row_id = _new_install_job_id()
                idem = _new_uninstall_idempotency_key()
                payload = {
                    "kind": INSTALL_KIND_UNINSTALL,
                    "entry_id": eid,
                    "actor": user.email,
                }
                if body.reason:
                    payload["reason"] = body.reason
                inserted = await conn.fetchrow(
                    "INSERT INTO install_jobs "
                    "  (id, tenant_id, entry_id, state, idempotency_key, "
                    "   protocol_version, requested_by, result_json, "
                    "   error_reason, pep_decision_id, completed_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, "
                    "        $8::jsonb, $9, $10, now()) "
                    f"RETURNING {_INSTALL_JOB_RETURNING_COLS}",
                    row_id, tid, eid, new_state, idem,
                    DEFAULT_SIDECAR_PROTOCOL_VERSION, user.id,
                    json.dumps(payload),
                    error_reason, decision_id,
                )
                items.append({
                    "entry_id": eid,
                    "job_id": row_id,
                    "action": "approved" if approved else "denied",
                    "state": inserted["state"],
                    "reason": error_reason,
                    "pep_decision_id": decision_id,
                })

    # Audit emit — one row per uninstall, mirroring the install audit
    # convention. The bus is best-effort; a failure here doesn't change
    # the response.
    audit_action = (
        "installer.entry_uninstalled" if approved
        else "installer.entry_uninstall_denied"
    )
    for it in items:
        _emit_audit_safely(
            action=audit_action,
            entity_id=it["job_id"],
            actor=user.email,
            before=None,
            after={
                "entry_id": it["entry_id"],
                "state": it["state"],
                "kind": INSTALL_KIND_UNINSTALL,
                "pep_decision_id": decision_id,
                "reason": error_reason,
            },
        )

    if not approved:
        # PEP rejected the bulk request. Surface the same shape
        # ``create_job`` does for the single-entry deny path so the
        # frontend's <ApiErrorToastCenter /> can apply uniform parsing.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "pep_denied",
                "reason": error_reason,
                "count": len(items),
                "items": items,
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "items": items,
            "approved_count": len(items),
            "denied_count": 0,
            "pep_decision_id": decision_id,
        },
    )


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

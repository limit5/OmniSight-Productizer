"""Y4 (#280) row 1 + row 2 + row 3 + row 4 + row 5 — tenant-scoped project
REST surface.

Row 1 — POST /api/v1/tenants/{tid}/projects: create a project under a
tenant.  Row 2 — GET /api/v1/tenants/{tid}/projects: list projects in
a tenant, scoped by the caller's visibility (super_admin / tenant
admin → full; member / viewer → only projects with explicit
``project_members`` rows).  Row 3 — PATCH
/api/v1/tenants/{tid}/projects/{pid}: partial update of name,
plan_override, disk_budget_bytes, parent_id (with cycle detection
for sub-project trees).  Row 4 — POST
/api/v1/tenants/{tid}/projects/{pid}/archive + .../restore: soft-
archive a project (workspaces / agents / crons should treat the row
as inert while the data is retained for ``OMNISIGHT_PROJECT_GC_RETENTION_DAYS``
days, default 90); restore lifts the archive within the retention
window.  After the retention window the background GC helper
``gc_archived_projects`` permanently removes the row and emits a
billing audit event so accounting can reconcile the freed quota.
Row 5 — POST /api/v1/tenants/{tid}/projects/{pid}/members + PATCH
/.../members/{user_id} + DELETE /.../members/{user_id}: project-level
membership grants overlaying the tenant default. Roles are the
``project_members`` enum (``owner / contributor / viewer``); see
``alembic/versions/0034_project_members.py`` for the table schema and
the default-resolution semantics (admin/owner tenant membership →
contributor on every project of the tenant; member/viewer → no
project access without an explicit row).

A project is the unit at which budgets / quotas / sharing get bound
(Y1 row 2 schema lives in ``alembic/versions/0033_projects.py``); a
tenant typically owns several projects across one or more product
lines (embedded / web / mobile / software / custom).

Endpoint contract
─────────────────
::

    POST /api/v1/tenants/{tid}/projects
    body  : {
              product_line  : "embedded|web|mobile|software|custom",
              name          : "<1..200 chars>",
              slug          : "<1..64 chars, ^[a-z0-9][a-z0-9-]*$>",
              plan_override : null | "free|starter|pro|enterprise",
              disk_budget_bytes : null | non-negative int,
              parent_id     : null | "p-..."
            }
    auth  : tenant admin or above on the target tenant or platform
            super_admin (same trust boundary as the Y3 invite + member
            management surfaces)
    out   : 201 {project_id, tenant_id, product_line, name, slug,
                 parent_id, plan_override, disk_budget_bytes,
                 llm_budget_tokens, created_by, created_at, archived_at}
    errors: 403 RBAC · 404 unknown tenant · 409 slug already taken
            in (tenant_id, product_line) · 422 malformed body / id /
            unknown product_line / unknown plan / negative budget /
            parent_id refers to a project that does not exist OR
            lives in a different tenant

Slug uniqueness is the (tenant_id, product_line, slug) composite UNIQUE
index from migration 0033 — meaning two projects can share a slug if
they live in different product lines (the embedded ``isp-tuning`` and
the web ``isp-tuning`` are distinct workloads).

Product line whitelist (TODO row literal)
─────────────────────────────────────────
``embedded`` / ``web`` / ``mobile`` / ``software`` / ``custom`` map to
the L4 skill-pack D / W / P / X / custom buckets. The DB CHECK
constraint on ``projects.product_line`` is purely length-based (1..64
chars) — the application layer is the only enforcement of the
whitelist, so this router's pydantic Literal IS the source of truth.

Parent linkage
──────────────
``parent_id`` may be omitted for top-level projects, or set to an
existing project in the SAME tenant for sub-projects. The DB enforces
``parent_id <> id`` at INSERT time (zero-cycle); deeper cycle detection
is unnecessary at create-time because the new project's id is freshly
minted in the handler — it cannot already appear in any chain.

Per-project quota inheritance
─────────────────────────────
``plan_override`` / ``disk_budget_bytes`` / ``llm_budget_tokens`` are
nullable on the row; ``NULL`` means "inherit from tenant". This handler
accepts ``plan_override`` and ``disk_budget_bytes`` from the body
(matching the TODO row literal); ``llm_budget_tokens`` is left for Y4
row 7 (per-project quota override). Negative budgets are rejected at
the schema layer; the DB CHECK is defence in depth.

Module-global state audit (SOP Step 1)
──────────────────────────────────────
None introduced. ``TENANT_ID_PATTERN`` / ``PROJECT_ID_PATTERN`` /
``SLUG_PATTERN`` regex strings + their compiled forms,
``PRODUCT_LINE_ENUM`` / ``PROJECT_PLAN_ENUM`` tuples, and three SQL
constants are all module-level immutable; each uvicorn worker derives
the same values from source. The asyncpg pool is shared via PG. No new
in-memory cache.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Single INSERT ... ON CONFLICT DO NOTHING RETURNING gives "insert-or-
detect-duplicate" atomicity — concurrent admins racing the same
``(tenant_id, product_line, slug)`` triple resolve to exactly one
winner (RETURNING populated) and one loser (RETURNING None → 409 by
re-fetching the existing row). Parent existence is checked before the
INSERT; on a race where the parent is deleted between probe and
INSERT, the FK ``ON DELETE SET NULL`` lets the row land with
``parent_id=NULL`` rather than failing — acceptable per the migration's
documented semantics.

Production readiness gate
─────────────────────────
* No new wheel — FastAPI / asyncpg / pydantic already shipped.
* No schema migration — projects table already shipped in 0033.
* No new env knob.
* No docker change.
* Production status after this commit: dev-only. Next gate is
  deployed-inactive — the prod image picks up the new code on rebuild;
  no operator action is required to flip a flag.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import auth
from backend.db_pool import get_pool
from backend import audit as _audit

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tenant-projects"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation patterns + whitelists
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant id — same source-of-truth as Y2 admin_tenants / Y3 row 1/6.
TENANT_ID_PATTERN = r"^t-[a-z0-9][a-z0-9-]{2,62}$"
_TENANT_ID_RE = re.compile(TENANT_ID_PATTERN)

# Project id — minted by this handler. Format ``p-<16 hex chars>`` keeps
# parity with the ``inv-<token_hex(8)>`` and ``u-<token_hex(...)>``
# conventions and stays well under the migration's length CHECKs.
PROJECT_ID_PATTERN = r"^p-[a-z0-9][a-z0-9-]{2,63}$"
_PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)

# Slug — lower-case alnum + hyphen only. Mirrors the migration's docs:
# "The slug character class is enforced at the application layer
# (Y2 will reject non ^[a-z0-9-]+$); the DB level check is just length."
# Adding a leading-char restriction so ``--cli-flag-like`` slugs cannot
# slip through and cause CLI / URL ambiguity downstream.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_SLUG_RE = re.compile(SLUG_PATTERN)

# Product line whitelist (TODO row literal). The DB CHECK is purely
# length-based; this tuple is the canonical enforcement.
PRODUCT_LINE_ENUM = ("embedded", "web", "mobile", "software", "custom")

# Plan override whitelist — must match the migration's CHECK on
# ``projects.plan_override``.
PROJECT_PLAN_ENUM = ("free", "starter", "pro", "enterprise")


def _is_valid_tenant_id(tid: str) -> bool:
    return bool(tid) and bool(_TENANT_ID_RE.match(tid))


def _is_valid_project_id(pid: str) -> bool:
    return bool(pid) and bool(_PROJECT_ID_RE.match(pid))


def _mint_project_id() -> str:
    """``p-`` + 16 hex chars (64 random bits). Distinct enough to make
    collisions a non-event over the lifetime of the platform."""
    return f"p-{secrets.token_hex(8)}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic body
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CreateProjectRequest(BaseModel):
    """Body for ``POST /api/v1/tenants/{tid}/projects``."""

    product_line: Literal[
        "embedded", "web", "mobile", "software", "custom",
    ] = Field(
        description=(
            "Product line bucket — must be one of (embedded, web, "
            "mobile, software, custom). Maps to the L4 skill-pack "
            "D / W / P / X / custom slots. Pydantic 422s on anything "
            "else before the handler runs."
        ),
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Display name. Operator-facing free-form text. Length "
            "matches the ``projects.name`` CHECK (1..200)."
        ),
    )
    slug: str = Field(
        pattern=SLUG_PATTERN,
        min_length=1,
        max_length=64,
        description=(
            "URL-safe shortname; must match ^[a-z0-9][a-z0-9-]*$. "
            "Unique within (tenant_id, product_line) per the "
            "migration 0033 composite UNIQUE."
        ),
    )
    plan_override: Literal[
        "free", "starter", "pro", "enterprise",
    ] | None = Field(
        default=None,
        description=(
            "Per-project plan override. ``None`` means inherit from "
            "tenant. Must match the migration's CHECK enum."
        ),
    )
    disk_budget_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Per-project disk quota override in bytes. ``None`` means "
            "inherit from tenant. Must be non-negative; the DB CHECK "
            "is defence in depth. Non-NULL values are subject to the "
            "Y4 row 7 oversell guard: Σ(non-NULL project budgets on "
            "this tenant) must not exceed the tenant's plan ceiling."
        ),
    )
    llm_budget_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Per-project LLM token override. ``None`` means inherit "
            "from the tenant's plan ceiling (see "
            "``backend.project_quota.PLAN_LLM_TOKEN_QUOTAS``). Must "
            "be non-negative. Subject to the same Σ ≤ tenant total "
            "oversell guard as ``disk_budget_bytes``."
        ),
    )
    parent_id: str | None = Field(
        default=None,
        pattern=PROJECT_ID_PATTERN,
        description=(
            "Parent project id for sub-project trees. ``None`` means "
            "top-level. The parent must belong to the same tenant."
        ),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorisation — tenant admin / owner on the target tenant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Membership roles that may create projects. ``member`` and ``viewer``
# may NOT (read-write-non-admin / read-only).
_PROJECT_CREATE_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})

# Y4 row 7 — per-tenant advisory lock prefix taken inside the POST /
# PATCH transaction whenever a project budget override is being set
# (non-NULL ``disk_budget_bytes`` or ``llm_budget_tokens``). Serialises
# concurrent POSTs / PATCHes within a tenant so two concurrent admins
# cannot each see the budget "fits" sum independently and then both
# commit (which together would breach the tenant cap). Per-tenant key
# (not platform-wide) so cross-tenant traffic does not collide. This
# is distinct from ``_PROJECT_PATCH_LOCK_PREFIX`` (re-parent
# serialisation) — the two contend on different surfaces and a single
# PATCH may take both.
_PROJECT_QUOTA_LOCK_PREFIX = "omnisight_project_quota:"


async def _user_can_create_project_in(
    user: auth.User,
    tenant_id: str,
) -> bool:
    """True iff ``user`` may create projects under ``tenant_id``.

    Order of checks (cheap → expensive):

      1. Platform ``super_admin`` — always allowed (matches Y3 trust
         boundary; super-admin manages cross-tenant state).
      2. Active membership row with role ∈ {owner, admin} on the
         target tenant — DB lookup against ``user_tenant_memberships``.

    The legacy ``users.role`` cache is NOT consulted: a user who is
    "admin on tenant A" must not be allowed to create projects under
    tenant B unless they are also explicitly admin on B.
    """
    if auth.role_at_least(user.role, "super_admin"):
        return True

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
    return row["role"] in _PROJECT_CREATE_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant existence probe — clean 404 ahead of the FK validation.
# ``plan`` is projected so the Y4 row 7 oversell guard can resolve the
# tenant's plan ceiling without a second round-trip; existing call
# sites that only check existence may discard the plan column.
_FETCH_TENANT_SQL = "SELECT id, plan FROM tenants WHERE id = $1"

# Parent existence + same-tenant probe. We project ``tenant_id`` so the
# handler can distinguish "parent does not exist" (row is None) from
# "parent exists but in a different tenant" (row["tenant_id"] !=
# requested) and surface a clear 422 in either case.
_FETCH_PARENT_PROJECT_SQL = (
    "SELECT id, tenant_id FROM projects WHERE id = $1"
)

# Atomic create with ``ON CONFLICT DO NOTHING`` — the UNIQUE
# ``(tenant_id, product_line, slug)`` index makes RETURNING None mean
# "duplicate slug in this product line on this tenant". On the duplicate
# branch we re-SELECT the colliding row to surface its id in the 409
# body (so the operator can navigate / recover without guessing).
_INSERT_PROJECT_SQL = """
INSERT INTO projects (
    id, tenant_id, product_line, name, slug,
    parent_id, plan_override, disk_budget_bytes,
    llm_budget_tokens, created_by
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8,
    $9, $10
)
ON CONFLICT (tenant_id, product_line, slug) DO NOTHING
RETURNING id, tenant_id, product_line, name, slug,
          parent_id, plan_override, disk_budget_bytes,
          llm_budget_tokens, created_by, created_at, archived_at
"""

# Used to surface the existing row's id when the INSERT lost the dup
# race. Fetched via the UNIQUE composite index — single-row.
_FETCH_EXISTING_PROJECT_SQL = """
SELECT id, name, slug
FROM projects
WHERE tenant_id = $1 AND product_line = $2 AND slug = $3
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Handler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _row_to_project_dict(row) -> dict:
    """Project the RETURNING row to a JSON-serialisable response body."""
    return {
        "project_id": row["id"],
        "tenant_id": row["tenant_id"],
        "product_line": row["product_line"],
        "name": row["name"],
        "slug": row["slug"],
        "parent_id": row["parent_id"],
        "plan_override": row["plan_override"],
        "disk_budget_bytes": row["disk_budget_bytes"],
        "llm_budget_tokens": row["llm_budget_tokens"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "archived_at": row["archived_at"],
    }


def _resolve_created_by(actor: auth.User) -> str | None:
    """The ``users(id)`` FK on ``projects.created_by`` is ON DELETE SET
    NULL. Synthetic anonymous (open-mode dev fallback) and api-key
    callers do not have rows in ``users``; storing their id would FK-
    violate. NULL preserves the audit trail (actor.email is still
    captured on the audit row) without breaking the FK.
    """
    aid = actor.id
    if aid == "anonymous":
        return None
    if isinstance(aid, str) and aid.startswith("apikey:"):
        return None
    return aid


@router.post("/tenants/{tenant_id}/projects", status_code=201)
async def create_project(
    tenant_id: str,
    body: CreateProjectRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Create a project under ``tenant_id``.

    Returns 201 with the full project row on success; 403 / 404 / 409
    / 422 for the conditions documented in the module docstring.
    """
    # 1. Tenant id format gate — defence-in-depth ahead of FastAPI's
    #    route-pattern matching (path params don't get pydantic
    #    validation by default).
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )

    # 2. RBAC — done before the tenant existence probe so a guess-the-
    #    tenant-id scan can't enumerate tenants via timing alone (both
    #    branches require a qualifying membership / role anyway).
    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )

    # 3. Tenant existence — 404 on miss. The membership check above
    #    succeeds for super-admins regardless of whether the tenant
    #    exists, so the explicit existence probe is necessary even for
    #    that branch.
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 4. Parent linkage validation — must exist AND live in the same
    #    tenant. A parent in a different tenant is a 422 (the operator
    #    likely typed the wrong id) rather than 403 (the operator does
    #    have admin on the target tenant by construction here).
    if body.parent_id is not None:
        async with get_pool().acquire() as conn:
            parent_row = await conn.fetchrow(
                _FETCH_PARENT_PROJECT_SQL, body.parent_id,
            )
        if parent_row is None:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"parent_id {body.parent_id!r} does not exist"
                    ),
                    "tenant_id": tenant_id,
                    "parent_id": body.parent_id,
                },
            )
        if parent_row["tenant_id"] != tenant_id:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"parent_id {body.parent_id!r} belongs to a "
                        f"different tenant; cross-tenant parent links "
                        f"are not permitted"
                    ),
                    "tenant_id": tenant_id,
                    "parent_id": body.parent_id,
                    "parent_tenant_id": parent_row["tenant_id"],
                },
            )

    # 5. Mint the project id + run the oversell guard + INSERT inside a
    #    single transaction. The per-tenant ``pg_advisory_xact_lock`` is
    #    taken whenever the body sets a non-NULL budget override so that
    #    concurrent POSTs / PATCHes on the same tenant cannot each
    #    independently see a "fits" sum and then both commit (which
    #    together would exceed the tenant cap). NULL-budget POSTs skip
    #    the lock + the SUM round-trip entirely, keeping the common
    #    "inherit from tenant" path lock-free.
    project_id = _mint_project_id()
    created_by = _resolve_created_by(actor)
    tenant_plan = tenant_row["plan"]
    needs_quota_lock = (
        body.disk_budget_bytes is not None
        or body.llm_budget_tokens is not None
    )

    from backend import project_quota as _pq

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            if needs_quota_lock:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    _PROJECT_QUOTA_LOCK_PREFIX + tenant_id,
                )
                try:
                    # POST has no existing row to exclude (the project
                    # is being minted in this transaction; its budget
                    # is not yet in the SUM).
                    await _pq.check_disk_budget_oversell(
                        conn,
                        tenant_id=tenant_id,
                        tenant_plan=tenant_plan,
                        exclude_project_id=None,
                        new_value=body.disk_budget_bytes,
                    )
                    await _pq.check_llm_budget_oversell(
                        conn,
                        tenant_id=tenant_id,
                        tenant_plan=tenant_plan,
                        exclude_project_id=None,
                        new_value=body.llm_budget_tokens,
                    )
                except _pq.ProjectBudgetOversell as exc:
                    return JSONResponse(
                        status_code=409,
                        content=exc.to_response_body(),
                    )

            row = await conn.fetchrow(
                _INSERT_PROJECT_SQL,
                project_id, tenant_id, body.product_line,
                body.name, body.slug,
                body.parent_id, body.plan_override, body.disk_budget_bytes,
                body.llm_budget_tokens, created_by,
            )

    if row is None:
        # 6a. Duplicate slug branch — surface the existing row's id so
        #     UI can navigate to it instead of forcing a re-list.
        async with get_pool().acquire() as conn:
            existing = await conn.fetchrow(
                _FETCH_EXISTING_PROJECT_SQL,
                tenant_id, body.product_line, body.slug,
            )
        existing_id = existing["id"] if existing else None
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"slug {body.slug!r} already taken in "
                    f"(tenant_id={tenant_id!r}, "
                    f"product_line={body.product_line!r}); revoke or "
                    f"rename the existing project before reusing"
                ),
                "tenant_id": tenant_id,
                "product_line": body.product_line,
                "slug": body.slug,
                "existing_project_id": existing_id,
            },
        )

    # 6b. Happy path — emit audit. Best-effort; failures are logged at
    #     warning and never raise (matches every other audit.log
    #     callsite in this codebase).
    project = _row_to_project_dict(row)
    try:
        await _audit.log(
            action="tenant_project_created",
            entity_kind="project",
            entity_id=project_id,
            before=None,
            after={
                "project_id": project_id,
                "tenant_id": tenant_id,
                "product_line": body.product_line,
                "name": body.name,
                "slug": body.slug,
                "parent_id": body.parent_id,
                "plan_override": body.plan_override,
                "disk_budget_bytes": body.disk_budget_bytes,
                "llm_budget_tokens": body.llm_budget_tokens,
                "created_by": created_by,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_created audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
        )

    # Y9 #285 row 1 — canonical dot-notation event ``project.created``.
    try:
        from backend import audit_events as _audit_events
        await _audit_events.emit_project_created(
            tenant_id=tenant_id,
            project_id=project_id,
            name=body.name,
            slug=body.slug,
            product_line=body.product_line,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "project.created audit emit failed (tenant=%s project=%s): %s",
            tenant_id, project_id, exc,
        )

    return JSONResponse(status_code=201, content=project)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y4 (#280) row 2 — GET /api/v1/tenants/{tid}/projects
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# List the projects a caller may see inside one tenant. Two query
# params (per the TODO row literal):
#
#   ?product_line=embedded|web|mobile|software|custom   (optional)
#   ?archived=false|true|all                            (default false)
#
# Visibility rule (per the TODO row + alembic 0034 default-resolution
# semantics):
#
#   • Platform ``super_admin`` → sees every project of the tenant.
#   • Tenant membership role ∈ {owner, admin}            → sees every
#     project of the tenant. The 0034 docstring documents that an
#     admin/owner membership row is treated as ``contributor`` on
#     every project of that tenant by default; "contributor on every
#     project" implies "can list every project".
#   • Tenant membership role ∈ {member, viewer}          → sees only
#     projects with an explicit ``project_members`` row for them.
#     The 0034 docstring is explicit: "member and viewer fall through
#     to no project access by default" — the default-resolution does
#     NOT promote them to contributor; they need an explicit per-
#     project grant.
#   • No active membership AND not super_admin           → 403. List
#     does not enumerate (project ids + slugs would otherwise leak
#     the tenant's product portfolio to a non-member).
#
# Auth wording note: a *suspended* membership row is treated as no
# membership for visibility — the same way Y3 row 6 PATCH/DELETE
# membership treats it.
#
# SQL design — single template, three archived branches
# ──────────────────────────────────────────────────────
# Visibility is collapsed to a boolean ``$caller_has_full_visibility``
# that the handler computes once before issuing the query. The SQL
# then uses ``$2::bool OR EXISTS (SELECT 1 FROM project_members ...)``
# to short-circuit the per-row membership probe for full-visibility
# callers, leaving a single planner-friendly EXISTS for the explicit-
# only branch. Three SQL constants (live / archived / all) cover the
# archived predicate, since ``archived_at IS NULL`` vs ``IS NOT NULL``
# vs no filter is a column-shape concern, not a value concern, and
# can't be parameterised cleanly.
#
# The ``idx_projects_tenant_active`` partial index (alembic 0033)
# is the hot-path index for the live branch; the archived + all
# branches walk the UNIQUE composite index with a tenant_id leading
# column (also from 0033).
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# Three new module-level SQL constants + ``LISTABLE_PROJECT_ARCHIVED_FILTERS``
# tuple + ``PROJECTS_LIST_*_LIMIT`` ints + ``_PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES``
# frozenset. All immutable; every uvicorn worker derives the same
# value from the same source — qualifying answer #1. DB state is
# shared via PG (qualifying answer #2). No new in-memory cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# Pure read endpoint — no writes. Under concurrent POST + GET the
# GET caller may either see or miss a freshly-inserted project
# depending on which transaction commits first; standard read-
# committed behaviour, not a regression.

# The full set of values the ``?archived=`` query parameter accepts.
# ``false`` is the default if the caller omits the param — the most
# common UI need is "show me my live projects". ``all`` is the
# audit-style "show everything"; ``true`` returns archived-only.
LISTABLE_PROJECT_ARCHIVED_FILTERS = ("false", "true", "all")

# Hard cap on rows projected per call. Keeps the response bounded
# under a tenant that has accumulated thousands of projects over
# years; the admin console paginates client-side. Same shape as the
# Y3 invite-list cap.
PROJECTS_LIST_DEFAULT_LIMIT = 100
PROJECTS_LIST_MAX_LIMIT = 500

# Membership roles that get full visibility into the tenant's
# project list. ``member`` / ``viewer`` fall through to explicit-only
# (per alembic 0034 default-resolution semantics).
_PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


# Three SQL templates — same SELECT shape, different archived predicate.
# Placeholder layout is identical across all three so the handler can
# pick a constant by branch and pass the same args:
#   $1 = tenant_id (text)
#   $2 = caller_has_full_visibility (bool)
#   $3 = caller_user_id (text)            -- used only when $2 is FALSE
#   $4 = product_line filter (text|NULL)
#   $5 = limit (int)
_LIST_PROJECTS_LIVE_SQL = """
SELECT p.id, p.tenant_id, p.product_line, p.name, p.slug,
       p.parent_id, p.plan_override, p.disk_budget_bytes,
       p.llm_budget_tokens, p.created_by, p.created_at, p.archived_at
FROM projects p
WHERE p.tenant_id = $1
  AND p.archived_at IS NULL
  AND ($2::bool
       OR EXISTS (
         SELECT 1 FROM project_members pm
         WHERE pm.project_id = p.id AND pm.user_id = $3
       ))
  AND ($4::text IS NULL OR p.product_line = $4)
ORDER BY p.created_at DESC, p.id DESC
LIMIT $5
"""

_LIST_PROJECTS_ARCHIVED_SQL = """
SELECT p.id, p.tenant_id, p.product_line, p.name, p.slug,
       p.parent_id, p.plan_override, p.disk_budget_bytes,
       p.llm_budget_tokens, p.created_by, p.created_at, p.archived_at
FROM projects p
WHERE p.tenant_id = $1
  AND p.archived_at IS NOT NULL
  AND ($2::bool
       OR EXISTS (
         SELECT 1 FROM project_members pm
         WHERE pm.project_id = p.id AND pm.user_id = $3
       ))
  AND ($4::text IS NULL OR p.product_line = $4)
ORDER BY p.created_at DESC, p.id DESC
LIMIT $5
"""

_LIST_PROJECTS_ALL_SQL = """
SELECT p.id, p.tenant_id, p.product_line, p.name, p.slug,
       p.parent_id, p.plan_override, p.disk_budget_bytes,
       p.llm_budget_tokens, p.created_by, p.created_at, p.archived_at
FROM projects p
WHERE p.tenant_id = $1
  AND ($2::bool
       OR EXISTS (
         SELECT 1 FROM project_members pm
         WHERE pm.project_id = p.id AND pm.user_id = $3
       ))
  AND ($4::text IS NULL OR p.product_line = $4)
ORDER BY p.created_at DESC, p.id DESC
LIMIT $5
"""


async def _resolve_list_visibility(
    user: auth.User,
    tenant_id: str,
) -> tuple[bool, bool]:
    """Return ``(may_list, has_full_visibility)``.

    ``may_list`` is True iff the caller is allowed to see *any* row
    of the tenant's project list (super_admin, or any active
    membership of any role — explicit-only callers still get a
    response, possibly empty).

    ``has_full_visibility`` is True iff the caller can see every
    project regardless of explicit ``project_members`` rows —
    super_admin or membership role ∈ {owner, admin}.
    """
    if auth.role_at_least(user.role, "super_admin"):
        return True, True

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user.id, tenant_id,
        )
    if row is None or row["status"] != "active":
        return False, False
    full = row["role"] in _PROJECT_LIST_FULL_VISIBILITY_MEMBERSHIP_ROLES
    return True, full


@router.get("/tenants/{tenant_id}/projects")
async def list_projects(
    tenant_id: str,
    _request: Request,
    product_line: str | None = Query(
        default=None,
        description=(
            "Filter to one product line. Must be one of "
            "(embedded, web, mobile, software, custom) or omitted. "
            "Other values 422 with the allowed list."
        ),
    ),
    archived: str = Query(
        default="false",
        description=(
            "Filter by archived state. ``false`` (default) returns "
            "only live projects (archived_at IS NULL); ``true`` "
            "returns only archived ones; ``all`` returns both. "
            "Other values 422 with the allowed list."
        ),
    ),
    limit: int = Query(
        default=PROJECTS_LIST_DEFAULT_LIMIT,
        ge=1,
        le=PROJECTS_LIST_MAX_LIMIT,
        description=(
            f"Max rows to return (1..{PROJECTS_LIST_MAX_LIMIT}). "
            f"Default {PROJECTS_LIST_DEFAULT_LIMIT}."
        ),
    ),
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List projects for ``tenant_id`` filtered by the caller's
    visibility.

    Returns 200 with::

        {
            "tenant_id": "t-acme",
            "product_line_filter": "embedded" | None,
            "archived_filter": "false" | "true" | "all",
            "count": 3,
            "projects": [
                {
                    "project_id": "p-...",
                    "tenant_id": "t-acme",
                    "product_line": "embedded",
                    "name": "ISP Tuning",
                    "slug": "isp-tuning",
                    "parent_id": null,
                    "plan_override": null,
                    "disk_budget_bytes": null,
                    "llm_budget_tokens": null,
                    "created_by": "u-...",
                    "created_at": "YYYY-MM-DD HH:MM:SS",
                    "archived_at": null
                },
                ...
            ]
        }
    """
    # 1. Path-id validation. Same regex source-of-truth as POST.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )

    # 2. ``product_line`` enum check. Done in handler (not Pydantic
    #    Literal on Query) to surface a clear 422 detail listing the
    #    allowed values rather than the FastAPI default wording.
    if product_line is not None and product_line not in PRODUCT_LINE_ENUM:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid product_line filter: {product_line!r}; "
                    f"must be one of {PRODUCT_LINE_ENUM} or omitted"
                ),
            },
        )

    # 3. ``archived`` enum check. Same pattern.
    if archived not in LISTABLE_PROJECT_ARCHIVED_FILTERS:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid archived filter: {archived!r}; "
                    f"must be one of {LISTABLE_PROJECT_ARCHIVED_FILTERS}"
                ),
            },
        )

    # 4. Visibility resolution (RBAC + explicit/full discrimination).
    #    Done before the tenant existence probe so a guess-the-id
    #    scan can't enumerate which tenants exist via timing.
    may_list, has_full_visibility = await _resolve_list_visibility(
        actor, tenant_id,
    )
    if not may_list:
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires active membership on {tenant_id!r} or "
                f"platform super_admin; caller has no qualifying role"
            ),
        )

    # 5. Tenant existence probe — clean 404 if the tenant is missing.
    #    Super-admin reaches here even for non-existent tenants, so
    #    the explicit probe is necessary even for that branch. (For
    #    members, ``may_list=True`` guarantees the membership row
    #    exists and FK-points at the tenant — but the explicit probe
    #    is cheap and keeps the failure mode uniform.)
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 6. Pick the SQL by archived branch and run.
    if archived == "false":
        sql = _LIST_PROJECTS_LIVE_SQL
    elif archived == "true":
        sql = _LIST_PROJECTS_ARCHIVED_SQL
    else:  # archived == "all"
        sql = _LIST_PROJECTS_ALL_SQL

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            sql,
            tenant_id,
            has_full_visibility,
            actor.id,
            product_line,
            limit,
        )

    projects = [_row_to_project_dict(r) for r in rows]

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "product_line_filter": product_line,
            "archived_filter": archived,
            "count": len(projects),
            "projects": projects,
        },
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y4 (#280) row 3 — PATCH /api/v1/tenants/{tid}/projects/{pid}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Partial update of name / plan_override / disk_budget_bytes /
# parent_id. The TODO row literal calls out exactly these four fields;
# product_line / slug / created_by are deliberately NOT patchable
# because:
#   * product_line + slug make up the URL-stable identity (the
#     UNIQUE composite from migration 0033). Mutating them would
#     break URLs / bookmarks / cached project_id-by-slug lookups.
#     If a project genuinely needs a different slug, the operator
#     creates a new project + migrates artifacts.
#   * created_by is an audit-only field; rewriting it would forge
#     authorship history.
#   * archived_at is owned by the separate archive / restore endpoint
#     (Y4 row 4).
#
# Tri-state body semantics (the JSON-PATCH problem)
# ─────────────────────────────────────────────────
# Three distinct caller intents per nullable column:
#   1. field absent from body                → leave the column alone
#   2. field present with non-null value     → set the column to that
#   3. field present with explicit JSON null → clear the column (set
#      it to NULL ⇒ "inherit from tenant" for plan_override /
#      budget; "promote to top-level" for parent_id)
#
# Pydantic v2 distinguishes (1) from (2)/(3) via ``model_fields_set``
# — a frozenset of names the caller *explicitly* supplied. The handler
# uses that to build the SET clause; columns absent from
# ``fields_set`` are left untouched.
#
# ``name`` is non-nullable in the DB (CHECK length(name) >= 1). An
# explicit JSON null on ``name`` is rejected as 422 in the handler;
# Pydantic alone permits ``str | None`` so the explicit-null guard
# lives in the handler.
#
# Cycle detection (sub-project trees)
# ───────────────────────────────────
# Migration 0033 documents that "deeper cycle detection is application-
# layer (a tree walk on insert in Y3's POST /projects)". For POST the
# new project's id is freshly minted so it cannot already appear in
# any chain, but PATCH can re-parent an existing project anywhere in
# the tenant — including under one of its own descendants. The cycle
# check uses a recursive CTE that walks the ancestor chain starting
# from the *proposed* new parent; if the project being patched ever
# appears, the change would create a cycle and is refused with 422.
# A trivial self-loop (``parent_id == project_id``) is caught earlier
# without round-tripping the CTE.
#
# Concurrent re-parent race protection
# ────────────────────────────────────
# Worker A patches P1.parent_id = P2; Worker B patches P2.parent_id =
# P1. Each individual cycle probe says "no cycle" before either
# transaction commits, then both commit — leaving P1 ↔ P2. To
# serialise re-parent operations on the same tenant we take a
# per-tenant ``pg_advisory_xact_lock`` on
# ``hashtext('omnisight_project_patch:' || tenant_id)`` whenever the
# patch may affect ``parent_id`` (caller set the field). Per-tenant
# (not platform-wide) so traffic across tenants does not collide.
# PATCHes that do not touch ``parent_id`` skip the lock entirely.
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# New module-level constants: ``_PROJECT_PATCH_LOCK_PREFIX`` (str),
# ``_PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES`` (frozenset),
# ``_PATCHABLE_PROJECT_FIELDS`` (frozenset),
# ``_FETCH_PROJECT_FOR_UPDATE_SQL`` / ``_CYCLE_DETECT_SQL`` /
# ``_PATCH_PROJECT_SQL`` (str). All immutable; every uvicorn worker
# derives the same value from source — qualifying answer #1. DB
# state is shared via PG; per-tenant advisory lock is PG-coordinated
# across workers — qualifying answer #2.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# Single-transaction PATCH: SELECT ... FOR UPDATE → optional cycle
# probe → UPDATE ... RETURNING. The advisory lock (when parent_id
# changes) further serialises the cross-row part of the cycle check.
# Concurrent same-row PATCHes are also serialised by FOR UPDATE on
# the projects row itself. No new timing-visible behaviour relative
# to the existing PATCH endpoints (admin_tenants / tenant_members).

# Per-tenant advisory lock prefix — taken inside the PATCH transaction
# whenever ``parent_id`` is being set, to serialise re-parent races
# within one tenant. Per-tenant key (not platform-wide) so cross-
# tenant traffic does not collide.
_PROJECT_PATCH_LOCK_PREFIX = "omnisight_project_patch:"

# Same role gate as POST/GET — owner / admin tenant membership or
# platform super_admin. Member / viewer get 403.
_PROJECT_PATCH_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})

# Whitelist of body fields the PATCH actually applies. Drift-guarded:
# any new pydantic field on PatchProjectRequest must be added here AND
# wired into _PATCH_PROJECT_SQL, otherwise the handler silently drops
# the value (test ``test_patchable_fields_match_pydantic_schema``
# enforces the alignment).
_PATCHABLE_PROJECT_FIELDS = frozenset({
    "name", "plan_override", "disk_budget_bytes",
    "llm_budget_tokens", "parent_id",
})


class PatchProjectRequest(BaseModel):
    """Body for ``PATCH /api/v1/tenants/{tid}/projects/{pid}``.

    All four fields are optional; at least one must be supplied. The
    schema accepts ``None`` for each field at the type level — the
    handler distinguishes "absent" from "explicit null" via
    ``model_fields_set`` (Pydantic v2 only contains the names the
    caller actually supplied) and rejects ``name=null`` explicitly
    because the underlying column is NOT NULL.
    """

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "New display name. Omit to keep current. Explicit JSON "
            "null is rejected (422) because the underlying column is "
            "NOT NULL — to drop a name use a DELETE on the project."
        ),
    )
    plan_override: Literal[
        "free", "starter", "pro", "enterprise",
    ] | None = Field(
        default=None,
        description=(
            "New per-project plan override. Omit to keep current; "
            "explicit JSON null clears the override (project then "
            "inherits the tenant's plan). Must match the migration's "
            "CHECK enum."
        ),
    )
    disk_budget_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "New per-project disk quota in bytes. Omit to keep current; "
            "explicit JSON null clears the override (project then "
            "inherits the tenant's PLAN quota). Must be non-negative; "
            "the DB CHECK is defence in depth. Non-NULL values are "
            "subject to the Y4 row 7 oversell guard (Σ over the "
            "tenant's other live projects ≤ tenant plan ceiling)."
        ),
    )
    llm_budget_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "New per-project LLM token override. Omit to keep current; "
            "explicit JSON null clears the override (project then "
            "inherits the tenant's plan ceiling). Must be non-negative. "
            "Subject to the same Σ ≤ tenant total oversell guard."
        ),
    )
    parent_id: str | None = Field(
        default=None,
        pattern=PROJECT_ID_PATTERN,
        description=(
            "New parent project id for sub-project trees. Omit to keep "
            "current; explicit JSON null promotes the project back to "
            "top-level. Parent must belong to the same tenant; cycles "
            "(self-loop or new parent is a descendant) are refused."
        ),
    )


# Read the current row inside the transaction with FOR UPDATE so a
# concurrent PATCH on the same project blocks rather than racing on
# stale state. Same SELECT shape as the POST RETURNING / GET response
# so ``_row_to_project_dict`` works against either.
_FETCH_PROJECT_FOR_UPDATE_SQL = """
SELECT id, tenant_id, product_line, name, slug,
       parent_id, plan_override, disk_budget_bytes,
       llm_budget_tokens, created_by, created_at, archived_at
FROM projects
WHERE id = $1 AND tenant_id = $2
FOR UPDATE
"""

# Recursive ancestor walk starting from $1 (the proposed new parent).
# Returns at most one row — the row whose id matches $2 (the project
# being patched) iff the project being patched is itself an ancestor
# of the proposed new parent (i.e. assigning $2.parent_id = $1 would
# create a cycle). The ``WHERE c.parent_id IS NOT NULL`` short-
# circuits the walk at the root; PG's recursion engine is finite-
# guard via the FK shape but we still bound iterations cleanly.
_CYCLE_DETECT_SQL = """
WITH RECURSIVE ancestor_chain AS (
    SELECT id, parent_id FROM projects WHERE id = $1
    UNION ALL
    SELECT p.id, p.parent_id
    FROM projects p
    JOIN ancestor_chain c ON p.id = c.parent_id
    WHERE c.parent_id IS NOT NULL
)
SELECT 1 FROM ancestor_chain WHERE id = $2 LIMIT 1
"""

# Single static UPDATE template using ``CASE WHEN $flag THEN $value
# ELSE col END`` per column. The boolean flags ($3, $5, $7, $9) are
# True iff the caller explicitly set that field in the body —
# letting the handler distinguish "leave alone" from "set to NULL"
# without dynamic SQL. The casts (``::text`` / ``::bigint``) are
# necessary because asyncpg infers parameter types from first non-
# NULL use, and a column may legitimately receive NULL on the very
# first call (then asyncpg has nothing to infer from). RETURNING
# gives the post-update row in one round-trip so the response body
# and the audit ``after`` payload share a single source of truth.
_PATCH_PROJECT_SQL = """
UPDATE projects
SET name              = CASE WHEN $3  THEN $4::text    ELSE name              END,
    plan_override     = CASE WHEN $5  THEN $6::text    ELSE plan_override     END,
    disk_budget_bytes = CASE WHEN $7  THEN $8::integer ELSE disk_budget_bytes END,
    llm_budget_tokens = CASE WHEN $9  THEN $10::integer ELSE llm_budget_tokens END,
    parent_id         = CASE WHEN $11 THEN $12::text   ELSE parent_id         END
WHERE id = $1 AND tenant_id = $2
RETURNING id, tenant_id, product_line, name, slug,
          parent_id, plan_override, disk_budget_bytes,
          llm_budget_tokens, created_by, created_at, archived_at
"""


@router.patch("/tenants/{tenant_id}/projects/{project_id}")
async def patch_project(
    tenant_id: str,
    project_id: str,
    body: PatchProjectRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Partial-update a project.

    Body accepts any subset of ``{name, plan_override,
    disk_budget_bytes, parent_id}``; at least one field must be
    present. Explicit JSON ``null`` on ``plan_override`` /
    ``disk_budget_bytes`` / ``parent_id`` clears that column (project
    then inherits from tenant / becomes top-level). Explicit ``null``
    on ``name`` is rejected as 422.

    Returns 200 with the post-update project row plus a ``no_change``
    flag for callers that PATCH'd the row to its current values
    (skips the audit emit).

    Status codes
    ────────────
    * 200 — applied successfully (or no_change=True with no audit).
    * 403 — caller is not a tenant admin / owner on this tenant and
            not platform super_admin.
    * 404 — well-formed ids but no such tenant or no such project in
            this tenant.
    * 422 — id format fails the pattern, body has no settable field,
            ``name`` is explicitly null, ``parent_id`` would create a
            self-loop / refers to a non-existent project / refers to
            a project in a different tenant / would create a cycle.
    """
    # 1. Path-id validation. Same regex source-of-truth as POST/GET.
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    # 2. Body must include at least one settable field. Same posture
    #    as Y2 PatchTenantRequest — empty PATCH wastes a round-trip
    #    and an audit row, plus the caller probably meant something.
    set_fields = body.model_fields_set & _PATCHABLE_PROJECT_FIELDS
    if not set_fields:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "PATCH body must include at least one of "
                    "'name', 'plan_override', 'disk_budget_bytes', "
                    "'llm_budget_tokens', or 'parent_id'."
                ),
            },
        )

    # 3. Reject explicit ``name: null`` — column is NOT NULL; pydantic
    #    accepts the union but the DB CHECK would otherwise fire after
    #    a bunch of work. Cleaner to fail fast at the boundary.
    if "name" in set_fields and body.name is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    "name cannot be set to null; the underlying column "
                    "is NOT NULL"
                ),
            },
        )

    # 4. Trivial self-loop guard — ``parent_id == project_id``. Cheaper
    #    than the recursive CTE; same outcome (422). Done before any
    #    DB I/O.
    if "parent_id" in set_fields and body.parent_id == project_id:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"parent_id cannot equal project_id "
                    f"({project_id!r}); a project cannot be its own "
                    f"parent"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
            },
        )

    # 5. RBAC — done before any tenant/project existence probe so a
    #    guess-the-id scan can't enumerate via timing.
    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    # 6. Tenant existence probe — clean 404. Done outside the patch
    #    transaction so a 404 caller does not hold a write lock.
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # 7. Cross-tenant + non-existent parent guard for the *new* parent
    #    if the body sets one. Done before opening the patch
    #    transaction so 422 callers don't hold the per-tenant lock.
    #    The cycle check happens INSIDE the transaction (after the lock)
    #    because cycles are a function of state that other writers can
    #    mutate concurrently.
    if "parent_id" in set_fields and body.parent_id is not None:
        async with get_pool().acquire() as conn:
            parent_row = await conn.fetchrow(
                _FETCH_PARENT_PROJECT_SQL, body.parent_id,
            )
        if parent_row is None:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"parent_id {body.parent_id!r} does not exist"
                    ),
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "parent_id": body.parent_id,
                },
            )
        if parent_row["tenant_id"] != tenant_id:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"parent_id {body.parent_id!r} belongs to a "
                        f"different tenant; cross-tenant parent links "
                        f"are not permitted"
                    ),
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "parent_id": body.parent_id,
                    "parent_tenant_id": parent_row["tenant_id"],
                },
            )

    # 8. The patch transaction. Two distinct per-tenant advisory locks
    #    may apply:
    #      * ``_PROJECT_PATCH_LOCK_PREFIX`` — taken when ``parent_id`` is
    #        being set, to serialise re-parent races within one tenant.
    #      * ``_PROJECT_QUOTA_LOCK_PREFIX`` (Y4 row 7) — taken when a
    #        non-NULL budget override is being set, to serialise the
    #        oversell SUM-then-UPDATE race within one tenant.
    #    Both are per-tenant (cross-tenant traffic does not collide).
    #    A PATCH that touches neither parent_id nor a non-NULL budget
    #    skips both locks; the row-level FOR UPDATE on the SELECT below
    #    serialises same-row PATCHes.
    parent_id_is_changing = "parent_id" in set_fields
    disk_budget_is_changing = "disk_budget_bytes" in set_fields
    llm_budget_is_changing = "llm_budget_tokens" in set_fields
    needs_quota_lock = (
        (disk_budget_is_changing and body.disk_budget_bytes is not None)
        or (llm_budget_is_changing and body.llm_budget_tokens is not None)
    )

    from backend import project_quota as _pq

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            if parent_id_is_changing:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    _PROJECT_PATCH_LOCK_PREFIX + tenant_id,
                )
            if needs_quota_lock:
                # Distinct lock key from the parent-patch lock so the
                # two surfaces don't deadlock when a single PATCH
                # exercises both. PG advisory locks are reentrant per-
                # session; ordering parent-then-quota matches the
                # alphabetical ordering of the prefixes ("patch" <
                # "quota") so concurrent PATCHes always grab them in
                # the same order — no ABBA deadlock.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    _PROJECT_QUOTA_LOCK_PREFIX + tenant_id,
                )

            cur_row = await conn.fetchrow(
                _FETCH_PROJECT_FOR_UPDATE_SQL, project_id, tenant_id,
            )
            if cur_row is None:
                # 404 — well-formed ids but no such project IN THIS
                # tenant. Note: a project that exists under a different
                # tenant returns 404 here (not 403 / not the parent
                # tenant id) because the caller has no business knowing
                # whether the id exists elsewhere.
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": (
                            f"project not found: {project_id!r} on "
                            f"tenant {tenant_id!r}"
                        ),
                    },
                )

            # 9. Cycle check — only when parent_id is being set to a
            #    non-null value AND it's actually changing AND that
            #    non-null value is not the current value already.
            if (
                parent_id_is_changing
                and body.parent_id is not None
                and body.parent_id != cur_row["parent_id"]
            ):
                cycle_hit = await conn.fetchrow(
                    _CYCLE_DETECT_SQL, body.parent_id, project_id,
                )
                if cycle_hit is not None:
                    return JSONResponse(
                        status_code=422,
                        content={
                            "detail": (
                                f"parent_id {body.parent_id!r} would "
                                f"create a cycle: project "
                                f"{project_id!r} is already an "
                                f"ancestor of the proposed parent"
                            ),
                            "tenant_id": tenant_id,
                            "project_id": project_id,
                            "parent_id": body.parent_id,
                        },
                    )

            # 9b. Y4 row 7 oversell guard — only when a non-NULL budget
            #     override is being set. Excludes the project being
            #     patched from the SUM so its old value doesn't double-
            #     count against the new value (effectively a "swap"
            #     instead of an "add"). NULL clears short-circuit
            #     inside the helper.
            if needs_quota_lock:
                tenant_plan = tenant_row["plan"]
                try:
                    if disk_budget_is_changing:
                        await _pq.check_disk_budget_oversell(
                            conn,
                            tenant_id=tenant_id,
                            tenant_plan=tenant_plan,
                            exclude_project_id=project_id,
                            new_value=body.disk_budget_bytes,
                        )
                    if llm_budget_is_changing:
                        await _pq.check_llm_budget_oversell(
                            conn,
                            tenant_id=tenant_id,
                            tenant_plan=tenant_plan,
                            exclude_project_id=project_id,
                            new_value=body.llm_budget_tokens,
                        )
                except _pq.ProjectBudgetOversell as exc:
                    return JSONResponse(
                        status_code=409,
                        content=exc.to_response_body(),
                    )

            # 10. Compute change-detection. If every field the caller
            #     supplied already matches the current row value,
            #     short-circuit to ``no_change=True`` without writing.
            no_change = all(
                getattr(body, f) == cur_row[f] for f in set_fields
            )
            if no_change:
                # Same-state PATCH — 200 with no_change=True, no UPDATE,
                # no audit row.  Returns the current row state (which
                # is ALSO the would-be post-state).
                project = _row_to_project_dict(cur_row)
                project["no_change"] = True
                return JSONResponse(status_code=200, content=project)

            # 11. Apply the UPDATE. Boolean flags + value pairs let
            #     ONE static SQL handle every subset of fields.
            new_row = await conn.fetchrow(
                _PATCH_PROJECT_SQL,
                project_id, tenant_id,
                "name" in set_fields, body.name,
                "plan_override" in set_fields, body.plan_override,
                "disk_budget_bytes" in set_fields, body.disk_budget_bytes,
                "llm_budget_tokens" in set_fields, body.llm_budget_tokens,
                "parent_id" in set_fields, body.parent_id,
            )
            # ``new_row`` cannot be None here: we held FOR UPDATE on
            # the row inside the same transaction so no concurrent
            # DELETE could intervene. assert documents the invariant.
            assert new_row is not None, (
                "FOR UPDATE invariant breached — projects row vanished "
                "between SELECT FOR UPDATE and UPDATE inside the same "
                "transaction"
            )

    # 12. Audit emission — best-effort. ``before`` / ``after`` capture
    #     ONLY the changed fields (with project_id + tenant_id always
    #     present for context). Restricting to changed fields keeps
    #     the audit blob tight and makes the diff obvious without
    #     loading the full row.
    before_blob: dict = {
        "tenant_id": tenant_id,
        "project_id": project_id,
    }
    after_blob: dict = {
        "tenant_id": tenant_id,
        "project_id": project_id,
    }
    for field in set_fields:
        before_blob[field] = cur_row[field]
        after_blob[field] = new_row[field]

    try:
        await _audit.log(
            action="tenant_project_updated",
            entity_kind="project",
            entity_id=project_id,
            before=before_blob,
            after=after_blob,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_updated audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
        )

    project = _row_to_project_dict(new_row)
    project["no_change"] = False
    return JSONResponse(status_code=200, content=project)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y4 (#280) row 4 — POST /api/v1/tenants/{tid}/projects/{pid}/archive
#                  + POST /api/v1/tenants/{tid}/projects/{pid}/restore
#                  + ``gc_archived_projects`` background helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Soft-archive lifecycle for a project. Three moving parts:
#
#   1. ``POST .../archive``  → flips ``archived_at`` from NULL to a
#      ``YYYY-MM-DD HH:MM:SS`` UTC stamp. Idempotent — a second archive
#      of an already-archived project returns 200 with ``no_change``
#      and emits no audit row. The TODO row literal says "workspace /
#      agent / cron 全部停工" — the policy decision is encoded as a
#      single boolean column on ``projects``; downstream surfaces
#      (workspace open, agent dispatch, cron tick) consult
#      ``archived_at IS NULL`` independently. This handler owns the
#      archive bit ONLY — it does not chase down the dozens of
#      enforcement sites, those are tracked under their own TODO rows.
#
#   2. ``POST .../restore`` → flips ``archived_at`` back to NULL,
#      provided the project still exists (ie. has not yet been
#      hard-removed by the GC). Idempotent — restoring a live project
#      returns 200 with ``no_change`` and emits no audit row. Once the
#      GC has fired the row is gone and a restore returns 404 (no row
#      to flip); the TODO row literal calls this out as the design
#      ("90 days 後背景 GC 正式刪除").
#
#   3. ``gc_archived_projects(*, retention_days=None, conn=None)`` →
#      background-callable helper (no FastAPI context required). Walks
#      ``projects WHERE archived_at IS NOT NULL AND archived_at <
#      <utc-now - retention_days>`` and DELETEs each row inside the
#      same transaction, emitting a ``tenant_project_billing_gc``
#      audit event per deletion. The audit event is the billing event
#      surface — accounting reconciles freed quota by querying
#      ``audit_log WHERE action = 'tenant_project_billing_gc'``.
#      Returns the list of removed project rows (id + tenant_id +
#      product_line + slug + disk_budget_bytes + archived_at) so a
#      caller / cron can log a one-line summary.
#
# Retention config
# ────────────────
# Knob: ``OMNISIGHT_PROJECT_GC_RETENTION_DAYS`` (env var, integer, > 0).
# Default ``90`` per the TODO row literal. Resolved per-call (not a
# module-level constant) so an operator can edit ``.env`` and bounce
# uvicorn without code change AND so test fixtures can override per
# test.  An invalid value (non-integer, ≤ 0) falls back to the default
# with a warning — best-effort knob, not a gate.
#
# Why audit-row-as-billing-event (not a separate billing.emit module)
# ───────────────────────────────────────────────────────────────────
# This codebase does not (yet) have a dedicated billing-event emit
# surface; the existing ``audit.log`` pipeline already provides
# tamper-evident, tenant-scoped, time-ordered, queryable records.
# Folding the billing event into the same hash chain means accounting
# inherits the per-tenant integrity guarantee for free, and downstream
# (when a real billing pipeline lands) the migration is a single
# ``audit.query(action='tenant_project_billing_gc')`` projection.
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# New module-level constants: ``_PROJECT_GC_RETENTION_DAYS_ENV``
# (str env-var name), ``_PROJECT_GC_RETENTION_DAYS_DEFAULT`` (int),
# ``_ARCHIVE_PROJECT_SQL`` / ``_RESTORE_PROJECT_SQL`` /
# ``_LIST_GC_ELIGIBLE_PROJECTS_SQL`` / ``_DELETE_PROJECT_SQL`` (str).
# All immutable; every uvicorn worker derives the same value from
# source — qualifying answer #1. ``_resolve_archive_retention_days``
# reads the env var per-call so a hot-edit propagates as soon as the
# next handler / GC tick runs (no per-worker cache to invalidate).
# DB state is shared via PG; archive / restore are single-row UPDATEs
# guarded by ``WHERE id=$1 AND tenant_id=$2 AND archived_at <pred>``
# so a concurrent archive + restore race resolves to one winner via
# RETURNING None on the loser — qualifying answer #2.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# Archive / restore are single-statement UPDATEs with a precondition
# in WHERE. RETURNING None means "no row matched the precondition"
# which the handler interprets as either (a) idempotent no-change
# (already in target state) or (b) row vanished (404). Concurrent
# archive + restore on the same project: PG row-level locks serialise
# them; the loser's WHERE precondition (``archived_at IS NULL`` for
# archive / ``IS NOT NULL`` for restore) no longer holds and the
# loser sees no_change=True. No new timing-visible behaviour relative
# to the existing PATCH endpoint.

_PROJECT_GC_RETENTION_DAYS_ENV = "OMNISIGHT_PROJECT_GC_RETENTION_DAYS"
_PROJECT_GC_RETENTION_DAYS_DEFAULT = 90


def _resolve_archive_retention_days() -> int:
    """Resolve ``OMNISIGHT_PROJECT_GC_RETENTION_DAYS`` to a positive
    int, falling back to the 90-day default on any parse failure."""
    import os
    raw = os.environ.get(_PROJECT_GC_RETENTION_DAYS_ENV, "").strip()
    if not raw:
        return _PROJECT_GC_RETENTION_DAYS_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %d-day default",
            _PROJECT_GC_RETENTION_DAYS_ENV, raw,
            _PROJECT_GC_RETENTION_DAYS_DEFAULT,
        )
        return _PROJECT_GC_RETENTION_DAYS_DEFAULT
    if n <= 0:
        logger.warning(
            "%s=%d must be positive; falling back to %d-day default",
            _PROJECT_GC_RETENTION_DAYS_ENV, n,
            _PROJECT_GC_RETENTION_DAYS_DEFAULT,
        )
        return _PROJECT_GC_RETENTION_DAYS_DEFAULT
    return n


# Archive: flip archived_at NULL → now() iff currently NULL.  The
# ``AND archived_at IS NULL`` precondition in the WHERE means RETURNING
# None signals "already archived" → idempotent no-change branch in
# the handler.  ``to_char(now() at time zone 'utc', ...)`` matches the
# format used by ``created_at`` and the ``_archive_project`` helper in
# the existing test_tenant_projects_list.py — a single sortable text
# format keeps the GC's text-comparison cutoff correct.
_ARCHIVE_PROJECT_SQL = """
UPDATE projects
SET archived_at = to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')
WHERE id = $1 AND tenant_id = $2 AND archived_at IS NULL
RETURNING id, tenant_id, product_line, name, slug,
          parent_id, plan_override, disk_budget_bytes,
          llm_budget_tokens, created_by, created_at, archived_at
"""

# Restore: flip archived_at non-NULL → NULL iff currently archived.
_RESTORE_PROJECT_SQL = """
UPDATE projects
SET archived_at = NULL
WHERE id = $1 AND tenant_id = $2 AND archived_at IS NOT NULL
RETURNING id, tenant_id, product_line, name, slug,
          parent_id, plan_override, disk_budget_bytes,
          llm_budget_tokens, created_by, created_at, archived_at
"""

# GC: enumerate projects whose archive has aged past the retention
# window. ``$1`` is the cutoff string in ``YYYY-MM-DD HH24:MI:SS`` UTC
# form; archived_at is text in the same shape so a plain text
# comparison sorts correctly.
_LIST_GC_ELIGIBLE_PROJECTS_SQL = """
SELECT id, tenant_id, product_line, name, slug,
       parent_id, plan_override, disk_budget_bytes,
       llm_budget_tokens, created_by, created_at, archived_at
FROM projects
WHERE archived_at IS NOT NULL
  AND archived_at < $1
ORDER BY archived_at ASC, id ASC
"""

# Hard delete a single project. The ON DELETE CASCADE on
# project_members.project_id (alembic 0034) drops member grants;
# the ON DELETE SET NULL on projects.parent_id (alembic 0033) promotes
# any sub-projects to top-level rather than cascading them out.
_DELETE_PROJECT_SQL = "DELETE FROM projects WHERE id = $1 AND tenant_id = $2"


# Same role gate as POST/GET/PATCH — owner / admin tenant membership
# or platform super_admin.
_PROJECT_ARCHIVE_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


@router.post("/tenants/{tenant_id}/projects/{project_id}/archive")
async def archive_project(
    tenant_id: str,
    project_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Soft-archive a project.

    Sets ``archived_at`` to the current UTC timestamp. The row stays in
    the table for ``OMNISIGHT_PROJECT_GC_RETENTION_DAYS`` days (default
    90) before the background GC permanently deletes it; restore is
    available throughout that window.

    Idempotent: a second archive returns 200 with ``no_change=True``
    and emits no audit row.

    Status codes
    ────────────
    * 200 — archived (or no_change=True if already archived).
    * 403 — caller is not tenant admin / owner and not super_admin.
    * 404 — well-formed ids but no such tenant or no such project in
            this tenant.
    * 422 — malformed tenant_id / project_id pattern.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # FOR UPDATE to serialise concurrent archive/restore on the
            # same row; the precondition lives in the UPDATE WHERE so
            # this SELECT only exists to distinguish 404 (no row) from
            # 200 no_change (row exists but already archived).
            cur_row = await conn.fetchrow(
                _FETCH_PROJECT_FOR_UPDATE_SQL, project_id, tenant_id,
            )
            if cur_row is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": (
                            f"project not found: {project_id!r} on "
                            f"tenant {tenant_id!r}"
                        ),
                    },
                )

            new_row = await conn.fetchrow(
                _ARCHIVE_PROJECT_SQL, project_id, tenant_id,
            )

    if new_row is None:
        # Already archived — idempotent no-change. Return current row
        # state with no_change=True; no audit emit.
        project = _row_to_project_dict(cur_row)
        project["no_change"] = True
        return JSONResponse(status_code=200, content=project)

    try:
        await _audit.log(
            action="tenant_project_archived",
            entity_kind="project",
            entity_id=project_id,
            before={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "archived_at": None,
            },
            after={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "archived_at": new_row["archived_at"],
                "retention_days": _resolve_archive_retention_days(),
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_archived audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
        )

    # Y9 #285 row 1 — canonical dot-notation event ``project.archived``.
    try:
        from backend import audit_events as _audit_events
        await _audit_events.emit_project_archived(
            tenant_id=tenant_id,
            project_id=project_id,
            archived_at=new_row["archived_at"],
            retention_days=_resolve_archive_retention_days(),
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "project.archived audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
        )

    project = _row_to_project_dict(new_row)
    project["no_change"] = False
    return JSONResponse(status_code=200, content=project)


@router.post("/tenants/{tenant_id}/projects/{project_id}/restore")
async def restore_project(
    tenant_id: str,
    project_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Restore a soft-archived project to live.

    Clears ``archived_at`` back to NULL, available throughout the
    retention window. Once the GC has hard-deleted the row, restore
    returns 404 (no row to flip).

    Idempotent: restoring a row that is already live returns 200 with
    ``no_change=True`` and emits no audit row.

    Status codes
    ────────────
    * 200 — restored (or no_change=True if already live).
    * 403 — caller is not tenant admin / owner and not super_admin.
    * 404 — well-formed ids but no such tenant or no such project in
            this tenant (eg. already GC'd).
    * 422 — malformed tenant_id / project_id pattern.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            cur_row = await conn.fetchrow(
                _FETCH_PROJECT_FOR_UPDATE_SQL, project_id, tenant_id,
            )
            if cur_row is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": (
                            f"project not found: {project_id!r} on "
                            f"tenant {tenant_id!r}"
                        ),
                    },
                )

            new_row = await conn.fetchrow(
                _RESTORE_PROJECT_SQL, project_id, tenant_id,
            )

    if new_row is None:
        # Already live — idempotent no-change.
        project = _row_to_project_dict(cur_row)
        project["no_change"] = True
        return JSONResponse(status_code=200, content=project)

    try:
        await _audit.log(
            action="tenant_project_restored",
            entity_kind="project",
            entity_id=project_id,
            before={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "archived_at": cur_row["archived_at"],
            },
            after={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "archived_at": None,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_restored audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
        )

    project = _row_to_project_dict(new_row)
    project["no_change"] = False
    return JSONResponse(status_code=200, content=project)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Background GC helper — called from cron / manual ops, NOT a route
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def gc_archived_projects(
    *,
    retention_days: int | None = None,
    now_utc=None,
) -> list[dict]:
    """Hard-delete projects whose archive has aged past the retention
    window. Emits a ``tenant_project_billing_gc`` audit row per
    deletion (the audit chain doubles as the billing event surface
    until a dedicated billing pipeline lands; see module docstring
    for the rationale).

    Parameters
    ──────────
    retention_days
        Override the resolved retention window. Defaults to
        ``_resolve_archive_retention_days()`` (env var or 90).
    now_utc
        Inject a ``datetime`` for tests. Defaults to
        ``datetime.utcnow()``.

    Returns
    ───────
    A list of dicts (``project_id`` / ``tenant_id`` / ``product_line``
    / ``slug`` / ``disk_budget_bytes`` / ``archived_at``) for each
    project removed. The shape doubles as the operator-facing summary
    when a cron / ops script logs the GC tick.

    Concurrency
    ───────────
    The enumeration + per-row delete is intentionally not held in a
    single transaction so a long-running GC tick (thousands of rows)
    does not pin the table. Each delete is its own UPDATE-then-audit
    pair; the WHERE matches at most one row per tick so concurrent
    GC ticks across workers cannot double-delete (the loser's DELETE
    affects 0 rows and is silently dropped).
    """
    from datetime import datetime as _dt, timedelta as _td

    if retention_days is None:
        retention_days = _resolve_archive_retention_days()
    if not isinstance(retention_days, int) or retention_days <= 0:
        raise ValueError(
            f"retention_days must be a positive int; got {retention_days!r}"
        )

    if now_utc is None:
        now_utc = _dt.utcnow()
    cutoff = (now_utc - _td(days=retention_days)).strftime(
        "%Y-%m-%d %H:%M:%S",
    )


    async with get_pool().acquire() as conn:
        eligible_rows = await conn.fetch(
            _LIST_GC_ELIGIBLE_PROJECTS_SQL, cutoff,
        )

    removed: list[dict] = []
    for row in eligible_rows:
        pid = row["id"]
        tid = row["tenant_id"]
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                # DELETE returns the affected-row count via execute()
                # status string ("DELETE N"); a concurrent GC worker
                # may have removed the row between our list and our
                # delete, in which case status is "DELETE 0" and we
                # skip emitting a duplicate billing event.
                status = await conn.execute(
                    _DELETE_PROJECT_SQL, pid, tid,
                )
        # asyncpg returns the PG command tag (eg ``"DELETE 1"``).
        try:
            affected = int(status.split(" ")[-1])
        except (AttributeError, ValueError):
            affected = 0
        if affected == 0:
            continue

        billed = {
            "project_id": pid,
            "tenant_id": tid,
            "product_line": row["product_line"],
            "slug": row["slug"],
            "disk_budget_bytes": row["disk_budget_bytes"],
            "llm_budget_tokens": row["llm_budget_tokens"],
            "archived_at": row["archived_at"],
            "retention_days": retention_days,
            "gc_at": _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            from backend.db_context import set_tenant_id, current_tenant_id
            prev_tid = current_tenant_id()
            try:
                # The GC runs without a request context so the
                # tenant_id contextvar is unset; pin it to the deleted
                # project's tenant so the audit row lands in the right
                # per-tenant chain.
                set_tenant_id(tid)
                await _audit.log(
                    action="tenant_project_billing_gc",
                    entity_kind="project",
                    entity_id=pid,
                    before={
                        "tenant_id": tid,
                        "project_id": pid,
                        "archived_at": row["archived_at"],
                    },
                    after=billed,
                    actor="system:gc",
                )
            finally:
                set_tenant_id(prev_tid)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "tenant_project_billing_gc audit emit failed "
                "(tenant=%s project=%s): %s", tid, pid, exc,
            )

        removed.append(billed)

    if removed:
        logger.info(
            "gc_archived_projects: removed %d project(s) past %d-day "
            "retention (cutoff=%s)",
            len(removed), retention_days, cutoff,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y4 (#280) row 5 — project-level membership management
#                   POST   /api/v1/tenants/{tid}/projects/{pid}/members
#                   PATCH  /api/v1/tenants/{tid}/projects/{pid}/members/{uid}
#                   DELETE /api/v1/tenants/{tid}/projects/{pid}/members/{uid}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Explicit per-(user, project) role binding overlaying the tenant
# default-resolution semantics from alembic 0034. The TODO row literal
# enumerates exactly three roles: ``owner / contributor / viewer`` —
# deliberately distinct from the tenant-level enum (``owner / admin /
# member / viewer``) so a project owner is not confused with a tenant
# owner. The DB CHECK on ``project_members.role`` enforces the same
# whitelist server-side.
#
# RBAC for these endpoints
# ────────────────────────
# A caller may add / patch / remove project members iff they are one
# of:
#   1. Platform ``super_admin`` — universal trust boundary.
#   2. Tenant membership role ∈ {owner, admin} on the target tenant
#      — admin/owner of the tenant inherits "manage everything in the
#      tenant" by the default-resolution semantics (alembic 0034
#      docstring: "admin/owner tenant membership is treated as
#      contributor on every project of that tenant by default" — and
#      tenant admin is the operator who creates projects + assigns
#      ownership in the first place).
#   3. Project-level ``owner`` row — the project owner is empowered
#      to manage their own project's roster without escalating to
#      tenant admin every time. This is the asymmetric privilege that
#      makes the ``owner`` project role meaningful (otherwise the
#      enum's only differentiating value over ``contributor`` would be
#      semantic).
#
# Note that ``contributor`` and ``viewer`` project rows do NOT confer
# member-management. Suspended / non-existent tenant membership also
# blocks management (suspension at the tenant layer revokes every
# project capability simultaneously).
#
# Soft-delete semantics
# ─────────────────────
# Per alembic 0034 docstring: "Suspension at the project layer is
# achieved by deleting the row, which falls back to the tenant
# default." There is no ``status`` column on ``project_members``;
# DELETE is the soft-delete equivalent. A second DELETE on a row that
# was never explicitly added (or was already removed) returns 200 with
# ``already_removed=True`` and emits no audit row — same idempotent
# posture as Y4 row 4 archive/restore.
#
# Tenant-membership precondition for the target user
# ──────────────────────────────────────────────────
# Adding a project_members row for a user without an active tenant
# membership on the same tenant is rejected with 422 ("user is not an
# active tenant member; invite them first"). Without this guard the
# project grant would dangle: the auth surface checks tenant
# membership before evaluating project capability, so a project_member
# row for a non-tenant-member user is meaningless. Suspended tenant
# memberships are also rejected — re-activate the tenant membership
# (PATCH /tenants/{tid}/members/{uid}) before granting project
# capability.
#
# Distinct from POST /tenants/{tid}/invites: that surface invites a
# new user (no users row yet) into the tenant; this surface promotes
# an existing tenant member to an explicit project role. Both flows
# are intentionally separate — the operator first uses the invite
# flow to onboard the user, then uses this row's POST to grant project
# scope.
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# New module-level constants:
#   * ``PROJECT_MEMBER_ROLE_ENUM`` — DB-CHECK-aligned tuple
#   * ``USER_ID_PATTERN`` / ``_USER_ID_RE`` — same shape as Y3 row 5
#     ``admin_super_admins.USER_ID_PATTERN`` (drift-guard test enforces)
#   * ``_PROJECT_MEMBER_MGMT_TENANT_ROLES`` — frozenset of tenant roles
#     that may manage project members ({"owner", "admin"})
#   * ``_PROJECT_MEMBER_MGMT_PROJECT_ROLES`` — frozenset of project
#     roles that may manage project members ({"owner"})
#   * 6 SQL constants — ``_FETCH_PROJECT_TENANT_SCOPED_SQL``,
#     ``_FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL``,
#     ``_FETCH_PROJECT_MEMBER_SQL``, ``_INSERT_PROJECT_MEMBER_SQL``,
#     ``_UPDATE_PROJECT_MEMBER_ROLE_SQL``,
#     ``_DELETE_PROJECT_MEMBER_SQL``
# All immutable; every uvicorn worker derives the same value from
# source — qualifying answer #1. DB state is shared via PG; the
# composite PK ``(user_id, project_id)`` serialises concurrent INSERTs
# via ``ON CONFLICT DO NOTHING`` and the row lock implicit in PG's
# UPDATE / DELETE serialises row-level mutations — qualifying answer
# #2. No new in-memory cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# POST is a single ``INSERT … ON CONFLICT (user_id, project_id) DO
# NOTHING RETURNING`` — concurrent admins racing the same target
# resolve to one winner (RETURNING populated) and one loser (RETURNING
# None → 409 with the existing role surfaced). PATCH is a single
# UPDATE … RETURNING — the row lock serialises concurrent PATCHes on
# the same row; concurrent PATCH + DELETE on the same row resolve to
# one winner and one ``RETURNING None`` (the DELETE may be the
# winner; the PATCH loser observes None and falls through to 404 /
# already-removed depending on shape). DELETE is a single ``DELETE …
# RETURNING`` — RETURNING None signals "row already gone" → idempotent
# already_removed branch.

# Membership roles allowed on a ``project_members`` row. Mirrors the
# DB CHECK on ``project_members.role`` from alembic 0034 — drift here
# would let a regressed POST/PATCH set a role the DB will then reject.
# Deliberately distinct from MEMBERSHIP_ROLE_ENUM (tenant-level) — a
# project owner is NOT a tenant owner.
PROJECT_MEMBER_ROLE_ENUM = ("owner", "contributor", "viewer")

# User id — same shape as Y3 row 5 admin_super_admins / Y3 row 6
# tenant_members. Drift-guard test imports from admin_super_admins to
# enforce the alignment.
USER_ID_PATTERN = r"^u-[a-z0-9]{4,64}$"
_USER_ID_RE = re.compile(USER_ID_PATTERN)


def _is_valid_user_id(uid: str) -> bool:
    return bool(uid) and bool(_USER_ID_RE.match(uid))


# Tenant-level roles whose holders may manage project members on a
# given tenant. Same set as the project-CRUD allowlist — admin/owner
# of the tenant are the operators who provision project ownership in
# the first place.
_PROJECT_MEMBER_MGMT_TENANT_ROLES = frozenset({"owner", "admin"})

# Project-level roles whose holders may manage other members on the
# same project. Only ``owner`` qualifies — ``contributor`` and
# ``viewer`` get capability on the project but not authority over
# the roster.
_PROJECT_MEMBER_MGMT_PROJECT_ROLES = frozenset({"owner"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic bodies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CreateProjectMemberRequest(BaseModel):
    """Body for ``POST /api/v1/tenants/{tid}/projects/{pid}/members``."""

    user_id: str = Field(
        pattern=USER_ID_PATTERN,
        description=(
            "Target user id. Must already exist in ``users`` and have "
            "an active ``user_tenant_memberships`` row on the same "
            "tenant — this surface promotes an existing tenant member "
            "to a project role; new-user onboarding goes through "
            "POST /tenants/{tid}/invites."
        ),
    )
    role: Literal["owner", "contributor", "viewer"] = Field(
        description=(
            "Project-scope role to grant. Must be one of "
            "(owner, contributor, viewer). Pydantic returns 422 on "
            "anything else before the handler runs."
        ),
    )


class PatchProjectMemberRequest(BaseModel):
    """Body for ``PATCH /api/v1/tenants/{tid}/projects/{pid}/members/{uid}``.

    Only ``role`` is patchable — ``user_id`` and ``project_id`` are
    the composite PK and not user-mutable; ``created_at`` is audit-
    only. The schema requires the field; a missing role is a 422.
    """

    role: Literal["owner", "contributor", "viewer"] = Field(
        description=(
            "New project-scope role. Must be one of "
            "(owner, contributor, viewer)."
        ),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Authorisation — tenant admin / owner OR project owner
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _user_can_manage_project_members(
    user: auth.User,
    tenant_id: str,
    project_id: str,
) -> bool:
    """True iff ``user`` may add / patch / delete project_members
    rows on ``(tenant_id, project_id)``.

    Order of checks (cheap → expensive):
      1. Platform ``super_admin`` — always allowed.
      2. Active tenant membership role ∈ {owner, admin}.
      3. Explicit ``project_members`` row with role='owner' on the
         target project.

    Suspended tenant memberships do NOT confer management — suspending
    the tenant membership revokes every project capability at once.
    A pending suspension during a write tx is fine: the per-(user,
    project) row lookup re-runs each call.
    """
    if auth.role_at_least(user.role, "super_admin"):
        return True

    async with get_pool().acquire() as conn:
        tm_row = await conn.fetchrow(
            "SELECT role, status FROM user_tenant_memberships "
            "WHERE user_id = $1 AND tenant_id = $2",
            user.id, tenant_id,
        )
    if (
        tm_row is not None
        and tm_row["status"] == "active"
        and tm_row["role"] in _PROJECT_MEMBER_MGMT_TENANT_ROLES
    ):
        return True

    # Project-owner branch — only consulted if tenant-level admin path
    # did not qualify. A user who is "project owner on P1" but only
    # "viewer on tenant T" can manage P1's members but no other
    # project; the per-project query enforces that.
    async with get_pool().acquire() as conn:
        pm_row = await conn.fetchrow(
            "SELECT role FROM project_members "
            "WHERE user_id = $1 AND project_id = $2",
            user.id, project_id,
        )
    if (
        pm_row is not None
        and pm_row["role"] in _PROJECT_MEMBER_MGMT_PROJECT_ROLES
    ):
        return True

    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL — project membership write paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant-scoped project existence probe — distinguishes "no such
# project on this tenant" (row is None) from "project belongs to
# another tenant" (also row is None — by intent: the caller has no
# business knowing the project lives elsewhere). Used by all three
# member-management handlers as the 404 gate.
_FETCH_PROJECT_TENANT_SCOPED_SQL = (
    "SELECT id FROM projects WHERE id = $1 AND tenant_id = $2"
)

# Verify the TARGET user has an active membership on the tenant
# before granting project capability. The roles are NOT filtered —
# any active tenant member can be promoted to any project role
# (member viewer → project owner is a valid state if a tenant admin
# wills it; the tenant-side suspension is the kill switch).
_FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL = (
    "SELECT role, status FROM user_tenant_memberships "
    "WHERE user_id = $1 AND tenant_id = $2"
)

# Read-only fetch of a single project_members row. Used to surface
# the existing role on a 409 conflict body and to short-circuit
# no_change branches on PATCH.
_FETCH_PROJECT_MEMBER_SQL = (
    "SELECT user_id, project_id, role, created_at "
    "FROM project_members WHERE user_id = $1 AND project_id = $2"
)

# Atomic INSERT with ``ON CONFLICT (user_id, project_id) DO NOTHING
# RETURNING`` — the composite PK from alembic 0034 makes RETURNING
# None mean "this user already has an explicit grant on this project".
# On the duplicate branch the handler re-SELECTs the existing row to
# surface its current role in the 409 body so the operator can
# decide between PATCH (change role) and DELETE (revoke).
_INSERT_PROJECT_MEMBER_SQL = """
INSERT INTO project_members (user_id, project_id, role)
VALUES ($1, $2, $3)
ON CONFLICT (user_id, project_id) DO NOTHING
RETURNING user_id, project_id, role, created_at
"""

# Atomic role update. ``RETURNING None`` here means the row vanished
# between the FOR-UPDATE-less precheck and the UPDATE — handler treats
# it as 404 (concurrent DELETE won the race).
_UPDATE_PROJECT_MEMBER_ROLE_SQL = """
UPDATE project_members
SET role = $3
WHERE user_id = $1 AND project_id = $2
RETURNING user_id, project_id, role, created_at
"""

# DELETE … RETURNING projects the soon-to-be-gone row so the audit
# blob can capture the prior role in one round-trip. RETURNING None
# means "row already gone" → idempotent already_removed branch.
_DELETE_PROJECT_MEMBER_SQL = """
DELETE FROM project_members
WHERE user_id = $1 AND project_id = $2
RETURNING user_id, project_id, role, created_at
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _row_to_project_member_dict(row) -> dict:
    """Project a project_members row to the JSON response body."""
    return {
        "user_id": row["user_id"],
        "project_id": row["project_id"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /tenants/{tid}/projects/{pid}/members
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/members",
    status_code=201,
)
async def create_project_member(
    tenant_id: str,
    project_id: str,
    body: CreateProjectMemberRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Grant an existing tenant member an explicit project role.

    Status codes
    ────────────
    * 201 — granted; ``project_members`` row inserted.
    * 403 — caller is not tenant admin / owner, not super_admin, and
            not project owner.
    * 404 — well-formed ids but no such tenant or no such project on
            this tenant.
    * 409 — target user already has an explicit row on this project;
            response body carries ``existing_role`` so the operator
            can PATCH (change) or DELETE (revoke) instead.
    * 422 — malformed tenant_id / project_id / user_id / role; or the
            target user has no active tenant membership on this
            tenant (operator must invite + accept first).
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    # RBAC ahead of any existence probe so a guess-the-id scan can't
    # enumerate via timing alone.
    if not await _user_can_manage_project_members(
        actor, tenant_id, project_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin / owner on {tenant_id!r}, "
                f"platform super_admin, or project owner on "
                f"{project_id!r}; caller has no qualifying role"
            ),
        )


    # Tenant existence — clean 404.
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # Project existence + tenant-scope — 404 covers both
    # "no such project" and "project belongs to another tenant" by
    # design (caller has no business probing cross-tenant existence).
    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    # Target-user-must-be-active-tenant-member precondition.
    async with get_pool().acquire() as conn:
        tm_row = await conn.fetchrow(
            _FETCH_TARGET_USER_TENANT_MEMBERSHIP_SQL,
            body.user_id, tenant_id,
        )
    if tm_row is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"user {body.user_id!r} is not a member of "
                    f"tenant {tenant_id!r}; invite the user first via "
                    f"POST /tenants/{tenant_id}/invites"
                ),
                "tenant_id": tenant_id,
                "user_id": body.user_id,
            },
        )
    if tm_row["status"] != "active":
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"user {body.user_id!r} has membership on "
                    f"tenant {tenant_id!r} but it is not active "
                    f"(status={tm_row['status']!r}); reactivate the "
                    f"tenant membership before granting project role"
                ),
                "tenant_id": tenant_id,
                "user_id": body.user_id,
                "tenant_membership_status": tm_row["status"],
            },
        )

    # Atomic INSERT — duplicate (user_id, project_id) lands on the
    # 409 branch with the existing role surfaced so the operator can
    # decide between PATCH and DELETE.
    async with get_pool().acquire() as conn:
        new_row = await conn.fetchrow(
            _INSERT_PROJECT_MEMBER_SQL,
            body.user_id, project_id, body.role,
        )

    if new_row is None:
        async with get_pool().acquire() as conn:
            existing = await conn.fetchrow(
                _FETCH_PROJECT_MEMBER_SQL, body.user_id, project_id,
            )
        existing_role = existing["role"] if existing else None
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"user {body.user_id!r} already has an explicit "
                    f"role on project {project_id!r}; PATCH to change "
                    f"or DELETE to revoke"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": body.user_id,
                "existing_role": existing_role,
            },
        )

    member = _row_to_project_member_dict(new_row)
    member["tenant_id"] = tenant_id
    try:
        await _audit.log(
            action="tenant_project_member_added",
            entity_kind="project_member",
            entity_id=f"{project_id}:{body.user_id}",
            before=None,
            after={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": body.user_id,
                "role": body.role,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_member_added audit emit failed (tenant=%s "
            "project=%s user=%s): %s",
            tenant_id, project_id, body.user_id, exc,
        )

    return JSONResponse(status_code=201, content=member)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATCH /tenants/{tid}/projects/{pid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.patch(
    "/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
)
async def patch_project_member(
    tenant_id: str,
    project_id: str,
    user_id: str,
    body: PatchProjectMemberRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Update an existing project_members row's role.

    Idempotent: re-PATCHing to the same role returns 200 with
    ``no_change=True`` and emits no audit row.

    Status codes
    ────────────
    * 200 — updated (or no_change=True if same-state PATCH).
    * 403 — caller is not tenant admin / owner, not super_admin, and
            not project owner on this project.
    * 404 — well-formed ids but no such tenant / no such project on
            this tenant / no explicit project_members row for this
            (user, project) pair (use POST to grant).
    * 422 — malformed ids / unknown role.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_user_id(user_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid user id: {user_id!r}; must match "
                    f"{USER_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_manage_project_members(
        actor, tenant_id, project_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin / owner on {tenant_id!r}, "
                f"platform super_admin, or project owner on "
                f"{project_id!r}; caller has no qualifying role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    async with get_pool().acquire() as conn:
        cur_row = await conn.fetchrow(
            _FETCH_PROJECT_MEMBER_SQL, user_id, project_id,
        )
    if cur_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"no explicit project membership for user "
                    f"{user_id!r} on project {project_id!r}; use POST "
                    f"to grant"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
            },
        )

    # Same-state short-circuit. No UPDATE, no audit row.
    if cur_row["role"] == body.role:
        member = _row_to_project_member_dict(cur_row)
        member["tenant_id"] = tenant_id
        member["no_change"] = True
        return JSONResponse(status_code=200, content=member)

    async with get_pool().acquire() as conn:
        new_row = await conn.fetchrow(
            _UPDATE_PROJECT_MEMBER_ROLE_SQL,
            user_id, project_id, body.role,
        )

    if new_row is None:
        # Concurrent DELETE removed the row between our precheck and
        # our UPDATE. Fall through to 404 — same outcome the caller
        # would have got if they had hit DELETE → PATCH ordering.
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project membership disappeared mid-PATCH for "
                    f"user {user_id!r} on project {project_id!r}; "
                    f"likely a concurrent DELETE — re-fetch and retry"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
            },
        )

    try:
        await _audit.log(
            action="tenant_project_member_updated",
            entity_kind="project_member",
            entity_id=f"{project_id}:{user_id}",
            before={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
                "role": cur_row["role"],
            },
            after={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
                "role": new_row["role"],
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_member_updated audit emit failed "
            "(tenant=%s project=%s user=%s): %s",
            tenant_id, project_id, user_id, exc,
        )

    member = _row_to_project_member_dict(new_row)
    member["tenant_id"] = tenant_id
    member["no_change"] = False
    return JSONResponse(status_code=200, content=member)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DELETE /tenants/{tid}/projects/{pid}/members/{user_id}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.delete(
    "/tenants/{tenant_id}/projects/{project_id}/members/{user_id}",
)
async def delete_project_member(
    tenant_id: str,
    project_id: str,
    user_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Remove the explicit project_members row for ``(user, project)``.

    Per alembic 0034 docstring this IS the project-layer suspension —
    once the row is gone the user falls back to the tenant default
    (admin/owner → contributor; member/viewer → no project access).
    There is no ``status`` column to soft-flip.

    Idempotent: a second DELETE on a row that was never granted (or
    was already removed) returns 200 with ``already_removed=True`` and
    emits no audit row — matches Y4 row 4 archive/restore posture.

    Status codes
    ────────────
    * 200 — removed (or already_removed=True if no row to delete).
    * 403 — caller is not tenant admin / owner, not super_admin, and
            not project owner on this project.
    * 404 — well-formed ids but no such tenant or no such project on
            this tenant.
    * 422 — malformed ids.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_user_id(user_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid user id: {user_id!r}; must match "
                    f"{USER_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_manage_project_members(
        actor, tenant_id, project_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin / owner on {tenant_id!r}, "
                f"platform super_admin, or project owner on "
                f"{project_id!r}; caller has no qualifying role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    async with get_pool().acquire() as conn:
        deleted_row = await conn.fetchrow(
            _DELETE_PROJECT_MEMBER_SQL, user_id, project_id,
        )

    if deleted_row is None:
        # Already absent — idempotent already_removed branch. No audit.
        return JSONResponse(
            status_code=200,
            content={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
                "already_removed": True,
            },
        )

    try:
        await _audit.log(
            action="tenant_project_member_removed",
            entity_kind="project_member",
            entity_id=f"{project_id}:{user_id}",
            before={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "user_id": user_id,
                "role": deleted_row["role"],
            },
            after=None,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_member_removed audit emit failed "
            "(tenant=%s project=%s user=%s): %s",
            tenant_id, project_id, user_id, exc,
        )

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "user_id": user_id,
            "role": deleted_row["role"],
            "already_removed": False,
        },
    )

    return removed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y4 (#280) row 6 — POST /api/v1/tenants/{tid}/projects/{pid}/shares
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Cross-tenant project share. Tenant A (the project's owning tenant)
# grants tenant B (the guest tenant) read- or write-level access to one
# of A's projects, without materialising membership rows on either side
# and without B paying A's egress / quota costs. Recorded in
# ``project_shares`` (alembic 0036).
#
# The TODO row literal:
#   POST /.../projects/{pid}/shares
#   body : { guest_tenant_id, role, expires_at? }
#   effect: project_shares row inserted; guest tenant sees this project
#           in its admin console under the "Guest" tab
#
# Design decisions (mirroring 0036 docstring contract)
# ────────────────────────────────────────────────────
# * **Role enum is ``viewer`` / ``contributor`` only — NOT ``owner``**.
#   A guest tenant fundamentally cannot own a project belonging to a
#   different tenant. The DB CHECK enforces this server-side; the
#   pydantic Literal mirrors it so 422 fires before any DB I/O.
# * **``guest_tenant_id <> tenant_id``** guard at the application layer
#   per the migration's deferred CHECK note. A tenant cannot share a
#   project to itself — this is a 422 (caller body malformed) not 403.
# * **Atomic INSERT with ``ON CONFLICT (project_id, guest_tenant_id)
#   DO NOTHING RETURNING …``** — the UNIQUE composite from migration
#   0036 makes RETURNING None unambiguously signal "this guest tenant
#   already has an active share row on this project". On the duplicate
#   branch the handler re-fetches the existing row to surface its id +
#   role in the 409 body so the operator can choose: PATCH role (Y4
#   row-7+) / DELETE revoke / leave alone.
# * **``share_id`` shape ``psh-<16 hex chars>``** matches the convention
#   from migration 0036's docstring (``psh-`` prefix; same minting
#   pattern as ``p-`` projects, ``inv-`` invites).
# * **``granted_by`` FK ``ON DELETE SET NULL``** — anonymous (open-mode
#   dev fallback) and api-key callers do not have rows in ``users``;
#   the existing ``_resolve_created_by`` helper already handles this
#   FK-clean (returns NULL for those callers). audit row still pins
#   the actor email.
# * **``expires_at`` semantics** — NULLable means "permanent share"
#   (the granter explicitly opts in to no TTL). Non-null must be a
#   ``YYYY-MM-DD HH:MM:SS`` UTC string strictly in the future of
#   ``now() at time zone 'utc'``; an expires_at already in the past
#   is a 422 (caller body malformed — granting an already-expired
#   share is meaningless). The format mirrors ``created_at`` /
#   ``archived_at`` so audit-side text comparisons remain correct.
# * **RBAC reuses ``_user_can_create_project_in``** — sharing a project
#   to another tenant is a tenant-level operation (it transfers some
#   degree of project control to a guest tenant) and matches the trust
#   boundary of project CRUD itself: super_admin / tenant
#   admin+owner only. Project owner is deliberately NOT a privilege
#   gate here — share grants change the project's exposure surface
#   beyond the owning tenant, and only an owning-tenant admin should
#   make that call. Member / viewer / contributor / project-owner
#   without tenant-admin: 403.
#
# Status codes
# ────────────
#   201 — share inserted; ``project_shares`` row created
#   403 — caller is not tenant admin / owner on the *owning* tenant
#         (the {tid} in the URL) and not platform super_admin
#   404 — well-formed ids but no such tenant / no such project on
#         this tenant / no such guest tenant
#   409 — share already exists for (project, guest_tenant); body
#         carries ``existing_share_id`` + ``existing_role`` so the
#         operator can decide PATCH (change role) vs DELETE (revoke)
#         vs no-op
#   422 — malformed tenant_id / project_id / guest_tenant_id pattern,
#         unknown role, guest_tenant_id == tenant_id (self-share),
#         malformed expires_at, expires_at in the past
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# New module-level constants:
#   * ``PROJECT_SHARE_ROLE_ENUM`` — DB-CHECK-aligned tuple (drift-guard
#     test enforces: not 'owner')
#   * ``SHARE_ID_PATTERN`` / ``_SHARE_ID_RE`` — minted-by-handler shape
#   * ``EXPIRES_AT_FORMAT`` — string-comparison-safe UTC text format
#   * ``_PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES`` — frozenset of
#     tenant roles that may grant shares
#   * 2 SQL constants — ``_INSERT_PROJECT_SHARE_SQL``,
#     ``_FETCH_EXISTING_PROJECT_SHARE_SQL`` (existing
#     ``_FETCH_TENANT_SQL`` reused for both owner-tenant and guest-
#     tenant existence probes; ``_FETCH_PROJECT_TENANT_SCOPED_SQL``
#     reused for the project ownership scope check)
# All immutable; every uvicorn worker derives the same value from
# source — qualifying answer #1. DB state is shared via PG; the
# composite UNIQUE ``(project_id, guest_tenant_id)`` serialises
# concurrent INSERTs via ``ON CONFLICT DO NOTHING`` — qualifying
# answer #2. No new in-memory cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# POST is a single ``INSERT … ON CONFLICT (project_id, guest_tenant_id)
# DO NOTHING RETURNING`` — concurrent admins racing the same
# (project, guest) target resolve to one winner (RETURNING populated)
# and one loser (RETURNING None → 409 with the existing row surfaced).
# No new timing-visible behaviour.

# Project-scope role enum for cross-tenant shares.  Mirrors the DB
# CHECK on ``project_shares.role`` from alembic 0036 — drift here
# would let a regressed POST set a role the DB then rejects.
# Deliberately distinct from PROJECT_MEMBER_ROLE_ENUM (which includes
# 'owner') because a guest tenant cannot own the host's project.
PROJECT_SHARE_ROLE_ENUM = ("viewer", "contributor")

# Share id — minted by this handler; ``psh-<16 hex chars>`` matches
# the migration 0036 docstring's prefix convention.
SHARE_ID_PATTERN = r"^psh-[a-z0-9]{4,64}$"
_SHARE_ID_RE = re.compile(SHARE_ID_PATTERN)

# String-comparison-safe UTC text format.  Same shape as
# ``projects.created_at`` / ``projects.archived_at`` — keeps
# ``expires_at < $now`` lexicographic comparisons correct without
# dialect-specific date arithmetic.
EXPIRES_AT_FORMAT = "%Y-%m-%d %H:%M:%S"


def _is_valid_share_id(sid: str) -> bool:
    return bool(sid) and bool(_SHARE_ID_RE.match(sid))


def _mint_share_id() -> str:
    """``psh-`` + 16 hex chars (64 random bits).  Distinct enough to
    make collisions a non-event over the lifetime of the platform."""
    return f"psh-{secrets.token_hex(8)}"


# Same admin-tier gate as project CRUD.  Sharing a project to another
# tenant is a tenant-level operation (changes the project's exposure
# surface beyond the owning tenant) and lives at the tenant admin
# trust boundary.
_PROJECT_SHARE_CREATE_ALLOWED_MEMBERSHIP_ROLES = frozenset({"owner", "admin"})


class CreateProjectShareRequest(BaseModel):
    """Body for ``POST /api/v1/tenants/{tid}/projects/{pid}/shares``."""

    guest_tenant_id: str = Field(
        pattern=TENANT_ID_PATTERN,
        description=(
            "Tenant id of the guest that should receive this share. "
            "Must NOT equal the URL's {tid} — a tenant cannot share a "
            "project to itself (handler 422)."
        ),
    )
    role: Literal["viewer", "contributor"] = Field(
        description=(
            "Project-scope role granted to members of the guest tenant. "
            "Must be one of (viewer, contributor) — 'owner' is "
            "deliberately rejected because a guest tenant cannot own a "
            "project belonging to a different tenant (alembic 0036 "
            "DB CHECK enforces this server-side)."
        ),
    )
    expires_at: str | None = Field(
        default=None,
        description=(
            "Optional UTC expiry timestamp in ``YYYY-MM-DD HH:MM:SS`` "
            "format.  ``None`` means the share is permanent (the "
            "granter explicitly opts in to no TTL).  Non-null values "
            "must be strictly in the future of ``now() at time zone "
            "'utc'``; an already-expired ``expires_at`` is a 422."
        ),
    )


# Atomic INSERT with ``ON CONFLICT (project_id, guest_tenant_id) DO
# NOTHING`` — the UNIQUE composite from migration 0036 makes RETURNING
# None signal "this guest tenant already has a share row on this
# project".  RETURNING the full row gives the response body and audit
# blob a single source of truth.
_INSERT_PROJECT_SHARE_SQL = """
INSERT INTO project_shares (
    id, project_id, guest_tenant_id, role, granted_by, expires_at
) VALUES (
    $1, $2, $3, $4, $5, $6
)
ON CONFLICT (project_id, guest_tenant_id) DO NOTHING
RETURNING id, project_id, guest_tenant_id, role,
          granted_by, created_at, expires_at
"""

# Read the existing share row when the INSERT lost the dup race.
# Surfaces existing_share_id + existing_role so the operator can pick
# PATCH (role change) vs DELETE (revoke) vs no-op.
_FETCH_EXISTING_PROJECT_SHARE_SQL = """
SELECT id, project_id, guest_tenant_id, role,
       granted_by, created_at, expires_at
FROM project_shares
WHERE project_id = $1 AND guest_tenant_id = $2
"""


def _row_to_project_share_dict(row) -> dict:
    """Project a project_shares row to the JSON response body."""
    return {
        "share_id": row["id"],
        "project_id": row["project_id"],
        "guest_tenant_id": row["guest_tenant_id"],
        "role": row["role"],
        "granted_by": row["granted_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def _validate_expires_at(raw: str) -> tuple[bool, str]:
    """Return ``(ok, normalized_or_reason)``.

    On success: ``(True, raw)`` — the caller-supplied string is valid
    UTC ``YYYY-MM-DD HH:MM:SS`` format and strictly in the future of
    ``datetime.utcnow()``.

    On failure: ``(False, "<reason>")`` — caller surfaces a 422 with
    the reason in the detail.
    """
    from datetime import datetime as _dt

    if not isinstance(raw, str) or not raw:
        return False, "expires_at must be a non-empty string"
    try:
        parsed = _dt.strptime(raw, EXPIRES_AT_FORMAT)
    except ValueError:
        return False, (
            f"expires_at {raw!r} is not in 'YYYY-MM-DD HH:MM:SS' UTC "
            f"format"
        )
    now = _dt.utcnow()
    if parsed <= now:
        return False, (
            f"expires_at {raw!r} is not strictly in the future of "
            f"the current UTC time"
        )
    return True, raw


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/shares",
    status_code=201,
)
async def create_project_share(
    tenant_id: str,
    project_id: str,
    body: CreateProjectShareRequest,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Grant a guest tenant cross-tenant access to a project.

    Status codes
    ────────────
    * 201 — share row inserted; guest tenant now sees this project in
            its admin console under the "Guest" tab.
    * 403 — caller is not tenant admin / owner on the *owning* tenant
            and not platform super_admin.
    * 404 — well-formed ids but no such tenant / no such project on
            this tenant / no such guest tenant.
    * 409 — share already exists for ``(project, guest_tenant)``.
            Body carries ``existing_share_id`` + ``existing_role`` so
            the caller can pick PATCH / DELETE / no-op.
    * 422 — malformed tenant_id / project_id / guest_tenant_id /
            unknown role / guest_tenant_id == tenant_id / malformed
            expires_at / expires_at not in the future.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    # Self-share guard — a tenant cannot grant guest access on its own
    # project (the project is already in that tenant's namespace; the
    # guest tab would always show its own projects, which is nonsense).
    # 422 because this is a body validation concern, not RBAC.
    if body.guest_tenant_id == tenant_id:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"guest_tenant_id {body.guest_tenant_id!r} must "
                    f"not equal the owning tenant_id; a tenant cannot "
                    f"share a project to itself"
                ),
                "tenant_id": tenant_id,
                "guest_tenant_id": body.guest_tenant_id,
            },
        )

    # Optional expires_at — must parse + be strictly in the future.
    if body.expires_at is not None:
        ok, reason = _validate_expires_at(body.expires_at)
        if not ok:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": reason,
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "expires_at": body.expires_at,
                },
            )

    # RBAC — done before the existence probe so a guess-the-id scan
    # can't enumerate via timing.
    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    # Owning tenant existence — 404.
    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    # Project existence + tenant-scope — 404 covers both "no such
    # project" and "project belongs to another tenant" by design
    # (caller has no business probing cross-tenant existence).
    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    # Guest tenant existence — 404. Distinct lookup from the owning
    # tenant probe; uses the same SQL constant.
    async with get_pool().acquire() as conn:
        guest_tenant_row = await conn.fetchrow(
            _FETCH_TENANT_SQL, body.guest_tenant_id,
        )
    if guest_tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"guest tenant not found: {body.guest_tenant_id!r}"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "guest_tenant_id": body.guest_tenant_id,
            },
        )

    # Atomic INSERT — duplicate (project, guest_tenant) lands on the
    # 409 branch with the existing share id + role surfaced.
    share_id = _mint_share_id()
    granted_by = _resolve_created_by(actor)

    async with get_pool().acquire() as conn:
        new_row = await conn.fetchrow(
            _INSERT_PROJECT_SHARE_SQL,
            share_id, project_id, body.guest_tenant_id, body.role,
            granted_by, body.expires_at,
        )

    if new_row is None:
        # Duplicate share branch — surface the existing row's id +
        # role so UI can decide PATCH (change role) vs DELETE (revoke).
        async with get_pool().acquire() as conn:
            existing = await conn.fetchrow(
                _FETCH_EXISTING_PROJECT_SHARE_SQL,
                project_id, body.guest_tenant_id,
            )
        existing_id = existing["id"] if existing else None
        existing_role = existing["role"] if existing else None
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"guest tenant {body.guest_tenant_id!r} already "
                    f"has a share row on project {project_id!r}; "
                    f"PATCH the share to change role or DELETE to "
                    f"revoke"
                ),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "guest_tenant_id": body.guest_tenant_id,
                "existing_share_id": existing_id,
                "existing_role": existing_role,
            },
        )

    share = _row_to_project_share_dict(new_row)
    share["tenant_id"] = tenant_id
    try:
        await _audit.log(
            action="tenant_project_shared",
            entity_kind="project_share",
            entity_id=share_id,
            before=None,
            after={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "share_id": share_id,
                "guest_tenant_id": body.guest_tenant_id,
                "role": body.role,
                "expires_at": body.expires_at,
                "granted_by": granted_by,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_shared audit emit failed (tenant=%s "
            "project=%s share=%s): %s",
            tenant_id, project_id, share_id, exc,
        )

    # Y9 #285 row 1 — canonical dot-notation event
    # ``project_share.granted``. Cross-tenant share events are written
    # to BOTH the host tenant's chain (``tenant_id`` — the project's
    # owner) AND the guest tenant's chain (``body.guest_tenant_id`` —
    # the recipient). Each chain ends up with its own row so a
    # ``/admin/audit/tenants/{tid}`` query against either side surfaces
    # the share without needing to peek into the other tenant's chain.
    try:
        from backend import audit_events as _audit_events
        await _audit_events.emit_project_share_granted(
            host_tenant_id=tenant_id,
            guest_tenant_id=body.guest_tenant_id,
            project_id=project_id,
            share_id=share_id,
            role=body.role,
            expires_at=body.expires_at,
            granted_by=granted_by,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "project_share.granted audit emit failed (host=%s guest=%s "
            "project=%s share=%s): %s",
            tenant_id, body.guest_tenant_id, project_id, share_id, exc,
        )

    return JSONResponse(status_code=201, content=share)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Y8 (#284) row 5 — GET /tenants/{tid}/projects/{pid}/members
#  Y8 (#284) row 5 — GET /tenants/{tid}/projects/{pid}/shares
#  Y8 (#284) row 5 — DELETE /tenants/{tid}/projects/{pid}/shares/{sid}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Read-side complement to the Y4 row 5 / row 6 write-only POST/PATCH/
# DELETE member surface and POST share surface — needed by the Y8
# row 5 ``/projects/{pid}/settings`` page (project owner only, three
# tabs Members / Budget / Shares). Without the GET endpoints the
# settings page would have no list to render; the DELETE share
# endpoint completes the share lifecycle (granted → revoked) so the
# operator can pull a share back without going through the DB.
#
# Module-global state audit (SOP Step 1)
# ──────────────────────────────────────
# New module-level constants:
#   * 3 SQL constants — ``_LIST_PROJECT_MEMBERS_SQL`` (joins
#     ``project_members`` to ``users`` for email/name surface),
#     ``_LIST_PROJECT_SHARES_SQL`` (filtering tied to project_id),
#     ``_DELETE_PROJECT_SHARE_SQL`` (DELETE ... RETURNING for audit
#     payload).
# All immutable; every uvicorn worker derives the same value from
# source — qualifying answer #1. DB state is shared via PG; the
# DELETE on the composite UNIQUE serialises naturally — qualifying
# answer #2. No new in-memory cache.
#
# Read-after-write timing audit (SOP Step 1)
# ──────────────────────────────────────────
# GETs are read-only single-statement fetches; DELETE is a single
# ``DELETE … RETURNING`` (idempotent: RETURNING None means "row
# already gone" → ``already_revoked=True``, no audit emitted). No
# new timing-visible behaviour.
#
# RBAC
# ────
# ``GET .../members`` — same gate as POST/PATCH/DELETE:
#   ``_user_can_manage_project_members`` (super_admin / tenant
#   admin/owner / explicit ``project_members.role='owner'``). A
#   plain tenant member can NOT enumerate the project's roster
#   (matches the ``GET /tenants/{tid}/members`` posture which is
#   admin-only).
# ``GET .../shares`` — same gate as POST share:
#   ``_user_can_create_project_in`` (super_admin / tenant
#   admin/owner). Sharing is a tenant-trust-boundary operation;
#   project owners and below cannot enumerate cross-tenant grants.
# ``DELETE .../shares/{sid}`` — same gate as POST share, mirroring
#   the create surface. Idempotent: re-revoking a missing share
#   returns 200 with ``already_revoked=True`` (no audit row, no
#   side-effect — same posture as DELETE project_member).

# Read-only list of explicit project member rows joined to the users
# table for email + name surface. Sorted oldest-first to match the
# tenant-members listing convention and give stable cursor ordering.
_LIST_PROJECT_MEMBERS_SQL = """
SELECT
    pm.user_id     AS user_id,
    u.email        AS email,
    u.name         AS name,
    pm.project_id  AS project_id,
    pm.role        AS role,
    pm.created_at  AS created_at,
    u.enabled      AS user_enabled
FROM project_members pm
JOIN users u ON u.id = pm.user_id
WHERE pm.project_id = $1
ORDER BY pm.created_at ASC, u.email ASC
LIMIT $2
"""

# Read-only list of project_shares for a single project. The router
# filters by tenant scope BEFORE the SQL runs (404 if the project
# does not belong to this tenant), so this query only needs the
# project_id. ``expires_at`` is projected as-is — the frontend
# decides how to render permanent (NULL) vs expiring shares.
_LIST_PROJECT_SHARES_SQL = """
SELECT id, project_id, guest_tenant_id, role,
       granted_by, created_at, expires_at
FROM project_shares
WHERE project_id = $1
ORDER BY created_at ASC, id ASC
LIMIT $2
"""

# Delete by composite (share_id, project_id) — projecting the row
# back via RETURNING so the audit payload captures the prior role +
# guest tenant in one round-trip. RETURNING None means "share
# already absent" → idempotent already_revoked branch.
_DELETE_PROJECT_SHARE_SQL = """
DELETE FROM project_shares
WHERE id = $1 AND project_id = $2
RETURNING id, project_id, guest_tenant_id, role,
          granted_by, created_at, expires_at
"""

# Default + max page sizes for the two list endpoints. Tuned the
# same way as the Y4 row-2 list_projects defaults (50 / 200) so
# operators on a busy project don't get truncated unexpectedly.
LIST_PROJECT_MEMBERS_DEFAULT_LIMIT = 50
LIST_PROJECT_MEMBERS_MAX_LIMIT = 200
LIST_PROJECT_SHARES_DEFAULT_LIMIT = 50
LIST_PROJECT_SHARES_MAX_LIMIT = 200


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/members",
)
async def list_project_members(
    tenant_id: str,
    project_id: str,
    _request: Request,
    limit: int = Query(
        default=LIST_PROJECT_MEMBERS_DEFAULT_LIMIT,
        ge=1,
        le=LIST_PROJECT_MEMBERS_MAX_LIMIT,
        description=(
            f"Max rows to return (1..{LIST_PROJECT_MEMBERS_MAX_LIMIT}). "
            f"Default {LIST_PROJECT_MEMBERS_DEFAULT_LIMIT}."
        ),
    ),
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List explicit project_members rows for ``(tenant_id, project_id)``.

    Returns 200 with::

        {
            "tenant_id": "t-acme",
            "project_id": "p-fw",
            "count": 3,
            "members": [
                {"user_id": "u-alice", "email": "alice@x.io",
                 "name": "Alice", "role": "owner",
                 "created_at": "...", "user_enabled": true},
                ...
            ]
        }

    Status codes
    ────────────
    * 200 — happy path; ``members`` may be empty if no explicit
      project_members rows exist (tenant admins / owners still have
      project access via the tenant-default fallback documented in
      alembic 0034 — those rows are NOT enumerated here because they
      are not stored).
    * 403 — caller is not tenant admin / owner, not super_admin, and
            not project owner on this project (same gate as the write
            surface).
    * 404 — well-formed ids but no such tenant or no such project on
            this tenant.
    * 422 — malformed tenant_id / project_id.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_manage_project_members(
        actor, tenant_id, project_id,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin / owner on {tenant_id!r}, "
                f"platform super_admin, or project owner on "
                f"{project_id!r}; caller has no qualifying role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            _LIST_PROJECT_MEMBERS_SQL, project_id, limit,
        )

    members = [
        {
            "user_id": r["user_id"],
            "email": r["email"],
            "name": r["name"],
            "project_id": r["project_id"],
            "role": r["role"],
            "created_at": r["created_at"],
            "user_enabled": bool(r["user_enabled"]),
        }
        for r in rows
    ]

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "count": len(members),
            "members": members,
        },
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/shares",
)
async def list_project_shares(
    tenant_id: str,
    project_id: str,
    _request: Request,
    limit: int = Query(
        default=LIST_PROJECT_SHARES_DEFAULT_LIMIT,
        ge=1,
        le=LIST_PROJECT_SHARES_MAX_LIMIT,
        description=(
            f"Max rows to return (1..{LIST_PROJECT_SHARES_MAX_LIMIT}). "
            f"Default {LIST_PROJECT_SHARES_DEFAULT_LIMIT}."
        ),
    ),
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """List cross-tenant share grants for ``(tenant_id, project_id)``.

    Returns 200 with::

        {
            "tenant_id": "t-acme",
            "project_id": "p-fw",
            "count": 1,
            "shares": [
                {"share_id": "psh-...", "project_id": "p-fw",
                 "guest_tenant_id": "t-bob", "role": "viewer",
                 "granted_by": "u-alice",
                 "created_at": "...", "expires_at": null},
                ...
            ]
        }

    Status codes
    ────────────
    * 200 — happy path; ``shares`` may be empty.
    * 403 — caller is not tenant admin / owner on the *owning*
            tenant and not platform super_admin.
    * 404 — well-formed ids but no such tenant or no such project
            on this tenant.
    * 422 — malformed tenant_id / project_id.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            _LIST_PROJECT_SHARES_SQL, project_id, limit,
        )

    shares = [_row_to_project_share_dict(r) for r in rows]

    return JSONResponse(
        status_code=200,
        content={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "count": len(shares),
            "shares": shares,
        },
    )


@router.delete(
    "/tenants/{tenant_id}/projects/{project_id}/shares/{share_id}",
)
async def delete_project_share(
    tenant_id: str,
    project_id: str,
    share_id: str,
    _request: Request,
    actor: auth.User = Depends(auth.current_user),
) -> JSONResponse:
    """Revoke a cross-tenant share grant.

    Idempotent: re-revoking a missing share returns 200 with
    ``already_revoked=True`` and emits no audit row — matches the
    DELETE project_member posture (Y4 row 5).

    Status codes
    ────────────
    * 200 — share removed (or already_revoked=True).
    * 403 — caller is not tenant admin / owner on the *owning*
            tenant and not platform super_admin.
    * 404 — well-formed ids but no such tenant or no such project
            on this tenant.
    * 422 — malformed tenant_id / project_id / share_id.
    """
    if not _is_valid_tenant_id(tenant_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid tenant id: {tenant_id!r}; must match "
                    f"{TENANT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_project_id(project_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid project id: {project_id!r}; must match "
                    f"{PROJECT_ID_PATTERN}"
                ),
            },
        )
    if not _is_valid_share_id(share_id):
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"invalid share id: {share_id!r}; must match "
                    f"{SHARE_ID_PATTERN}"
                ),
            },
        )

    if not await _user_can_create_project_in(actor, tenant_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"requires tenant admin or above on {tenant_id!r}; "
                f"caller has no qualifying membership / role"
            ),
        )


    async with get_pool().acquire() as conn:
        tenant_row = await conn.fetchrow(_FETCH_TENANT_SQL, tenant_id)
    if tenant_row is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"tenant not found: {tenant_id!r}"},
        )

    async with get_pool().acquire() as conn:
        project_row = await conn.fetchrow(
            _FETCH_PROJECT_TENANT_SCOPED_SQL, project_id, tenant_id,
        )
    if project_row is None:
        return JSONResponse(
            status_code=404,
            content={
                "detail": (
                    f"project not found: {project_id!r} on "
                    f"tenant {tenant_id!r}"
                ),
            },
        )

    async with get_pool().acquire() as conn:
        deleted_row = await conn.fetchrow(
            _DELETE_PROJECT_SHARE_SQL, share_id, project_id,
        )

    if deleted_row is None:
        return JSONResponse(
            status_code=200,
            content={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "share_id": share_id,
                "already_revoked": True,
            },
        )

    try:
        await _audit.log(
            action="tenant_project_share_revoked",
            entity_kind="project_share",
            entity_id=share_id,
            before={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "share_id": share_id,
                "guest_tenant_id": deleted_row["guest_tenant_id"],
                "role": deleted_row["role"],
                "expires_at": deleted_row["expires_at"],
            },
            after=None,
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_share_revoked audit emit failed "
            "(tenant=%s project=%s share=%s): %s",
            tenant_id, project_id, share_id, exc,
        )

    revoked = _row_to_project_share_dict(deleted_row)
    revoked["tenant_id"] = tenant_id
    revoked["already_revoked"] = False
    return JSONResponse(status_code=200, content=revoked)

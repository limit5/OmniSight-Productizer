"""Y4 (#280) row 1 + row 2 + row 3 — tenant-scoped project REST surface.

Row 1 — POST /api/v1/tenants/{tid}/projects: create a project under a
tenant.  Row 2 — GET /api/v1/tenants/{tid}/projects: list projects in
a tenant, scoped by the caller's visibility (super_admin / tenant
admin → full; member / viewer → only projects with explicit
``project_members`` rows).  Row 3 — PATCH
/api/v1/tenants/{tid}/projects/{pid}: partial update of name,
plan_override, disk_budget_bytes, parent_id (with cycle detection
for sub-project trees).

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
            "is defence in depth."
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
    return row["role"] in _PROJECT_CREATE_ALLOWED_MEMBERSHIP_ROLES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SQL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Tenant existence probe — clean 404 ahead of the FK validation.
_FETCH_TENANT_SQL = "SELECT id FROM tenants WHERE id = $1"

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
    parent_id, plan_override, disk_budget_bytes, created_by
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9
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
    from backend.db_pool import get_pool
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

    # 5. Mint the project id + INSERT atomically. ON CONFLICT DO
    #    NOTHING + RETURNING resolves "insert-or-detect-duplicate" in
    #    one round-trip; RETURNING None unambiguously signals slug
    #    duplicate (PG's UNIQUE constraint owns the contention).
    project_id = _mint_project_id()
    created_by = _resolve_created_by(actor)

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            _INSERT_PROJECT_SQL,
            project_id, tenant_id, body.product_line,
            body.name, body.slug,
            body.parent_id, body.plan_override, body.disk_budget_bytes,
            created_by,
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
        from backend import audit as _audit
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
                "created_by": created_by,
            },
            actor=actor.email,
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "tenant_project_created audit emit failed (tenant=%s "
            "project=%s): %s", tenant_id, project_id, exc,
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

    from backend.db_pool import get_pool
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
    from backend.db_pool import get_pool
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
    "name", "plan_override", "disk_budget_bytes", "parent_id",
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
            "the DB CHECK is defence in depth."
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
    parent_id         = CASE WHEN $9  THEN $10::text   ELSE parent_id         END
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
                    "or 'parent_id'."
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

    from backend.db_pool import get_pool

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

    # 8. The patch transaction. Take the per-tenant advisory lock only
    #    when ``parent_id`` is being set (the only field that needs
    #    cross-row coordination); other fields touch only the single
    #    project row, which FOR UPDATE on the SELECT below already
    #    serialises.
    parent_id_is_changing = "parent_id" in set_fields
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            if parent_id_is_changing:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    _PROJECT_PATCH_LOCK_PREFIX + tenant_id,
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
        from backend import audit as _audit
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

"""Y4 (#280) row 1 + row 2 — tenant-scoped project REST surface.

Row 1 — POST /api/v1/tenants/{tid}/projects: create a project under a
tenant.  Row 2 — GET /api/v1/tenants/{tid}/projects: list projects in
a tenant, scoped by the caller's visibility (super_admin / tenant
admin → full; member / viewer → only projects with explicit
``project_members`` rows).

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

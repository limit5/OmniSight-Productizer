"""BS.2.1 — Catalog REST API (entries CRUD + subscription CRUD).

Surface
───────
``GET    /catalog/entries``           — list with filter / sort / pagination
``GET    /catalog/entries/{id}``      — single resolved entry
``POST   /catalog/entries``           — admin only, source ∈ {operator, override}
``PATCH  /catalog/entries/{id}``      — admin only (override-layer overlay)
``DELETE /catalog/entries/{id}``      — admin only (soft-delete custom / hide shipped)

``GET    /catalog/sources``                — admin only — list catalog_subscriptions
``POST   /catalog/sources``                — admin only — add catalog_subscription
``PATCH  /catalog/sources/{sub_id}``       — admin only — patch catalog_subscription
``DELETE /catalog/sources/{sub_id}``       — admin only — delete catalog_subscription
``POST   /catalog/sources/{sub_id}/sync``  — admin only — request immediate refresh

Resolution semantics (ADR §3.2)
───────────────────────────────
For a given entry id, the *resolved* row is the highest-priority live
row in the tuple ``(override, operator, shipped)``:

* ``shipped``  — global, ``tenant_id IS NULL``
* ``operator`` — per-tenant, custom row that has no shipped twin
  (eg. a tenant-only firmware)
* ``override`` — per-tenant, partial diff applied on top of a shipped
  base; only fields present in the override row replace the shipped
  values

A ``hidden = TRUE`` row tombstones a shipped row for that tenant —
the resolver still sees the shipped row globally, but the per-tenant
view filters it out. Same mechanic for ``operator`` rows that the
admin retired without losing audit history.

Auth (BS.2.3 wires the per-endpoint deps)
─────────────────────────────────────────
* ``GET /catalog/entries[/{id}]``     — ``require_operator`` (any
  authenticated operator can browse the catalog)
* ``POST/PATCH/DELETE /catalog/entries[...]`` — ``require_admin``
  (write paths are admin-only at this row; the install-track gating
  for tenant-side install is BS.2.2 + BS.7's PEP coaching card)
* ``*  /catalog/sources``             — ``require_admin``
  (subscription management is admin-only across the board; the
  subscription feed is per-tenant scope)

Tenant scope
────────────
Every read filters ``shipped`` rows globally + ``operator`` /
``override`` / ``subscription`` rows scoped to the caller's tenant
(``user.tenant_id``). Per ADR §3, ``shipped`` rows carry
``tenant_id IS NULL`` and the alembic 0051 ``CHECK`` enforces the
XOR. ``operator`` writes pin ``tenant_id`` from the caller; the
DB ``CHECK`` rejects any cross-tenant smuggling attempt that bypasses
the router.

Module-global / cross-worker state audit
────────────────────────────────────────
None introduced. Every read / write goes through ``db_pool.get_pool()``
which is shared across uvicorn workers via asyncpg + PG. Audit log
fans out via ``audit.log`` which uses the existing tenant-scoped
``pg_advisory_xact_lock`` for chain integrity. No new in-memory
caches, no module-level singletons.

Read-after-write timing audit
─────────────────────────────
The single inflight catalog write touches one row at a time
(``INSERT``, ``UPDATE``, soft-delete ``UPDATE``). PG MVCC + asyncpg
pool semantics mean a subsequent ``GET`` on the same tenant + same
entry id sees the new state on commit. There is no shared in-memory
cache that could lag — the resolver always re-runs against PG.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as _secrets
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend import auth as _au
from backend.db_context import set_tenant_id
from backend.routers._pagination import Limit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/catalog", tags=["catalog"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation constants (mirrored from alembic 0051 + _schema.yaml)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Closed sets exactly mirror the DB CHECK constraints from alembic 0051
# so 422 fires before the DB rejects the row. Keeping these as module
# constants (rather than re-importing from alembic) avoids a router →
# alembic import dependency at runtime.
ENTRY_FAMILIES: tuple[str, ...] = (
    "mobile", "embedded", "web", "software",
    "rtos", "cross-toolchain", "custom",
)
ENTRY_INSTALL_METHODS: tuple[str, ...] = (
    "noop", "docker_pull", "shell_script", "vendor_installer",
)
# 'shipped' / 'subscription' are read-only via the API surface:
#   shipped       — alembic seed migration (0052) is the only writer
#   subscription  — synced by the BS.8.5 feed worker, not human-written
# POST therefore accepts only 'operator' / 'override'.
WRITABLE_SOURCES: tuple[str, ...] = ("operator", "override")
ALL_SOURCES: tuple[str, ...] = ("shipped", "operator", "override", "subscription")

# kebab-case: lowercase alphanumerics in groups separated by single
# hyphens; no leading/trailing hyphen, no double hyphens. Equivalent
# to the lookahead form ``^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9]))*$`` in
# the catalog _schema.yaml, but written in lookahead-free form so
# pydantic-core (Rust regex backend) can compile it.
ENTRY_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_ENTRY_ID_RE = re.compile(ENTRY_ID_PATTERN)
ENTRY_ID_MAX_LEN = 64

SOURCE_AUTH_METHODS: tuple[str, ...] = ("none", "basic", "bearer", "signed_url")

# Pool ceiling for size_bytes — 1 TiB. Alembic doesn't enforce a cap
# (BIGINT is enough for any plausible install bundle); the schema yaml
# sets the same cap. Keeping it here means a malformed body returns
# 422 before it lands in PG.
SIZE_BYTES_MAX = 1 << 40

ENTRY_SORT_FIELDS: tuple[str, ...] = (
    "id", "vendor", "family", "display_name", "created_at", "updated_at",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pydantic schemas
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EntryCreate(BaseModel):
    """POST body for ``operator`` (per-tenant new entry) or ``override``
    (per-tenant overlay of an existing shipped row).

    For ``operator``: every required column must be present (the row is
    a standalone, fully-resolved entry). For ``override``: only the
    columns the admin wants to overlay are required; ``id`` must match
    an existing ``shipped`` row's id.
    """

    id: str = Field(min_length=1, max_length=ENTRY_ID_MAX_LEN, pattern=ENTRY_ID_PATTERN)
    source: Literal["operator", "override"] = "operator"
    vendor: str | None = Field(default=None, min_length=1, max_length=128)
    family: Literal[
        "mobile", "embedded", "web", "software",
        "rtos", "cross-toolchain", "custom",
    ] | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    install_method: Literal[
        "noop", "docker_pull", "shell_script", "vendor_installer",
    ] | None = None
    install_url: str | None = Field(default=None, max_length=2048)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0, le=SIZE_BYTES_MAX)
    depends_on: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class EntryPatch(BaseModel):
    """PATCH body — partial overlay (override layer). Every field is
    optional; ``None`` means "leave alone", a present value means "set".

    A PATCH on an entry id with no existing override / operator row
    creates an ``override`` row that overlays the shipped base. A
    PATCH on an existing operator/override row updates that row in
    place.
    """

    vendor: str | None = Field(default=None, min_length=1, max_length=128)
    family: Literal[
        "mobile", "embedded", "web", "software",
        "rtos", "cross-toolchain", "custom",
    ] | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    install_method: Literal[
        "noop", "docker_pull", "shell_script", "vendor_installer",
    ] | None = None
    install_url: str | None = Field(default=None, max_length=2048)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0, le=SIZE_BYTES_MAX)
    depends_on: list[str] | None = None
    metadata: dict | None = None
    hidden: bool | None = None

    def has_any_field(self) -> bool:
        return any(
            v is not None
            for v in (
                self.vendor, self.family, self.display_name, self.version,
                self.install_method, self.install_url, self.sha256,
                self.size_bytes, self.depends_on, self.metadata, self.hidden,
            )
        )


class SubscriptionCreate(BaseModel):
    """POST body for ``catalog_subscriptions`` rows (BS.8.5 feed).

    The ``auth_secret_ref`` field never carries plaintext — it's a
    pointer into the tenant secret store (set out-of-band by the
    operator). The router refuses any value containing whitespace
    so an accidental "paste the bearer token here" scenario fails
    422 instead of leaking into the DB.
    """

    feed_url: str = Field(min_length=1, max_length=2048)
    auth_method: Literal["none", "basic", "bearer", "signed_url"] = "none"
    auth_secret_ref: str | None = Field(default=None, max_length=256)
    refresh_interval_s: int = Field(default=86400, ge=60, le=30 * 86400)
    enabled: bool = True

    @field_validator("auth_secret_ref")
    @classmethod
    def _no_whitespace_in_ref(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if any(ch.isspace() for ch in v):
            raise ValueError(
                "auth_secret_ref must be a secret-store reference, not "
                "a literal secret (no whitespace allowed)"
            )
        return v


class SubscriptionPatch(BaseModel):
    feed_url: str | None = Field(default=None, min_length=1, max_length=2048)
    auth_method: Literal["none", "basic", "bearer", "signed_url"] | None = None
    auth_secret_ref: str | None = Field(default=None, max_length=256)
    refresh_interval_s: int | None = Field(default=None, ge=60, le=30 * 86400)
    enabled: bool | None = None

    def has_any_field(self) -> bool:
        return any(
            v is not None
            for v in (
                self.feed_url, self.auth_method, self.auth_secret_ref,
                self.refresh_interval_s, self.enabled,
            )
        )

    @field_validator("auth_secret_ref")
    @classmethod
    def _no_whitespace_in_ref(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if any(ch.isspace() for ch in v):
            raise ValueError(
                "auth_secret_ref must be a secret-store reference, not "
                "a literal secret (no whitespace allowed)"
            )
        return v


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ensure_tenant(user: _au.User) -> str:
    """Pin the request-scoped tenant context and return the tid."""
    tid = user.tenant_id or "t-default"
    set_tenant_id(tid)
    return tid


def _new_subscription_id() -> str:
    # ``sub-`` prefix matches the alembic 0051 PK convention. 16 hex
    # chars = 64 bits of entropy — plenty for a per-tenant id space.
    return f"sub-{_secrets.token_hex(8)}"


def _ts_to_iso(v: Any) -> Any:
    """Coerce a PG TIMESTAMPTZ (datetime) or SQLite REAL epoch into
    a JSON-serialisable representation.

    ``starlette.responses.JSONResponse`` calls plain ``json.dumps``
    which cannot serialise ``datetime``; passing the object through
    raises ``TypeError`` at response time. ISO 8601 with timezone is
    the contract the frontend already consumes for every other
    timestamp column in the API.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    isoformat = getattr(v, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return v


def _row_to_entry(row: Any) -> dict[str, Any]:
    """Marshal an ``asyncpg.Record`` from ``catalog_entries`` into the
    JSON-friendly shape the API contract returns.

    asyncpg gives ``JSONB`` columns back as Python objects already, but
    only when the connection has the ``json`` codec set; we don't rely
    on that here — the alembic migration uses ``JSONB`` so asyncpg's
    default behaviour is to deserialise. As a defence-in-depth the
    helper also accepts a raw string and parses it (covers the case
    where a connection-level codec is absent in dev).
    """
    def _coerce_json(v: Any, default: Any) -> Any:
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

    return {
        "id": row["id"],
        "source": row["source"],
        "schema_version": int(row["schema_version"]),
        "tenant_id": row["tenant_id"],
        "vendor": row["vendor"],
        "family": row["family"],
        "display_name": row["display_name"],
        "version": row["version"],
        "install_method": row["install_method"],
        "install_url": row["install_url"],
        "sha256": row["sha256"],
        "size_bytes": (
            int(row["size_bytes"]) if row["size_bytes"] is not None else None
        ),
        "depends_on": _coerce_json(row["depends_on"], []),
        "metadata": _coerce_json(row["metadata"], {}),
        "hidden": bool(row["hidden"]),
        "created_at": _ts_to_iso(row["created_at"]),
        "updated_at": _ts_to_iso(row["updated_at"]),
    }


def _resolve(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply ADR §3.2 priority: override > operator > shipped.

    ``override`` rows carry a partial overlay — only non-NULL columns
    replace the base. ``operator`` rows are standalone and shadow
    ``shipped`` if present. ``hidden=True`` removes the row from the
    visible result entirely (the caller is expected to filter the
    list returned by this helper).
    """
    by_source: dict[str, dict[str, Any]] = {r["source"]: r for r in rows}
    base: dict[str, Any] | None = None
    if "shipped" in by_source:
        base = dict(by_source["shipped"])
    if "operator" in by_source:
        # operator entirely shadows shipped (per ADR §3 — a tenant
        # carrying the same id as a shipped row is a *replacement*,
        # not an overlay).
        base = dict(by_source["operator"])
    if "override" in by_source:
        if base is None:
            # No shipped/operator base — an override-only row is
            # effectively the resolved entry on its own.
            base = dict(by_source["override"])
        else:
            # Partial overlay: only non-NULL columns from override
            # replace base. ``id`` / ``schema_version`` / ``hidden``
            # are special-cased — see comments below.
            ov = by_source["override"]
            for col, val in ov.items():
                if col in {"id", "tenant_id", "source", "created_at"}:
                    # id stays the same; tenant_id pivots to the
                    # override's tenant scope; source becomes
                    # 'override' so the consumer sees that the row
                    # is overlay-resolved, not pristine shipped.
                    if col == "source":
                        base["source"] = "override"
                    elif col == "tenant_id":
                        base["tenant_id"] = ov["tenant_id"]
                    continue
                if col == "hidden":
                    # hidden=TRUE on override = tombstone for that
                    # tenant; the caller filters it out. We propagate
                    # so the resolver caller can decide.
                    if val:
                        base["hidden"] = True
                    continue
                if val is None:
                    continue
                # depends_on / metadata: empty list / empty dict
                # counts as "explicitly set to empty" — caller wrote
                # the empty value, we honour it.
                base[col] = val
            base["updated_at"] = ov["updated_at"] or base.get("updated_at")
    return base or {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GET /catalog/entries — list (filter + sort + pagination)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/entries")
async def list_entries(
    request: Request,
    family: str | None = Query(default=None),
    source: str | None = Query(default=None),
    vendor: str | None = Query(default=None, max_length=128),
    install_method: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=256),
    sort: Literal[
        "id", "vendor", "family", "display_name",
        "created_at", "updated_at",
    ] = Query(default="display_name"),
    order: Literal["asc", "desc"] = Query(default="asc"),
    include_hidden: bool = Query(default=False),
    limit: int = Limit(default=100, max_cap=500),
    offset: int = Query(default=0, ge=0, le=100_000),
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """List catalog entries visible to the caller.

    Visibility rules:

    * ``shipped`` rows are global; every authenticated operator sees
      every shipped row regardless of tenant.
    * ``operator`` / ``override`` / ``subscription`` rows are scoped
      to ``user.tenant_id`` only.
    * ``hidden = TRUE`` rows are excluded by default; ``include_hidden``
      flips the filter for admin-side debugging (the auth gate is
      ``require_operator`` here so an operator can see what the admin
      tombstoned, but that's the same trust boundary as already
      reading the catalog).

    Sort key + pagination: server-side, indexed where possible. The
    ``offset`` ceiling (100k) is the same defence-in-depth guard the
    rest of the codebase uses against ``offset=10^9`` DoS.
    """
    tid = _ensure_tenant(user)

    if family is not None and family not in ENTRY_FAMILIES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown family: {family!r}; "
                   f"must be one of {list(ENTRY_FAMILIES)}",
        )
    if source is not None and source not in ALL_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown source: {source!r}; "
                   f"must be one of {list(ALL_SOURCES)}",
        )
    if install_method is not None and install_method not in ENTRY_INSTALL_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown install_method: {install_method!r}; "
                   f"must be one of {list(ENTRY_INSTALL_METHODS)}",
        )

    # Tenant scope: shipped (NULL tenant_id) ∪ caller's tenant rows.
    where = ["(tenant_id IS NULL OR tenant_id = $1)"]
    params: list[Any] = [tid]
    if not include_hidden:
        where.append("hidden = FALSE")
    if family is not None:
        params.append(family)
        where.append(f"family = ${len(params)}")
    if source is not None:
        params.append(source)
        where.append(f"source = ${len(params)}")
    if vendor is not None:
        params.append(vendor)
        where.append(f"vendor = ${len(params)}")
    if install_method is not None:
        params.append(install_method)
        where.append(f"install_method = ${len(params)}")
    if q:
        # ILIKE on display_name + vendor + id. Three-column OR is
        # cheap at the catalog scale (~1k rows worst case for the
        # first year) and avoids the operational cost of a tsvector
        # column right now. If the catalog grows past 50k rows the
        # right answer is a GIN index over to_tsvector(...) added
        # in a future alembic.
        params.append(f"%{q}%")
        idx = len(params)
        where.append(
            f"(display_name ILIKE ${idx} OR vendor ILIKE ${idx} "
            f"OR id ILIKE ${idx})"
        )

    # Sort whitelist guards against injection — Literal-typed Query
    # already does this at the FastAPI layer, but we re-check before
    # interpolating into SQL because that's the load-bearing safety
    # rule when we drop a parameter into an ORDER BY clause (asyncpg
    # doesn't allow ``$N`` placeholders for column names).
    if sort not in ENTRY_SORT_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown sort field: {sort!r}",
        )
    order_sql = "ASC" if order == "asc" else "DESC"

    params.append(int(limit))
    params.append(int(offset))
    sql = (
        "SELECT id, source, schema_version, tenant_id, vendor, family, "
        "display_name, version, install_method, install_url, sha256, "
        "size_bytes, depends_on, metadata, hidden, "
        "created_at, updated_at "
        "FROM catalog_entries "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {sort} {order_sql}, id ASC "
        f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )
    count_sql = (
        f"SELECT COUNT(*) FROM catalog_entries WHERE {' AND '.join(where)}"
    )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
        # Count uses every WHERE param except the trailing limit/offset
        # pair (the last two we appended).
        total = await conn.fetchval(count_sql, *params[:-2])

    items = [_row_to_entry(r) for r in rows]
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
#  GET /catalog/entries/{entry_id} — single resolved entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    raw: bool = Query(default=False),
    user: _au.User = Depends(_au.require_operator),
) -> JSONResponse:
    """Return the resolved entry for *entry_id* in the caller's tenant.

    By default returns the resolved view (override > operator > shipped).
    ``raw=true`` returns every layer the caller can see, in priority
    order, so the admin UI can show the diff a tenant override layered
    on top of the shipped base.
    """
    if not _ENTRY_ID_RE.match(entry_id) or len(entry_id) > ENTRY_ID_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"invalid entry id: {entry_id!r}; must match "
                   f"{ENTRY_ID_PATTERN}",
        )
    tid = _ensure_tenant(user)

    # NOTE: deliberately NO ``hidden = FALSE`` filter here. A hidden
    # override row is a TOMBSTONE — it has to reach ``_resolve`` so the
    # propagation rule (override.hidden=TRUE → resolved.hidden=TRUE)
    # can fire and the handler can 404. Filtering at SQL would make the
    # resolver see only the shipped base and merge to a non-hidden
    # row — which is exactly the bug BS.2.4 caught.
    sql = (
        "SELECT id, source, schema_version, tenant_id, vendor, family, "
        "display_name, version, install_method, install_url, sha256, "
        "size_bytes, depends_on, metadata, hidden, "
        "created_at, updated_at "
        "FROM catalog_entries "
        "WHERE id = $1 AND (tenant_id IS NULL OR tenant_id = $2) "
        "ORDER BY CASE source "
        "  WHEN 'override' THEN 0 "
        "  WHEN 'operator' THEN 1 "
        "  WHEN 'subscription' THEN 2 "
        "  WHEN 'shipped' THEN 3 "
        "  ELSE 4 END"
    )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, entry_id, tid)

    if not rows:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    layers = [_row_to_entry(r) for r in rows]
    if raw:
        return JSONResponse(status_code=200, content={"id": entry_id, "layers": layers})
    resolved = _resolve(layers)
    if resolved.get("hidden"):
        # Tenant tombstoned this entry — caller doesn't see it via the
        # default surface. (raw=true still returns the layers so the
        # admin UI can recover.)
        raise HTTPException(status_code=404, detail="catalog entry not found")
    return JSONResponse(status_code=200, content=resolved)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  POST /catalog/entries — admin only
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.post("/entries", status_code=201)
async def create_entry(
    body: EntryCreate,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Create an ``operator`` (per-tenant new entry) or ``override``
    (per-tenant overlay of an existing shipped row) catalog entry.

    For ``operator`` source: every required column must be present —
    the row is a standalone resolved entry. For ``override``: ``id``
    must reference an existing shipped row (we look it up to validate);
    optional columns left ``None`` mean "inherit from the shipped base".

    409 on duplicate ``(id, source, tenant_id)`` per the partial UNIQUE
    index ``uq_catalog_entries_visible``.
    """
    if body.source == "operator":
        # Operator rows are standalone — required cols must be set.
        missing = [
            f for f in ("vendor", "family", "display_name", "version", "install_method")
            if getattr(body, f) is None
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"operator entry missing required fields: {missing}",
            )

    tid = _ensure_tenant(user)

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        if body.source == "override":
            # Validate the shipped base exists. Without this, an admin
            # can attach an override to a non-existent id which silently
            # becomes a "phantom override" the resolver returns as if
            # it were a real entry.
            base_exists = await conn.fetchval(
                "SELECT 1 FROM catalog_entries "
                "WHERE id = $1 AND source = 'shipped' AND hidden = FALSE",
                body.id,
            )
            if not base_exists:
                raise HTTPException(
                    status_code=404,
                    detail=f"shipped base {body.id!r} does not exist; "
                           "override needs an existing shipped row",
                )

        # Defaults: override rows carry NULL for unset columns (so
        # the resolver inherits from the base); operator rows must
        # be fully populated (validated above).
        try:
            row = await conn.fetchrow(
                "INSERT INTO catalog_entries "
                "  (id, source, tenant_id, vendor, family, display_name, "
                "   version, install_method, install_url, sha256, "
                "   size_bytes, depends_on, metadata, hidden) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, "
                "        $12::jsonb, $13::jsonb, FALSE) "
                "RETURNING id, source, schema_version, tenant_id, vendor, "
                "          family, display_name, version, install_method, "
                "          install_url, sha256, size_bytes, depends_on, "
                "          metadata, hidden, created_at, updated_at",
                body.id, body.source, tid,
                body.vendor, body.family, body.display_name,
                body.version, body.install_method, body.install_url,
                body.sha256, body.size_bytes,
                json.dumps(body.depends_on),
                json.dumps(body.metadata),
            )
        except Exception as exc:
            msg = str(exc)
            # asyncpg unique-violation on uq_catalog_entries_visible
            if "uq_catalog_entries_visible" in msg or "duplicate key" in msg:
                raise HTTPException(
                    status_code=409,
                    detail=f"entry {body.id!r} with source={body.source!r} "
                           "already exists for this tenant",
                ) from exc
            # CHECK violation (family / install_method / source XOR)
            if "violates check constraint" in msg or "check constraint" in msg:
                raise HTTPException(status_code=422, detail=msg) from exc
            raise

    out = _row_to_entry(row)
    _emit_audit_safely(
        action="catalog_entry_created",
        entity_id=body.id,
        actor=user.email,
        before=None,
        after={"id": body.id, "source": body.source, "family": body.family},
    )
    return JSONResponse(status_code=201, content=out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATCH /catalog/entries/{id} — admin only (override layer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.patch("/entries/{entry_id}")
async def patch_entry(
    entry_id: str,
    body: EntryPatch,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Apply an override-layer patch to *entry_id*.

    Behaviour:

    * If a tenant-scoped ``override`` row already exists for *entry_id*,
      update it in place.
    * Else if a tenant-scoped ``operator`` row exists, update the
      ``operator`` row in place (operator entries patch in place — they
      have no shipped base to overlay).
    * Else create a new ``override`` row that overlays the shipped base.
      404 if no shipped / operator / override row exists for *entry_id*.

    Partial semantics: every ``None`` field in the body is "leave
    alone"; every present field replaces the column on the override /
    operator row.
    """
    if not _ENTRY_ID_RE.match(entry_id) or len(entry_id) > ENTRY_ID_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"invalid entry id: {entry_id!r}; must match "
                   f"{ENTRY_ID_PATTERN}",
        )
    if not body.has_any_field():
        raise HTTPException(
            status_code=422,
            detail="empty PATCH body — supply at least one field",
        )

    tid = _ensure_tenant(user)
    fields = body.model_dump(exclude_unset=True)

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            # Locate an existing operator or override row for this
            # (entry_id, tenant_id) — that row gets patched in place.
            existing = await conn.fetchrow(
                "SELECT id, source, tenant_id, hidden FROM catalog_entries "
                "WHERE id = $1 AND tenant_id = $2 "
                "  AND source IN ('operator','override') "
                "ORDER BY CASE source "
                "  WHEN 'override' THEN 0 ELSE 1 END "
                "LIMIT 1",
                entry_id, tid,
            )
            if existing is not None:
                row = await _update_entry_row(
                    conn, existing["id"], existing["source"], tid, fields,
                )
            else:
                # No tenant-scoped row → need a shipped base to overlay.
                base = await conn.fetchval(
                    "SELECT 1 FROM catalog_entries "
                    "WHERE id = $1 AND source = 'shipped' "
                    "  AND hidden = FALSE",
                    entry_id,
                )
                if not base:
                    raise HTTPException(
                        status_code=404,
                        detail="catalog entry not found",
                    )
                row = await _create_override_row(
                    conn, entry_id, tid, fields,
                )

    out = _row_to_entry(row)
    _emit_audit_safely(
        action="catalog_entry_patched",
        entity_id=entry_id,
        actor=user.email,
        before=None,
        after={"id": entry_id, "fields": list(fields.keys())},
    )
    return JSONResponse(status_code=200, content=out)


async def _update_entry_row(
    conn: Any, entry_id: str, source: str, tenant_id: str,
    fields: dict[str, Any],
) -> Any:
    """Apply a partial UPDATE to an existing operator/override row.

    ``fields`` is the subset of ``EntryPatch`` columns the caller wrote.
    ``depends_on`` / ``metadata`` are JSONB and need explicit casts.
    """
    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in fields.items():
        if col in ("depends_on", "metadata"):
            params.append(json.dumps(val))
            set_parts.append(f"{col} = ${len(params)}::jsonb")
        else:
            params.append(val)
            set_parts.append(f"{col} = ${len(params)}")
    # updated_at bump on every PATCH so the resolver picks up the
    # mutation in the column-level "what changed last" view.
    set_parts.append("updated_at = now()")

    params.append(entry_id)
    params.append(source)
    params.append(tenant_id)
    sql = (
        "UPDATE catalog_entries "
        f"SET {', '.join(set_parts)} "
        f"WHERE id = ${len(params) - 2} "
        f"  AND source = ${len(params) - 1} "
        f"  AND tenant_id = ${len(params)} "
        "RETURNING id, source, schema_version, tenant_id, vendor, family, "
        "          display_name, version, install_method, install_url, "
        "          sha256, size_bytes, depends_on, metadata, hidden, "
        "          created_at, updated_at"
    )
    return await conn.fetchrow(sql, *params)


async def _create_override_row(
    conn: Any, entry_id: str, tenant_id: str, fields: dict[str, Any],
) -> Any:
    """Create a fresh override row for (entry_id, tenant_id).

    ``fields`` carries only the columns the admin wants to overlay;
    everything else stays NULL so the resolver inherits from the
    shipped base.
    """
    cols = ["id", "source", "tenant_id"]
    placeholders = ["$1", "'override'", "$2"]
    params: list[Any] = [entry_id, tenant_id]
    for col, val in fields.items():
        if col in ("depends_on", "metadata"):
            params.append(json.dumps(val))
            cols.append(col)
            placeholders.append(f"${len(params)}::jsonb")
        elif col == "hidden":
            params.append(bool(val))
            cols.append(col)
            placeholders.append(f"${len(params)}")
        else:
            params.append(val)
            cols.append(col)
            placeholders.append(f"${len(params)}")
    sql = (
        f"INSERT INTO catalog_entries ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        "RETURNING id, source, schema_version, tenant_id, vendor, family, "
        "          display_name, version, install_method, install_url, "
        "          sha256, size_bytes, depends_on, metadata, hidden, "
        "          created_at, updated_at"
    )
    return await conn.fetchrow(sql, *params)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DELETE /catalog/entries/{id} — soft-delete custom / hide shipped
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Hide an entry from the caller's tenant.

    * If a tenant-scoped ``operator`` row exists: soft-delete via
      ``hidden = TRUE``. The row stays in the table (audit trail) but
      drops out of the resolved view.
    * Else if a tenant-scoped ``override`` row exists: same — set
      ``hidden = TRUE``.
    * Else create a tombstone ``override`` row with ``hidden = TRUE``
      so the shipped row stops resolving for this tenant.

    Shipped rows themselves are never deleted by this endpoint —
    deletion is only ever per-tenant. A tenant who wants the shipped
    row back can DELETE the tombstone (the admin UI calls
    ``unhide_entry`` for that, BS.6.x).
    """
    if not _ENTRY_ID_RE.match(entry_id) or len(entry_id) > ENTRY_ID_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"invalid entry id: {entry_id!r}; must match "
                   f"{ENTRY_ID_PATTERN}",
        )

    tid = _ensure_tenant(user)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, source FROM catalog_entries "
                "WHERE id = $1 AND tenant_id = $2 "
                "  AND source IN ('operator','override') "
                "  AND hidden = FALSE "
                "ORDER BY CASE source "
                "  WHEN 'override' THEN 0 ELSE 1 END "
                "LIMIT 1",
                entry_id, tid,
            )
            if existing is not None:
                # Soft-delete via partial UNIQUE — the index has
                # ``WHERE hidden = FALSE``, so flipping the flag
                # frees the slot for a future re-create.
                await conn.execute(
                    "UPDATE catalog_entries "
                    "SET hidden = TRUE, updated_at = now() "
                    "WHERE id = $1 AND source = $2 AND tenant_id = $3",
                    existing["id"], existing["source"], tid,
                )
            else:
                # Need a shipped base to tombstone — else 404.
                base = await conn.fetchval(
                    "SELECT 1 FROM catalog_entries "
                    "WHERE id = $1 AND source = 'shipped' "
                    "  AND hidden = FALSE",
                    entry_id,
                )
                if not base:
                    raise HTTPException(
                        status_code=404,
                        detail="catalog entry not found",
                    )
                await conn.execute(
                    "INSERT INTO catalog_entries "
                    "  (id, source, tenant_id, hidden) "
                    "VALUES ($1, 'override', $2, TRUE)",
                    entry_id, tid,
                )

    _emit_audit_safely(
        action="catalog_entry_deleted",
        entity_id=entry_id,
        actor=user.email,
        before=None,
        after={"id": entry_id, "tenant_id": tid},
    )
    return JSONResponse(
        status_code=200,
        content={"status": "deleted", "id": entry_id, "tenant_id": tid},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  /catalog/sources — catalog_subscriptions CRUD (admin only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _row_to_subscription(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "feed_url": row["feed_url"],
        "auth_method": row["auth_method"],
        "auth_secret_ref": row["auth_secret_ref"],
        "refresh_interval_s": int(row["refresh_interval_s"]),
        "last_synced_at": _ts_to_iso(row["last_synced_at"]),
        "last_sync_status": row["last_sync_status"],
        "enabled": bool(row["enabled"]),
        "created_at": _ts_to_iso(row["created_at"]),
        "updated_at": _ts_to_iso(row["updated_at"]),
    }


@router.get("/sources")
async def list_sources(
    enabled_only: bool = Query(default=False),
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """List the caller's tenant's catalog subscriptions."""
    tid = _ensure_tenant(user)
    where = ["tenant_id = $1"]
    params: list[Any] = [tid]
    if enabled_only:
        where.append("enabled = TRUE")
    sql = (
        "SELECT id, tenant_id, feed_url, auth_method, auth_secret_ref, "
        "       refresh_interval_s, last_synced_at, last_sync_status, "
        "       enabled, created_at, updated_at "
        "FROM catalog_subscriptions "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at ASC, id ASC"
    )
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return JSONResponse(
        status_code=200,
        content={
            "items": [_row_to_subscription(r) for r in rows],
            "count": len(rows),
        },
    )


@router.post("/sources", status_code=201)
async def create_source(
    body: SubscriptionCreate,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Add a new catalog feed subscription for the caller's tenant.

    409 if the same ``(tenant_id, feed_url)`` already exists per the
    UNIQUE constraint in alembic 0051.
    """
    tid = _ensure_tenant(user)
    sub_id = _new_subscription_id()
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        try:
            row = await conn.fetchrow(
                "INSERT INTO catalog_subscriptions "
                "  (id, tenant_id, feed_url, auth_method, auth_secret_ref, "
                "   refresh_interval_s, enabled) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "RETURNING id, tenant_id, feed_url, auth_method, "
                "          auth_secret_ref, refresh_interval_s, "
                "          last_synced_at, last_sync_status, "
                "          enabled, created_at, updated_at",
                sub_id, tid, body.feed_url, body.auth_method,
                body.auth_secret_ref, body.refresh_interval_s, body.enabled,
            )
        except Exception as exc:
            msg = str(exc)
            if "catalog_subscriptions_tenant_id_feed_url_key" in msg or "duplicate key" in msg:
                raise HTTPException(
                    status_code=409,
                    detail=f"subscription for feed_url={body.feed_url!r} "
                           "already exists in this tenant",
                ) from exc
            if "violates check constraint" in msg or "check constraint" in msg:
                raise HTTPException(status_code=422, detail=msg) from exc
            raise

    _emit_audit_safely(
        action="catalog_source_created",
        entity_id=sub_id,
        actor=user.email,
        before=None,
        after={"id": sub_id, "feed_url": body.feed_url},
    )
    return JSONResponse(status_code=201, content=_row_to_subscription(row))


@router.patch("/sources/{sub_id}")
async def patch_source(
    sub_id: str,
    body: SubscriptionPatch,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Update a subscription. 404 if no such row in caller's tenant."""
    if not body.has_any_field():
        raise HTTPException(
            status_code=422,
            detail="empty PATCH body — supply at least one field",
        )
    tid = _ensure_tenant(user)
    fields = body.model_dump(exclude_unset=True)

    set_parts: list[str] = []
    params: list[Any] = []
    for col, val in fields.items():
        params.append(val)
        set_parts.append(f"{col} = ${len(params)}")
    set_parts.append("updated_at = now()")
    params.append(sub_id)
    params.append(tid)
    sql = (
        "UPDATE catalog_subscriptions "
        f"SET {', '.join(set_parts)} "
        f"WHERE id = ${len(params) - 1} AND tenant_id = ${len(params)} "
        "RETURNING id, tenant_id, feed_url, auth_method, auth_secret_ref, "
        "          refresh_interval_s, last_synced_at, last_sync_status, "
        "          enabled, created_at, updated_at"
    )

    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        try:
            row = await conn.fetchrow(sql, *params)
        except Exception as exc:
            msg = str(exc)
            if "catalog_subscriptions_tenant_id_feed_url_key" in msg or "duplicate key" in msg:
                raise HTTPException(
                    status_code=409,
                    detail="subscription with that feed_url already exists "
                           "in this tenant",
                ) from exc
            if "violates check constraint" in msg or "check constraint" in msg:
                raise HTTPException(status_code=422, detail=msg) from exc
            raise

    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    _emit_audit_safely(
        action="catalog_source_patched",
        entity_id=sub_id,
        actor=user.email,
        before=None,
        after={"id": sub_id, "fields": list(fields.keys())},
    )
    return JSONResponse(status_code=200, content=_row_to_subscription(row))


@router.delete("/sources/{sub_id}")
async def delete_source(
    sub_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Delete a catalog subscription. Hard-delete (no tombstone) — the
    feed sync workflow has no audit dependency on the row beyond the
    audit_log entry below."""
    tid = _ensure_tenant(user)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM catalog_subscriptions "
            "WHERE id = $1 AND tenant_id = $2 RETURNING id",
            sub_id, tid,
        )
    if deleted is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    _emit_audit_safely(
        action="catalog_source_deleted",
        entity_id=sub_id,
        actor=user.email,
        before=None,
        after={"id": sub_id, "tenant_id": tid},
    )
    return JSONResponse(
        status_code=200,
        content={"status": "deleted", "id": sub_id, "tenant_id": tid},
    )


SOURCE_SYNC_STATUS_PENDING_MANUAL = "pending_manual"


@router.post("/sources/{sub_id}/sync")
async def sync_source(
    sub_id: str,
    request: Request,
    user: _au.User = Depends(_au.require_admin),
) -> JSONResponse:
    """Request an immediate catalog feed refresh for a single subscription.

    BS.8.5 — "Sync now" button on the Sources tab. Stamps the row so the
    feed-sync cron worker (separate row) picks it up on the next tick.
    Pure SQL UPDATE — no synchronous feed fetch in the request path.

    * ``last_sync_status`` ← ``"pending_manual"``
    * ``last_synced_at`` ← NULL  (jumps row to front of
      ``idx_catalog_subscriptions_due`` ``NULLS FIRST`` queue)
    * ``updated_at`` ← ``now()``

    Tenant-scoped: 404 if ``sub_id`` does not belong to the caller's
    tenant. Subscribing / unsubscribing is already admin-only and
    audit-logged; a manual refresh of an already-subscribed source is
    a stamp + cron-priority bump, so no PEP HOLD here.

    Module-global state audit: stateless SQL through ``db_pool.get_pool()``;
    multi-worker safe via PG MVCC. Read-after-write: single UPDATE …
    RETURNING in one tx; the response carries the post-commit row.
    """
    tid = _ensure_tenant(user)
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE catalog_subscriptions "
            "SET last_sync_status = $1, "
            "    last_synced_at = NULL, "
            "    updated_at = now() "
            "WHERE id = $2 AND tenant_id = $3 "
            "RETURNING id, tenant_id, feed_url, auth_method, auth_secret_ref, "
            "          refresh_interval_s, last_synced_at, last_sync_status, "
            "          enabled, created_at, updated_at",
            SOURCE_SYNC_STATUS_PENDING_MANUAL, sub_id, tid,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    _emit_audit_safely(
        action="catalog_source_sync_requested",
        entity_id=sub_id,
        actor=user.email,
        before=None,
        after={"id": sub_id, "tenant_id": tid, "status": SOURCE_SYNC_STATUS_PENDING_MANUAL},
    )
    return JSONResponse(status_code=200, content=_row_to_subscription(row))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _emit_audit_safely(
    *, action: str, entity_id: str, actor: str,
    before: dict | None, after: dict | None,
) -> None:
    """Best-effort fire-and-forget audit emit.

    ``audit.log`` already swallows its own failures; this wrapper sits
    in front of the await so a missing event loop / shutdown-time call
    doesn't trip the request handler. Mirrors the pattern in
    admin_tenants.py — every catalog mutation should generate exactly
    one audit row, so we centralise it here.
    """
    try:
        import asyncio
        from backend import audit as _audit
        loop = asyncio.get_running_loop()
        loop.create_task(
            _audit.log(
                action=action,
                entity_kind="catalog_entry"
                    if action.startswith("catalog_entry_")
                    else "catalog_subscription",
                entity_id=entity_id,
                before=before,
                after=after,
                actor=actor,
            )
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning("%s audit emit failed: %s", action, exc)

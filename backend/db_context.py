"""I2 — Tenant-scoped database context using Python contextvars.

Provides request-scoped tenant isolation for the raw aiosqlite layer.
The ``require_tenant`` FastAPI dependency sets the current tenant from
the authenticated user; all db helper functions read it to auto-inject
WHERE / INSERT filters.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_tenant_var: ContextVar[Optional[str]] = ContextVar("current_tenant_id", default=None)


def current_tenant_id() -> Optional[str]:
    return _tenant_var.get()


def set_tenant_id(tid: str | None) -> None:
    _tenant_var.set(tid)


def require_current_tenant() -> str:
    tid = _tenant_var.get()
    if tid is None:
        raise RuntimeError("No tenant_id set in request context")
    return tid


def tenant_where(
    conditions: list[str],
    params: list,
    *,
    table_alias: str = "",
) -> None:
    """Append a tenant_id filter to *conditions* / *params* in-place.

    Only appends when a tenant is active in the current context.
    Uses ``?`` placeholder — the aiosqlite-compatible form used by
    db.py functions that are still on the compat wrapper. For PG-native
    (``$N``) filtering see :func:`tenant_where_pg`.
    """
    tid = _tenant_var.get()
    if tid is None:
        return
    col = f"{table_alias}.tenant_id" if table_alias else "tenant_id"
    conditions.append(f"{col} = ?")
    params.append(tid)


def tenant_where_pg(
    conditions: list[str],
    params: list,
    *,
    table_alias: str = "",
) -> None:
    """PG-native sibling of :func:`tenant_where`.

    Appends a ``tenant_id = $N`` filter to *conditions* using the NEXT
    positional placeholder index (``len(params) + 1``), and the current
    tenant id to *params*. Callers are responsible for passing
    *params* (with the new tenant value appended) to
    ``asyncpg.Connection.fetch`` / ``execute`` / etc.

    Phase-3-Runtime-v2 SP-3.9 (2026-04-20): promoted from the inline
    ``_append_tenant_pg`` helper introduced in SP-3.6a (artifacts)
    now that a second tenant-scoped domain (debug_findings) needs it.
    Further tenant-scoped ports (events, decision_rules) will reuse
    this helper.

    The no-tenant-set fallthrough is intentional: administrative /
    cross-tenant reads (e.g. ``list_all_users``) don't set the
    contextvar and get an unfiltered result set. Production routers
    set the contextvar via the ``require_tenant`` dependency BEFORE
    any DB call, so handler code cannot accidentally fall through to
    the unfiltered path.
    """
    tid = _tenant_var.get()
    if tid is None:
        return
    col = f"{table_alias}.tenant_id" if table_alias else "tenant_id"
    conditions.append(f"{col} = ${len(params) + 1}")
    params.append(tid)


def tenant_insert_value() -> str:
    """Return the current tenant_id for INSERT, or the default."""
    return _tenant_var.get() or "t-default"

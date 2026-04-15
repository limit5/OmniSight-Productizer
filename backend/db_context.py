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
    """
    tid = _tenant_var.get()
    if tid is None:
        return
    col = f"{table_alias}.tenant_id" if table_alias else "tenant_id"
    conditions.append(f"{col} = ?")
    params.append(tid)


def tenant_insert_value() -> str:
    """Return the current tenant_id for INSERT, or the default."""
    return _tenant_var.get() or "t-default"

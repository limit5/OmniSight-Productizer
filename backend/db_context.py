"""I2 — Tenant-scoped database context using Python contextvars.

Provides request-scoped tenant isolation for the raw aiosqlite layer.
The ``require_tenant`` FastAPI dependency sets the current tenant from
the authenticated user; all db helper functions read it to auto-inject
WHERE / INSERT filters.

Y5 (#281) extended the original single ``current_tenant_id`` ContextVar
into a triple ``(tenant_id, project_id, user_role)`` so the SQLAlchemy
event listener can auto-inject ``WHERE tenant_id = :t AND (project_id =
:p OR project_id IS NULL)`` on every SELECT, and ``require_project_member``
can short-circuit RBAC checks against the current user role without
re-fetching ``user_tenant_memberships`` / ``project_members``.

Why three separate ContextVars (and not one tuple):

* They're written at *different times* in the request lifecycle —
  ``tenant_id`` from the bearer token at auth-time, ``project_id`` from
  the URL path at routing-time (after ``require_project_member`` runs),
  ``user_role`` from the membership lookup that the same dependency
  performs.  A single tuple would force callers to read-modify-write
  three slots even when only one slice changes, which loses the
  atomicity benefit of ContextVar.
* Each piece can legitimately be ``None``:
  - tenant_id None  → admin / cross-tenant read (e.g. ``list_all_users``)
  - project_id None → tenant-wide route or pre-Y5 legacy data
  - user_role  None → before the membership lookup runs, or for
    machine-issued requests (api-key / cron) that don't map to a user

Allowed role tokens (kept here as a single source of truth so the
listener / dependency / tests don't drift):

* Tenant-level (``user_tenant_memberships.role``):
  ``owner / admin / member / viewer``
* Project-level (``project_members.role``):
  ``owner / contributor / viewer``
* Synthetic super-admin role: ``super_admin`` — set by the
  ``require_super_admin`` dependency for users in the ``super_admins``
  table.  Bypasses tenant + project isolation when paired with the
  ``X-Admin-Cross-Tenant: 1`` / ``X-Admin-Cross-Project: 1`` headers.
  Listener treats this as "no filter" but emits an audit row.

Module-global state audit (per implement_phase_step.md Step 1):
the three ContextVars are per-asyncio-Task state, NOT module-global
shared state — each request runs in its own Task copy, and the values
do not leak across workers.  This satisfies "case 3 — intentionally
per-worker independent" because there is *no* cross-request state to
share.  The ``set_*`` helpers MUST be called from the request scope
(FastAPI dependency) and never from a startup hook, otherwise the
value would survive on the worker's main task and bleed into the next
request that lacks an explicit set.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_tenant_var: ContextVar[Optional[str]] = ContextVar("current_tenant_id", default=None)
_project_var: ContextVar[Optional[str]] = ContextVar("current_project_id", default=None)
_user_role_var: ContextVar[Optional[str]] = ContextVar("current_user_role", default=None)


def current_tenant_id() -> Optional[str]:
    return _tenant_var.get()


def set_tenant_id(tid: str | None) -> None:
    _tenant_var.set(tid)


def require_current_tenant() -> str:
    tid = _tenant_var.get()
    if tid is None:
        raise RuntimeError("No tenant_id set in request context")
    return tid


def current_project_id() -> Optional[str]:
    """Return the project_id for the current request scope, or ``None``.

    ``None`` means the caller is on a tenant-wide route (no
    ``{project_id}`` in the URL), is a super-admin reading across
    projects, or is operating on legacy rows that pre-date Y4 project
    backfill.  Routes that require a project use ``require_project_member``
    which guarantees this returns a string.
    """
    return _project_var.get()


def set_project_id(pid: str | None) -> None:
    _project_var.set(pid)


def require_current_project() -> str:
    pid = _project_var.get()
    if pid is None:
        raise RuntimeError("No project_id set in request context")
    return pid


def current_user_role() -> Optional[str]:
    """Return the effective role for the current user in the current scope.

    Resolution order (set by the auth / membership dependencies):

    1. ``super_admin`` if the user is in the ``super_admins`` table.
    2. The project-level role from ``project_members`` for the current
       ``(user_id, project_id)`` if a row exists.
    3. The tenant-level role from ``user_tenant_memberships`` for the
       current ``(user_id, tenant_id)``, which acts as the default role
       on every project of that tenant when no explicit
       ``project_members`` row exists (per alembic 0034 docstring).
    4. ``None`` for anonymous / machine-issued requests, or before the
       membership lookup has run.
    """
    return _user_role_var.get()


def set_user_role(role: str | None) -> None:
    _user_role_var.set(role)


def require_current_user_role() -> str:
    role = _user_role_var.get()
    if role is None:
        raise RuntimeError("No user_role set in request context")
    return role


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

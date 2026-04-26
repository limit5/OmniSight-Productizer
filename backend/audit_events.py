"""Y9 #285 row 1 — canonical audit event types for tenant / project /
membership / invite / workspace-GC operations.

Why this module exists
──────────────────────
Pre-Y9 the Y series routers each invented their own audit ``action``
string (``tenant_created`` / ``tenant_invite_created`` /
``tenant_project_shared`` / ``workspace.gc_trashed`` etc.). The strings
were correct but ad-hoc — ten different surfaces, three different naming
conventions (``snake_case`` / ``snake.case`` / ``snake_case_verb``),
and no single place to look up "what's the canonical event for X".

Y9 row 1 freezes ten event names as a contract — anything that observes
the audit stream (T-series billing aggregator, I8 chain verifier, the
``/admin/audit/tenants/{tid}`` query surface) keys on these strings.
The names use ``domain.verb`` (dot-separated) which is the same shape
the workspace-GC events already used.

The ten event types
───────────────────
* ``tenant.created`` — a new tenant row landed.
* ``tenant.plan_changed`` — the tenant's billing plan changed.
* ``tenant.disabled`` — the tenant flipped enabled→disabled
  (re-enabling does NOT fire this event; that's covered by the existing
  ``tenant_updated`` row from the same PATCH).
* ``invite.sent`` — admin issued a membership invite.
* ``invite.accepted`` — recipient consumed an invite.
* ``membership.role_changed`` — a tenant member's role changed.
* ``project.created`` — a new project landed under a tenant.
* ``project.archived`` — a project flipped to soft-archived.
* ``project_share.granted`` — a project shared cross-tenant. Two rows
  are written: one in the host tenant chain, one in the guest tenant
  chain (so each tenant's audit log has a complete picture of cross-
  trust-boundary access without having to reach into the other chain).
* ``workspace.gc_executed`` — the periodic workspace GC sweep
  completed. Aggregate-level event; the existing per-leaf
  ``workspace.gc_trashed`` / ``workspace.gc_purged`` /
  ``workspace.gc_quota_evicted`` rows continue to fire alongside.

Tenant-context routing
──────────────────────
``audit.log`` reads ``tenant_id`` from the current ContextVar
(``backend.db_context.current_tenant_id``) and falls back to
``t-default``. That contract is the right one for nine of the ten
events: callers run inside the request handler whose ``require_tenant``
dependency has already pinned the context to the acting tenant. The
exception is ``project_share.granted`` — the action straddles two
tenants by design. ``emit_project_share_granted`` swap-and-restores
the contextvar to write one row per chain (host first, guest second).

The dual-write pattern uses ``set_tenant_id(...)`` rather than
constructing a synthetic conn / passing tenant_id explicitly because
``audit.log`` is the authoritative entry point — duplicating its
chain-append logic in this module would create a second source of
truth for hash chaining and risk drift. Saving and restoring the
contextvar keeps the module a thin wrapper.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
The ten ``EVENT_*`` constants are immutable strings — every uvicorn
worker derives the same value from this source file (audit answer #1).
There are no in-memory caches, locks, or counters. The
``set_tenant_id``/restore pattern in :func:`emit_project_share_granted`
flips the per-asyncio-Task ContextVar, never module state — the value
is restored before the function returns even on exception, so a single
request that calls into this helper cannot leak an altered tenant
context to a sibling request handler.

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
N/A — the helpers fan out to ``audit.log`` which holds a
``pg_advisory_xact_lock`` per tenant chain across its INSERT, so
two concurrent emitters serialise inside PG. The dual-write in
:func:`emit_project_share_granted` calls ``audit.log`` twice in
sequence, each in its own pool connection / transaction — the
host row commits before the guest row begins, so the chains are
appended one at a time per tenant.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend import audit
from backend.db_context import current_tenant_id, set_tenant_id

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event type constants — single source of truth
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EVENT_TENANT_CREATED = "tenant.created"
EVENT_TENANT_PLAN_CHANGED = "tenant.plan_changed"
EVENT_TENANT_DISABLED = "tenant.disabled"
EVENT_INVITE_SENT = "invite.sent"
EVENT_INVITE_ACCEPTED = "invite.accepted"
EVENT_MEMBERSHIP_ROLE_CHANGED = "membership.role_changed"
EVENT_PROJECT_CREATED = "project.created"
EVENT_PROJECT_ARCHIVED = "project.archived"
EVENT_PROJECT_SHARE_GRANTED = "project_share.granted"
EVENT_WORKSPACE_GC_EXECUTED = "workspace.gc_executed"


# Tuple of all canonical event types — used by tests / verifiers
# to assert "every name is wired" without having to hand-list ten
# constants in every assertion.
ALL_EVENT_TYPES: tuple[str, ...] = (
    EVENT_TENANT_CREATED,
    EVENT_TENANT_PLAN_CHANGED,
    EVENT_TENANT_DISABLED,
    EVENT_INVITE_SENT,
    EVENT_INVITE_ACCEPTED,
    EVENT_MEMBERSHIP_ROLE_CHANGED,
    EVENT_PROJECT_CREATED,
    EVENT_PROJECT_ARCHIVED,
    EVENT_PROJECT_SHARE_GRANTED,
    EVENT_WORKSPACE_GC_EXECUTED,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Single-tenant emitters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _emit_single_chain(
    *,
    action: str,
    entity_kind: str,
    entity_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: str,
    tenant_id_override: str | None = None,
) -> Optional[int]:
    """Write a single row to the chain identified by either the current
    context tenant or *tenant_id_override*.

    Restores the previous contextvar value on exit even on exception
    so one helper call cannot leak the override into an unrelated
    code path on the same task.
    """
    if tenant_id_override is None:
        return await audit.log(
            action=action,
            entity_kind=entity_kind,
            entity_id=entity_id,
            before=before,
            after=after,
            actor=actor,
        )
    saved = current_tenant_id()
    try:
        set_tenant_id(tenant_id_override)
        return await audit.log(
            action=action,
            entity_kind=entity_kind,
            entity_id=entity_id,
            before=before,
            after=after,
            actor=actor,
        )
    finally:
        set_tenant_id(saved)


async def emit_tenant_created(
    *, tenant_id: str, name: str, plan: str, enabled: bool, actor: str,
) -> Optional[int]:
    """Audit row for a freshly-minted tenant. The row goes into the
    ACTOR's chain (super-admin's ``t-default`` by default) — the new
    tenant has no prior chain to append to. The body carries the new
    tenant's id in ``after`` for query selectivity.
    """
    return await _emit_single_chain(
        action=EVENT_TENANT_CREATED,
        entity_kind="tenant",
        entity_id=tenant_id,
        before=None,
        after={
            "id": tenant_id,
            "name": name,
            "plan": plan,
            "enabled": bool(enabled),
        },
        actor=actor,
    )


async def emit_tenant_plan_changed(
    *,
    tenant_id: str,
    old_plan: str,
    new_plan: str,
    actor: str,
) -> Optional[int]:
    """Audit row when a tenant's billing plan changes. Goes into the
    target tenant's chain (the chain of the tenant whose plan changed)
    via *tenant_id_override* — the operator may be a super-admin
    operating from ``t-default``, but the event of record belongs to
    the affected tenant.
    """
    return await _emit_single_chain(
        action=EVENT_TENANT_PLAN_CHANGED,
        entity_kind="tenant",
        entity_id=tenant_id,
        before={"id": tenant_id, "plan": old_plan},
        after={"id": tenant_id, "plan": new_plan},
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_tenant_disabled(
    *, tenant_id: str, actor: str,
) -> Optional[int]:
    """Audit row when a tenant flips enabled→disabled. Goes into the
    target tenant's chain so the tenant's own audit pane carries the
    record of when it was suspended."""
    return await _emit_single_chain(
        action=EVENT_TENANT_DISABLED,
        entity_kind="tenant",
        entity_id=tenant_id,
        before={"id": tenant_id, "enabled": True},
        after={"id": tenant_id, "enabled": False},
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_invite_sent(
    *,
    tenant_id: str,
    invite_id: str,
    email: str,
    role: str,
    expires_at: str | None,
    invited_by: str | None,
    actor: str,
) -> Optional[int]:
    """Audit row when an invite is created. Goes into the *target
    tenant's* chain — the invite belongs to that tenant's recruitment
    activity, not the operator's home tenant."""
    return await _emit_single_chain(
        action=EVENT_INVITE_SENT,
        entity_kind="tenant_invite",
        entity_id=invite_id,
        before=None,
        after={
            "invite_id": invite_id,
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "expires_at": expires_at,
            "invited_by": invited_by,
        },
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_invite_accepted(
    *,
    tenant_id: str,
    invite_id: str,
    user_id: str,
    role: str,
    user_was_created: bool,
    already_member: bool,
    actor: str,
) -> Optional[int]:
    """Audit row when an invite is consumed. Goes into the target
    tenant's chain — the audit row records "user X joined tenant T".
    The accept handler is a public endpoint with no request-scoped
    tenant context; *tenant_id_override* sources the chain from the
    invite row itself."""
    return await _emit_single_chain(
        action=EVENT_INVITE_ACCEPTED,
        entity_kind="tenant_invite",
        entity_id=invite_id,
        before={"invite_id": invite_id, "status": "pending"},
        after={
            "invite_id": invite_id,
            "tenant_id": tenant_id,
            "status": "accepted",
            "user_id": user_id,
            "role": role,
            "user_was_created": bool(user_was_created),
            "already_member": bool(already_member),
        },
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_membership_role_changed(
    *,
    tenant_id: str,
    user_id: str,
    old_role: str,
    new_role: str,
    actor: str,
) -> Optional[int]:
    """Audit row when a tenant member's role changes. Goes into the
    target tenant's chain (the affected tenant's audit log)."""
    return await _emit_single_chain(
        action=EVENT_MEMBERSHIP_ROLE_CHANGED,
        entity_kind="tenant_membership",
        entity_id=user_id,
        before={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": old_role,
        },
        after={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": new_role,
        },
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_project_created(
    *,
    tenant_id: str,
    project_id: str,
    name: str,
    slug: str,
    product_line: str | None,
    actor: str,
) -> Optional[int]:
    """Audit row when a new project lands under a tenant. Goes into
    the owning tenant's chain — the project's birth is part of the
    tenant's history."""
    return await _emit_single_chain(
        action=EVENT_PROJECT_CREATED,
        entity_kind="project",
        entity_id=project_id,
        before=None,
        after={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "name": name,
            "slug": slug,
            "product_line": product_line,
        },
        actor=actor,
        tenant_id_override=tenant_id,
    )


async def emit_project_archived(
    *,
    tenant_id: str,
    project_id: str,
    archived_at: str,
    retention_days: int | None,
    actor: str,
) -> Optional[int]:
    """Audit row when a project flips to soft-archived. Goes into the
    owning tenant's chain."""
    return await _emit_single_chain(
        action=EVENT_PROJECT_ARCHIVED,
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
            "archived_at": archived_at,
            "retention_days": retention_days,
        },
        actor=actor,
        tenant_id_override=tenant_id,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Cross-tenant emitter — project_share.granted
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def emit_project_share_granted(
    *,
    host_tenant_id: str,
    guest_tenant_id: str,
    project_id: str,
    share_id: str,
    role: str,
    expires_at: str | None,
    granted_by: str | None,
    actor: str,
) -> tuple[Optional[int], Optional[int]]:
    """Audit row pair for a cross-tenant project share.

    Writes one row per tenant chain:
      * Host chain (``host_tenant_id``) — "we granted access to X".
      * Guest chain (``guest_tenant_id``) — "we received access from X".

    Returns ``(host_row_id, guest_row_id)``. Either id may be ``None``
    if ``audit.log`` swallows a transient chain-append failure
    (best-effort policy; failures are logged at warning).

    The two writes are sequential — host first, then guest. Each
    holds its own ``pg_advisory_xact_lock`` on its respective tenant
    chain, so concurrent shares between the same pair of tenants
    cannot interleave their host / guest rows incoherently.
    """
    payload = {
        "host_tenant_id": host_tenant_id,
        "guest_tenant_id": guest_tenant_id,
        "project_id": project_id,
        "share_id": share_id,
        "role": role,
        "expires_at": expires_at,
        "granted_by": granted_by,
    }
    host_after = {**payload, "chain_role": "host"}
    guest_after = {**payload, "chain_role": "guest"}

    host_id = await _emit_single_chain(
        action=EVENT_PROJECT_SHARE_GRANTED,
        entity_kind="project_share",
        entity_id=share_id,
        before=None,
        after=host_after,
        actor=actor,
        tenant_id_override=host_tenant_id,
    )
    guest_id = await _emit_single_chain(
        action=EVENT_PROJECT_SHARE_GRANTED,
        entity_kind="project_share",
        entity_id=share_id,
        before=None,
        after=guest_after,
        actor=actor,
        tenant_id_override=guest_tenant_id,
    )
    return host_id, guest_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Workspace GC summary emitter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def emit_workspace_gc_executed(
    *,
    summary: dict[str, Any],
    actor: str = "system:workspace-gc",
) -> Optional[int]:
    """Audit row for a completed workspace GC sweep.

    Aggregate-level event. The existing per-leaf
    ``workspace.gc_trashed`` / ``workspace.gc_purged`` /
    ``workspace.gc_quota_evicted`` rows continue to fire alongside —
    this row gives the operator a single point-in-time "how big was
    this sweep" record for billing rollup and ops dashboards without
    having to scan thousands of per-leaf rows.

    The sweep is a system action with no request context, so the row
    falls into the platform default (``t-default``) chain via the
    standard ``audit.log`` fallback.
    """
    return await _emit_single_chain(
        action=EVENT_WORKSPACE_GC_EXECUTED,
        entity_kind="workspace",
        entity_id="sweep",
        before=None,
        after={
            "trashed_count": len(summary.get("trashed", []) or []),
            "purged_count": len(summary.get("purged", []) or []),
            "quota_evicted_count": len(
                summary.get("quota_evicted", []) or []
            ),
            "skipped_busy_count": len(
                summary.get("skipped_busy", []) or []
            ),
            "skipped_fresh_count": len(
                summary.get("skipped_fresh", []) or []
            ),
        },
        actor=actor,
    )

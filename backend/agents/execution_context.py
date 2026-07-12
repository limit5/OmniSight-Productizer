"""U6-0 kernel identity substrate (dormant).

Frozen ``ExecutionContext`` + the central trusted factory that mints it.
Every U6-0 authorization decision keys on a SERVER-CONSTRUCTED principal —
never on model-supplied text. This module is additive and dormant: nothing
constructs or consumes it in this ticket. Later tickets wire it in
(T4b = chat path; T4c = runner + A2A paths); the kernel (T6) reads it.

The factory (not the caller) assigns ``principal_type`` and the trusted
``authorization_source``, so a caller cannot smuggle those in as
free/model-controlled strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from backend.auth import User

PrincipalType = Literal["human", "service", "machine", "unbound"]

# Single source of truth for "what is a bound/authorizing principal".
BOUND_PRINCIPAL_TYPES = frozenset({"human", "service", "machine"})
ALLOWED_PRINCIPAL_TYPES = BOUND_PRINCIPAL_TYPES | {"unbound"}


@dataclass(frozen=True)
class ExecutionContext:
    principal_type: PrincipalType
    tenant_id: str
    actor_id: str
    roles: tuple[str, ...]
    session_id: str | None
    request_id: str
    message_id: str | None
    authorization_source: str

    def __post_init__(self) -> None:
        # Literal is not runtime-enforced and the frozen dataclass is
        # publicly constructible — a malformed principal_type must never
        # silently reach a lenient verdict.
        if self.principal_type not in ALLOWED_PRINCIPAL_TYPES:
            raise ValueError(f"invalid principal_type: {self.principal_type!r}")


def for_human(
    *,
    user: User,
    tenant_id: str,
    session_id: str | None,
    request_id: str,
    message_id: str | None,
    authorization_source: str,
) -> ExecutionContext:
    # backend.auth.User carries a single ``role: str`` (no ``.scopes``); wrap in
    # a 1-tuple. tuple(user.role) would shatter the string into characters.
    return ExecutionContext(
        principal_type="human",
        tenant_id=tenant_id,
        actor_id=user.id,
        roles=(user.role,),
        session_id=session_id,
        request_id=request_id,
        message_id=message_id,
        authorization_source=authorization_source,
    )


def for_service(
    *,
    service_name: str,
    tenant_id: str,
    request_id: str,
    roles: Iterable[str],
    authorization_source: str,
) -> ExecutionContext:
    return ExecutionContext(
        principal_type="service",
        tenant_id=tenant_id,
        actor_id=service_name,
        roles=tuple(roles),
        session_id=None,
        request_id=request_id,
        message_id=None,
        authorization_source=authorization_source,
    )


def for_machine(
    *,
    service_name: str,
    request_id: str,
    tenant_id: str = "",
) -> ExecutionContext:
    # authorization_source is hard-assigned — the kernel treats machine
    # principals as NON-AUTHORIZING for protected actions, so an empty/unknown
    # identity can never become ambient authority.
    return ExecutionContext(
        principal_type="machine",
        tenant_id=tenant_id,
        actor_id=service_name,
        roles=(),
        session_id=None,
        request_id=request_id,
        message_id=None,
        authorization_source="internal_scheduler",
    )


def for_unbound() -> ExecutionContext:
    # The guard's missing-context fallback: the MOST restrictive principal.
    # Every field is hard-assigned (factory owns the trusted fields).
    # authorization_source="unbound" is the metric/audit label the later
    # guard (T7-0) emits so a missing context is OBSERVABLE — but the
    # kernel keys on principal_type (via is_unbound), never this string.
    return ExecutionContext(
        principal_type="unbound",
        tenant_id="",
        actor_id="unbound",
        roles=(),
        session_id=None,
        request_id="unbound",
        message_id=None,
        authorization_source="unbound",
    )


def for_child(
    parent: ExecutionContext,
    *,
    request_id: str | None = None,
) -> ExecutionContext:
    # A nested sub-agent delegation acts AS THE SAME principal: identity
    # fields are copied from the parent. message_id=None marks a fresh
    # model turn — it does NOT confer a distinct identity.
    return ExecutionContext(
        principal_type=parent.principal_type,
        tenant_id=parent.tenant_id,
        actor_id=parent.actor_id,
        roles=parent.roles,
        session_id=parent.session_id,
        request_id=request_id if request_id is not None else parent.request_id,
        message_id=None,
        authorization_source=parent.authorization_source,
    )


def is_unbound(ctx: ExecutionContext) -> bool:
    # Fail-closed: True for "unbound" AND any unexpected/malformed
    # principal_type (defense-in-depth beyond __post_init__). Single
    # definition of "unbound" for the kernel to import.
    return ctx.principal_type not in BOUND_PRINCIPAL_TYPES

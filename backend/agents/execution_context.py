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

PrincipalType = Literal["human", "service", "machine"]


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

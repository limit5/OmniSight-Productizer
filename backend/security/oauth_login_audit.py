"""FX2.D9.7.15 — OAuth login audit event family.

Canonical emit layer for the operator-facing ``oauth_login_*`` audit
rows used to troubleshoot OmniSight's self-login buttons.  This is
separate from the existing AS.1.4 forensic ``oauth.login_*`` family:
those rows preserve protocol-step detail, while these rows name the
user-visible outcome directly (initiated / success / known failure).

Module-global state audit (per implement_phase_step.md SOP Step 1):
Only immutable string / frozenset constants and frozen dataclasses live
at module scope.  No connection or cache is held here; every emitter
delegates to ``backend.audit.log``, whose per-tenant PG advisory lock
serialises audit-chain writes across uvicorn workers.

Read-after-write timing audit (per SOP Step 1): N/A.  This module only
appends best-effort audit rows and does not introduce a read path whose
result depends on the timing of the append.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from backend import audit


EVENT_OAUTH_LOGIN_INITIATED = "oauth_login_initiated"
EVENT_OAUTH_LOGIN_SUCCESS = "oauth_login_success"
EVENT_OAUTH_LOGIN_FAILED_PROVIDER_NOT_CONFIGURED = (
    "oauth_login_failed_provider_not_configured"
)
EVENT_OAUTH_LOGIN_FAILED_CALLBACK_INVALID = (
    "oauth_login_failed_callback_invalid"
)

ALL_OAUTH_LOGIN_EVENTS: tuple[str, ...] = (
    EVENT_OAUTH_LOGIN_INITIATED,
    EVENT_OAUTH_LOGIN_SUCCESS,
    EVENT_OAUTH_LOGIN_FAILED_PROVIDER_NOT_CONFIGURED,
    EVENT_OAUTH_LOGIN_FAILED_CALLBACK_INVALID,
)

ENTITY_KIND_OAUTH_LOGIN = "oauth_login"

OAUTH_LOGIN_FAILURE_REASONS: frozenset[str] = frozenset({
    "provider_not_configured",
    "callback_invalid",
})

FINGERPRINT_LENGTH = 12


def fingerprint(value: Optional[str]) -> Optional[str]:
    """Return a stable first-12-chars SHA-256 fingerprint of *value*."""
    if value is None or value == "":
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class OAuthLoginInitiatedContext:
    provider: str
    state: str
    redirect_uri: str
    scope: tuple[str, ...] = ()
    actor: str = "anonymous"


@dataclass(frozen=True)
class OAuthLoginSuccessContext:
    provider: str
    user_id: str
    state: str
    actor: Optional[str] = None


@dataclass(frozen=True)
class OAuthLoginFailureContext:
    provider: str
    reason: str
    state: str = ""
    detail: Optional[str] = None
    actor: str = "anonymous"


@dataclass(frozen=True)
class OAuthLoginAuditPayload:
    action: str
    entity_kind: str
    entity_id: str
    before: Optional[dict[str, Any]]
    after: dict[str, Any]
    actor: str


def _entity_id(provider: str, state: str = "") -> str:
    state_fp = fingerprint(state)
    return f"{provider}:{state_fp or 'unknown'}"


def build_initiated_payload(
    ctx: OAuthLoginInitiatedContext,
) -> OAuthLoginAuditPayload:
    after = {
        "provider": ctx.provider,
        "state_fp": fingerprint(ctx.state),
        "redirect_uri": ctx.redirect_uri,
        "scope": list(ctx.scope),
    }
    return OAuthLoginAuditPayload(
        action=EVENT_OAUTH_LOGIN_INITIATED,
        entity_kind=ENTITY_KIND_OAUTH_LOGIN,
        entity_id=_entity_id(ctx.provider, ctx.state),
        before=None,
        after=after,
        actor=ctx.actor,
    )


def build_success_payload(ctx: OAuthLoginSuccessContext) -> OAuthLoginAuditPayload:
    after = {
        "provider": ctx.provider,
        "user_id": ctx.user_id,
        "state_fp": fingerprint(ctx.state),
    }
    return OAuthLoginAuditPayload(
        action=EVENT_OAUTH_LOGIN_SUCCESS,
        entity_kind=ENTITY_KIND_OAUTH_LOGIN,
        entity_id=_entity_id(ctx.provider, ctx.state),
        before=None,
        after=after,
        actor=ctx.actor or ctx.user_id,
    )


def build_failure_payload(ctx: OAuthLoginFailureContext) -> OAuthLoginAuditPayload:
    if ctx.reason not in OAUTH_LOGIN_FAILURE_REASONS:
        raise ValueError(
            f"oauth_login failure reason {ctx.reason!r} not in "
            f"{sorted(OAUTH_LOGIN_FAILURE_REASONS)}"
        )
    after: dict[str, Any] = {
        "provider": ctx.provider,
        "reason": ctx.reason,
        "state_fp": fingerprint(ctx.state),
    }
    if ctx.detail:
        after["detail"] = str(ctx.detail)
    action = (
        EVENT_OAUTH_LOGIN_FAILED_PROVIDER_NOT_CONFIGURED
        if ctx.reason == "provider_not_configured"
        else EVENT_OAUTH_LOGIN_FAILED_CALLBACK_INVALID
    )
    return OAuthLoginAuditPayload(
        action=action,
        entity_kind=ENTITY_KIND_OAUTH_LOGIN,
        entity_id=_entity_id(ctx.provider, ctx.state),
        before=None,
        after=after,
        actor=ctx.actor,
    )


async def _route_to_audit_log(payload: OAuthLoginAuditPayload) -> Optional[int]:
    return await audit.log(
        action=payload.action,
        entity_kind=payload.entity_kind,
        entity_id=payload.entity_id,
        before=payload.before,
        after=payload.after,
        actor=payload.actor,
    )


async def emit_initiated(ctx: OAuthLoginInitiatedContext) -> Optional[int]:
    return await _route_to_audit_log(build_initiated_payload(ctx))


async def emit_success(ctx: OAuthLoginSuccessContext) -> Optional[int]:
    return await _route_to_audit_log(build_success_payload(ctx))


async def emit_failure(ctx: OAuthLoginFailureContext) -> Optional[int]:
    return await _route_to_audit_log(build_failure_payload(ctx))


__all__ = [
    "ALL_OAUTH_LOGIN_EVENTS",
    "ENTITY_KIND_OAUTH_LOGIN",
    "EVENT_OAUTH_LOGIN_FAILED_CALLBACK_INVALID",
    "EVENT_OAUTH_LOGIN_FAILED_PROVIDER_NOT_CONFIGURED",
    "EVENT_OAUTH_LOGIN_INITIATED",
    "EVENT_OAUTH_LOGIN_SUCCESS",
    "FINGERPRINT_LENGTH",
    "OAUTH_LOGIN_FAILURE_REASONS",
    "OAuthLoginAuditPayload",
    "OAuthLoginFailureContext",
    "OAuthLoginInitiatedContext",
    "OAuthLoginSuccessContext",
    "build_failure_payload",
    "build_initiated_payload",
    "build_success_payload",
    "emit_failure",
    "emit_initiated",
    "emit_success",
    "fingerprint",
]

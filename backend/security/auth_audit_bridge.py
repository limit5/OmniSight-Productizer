"""AS.6.5 — Bridge OmniSight self-handlers' legacy audit log to AS.5.1.

Single-purpose helper that wires OmniSight's existing handlers
(``/auth/login``, ``/auth/mfa/*``, ``oauth_refresh_hook``,
``/auth/change-password``) through the AS.5.1 ``auth.*`` rollup-event
family so the AS.5.2 per-tenant dashboard, the suspicious-pattern
detector, and any generated-app self-audit sink can count real
OmniSight login activity instead of seeing only the OAuth callback's
synthetic rows.

Why this is a thin bridge instead of a refactor
────────────────────────────────────────────────
Existing handlers emit legacy audit rows whose action strings
(``login_ok`` / ``auth.login.fail`` / ``mfa.challenge.passed`` /
``auth.lockout``) are documented contracts that downstream tools and
the I8 chain verifier already key on.  Replacing them with
``auth.login_success`` etc. would break:

  * The :mod:`backend.routers.audit` admin query surface (filters by
    legacy action strings).
  * The I8 hash-chain ``verify_chain`` order — chain rows are
    immutable; renaming an existing event in-place is not allowed.
  * Any external SIEM / log aggregator the operator wired to legacy
    action strings.

So this row's contract is **dual-emit, additive**: the legacy row
stays exactly where it was; this bridge fires one additional AS.5.1
rollup row alongside it.  The two rows coexist by design — the same
shape AS.6.1 already adopted in the OAuth callback (forensic
``oauth.login_callback`` + rollup ``auth.login_success``).

What this row ships
───────────────────
Helpers (each best-effort, swallow on failure, never raise):

  * :func:`emit_login_success_event` — wraps
    :func:`auth_event.emit_login_success` with FastAPI ``Request``-
    aware IP / user-agent extraction (CF-Connecting-IP precedence
    matches AS.6.1's :func:`_client_key`).
  * :func:`emit_login_fail_event` — wraps
    :func:`auth_event.emit_login_fail`; same request extraction.
  * :func:`emit_token_refresh_event` — wraps
    :func:`auth_event.emit_token_refresh` (AS.5.1 rollup sibling of
    AS.1.4 forensic ``oauth.refresh``).
  * :func:`emit_token_rotated_event` — wraps
    :func:`auth_event.emit_token_rotated` (AS.5.1 rollup sibling of
    AS.1.4 forensic ``oauth.token_rotated``).
  * :func:`schedule_login_success_event` /
    :func:`schedule_login_fail_event` — fire-and-forget variants for
    code paths that must not block the response (used by the OAuth
    callback in :mod:`backend.routers.auth`).
  * :func:`request_client_ip` / :func:`request_user_agent` — request
    extraction helpers, exported so callers that want a different
    audit shape (e.g. legacy ``audit.log`` rows) can reuse the same
    CF-aware extraction logic and stay byte-aligned with what the
    bridge writes.

A small :data:`MFA_METHOD_TO_AUTH_METHOD` ``MappingProxyType`` maps
the 3 OmniSight MFA-challenge method labels (``totp`` / ``backup_code``
/ ``webauthn``) onto the AS.5.1 :data:`auth_event.AUTH_METHODS`
vocabulary so the MFA challenge handlers don't reinvent the mapping
inline.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All public symbols are immutable: function defs + ``MappingProxyType``
  + ``frozenset``.  No module-level mutable container.  Answer #1 of
  SOP §1: every uvicorn worker derives the same routing decisions
  from the same source so cross-worker behaviour is deterministic-
  by-construction.
* No DB connections held at module level.  Audit emit is delegated
  to :mod:`backend.security.auth_event` which delegates to
  :func:`backend.audit.log` which holds a connection only inside its
  ``pg_advisory_xact_lock`` chain-append transaction.
* :func:`auth_event.is_enabled` is checked inside each
  :func:`emit_*` (single-knob AS.0.8 §3.1); the bridge does not
  re-check the knob (would double-cost env reads).
* No env reads at module top.  ``request_client_ip`` reads the
  ``cf-connecting-ip`` header per call — same lazy pattern as the
  AS.6.1 / AS.6.3 helpers.
* :mod:`backend.security.auth_event` is re-imported lazily inside
  each function body to avoid a top-level import cycle if some
  downstream module imports this bridge before the audit emit layer
  is fully constructed.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — bridge is pure routing.  Audit writes go through
``audit.log`` which serialises chain appends via
``pg_advisory_xact_lock(hashtext('audit-chain-' || tenant_id))``.
Two concurrent bridge calls in the same tenant cannot interleave
their chain rows; in different tenants run on independent advisory
locks (no cross-tenant contention).  Caller-side observers don't
read the audit row back same-request.

AS.0.8 single-knob behaviour
────────────────────────────
* Inherits ``settings.as_enabled`` gate from
  :func:`auth_event.is_enabled` — when knob-false, every emit
  short-circuits to ``None`` without writing a row.

Path deviation note (consistent with AS.6.1 / AS.6.2 / AS.6.3 /
AS.6.4 precedent)
─────────────────────────────────────────────────────────────────
Module lives at ``backend/security/auth_audit_bridge.py`` to align
with sibling AS submodules.  Canonical ``backend/auth/...`` blocked
by legacy ``backend/auth.py`` namespace — same precedent the entire
AS family already follows.
"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MFA method → AS.5.1 auth_method mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# OmniSight's MFA layer labels challenges as ``totp`` / ``backup_code``
# / ``webauthn`` (see ``backend.routers.mfa.mfa_challenge`` body field
# ``after.method``).  AS.5.1 :data:`auth_event.AUTH_METHODS` enumerates
# six values; backup_code is intentionally folded onto ``mfa_totp``
# because operationally a backup code is the totp-fallback path (same
# user secret, same enrollment workflow), and the dashboard rolls them
# up under one bucket.  ``webauthn`` maps to ``mfa_webauthn``; ``totp``
# maps to ``mfa_totp``.

MFA_METHOD_TOTP: str = "totp"
MFA_METHOD_BACKUP_CODE: str = "backup_code"
MFA_METHOD_WEBAUTHN: str = "webauthn"

SUPPORTED_MFA_METHODS: frozenset[str] = frozenset({
    MFA_METHOD_TOTP,
    MFA_METHOD_BACKUP_CODE,
    MFA_METHOD_WEBAUTHN,
})

# Read-only mapping; ``MappingProxyType`` so a bug elsewhere can't
# accidentally mutate the table at runtime.  Keys live here as the
# SoT; values come from :mod:`auth_event`.  The drift-guard test
# pins the value half against ``auth_event.AUTH_METHODS`` byte-for-
# byte so a typo cannot go undetected.
def _build_mfa_method_to_auth_method() -> Mapping[str, str]:
    from backend.security import auth_event as _ae

    return MappingProxyType({
        MFA_METHOD_TOTP: _ae.AUTH_METHOD_MFA_TOTP,
        MFA_METHOD_BACKUP_CODE: _ae.AUTH_METHOD_MFA_TOTP,
        MFA_METHOD_WEBAUTHN: _ae.AUTH_METHOD_MFA_WEBAUTHN,
    })


def mfa_method_to_auth_method(mfa_method: str) -> str:
    """Map an OmniSight MFA challenge method label onto an AS.5.1
    :data:`auth_event.AUTH_METHODS` value.

    Raises :class:`ValueError` on an unknown label so a typo in the
    challenge handler can't silently widen the dashboard auth-method
    distribution to a non-vocabulary string.
    """
    table = _build_mfa_method_to_auth_method()
    try:
        return table[mfa_method]
    except KeyError as exc:
        raise ValueError(
            f"unknown MFA method {mfa_method!r}; expected one of "
            f"{sorted(SUPPORTED_MFA_METHODS)}"
        ) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Request-extraction helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def request_client_ip(request: Optional[Any]) -> Optional[str]:
    """Return the real-client IP from a FastAPI / Starlette ``Request``.

    Honours ``cf-connecting-ip`` (Cloudflare Tunnel injects the
    upstream IP here when terminating TLS) so behind a tunnel the
    audit row reflects the real caller, not the tunnel egress.
    Falls back to ``request.client.host``.  Returns ``None`` when
    no request is supplied (background callers / cron / DSAR).
    """
    if request is None:
        return None
    try:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
    except Exception:  # noqa: BLE001 — Request shim variants
        cf = ""
    if cf:
        return cf
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return host or None


def request_user_agent(request: Optional[Any]) -> Optional[str]:
    """Return the User-Agent header from a FastAPI / Starlette
    ``Request``, or ``None`` when missing / no request supplied."""
    if request is None:
        return None
    try:
        ua = request.headers.get("user-agent")
    except Exception:  # noqa: BLE001 — Request shim variants
        ua = None
    return ua or None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async emitters — wrap auth_event.emit_* with safe-on-failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def emit_login_success_event(
    *,
    user_id: str,
    request: Optional[Any] = None,
    auth_method: Optional[str] = None,
    provider: Optional[str] = None,
    mfa_satisfied: bool = False,
    actor: Optional[str] = None,
) -> Optional[int]:
    """Fan one ``auth.login_success`` row out via AS.5.1.

    Best-effort: returns ``None`` on knob-off / transient audit
    failure / vocabulary error.  Never raises.  Logging at ``debug``
    so a misbehaving audit chain can't flood ``warning``.
    """
    from backend.security import auth_event as _ae

    method = auth_method or _ae.AUTH_METHOD_PASSWORD
    try:
        return await _ae.emit_login_success(_ae.LoginSuccessContext(
            user_id=user_id,
            auth_method=method,
            provider=provider,
            mfa_satisfied=bool(mfa_satisfied),
            ip=request_client_ip(request),
            user_agent=request_user_agent(request),
            actor=actor or user_id,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AS.6.5] auth.login_success emit failed: %s", exc)
        return None


async def emit_login_fail_event(
    *,
    attempted_user: str,
    fail_reason: str,
    request: Optional[Any] = None,
    auth_method: Optional[str] = None,
    provider: Optional[str] = None,
    actor: str = "anonymous",
) -> Optional[int]:
    """Fan one ``auth.login_fail`` row out via AS.5.1.

    The bridge accepts ``attempted_user`` raw; the AS.5.1 builder
    fingerprints it via 12-char SHA-256 (PII redaction) before the
    chain row is written.  Best-effort: never raises.
    """
    from backend.security import auth_event as _ae

    method = auth_method or _ae.AUTH_METHOD_PASSWORD
    try:
        return await _ae.emit_login_fail(_ae.LoginFailContext(
            attempted_user=attempted_user,
            auth_method=method,
            fail_reason=fail_reason,
            provider=provider,
            ip=request_client_ip(request),
            user_agent=request_user_agent(request),
            actor=actor,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AS.6.5] auth.login_fail emit failed: %s", exc)
        return None


async def emit_token_refresh_event(
    *,
    user_id: str,
    provider: str,
    outcome: str,
    new_expires_in_seconds: Optional[int] = None,
    actor: Optional[str] = None,
) -> Optional[int]:
    """Fan one ``auth.token_refresh`` row out via AS.5.1.

    Sibling of the AS.1.4 forensic ``oauth.refresh`` that
    :func:`oauth_refresh_hook._emit_refresh_audit` already emits;
    this row is the dashboard rollup.
    """
    from backend.security import auth_event as _ae

    try:
        return await _ae.emit_token_refresh(_ae.TokenRefreshContext(
            user_id=user_id,
            provider=provider,
            outcome=outcome,
            new_expires_in_seconds=new_expires_in_seconds,
            actor=actor or user_id,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AS.6.5] auth.token_refresh emit failed: %s", exc)
        return None


async def emit_token_rotated_event(
    *,
    user_id: str,
    provider: str,
    previous_refresh_token: str,
    new_refresh_token: str,
    triggered_by: str,
    actor: Optional[str] = None,
) -> Optional[int]:
    """Fan one ``auth.token_rotated`` row out via AS.5.1.

    Sibling of AS.1.4 forensic ``oauth.token_rotated`` already emitted
    by :func:`oauth_refresh_hook` after a successful rotation; this
    is the dashboard rollup.  Both refresh tokens are stored as 12-
    char SHA-256 fingerprints by the AS.5.1 builder — raw values
    never land in the chain.
    """
    from backend.security import auth_event as _ae

    try:
        return await _ae.emit_token_rotated(_ae.TokenRotatedContext(
            user_id=user_id,
            provider=provider,
            previous_refresh_token=previous_refresh_token,
            new_refresh_token=new_refresh_token,
            triggered_by=triggered_by,
            actor=actor or user_id,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AS.6.5] auth.token_rotated emit failed: %s", exc)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Fire-and-forget schedulers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Use these from code paths where the audit emit must NOT block the
# response (typically a 302 redirect like the OAuth callback).  The
# scheduler swallows scheduling errors (e.g. when called from a sync
# context with no running loop in pytest); the awaitable variants
# above are preferred when the caller is already async.


def _safe_schedule(coro_factory, *, label: str) -> None:
    """Schedule a coroutine without awaiting it; swallow failures.

    Mirrors :func:`backend.routers.auth._oauth_log_audit_safe` so all
    AS.6.x fan-out helpers share identical scheduling semantics.

    Probes for a *running* loop via :func:`asyncio.get_running_loop`
    before scheduling — bare :func:`asyncio.ensure_future` in Py 3.10+
    silently spawns a fresh loop in sync contexts and the task is
    then destroyed pending at interpreter exit (noisy
    ``coroutine was never awaited`` warning + leaked coroutine
    object).  This guard keeps fire-and-forget callers safe in
    sync test fixtures.
    """
    try:
        coro = coro_factory()
        if coro is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop — close the coroutine cleanly so
            # the interpreter doesn't surface a "never awaited" warning.
            coro.close()
            return
        loop.create_task(coro)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[AS.6.5] %s schedule failed: %s", label, exc)


def schedule_login_success_event(
    *,
    user_id: str,
    request: Optional[Any] = None,
    auth_method: Optional[str] = None,
    provider: Optional[str] = None,
    mfa_satisfied: bool = False,
    actor: Optional[str] = None,
) -> None:
    """Schedule an ``auth.login_success`` emit; do not block the
    response.  See :func:`emit_login_success_event` for arg semantics.
    """
    _safe_schedule(
        lambda: emit_login_success_event(
            user_id=user_id,
            request=request,
            auth_method=auth_method,
            provider=provider,
            mfa_satisfied=mfa_satisfied,
            actor=actor,
        ),
        label="login_success",
    )


def schedule_login_fail_event(
    *,
    attempted_user: str,
    fail_reason: str,
    request: Optional[Any] = None,
    auth_method: Optional[str] = None,
    provider: Optional[str] = None,
    actor: str = "anonymous",
) -> None:
    """Schedule an ``auth.login_fail`` emit; do not block the
    response.  See :func:`emit_login_fail_event` for arg semantics.
    """
    _safe_schedule(
        lambda: emit_login_fail_event(
            attempted_user=attempted_user,
            fail_reason=fail_reason,
            request=request,
            auth_method=auth_method,
            provider=provider,
            actor=actor,
        ),
        label="login_fail",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface — stable export list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    # MFA method vocabulary + dispatch
    "MFA_METHOD_TOTP",
    "MFA_METHOD_BACKUP_CODE",
    "MFA_METHOD_WEBAUTHN",
    "SUPPORTED_MFA_METHODS",
    "mfa_method_to_auth_method",
    # Request extraction helpers
    "request_client_ip",
    "request_user_agent",
    # Async emitters (safe-on-failure wrappers around auth_event.emit_*)
    "emit_login_success_event",
    "emit_login_fail_event",
    "emit_token_refresh_event",
    "emit_token_rotated_event",
    # Fire-and-forget schedulers
    "schedule_login_success_event",
    "schedule_login_fail_event",
]

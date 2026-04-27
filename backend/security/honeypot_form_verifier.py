"""AS.6.4 — OmniSight self-form honeypot backend verify wiring.

Universal helper that lets the four OmniSight self-forms (login /
signup / password-reset / contact) share one entry point onto
:func:`backend.security.honeypot.validate_honeypot` +
:func:`backend.security.honeypot.should_reject` + the AS.4.1
``bot_challenge.honeypot_*`` forensic audit-row emit.

Why a wrapper instead of inline ``honeypot.validate_honeypot(...)``
─────────────────────────────────────────────────────────────────
Mirrors the AS.6.3 :mod:`turnstile_form_verifier` rationale: the four
form callers must agree on (a) which axes count as a bypass (caller
authenticated by API key / matched the IP allowlist / sent a valid
test-token), (b) which form_path string the honeypot field name keys
on, (c) how the per-tenant ``honeypot_active`` flag is fetched, (d)
which audit-row sink the forensic event lands in, (e) the order of
honeypot vs. captcha enforcement (honeypot first — it's a pure-CPU
predicate so denying a confirmed bot at the cheapest layer keeps
the verify HTTP and DB lookups out of the bot's path entirely).

Per AS.0.5 §6 the four forms must emit audit rows that the AS.5.2
dashboard can natural-join to the captcha rows on ``form_path``;
:data:`SUPPORTED_FORM_PATHS` is byte-equal the honeypot
``_FORM_PREFIXES`` keys + the AS.6.3
:data:`turnstile_form_verifier.SUPPORTED_FORM_PATHS` set
(cross-module drift-guarded).

Plan / spec source
──────────────────
* ``docs/security/as_0_7_honeypot_field_design.md``
  — 4 form-path mapping (§4.1), 12-word rare pool (§2.1),
  bypass short-circuit precedence (§3.3), 3-event audit
  family (§3.4), 30-day rotation epoch grace (§2.1),
  AS.0.6 bypass interaction (§3.3), AS.0.8 single-knob
  noop (§4.3), drift guards (§8).
* ``docs/security/as_0_5_turnstile_fail_open_phased_strategy.md``
  §6 — 4-form caller invariant for the honeypot layer
  (parallel to the AS.6.3 captcha invariant).
* ``docs/security/as_0_6_automation_bypass_list.md``
  §2 / §4 — three bypass axes
  (api_key / ip_allowlist / test_token) short-circuit
  honeypot before any field check.
* ``docs/security/as_0_8_single_knob_rollback.md``
  §3.1 — knob-off path returns AS.4.1 ``OUTCOME_HONEYPOT_BYPASS``
  with ``bypass_kind="knob_off"`` so caller skips audit emit.

What this row ships (AS.6.4 scope, strict)
──────────────────────────────────────────
1. **Form action constants reuse** — :data:`FORM_ACTION_LOGIN` /
   ``SIGNUP`` / ``PASSWORD_RESET`` / ``CONTACT`` byte-equal the
   AS.6.3 :mod:`turnstile_form_verifier` action vocabulary.
2. **Form path constants reuse** — :data:`FORM_PATH_LOGIN` etc.
   byte-equal the AS.4.1 :data:`honeypot._FORM_PREFIXES` keys
   (cross-module drift-guarded).
3. **Default tenant for anonymous forms** —
   :data:`ANONYMOUS_TENANT_ID` (``"_anonymous"``) used as the
   honeypot field-name seed when the caller has no authenticated
   tenant context (login / signup / password-reset before the
   user is identified). The frontend (AS.7.x) computes the same
   field name from the same constant so the field round-trips.
4. **Pure helpers** —
   :func:`extract_bypass_kind_from_request` walks the AS.0.6 §4
   axes A → C → B in order and returns the first hit (or ``None``
   if no axis fires); :func:`form_path_for_action` dispatches on
   the action vocabulary.
5. **Async orchestrators** —
   :func:`verify_form_honeypot(form_action, *, request, ...)` runs
   :func:`honeypot.validate_honeypot` + audit fan-out (forensic
   ``bot_challenge.honeypot_*`` row via :func:`backend.audit.log`)
   and returns the :class:`honeypot.HoneypotResult`;
   :func:`verify_form_honeypot_or_reject(...)` wraps the above and
   raises :class:`honeypot.HoneypotRejected` on confirmed bot
   (field filled) or form drift so the caller's HTTP layer can
   serialise the canonical 429 ``bot_challenge_failed`` response —
   same surface as :class:`bot_challenge.BotChallengeRejected`.
6. **AS.0.8 single-knob hook** — :func:`is_enabled` re-exports
   :func:`honeypot.is_enabled` (one knob across the AS family);
   knob-off → orchestrator returns AS.4.1 bypass result with
   ``bypass_kind="knob_off"``, no audit emit.

Out of scope (deferred to follow-up rows)
─────────────────────────────────────────
* AS.6.5 — Routing the active-path honeypot rows into OmniSight's
  own dashboard (already enabled because we emit
  ``bot_challenge.honeypot_*`` via :func:`backend.audit.log`;
  AS.6.5 is the visualisation layer).
* AS.7.x React ``<HoneypotField>`` JSX component — the frontend
  side that actually renders the hidden input. The TS twin under
  ``templates/_shared/honeypot/`` exposes the field-name generator
  the React widget reuses byte-for-byte.
* Per-tenant ``auth_features.honeypot_active`` SQL fetch — the
  helper accepts the bool directly (defaults ``True``) so the
  caller owns the SQL ``SELECT auth_features ->> 'honeypot_active'
  FROM tenants WHERE id = $1`` (callers vary by route — login is
  pre-auth so no tenant_id known, change-password reads the
  authed user's tenant).
* New ``/auth/signup`` / ``/auth/contact`` public route handlers —
  those routes don't exist in OmniSight today; this helper pins
  the wiring contract so they can be wired in O(1) when those
  routes ship. The two routes that DO exist (``/auth/login`` +
  ``/auth/change-password``) are wired in this row by the auth
  router, not here.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All public symbols are immutable: 4 ``str`` form-action constants
  + 4 ``str`` form-path constants + 1 ``MappingProxyType`` action
  → path lookup + 1 ``frozenset`` for each of the action / path
  vocabularies + 1 ``str`` :data:`ANONYMOUS_TENANT_ID`. No
  module-level dict / list / set that two workers could disagree
  on.
* No DB connection / no env reads at module top. Every
  :func:`honeypot.is_enabled` / :func:`backend.audit.log` call is
  lazy at call time per worker (answer #1 of SOP §1: each uvicorn
  worker derives the same constant from the same source).
* :func:`honeypot.validate_honeypot` itself is pure (SHA-256 +
  ``MappingProxyType`` lookup + one constant-time string compare —
  no IO, no env reads). The forensic emit goes through
  ``backend.audit.log`` which holds a connection only inside the
  per-tenant ``pg_advisory_xact_lock`` chain-append transaction.
* Module-import is side-effect free — pure constants + function
  defs only.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — honeypot is a pure-stateless predicate over the submitted
form dict + the 5 module constants. No DB write owned by this
layer (the audit-row emit goes through the chain transaction
which is already row-locked); no shared in-memory state; no
read-after-write race.

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` is a thin re-export of
  :func:`honeypot.is_enabled` so the captcha + honeypot layers
  share one knob (operator can't accidentally disable one without
  the other).
* :func:`verify_form_honeypot` short-circuits with the AS.4.1
  ``OUTCOME_HONEYPOT_BYPASS`` result + ``bypass_kind="knob_off"``
  when knob-off — no forensic emit, no env read, no DB call.
"""

from __future__ import annotations

import logging
import os
import types
from typing import Any, Iterable, Mapping, Optional, TYPE_CHECKING

from backend.security import bot_challenge, honeypot

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Form action / form path vocabulary (byte-equal AS.6.3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Re-pinned (not imported from turnstile_form_verifier) so the two
# helpers stay decoupled — a future row that splits the captcha and
# honeypot vocabularies can add a new action here without touching
# the AS.6.3 module. The cross-module drift guard test
# ``test_form_action_constants_match_turnstile_helper`` keeps them
# byte-equal for now.
FORM_ACTION_LOGIN: str = "login"
FORM_ACTION_SIGNUP: str = "signup"
FORM_ACTION_PASSWORD_RESET: str = "pwreset"
FORM_ACTION_CONTACT: str = "contact"


SUPPORTED_FORM_ACTIONS: frozenset[str] = frozenset({
    FORM_ACTION_LOGIN,
    FORM_ACTION_SIGNUP,
    FORM_ACTION_PASSWORD_RESET,
    FORM_ACTION_CONTACT,
})


# Byte-equal :data:`backend.security.honeypot._FORM_PREFIXES` keys —
# locked by ``test_supported_form_paths_aligned_with_honeypot``.
FORM_PATH_LOGIN: str = "/api/v1/auth/login"
FORM_PATH_SIGNUP: str = "/api/v1/auth/signup"
FORM_PATH_PASSWORD_RESET: str = "/api/v1/auth/password-reset"
FORM_PATH_CONTACT: str = "/api/v1/auth/contact"


SUPPORTED_FORM_PATHS: frozenset[str] = frozenset({
    FORM_PATH_LOGIN,
    FORM_PATH_SIGNUP,
    FORM_PATH_PASSWORD_RESET,
    FORM_PATH_CONTACT,
})


_ACTION_TO_PATH: Mapping[str, str] = types.MappingProxyType({
    FORM_ACTION_LOGIN: FORM_PATH_LOGIN,
    FORM_ACTION_SIGNUP: FORM_PATH_SIGNUP,
    FORM_ACTION_PASSWORD_RESET: FORM_PATH_PASSWORD_RESET,
    FORM_ACTION_CONTACT: FORM_PATH_CONTACT,
})


def form_path_for_action(form_action: str) -> str:
    """Return the canonical form_path for *form_action*.

    Raises :class:`ValueError` on an unknown action. Pure lookup, no IO.
    """
    try:
        return _ACTION_TO_PATH[form_action]
    except KeyError as exc:
        raise ValueError(
            f"unknown form_action: {form_action!r} "
            f"(supported: {sorted(SUPPORTED_FORM_ACTIONS)})"
        ) from exc


# Sentinel tenant_id for anonymous forms (login / signup / password-
# reset before user identification). The honeypot field name is
# deterministic in (form_path, tenant_id, epoch); the React widget
# (AS.7.x) renders the field with the same constant so the form
# round-trips. Authenticated forms (change-password) pass the
# real ``user.tenant_id`` — that splits the field-name space per
# tenant for anti-fingerprint reasons (a bot can't memorise one
# tenant's field name and reuse it on another).
ANONYMOUS_TENANT_ID: str = "_anonymous"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Re-exports :func:`honeypot.is_enabled` so the captcha and
    honeypot helpers share one knob (operator can't accidentally
    disable one without the other).
    """
    return honeypot.is_enabled()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bypass extraction — walks AS.0.6 §4 axes A → C → B
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _extract_caller_kind(request: "Request") -> Optional[str]:
    """Return the AS.0.6 §2.1 caller_kind if the request was
    authenticated via an API key family the bot_challenge bypass
    list recognises.

    Mirrors :func:`turnstile_form_verifier._extract_caller_kind`.
    """
    if request is None:
        return None
    state = getattr(request, "state", None)
    if state is None:
        return None
    caller_kind = getattr(state, "caller_kind", None)
    if isinstance(caller_kind, str) and caller_kind:
        return caller_kind
    return None


def _extract_test_token(request: "Request") -> Optional[str]:
    """Read the AS.0.6 §2.3 test-token header value off the request."""
    if request is None:
        return None
    value = request.headers.get(bot_challenge.TEST_TOKEN_HEADER) or ""
    return value.strip() or None


def _expected_test_token() -> Optional[str]:
    """Read the operator-side expected test-token from env."""
    value = (os.environ.get("OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN") or "").strip()
    return value or None


def _extract_client_ip(request: "Request") -> Optional[str]:
    """Real-client IP per the existing auth router convention.

    Prefers the Cloudflare ``cf-connecting-ip`` header (CF Tunnel
    terminates TLS so the immediate peer is always the tunnel) over
    ``request.client.host``.
    """
    if request is None:
        return None
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    if request.client and request.client.host:
        return request.client.host
    return None


def _resolve_tenant_ip_allowlist(
    tenant_ip_allowlist: Optional[Iterable[str]],
) -> tuple[str, ...]:
    """Normalise the per-tenant CIDR allowlist into an immutable
    tuple, dropping corrupt entries (mirrors the AS.6.3 normaliser)."""
    if not tenant_ip_allowlist:
        return ()
    return tuple(
        entry for entry in tenant_ip_allowlist
        if isinstance(entry, str) and entry.strip()
    )


def extract_bypass_kind_from_request(
    request: "Request",
    *,
    form_path: str,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Walk the AS.0.6 §4 axis-internal precedence (api_key →
    test_token → ip_allowlist) and return the first matching bypass
    kind in :data:`honeypot.ALL_BYPASS_KINDS`, or ``None`` if no axis
    fires.

    Routes :class:`bot_challenge.BypassContext` through
    :func:`bot_challenge.evaluate_bypass` so the precedence + match
    semantics stay byte-identical to the AS.6.3 captcha layer; the
    returned outcome literal is mapped onto the honeypot bypass
    vocabulary (``apikey`` / ``test_token`` / ``ip_allowlist``).

    Pure helper — reads request headers + state attributes only, no
    DB calls.
    """
    bypass_ctx = bot_challenge.BypassContext(
        path=form_path,
        caller_kind=_extract_caller_kind(request),
        api_key_id=None,
        api_key_prefix=None,
        client_ip=_extract_client_ip(request),
        tenant_ip_allowlist=_resolve_tenant_ip_allowlist(tenant_ip_allowlist),
        test_token_header_value=_extract_test_token(request),
        test_token_expected=_expected_test_token(),
        tenant_id=None,
        widget_action=None,
    )
    reason = bot_challenge.evaluate_bypass(bypass_ctx)
    if reason is None:
        return None
    return _OUTCOME_TO_HONEYPOT_BYPASS_KIND.get(reason.outcome)


# AS.0.6 outcome → AS.4.1 honeypot bypass-kind vocabulary. Keys are
# the AS.3.1 ``OUTCOME_BYPASS_*`` literals (one per axis). Values are
# the :data:`honeypot.ALL_BYPASS_KINDS` sub-vocabulary the active
# honeypot path uses on the caller's HoneypotResult. Drift guard
# ``test_outcome_to_honeypot_bypass_kind_keys_in_bot_challenge`` +
# ``..._values_in_honeypot``.
_OUTCOME_TO_HONEYPOT_BYPASS_KIND: Mapping[str, str] = types.MappingProxyType({
    bot_challenge.OUTCOME_BYPASS_APIKEY: honeypot.BYPASS_KIND_API_KEY,
    bot_challenge.OUTCOME_BYPASS_TEST_TOKEN: honeypot.BYPASS_KIND_TEST_TOKEN,
    bot_challenge.OUTCOME_BYPASS_IP_ALLOWLIST: honeypot.BYPASS_KIND_IP_ALLOWLIST,
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Forensic audit fan-out (bot_challenge.honeypot_* family)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _emit_forensic_audit(
    result: honeypot.HoneypotResult,
    *,
    form_path: str,
    actor: str,
) -> None:
    """Fan out the AS.4.1 ``bot_challenge.honeypot_*`` forensic audit
    row when ``result.audit_event`` is set.

    Bypass paths intentionally have ``audit_event=None`` per
    :func:`event_for_honeypot_outcome` — the AS.0.6 ``bypass_*``
    forensic row is the caller's responsibility from its own bypass-
    detection layer.

    Swallow audit-emit failures so a temporarily-flaky audit chain
    doesn't break the verify path itself; the chain has its own retry
    policy at :mod:`backend.audit`. Mirrors the AS.6.3
    :func:`turnstile_form_verifier._emit_rollup_audit` swallow.
    """
    if result.audit_event is None:
        return
    try:
        from backend import audit as _audit  # local import — keep module-import side-effect free
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "AS.6.4: audit module import failed for form_path=%s outcome=%s: %s",
            form_path, result.outcome, exc,
        )
        return
    try:
        await _audit.log(
            action=result.audit_event,
            entity_kind="auth_session",
            entity_id=form_path,
            before=None,
            after={
                "outcome": result.outcome,
                "field_name_used": result.field_name_used,
                "failure_reason": result.failure_reason,
                **dict(result.audit_metadata or {}),
            },
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "AS.6.4: %s emit failed for form_path=%s actor=%s: %s",
            result.audit_event, form_path, actor, exc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  End-to-end orchestrators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def verify_form_honeypot(
    form_action: str,
    submitted: Mapping[str, Any],
    *,
    request: "Request",
    tenant_id: Optional[str] = None,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
    tenant_honeypot_active: bool = True,
    actor: str = "anonymous",
    bypass_kind: Optional[str] = None,
    now: Optional[float] = None,
) -> honeypot.HoneypotResult:
    """End-to-end verify orchestrator for one of the four OmniSight
    self-forms.

    Pipeline (mirrors AS.6.3 :func:`verify_form_token`):

      1. AS.0.8 single-knob short-circuit — knob-off → AS.4.1 returns
         ``OUTCOME_HONEYPOT_BYPASS`` with ``bypass_kind="knob_off"``.
         No forensic emit, no DB call. The caller treats the result
         as fail-open.
      2. Resolve effective :data:`tenant_id` — when the caller passes
         ``None`` (anonymous form), substitute :data:`ANONYMOUS_TENANT_ID`
         so the field name remains deterministic. Authenticated forms
         pass the real ``user.tenant_id`` so the field-name space
         splits per tenant (anti-fingerprint).
      3. Resolve the bypass kind from the request's AS.0.6 axes
         (caller_kind / test_token / ip_allowlist) when the caller
         didn't pass an explicit ``bypass_kind``. Caller can override
         (e.g. routes that already detected an axis hit upstream).
      4. Run :func:`honeypot.validate_honeypot` which is pure-
         deterministic — SHA-256(form_path:tenant_id:epoch) →
         field-name lookup; any non-empty value at that field name
         is a bot.
      5. Fan out the AS.4.1 ``bot_challenge.honeypot_*`` forensic
         audit row via :func:`backend.audit.log`. Bypass paths
         (knob_off / tenant_disabled / api_key / test_token / ip_allow)
         intentionally don't emit (caller's bypass layer owns those).
      6. Return the :class:`honeypot.HoneypotResult` for the caller
         to act on (``allow`` decides 4xx vs continue).

    The frontend (AS.7.x React ``<HoneypotField>``) computes the
    field name from the same (form_path, tenant_id, epoch) triple,
    so passing :data:`ANONYMOUS_TENANT_ID` here when the form is
    pre-auth lets the frontend render the right hidden input.

    Never raises on the happy / fail / bypass paths — the caller
    decides whether to surface the result to the user; use
    :func:`verify_form_honeypot_or_reject` for the canonical 429
    raise pattern.
    """
    if form_action not in SUPPORTED_FORM_ACTIONS:
        raise ValueError(
            f"unknown form_action: {form_action!r} "
            f"(supported: {sorted(SUPPORTED_FORM_ACTIONS)})"
        )

    form_path = form_path_for_action(form_action)

    if not is_enabled():
        # AS.0.8 noop — let the AS.4.1 module construct the canonical
        # bypass result so the shape is identical to a direct
        # ``honeypot.validate_honeypot`` call with knob-off (caller
        # treats both as fail-open).
        return honeypot.HoneypotResult(
            allow=True,
            outcome=honeypot.OUTCOME_HONEYPOT_BYPASS,
            audit_event=None,
            bypass_kind=honeypot.BYPASS_KIND_KNOB_OFF,
            field_name_used=None,
            failure_reason=None,
            audit_metadata=types.MappingProxyType({
                "form_path": form_path,
                "bypass_kind": honeypot.BYPASS_KIND_KNOB_OFF,
            }),
        )

    effective_tenant_id = tenant_id if tenant_id else ANONYMOUS_TENANT_ID

    if bypass_kind is None:
        bypass_kind = extract_bypass_kind_from_request(
            request,
            form_path=form_path,
            tenant_ip_allowlist=tenant_ip_allowlist,
        )

    result = honeypot.validate_honeypot(
        form_path,
        effective_tenant_id,
        submitted,
        bypass_kind=bypass_kind,
        tenant_honeypot_active=tenant_honeypot_active,
        now=now,
    )

    await _emit_forensic_audit(result, form_path=form_path, actor=actor)
    return result


async def verify_form_honeypot_or_reject(
    form_action: str,
    submitted: Mapping[str, Any],
    *,
    request: "Request",
    tenant_id: Optional[str] = None,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
    tenant_honeypot_active: bool = True,
    actor: str = "anonymous",
    bypass_kind: Optional[str] = None,
    now: Optional[float] = None,
) -> honeypot.HoneypotResult:
    """Same pipeline as :func:`verify_form_honeypot`, then if
    :func:`honeypot.should_reject` says yes (field filled or form
    drift), raise :class:`honeypot.HoneypotRejected` carrying the
    result so the caller's HTTP layer can serialise the canonical
    429 ``bot_challenge_failed`` response — same surface as
    :class:`bot_challenge.BotChallengeRejected` from AS.3.4 / AS.6.3.

    On allow=True (every pass / bypass / knob-off path), returns the
    result.

    Caller template (the four OmniSight self-form route handlers
    follow this shape verbatim per AS.0.5 §6.1)::

        from backend.security import honeypot as _hp
        from backend.security import honeypot_form_verifier as _hpv
        try:
            await _hpv.verify_form_honeypot_or_reject(
                _hpv.FORM_ACTION_LOGIN,
                submitted=request_body.model_dump(),
                request=request,
                actor=email_key or "anonymous",
            )
        except _hp.HoneypotRejected as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code},
            )
    """
    result = await verify_form_honeypot(
        form_action,
        submitted,
        request=request,
        tenant_id=tenant_id,
        tenant_ip_allowlist=tenant_ip_allowlist,
        tenant_honeypot_active=tenant_honeypot_active,
        actor=actor,
        bypass_kind=bypass_kind,
        now=now,
    )
    if honeypot.should_reject(result):
        raise honeypot.HoneypotRejected(result)
    return result


__all__ = [
    # Form action constants
    "FORM_ACTION_LOGIN",
    "FORM_ACTION_SIGNUP",
    "FORM_ACTION_PASSWORD_RESET",
    "FORM_ACTION_CONTACT",
    "SUPPORTED_FORM_ACTIONS",
    # Form path constants
    "FORM_PATH_LOGIN",
    "FORM_PATH_SIGNUP",
    "FORM_PATH_PASSWORD_RESET",
    "FORM_PATH_CONTACT",
    "SUPPORTED_FORM_PATHS",
    "form_path_for_action",
    # Anonymous tenant_id sentinel
    "ANONYMOUS_TENANT_ID",
    # AS.0.8 single-knob hook
    "is_enabled",
    # Pure helpers
    "extract_bypass_kind_from_request",
    # Async orchestrators
    "verify_form_honeypot",
    "verify_form_honeypot_or_reject",
]

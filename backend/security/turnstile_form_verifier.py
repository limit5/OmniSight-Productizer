"""AS.6.3 — OmniSight self-form Turnstile backend verify wiring.

Universal helper that lets the four OmniSight self-forms (login /
signup / password-reset / contact) share one entry point onto
:func:`backend.security.bot_challenge.verify_with_fallback` +
:func:`backend.security.bot_challenge.should_reject` + the AS.5.1
``auth.bot_challenge_pass`` / ``auth.bot_challenge_fail`` rollup
emitters.

Why a wrapper instead of inline ``await bot_challenge.verify(ctx)``
─────────────────────────────────────────────────────────────────
AS.0.5 §6.1 acceptance criteria for AS.6.3 ships a 4-form invariant:

    "AS.6.3 OmniSight self login/signup/password-reset/contact form
     全 wire 完成、grep 4 處 caller 確認都呼叫 ``bot_challenge.verify()``"

If each route did its own ``VerifyContext`` construction + bypass-
context build + audit fan-out, the 4 callers would drift on (a) which
provider env carries the secret, (b) which bypass axes are inspected,
(c) which form_path string the AS.5.1 rollup keys on, (d) which header
carries the test-token, and (e) the order of fail-open vs reject
enforcement. This helper pins the answer once so every form behaves
identically — and so AS.6.4 (honeypot) and AS.6.5 (audit-log
integration) can compose with the same shape.

Plan / spec source
──────────────────
* ``docs/security/as_0_5_turnstile_fail_open_phased_strategy.md``
  — Phase 1/2 fail-open + Phase 3 fail-closed contract (§2), audit
  metadata schema (§3 ``widget_action`` per form), bypass list
  precedence (§4), provider site-secret env wiring (§5), 4-form
  caller invariant (§6.1).
* ``docs/security/as_0_6_automation_bypass_list.md``
  — three bypass mechanisms with axis-internal precedence A → C → B
  → D (§4) consumed via :func:`bot_challenge.evaluate_bypass`.
* ``docs/security/as_0_8_single_knob_rollback.md``
  — ``OMNISIGHT_AS_ENABLED`` short-circuit at module entry, no audit
  on knob-off, no DB read.
* ``docs/security/as_0_7_honeypot_field_design.md`` §4.1
  — 4 form path constants reused byte-for-byte from
  :mod:`backend.security.honeypot._FORM_PREFIXES` so the AS.5.2
  dashboard can natural-join captcha + honeypot rollups on the
  same ``form_path``.

What this row ships (AS.6.3 scope, strict)
──────────────────────────────────────────
1. **Four canonical form action constants** + frozenset
   :data:`SUPPORTED_FORM_ACTIONS` — ``login`` / ``signup`` /
   ``pwreset`` / ``contact``. The audit metadata's ``widget_action``
   field carries this verbatim.
2. **Four canonical form path constants** + frozenset
   :data:`SUPPORTED_FORM_PATHS` — must be byte-equal the AS.4.1
   honeypot ``_FORM_PREFIXES`` keys (cross-module drift-guarded).
   The AS.5.1 rollup row's ``form_path`` field carries this verbatim.
3. **Token surface constants** —
   :data:`TURNSTILE_TOKEN_BODY_FIELD` (``"turnstile_token"``) +
   :data:`TURNSTILE_TOKEN_HEADER` (``"X-Turnstile-Token"``). Pinned
   here so the React widgets (AS.7.1 / AS.7.2 / AS.7.3) and the
   server route handlers reference the same name.
4. **Phase-knob hook** — :func:`current_phase` reads
   ``OMNISIGHT_BOT_CHALLENGE_PHASE`` env var (1/2/3) per AS.0.5 §2
   phase matrix. Default 1 (fail-open everywhere) so a fresh deploy
   never accidentally locks users out of login.
5. **Form-action → widget-action / form-path / fail-reason maps** —
   immutable ``MappingProxyType`` constants the helper consumes.
6. **Pure helpers** —
   :func:`extract_token_from_request` reads body field + header in
   that order; :func:`build_bypass_context` constructs the
   :class:`bot_challenge.BypassContext` from a FastAPI Request +
   form_path + tenant_id; :func:`resolve_provider_secret` reads the
   per-provider env via :func:`bot_challenge.secret_env_for`.
7. **Async orchestrators** —
   :func:`verify_form_token(form_action, token, *, request, ...)`
   runs verify_with_fallback + AS.5.1 audit fan-out (pass / fail
   rollup) and returns the :class:`bot_challenge.BotChallengeResult`;
   :func:`verify_form_token_or_reject(...)` wraps the above and
   raises :class:`bot_challenge.BotChallengeRejected` on Phase 3
   confirmed reject so the caller's HTTP layer can serialise the
   canonical 429 ``bot_challenge_failed`` response.
8. **AS.0.8 single-knob hook** — :func:`is_enabled` short-circuits
   to ``True`` passthrough when ``settings.as_enabled=False``; the
   helper returns the AS.3.1 :func:`bot_challenge.passthrough`
   result without writing any audit row (AS.0.8 §3.1 noop matrix).

Out of scope (deferred to follow-up rows)
─────────────────────────────────────────
* AS.6.4 — Per-form honeypot wiring.
  (the helper makes ``form_path`` available so the honeypot wiring
  can natural-join its rollup row to ours.)
* AS.6.5 — Routing the AS.5.1 rollup rows into OmniSight's own
  dashboard (already enabled because we emit via
  :func:`auth_event.emit_bot_challenge_pass` / fail; AS.6.5 is the
  visualisation layer.)
* Phase 2 → Phase 3 advance audit emit (AS.5.2 dashboard "Advance
  to Phase X" button owns the helper for that, per AS.0.5 §8.4).
* New ``/auth/signup`` / ``/auth/contact`` public route handlers —
  those routes don't exist in OmniSight today; this helper pins
  the wiring contract so they can be wired in O(1) when those
  routes ship. The two routes that DO exist (``/auth/login`` +
  ``/auth/change-password``) are wired in this row by the auth
  router, not here.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All public symbols are immutable: 4 ``str`` form-action constants
  + 4 ``str`` form-path constants + 2 token-surface ``str`` constants
  + 4 ``MappingProxyType`` lookup tables + 1 ``frozenset`` for each
  of the action / path vocabularies. No module-level dict / list /
  set that two workers could disagree on.
* No DB connections held at module level. No env reads at module
  top. Every env / settings read is lazy at call time per worker
  (answer #1 of SOP §1: each uvicorn worker derives the same
  constant from the same source).
* HTTP transport is delegated to the AS.3.1 layer — no httpx client
  pool here. The AS.5.1 emit layer routes through
  ``backend.audit.log`` which holds a connection only inside the
  per-tenant ``pg_advisory_xact_lock`` chain-append transaction.
* Module-import is side-effect free — pure constants + function
  defs only.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — verify is per-request, stateless RPC to the upstream provider's
siteverify endpoint. No DB write, no shared in-memory state, no
read-after-write race. The audit row fan-out happens *after* the
verify result is computed; downstream callers don't read it back in
the same request.

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
  fallback (defaults ``True`` if the field hasn't landed yet).
  Mirrors :func:`backend.security.bot_challenge.is_enabled`.
* :func:`verify_form_token` short-circuits with the AS.3.1
  :func:`bot_challenge.passthrough` result when knob-off — no
  AS.5.1 rollup row, no AS.3.1 verify HTTP, no env read for
  provider secrets. Caller treats the result as fail-open.
"""

from __future__ import annotations

import logging
import os
import types
from typing import Any, Iterable, Mapping, Optional, TYPE_CHECKING

from backend.security import auth_event, bot_challenge

if TYPE_CHECKING:
    import httpx
    from fastapi import Request


logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Form action vocabulary (widget_action label per AS.0.5 §3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# The four form actions the AS.0.5 §3 audit metadata schema pins as
# the canonical ``widget_action`` field values. The widget on the
# client side renders with the matching action label so the
# provider's ``action`` echo can be cross-checked by
# :func:`bot_challenge.verify_provider` (anti-replay across forms).
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Form path vocabulary (AS.5.1 form_path field per AS.4.1 §4.1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Byte-equal the keys of :data:`backend.security.honeypot._FORM_PREFIXES`
# so the AS.5.2 dashboard can natural-join captcha + honeypot rollup
# rows on the same ``form_path``. Drift-guarded by
# ``test_supported_form_paths_aligned_with_honeypot``.
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


# Form action → form path lookup. Frozen, no module-level mutable
# container. Inverse mapping not exposed because the form action is
# the SoT — multiple paths could theoretically share an action (e.g.
# ``/auth/admin/password-reset`` reuses the ``pwreset`` action) but
# the helper API takes the action explicitly.
_ACTION_TO_PATH: Mapping[str, str] = types.MappingProxyType({
    FORM_ACTION_LOGIN: FORM_PATH_LOGIN,
    FORM_ACTION_SIGNUP: FORM_PATH_SIGNUP,
    FORM_ACTION_PASSWORD_RESET: FORM_PATH_PASSWORD_RESET,
    FORM_ACTION_CONTACT: FORM_PATH_CONTACT,
})


def form_path_for_action(form_action: str) -> str:
    """Return the canonical form_path for *form_action*.

    Raises :class:`ValueError` on an unknown action. Pure lookup, no
    IO. Used by the helper internally and exposed for callers that
    need to log / fingerprint the form_path.
    """
    try:
        return _ACTION_TO_PATH[form_action]
    except KeyError as exc:
        raise ValueError(
            f"unknown form_action: {form_action!r} "
            f"(supported: {sorted(SUPPORTED_FORM_ACTIONS)})"
        ) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token surface constants (request body field + header)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Body field name carrying the widget-issued token. The React widget
# (AS.7.1 ``<TurnstileWidget />``) writes to this field on every
# OmniSight self-form so the server reads it from a single place
# regardless of which provider the widget rendered.
TURNSTILE_TOKEN_BODY_FIELD: str = "turnstile_token"


# Optional header carrying the widget-issued token. Some clients
# (CLI tools, automation scripts that *do* go through the captcha
# layer voluntarily) prefer to keep the JSON body free of widget
# state. Both surfaces are accepted; body wins on conflict so a
# misconfigured client can't sneak a stale header value past a
# fresh body submit.
TURNSTILE_TOKEN_HEADER: str = "X-Turnstile-Token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.5 §2 phase knob
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Default phase for first-deploy: AS.0.5 §2.2 says Phase 1 is fail-
# open everywhere. The advance to Phase 2/3 is driven by ops/admin
# action per AS.0.5 §6.2 / §6.3 — never by code change. Pinned at 1
# so a fresh deploy can't accidentally run Phase 3 fail-closed.
DEFAULT_BOT_CHALLENGE_PHASE: int = 1


# Env var name carrying the global phase. AS.0.5 §2.4 ships this as a
# global runtime knob so ops can flip the entire AS layer between
# phases without redeploy. Per-tenant Phase 3 opt-in is a separate
# axis (``tenants.auth_features.turnstile_required``) that AS.6.4
# / AS.5.2 will wire on top — this helper reads only the global
# phase for now (per AS.0.5 §6.1 acceptance criteria, AS.6.3 is the
# global wire; per-tenant override is AS.5.2 / AS.6.4 scope).
PHASE_ENV_VAR: str = "OMNISIGHT_BOT_CHALLENGE_PHASE"


def current_phase() -> int:
    """Return the current global bot-challenge phase (1 / 2 / 3).

    Reads ``OMNISIGHT_BOT_CHALLENGE_PHASE`` env var per AS.0.5 §2.4.
    Invalid / out-of-range values fall back to
    :data:`DEFAULT_BOT_CHALLENGE_PHASE` (1) — fail-open semantics
    are the safer default if the operator typoed the env var.

    Module-global state audit: pure env read, no module-level cache.
    Cross-worker consistency: every uvicorn worker reads the same
    env value (answer #1 of SOP §1). Restart-only effective per
    AS.0.5 §7.2 (no hot-reload).
    """
    raw = (os.environ.get(PHASE_ENV_VAR) or "").strip()
    if not raw:
        return DEFAULT_BOT_CHALLENGE_PHASE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "AS.6.3: %s=%r is not an integer; falling back to phase %d",
            PHASE_ENV_VAR, raw, DEFAULT_BOT_CHALLENGE_PHASE,
        )
        return DEFAULT_BOT_CHALLENGE_PHASE
    if value not in (1, 2, 3):
        logger.warning(
            "AS.6.3: %s=%d not in {1,2,3}; falling back to phase %d",
            PHASE_ENV_VAR, value, DEFAULT_BOT_CHALLENGE_PHASE,
        )
        return DEFAULT_BOT_CHALLENGE_PHASE
    return value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Mirrors :func:`backend.security.bot_challenge.is_enabled` so the
    two layers share one knob. Default ``True`` if the field hasn't
    landed on :class:`backend.config.Settings` yet (forward-promotion
    guard — pre-AS.0.8 boot order is unaffected).
    """
    try:
        from backend.config import settings  # local import: zero import-time side effect
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Token / IP / bypass-context extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def extract_token_from_request(
    request: "Request",
    *,
    body_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Read the widget-issued token from request body or header.

    Body wins on conflict — a stale header value can't override a
    fresh body submit. Returns ``None`` when neither surface carries
    a non-empty token (the AS.3.1 verify path then routes through
    the ``unverified_servererr`` fail-open branch with a
    ``missing-input-response`` error code).

    *body_payload* is the parsed JSON / form body the caller already
    has — typically the FastAPI :class:`pydantic.BaseModel` instance
    cast to dict. The helper accepts a generic Mapping so callers
    can pass either ``request_body.model_dump()`` or a hand-built
    dict (the latter for routes that don't use a Pydantic model).
    """
    if body_payload is not None:
        body_value = body_payload.get(TURNSTILE_TOKEN_BODY_FIELD)
        if isinstance(body_value, str) and body_value.strip():
            return body_value.strip()
    header_value = request.headers.get(TURNSTILE_TOKEN_HEADER) if request else None
    if header_value and header_value.strip():
        return header_value.strip()
    return None


def _extract_client_ip(request: "Request") -> Optional[str]:
    """Real-client IP per the existing auth router convention.

    Mirrors :func:`backend.routers.auth._client_key`: prefer the
    Cloudflare ``cf-connecting-ip`` header (CF Tunnel terminates TLS
    so the immediate peer is always the tunnel), fall back to
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


def _extract_caller_kind(request: "Request") -> Optional[str]:
    """Return the AS.0.6 §2.1 caller_kind if the request was
    authenticated via an API key family the bot_challenge bypass
    list recognises.

    Routes downstream of the auth middleware can attach a
    ``caller_kind`` attribute to ``request.state`` to opt into the
    automation-bypass axis. Routes that don't (the typical /auth/login
    path — anonymous request) return ``None`` and the bypass evaluator
    falls through to the test-token / IP-allowlist / path axes.
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
    """Read the AS.0.6 §2.3 test-token header value off the request.

    The header name is the AS.3.1 :data:`bot_challenge.TEST_TOKEN_HEADER`
    constant. A missing header returns ``None``; the bypass evaluator
    then falls through to the next axis.
    """
    if request is None:
        return None
    value = request.headers.get(bot_challenge.TEST_TOKEN_HEADER) or ""
    return value.strip() or None


def _expected_test_token() -> Optional[str]:
    """Read the operator-side expected test-token from env.

    Env var name pinned at ``OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN`` so
    a single token works across the whole AS layer. AS.0.6 §2.3
    invariant: empty / < 32 chars treated as unset (handled by
    :func:`bot_challenge._test_token_matches`).
    """
    value = (os.environ.get("OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN") or "").strip()
    return value or None


def _resolve_tenant_ip_allowlist(
    tenant_ip_allowlist: Optional[Iterable[str]],
) -> tuple[str, ...]:
    """Normalise the per-tenant CIDR allowlist into the immutable
    tuple :class:`bot_challenge.BypassContext` expects.

    The allowlist source is the per-tenant
    ``tenants.auth_features.automation_ip_allowlist`` JSONB column
    (AS.0.2). Reads of that column live in the caller (it has the
    tenant_id + DB conn already); the helper just normalises the
    shape so corrupt entries don't crash :func:`bot_challenge.evaluate_bypass`
    (which already logs corrupt CIDRs and skips them).
    """
    if not tenant_ip_allowlist:
        return ()
    return tuple(
        entry for entry in tenant_ip_allowlist
        if isinstance(entry, str) and entry.strip()
    )


def build_bypass_context(
    request: "Request",
    *,
    form_path: str,
    tenant_id: Optional[str] = None,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
    widget_action: Optional[str] = None,
) -> bot_challenge.BypassContext:
    """Build the AS.3.1 :class:`bot_challenge.BypassContext` from a
    FastAPI Request + form metadata.

    Pure helper — reads request headers + state attributes only, no
    DB calls. The caller passes ``tenant_ip_allowlist`` after fetching
    it from ``tenants.auth_features.automation_ip_allowlist``;
    omitting it disables the IP-allowlist axis (other axes still fire
    independently per AS.0.6 §4 precedence).
    """
    return bot_challenge.BypassContext(
        path=form_path,
        caller_kind=_extract_caller_kind(request),
        api_key_id=None,
        api_key_prefix=None,
        client_ip=_extract_client_ip(request),
        tenant_ip_allowlist=_resolve_tenant_ip_allowlist(tenant_ip_allowlist),
        test_token_header_value=_extract_test_token(request),
        test_token_expected=_expected_test_token(),
        tenant_id=tenant_id,
        widget_action=widget_action,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider selection + secret resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _request_region(request: "Request") -> Optional[str]:
    """Return the ISO 3166 country code from the Cloudflare
    ``CF-IPCountry`` header, or ``None`` if absent.

    Used by :func:`pick_form_provider` to drive the AS.3.3 region-
    based provider heuristic (GDPR-strict regions → hCaptcha).
    """
    if request is None:
        return None
    value = (request.headers.get("cf-ipcountry") or "").strip()
    return value or None


def pick_form_provider(
    request: "Request",
    *,
    override: Optional[bot_challenge.Provider] = None,
    ecosystem_hints: Iterable[str] = (),
) -> bot_challenge.Provider:
    """Pick the AS.3.3 captcha provider for *request*.

    Reads ``CF-IPCountry`` from the request headers + caller-supplied
    ecosystem hints + optional explicit override. Defaults to
    :data:`bot_challenge.Provider.TURNSTILE` per AS.0.5 §1 (Turnstile
    is the family default).
    """
    return bot_challenge.pick_provider(
        default=bot_challenge.Provider.TURNSTILE,
        region=_request_region(request),
        ecosystem_hints=ecosystem_hints,
        override=override,
    )


def resolve_provider_secret(provider: bot_challenge.Provider) -> str:
    """Read the per-provider site secret env var.

    Empty string is returned (not raised) when the env var is unset —
    :func:`bot_challenge.verify` then routes through the
    ``unverified_servererr`` fail-open branch with
    ``error_kind=config_missing_secret``. This matches AS.0.5 §2.4
    row 3 (server-side config missing must NEVER lock users out).
    """
    env_var = bot_challenge.secret_env_for(provider)
    return (os.environ.get(env_var) or "").strip()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.5.1 rollup audit fan-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# AS.3.1 bot_challenge OUTCOME_* literal → AS.5.1 BOT_CHALLENGE_PASS_KIND
# vocabulary. Frozen mapping covers the four ``pass``-side outcomes
# the AS.5.1 rollup family knows about. Verify-pass / bypass paths
# fan out an ``auth.bot_challenge_pass`` row with the matching kind.
_PASS_OUTCOME_TO_KIND: Mapping[str, str] = types.MappingProxyType({
    bot_challenge.OUTCOME_PASS: auth_event.BOT_CHALLENGE_PASS_VERIFIED,
    bot_challenge.OUTCOME_BYPASS_APIKEY: auth_event.BOT_CHALLENGE_PASS_BYPASS_APIKEY,
    bot_challenge.OUTCOME_BYPASS_IP_ALLOWLIST: auth_event.BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST,
    bot_challenge.OUTCOME_BYPASS_TEST_TOKEN: auth_event.BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN,
})


# AS.3.1 bot_challenge OUTCOME_* literal → AS.5.1 BOT_CHALLENGE_FAIL_REASON
# vocabulary. Covers the verify-fail / jsfail-honeypot-fail outcomes
# that the rollup family records as ``auth.bot_challenge_fail`` rows.
_FAIL_OUTCOME_TO_REASON: Mapping[str, str] = types.MappingProxyType({
    bot_challenge.OUTCOME_BLOCKED_LOWSCORE: auth_event.BOT_CHALLENGE_FAIL_LOWSCORE,
    bot_challenge.OUTCOME_UNVERIFIED_LOWSCORE: auth_event.BOT_CHALLENGE_FAIL_LOWSCORE,
    bot_challenge.OUTCOME_UNVERIFIED_SERVERERR: auth_event.BOT_CHALLENGE_FAIL_SERVER_ERROR,
    bot_challenge.OUTCOME_JSFAIL_HONEYPOT_FAIL: auth_event.BOT_CHALLENGE_FAIL_HONEYPOT,
})


# ``OUTCOME_BYPASS_BOOTSTRAP`` / ``OUTCOME_BYPASS_WEBHOOK`` /
# ``OUTCOME_BYPASS_CHATOPS`` / ``OUTCOME_BYPASS_PROBE`` /
# ``OUTCOME_JSFAIL_FALLBACK_*`` / ``OUTCOME_JSFAIL_HONEYPOT_PASS``
# are intentionally NOT in either map: bootstrap / webhook / chatops
# / probe paths shouldn't be hitting the OmniSight self-form
# verifier in the first place (different routes), and the jsfail
# fallback / honeypot-pass paths are handled by AS.6.4's wiring,
# not here. Anything not in either map gets logged + skipped on
# the rollup fan-out so a future outcome literal addition surfaces
# in journalctl rather than silently writing a broken audit row.


def _route_to_pass_or_fail(
    result: bot_challenge.BotChallengeResult,
    *,
    form_path: str,
    actor: str,
) -> Optional[auth_event.AuthAuditPayload]:
    """Map a :class:`BotChallengeResult` onto the AS.5.1 rollup payload
    (``auth.bot_challenge_pass`` / ``auth.bot_challenge_fail``) or
    return ``None`` if no rollup row should fire for this outcome.

    Pure builder — no IO, no knob check. The async emit wrapper
    around it does the knob check + ``audit.log`` route.
    """
    provider_value = result.provider.value if result.provider else None
    if result.outcome in _PASS_OUTCOME_TO_KIND:
        kind = _PASS_OUTCOME_TO_KIND[result.outcome]
        # ``score`` is required for verified pass; for bypass kinds
        # the AS.5.1 builder REJECTS a non-None score (it'd be
        # misleading because no challenge ran). Pass through the
        # underlying score only on the verified branch.
        score = result.score if kind == auth_event.BOT_CHALLENGE_PASS_VERIFIED else None
        return auth_event.build_bot_challenge_pass_payload(
            auth_event.BotChallengePassContext(
                form_path=form_path,
                kind=kind,
                provider=provider_value,
                score=score,
                actor=actor,
            )
        )
    if result.outcome in _FAIL_OUTCOME_TO_REASON:
        reason = _FAIL_OUTCOME_TO_REASON[result.outcome]
        # ``score`` is meaningful only for the lowscore reason; for
        # honeypot / server_error / jsfail it's left as None per
        # AS.5.1 :class:`BotChallengeFailContext` docstring.
        score = result.score if reason == auth_event.BOT_CHALLENGE_FAIL_LOWSCORE else None
        return auth_event.build_bot_challenge_fail_payload(
            auth_event.BotChallengeFailContext(
                form_path=form_path,
                reason=reason,
                provider=provider_value,
                score=score,
                actor=actor,
            )
        )
    return None


async def _emit_rollup_audit(
    result: bot_challenge.BotChallengeResult,
    *,
    form_path: str,
    actor: str,
) -> None:
    """Fan out the AS.5.1 ``auth.bot_challenge_pass`` /
    ``auth.bot_challenge_fail`` row, swallowing any audit-emit
    failure (the chain has its own retry; we don't want a rollup
    fan-out failure to break the verify path itself).
    """
    payload = _route_to_pass_or_fail(result, form_path=form_path, actor=actor)
    if payload is None:
        # Outcome literal not mapped — log warn so the gap is visible
        # in journalctl and a future row addition surfaces here, but
        # don't raise. The forensic ``bot_challenge.*`` row still
        # fires from AS.3.1's own emit path (out of our control).
        logger.warning(
            "AS.6.3: bot_challenge result outcome %r has no AS.5.1 "
            "rollup mapping; skipping rollup emit (form_path=%s)",
            result.outcome, form_path,
        )
        return
    if payload.action == auth_event.EVENT_AUTH_BOT_CHALLENGE_PASS:
        try:
            await auth_event.emit_bot_challenge_pass(
                auth_event.BotChallengePassContext(
                    form_path=form_path,
                    kind=_PASS_OUTCOME_TO_KIND[result.outcome],
                    provider=result.provider.value if result.provider else None,
                    score=result.score if _PASS_OUTCOME_TO_KIND[result.outcome] == auth_event.BOT_CHALLENGE_PASS_VERIFIED else None,
                    actor=actor,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "AS.6.3: auth.bot_challenge_pass emit failed for "
                "form_path=%s outcome=%s: %s",
                form_path, result.outcome, exc,
            )
    else:
        try:
            await auth_event.emit_bot_challenge_fail(
                auth_event.BotChallengeFailContext(
                    form_path=form_path,
                    reason=_FAIL_OUTCOME_TO_REASON[result.outcome],
                    provider=result.provider.value if result.provider else None,
                    score=result.score if _FAIL_OUTCOME_TO_REASON[result.outcome] == auth_event.BOT_CHALLENGE_FAIL_LOWSCORE else None,
                    actor=actor,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "AS.6.3: auth.bot_challenge_fail emit failed for "
                "form_path=%s outcome=%s: %s",
                form_path, result.outcome, exc,
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  End-to-end orchestrators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _build_verify_context(
    *,
    form_action: str,
    form_path: str,
    token: Optional[str],
    request: "Request",
    tenant_id: Optional[str],
    tenant_ip_allowlist: Optional[Iterable[str]],
    provider: bot_challenge.Provider,
    secret: str,
    phase: int,
) -> bot_challenge.VerifyContext:
    """Build the AS.3.1 :class:`VerifyContext` for *form_action*.

    Pure helper — reads request only, no IO. Caller-supplied
    *provider* + *secret* + *phase* are honoured verbatim.
    """
    bypass_ctx = build_bypass_context(
        request,
        form_path=form_path,
        tenant_id=tenant_id,
        tenant_ip_allowlist=tenant_ip_allowlist,
        widget_action=form_action,
    )
    return bot_challenge.VerifyContext(
        provider=provider,
        token=token,
        secret=secret,
        phase=phase,
        widget_action=form_action,
        expected_action=form_action,
        remote_ip=_extract_client_ip(request),
        bypass=bypass_ctx,
    )


async def verify_form_token(
    form_action: str,
    token: Optional[str],
    *,
    request: "Request",
    tenant_id: Optional[str] = None,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
    provider: Optional[bot_challenge.Provider] = None,
    fallback_providers: Iterable[bot_challenge.Provider] = (),
    fallback_token: Optional[str] = None,
    actor: str = "anonymous",
    http_client: Optional["httpx.AsyncClient"] = None,
    phase: Optional[int] = None,
) -> bot_challenge.BotChallengeResult:
    """End-to-end verify orchestrator for one of the four
    OmniSight self-forms.

    Pipeline (AS.0.5 §4 precedence):

      1. AS.0.8 single-knob short-circuit — knob-off → return
         :func:`bot_challenge.passthrough`, no audit, no env read.
      2. Resolve provider + secret + phase (caller can override
         each; defaults to :func:`pick_form_provider` +
         :func:`resolve_provider_secret` + :func:`current_phase`).
      3. Build :class:`VerifyContext` + :class:`BypassContext` from
         the request.
      4. Run :func:`bot_challenge.verify_with_fallback` (AS.3.5 chain
         when *fallback_providers* is non-empty, otherwise behaves
         identically to :func:`bot_challenge.verify`).
      5. Fan out the AS.5.1 ``auth.bot_challenge_pass`` /
         ``auth.bot_challenge_fail`` rollup row.
      6. Return the :class:`BotChallengeResult` for the caller to
         act on (``allow`` decides 4xx vs continue; the forensic
         ``bot_challenge.*`` audit row already fired from AS.3.1's
         own emit layer).

    Never raises on the happy / fail paths — only the
    misconfiguration corner (:class:`bot_challenge.ProviderConfigError`,
    when the provider's secret env exists but is empty AND the AS.3.1
    layer rejected it instead of routing through fail-open) propagates.
    """
    if form_action not in SUPPORTED_FORM_ACTIONS:
        raise ValueError(
            f"unknown form_action: {form_action!r} "
            f"(supported: {sorted(SUPPORTED_FORM_ACTIONS)})"
        )

    form_path = form_path_for_action(form_action)

    if not is_enabled():
        # AS.0.8 noop — return the AS.3.1 passthrough result with
        # ``passthrough_reason="knob_off"`` so the caller fan-out
        # layer can grep for the bypass without re-deriving why.
        return bot_challenge.passthrough(reason="knob_off")

    chosen_provider = provider if provider is not None else pick_form_provider(request)
    chosen_secret = resolve_provider_secret(chosen_provider)
    chosen_phase = phase if phase is not None else current_phase()

    primary_ctx = _build_verify_context(
        form_action=form_action,
        form_path=form_path,
        token=token,
        request=request,
        tenant_id=tenant_id,
        tenant_ip_allowlist=tenant_ip_allowlist,
        provider=chosen_provider,
        secret=chosen_secret,
        phase=chosen_phase,
    )

    fallback_ctxs: tuple[bot_challenge.VerifyContext, ...] = ()
    if fallback_providers:
        fallback_ctxs = tuple(
            _build_verify_context(
                form_action=form_action,
                form_path=form_path,
                token=fallback_token,
                request=request,
                tenant_id=tenant_id,
                tenant_ip_allowlist=tenant_ip_allowlist,
                provider=fb_provider,
                secret=resolve_provider_secret(fb_provider),
                phase=chosen_phase,
            )
            for fb_provider in fallback_providers
        )

    if fallback_ctxs:
        result = await bot_challenge.verify_with_fallback(
            primary_ctx, fallbacks=fallback_ctxs, http_client=http_client,
        )
    else:
        result = await bot_challenge.verify(primary_ctx, http_client=http_client)

    await _emit_rollup_audit(result, form_path=form_path, actor=actor)
    return result


async def verify_form_token_or_reject(
    form_action: str,
    token: Optional[str],
    *,
    request: "Request",
    tenant_id: Optional[str] = None,
    tenant_ip_allowlist: Optional[Iterable[str]] = None,
    provider: Optional[bot_challenge.Provider] = None,
    fallback_providers: Iterable[bot_challenge.Provider] = (),
    fallback_token: Optional[str] = None,
    actor: str = "anonymous",
    http_client: Optional["httpx.AsyncClient"] = None,
    phase: Optional[int] = None,
) -> bot_challenge.BotChallengeResult:
    """Same pipeline as :func:`verify_form_token`, then if
    :func:`bot_challenge.should_reject` says yes (Phase 3 confirmed
    low score), raise :class:`bot_challenge.BotChallengeRejected`
    carrying the result so the caller's HTTP layer can serialise the
    canonical 429 ``bot_challenge_failed`` response.

    On allow=True (every Phase 1 / 2 outcome + Phase 3 pass + bypass
    + fail-open server-error), returns the result.

    Caller template (the four OmniSight self-form route handlers
    follow this shape verbatim per AS.0.5 §6.1):

        from backend.security import turnstile_form_verifier as _tv
        from backend.security import bot_challenge as _bc
        try:
            await _tv.verify_form_token_or_reject(
                _tv.FORM_ACTION_LOGIN,
                token=request_body.turnstile_token,
                request=request,
            )
        except _bc.BotChallengeRejected as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code},
            )
    """
    result = await verify_form_token(
        form_action,
        token,
        request=request,
        tenant_id=tenant_id,
        tenant_ip_allowlist=tenant_ip_allowlist,
        provider=provider,
        fallback_providers=fallback_providers,
        fallback_token=fallback_token,
        actor=actor,
        http_client=http_client,
        phase=phase,
    )
    if bot_challenge.should_reject(result):
        raise bot_challenge.BotChallengeRejected(result)
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
    # Token surface constants
    "TURNSTILE_TOKEN_BODY_FIELD",
    "TURNSTILE_TOKEN_HEADER",
    # Phase
    "DEFAULT_BOT_CHALLENGE_PHASE",
    "PHASE_ENV_VAR",
    "current_phase",
    # AS.0.8 single-knob hook
    "is_enabled",
    # Pure helpers
    "extract_token_from_request",
    "build_bypass_context",
    "pick_form_provider",
    "resolve_provider_secret",
    # Async orchestrators
    "verify_form_token",
    "verify_form_token_or_reject",
]

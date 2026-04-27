"""AS.3.1 — Bot challenge unified interface (Turnstile / reCAPTCHA v2 /
reCAPTCHA v3 / hCaptcha).

Provider-agnostic surface for the AS bot-challenge family.  Lets every
caller (AS.6.3 OmniSight self login / signup / password-reset / contact
form, AS.7.x scaffolded forms in generated apps, SC.13 reuse) hand the
same shape of inputs to the same ``verify`` entry point regardless of
which captcha vendor sits behind the request — Turnstile (Cloudflare),
reCAPTCHA v2 / v3 (Google), or hCaptcha — and read the same shape of
result back.

Plan / spec source
──────────────────
* `docs/security/as_0_5_turnstile_fail_open_phased_strategy.md`
  — phase semantics, fail-open invariant, audit event canonical names
  (§3, 13 ``EVENT_BOT_CHALLENGE_*`` strings + 4 phase advance / revert),
  bypass-list precedence (§4), provider site-secret env wiring (§5),
  drift-guard test patterns (§8).
* `docs/security/as_0_6_automation_bypass_list.md`
  — three bypass mechanisms (API key auth / per-tenant IP allowlist /
  test-token header), axis-internal precedence A → C → B (§4),
  audit metadata schema (§3), 2 extra ``bypass_*`` events (§3).
* `docs/design/as-auth-security-shared-library.md` §3
  — TS twin contract sketch (AS.3.2 will mirror this surface).

What this row ships (AS.3.1 scope, strict)
──────────────────────────────────────────
1. **Provider abstraction** — :class:`Provider` enum + per-provider
   server-side ``siteverify`` HTTP call wrapped in :func:`verify_provider`.
2. **Result classification** — :class:`BotChallengeResult` frozen
   dataclass + ``Outcome`` literal vocabulary (``pass`` / ``unverified_*``
   / ``blocked_lowscore`` / ``bypass_*`` / ``jsfail_*``).
3. **Bypass list helpers** — :func:`evaluate_bypass` walks the AS.0.6 §4
   axis-internal precedence (A api_key → C test-token → B ip-allowlist
   → D path/caller) and returns either a :class:`BypassReason` or
   ``None``; mid-orchestration callers stop on first hit.
   :func:`pick_provider` (AS.3.3) implements the region + ecosystem
   heuristic that picks a captcha vendor before :func:`verify` is
   called.
4. **Phase-aware classify** — :func:`classify_outcome` turns the
   provider's normalized score + (failure mode, phase) tuple into the
   final :class:`BotChallengeResult` per AS.0.5 §2 phase matrix
   (Phase 1/2 fail-open, Phase 3 fail-closed only for confirmed
   low-score).
5. **13 + 4 audit event constants** — frozen ``EVENT_*`` strings
   matching AS.0.5 §3 + AS.0.6 §3 byte-for-byte (drift-guard tested).
6. **AS.0.8 single-knob hook** — :func:`is_enabled` short-circuits to
   ``True`` (passthrough) when ``settings.as_enabled=False``;
   :func:`verify` returns a ``passthrough`` result without any HTTP /
   audit / phase logic in that case.

Out of scope (deferred to follow-up rows in the same epic)
──────────────────────────────────────────────────────────
* AS.3.2 — TS twin under ``templates/_shared/bot-challenge/``.
* AS.3.3 — Provider-selection logic (region / ecosystem heuristics).
  Landed: :func:`pick_provider` now consumes ``region`` +
  ``ecosystem_hints`` + ``override`` to pick a vendor per the
  heuristic doc-string. Cross-twin parity guard locks the
  :data:`GDPR_STRICT_REGIONS` codes to the TS twin.
* AS.3.4 — Server-side score verification + reject enforcement
  (``score < 0.5`` → reject in Phase 3 fail-closed branch).
  Landed: :func:`should_reject` predicate +
  :class:`BotChallengeRejected` exception + :func:`verify_and_enforce`
  single-call orchestrator + :data:`BOT_CHALLENGE_REJECTED_CODE`
  (``"bot_challenge_failed"`` per AS.0.5 §3 row 116) +
  :data:`BOT_CHALLENGE_REJECTED_HTTP_STATUS` (``429``). Caller's
  HTTP layer maps the exception to a 429 response. Wiring into the
  four OmniSight self forms (login / signup / password-reset / contact)
  is AS.6.3 — the primitives ship here so AS.6.3 can call a single
  `verify_and_enforce(ctx)` instead of re-implementing reject logic
  per route.
* AS.3.5 — Fallback chain (primary → secondary → tertiary on jsfail).
  This row exposes the primitives — :func:`verify_provider` per
  provider — but the orchestrator that chains them on widget JS load
  failure is AS.3.5.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* All public symbols are immutable (``frozen=True`` dataclasses, tuples,
  strings, frozensets).  The only module-level dict
  (:data:`_PROVIDER_SECRET_ENVS`) is a constant and is asserted never to
  mutate by ``test_as_0_5_provider_site_secret_envs_distinct``.
* No DB writes, no module-level cache, no env reads at import time —
  every env / settings read is lazy at call time per worker (answer #1
  of SOP §1: each uvicorn worker derives the same constant from the
  same source).
* HTTP client is per-call (``httpx.AsyncClient`` context manager); no
  module-level connection pool that could leak across workers.
* Module-import is side-effect free — pure constants + dataclasses +
  function defs only.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — bot challenge verification is a per-request, stateless RPC to the
upstream IdP siteverify endpoint.  No DB write, no shared in-memory
state, no read-after-write race.  The audit row is fan-out *after* the
verify result is computed; downstream callers don't read it back in the
same request.

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
  fallback (defaults ``True`` if the field hasn't landed on
  :class:`backend.config.Settings` yet — AS-roadmap forward promotion).
* :func:`verify` short-circuits with :func:`passthrough` (i.e.
  ``Outcome="pass"`` + ``score=1.0`` + ``audit=False``) when knob-off
  per AS.0.5 §4 precedence axis #2 + AS.0.5 §7.2 decoupling rules.
* No audit row is written when knob-off; downstream emit layer
  (caller) silently skips on the same gate per AS.0.8 §5 truth-table.

TS twin
───────
``templates/_shared/bot-challenge/`` (AS.3.2) will mirror the public
shape — ``Provider`` enum values, 13 + 4 ``EVENT_BOT_CHALLENGE_*``
strings, ``Outcome`` literal vocabulary, and the score-calibration
table.  AS.3.2 PR will land a SHA-256 cross-twin drift guard the same
pattern AS.1.2 / AS.1.4 / AS.2.3 established.
"""

from __future__ import annotations

import enum
import hashlib
import ipaddress
import logging
import secrets
import types
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import httpx


logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — providers, endpoints, envs, defaults
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class Provider(str, enum.Enum):
    """The four bot-challenge vendors AS.3 ships support for.

    Values are the canonical short strings used in audit metadata
    (``metadata.provider``), config envs (``OMNISIGHT_<PROVIDER>_SECRET``),
    and TS twin enum (AS.3.2 mirrors).
    """

    TURNSTILE = "turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"


# Canonical siteverify endpoints per provider.  Wrapped in
# ``MappingProxyType`` for module-level immutability — assigning to or
# deleting from this mapping at runtime raises TypeError, satisfying
# the SOP §1 "no module-level mutable containers" invariant.
SITEVERIFY_URLS: Mapping[Provider, str] = types.MappingProxyType({
    Provider.TURNSTILE: "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    Provider.RECAPTCHA_V2: "https://www.google.com/recaptcha/api/siteverify",
    Provider.RECAPTCHA_V3: "https://www.google.com/recaptcha/api/siteverify",
    Provider.HCAPTCHA: "https://hcaptcha.com/siteverify",
})


# Per-provider env-var name carrying the site secret.  AS.0.5 §5 third
# invariant (``test_as_0_5_provider_site_secret_envs_distinct``): each
# provider has its own env, no env may be reused across providers, no
# heat-swap.  reCAPTCHA v2 + v3 share an env (same Google account /
# project, just two different site keys); the secret-key envelope is
# per-key, so we route v2 + v3 to the same env on purpose — Google's
# /siteverify will dispatch on the secret-key version internally.
_PROVIDER_SECRET_ENVS: Mapping[str, str] = types.MappingProxyType({
    "turnstile": "OMNISIGHT_TURNSTILE_SECRET",
    "recaptcha": "OMNISIGHT_RECAPTCHA_SECRET",
    "hcaptcha": "OMNISIGHT_HCAPTCHA_SECRET",
})


def secret_env_for(provider: Provider) -> str:
    """Return the env-var name carrying the site secret for *provider*.

    Folds reCAPTCHA v2 + v3 onto the same env (per the comment on
    :data:`_PROVIDER_SECRET_ENVS`).  AS.0.5 §5 invariant.
    """
    if provider in (Provider.RECAPTCHA_V2, Provider.RECAPTCHA_V3):
        return _PROVIDER_SECRET_ENVS["recaptcha"]
    if provider is Provider.TURNSTILE:
        return _PROVIDER_SECRET_ENVS["turnstile"]
    if provider is Provider.HCAPTCHA:
        return _PROVIDER_SECRET_ENVS["hcaptcha"]
    raise ValueError(f"unknown provider: {provider!r}")  # pragma: no cover


# Default fail-mode score threshold per AS.0.5 §2.4 + design doc §3.5
# (``score < 0.5`` → reject in Phase 3 fail-closed branch).  Pinned at
# 0.5 — design doc §10 explicitly forbids stricter thresholds because
# vendor calibrations differ; raising it amplifies false-positives.
DEFAULT_SCORE_THRESHOLD: float = 0.5


# Default HTTP timeout for siteverify calls.  3 s is the upper bound
# every vendor's SLA covers; longer means we're queueing user-visible
# latency on a captcha that's almost certainly already mis-configured.
DEFAULT_VERIFY_TIMEOUT_SECONDS: float = 3.0


# Test-token header name (AS.0.6 §2.3 invariant).  Constant — drift
# guard test asserts no inline string anywhere in callers.
TEST_TOKEN_HEADER: str = "X-OmniSight-Test-Token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.3.4 — server-side score verification + reject enforcement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Canonical error code returned to the client when the request is
# rejected by a confirmed low-score bot challenge (Phase 3 fail-closed
# branch, ``response.success and response.score < threshold``).  Pinned
# by AS.0.5 §3 row 116:
#
#     | Browser user, widget OK, score < 0.5 | fail | HTTP 429
#       `bot_challenge_failed`, UI 顯示「Try again or contact admin」 |
#       `bot_challenge.blocked_lowscore` |
#
# The string is the contract surface the front-end UI keys on to render
# its retry CTA + "contact admin" copy.  Cross-twin drift guard locks it
# byte-for-byte against the TS twin.
BOT_CHALLENGE_REJECTED_CODE: str = "bot_challenge_failed"


# Canonical HTTP status code for a bot challenge rejection.  AS.0.5 §3
# ships 429 (rate-limit class) over 401 (auth class) deliberately:
#
#   * 429 stays vague about *which* signal we used (low-score vs
#     honeypot vs missing token), denying an attacker the side-channel
#     they'd get from per-failure-mode HTTP codes.
#   * 429 matches operator runbooks for retry semantics — the client SDK
#     already knows to back-off + offer a retry CTA on a 429, so we
#     piggy-back on existing retry infrastructure instead of inventing a
#     bot-specific class.
#   * Pinned at 429; the TS twin mirrors the same int. Drift guard locks
#     both sides byte-for-byte.
BOT_CHALLENGE_REJECTED_HTTP_STATUS: int = 429


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit event canonical names — AS.0.5 §3 + AS.0.6 §3
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Verify-outcome events (8) — emitted from :func:`verify` once classification
# completes.  Strings are part of the AS-roadmap contract; tests pin them.
EVENT_BOT_CHALLENGE_PASS = "bot_challenge.pass"
EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE = "bot_challenge.unverified_lowscore"
EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR = "bot_challenge.unverified_servererr"
EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE = "bot_challenge.blocked_lowscore"
EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA = "bot_challenge.jsfail_fallback_recaptcha"
EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA = "bot_challenge.jsfail_fallback_hcaptcha"
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS = "bot_challenge.jsfail_honeypot_pass"
EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL = "bot_challenge.jsfail_honeypot_fail"

# Bypass events (7) — emitted from the bypass branch of :func:`verify`.
# AS.0.5 §3 ships 5 (apikey / webhook / chatops / bootstrap / probe),
# AS.0.6 §3 adds 2 (ip_allowlist / test_token).  Five + two = seven
# always-on bypass categories.
EVENT_BOT_CHALLENGE_BYPASS_APIKEY = "bot_challenge.bypass_apikey"
EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK = "bot_challenge.bypass_webhook"
EVENT_BOT_CHALLENGE_BYPASS_CHATOPS = "bot_challenge.bypass_chatops"
EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP = "bot_challenge.bypass_bootstrap"
EVENT_BOT_CHALLENGE_BYPASS_PROBE = "bot_challenge.bypass_probe"
EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST = "bot_challenge.bypass_ip_allowlist"
EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN = "bot_challenge.bypass_test_token"

# Phase advance / revert events (4) — AS.5.2 dashboard owns the
# emitter helper (:func:`emit_phase_advance` / `emit_phase_revert` will
# land on AS.5.2); the strings live here because this is the bot-
# challenge family's namespace SoT.
EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2 = "bot_challenge.phase_advance_p1_to_p2"
EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3 = "bot_challenge.phase_advance_p2_to_p3"
EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2 = "bot_challenge.phase_revert_p3_to_p2"
EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1 = "bot_challenge.phase_revert_p2_to_p1"


ALL_BOT_CHALLENGE_EVENTS: tuple[str, ...] = (
    EVENT_BOT_CHALLENGE_PASS,
    EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
    EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
    EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
    EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA,
    EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA,
    EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS,
    EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL,
    EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
    EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK,
    EVENT_BOT_CHALLENGE_BYPASS_CHATOPS,
    EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP,
    EVENT_BOT_CHALLENGE_BYPASS_PROBE,
    EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST,
    EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN,
    EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2,
    EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3,
    EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2,
    EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1,
)


# Outcome literal vocabulary — each result row carries one of these as
# ``BotChallengeResult.outcome``.  Audit-event mapping is:
#
#   pass                   → EVENT_BOT_CHALLENGE_PASS
#   unverified_lowscore    → EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE
#   unverified_servererr   → EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR
#   blocked_lowscore       → EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE
#   bypass_*               → EVENT_BOT_CHALLENGE_BYPASS_* (7 flavours)
#   jsfail_*               → EVENT_BOT_CHALLENGE_JSFAIL_* (4 flavours)
#
# :func:`event_for_outcome` does the lookup; constants kept in sync via
# ``test_event_for_outcome_covers_every_outcome``.
OUTCOME_PASS = "pass"
OUTCOME_UNVERIFIED_LOWSCORE = "unverified_lowscore"
OUTCOME_UNVERIFIED_SERVERERR = "unverified_servererr"
OUTCOME_BLOCKED_LOWSCORE = "blocked_lowscore"
OUTCOME_BYPASS_APIKEY = "bypass_apikey"
OUTCOME_BYPASS_WEBHOOK = "bypass_webhook"
OUTCOME_BYPASS_CHATOPS = "bypass_chatops"
OUTCOME_BYPASS_BOOTSTRAP = "bypass_bootstrap"
OUTCOME_BYPASS_PROBE = "bypass_probe"
OUTCOME_BYPASS_IP_ALLOWLIST = "bypass_ip_allowlist"
OUTCOME_BYPASS_TEST_TOKEN = "bypass_test_token"
OUTCOME_JSFAIL_FALLBACK_RECAPTCHA = "jsfail_fallback_recaptcha"
OUTCOME_JSFAIL_FALLBACK_HCAPTCHA = "jsfail_fallback_hcaptcha"
OUTCOME_JSFAIL_HONEYPOT_PASS = "jsfail_honeypot_pass"
OUTCOME_JSFAIL_HONEYPOT_FAIL = "jsfail_honeypot_fail"


ALL_OUTCOMES: tuple[str, ...] = (
    OUTCOME_PASS,
    OUTCOME_UNVERIFIED_LOWSCORE,
    OUTCOME_UNVERIFIED_SERVERERR,
    OUTCOME_BLOCKED_LOWSCORE,
    OUTCOME_BYPASS_APIKEY,
    OUTCOME_BYPASS_WEBHOOK,
    OUTCOME_BYPASS_CHATOPS,
    OUTCOME_BYPASS_BOOTSTRAP,
    OUTCOME_BYPASS_PROBE,
    OUTCOME_BYPASS_IP_ALLOWLIST,
    OUTCOME_BYPASS_TEST_TOKEN,
    OUTCOME_JSFAIL_FALLBACK_RECAPTCHA,
    OUTCOME_JSFAIL_FALLBACK_HCAPTCHA,
    OUTCOME_JSFAIL_HONEYPOT_PASS,
    OUTCOME_JSFAIL_HONEYPOT_FAIL,
)


# Outcome → audit event lookup.  Source-of-truth mapping; tests pin
# every outcome has exactly one event and vice versa.
_OUTCOME_TO_EVENT: Mapping[str, str] = types.MappingProxyType({
    OUTCOME_PASS: EVENT_BOT_CHALLENGE_PASS,
    OUTCOME_UNVERIFIED_LOWSCORE: EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
    OUTCOME_UNVERIFIED_SERVERERR: EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
    OUTCOME_BLOCKED_LOWSCORE: EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
    OUTCOME_BYPASS_APIKEY: EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
    OUTCOME_BYPASS_WEBHOOK: EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK,
    OUTCOME_BYPASS_CHATOPS: EVENT_BOT_CHALLENGE_BYPASS_CHATOPS,
    OUTCOME_BYPASS_BOOTSTRAP: EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP,
    OUTCOME_BYPASS_PROBE: EVENT_BOT_CHALLENGE_BYPASS_PROBE,
    OUTCOME_BYPASS_IP_ALLOWLIST: EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST,
    OUTCOME_BYPASS_TEST_TOKEN: EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN,
    OUTCOME_JSFAIL_FALLBACK_RECAPTCHA: EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA,
    OUTCOME_JSFAIL_FALLBACK_HCAPTCHA: EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA,
    OUTCOME_JSFAIL_HONEYPOT_PASS: EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS,
    OUTCOME_JSFAIL_HONEYPOT_FAIL: EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL,
})


def event_for_outcome(outcome: str) -> str:
    """Return the canonical ``bot_challenge.*`` event string for an
    outcome literal.  Raises :class:`ValueError` on unknown outcome."""
    try:
        return _OUTCOME_TO_EVENT[outcome]
    except KeyError as exc:
        raise ValueError(f"unknown outcome: {outcome!r}") from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bypass list — AS.0.5 §8.1 + AS.0.6 §2.1 / §2.4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Path-prefix bypass list — AS.0.1 §4.5 inventory.  Frozen set; the
# AS.0.5 §8.1 drift guard test asserts ``expected_paths.issubset(...)``
# so adding a new prefix here is fine, removing one breaks CI.
_BYPASS_PATH_PREFIXES: frozenset[str] = frozenset({
    "/api/v1/livez",
    "/api/v1/readyz",
    "/api/v1/healthz",
    "/api/v1/bootstrap/",
    "/api/v1/webhooks/",
    "/api/v1/chatops/webhook/",
    "/api/v1/auth/oidc/",
    "/api/v1/auth/mfa/challenge",
    "/api/v1/auth/mfa/webauthn/challenge/",
})


# Caller-kind bypass list (AS.0.5 §8.1 + AS.0.6 §2.1).  The audit row
# carries the granular ``caller_kind`` so the dashboard can split
# ``bypass_apikey`` rows by which key family was used.
_BYPASS_CALLER_KINDS: frozenset[str] = frozenset({
    "apikey_omni",
    "apikey_legacy",
    "metrics_token",
})


# Path → bypass-outcome dispatch.  Tested separately so the routing is
# explicit (and so a typo here can't silently route a probe path into
# bypass_bootstrap).  Order matters: the longer prefix wins so
# ``/api/v1/bootstrap/init`` routes to bootstrap not via webhooks.
_PATH_PREFIX_TO_OUTCOME: tuple[tuple[str, str], ...] = (
    ("/api/v1/livez", OUTCOME_BYPASS_PROBE),
    ("/api/v1/readyz", OUTCOME_BYPASS_PROBE),
    ("/api/v1/healthz", OUTCOME_BYPASS_PROBE),
    ("/api/v1/bootstrap/", OUTCOME_BYPASS_BOOTSTRAP),
    ("/api/v1/chatops/webhook/", OUTCOME_BYPASS_CHATOPS),
    ("/api/v1/webhooks/", OUTCOME_BYPASS_WEBHOOK),
    ("/api/v1/auth/oidc/", OUTCOME_BYPASS_PROBE),
    ("/api/v1/auth/mfa/challenge", OUTCOME_BYPASS_PROBE),
    ("/api/v1/auth/mfa/webauthn/challenge/", OUTCOME_BYPASS_PROBE),
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Exceptions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class BotChallengeError(Exception):
    """Base class for all errors this module raises."""


class ProviderConfigError(BotChallengeError):
    """Site secret env unset / empty when verify was attempted."""


class InvalidProviderError(BotChallengeError, ValueError):
    """Caller passed a string that doesn't match any :class:`Provider`."""


class BotChallengeRejected(BotChallengeError):
    """AS.3.4 — raised by :func:`verify_and_enforce` when the request is
    rejected by a confirmed low-score bot challenge (or honeypot fail).

    Carries the underlying :class:`BotChallengeResult` so the caller's
    HTTP layer can:

      * Read :attr:`result.outcome` to log the rejection reason
        (``blocked_lowscore`` vs ``jsfail_honeypot_fail``).
      * Read :attr:`result.audit_event` + :attr:`result.audit_metadata`
        to fan out the audit row before the 429 response goes back.
      * Read :attr:`code` (default :data:`BOT_CHALLENGE_REJECTED_CODE`)
        + :attr:`http_status` (default
        :data:`BOT_CHALLENGE_REJECTED_HTTP_STATUS`) to serialise the
        canonical 429 body without re-deriving them per route.

    Subclassing :class:`BotChallengeError` (not :class:`Exception`
    directly) means a caller's ``except BotChallengeError`` block on
    the :func:`verify` path catches both fail-closed reject and
    transport / config error in one place — useful for telemetry.
    """

    def __init__(
        self,
        result: "BotChallengeResult",
        *,
        code: str = BOT_CHALLENGE_REJECTED_CODE,
        http_status: int = BOT_CHALLENGE_REJECTED_HTTP_STATUS,
    ) -> None:
        self.result = result
        self.code = code
        self.http_status = http_status
        super().__init__(
            f"bot challenge rejected: outcome={result.outcome} "
            f"code={code} http_status={http_status}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen public dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class ProviderResponse:
    """Normalised raw response from one of the four siteverify
    endpoints.  Vendors return slightly different JSON shapes; this
    structure flattens them onto a single contract before classification.

    Attributes
    ----------
    success
        Vendor's ``success`` boolean.  ``True`` ⇒ token was structurally
        valid and not a known forgery; the *separate* score gate then
        decides whether to treat the user as bot or human.
    score
        Normalised 0.0 – 1.0 confidence score.  Calibration:

          * Turnstile → vendor's ``score`` (Cloudflare ships 0/0.5/1
            buckets internally; we trust the float).
          * reCAPTCHA v3 → vendor's ``score`` (continuous 0.0–1.0).
          * reCAPTCHA v2 → 1.0 on success / 0.0 on failure (binary,
            checkbox solved or not).
          * hCaptcha → 1.0 on success / 0.0 on failure (binary, no
            score in standard plan; Enterprise ``score`` not used here
            to avoid plan-tier branching).
    action
        Vendor-supplied action label echoed back from the widget
        (``login`` / ``signup`` / ``pwreset`` / ``contact``).  Used
        for reCAPTCHA v3 binding-action check (the action the widget
        rendered must match the action we expect, else we suspect
        token replay).
    hostname
        Vendor's hostname echo (where the widget rendered).  Caller
        validates against expected origin in AS.6.3.
    raw
        Full vendor JSON for forensic / debug purposes.  Treat as
        read-only — frozen dataclass.
    error_codes
        Tuple of vendor ``error-codes`` strings (e.g.
        ``invalid-input-secret``, ``timeout-or-duplicate``).  Empty
        tuple on success.
    """

    success: bool
    score: float
    action: Optional[str]
    hostname: Optional[str]
    raw: Mapping[str, Any]
    error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BypassReason:
    """A single bypass-axis hit with the metadata the audit row needs.

    Constructed by :func:`evaluate_bypass`.  ``outcome`` is one of the
    seven ``OUTCOME_BYPASS_*`` literals; ``audit_metadata`` is fed
    verbatim into the audit row's ``after`` JSON.
    """

    outcome: str
    audit_metadata: Mapping[str, Any]
    # Lower-precedence axes that *also* matched on this request — kept
    # for the AS.0.6 §4 ``also_matched`` audit field so ops can grep
    # belt-and-braces wiring.
    also_matched: tuple[str, ...] = ()


@dataclass(frozen=True)
class BotChallengeResult:
    """Final result returned by :func:`verify` to the caller.

    The caller (AS.6.3 OmniSight self login / signup / password-reset
    / contact form, or generated-app form) reads ``allow`` to decide
    whether to continue or 4xx the request, then optionally fans the
    full result into the audit emitter and metrics widget.

    Attributes
    ----------
    outcome
        One of the 15 :data:`ALL_OUTCOMES` literals.
    allow
        Whether the caller should permit the underlying action
        (login / signup / etc.) to proceed.  ``True`` for ``pass`` /
        all ``bypass_*`` / ``unverified_*`` (Phase 1/2 fail-open or
        Phase 3 server-error fail-open) / ``jsfail_honeypot_pass``;
        ``False`` only for ``blocked_lowscore`` (Phase 3 confirmed
        bot) and ``jsfail_honeypot_fail`` (honeypot caught the bot).
    score
        The classification score (0.0 – 1.0).  ``1.0`` for bypass /
        passthrough; the vendor's normalized score for verify outcomes.
    provider
        The vendor that produced the score, or ``None`` if the result
        came from a bypass / passthrough path (no vendor was called).
    audit_event
        The canonical ``bot_challenge.*`` event string the caller
        should emit.  Pre-computed for caller convenience.
    audit_metadata
        ``metadata`` JSON for the audit row.  Always present (frozen
        empty mapping if there's nothing to record).  Caller merges
        with its own per-route metadata before emit.
    error
        Free-text error description for the ``unverified_servererr``
        path (transport / 5xx / DNS).  ``None`` otherwise.
    """

    outcome: str
    allow: bool
    score: float
    provider: Optional[Provider]
    audit_event: str
    audit_metadata: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled — AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1 noop
    matrix.

    Reads ``settings.as_enabled`` via ``getattr`` fallback so the lib
    works before AS.3.1 lands the field on
    :class:`backend.config.Settings` (forward-promotion guard).
    Default ``True`` — AS feature family is on unless explicitly
    disabled.
    """
    try:
        from backend.config import settings  # local import to avoid module-import side effect
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  passthrough — knob-off / dev-mode short-circuit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def passthrough(*, reason: str = "knob_off") -> BotChallengeResult:
    """Return a permissive result that does NOT write any audit row.

    Used by:

    * ``OMNISIGHT_AS_ENABLED=false`` (AS.0.8 single-knob global rollback
      — ``is_enabled()`` returns False).
    * ``OMNISIGHT_AUTH_MODE=open`` (dev / test — caller checks the env
      itself before calling :func:`verify`, which then routes through
      this helper for shape consistency).

    The ``audit_event`` is set to :data:`EVENT_BOT_CHALLENGE_PASS` and
    ``audit_metadata`` carries the bypass reason for *grep-only*
    debugging (the caller's emitter MUST NOT actually fan this out
    when knob-off — see AS.0.5 §4 precedence axis #2).  ``allow=True``,
    ``score=1.0``, no provider attribution.
    """
    return BotChallengeResult(
        outcome=OUTCOME_PASS,
        allow=True,
        score=1.0,
        provider=None,
        audit_event=EVENT_BOT_CHALLENGE_PASS,
        audit_metadata={"passthrough_reason": reason},
        error=None,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bypass evaluation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _path_bypass(path: Optional[str]) -> Optional[tuple[str, str]]:
    """Return ``(matched_prefix, outcome)`` for a path bypass, or
    ``None`` if the path doesn't hit any bypass prefix."""
    if not path:
        return None
    # Longest-prefix wins.
    matches = [
        (prefix, outcome)
        for (prefix, outcome) in _PATH_PREFIX_TO_OUTCOME
        if path.startswith(prefix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda t: len(t[0]), reverse=True)
    return matches[0]


def _ip_in_allowlist(client_ip: Optional[str], allowlist: tuple[str, ...]) -> Optional[str]:
    """Return the matching CIDR string if *client_ip* is inside any
    entry of *allowlist*; ``None`` otherwise.  Mirrors AS.0.6 §2.2
    pseudocode — corrupt entries are skipped (logged warn), parse
    failures fall through (no bypass)."""
    if not client_ip or not allowlist:
        return None
    try:
        ip = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return None
    for entry in allowlist:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning("bot_challenge: corrupt allowlist entry skipped: %r", entry)
            continue
        if ip.version == net.version and ip in net:
            return entry
    return None


def _is_wide_cidr(cidr: str) -> bool:
    """Whether a CIDR is "wide" per AS.0.6 §2.2 - /0..24 IPv4 or /0..48
    IPv6.  Returns False on parse error (caller treats unparseable as
    not-wide; we already logged warn upstream)."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    if net.version == 4:
        return net.prefixlen <= 24
    return net.prefixlen <= 48


def _subnet_prefix(client_ip: str) -> str:
    """AS.0.1 ``_subnet_prefix`` convention - /24 for IPv4, /64 for
    IPv6 - used as the audit-row's ``client_ip_subnet`` to avoid leaking
    the full IP while keeping enough selectivity for grep."""
    try:
        ip = ipaddress.ip_address(client_ip.strip())
    except ValueError:
        return "invalid"
    if ip.version == 4:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def _test_token_matches(header_value: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time compare of test-token header.  Returns False if
    either side is unset / empty / shorter than 32 chars (AS.0.6 §2.3
    min-length invariant — ``< 32 chars`` is treated as unset to make
    accidental short-token misconfig fail closed on the bypass axis)."""
    if not header_value or not expected:
        return False
    if len(expected) < 32:
        return False
    return secrets.compare_digest(header_value, expected)


def _fingerprint(value: str) -> str:
    """Last-12-chars SHA-256 fingerprint convention (mirrors AS.1.4
    :func:`oauth_audit.fingerprint`)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class BypassContext:
    """Inputs to :func:`evaluate_bypass`.

    Built by the caller from request-context: the route's path, the
    authenticated principal's ``caller_kind`` (None for anonymous /
    cookie-session), the trust-chain client IP, the per-tenant CIDR
    allowlist (loaded from ``tenants.auth_features.automation_ip_allowlist``),
    and the request's ``X-OmniSight-Test-Token`` header value (if any).
    """

    path: Optional[str] = None
    caller_kind: Optional[str] = None
    api_key_id: Optional[str] = None
    api_key_prefix: Optional[str] = None
    client_ip: Optional[str] = None
    tenant_ip_allowlist: tuple[str, ...] = ()
    test_token_header_value: Optional[str] = None
    test_token_expected: Optional[str] = None
    tenant_id: Optional[str] = None
    widget_action: Optional[str] = None


def evaluate_bypass(ctx: BypassContext) -> Optional[BypassReason]:
    """Walk the AS.0.6 §4 axis-internal precedence and return the
    highest-precedence bypass match (or ``None``).

    Precedence (highest → lowest):

      1. **A — API key auth** (``caller_kind`` ∈ ``_BYPASS_CALLER_KINDS``)
      2. **C — Test-token header** (header value matches env, ≥32 chars)
      3. **B — IP allowlist** (client IP in tenant's CIDR allowlist)
      4. **Path bypass** (route in ``_BYPASS_PATH_PREFIXES``)

    Multi-axis matches: only the highest-precedence axis emits the
    bypass row; the others land in :attr:`BypassReason.also_matched`
    so ops can grep belt-and-braces config.
    """
    matches: list[tuple[str, BypassReason]] = []

    # Axis A — API key auth.
    if ctx.caller_kind and ctx.caller_kind in _BYPASS_CALLER_KINDS:
        meta: dict[str, Any] = {"caller_kind": ctx.caller_kind}
        if ctx.api_key_id:
            meta["key_id"] = ctx.api_key_id
        if ctx.api_key_prefix:
            meta["key_prefix"] = ctx.api_key_prefix
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        matches.append(("apikey", BypassReason(
            outcome=OUTCOME_BYPASS_APIKEY,
            audit_metadata=meta,
        )))

    # Axis C — Test-token header.
    if _test_token_matches(ctx.test_token_header_value, ctx.test_token_expected):
        meta = {
            "token_fp": _fingerprint(ctx.test_token_header_value or ""),
            "tenant_id_or_null": ctx.tenant_id,
        }
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        matches.append(("test_token", BypassReason(
            outcome=OUTCOME_BYPASS_TEST_TOKEN,
            audit_metadata=meta,
        )))

    # Axis B — IP allowlist.
    matched_cidr = _ip_in_allowlist(ctx.client_ip, ctx.tenant_ip_allowlist)
    if matched_cidr is not None and ctx.client_ip is not None:
        meta = {
            "cidr_match": matched_cidr,
            "client_ip_subnet": _subnet_prefix(ctx.client_ip),
            "wide_cidr": _is_wide_cidr(matched_cidr),
        }
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        matches.append(("ip_allowlist", BypassReason(
            outcome=OUTCOME_BYPASS_IP_ALLOWLIST,
            audit_metadata=meta,
        )))

    # Axis D — Path prefix.
    path_hit = _path_bypass(ctx.path)
    if path_hit is not None:
        prefix, outcome = path_hit
        meta = {"matched_prefix": prefix}
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        matches.append(("path", BypassReason(
            outcome=outcome,
            audit_metadata=meta,
        )))

    if not matches:
        return None

    # AS.0.6 §4 precedence: A > C > B > D
    precedence = {"apikey": 0, "test_token": 1, "ip_allowlist": 2, "path": 3}
    matches.sort(key=lambda m: precedence[m[0]])
    head_axis, head_reason = matches[0]
    other_axes = tuple(axis for (axis, _) in matches[1:])
    if other_axes:
        merged_meta = dict(head_reason.audit_metadata)
        merged_meta["also_matched"] = list(other_axes)
        return BypassReason(
            outcome=head_reason.outcome,
            audit_metadata=merged_meta,
            also_matched=other_axes,
        )
    return head_reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider verifiers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Type alias: an HTTP transport callable the caller may inject
# (production = ``httpx.AsyncClient.post``).  Tests inject a fake to
# avoid network calls.
HttpPost = Callable[..., Awaitable["httpx.Response"]]


def _normalise_provider(value: Any) -> Provider:
    """Coerce a string / Provider value into a :class:`Provider`."""
    if isinstance(value, Provider):
        return value
    if isinstance(value, str):
        try:
            return Provider(value)
        except ValueError as exc:
            raise InvalidProviderError(f"unknown provider: {value!r}") from exc
    raise InvalidProviderError(f"provider must be Provider or str, got {type(value).__name__}")


def _parse_response(provider: Provider, payload: Mapping[str, Any]) -> ProviderResponse:
    """Normalise a raw siteverify JSON payload into :class:`ProviderResponse`.

    Score calibration:
      * Turnstile / reCAPTCHA v3 — vendor's ``score`` field (float).
      * reCAPTCHA v2 / hCaptcha — binary 1.0 on success, 0.0 on failure.

    All four vendors set ``success: bool`` + ``error-codes: list[str]``.
    Hostname / action are vendor-specific:
      * Turnstile: ``hostname`` + ``action`` (called ``cdata`` is custom
        data, not the action — we ignore it and key on ``action``).
      * reCAPTCHA v3: ``hostname`` + ``action``.
      * reCAPTCHA v2: ``hostname`` only, no action.
      * hCaptcha: ``hostname`` only, no action.
    """
    success = bool(payload.get("success", False))
    raw_score = payload.get("score")
    if provider in (Provider.TURNSTILE, Provider.RECAPTCHA_V3) and isinstance(raw_score, (int, float)):
        # Clamp to [0.0, 1.0] in case the vendor ever ships a value
        # outside the documented band.
        score = max(0.0, min(1.0, float(raw_score)))
    else:
        # Binary providers (reCAPTCHA v2, hCaptcha) and any provider
        # that omitted score for whatever reason.
        score = 1.0 if success else 0.0
    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    hostname = payload.get("hostname") if isinstance(payload.get("hostname"), str) else None
    raw_codes = payload.get("error-codes") or payload.get("error_codes") or []
    error_codes: tuple[str, ...] = tuple(str(c) for c in raw_codes if isinstance(c, str))
    # Deep-freeze raw payload by shallow-copying into an immutable dict-
    # equivalent (we trust callers to treat the field as read-only).
    return ProviderResponse(
        success=success,
        score=score,
        action=action,
        hostname=hostname,
        raw=dict(payload),
        error_codes=error_codes,
    )


async def verify_provider(
    *,
    provider: Provider,
    token: str,
    secret: str,
    remote_ip: Optional[str] = None,
    expected_action: Optional[str] = None,
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    http_client: Optional["httpx.AsyncClient"] = None,
) -> ProviderResponse:
    """Server-side ``siteverify`` call against *provider*.

    Sends ``secret`` + ``response=<token>`` (+ optional ``remoteip``)
    to the provider's siteverify endpoint, parses the JSON, and returns
    a normalised :class:`ProviderResponse`.

    Parameters
    ----------
    provider
        Which vendor to call.  See :class:`Provider`.
    token
        The widget-issued token from the client (Turnstile
        ``cf-turnstile-response`` / reCAPTCHA ``g-recaptcha-response``
        / hCaptcha ``h-captcha-response``).
    secret
        The site-secret for *provider*, loaded by the caller from
        :func:`secret_env_for`.  Empty string raises
        :class:`ProviderConfigError` to fail-fast on misconfiguration.
    remote_ip
        Optional client IP to forward (RFC 6749-style provenance).
        Most vendors accept it as a soft signal.
    expected_action
        Optional action label (``login`` / ``signup`` / ``pwreset`` /
        ``contact``) the widget rendered with.  If given AND the
        provider returned an action, the values must match.  A
        mismatch demotes the result to ``success=False`` with
        ``error_codes += ("action-mismatch",)`` (anti-replay across
        forms).  reCAPTCHA v2 / hCaptcha don't ship action; mismatch
        check is no-op for them.
    timeout_seconds
        Per-call HTTP timeout.  Default 3 s.
    http_client
        Optional :class:`httpx.AsyncClient` for transport injection
        (tests / shared connection pooling).  When omitted the function
        opens a fresh client per call.

    Raises
    ------
    ProviderConfigError
        If *secret* is empty.
    """
    if not secret:
        raise ProviderConfigError(
            f"site secret for {provider.value} is empty (env "
            f"{secret_env_for(provider)} unset)"
        )
    if not token:
        # Empty token isn't a config error — vendor will say success=False;
        # we return a synthetic failure shape without making the call.
        return ProviderResponse(
            success=False,
            score=0.0,
            action=None,
            hostname=None,
            raw={"success": False, "error-codes": ["missing-input-response"]},
            error_codes=("missing-input-response",),
        )
    url = SITEVERIFY_URLS[provider]
    form: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        form["remoteip"] = remote_ip

    async def _post(client: "httpx.AsyncClient") -> "httpx.Response":
        return await client.post(url, data=form, timeout=timeout_seconds)

    if http_client is not None:
        resp = await _post(http_client)
    else:
        async with httpx.AsyncClient() as client:
            resp = await _post(client)
    if resp.status_code >= 500:
        raise BotChallengeError(
            f"siteverify {provider.value} returned {resp.status_code}"
        )
    if resp.status_code >= 400:
        # 400 from vendor is a "your request was malformed" — surface
        # as failure (not exception) so caller can map onto
        # unverified_servererr.
        return ProviderResponse(
            success=False,
            score=0.0,
            action=None,
            hostname=None,
            raw={"success": False, "error-codes": [f"http-{resp.status_code}"]},
            error_codes=(f"http-{resp.status_code}",),
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise BotChallengeError(
            f"siteverify {provider.value} returned non-JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise BotChallengeError(
            f"siteverify {provider.value} returned non-object JSON: {type(payload).__name__}"
        )
    parsed = _parse_response(provider, payload)
    # Action-mismatch demotion (reCAPTCHA v3 + Turnstile only — they
    # echo the action; v2 / hCaptcha don't).
    if (
        expected_action is not None
        and parsed.action is not None
        and parsed.action != expected_action
    ):
        return ProviderResponse(
            success=False,
            score=0.0,
            action=parsed.action,
            hostname=parsed.hostname,
            raw=parsed.raw,
            error_codes=parsed.error_codes + ("action-mismatch",),
        )
    return parsed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Phase-aware classifier (AS.0.5 §2 phase matrix)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def classify_outcome(
    response: ProviderResponse,
    *,
    provider: Provider,
    phase: int,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    widget_action: Optional[str] = None,
) -> BotChallengeResult:
    """Turn a provider response into the final :class:`BotChallengeResult`
    per AS.0.5 §2 phase matrix.

    ``phase`` is an int 1 / 2 / 3 corresponding to the three live
    phases (Phase 0 is "Turnstile not deployed" — the caller wouldn't
    reach this function at all; we accept ``phase=1`` as the default).

    Phase 1 / Phase 2: fail-open everywhere.  Low score → ``unverified_lowscore``,
    server error → ``unverified_servererr``; both ``allow=True``.

    Phase 3: fail-closed for confirmed low score (vendor said success
    AND score < threshold → ``blocked_lowscore`` ``allow=False``);
    server error stays fail-open (our-side fault).

    Server-error detection: ``response.success=False`` *combined with*
    a score of 0.0 means the vendor rejected the token (unverified
    server-side); the audit row carries ``error_kind`` derived from
    ``error_codes`` for ops grep.
    """
    if phase not in (1, 2, 3):
        raise ValueError(f"phase must be 1/2/3, got {phase!r}")

    metadata: dict[str, Any] = {
        "provider": provider.value,
        "score": response.score,
    }
    if widget_action is not None:
        metadata["widget_action"] = widget_action

    if response.success and response.score >= score_threshold:
        return BotChallengeResult(
            outcome=OUTCOME_PASS,
            allow=True,
            score=response.score,
            provider=provider,
            audit_event=EVENT_BOT_CHALLENGE_PASS,
            audit_metadata=metadata,
        )

    if response.success and response.score < score_threshold:
        # Confirmed low score (token was valid but vendor's risk model
        # flagged the user as bot-suspect).
        if phase == 3:
            return BotChallengeResult(
                outcome=OUTCOME_BLOCKED_LOWSCORE,
                allow=False,
                score=response.score,
                provider=provider,
                audit_event=EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
                audit_metadata=metadata,
            )
        return BotChallengeResult(
            outcome=OUTCOME_UNVERIFIED_LOWSCORE,
            allow=True,
            score=response.score,
            provider=provider,
            audit_event=EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
            audit_metadata=metadata,
        )

    # response.success == False — server-side verify error.  Fail-open
    # for ALL phases (Phase 3 too — our-side fault, not user fault per
    # AS.0.5 §2.4 row 3).  Caller can read error_codes via metadata.
    error_kind = _classify_error_kind(response.error_codes)
    metadata["error_kind"] = error_kind
    if response.error_codes:
        metadata["error_codes"] = list(response.error_codes)
    return BotChallengeResult(
        outcome=OUTCOME_UNVERIFIED_SERVERERR,
        allow=True,
        score=response.score,
        provider=provider,
        audit_event=EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
        audit_metadata=metadata,
        error=", ".join(response.error_codes) if response.error_codes else None,
    )


def _classify_error_kind(error_codes: tuple[str, ...]) -> str:
    """Map vendor-specific error codes onto a coarse ``error_kind``
    label (per AS.0.5 §3 metadata schema): ``timeout`` / ``5xx`` /
    ``4xx_invalid_token`` / ``dns_fail`` / ``unknown``."""
    if not error_codes:
        return "unknown"
    codes = [c.lower() for c in error_codes]
    if any("timeout" in c or "duplicate" in c for c in codes):
        return "timeout"
    if any(c.startswith("http-5") for c in codes):
        return "5xx"
    if any(c.startswith("http-4") for c in codes):
        return "4xx_invalid_token"
    if any("invalid" in c for c in codes):
        return "4xx_invalid_token"
    if any("missing" in c for c in codes):
        return "4xx_invalid_token"
    return "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Top-level verify()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class VerifyContext:
    """Inputs to :func:`verify`.

    Built by the caller's request handler.  Most fields are optional —
    the bypass branch only consults the bypass-axis fields, the verify
    branch only consults the token / provider / secret fields.
    """

    provider: Provider
    token: Optional[str] = None
    secret: Optional[str] = None
    phase: int = 1
    widget_action: Optional[str] = None
    expected_action: Optional[str] = None
    remote_ip: Optional[str] = None
    score_threshold: float = DEFAULT_SCORE_THRESHOLD
    timeout_seconds: float = DEFAULT_VERIFY_TIMEOUT_SECONDS
    bypass: BypassContext = field(default_factory=BypassContext)


async def verify(
    ctx: VerifyContext,
    *,
    http_client: Optional["httpx.AsyncClient"] = None,
) -> BotChallengeResult:
    """End-to-end orchestrator: knob → bypass → provider verify →
    classify.

    Returns a :class:`BotChallengeResult` the caller acts on
    (``allow`` decides 4xx vs continue; ``audit_event`` +
    ``audit_metadata`` feeds the audit emitter; ``score`` +
    ``provider`` feed the AS.5.2 dashboard).  Never raises on the
    happy / fail paths — only the misconfiguration corner
    (:class:`ProviderConfigError`) propagates.

    Order of evaluation (AS.0.5 §4 precedence):

      1. Knob off ⇒ :func:`passthrough`.
      2. Bypass list match ⇒ :class:`BypassReason` → result.
      3. Provider verify call.
      4. Phase-aware classification.
    """
    if not is_enabled():
        return passthrough(reason="knob_off")

    bypass = evaluate_bypass(ctx.bypass)
    if bypass is not None:
        meta = dict(bypass.audit_metadata)
        if ctx.widget_action and "widget_action" not in meta:
            meta["widget_action"] = ctx.widget_action
        return BotChallengeResult(
            outcome=bypass.outcome,
            allow=True,
            score=1.0,
            provider=None,
            audit_event=event_for_outcome(bypass.outcome),
            audit_metadata=meta,
        )

    # Provider verify branch.
    if ctx.secret is None or ctx.secret == "":
        # Treat unset secret as a server error (fail-open per AS.0.5
        # §2.4 row 3).  This is the correct behaviour when an operator
        # forgot to set the env — block-ALL behaviour would lock every
        # user out of login.
        meta = {
            "provider": ctx.provider.value,
            "score": 0.0,
            "error_kind": "config_missing_secret",
        }
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        return BotChallengeResult(
            outcome=OUTCOME_UNVERIFIED_SERVERERR,
            allow=True,
            score=0.0,
            provider=ctx.provider,
            audit_event=EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
            audit_metadata=meta,
            error=f"{secret_env_for(ctx.provider)} unset",
        )

    try:
        response = await verify_provider(
            provider=ctx.provider,
            token=ctx.token or "",
            secret=ctx.secret,
            remote_ip=ctx.remote_ip,
            expected_action=ctx.expected_action,
            timeout_seconds=ctx.timeout_seconds,
            http_client=http_client,
        )
    except (httpx.RequestError, BotChallengeError) as exc:
        # Transport failure / 5xx / non-JSON body — treat as server
        # error fail-open per AS.0.5 §2.4 row 3.
        meta = {
            "provider": ctx.provider.value,
            "score": 0.0,
            "error_kind": "timeout" if isinstance(exc, httpx.TimeoutException) else "5xx",
        }
        if ctx.widget_action:
            meta["widget_action"] = ctx.widget_action
        return BotChallengeResult(
            outcome=OUTCOME_UNVERIFIED_SERVERERR,
            allow=True,
            score=0.0,
            provider=ctx.provider,
            audit_event=EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
            audit_metadata=meta,
            error=type(exc).__name__,
        )

    return classify_outcome(
        response,
        provider=ctx.provider,
        phase=ctx.phase,
        score_threshold=ctx.score_threshold,
        widget_action=ctx.widget_action,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.3.4 — reject enforcement primitives
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def should_reject(result: BotChallengeResult) -> bool:
    """Whether AS.3.4 enforcement should turn *result* into an HTTP 429.

    Pure predicate — the answer is always ``not result.allow``.  Kept as
    a separate, named helper so callers can preview the decision (e.g.
    fan a metric / probe header BEFORE choosing to raise) without
    duplicating the ``allow`` semantics in every route.

    True for:

      * :data:`OUTCOME_BLOCKED_LOWSCORE` — Phase 3 confirmed low score
        (vendor said success AND score < threshold).  This is the only
        outcome the AS.0.5 §2 phase matrix marks as fail-closed.
      * :data:`OUTCOME_JSFAIL_HONEYPOT_FAIL` — honeypot caught the bot
        (AS.0.7 §3.4 fail-closed branch). The classifier here doesn't
        construct this outcome itself; AS.3.5 / AS.0.7 wire the
        honeypot fallback layer that emits this result.

    False for everything else, including ``unverified_*`` server errors
    (which stay fail-open even in Phase 3 per AS.0.5 §2.4 row 3) and
    every ``bypass_*`` outcome.

    Module-global state audit (per implement_phase_step.md SOP §1):
    pure function — reads no mutable state, makes no IO, has no side
    effects.  Cross-worker consistency: every uvicorn worker derives the
    same boolean from the same input dataclass (answer #1 of SOP §1).
    """
    return not result.allow


async def verify_and_enforce(
    ctx: VerifyContext,
    *,
    http_client: Optional["httpx.AsyncClient"] = None,
) -> BotChallengeResult:
    """End-to-end verify + reject enforcement (AS.3.4 single-call entry).

    Runs :func:`verify` to compute a :class:`BotChallengeResult`, then
    if :func:`should_reject` says yes, raises :class:`BotChallengeRejected`
    carrying the result so the caller's HTTP layer can serialise a
    canonical 429 ``bot_challenge_failed`` response.

    On allow=True paths (``pass`` / ``unverified_*`` fail-open /
    ``bypass_*`` / ``jsfail_*``-but-not-fail), returns the result for
    the caller to fan into the audit emitter and continue the request.

    AS.0.5 §3 wire-up table (the routes AS.6.3 lands consume this):

      * ``outcome=blocked_lowscore``      → raise (HTTP 429
        ``bot_challenge_failed``, audit row before response)
      * ``outcome=jsfail_honeypot_fail``  → raise (same shape)
      * everything else                   → return result, caller emits
        audit + continues the underlying action

    Module-global state audit (per implement_phase_step.md SOP §1):
    delegates entirely to :func:`verify` (no extra module-global state
    introduced) + :func:`should_reject` (pure predicate).  Same
    cross-worker semantics as :func:`verify`: stateless RPC to the
    vendor's siteverify, reject decision derived from the response.

    Read-after-write timing audit (per implement_phase_step.md SOP §1):
    N/A — verify is per-request, stateless RPC; reject decision is
    computed in-process from the response.  No DB write, no shared
    in-memory state, no read-after-write race.
    """
    result = await verify(ctx, http_client=http_client)
    if should_reject(result):
        raise BotChallengeRejected(result)
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider selection (AS.3.3 region + ecosystem heuristic)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# GDPR strict regions — ISO 3166-1 alpha-2 country codes for which the
# provider-selection heuristic prefers hCaptcha. The list covers:
#
#   * EU 27 member states (the GDPR's home jurisdiction).
#   * EEA additions (Iceland, Liechtenstein, Norway — bound by GDPR via
#     the EEA agreement).
#   * UK (post-Brexit "UK GDPR" mirrors EU GDPR substantively).
#   * Switzerland (FADP / nFADP closely tracks GDPR adequacy).
#
# Why hCaptcha for these regions: the vendor publishes an EU data-
# residency option and is positioned as the privacy-first alternative
# to Google reCAPTCHA + Cloudflare Turnstile (both of which involve
# cross-border data transfers that operators in these regions often
# need extra paperwork to defend). Operators can still override per
# tenant via the ``override`` arg if they have a different vendor
# preference; this is a *default* heuristic, not a policy lock.
#
# Frozen frozenset — module-level constant, no mutation possible at
# runtime; satisfies SOP §1 "no module-level mutable containers"
# invariant. Cross-twin parity guard locks the codes byte-for-byte
# against the TS twin's same-named export.
GDPR_STRICT_REGIONS: frozenset[str] = frozenset({
    # EU 27
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT",
    "NL", "PL", "PT", "RO", "SE", "SI", "SK",
    # EEA additions
    "IS", "LI", "NO",
    # UK + Switzerland
    "GB", "CH",
})


# Ecosystem hint that routes to reCAPTCHA v3 — caller passes the
# canonical lowercase string ``"google"`` when the principal already
# has a Google OAuth link or is signing in from a Google Workspace
# email domain. Keeping the hint vocabulary tiny + lowercase keeps the
# cross-twin parity guard simple (no case folding issues across
# Python's ``str.lower()`` and TS's ``String.prototype.toLowerCase``).
ECOSYSTEM_HINT_GOOGLE: str = "google"


def is_gdpr_strict_region(region: str) -> bool:
    """Whether *region* is in the AS.3.3 GDPR-strict region list.

    Case-insensitive, surrounding whitespace tolerated. Returns
    ``False`` for empty / non-ISO inputs (the caller doesn't have a
    region hint → fall through to the next axis of the heuristic).
    """
    if not region:
        return False
    return region.strip().upper() in GDPR_STRICT_REGIONS


def pick_provider(
    *,
    default: Provider = Provider.TURNSTILE,
    region: Optional[str] = None,
    ecosystem_hints: Iterable[str] = (),
    override: Optional[Provider] = None,
) -> Provider:
    """Pick a captcha provider per AS.3.3 region + ecosystem heuristic.

    Precedence (highest first):

      1. ``override`` — caller-supplied force value (e.g. per-tenant
         admin pin loaded from ``tenants.auth_features.captcha_provider``).
         Wins unconditionally; lets ops override the heuristic without
         modifying caller code.
      2. **GDPR strict region** (``region`` ∈ :data:`GDPR_STRICT_REGIONS`)
         → :data:`Provider.HCAPTCHA`. Privacy-first vendor; sidesteps
         the Cloudflare / Google cross-border data-transfer paperwork
         most EU/EEA/UK/CH operators need to file.
      3. **Google ecosystem hint** (``"google"`` ∈ ``ecosystem_hints``)
         → :data:`Provider.RECAPTCHA_V3`. UX continuity: the principal
         already accepted Google's data-collection terms via OAuth, so
         routing them through reCAPTCHA preserves the same vendor
         relationship rather than introducing a second one.
      4. **Default** → ``default`` (defaults to :data:`Provider.TURNSTILE`).

    Parameters
    ----------
    default
        The provider to fall back to when no heuristic axis fires.
        Caller can override this per call to e.g. force Turnstile in
        every non-EU tenant. Defaults to :data:`Provider.TURNSTILE` —
        the AS.0.5 fail-open phased strategy chose Turnstile as the
        family default because it's privacy-friendly without GDPR
        paperwork burden and has the best CDN coverage outside China.
    region
        ISO 3166-1 alpha-2 country code derived from the request's
        Cloudflare ``CF-IPCountry`` header (or any equivalent
        geo-IP hint the caller has). Case-insensitive. ``None`` /
        empty string → axis #2 doesn't fire.
    ecosystem_hints
        Iterable of canonical lowercase ecosystem strings the caller
        knows about — currently only :data:`ECOSYSTEM_HINT_GOOGLE`
        (``"google"``) is acted on. Caller passes ``("google",)`` when
        the principal has an active Google OAuth link or is signing in
        from a Google Workspace domain. Empty iterable → axis #3 doesn't
        fire. Future ecosystem hints (e.g. ``"microsoft"`` →
        Microsoft's bot-challenge) can be added without a signature
        change.
    override
        Caller-supplied force value — wins unconditionally over every
        heuristic axis. Use case: a tenant admin set a per-tenant
        captcha_provider override in their auth_features row, and the
        caller wants the heuristic to honour it without re-implementing
        priority logic. ``None`` → axis #1 doesn't fire.

    Returns
    -------
    Provider
        The selected provider enum value.

    Module-global state audit (per implement_phase_step.md SOP §1)
    ──────────────────────────────────────────────────────────────
    * Pure function — reads no mutable state, makes no IO, has no side
      effects. Cross-worker consistency: every uvicorn worker derives
      the same return value from the same input args (answer #1 of
      SOP §1).
    * :data:`GDPR_STRICT_REGIONS` is a frozenset constant; no
      module-level mutable container. The TS twin's same-named export
      mirrors the codes byte-for-byte (cross-twin drift guard locks).
    """
    if override is not None:
        return override
    if region and is_gdpr_strict_region(region):
        return Provider.HCAPTCHA
    for hint in ecosystem_hints:
        if hint and hint.lower() == ECOSYSTEM_HINT_GOOGLE:
            return Provider.RECAPTCHA_V3
    return default


__all__ = [
    # Provider enum / constants
    "Provider",
    "SITEVERIFY_URLS",
    "secret_env_for",
    "DEFAULT_SCORE_THRESHOLD",
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "TEST_TOKEN_HEADER",
    # Audit event constants (15 verify+bypass + 4 phase)
    "EVENT_BOT_CHALLENGE_PASS",
    "EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE",
    "EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR",
    "EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE",
    "EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA",
    "EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA",
    "EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS",
    "EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL",
    "EVENT_BOT_CHALLENGE_BYPASS_APIKEY",
    "EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK",
    "EVENT_BOT_CHALLENGE_BYPASS_CHATOPS",
    "EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP",
    "EVENT_BOT_CHALLENGE_BYPASS_PROBE",
    "EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST",
    "EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN",
    "EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2",
    "EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3",
    "EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2",
    "EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1",
    "ALL_BOT_CHALLENGE_EVENTS",
    # Outcome literals
    "OUTCOME_PASS",
    "OUTCOME_UNVERIFIED_LOWSCORE",
    "OUTCOME_UNVERIFIED_SERVERERR",
    "OUTCOME_BLOCKED_LOWSCORE",
    "OUTCOME_BYPASS_APIKEY",
    "OUTCOME_BYPASS_WEBHOOK",
    "OUTCOME_BYPASS_CHATOPS",
    "OUTCOME_BYPASS_BOOTSTRAP",
    "OUTCOME_BYPASS_PROBE",
    "OUTCOME_BYPASS_IP_ALLOWLIST",
    "OUTCOME_BYPASS_TEST_TOKEN",
    "OUTCOME_JSFAIL_FALLBACK_RECAPTCHA",
    "OUTCOME_JSFAIL_FALLBACK_HCAPTCHA",
    "OUTCOME_JSFAIL_HONEYPOT_PASS",
    "OUTCOME_JSFAIL_HONEYPOT_FAIL",
    "ALL_OUTCOMES",
    "event_for_outcome",
    # Dataclasses
    "ProviderResponse",
    "BypassReason",
    "BypassContext",
    "BotChallengeResult",
    "VerifyContext",
    # Exceptions
    "BotChallengeError",
    "ProviderConfigError",
    "InvalidProviderError",
    "BotChallengeRejected",
    # Public functions
    "is_enabled",
    "passthrough",
    "evaluate_bypass",
    "verify_provider",
    "classify_outcome",
    "verify",
    "pick_provider",
    # AS.3.3 provider-selection helpers
    "GDPR_STRICT_REGIONS",
    "ECOSYSTEM_HINT_GOOGLE",
    "is_gdpr_strict_region",
    # AS.3.4 reject enforcement primitives
    "BOT_CHALLENGE_REJECTED_CODE",
    "BOT_CHALLENGE_REJECTED_HTTP_STATUS",
    "should_reject",
    "verify_and_enforce",
]

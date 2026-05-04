"""AS.4.1 — Honeypot field generator + bot detection (Python side).

Server-side helper for the AS.4 honeypot family.  Provides the canonical
hidden-form-field name generator + form-submission validator that the
four OmniSight self forms (login / signup / password-reset / contact)
and the AS.7.x scaffolded apps wire onto.  The 5-attribute spec
(rare field name + off-screen CSS hide + ``tabindex="-1"`` +
``autocomplete="off"`` + ``aria-hidden="true"``) is frozen by AS.0.7
§2 and mirrored byte-for-byte in the TS twin.

Plan / spec source
──────────────────
* ``docs/security/as_0_7_honeypot_field_design.md``
  — 12-word rare pool (§2.1), 4 form-prefix mapping (§2.1 / §4.1),
  5-attribute hidden-field invariant (§2), validate helper interface
  (§3.1), bypass short-circuit precedence (§3.3), 3-event audit
  family (§3.4), 30-day rotation epoch (§2.1), AS.0.5 phase metric
  decoupling (§3.5), AS.0.6 bypass interaction (§3.3),
  AS.0.8 single-knob noop (§4.3), drift guards (§8).
* ``docs/security/as_0_5_turnstile_fail_open_phased_strategy.md``
  §3.5 / §5 — chain terminal layer + jsfail honeypot interaction.
* ``docs/security/as_0_6_automation_bypass_list.md``
  §2 / §4 — three bypass axes (api_key / ip_allowlist / test_token)
  short-circuit honeypot before any field check.
* ``docs/design/as-auth-security-shared-library.md`` §3 — twin pattern.

What this row ships (AS.4.1 scope, strict)
──────────────────────────────────────────
1. **Constants** — :data:`_FORM_PREFIXES` (4 form paths → 2-letter
   prefix), :data:`_RARE_WORD_POOL` (12 words), :data:`OS_HONEYPOT_CLASS`
   (CSS class string), :data:`HONEYPOT_ROTATION_PERIOD_SECONDS` (30 days),
   :data:`HONEYPOT_REJECTED_HTTP_STATUS` (429), :data:`HONEYPOT_REJECTED_CODE`
   (``"bot_challenge_failed"`` — same surface as AS.3.4 so the front-end
   keys on a single error code regardless of which layer caught the bot).
2. **3 audit events** — :data:`EVENT_BOT_CHALLENGE_HONEYPOT_PASS` /
   ``HONEYPOT_FAIL`` / ``HONEYPOT_FORM_DRIFT`` per AS.0.7 §3.4.
3. **Outcome literals** — :data:`OUTCOME_HONEYPOT_PASS` /
   ``HONEYPOT_FAIL`` / ``HONEYPOT_FORM_DRIFT`` / ``HONEYPOT_BYPASS``;
   one-to-one mapping to the audit events for a deterministic lookup.
4. **Frozen result dataclass** — :class:`HoneypotResult` with
   ``allow``, ``outcome``, ``audit_event``, ``bypass_kind``,
   ``field_name_used``, ``failure_reason``, ``audit_metadata``.
5. **Field-name generator** — :func:`honeypot_field_name(form_path,
   tenant_id, epoch)` deterministic SHA-256 → rare-pool index lookup,
   prefixed by form. :func:`current_epoch` reads ``time.time()`` lazily
   (per-call, no module-global cache).
6. **Validator** — :func:`validate_honeypot(form_path, tenant_id,
   submitted, *, bypass_kind=None, now=None)` with the AS.0.7 §3.1
   precedence order: bypass-flagged → field_missing_in_form →
   field_filled → pass. The 30-day epoch boundary 1-request grace
   (current epoch + previous epoch both accepted) is built in.
7. **AS.0.8 single-knob hook** — :func:`is_enabled` short-circuits to
   ``True`` passthrough when ``settings.as_enabled=False``.
8. **Reject-enforcement primitives** — :class:`HoneypotRejected`
   exception + :func:`should_reject` predicate +
   :func:`validate_and_enforce` orchestrator (mirrors the AS.3.4
   shape so AS.6.3 wiring can ``except (BotChallengeRejected,
   HoneypotRejected)`` once and serialise both to HTTP 429).

Out of scope (deferred to follow-up rows in the same epic / family)
───────────────────────────────────────────────────────────────────
* AS.6.3 self-form wiring (4 caller paths actually call
  ``validate_honeypot()`` and emit the audit row).
* AS.7.x React ``<HoneypotField>`` JSX component (the TS twin here
  ships the data primitives — :func:`honeypotFieldName` + the 5
  ``HONEYPOT_INPUT_ATTRS`` keys; the JSX component land per-app).
* AS.6.4 admin Settings UI to flip ``auth_features.honeypot_active``.
* Form-drift persistence counter / per-tenant rotation cadence
  override knob (AS.5.2 dashboard / AS.8.3 ops runbook owns).

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* Public constants (``_FORM_PREFIXES`` / ``_RARE_WORD_POOL`` /
  ``OS_HONEYPOT_CLASS`` / ``HONEYPOT_ROTATION_PERIOD_SECONDS`` /
  3 ``EVENT_*`` strings / 4 ``OUTCOME_*`` literals) are wrapped in
  ``MappingProxyType`` / tuple / frozen dataclass — every uvicorn
  worker derives byte-identical values from the same source code
  (answer #1 of SOP §1).
* :func:`honeypot_field_name` is pure: SHA-256 hash + tuple index, no
  IO, no env reads, no caches. Same input → same output across every
  worker without coordination.
* :func:`current_epoch` reads ``time.time()`` lazily per-call; clock
  skew between workers is bounded by NTP (≤ ~60s) and the validator
  always accepts ``epoch`` and ``epoch − 1`` as a 1-request grace, so
  workers can disagree about "current epoch" near the 30-day boundary
  without rejecting legitimate submissions.
* :func:`validate_honeypot` is a pure function over its arguments and
  the constant tables — no DB writes, no module-level cache, no
  audit-row emit (the caller owns the audit emit per AS.0.6 §11).
* :func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
  fallback at call time (lazy import, no top-level side effect).
* No HTTP client, no DB connection, no thread / async lock — the
  module is import-side-effect free.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
* Honeypot validation is a pure stateless operation over the
  submitted form dict + the 5 module constants. No DB write → no
  read-after-write race.
* The 30-day rotation epoch transition is the only timing-sensitive
  surface: client computes epoch at form-mount, server computes
  epoch + epoch-1 at submit, so a user who renders a form at the end
  of one epoch and submits at the start of the next is still
  accepted (1-request grace, AS.0.7 §2.1 / §11 invariant).
* The caller's audit-emit fan-out happens *after* validate returns,
  in the caller's request handler — downstream readers don't read the
  honeypot result back in the same request.

AS.0.8 single-knob behaviour
────────────────────────────
* :func:`is_enabled` mirrors :func:`bot_challenge.is_enabled` — same
  ``settings.as_enabled`` env, same forward-promotion fallback to
  ``True`` if the field hasn't landed on
  :class:`backend.config.Settings` yet.
* :func:`validate_honeypot` short-circuits with
  ``HoneypotResult(allow=True, outcome="bypass", bypass_kind="knob_off",
  audit_metadata={"reason": "as_enabled_false"})`` when knob-off; the
  caller's emit layer skips on the same gate per AS.0.8 §5 truth-table.

TS twin
───────
``templates/_shared/honeypot/`` (AS.4.1 ships the data primitives;
AS.7.x ships the React JSX component).  The drift guard locks the
12 rare words + 4 form prefixes + the CSS class string + the 5
required HTML attribute keys (cross-twin parity).
"""

from __future__ import annotations

import hashlib
import logging
import time
import types
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants — frozen mappings + tuples (AS.0.7 §2.1 / §4.1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Per-form prefix mapping per AS.0.7 §4.1.  Wrapped in
# ``MappingProxyType`` so the 4-entry invariant is enforced at the
# Python level (assignment / deletion raises ``TypeError``).  The drift
# guard test ``test_form_prefixes_locked`` asserts the exact pair set.
_FORM_PREFIXES: Mapping[str, str] = types.MappingProxyType({
    "/api/v1/auth/login": "lg_",
    "/api/v1/auth/signup": "sg_",
    "/api/v1/auth/password-reset": "pr_",
    "/api/v1/auth/contact": "ct_",
})


def supported_form_paths() -> tuple[str, ...]:
    """Return the 4 form paths the honeypot helper knows about.

    Convenience accessor for callers that want to assert their path is
    on the supported list before computing a field name.
    """
    return tuple(_FORM_PREFIXES.keys())


# 12-word rare pool per AS.0.7 §2.1.  Selected to (a) not collide with
# any WHATWG ``autocomplete`` value, (b) not collide with any field name
# already used by OmniSight forms (AS.0.7 §4.2 grep verified zero hits),
# (c) look plausible enough that a naive form-fill bot will populate
# them.  Frozen tuple — adding / removing words requires a new design
# plan PR + the cross-twin drift guard rerun.
_RARE_WORD_POOL: tuple[str, ...] = (
    "fax_office",
    "secondary_address",
    "company_role",
    "alt_contact",
    "referral_source",
    "marketing_pref",
    "newsletter_freq",
    "preferred_language",
    "fax_number",
    "secondary_email",
    "alt_phone",
    "office_extension",
)


# CSS class on the hidden field.  AS.0.7 §2.2 invariant: the hide style
# MUST be off-screen positioning (``position:absolute;left:-9999px``);
# ``display:none`` / ``visibility:hidden`` are forbidden because some
# bots (Selenium / Playwright headless) skip them.  The class name is a
# string constant so the TS twin and the React JSX component reference
# the same identifier; the actual CSS rule lives in the AS.7.x
# critical-CSS bundle (out of scope here).
OS_HONEYPOT_CLASS: str = "os-honeypot-field"


# Canonical CSS rule body (newline-stripped) for the off-screen hide
# style.  Exposed as a string constant so the TS twin can lock the
# byte-equal rule via the cross-twin drift guard.  Production builds
# inline this rule into critical CSS to survive CSS load failure
# (AS.0.7 §2.2 build invariant).
HONEYPOT_HIDE_CSS: str = (
    "position:absolute;left:-9999px;top:auto;"
    "width:1px;height:1px;overflow:hidden;"
)


# Five required HTML attributes per AS.0.7 §2.6 invariant — every
# honeypot input must render with all five.  Frozen tuple of (key,
# value) pairs so the TS twin can lock the same set.  The React JSX
# component (AS.7.x) renders these byte-equal.
HONEYPOT_INPUT_ATTRS: Mapping[str, str] = types.MappingProxyType({
    "tabindex": "-1",
    "autocomplete": "off",
    "data-1p-ignore": "true",
    "data-lpignore": "true",
    "data-bwignore": "true",
    "aria-hidden": "true",
    "aria-label": "Do not fill",
})


# 30-day rotation cadence per AS.0.7 §2.1.  Long enough that bot
# fingerprints don't churn fast enough to lift the trap, short enough
# that any blacklist a bot accumulates expires within a month.  Decoupled
# from AS.0.5 phase advance (28 days) — different cadence on purpose.
HONEYPOT_ROTATION_PERIOD_SECONDS: int = 30 * 86400


# Same surface as AS.3.4: when honeypot fires, the front-end UI keys
# on a single error code regardless of which layer (captcha / honeypot)
# caught the bot.  Pinned at ``"bot_challenge_failed"`` so AS.6.3 wiring
# can ``except (BotChallengeRejected, HoneypotRejected)`` and emit the
# same 429 + same JSON body.
HONEYPOT_REJECTED_CODE: str = "bot_challenge_failed"


# Same HTTP status as AS.3.4 — 429 (rate-limit class) over 401 (auth
# class) deliberately: keeps the response vague about *which* signal
# caught the bot, denying the per-failure-mode side-channel.
HONEYPOT_REJECTED_HTTP_STATUS: int = 429


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit event canonical names — AS.0.7 §3.4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# These three events are emitted by the *active* honeypot path
# (``validate_honeypot()`` returns a result row that the caller fans
# out as audit).  Distinct from the AS.0.5 §3 ``jsfail_honeypot_*``
# events, which are emitted by the AS.3 captcha-fallback chain when
# the widget JS fails and the chain terminates at honeypot.  Keep the
# two families separate so the AS.5.2 dashboard can split "bot caught
# by honeypot in the normal path" vs "bot caught by honeypot after
# widget JS failed".
EVENT_BOT_CHALLENGE_HONEYPOT_PASS: str = "bot_challenge.honeypot_pass"
EVENT_BOT_CHALLENGE_HONEYPOT_FAIL: str = "bot_challenge.honeypot_fail"
EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT: str = "bot_challenge.honeypot_form_drift"


ALL_HONEYPOT_EVENTS: tuple[str, ...] = (
    EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
    EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
    EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome literals — internal vocabulary for ``HoneypotResult.outcome``
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OUTCOME_HONEYPOT_PASS: str = "honeypot_pass"
OUTCOME_HONEYPOT_FAIL: str = "honeypot_fail"
OUTCOME_HONEYPOT_FORM_DRIFT: str = "honeypot_form_drift"
OUTCOME_HONEYPOT_BYPASS: str = "honeypot_bypass"


ALL_HONEYPOT_OUTCOMES: tuple[str, ...] = (
    OUTCOME_HONEYPOT_PASS,
    OUTCOME_HONEYPOT_FAIL,
    OUTCOME_HONEYPOT_FORM_DRIFT,
    OUTCOME_HONEYPOT_BYPASS,
)


# Outcome → audit event lookup.  Bypass outcomes intentionally have no
# honeypot-family event: the *caller* emits the AS.0.6 ``bypass_*``
# event from its bypass-detection layer.  ``event_for_honeypot_outcome``
# returns ``None`` for bypass; the test
# ``test_event_for_honeypot_outcome_covers_every_outcome`` pins this.
_OUTCOME_TO_EVENT: Mapping[str, Optional[str]] = types.MappingProxyType({
    OUTCOME_HONEYPOT_PASS: EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
    OUTCOME_HONEYPOT_FAIL: EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
    OUTCOME_HONEYPOT_FORM_DRIFT: EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
    OUTCOME_HONEYPOT_BYPASS: None,
})


def event_for_honeypot_outcome(outcome: str) -> Optional[str]:
    """Return the canonical ``bot_challenge.honeypot_*`` event string
    for an outcome literal, or ``None`` for the bypass outcome (the
    caller emits the AS.0.6 ``bypass_*`` event itself).

    Raises :class:`ValueError` on an unknown outcome.
    """
    try:
        return _OUTCOME_TO_EVENT[outcome]
    except KeyError as exc:
        raise ValueError(f"unknown honeypot outcome: {outcome!r}") from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Failure reason vocabulary — for ``HoneypotResult.failure_reason``
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FAILURE_REASON_FIELD_FILLED: str = "field_filled"
FAILURE_REASON_FIELD_MISSING_IN_FORM: str = "field_missing_in_form"
FAILURE_REASON_FORM_PATH_UNKNOWN: str = "form_path_unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bypass-kind vocabulary — mirrors AS.0.6 axes + the AS.0.8 knob-off
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BYPASS_KIND_API_KEY: str = "apikey"
BYPASS_KIND_TEST_TOKEN: str = "test_token"
BYPASS_KIND_IP_ALLOWLIST: str = "ip_allowlist"
BYPASS_KIND_KNOB_OFF: str = "knob_off"
BYPASS_KIND_TENANT_DISABLED: str = "tenant_disabled"


ALL_BYPASS_KINDS: tuple[str, ...] = (
    BYPASS_KIND_API_KEY,
    BYPASS_KIND_TEST_TOKEN,
    BYPASS_KIND_IP_ALLOWLIST,
    BYPASS_KIND_KNOB_OFF,
    BYPASS_KIND_TENANT_DISABLED,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Result dataclass — frozen, immutable
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class HoneypotResult:
    """Outcome of a honeypot validation.

    Returned by :func:`validate_honeypot`.  The caller fans out an audit
    row keyed on :attr:`audit_event` (or, for ``allow=True`` knob-off /
    bypass paths, skips audit per AS.0.6 §11 / AS.0.8 §5 truth-table).

    Attributes
    ──────────
    allow
        Whether the request should be allowed past the honeypot
        layer.  ``True`` for pass / bypass / knob-off; ``False`` only
        for confirmed bot (field filled) or form-drift (field missing).
    outcome
        One of :data:`ALL_HONEYPOT_OUTCOMES`.
    audit_event
        ``bot_challenge.honeypot_pass`` / ``honeypot_fail`` /
        ``honeypot_form_drift`` for the active path; ``None`` for the
        bypass / knob-off path (caller emits AS.0.6 ``bypass_*``
        from its own layer).
    bypass_kind
        ``"apikey"`` / ``"test_token"`` / ``"ip_allowlist"`` /
        ``"knob_off"`` / ``"tenant_disabled"`` when ``allow=True`` due
        to a short-circuit; ``None`` when honeypot actually ran.
    field_name_used
        The expected honeypot field name (current epoch) — included
        in the audit row for diagnostic / dashboard correlation.
        ``None`` when honeypot didn't run (bypass / knob-off /
        unknown form path).
    failure_reason
        Set when ``allow=False`` to one of
        :data:`FAILURE_REASON_FIELD_FILLED` /
        :data:`FAILURE_REASON_FIELD_MISSING_IN_FORM` /
        :data:`FAILURE_REASON_FORM_PATH_UNKNOWN`.
    audit_metadata
        Frozen mapping of extra fields the audit row emitter merges
        into ``metadata`` JSONB.  Includes ``form_path``, ``epoch``,
        ``submitted_field_length`` (for honeypot fail; never the raw
        value, per AS.0.7 §3.4 PII redaction).
    """

    allow: bool
    outcome: str
    audit_event: Optional[str]
    bypass_kind: Optional[str] = None
    field_name_used: Optional[str] = None
    failure_reason: Optional[str] = None
    audit_metadata: Mapping[str, Any] = types.MappingProxyType({})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class HoneypotError(Exception):
    """Base class for honeypot-family typed errors."""


class HoneypotRejected(HoneypotError):
    """Raised by :func:`validate_and_enforce` when a honeypot result has
    ``allow=False``.  Caller's HTTP layer maps this to a 429 response
    with body ``{"error": "bot_challenge_failed"}`` (same surface as
    :exc:`backend.security.bot_challenge.BotChallengeRejected` from
    AS.3.4 — the front-end UI keys on a single error code regardless of
    which layer caught the bot).

    Attributes mirror the AS.3.4 ``BotChallengeRejected`` shape so
    callers can ``except (BotChallengeRejected, HoneypotRejected)``
    once and serialise both to the same HTTP response.
    """

    def __init__(
        self,
        result: HoneypotResult,
        *,
        code: str = HONEYPOT_REJECTED_CODE,
        http_status: int = HONEYPOT_REJECTED_HTTP_STATUS,
    ) -> None:
        self.result = result
        self.code = code
        self.http_status = http_status
        super().__init__(
            f"honeypot rejected: outcome={result.outcome} "
            f"reason={result.failure_reason} "
            f"code={code} http_status={http_status}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  is_enabled — AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS family is enabled per AS.0.8 §3.1 noop matrix.

    Mirrors :func:`backend.security.bot_challenge.is_enabled` — same
    ``settings.as_enabled`` env, same forward-promotion guard.  Default
    ``True``.  Read lazily via ``getattr`` fallback so the module is
    importable before AS.3.1 lands the field on
    :class:`backend.config.Settings`.
    """
    try:
        from backend.config import settings  # local import: zero import-time side effect
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Field-name generator (AS.0.7 §2.1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def current_epoch(*, now: Optional[float] = None) -> int:
    """Return the current 30-day rotation epoch.

    *now*: optional UNIX timestamp override (testing).  When omitted,
    reads ``time.time()`` lazily — no module-global cache, no
    coordination across workers (every worker derives the same epoch
    from the same clock, NTP-bounded; the validator's epoch grace
    absorbs the residual ≤ ~60s skew).
    """
    ts = now if now is not None else time.time()
    return int(ts // HONEYPOT_ROTATION_PERIOD_SECONDS)


def honeypot_field_name(form_path: str, tenant_id: str, epoch: int) -> str:
    """Return the canonical honeypot field name for a (form, tenant,
    epoch) triple.

    Pure deterministic function: SHA-256 over ``"<tenant>:<epoch>"`` →
    integer-mod-12 → rare-pool index; prefix from
    :data:`_FORM_PREFIXES`.  No IO, no env reads, no caches.

    Raises :class:`ValueError` if *form_path* is not one of the four
    supported paths (per AS.0.7 §4.1).
    """
    try:
        prefix = _FORM_PREFIXES[form_path]
    except KeyError as exc:
        raise ValueError(
            f"unknown form_path: {form_path!r} "
            f"(supported: {tuple(_FORM_PREFIXES.keys())})"
        ) from exc
    seed = f"{tenant_id}:{epoch}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    idx = int(digest, 16) % len(_RARE_WORD_POOL)
    return prefix + _RARE_WORD_POOL[idx]


def expected_field_names(
    form_path: str,
    tenant_id: str,
    *,
    now: Optional[float] = None,
) -> tuple[str, str]:
    """Return ``(current_epoch_name, prev_epoch_name)`` — both accepted
    by :func:`validate_honeypot` to cover the 30-day boundary
    1-request grace per AS.0.7 §2.1.
    """
    epoch_now = current_epoch(now=now)
    return (
        honeypot_field_name(form_path, tenant_id, epoch_now),
        honeypot_field_name(form_path, tenant_id, epoch_now - 1),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validator — pure function over the form submission (AS.0.7 §3.1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _bypass_result(
    *,
    form_path: str,
    bypass_kind: str,
) -> HoneypotResult:
    """Construct the canonical bypass-shape result.

    Caller's audit-emit layer skips on the bypass kind (knob_off /
    tenant_disabled) or emits the AS.0.6 ``bypass_*`` event for the
    A/B/C axes.
    """
    return HoneypotResult(
        allow=True,
        outcome=OUTCOME_HONEYPOT_BYPASS,
        audit_event=None,
        bypass_kind=bypass_kind,
        field_name_used=None,
        failure_reason=None,
        audit_metadata=types.MappingProxyType({
            "form_path": form_path,
            "bypass_kind": bypass_kind,
        }),
    )


def validate_honeypot(
    form_path: str,
    tenant_id: str,
    submitted: Mapping[str, Any],
    *,
    bypass_kind: Optional[str] = None,
    tenant_honeypot_active: bool = True,
    now: Optional[float] = None,
) -> HoneypotResult:
    """Validate a form submission against the honeypot field.

    Precedence (AS.0.7 §3.1 / §3.3 / §4.3 / AS.0.8):

    1. ``is_enabled() == False`` → bypass (``knob_off``).  AS.0.8 §3.1
       single-knob noop overrides everything else.
    2. *bypass_kind* is set (caller pre-detected an AS.0.6 axis hit) →
       bypass; we don't check the form because bypass-flagged callers
       use form-less endpoints.
    3. *tenant_honeypot_active* is ``False`` → bypass
       (``tenant_disabled``).  AS.0.7 §4.3 invariant: per-tenant
       opt-in via ``auth_features.honeypot_active``; the lib doesn't
       read the column, the caller passes it in (so the helper stays
       DB-free and per-test injectable).
    4. *form_path* is not on :data:`_FORM_PREFIXES` →
       ``form_path_unknown`` failure (caller treats as 4xx form-drift).
    5. Compute current + previous epoch field names; if neither is in
       *submitted* → ``field_missing_in_form`` failure
       (frontend deploy-drift alarm).
    6. If the submitted field's value (after ``.strip()``) is non-empty
       → ``field_filled`` failure (bot caught).
    7. Otherwise → pass.

    *submitted* can be any ``Mapping`` (dict, ``MultiDict``,
    ``MappingProxyType``).  Values can be ``str`` / ``None`` — anything
    else is coerced via ``str()`` for the empty-check (a list of values
    coming from a duplicate form key is treated as filled if any element
    is non-empty after strip, since a bot would pile values on).
    """
    # 1. AS.0.8 single-knob: knob-off overrides everything.
    if not is_enabled():
        return _bypass_result(
            form_path=form_path,
            bypass_kind=BYPASS_KIND_KNOB_OFF,
        )

    # 2. AS.0.6 axis hit → caller pre-detected; we trust and short-circuit.
    if bypass_kind:
        if bypass_kind not in ALL_BYPASS_KINDS:
            raise ValueError(
                f"unknown bypass_kind: {bypass_kind!r} "
                f"(supported: {ALL_BYPASS_KINDS})"
            )
        return _bypass_result(form_path=form_path, bypass_kind=bypass_kind)

    # 3. AS.0.7 §4.3 per-tenant opt-out.
    if not tenant_honeypot_active:
        return _bypass_result(
            form_path=form_path,
            bypass_kind=BYPASS_KIND_TENANT_DISABLED,
        )

    # 4. Form path must be one of the 4 known.
    if form_path not in _FORM_PREFIXES:
        return HoneypotResult(
            allow=False,
            outcome=OUTCOME_HONEYPOT_FORM_DRIFT,
            audit_event=EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
            bypass_kind=None,
            field_name_used=None,
            failure_reason=FAILURE_REASON_FORM_PATH_UNKNOWN,
            audit_metadata=types.MappingProxyType({
                "form_path": form_path,
                "supported_paths": tuple(_FORM_PREFIXES.keys()),
            }),
        )

    epoch_now = current_epoch(now=now)
    name_now = honeypot_field_name(form_path, tenant_id, epoch_now)
    name_prev = honeypot_field_name(form_path, tenant_id, epoch_now - 1)

    # 5. Field-missing-in-form: frontend deploy-drift alarm.
    submitted_keys = frozenset(submitted.keys())
    if name_now not in submitted_keys and name_prev not in submitted_keys:
        return HoneypotResult(
            allow=False,
            outcome=OUTCOME_HONEYPOT_FORM_DRIFT,
            audit_event=EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT,
            bypass_kind=None,
            field_name_used=name_now,
            failure_reason=FAILURE_REASON_FIELD_MISSING_IN_FORM,
            audit_metadata=types.MappingProxyType({
                "form_path": form_path,
                "epoch": epoch_now,
                "expected_field_names": (name_now, name_prev),
                # PII-redacted: only the keys, never the values
                "submitted_keys": tuple(sorted(submitted_keys)),
            }),
        )

    # 6 / 7. Pull the value from whichever epoch matched (prefer current).
    field_used = name_now if name_now in submitted_keys else name_prev
    raw_value = submitted[field_used]

    if _value_is_filled(raw_value):
        # Bot caught.  Audit row metadata records the *length* only —
        # never the raw value (autofill-filled value can be PII).
        return HoneypotResult(
            allow=False,
            outcome=OUTCOME_HONEYPOT_FAIL,
            audit_event=EVENT_BOT_CHALLENGE_HONEYPOT_FAIL,
            bypass_kind=None,
            field_name_used=field_used,
            failure_reason=FAILURE_REASON_FIELD_FILLED,
            audit_metadata=types.MappingProxyType({
                "form_path": form_path,
                "epoch": epoch_now,
                "field_filled_length": _value_length(raw_value),
            }),
        )

    return HoneypotResult(
        allow=True,
        outcome=OUTCOME_HONEYPOT_PASS,
        audit_event=EVENT_BOT_CHALLENGE_HONEYPOT_PASS,
        bypass_kind=None,
        field_name_used=field_used,
        failure_reason=None,
        audit_metadata=types.MappingProxyType({
            "form_path": form_path,
            "epoch": epoch_now,
        }),
    )


def _value_is_filled(raw: Any) -> bool:
    """Return whether a submitted value should count as 'filled'.

    Strings: stripped non-empty.  None / missing: not filled.
    Sequences (list / tuple from a multi-valued form key): any element
    counts.  Anything else is coerced via ``str()`` and stripped.
    """
    if raw is None:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, (list, tuple)):
        return any(_value_is_filled(v) for v in raw)
    return bool(str(raw).strip())


def _value_length(raw: Any) -> int:
    """Return the diagnostic length of a submitted value (for audit
    metadata; PII-redacted, never the raw bytes).

    Strings: ``len()``.  Sequences: total length across elements.
    None: 0.  Anything else: ``len(str(raw))``.
    """
    if raw is None:
        return 0
    if isinstance(raw, str):
        return len(raw)
    if isinstance(raw, (list, tuple)):
        return sum(_value_length(v) for v in raw)
    return len(str(raw))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Reject-enforcement primitives — mirror AS.3.4 surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def should_reject(result: HoneypotResult) -> bool:
    """Pure predicate: ``not result.allow``.

    Mirrors :func:`backend.security.bot_challenge.should_reject` so
    AS.6.3 wiring composes the two layers cleanly.
    """
    return not result.allow


def validate_and_enforce(
    form_path: str,
    tenant_id: str,
    submitted: Mapping[str, Any],
    *,
    bypass_kind: Optional[str] = None,
    tenant_honeypot_active: bool = True,
    now: Optional[float] = None,
) -> HoneypotResult:
    """Run :func:`validate_honeypot`; on a reject result, raise
    :class:`HoneypotRejected`.  On pass / bypass, return the result.

    Single-call orchestrator that AS.6.3 wiring uses to chain honeypot
    + bot-challenge in a uniform try / except block.
    """
    result = validate_honeypot(
        form_path,
        tenant_id,
        submitted,
        bypass_kind=bypass_kind,
        tenant_honeypot_active=tenant_honeypot_active,
        now=now,
    )
    if should_reject(result):
        raise HoneypotRejected(result)
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API surface — keep in sync with ``ALL_HONEYPOT_*`` tuples
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = (
    # Constants
    "_FORM_PREFIXES",
    "_RARE_WORD_POOL",
    "OS_HONEYPOT_CLASS",
    "HONEYPOT_HIDE_CSS",
    "HONEYPOT_INPUT_ATTRS",
    "HONEYPOT_ROTATION_PERIOD_SECONDS",
    "HONEYPOT_REJECTED_CODE",
    "HONEYPOT_REJECTED_HTTP_STATUS",
    # Audit events
    "EVENT_BOT_CHALLENGE_HONEYPOT_PASS",
    "EVENT_BOT_CHALLENGE_HONEYPOT_FAIL",
    "EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT",
    "ALL_HONEYPOT_EVENTS",
    # Outcomes
    "OUTCOME_HONEYPOT_PASS",
    "OUTCOME_HONEYPOT_FAIL",
    "OUTCOME_HONEYPOT_FORM_DRIFT",
    "OUTCOME_HONEYPOT_BYPASS",
    "ALL_HONEYPOT_OUTCOMES",
    # Failure reasons
    "FAILURE_REASON_FIELD_FILLED",
    "FAILURE_REASON_FIELD_MISSING_IN_FORM",
    "FAILURE_REASON_FORM_PATH_UNKNOWN",
    # Bypass kinds
    "BYPASS_KIND_API_KEY",
    "BYPASS_KIND_TEST_TOKEN",
    "BYPASS_KIND_IP_ALLOWLIST",
    "BYPASS_KIND_KNOB_OFF",
    "BYPASS_KIND_TENANT_DISABLED",
    "ALL_BYPASS_KINDS",
    # Result dataclass + errors
    "HoneypotResult",
    "HoneypotError",
    "HoneypotRejected",
    # Functions
    "is_enabled",
    "supported_form_paths",
    "current_epoch",
    "honeypot_field_name",
    "expected_field_names",
    "validate_honeypot",
    "should_reject",
    "validate_and_enforce",
    "event_for_honeypot_outcome",
)

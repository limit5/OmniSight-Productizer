"""AS.5.2 — Per-tenant dashboard rollup + suspicious-pattern detection.

Pure-functional read-side companion to the AS.5.1 ``auth_event`` write-side
emitters.  Reads the eight ``auth.*`` rollup events out of ``audit_log``
and computes the per-tenant dashboard widgets the AS.6.x admin pane
renders + the suspicious-pattern alerts the AS.7.x notification surface
fires on:

    * Challenge pass / fail rate (per tenant, per time window).
    * Auth-method distribution (% of password / oauth / passkey / mfa /
      magic_link logins).
    * Suspicious-pattern alerts:
        - Login-fail burst (N fails on the same attempted_user_fp in W).
        - Bot-challenge fail spike (N fails per form_path in W).
        - Token-refresh storm (N refreshes per provider+user in W).
        - Honeypot triggered (any ``reason="honeypot"`` row).
        - OAuth revoke/relink loop (alternating revoke+connect rows).
        - Distributed login-fail (one attempted_user_fp from many ip_fps).

Why a separate module from AS.5.1
─────────────────────────────────
AS.5.1 ``auth_event`` is the **write surface**: emitter helpers + frozen
vocabularies + canonical ``after`` field shapes.  AS.5.2 is the **read
surface**: counts, ratios, distributions, anomaly detection.  Splitting
them keeps the write path cheap (no aggregation logic in the request
handler) and the read path independently testable (no DB round-trip in
the unit tests — pass row dicts directly).

Plan / spec source
──────────────────
* TODO row AS.5.2 — per-tenant dashboard (challenge pass/fail rate /
  auth method distribution / suspicious pattern alert).
* ``backend/security/auth_event.py`` — eight ``auth.*`` rollup events
  + frozen vocabularies the rules key on.
* ``docs/security/as_0_8_single_knob_rollback.md`` §6 — knob-off banner
  + cron-skip behaviour matrix (knob-false ⇒ ``compute_dashboard``
  returns the empty-summary banner shape, no DB read).
* ``docs/security/as_0_6_automation_bypass_list.md`` §5 — bypass-pass
  rows feed the per-tenant monthly aggregate report (this module
  surfaces the same ``bot_challenge_pass_kinds`` distribution the
  monthly cron consumes).

What this row ships (AS.5.2 scope, strict)
──────────────────────────────────────────
1. **Frozen :class:`DashboardSummary`** — per-tenant aggregate totals,
   per-event counts, per-vocabulary breakdowns, ratios.
2. **Frozen :class:`SuspiciousPatternAlert`** — typed alert with
   ``rule`` / ``severity`` / ``evidence`` (a per-rule dict for the UI).
3. **Six rule constants** + :data:`ALL_DASHBOARD_RULES` tuple — keys
   the AS.7.x notification template hooks on.
4. **Three severity literals** + :data:`SEVERITIES` frozenset.
5. **Frozen :data:`DEFAULT_THRESHOLDS`** (``MappingProxyType``) — per-rule
   numeric defaults; callers may override via ``thresholds=`` kwarg.
6. **Pure :func:`summarise`** — reduce an iterable of audit row dicts
   into a :class:`DashboardSummary`.  No IO, deterministic by input.
7. **Pure :func:`detect_suspicious_patterns`** — apply the six rules
   over the same iterable and return a tuple of alerts.
8. **Async :func:`compute_dashboard`** — orchestrator: knob check →
   PG fetch via :func:`_fetch_auth_rows` → summarise + detect →
   :class:`DashboardResult` (summary + alerts + knob banner flag).
9. **AS.0.8 knob hook** :func:`is_enabled` — reads ``settings.as_enabled``
   lazily (mirrors :func:`auth_event.is_enabled`).

Out of scope (deferred to follow-up rows)
─────────────────────────────────────────
* Admin Settings UI widget (AS.6.4 surfaces these rollups in the
  per-tenant settings pane).
* The HTTP endpoint exposing :func:`compute_dashboard` to the frontend
  (AS.6.5 wires ``GET /api/v1/admin/audit/dashboard``).
* The notification fan-out for alerts (AS.7.x picks alerts off this
  surface and renders them; the alert payload shape is the contract).
* Long-window rollups (>30 days) — :func:`compute_dashboard` enforces
  ``LIMIT_ROWS_DEFAULT`` (50 000 rows) so a runaway query can't OOM the
  worker; longer windows want a materialised-view backed surface (out
  of scope).
* The monthly aggregate-report cron AS.0.6 §5 references — that cron
  consumes :data:`DashboardSummary.bot_challenge_pass_kinds` but lives
  in its own module (AS.0.6 scheduler, not this row).

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* Public surface is immutable (``frozen=True`` dataclasses,
  ``MappingProxyType`` thresholds, ``frozenset`` severity set, plain
  strings).  No module-level mutable container two workers could
  disagree on.
* No env reads at module top.  :func:`is_enabled` reads
  ``settings.as_enabled`` lazily on every call so each uvicorn worker
  derives the same value from the same source — answer #1 of SOP §1
  (deterministic-by-construction across workers).
* :func:`compute_dashboard`'s DB borrow happens inside the function
  body (same pattern as :mod:`backend.audit` query).  No connection
  held at module level.
* Module import is side-effect free — pure constants + dataclasses +
  function defs.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
N/A — read-only path against ``audit_log``.  Two concurrent dashboard
queries cannot race each other (each reads a snapshot under its own
implicit transaction).  Race vs. a concurrent
:func:`backend.audit.log` write is benign: we may or may not see the
write depending on commit timing, but the AS.5.2 dashboard is
intentionally an eventual-consistency view (the AS.7.x notification
surface re-polls on a fixed cron, not subscribe+stream).

AS.0.8 single-knob behaviour
────────────────────────────
:func:`compute_dashboard` checks :func:`is_enabled` first.  When
knob-false, returns :data:`KNOB_OFF_RESULT` (an empty summary +
``knob_off=True`` flag) WITHOUT touching the DB — AS.0.8 §6 banner
contract.  The pure helpers (:func:`summarise` /
:func:`detect_suspicious_patterns`) deliberately ignore the knob: a
script that wants to inspect dashboard counts (test harness, doc
generator, on-demand audit pull) must work regardless of knob state.

TS twin
───────
``templates/_shared/auth-dashboard/`` ships the byte-equal mirror.  The
AS.5.2 cross-twin drift guard
(``backend/tests/test_auth_dashboard_shape_drift.py``) locks the rule
set + severity set + threshold defaults + summary field set + alert
field set + per-event counter mapping.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from backend.security import auth_event as ae

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Six dashboard-rule constants — AS.5.2 SoT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Login-fail burst.  Many ``auth.login_fail`` rows on the same
# ``attempted_user_fp`` within a short window (default: 10 fails / 60 s).
# Detects credential-stuffing aimed at one account.
RULE_LOGIN_FAIL_BURST: str = "login_fail_burst"

# Bot-challenge fail spike.  Many ``auth.bot_challenge_fail`` rows on
# the same ``form_path`` within a short window (default: 20 fails / 60 s).
# Detects sustained bot pressure on one form (signup spam, contact-form
# abuse).
RULE_BOT_CHALLENGE_FAIL_SPIKE: str = "bot_challenge_fail_spike"

# Token-refresh storm.  Many ``auth.token_refresh`` rows on the same
# ``provider:user_id`` entity within a short window (default: 10
# refreshes / 60 s).  Detects a stuck refresh loop (Phase-3-Runtime-v2
# G4-class incident — vault encryption error → caller retries forever).
RULE_TOKEN_REFRESH_STORM: str = "token_refresh_storm"

# Honeypot triggered.  Any ``auth.bot_challenge_fail`` with
# ``reason="honeypot"``.  Per AS.4.1, hidden-field submissions are
# 100 % bot traffic; one trigger is enough to alert.
RULE_HONEYPOT_TRIGGERED: str = "honeypot_triggered"

# OAuth revoke/relink loop.  Repeated ``auth.oauth_revoke`` +
# ``auth.oauth_connect`` pairs on the same ``provider:user_id`` within
# a short window (default: 3 revoke+connect cycles / 600 s).  Detects
# the user-who-keeps-revoking anti-pattern AS.5.1 §"OAuth-connect
# outcomes" calls out.
RULE_OAUTH_REVOKE_RELINK_LOOP: str = "oauth_revoke_relink_loop"

# Distributed login-fail.  One ``attempted_user_fp`` failing from many
# distinct ``ip_fp`` values in a short window (default: 5 distinct IPs /
# 300 s).  Detects botnet credential-stuffing aimed at one account.
RULE_DISTRIBUTED_LOGIN_FAIL: str = "distributed_login_fail"


ALL_DASHBOARD_RULES: tuple[str, ...] = (
    RULE_LOGIN_FAIL_BURST,
    RULE_BOT_CHALLENGE_FAIL_SPIKE,
    RULE_TOKEN_REFRESH_STORM,
    RULE_HONEYPOT_TRIGGERED,
    RULE_OAUTH_REVOKE_RELINK_LOOP,
    RULE_DISTRIBUTED_LOGIN_FAIL,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Three severity literals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SEVERITY_INFO: str = "info"
SEVERITY_WARN: str = "warn"
SEVERITY_CRITICAL: str = "critical"

SEVERITIES: frozenset[str] = frozenset({
    SEVERITY_INFO,
    SEVERITY_WARN,
    SEVERITY_CRITICAL,
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Default thresholds — frozen MappingProxyType
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Per-rule defaults.  Each entry is ``{"count": int, "window_s": int}``
# unless noted.  ``MappingProxyType`` so a caller can read but not
# mutate the shared default object — overrides go through the
# ``thresholds=`` kwarg which falls back to these values per-key.
DEFAULT_THRESHOLDS: Mapping[str, Mapping[str, int]] = MappingProxyType({
    RULE_LOGIN_FAIL_BURST: MappingProxyType({"count": 10, "window_s": 60}),
    RULE_BOT_CHALLENGE_FAIL_SPIKE: MappingProxyType(
        {"count": 20, "window_s": 60}
    ),
    RULE_TOKEN_REFRESH_STORM: MappingProxyType(
        {"count": 10, "window_s": 60}
    ),
    # ``count``=1 because honeypot is a bright-line signal — one trigger
    # is enough to fire.  ``window_s`` is irrelevant for count=1 but
    # kept for shape uniformity.
    RULE_HONEYPOT_TRIGGERED: MappingProxyType({"count": 1, "window_s": 60}),
    RULE_OAUTH_REVOKE_RELINK_LOOP: MappingProxyType(
        {"count": 3, "window_s": 600}
    ),
    RULE_DISTRIBUTED_LOGIN_FAIL: MappingProxyType(
        {"count": 5, "window_s": 300}
    ),
})


# Per-rule severity assignment.  Frozen.
DEFAULT_RULE_SEVERITIES: Mapping[str, str] = MappingProxyType({
    RULE_LOGIN_FAIL_BURST: SEVERITY_WARN,
    RULE_BOT_CHALLENGE_FAIL_SPIKE: SEVERITY_WARN,
    RULE_TOKEN_REFRESH_STORM: SEVERITY_WARN,
    # Honeypot trip is by definition a confirmed bot — escalate.
    RULE_HONEYPOT_TRIGGERED: SEVERITY_CRITICAL,
    RULE_OAUTH_REVOKE_RELINK_LOOP: SEVERITY_INFO,
    # Distributed credential stuffing across IPs — escalate.
    RULE_DISTRIBUTED_LOGIN_FAIL: SEVERITY_CRITICAL,
})


# Default fetch limit for :func:`compute_dashboard`.  Mirrors the
# :func:`backend.audit.query` default-limit philosophy: a single
# dashboard call MUST cap its row read so one wide-window query can't
# OOM a worker.  50 000 rows × ~1 KB/row ≈ 50 MB peak Python heap; far
# below the per-worker budget.  Long windows want a materialised view.
LIMIT_ROWS_DEFAULT: int = 50_000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen output dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class DashboardSummary:
    """Per-tenant aggregate counts + ratios + distributions.

    ``rate`` fields are ``None`` (not 0.0) when the denominator is
    zero — the AS.6.4 widget renders ``None`` as "no data" and ``0.0``
    as "0% of N", and conflating them would be a UX bug.
    """

    tenant_id: str
    since: Optional[float]
    until: Optional[float]
    total_events: int

    # Login family
    login_success_count: int
    login_fail_count: int
    login_success_rate: Optional[float]
    login_fail_reasons: Mapping[str, int]
    auth_method_distribution: Mapping[str, int]

    # Bot-challenge family
    bot_challenge_pass_count: int
    bot_challenge_fail_count: int
    bot_challenge_pass_rate: Optional[float]
    bot_challenge_pass_kinds: Mapping[str, int]
    bot_challenge_fail_reasons: Mapping[str, int]

    # OAuth family
    oauth_connect_count: int
    oauth_revoke_count: int
    oauth_revoke_initiators: Mapping[str, int]

    # Token family
    token_refresh_count: int
    token_refresh_outcomes: Mapping[str, int]
    token_rotated_count: int


@dataclass(frozen=True)
class SuspiciousPatternAlert:
    """One detected suspicious pattern, ready for the AS.7.x notification
    surface.

    ``rule`` is one of :data:`ALL_DASHBOARD_RULES`; ``severity`` is one
    of :data:`SEVERITIES`.  ``evidence`` is a per-rule mapping of fields
    the UI renders (e.g. the offending fingerprint, the count observed,
    the time window, the list of distinct IP fingerprints).  Frozen so
    the UI can stash an alert object across re-renders without copying.
    """

    rule: str
    severity: str
    tenant_id: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class DashboardResult:
    """Combined dashboard payload returned by :func:`compute_dashboard`.

    ``knob_off`` mirrors the AS.0.8 §6 banner contract: ``True`` when
    the AS knob is disabled and the dashboard rendered the
    "auth-security feature disabled" banner instead of live counts.
    """

    summary: DashboardSummary
    alerts: tuple[SuspiciousPatternAlert, ...]
    knob_off: bool
    row_count_observed: int
    row_count_truncated: bool


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether AS.5.2 is enabled per AS.0.8 §6 dashboard noop matrix.

    Mirrors :func:`backend.security.auth_event.is_enabled`.  Reads
    ``settings.as_enabled`` lazily via ``getattr`` fallback (default
    ``True``) so the module is importable before AS.3.1 lands the field
    on :class:`backend.config.Settings`.
    """
    try:
        from backend.config import settings  # local import: zero side effect
    except Exception:
        return True
    return bool(getattr(settings, "as_enabled", True))


def _gate() -> bool:
    """AS.0.8 §6 dashboard-behaviour matrix: knob-false ⇒ no DB read,
    return banner shape.  Mirrors :func:`auth_event._gate`.
    """
    return is_enabled()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Row helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _action(row: Mapping[str, Any]) -> str:
    """Read ``action`` from an audit row dict.  Returns ``""`` if absent
    (defensive — a malformed row should be skipped, not crash the rollup).
    """
    a = row.get("action")
    return a if isinstance(a, str) else ""


def _after(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read ``after`` dict from an audit row; returns ``{}`` if absent
    or malformed."""
    a = row.get("after")
    return a if isinstance(a, Mapping) else {}


def _ts(row: Mapping[str, Any]) -> float:
    """Read ``ts`` (epoch seconds) from an audit row.  Returns ``0.0``
    if absent — pushes malformed rows to the start of the window so
    they don't poison time-window-based rule evaluation."""
    t = row.get("ts")
    if isinstance(t, (int, float)):
        return float(t)
    return 0.0


def _entity_id(row: Mapping[str, Any]) -> str:
    e = row.get("entity_id")
    return e if isinstance(e, str) else ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Pure summariser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _safe_rate(numerator: int, denominator: int) -> Optional[float]:
    """Return ``num / denom`` as ``float``, or ``None`` if ``denom == 0``.

    UX contract (see :class:`DashboardSummary` docstring): ``None`` is
    "no data"; ``0.0`` is "0 % of N".  Conflating them is a bug.
    """
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def empty_summary(
    tenant_id: str,
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> DashboardSummary:
    """Return a zero-filled :class:`DashboardSummary` for *tenant_id*.

    Used as the knob-off banner shape AND as the AS.6.4 settings-pane
    placeholder when a tenant has zero auth events in the requested
    window.  Counters are ``0``; rates are ``None`` per the UX
    contract; distributions are empty :class:`MappingProxyType` so a
    caller cannot accidentally mutate the shared empty-dict singleton.
    """
    empty: Mapping[str, int] = MappingProxyType({})
    return DashboardSummary(
        tenant_id=tenant_id,
        since=since,
        until=until,
        total_events=0,
        login_success_count=0,
        login_fail_count=0,
        login_success_rate=None,
        login_fail_reasons=empty,
        auth_method_distribution=empty,
        bot_challenge_pass_count=0,
        bot_challenge_fail_count=0,
        bot_challenge_pass_rate=None,
        bot_challenge_pass_kinds=empty,
        bot_challenge_fail_reasons=empty,
        oauth_connect_count=0,
        oauth_revoke_count=0,
        oauth_revoke_initiators=empty,
        token_refresh_count=0,
        token_refresh_outcomes=empty,
        token_rotated_count=0,
    )


def summarise(
    rows: Iterable[Mapping[str, Any]],
    *,
    tenant_id: str,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> DashboardSummary:
    """Reduce *rows* into a :class:`DashboardSummary`.

    Pure function — no IO.  Skips rows whose ``action`` is not in the
    AS.5.1 :data:`auth_event.ALL_AUTH_EVENTS` set (caller may pass a
    superset; the dashboard only counts the eight rollup events).
    Validates each row's vocabulary value against the AS.5.1 frozen
    sets and silently drops unknown values (a typo in a sibling caller
    must not fabricate phantom rows in the dashboard counts).
    """
    login_success = 0
    login_fail = 0
    login_fail_reasons: Counter[str] = Counter()
    auth_methods: Counter[str] = Counter()

    bc_pass = 0
    bc_fail = 0
    bc_pass_kinds: Counter[str] = Counter()
    bc_fail_reasons: Counter[str] = Counter()

    oauth_connect = 0
    oauth_revoke = 0
    oauth_revoke_initiators: Counter[str] = Counter()

    token_refresh = 0
    token_refresh_outcomes: Counter[str] = Counter()
    token_rotated = 0

    total = 0

    for row in rows:
        action = _action(row)
        if action not in ae.ALL_AUTH_EVENTS:
            continue
        after = _after(row)
        total += 1

        if action == ae.EVENT_AUTH_LOGIN_SUCCESS:
            login_success += 1
            method = after.get("auth_method")
            if isinstance(method, str) and method in ae.AUTH_METHODS:
                auth_methods[method] += 1
        elif action == ae.EVENT_AUTH_LOGIN_FAIL:
            login_fail += 1
            reason = after.get("fail_reason")
            if isinstance(reason, str) and reason in ae.LOGIN_FAIL_REASONS:
                login_fail_reasons[reason] += 1
        elif action == ae.EVENT_AUTH_BOT_CHALLENGE_PASS:
            bc_pass += 1
            kind = after.get("kind")
            if isinstance(kind, str) and kind in ae.BOT_CHALLENGE_PASS_KINDS:
                bc_pass_kinds[kind] += 1
        elif action == ae.EVENT_AUTH_BOT_CHALLENGE_FAIL:
            bc_fail += 1
            reason = after.get("reason")
            if isinstance(reason, str) and reason in ae.BOT_CHALLENGE_FAIL_REASONS:
                bc_fail_reasons[reason] += 1
        elif action == ae.EVENT_AUTH_OAUTH_CONNECT:
            oauth_connect += 1
        elif action == ae.EVENT_AUTH_OAUTH_REVOKE:
            oauth_revoke += 1
            initiator = after.get("initiator")
            if isinstance(initiator, str) and initiator in ae.OAUTH_REVOKE_INITIATORS:
                oauth_revoke_initiators[initiator] += 1
        elif action == ae.EVENT_AUTH_TOKEN_REFRESH:
            token_refresh += 1
            outcome = after.get("outcome")
            if isinstance(outcome, str) and outcome in ae.TOKEN_REFRESH_OUTCOMES:
                token_refresh_outcomes[outcome] += 1
        elif action == ae.EVENT_AUTH_TOKEN_ROTATED:
            token_rotated += 1

    return DashboardSummary(
        tenant_id=tenant_id,
        since=since,
        until=until,
        total_events=total,
        login_success_count=login_success,
        login_fail_count=login_fail,
        login_success_rate=_safe_rate(login_success, login_success + login_fail),
        login_fail_reasons=MappingProxyType(dict(login_fail_reasons)),
        auth_method_distribution=MappingProxyType(dict(auth_methods)),
        bot_challenge_pass_count=bc_pass,
        bot_challenge_fail_count=bc_fail,
        bot_challenge_pass_rate=_safe_rate(bc_pass, bc_pass + bc_fail),
        bot_challenge_pass_kinds=MappingProxyType(dict(bc_pass_kinds)),
        bot_challenge_fail_reasons=MappingProxyType(dict(bc_fail_reasons)),
        oauth_connect_count=oauth_connect,
        oauth_revoke_count=oauth_revoke,
        oauth_revoke_initiators=MappingProxyType(dict(oauth_revoke_initiators)),
        token_refresh_count=token_refresh,
        token_refresh_outcomes=MappingProxyType(dict(token_refresh_outcomes)),
        token_rotated_count=token_rotated,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Suspicious-pattern detector — six rules
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resolve_threshold(
    rule: str,
    overrides: Optional[Mapping[str, Mapping[str, int]]],
) -> Mapping[str, int]:
    """Pick the threshold mapping for *rule*, preferring *overrides* keys.

    Per-key fallback to :data:`DEFAULT_THRESHOLDS` so a caller can
    override only ``count`` without re-specifying ``window_s``.
    """
    default = DEFAULT_THRESHOLDS[rule]
    if not overrides or rule not in overrides:
        return default
    merged: dict[str, int] = dict(default)
    merged.update(overrides[rule])
    return MappingProxyType(merged)


def _has_window_burst(
    timestamps: list[float], *, count: int, window_s: int,
) -> tuple[bool, Optional[float], Optional[float]]:
    """Return ``(triggered, first_ts, last_ts)`` if *timestamps* contain
    ``count`` entries within a sliding ``window_s`` window.

    Two-pointer sweep over a sorted list — O(n).
    """
    if not timestamps or count <= 0:
        return (False, None, None)
    sorted_ts = sorted(timestamps)
    left = 0
    for right in range(len(sorted_ts)):
        while sorted_ts[right] - sorted_ts[left] > window_s:
            left += 1
        if right - left + 1 >= count:
            return (True, sorted_ts[left], sorted_ts[right])
    return (False, None, None)


def _detect_login_fail_burst(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_LOGIN_FAIL_BURST — group login_fail rows by attempted_user_fp,
    fire one alert per fp with count >= threshold within the window.
    """
    by_fp: dict[str, list[float]] = {}
    for row in rows:
        if _action(row) != ae.EVENT_AUTH_LOGIN_FAIL:
            continue
        after = _after(row)
        fp = after.get("attempted_user_fp")
        if not isinstance(fp, str) or not fp:
            continue
        by_fp.setdefault(fp, []).append(_ts(row))
    alerts: list[SuspiciousPatternAlert] = []
    for fp, timestamps in by_fp.items():
        ok, first_ts, last_ts = _has_window_burst(
            timestamps,
            count=threshold["count"],
            window_s=threshold["window_s"],
        )
        if not ok:
            continue
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_LOGIN_FAIL_BURST,
            severity=DEFAULT_RULE_SEVERITIES[RULE_LOGIN_FAIL_BURST],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "attempted_user_fp": fp,
                "fail_count": len(timestamps),
                "window_s": threshold["window_s"],
                "first_ts": first_ts,
                "last_ts": last_ts,
                "threshold": threshold["count"],
            }),
        ))
    return alerts


def _detect_bot_challenge_fail_spike(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_BOT_CHALLENGE_FAIL_SPIKE — group bot_challenge_fail rows by
    form_path, fire one alert per form path with count >= threshold."""
    by_form: dict[str, list[float]] = {}
    for row in rows:
        if _action(row) != ae.EVENT_AUTH_BOT_CHALLENGE_FAIL:
            continue
        after = _after(row)
        form_path = after.get("form_path")
        if not isinstance(form_path, str) or not form_path:
            continue
        by_form.setdefault(form_path, []).append(_ts(row))
    alerts: list[SuspiciousPatternAlert] = []
    for form_path, timestamps in by_form.items():
        ok, first_ts, last_ts = _has_window_burst(
            timestamps,
            count=threshold["count"],
            window_s=threshold["window_s"],
        )
        if not ok:
            continue
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_BOT_CHALLENGE_FAIL_SPIKE,
            severity=DEFAULT_RULE_SEVERITIES[RULE_BOT_CHALLENGE_FAIL_SPIKE],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "form_path": form_path,
                "fail_count": len(timestamps),
                "window_s": threshold["window_s"],
                "first_ts": first_ts,
                "last_ts": last_ts,
                "threshold": threshold["count"],
            }),
        ))
    return alerts


def _detect_token_refresh_storm(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_TOKEN_REFRESH_STORM — group token_refresh rows by entity_id
    (= ``provider:user_id``), fire one alert per entity with count >= threshold.
    """
    by_entity: dict[str, list[float]] = {}
    for row in rows:
        if _action(row) != ae.EVENT_AUTH_TOKEN_REFRESH:
            continue
        eid = _entity_id(row)
        if not eid:
            continue
        by_entity.setdefault(eid, []).append(_ts(row))
    alerts: list[SuspiciousPatternAlert] = []
    for eid, timestamps in by_entity.items():
        ok, first_ts, last_ts = _has_window_burst(
            timestamps,
            count=threshold["count"],
            window_s=threshold["window_s"],
        )
        if not ok:
            continue
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_TOKEN_REFRESH_STORM,
            severity=DEFAULT_RULE_SEVERITIES[RULE_TOKEN_REFRESH_STORM],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "entity_id": eid,
                "refresh_count": len(timestamps),
                "window_s": threshold["window_s"],
                "first_ts": first_ts,
                "last_ts": last_ts,
                "threshold": threshold["count"],
            }),
        ))
    return alerts


def _detect_honeypot_triggered(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_HONEYPOT_TRIGGERED — bright-line: any bot_challenge_fail row
    with reason="honeypot" fires.  Aggregates per form_path so the UI
    sees one alert per affected form (not one per row), with the row
    count attached as evidence.
    """
    by_form: dict[str, list[float]] = {}
    for row in rows:
        if _action(row) != ae.EVENT_AUTH_BOT_CHALLENGE_FAIL:
            continue
        after = _after(row)
        if after.get("reason") != ae.BOT_CHALLENGE_FAIL_HONEYPOT:
            continue
        form_path = after.get("form_path")
        if not isinstance(form_path, str) or not form_path:
            continue
        by_form.setdefault(form_path, []).append(_ts(row))
    alerts: list[SuspiciousPatternAlert] = []
    threshold_count = max(1, int(threshold.get("count", 1)))
    for form_path, timestamps in by_form.items():
        if len(timestamps) < threshold_count:
            continue
        timestamps.sort()
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_HONEYPOT_TRIGGERED,
            severity=DEFAULT_RULE_SEVERITIES[RULE_HONEYPOT_TRIGGERED],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "form_path": form_path,
                "trigger_count": len(timestamps),
                "first_ts": timestamps[0],
                "last_ts": timestamps[-1],
                "threshold": threshold_count,
            }),
        ))
    return alerts


def _detect_oauth_revoke_relink_loop(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_OAUTH_REVOKE_RELINK_LOOP — count revoke + connect pairs
    per ``provider:user_id`` within the window.  One pair = one
    revoke followed (in time) by a connect on the same entity.
    """
    by_entity: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        action = _action(row)
        if action not in {ae.EVENT_AUTH_OAUTH_CONNECT, ae.EVENT_AUTH_OAUTH_REVOKE}:
            continue
        eid = _entity_id(row)
        if not eid:
            continue
        by_entity.setdefault(eid, []).append((_ts(row), action))
    alerts: list[SuspiciousPatternAlert] = []
    threshold_count = int(threshold["count"])
    window_s = int(threshold["window_s"])
    for eid, events in by_entity.items():
        events.sort(key=lambda p: p[0])
        # Sliding window: count complete revoke→connect cycles in any
        # window_s window.  Walk the list; whenever we see a
        # ``revoke`` followed later (within the same window) by a
        # ``connect`` we count one cycle.  We track cycles inside a
        # rolling window keyed on the cycle's *connect* timestamp.
        cycle_ts: list[float] = []
        last_revoke_ts: Optional[float] = None
        for ts, kind in events:
            if kind == ae.EVENT_AUTH_OAUTH_REVOKE:
                last_revoke_ts = ts
            elif kind == ae.EVENT_AUTH_OAUTH_CONNECT and last_revoke_ts is not None:
                cycle_ts.append(ts)
                last_revoke_ts = None  # consume the pair
        ok, first_ts, last_ts = _has_window_burst(
            cycle_ts, count=threshold_count, window_s=window_s,
        )
        if not ok:
            continue
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_OAUTH_REVOKE_RELINK_LOOP,
            severity=DEFAULT_RULE_SEVERITIES[RULE_OAUTH_REVOKE_RELINK_LOOP],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "entity_id": eid,
                "cycle_count": len(cycle_ts),
                "window_s": window_s,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "threshold": threshold_count,
            }),
        ))
    return alerts


def _detect_distributed_login_fail(
    rows: list[Mapping[str, Any]],
    tenant_id: str,
    threshold: Mapping[str, int],
) -> list[SuspiciousPatternAlert]:
    """RULE_DISTRIBUTED_LOGIN_FAIL — one attempted_user_fp from many
    distinct ip_fp values within the window.
    """
    by_fp: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        if _action(row) != ae.EVENT_AUTH_LOGIN_FAIL:
            continue
        after = _after(row)
        user_fp = after.get("attempted_user_fp")
        ip_fp = after.get("ip_fp")
        if not isinstance(user_fp, str) or not user_fp:
            continue
        if not isinstance(ip_fp, str) or not ip_fp:
            continue
        by_fp.setdefault(user_fp, []).append((_ts(row), ip_fp))
    alerts: list[SuspiciousPatternAlert] = []
    threshold_count = int(threshold["count"])
    window_s = int(threshold["window_s"])
    for user_fp, events in by_fp.items():
        events.sort(key=lambda p: p[0])
        # Sliding window over time; track the set of distinct ip_fp
        # values inside the window.  When the set size hits the
        # threshold, fire.
        left = 0
        seen_ips: dict[str, int] = {}
        triggered = False
        first_ts: Optional[float] = None
        last_ts: Optional[float] = None
        winner_ips: tuple[str, ...] = ()
        for right in range(len(events)):
            ts_r, ip_r = events[right]
            seen_ips[ip_r] = seen_ips.get(ip_r, 0) + 1
            while ts_r - events[left][0] > window_s:
                ts_l, ip_l = events[left]
                seen_ips[ip_l] -= 1
                if seen_ips[ip_l] <= 0:
                    del seen_ips[ip_l]
                left += 1
            if len(seen_ips) >= threshold_count:
                triggered = True
                first_ts = events[left][0]
                last_ts = ts_r
                winner_ips = tuple(sorted(seen_ips.keys()))
                break
        if not triggered:
            continue
        alerts.append(SuspiciousPatternAlert(
            rule=RULE_DISTRIBUTED_LOGIN_FAIL,
            severity=DEFAULT_RULE_SEVERITIES[RULE_DISTRIBUTED_LOGIN_FAIL],
            tenant_id=tenant_id,
            evidence=MappingProxyType({
                "attempted_user_fp": user_fp,
                "distinct_ip_count": len(winner_ips),
                "ip_fps": winner_ips,
                "window_s": window_s,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "threshold": threshold_count,
            }),
        ))
    return alerts


# Mapping rule → detector function.  Frozen so adding a rule must touch
# both this dict AND :data:`ALL_DASHBOARD_RULES` (the drift guard
# enforces equality between the two key sets).
_DETECTORS: Mapping[str, Any] = MappingProxyType({
    RULE_LOGIN_FAIL_BURST: _detect_login_fail_burst,
    RULE_BOT_CHALLENGE_FAIL_SPIKE: _detect_bot_challenge_fail_spike,
    RULE_TOKEN_REFRESH_STORM: _detect_token_refresh_storm,
    RULE_HONEYPOT_TRIGGERED: _detect_honeypot_triggered,
    RULE_OAUTH_REVOKE_RELINK_LOOP: _detect_oauth_revoke_relink_loop,
    RULE_DISTRIBUTED_LOGIN_FAIL: _detect_distributed_login_fail,
})


def detect_suspicious_patterns(
    rows: Iterable[Mapping[str, Any]],
    *,
    tenant_id: str,
    thresholds: Optional[Mapping[str, Mapping[str, int]]] = None,
    enabled_rules: Optional[Iterable[str]] = None,
) -> tuple[SuspiciousPatternAlert, ...]:
    """Apply every dashboard rule to *rows* and return the alerts.

    Pure function — no IO.  Materialises *rows* once into a list so each
    detector can re-iterate without consuming a generator.

    *thresholds* overrides the per-rule count / window_s defaults; a
    caller may override only the keys it cares about.  *enabled_rules*
    restricts evaluation to a subset of :data:`ALL_DASHBOARD_RULES`
    (default: all rules enabled).

    Order: returned alerts are stable — sorted by ``(rule, evidence
    serialised key)`` so two evaluations over byte-equal input produce
    byte-equal output (helps the AS.7.x notification de-dup logic
    spot already-fired alerts).
    """
    rule_set = (
        tuple(enabled_rules) if enabled_rules is not None else ALL_DASHBOARD_RULES
    )
    materialised = list(rows)
    out: list[SuspiciousPatternAlert] = []
    for rule in rule_set:
        if rule not in _DETECTORS:
            raise ValueError(f"unknown dashboard rule: {rule!r}")
        threshold = _resolve_threshold(rule, thresholds)
        detector = _DETECTORS[rule]
        out.extend(detector(materialised, tenant_id, threshold))
    out.sort(key=lambda a: (a.rule, _alert_sort_key(a)))
    return tuple(out)


def _alert_sort_key(alert: SuspiciousPatternAlert) -> str:
    """Stable secondary sort key for an alert.

    Per-rule, picks the field that uniquely identifies the alert
    subject (attempted_user_fp / form_path / entity_id) — gives a
    deterministic order across evaluations even when several alerts
    share the same rule.
    """
    ev = alert.evidence
    for key in ("attempted_user_fp", "form_path", "entity_id"):
        v = ev.get(key)
        if isinstance(v, str):
            return v
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Async orchestrator — knob → fetch → summarise + detect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def _fetch_auth_rows(
    conn,
    *,
    tenant_id: str,
    since: Optional[float],
    until: Optional[float],
    limit: int,
) -> list[dict[str, Any]]:
    """Pull the eight ``auth.*`` rollup events for *tenant_id* out of
    ``audit_log``.

    Returns up to *limit* rows ordered ASC by id (stable — the rules
    rely on time-monotonic processing).  Filters by
    ``action LIKE 'auth.%'`` so other event families (forensic
    ``oauth.*``, vendor ``bot_challenge.*``, honeypot ``honeypot.*``)
    don't pollute the rollup counts.
    """
    import json
    where: list[str] = ["a.tenant_id = $1", "a.action LIKE 'auth.%'"]
    params: list[Any] = [tenant_id]
    if since is not None:
        where.append(f"a.ts >= ${len(params) + 1}")
        params.append(since)
    if until is not None:
        where.append(f"a.ts <= ${len(params) + 1}")
        params.append(until)
    where_sql = " WHERE " + " AND ".join(where)
    params.append(int(limit))
    sql = (
        "SELECT a.id, a.ts, a.actor, a.action, a.entity_kind, a.entity_id, "
        "a.before_json, a.after_json "
        "FROM audit_log a"
        + where_sql
        + f" ORDER BY a.id ASC LIMIT ${len(params)}"
    )
    rows = await conn.fetch(sql, *params)
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "entity_kind": r["entity_kind"],
            "entity_id": r["entity_id"],
            "before": json.loads(r["before_json"] or "{}"),
            "after": json.loads(r["after_json"] or "{}"),
        }
        for r in rows
    ]


def _empty_result(
    tenant_id: str,
    *,
    since: Optional[float],
    until: Optional[float],
    knob_off: bool,
) -> DashboardResult:
    return DashboardResult(
        summary=empty_summary(tenant_id, since=since, until=until),
        alerts=(),
        knob_off=knob_off,
        row_count_observed=0,
        row_count_truncated=False,
    )


async def compute_dashboard(
    tenant_id: str,
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
    limit: int = LIMIT_ROWS_DEFAULT,
    thresholds: Optional[Mapping[str, Mapping[str, int]]] = None,
    enabled_rules: Optional[Iterable[str]] = None,
    conn: Any = None,
) -> DashboardResult:
    """Compute the per-tenant dashboard payload.

    Knob-off (AS.0.8 §6) ⇒ returns the empty-summary banner shape
    immediately, no DB read.  Otherwise pulls up to *limit* ``auth.*``
    rows for *tenant_id*, summarises them, runs the suspicious-pattern
    rules, and returns a :class:`DashboardResult`.

    *conn* is polymorphic (mirrors :func:`backend.audit.log`): pass
    your request-scoped pool conn, or omit and the function borrows
    from the pool via :func:`backend.db_pool.get_pool`.
    """
    if not _gate():
        return _empty_result(
            tenant_id, since=since, until=until, knob_off=True,
        )

    if conn is None:
        from backend.db_pool import get_pool
        async with get_pool().acquire() as owned_conn:
            rows = await _fetch_auth_rows(
                owned_conn,
                tenant_id=tenant_id,
                since=since,
                until=until,
                limit=limit,
            )
    else:
        rows = await _fetch_auth_rows(
            conn,
            tenant_id=tenant_id,
            since=since,
            until=until,
            limit=limit,
        )

    summary = summarise(
        rows, tenant_id=tenant_id, since=since, until=until,
    )
    alerts = detect_suspicious_patterns(
        rows,
        tenant_id=tenant_id,
        thresholds=thresholds,
        enabled_rules=enabled_rules,
    )
    return DashboardResult(
        summary=summary,
        alerts=alerts,
        knob_off=False,
        row_count_observed=len(rows),
        row_count_truncated=len(rows) >= limit,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface — stable export list
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    # Six rule constants + tuple
    "RULE_LOGIN_FAIL_BURST",
    "RULE_BOT_CHALLENGE_FAIL_SPIKE",
    "RULE_TOKEN_REFRESH_STORM",
    "RULE_HONEYPOT_TRIGGERED",
    "RULE_OAUTH_REVOKE_RELINK_LOOP",
    "RULE_DISTRIBUTED_LOGIN_FAIL",
    "ALL_DASHBOARD_RULES",
    # Severities
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "SEVERITY_CRITICAL",
    "SEVERITIES",
    # Defaults
    "DEFAULT_THRESHOLDS",
    "DEFAULT_RULE_SEVERITIES",
    "LIMIT_ROWS_DEFAULT",
    # Frozen output dataclasses
    "DashboardSummary",
    "SuspiciousPatternAlert",
    "DashboardResult",
    # Pure helpers
    "summarise",
    "detect_suspicious_patterns",
    "empty_summary",
    # Knob hook
    "is_enabled",
    # Async orchestrator
    "compute_dashboard",
]

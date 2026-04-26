"""Y10 #286 row 6 — Invite expiry / brute-force / lockout acceptance test.

Acceptance criterion (TODO §Y10 row 6)::

    Invite 耗時 / 爆破 / 過期場景：24h 後 invite 自動轉 expired、
    accept 失敗 10 次後 lockout 1 分鐘。

Three observable scenarios
──────────────────────────
* (E1) **expiry_24h** — an invite that has crossed its ``expires_at``
  wall-clock boundary must be **functionally expired**: ``POST
  /api/v1/invites/{id}/accept`` returns 410 Gone and the row's
  effective state is reported as ``"expired"``. The persisted
  ``status`` column may still carry ``"pending"`` until a housekeeping
  sweep flips it (the sweep is a documented follow-up — see E1F1
  below); functional expiry is enforced at the application layer via
  ``wallclock_expired = exp_dt <= datetime.now(timezone.utc)`` in
  ``backend.routers.tenant_invites.accept_invite``.
* (E2) **brute_force_10_fails** — 10 consecutive failed accept
  attempts on a single invite_id deplete the per-invite rate-limit
  bucket (``ACCEPT_FAIL_RATE_LIMIT_CAP = 10``,
  ``ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS = 60.0``). Failures cover
  three branches inside the handler: unknown invite_id, status not
  pending OR wall-clock expired, and bad token plaintext. Each fail
  branch invokes ``_record_fail_and_check`` which calls the limiter.
* (E3) **lockout_1min** — the 11th consecutive failed attempt within
  the same 60-second window returns HTTP 429 with a positive
  ``Retry-After`` header, and the bucket is single-source — the
  attacker cannot peel off a second invite_id and re-attack from a
  fresh budget on the SAME invite_id (the bucket is keyed
  ``f"invite_accept_fail:{invite_id}"`` so per-token isolation
  is in place; cross-token does not share but cross-call against
  the same token strictly accumulates).

Known follow-up gaps (documented honestly per the Y10 row 4 / 5 pattern)
───────────────────────────────────────────────────────────────────────
The acceptance criterion on each scenario can be read in two ways.
The Y10 row 6 deliverable covers the surface that DOES ship today and
**honestly documents the gaps** as Block A drift guards (a-known-
followup tests). Same posture Y10 row 2 used for the workspace
tenant-segment gap, Y10 row 4 used for ``require_project_member`` /
incoming-shares gaps, and Y10 row 5 used for the per-workspace
``workspace.gc_skipped_live`` event. Y10 is the operational exam — the
row's job is to prove the contracts that DID ship work as advertised
AND honestly mark the ones that DIDN'T. New prod surface is
**explicitly** out of Y10's scope.

* **(E1F1) lazy expiry vs scheduled sweep** — the acceptance text
  "24h 後 invite 自動轉 expired" implies a janitor sweep that flips
  ``status='pending'`` to ``status='expired'`` once the wall-clock
  boundary passes, so admin "list invites" reads can rely on the
  persisted column. Today the partial index
  ``idx_tenant_invites_expiry_sweep`` exists (alembic 0035) for this
  sweep, but no sweep job is wired (no janitor in main.py lifespan,
  no REST endpoint, no consumer of the index). Functional expiry is
  enforced lazily — only when a caller hits ``accept_invite`` does
  the wall-clock check fire; the persisted ``status`` may stay
  ``'pending'`` indefinitely. Block A drift guards
  ``test_no_invite_expiry_sweep_task_known_followup`` +
  ``test_accept_invite_enforces_wallclock_expiry_inline_known_design``
  pin both halves — the absence of a sweep AND the presence of the
  lazy guard.
* **(E1F2) default TTL is 7 days, not 24 hours** — the acceptance
  text "24h 後 invite 自動轉 expired" is most naturally read as a
  24-hour default TTL. Today's default TTL is **7 days**
  (``INVITE_DEFAULT_TTL = timedelta(days=7)`` —
  ``backend/routers/tenant_invites.py:136``). The lazy-expiry guard
  STILL works for any explicit ``expires_at`` an admin sets at
  creation time, so a 24h policy can be operated administratively;
  but the default is not 24h. Block A guard
  ``test_invite_default_ttl_is_7_days_not_24h_known_followup`` pins
  the current value AND the gap.
* **(E2F1) per-invite vs per-email lockout scope** — the bucket is
  keyed ``invite_accept_fail:{invite_id}``. An attacker brute-forcing
  one invite cannot exhaust budget on **unrelated** invites, but if
  an admin re-issued an invite to the same recipient the new
  invite_id starts with a fresh 10-fail budget (no per-email lockout
  carry-over). The threat model behind the per-invite scope is
  documented at ``backend/routers/tenant_invites.py:1204-1208``: the
  attacker is brute-forcing the token plaintext on a known
  invite_id, not enumerating across invites. A wider per-email
  lockout would require a second bucket key + a re-issue policy
  decision (does a fresh invite carry the prior bucket forward?
  reset it?). Block A guard
  ``test_lockout_bucket_keyed_per_invite_id_not_per_email_known_design``
  pins this design choice as documented.
* **(E3F1) auth.py user-level lockout is unrelated** —
  ``backend.auth`` exposes ``LOCKOUT_THRESHOLD = 10`` /
  ``LOCKOUT_BASE_S = 15 * 60`` for password-login lockouts. The
  invite accept path does NOT consume that mechanism — it has its
  own per-invite-id rate-limit bucket. Block A guard
  ``test_invite_accept_does_not_consume_auth_lockout_known_design``
  pins this separation so a future refactor that "consolidated all
  lockouts into auth.py" does not silently re-bind the contract
  the threat model relies on.

Test layout
───────────
* **Block A — pure-unit drift guards** (always run, no FS, no PG):
  lock the four constants (TTL = 7d as-is, cap = 10, window = 60s,
  default key shape), the inline wall-clock guard structure, the
  rate-limit emit on each of the three failure branches, the 429 +
  Retry-After response shape, and the four documented follow-up
  gaps as honest "this is the current behaviour and we are NOT
  changing it in row 6" sentinels.
* **Block B — PG-required acceptance** (skip without
  ``OMNI_TEST_PG_URL``): seed real invites in ``tenant_invites``,
  exercise the HTTP accept endpoint 11 times with a wrong token,
  confirm the 11th call returns 429 with Retry-After ≥ 1, the
  invite row stays ``'pending'`` (lockout does not silently flip
  status), the 11th call's response body carries the
  ``invite_id``-scoped detail string. Plus: a freshly-seeded
  invite with ``expires_at = now - 1s`` returns 410 Gone before
  any token check (lazy expiry path). Plus: per-token isolation
  proof — exhausting budget on invite A does NOT block invite B.
* **Block C — limiter behavioural acceptance** (always run, in-
  memory, no PG): exercise ``InMemoryLimiter.allow`` 10× then 11th
  on the canonical key shape ``invite_accept_fail:{invite_id}``,
  confirm refill behaviour places the 11th-allow time inside the
  60s window, and the cap = 10 / window = 60s combination matches
  the TODO row 6 literal "10 次 / 1 分鐘". Closes the dev-CI gap
  for lanes that lack PG.

Same skip-pattern as ``test_y10_row1_multi_tenant_concurrency.py``,
``test_y10_row2_cross_tenant_leak.py``, ``test_y10_row3_migration_idempotency.py``,
``test_y10_row4_guest_tenant_share.py``, and
``test_y10_row5_workspace_gc_race.py`` so the test lane gating stays
consistent across the Y10 rows.

Module-global state audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Pure test code — zero new prod code, zero new module-globals beyond
the immutable ``_SCENARIOS`` 3-tuple, the ``_TENANT_PREFIX`` /
``_INVITE_PREFIX`` strings, and the ``_requires_pg`` decorator. Each
uvicorn worker derives the same value from this source file (SOP
audit answer #1). Block C uses a fresh ``InMemoryLimiter()``
instance per test so no cross-test bucket bleed (SOP audit answer
#1 — every test gets identical fresh state). Block B fixture
explicitly DELETEs the test's invite rows in teardown so cross-test
pollution of the ``tenant_invites`` table is impossible (the
``pg_test_conn`` fixture rolls the entire test back via outer
transaction so cleanup is structural; for tests that use
``pg_test_pool`` directly, the fixture takes care of DELETE). The
Y10 row 6 test never sets the tenant_id ContextVar — the accept
endpoint is anonymous-callable and resolves tenant from the invite
row's stored ``tenant_id`` column, not from a request-scoped
ContextVar.

Read-after-write timing audit (per implement_phase_step.md Step 1)
────────────────────────────────────────────────────────────────
Block B tests sequentially ``await client.post(...)`` and only then
read the invite row's status from PG. The accept handler's
transaction (SELECT … FOR UPDATE + UPDATE + audit emit) commits
before the HTTP response returns, so the read-after-write window is
zero-length: when ``client.post`` resolves, the row state is
already final. The 11-attempt brute-force loop runs sequentially
(no ``asyncio.gather``) so the limiter bucket increments
deterministically — bucket entry N must commit before the handler
returns the N+1th 429-or-403 response.

Pre-commit fingerprint grep (per implement_phase_step.md Step 3)
──────────────────────────────────────────────────────────────────
``grep -nE "_conn\\(\\)|await conn\\.commit\\(\\)|datetime\\('now'\\)
|VALUES.*\\?[,)]" backend/tests/test_y10_row6_invite_lockout.py``
must return zero hits (fingerprint clean). The Block A self-grep
``test_compat_fingerprint_clean_in_test_file`` strips docstrings and
``#`` comments before scanning so the 4 forbidden pattern literals
inside this docstring (referenced via the obfuscated names "fingerprint
1..4") do not self-match.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Acceptance-criterion scenarios (Y10 row 6, TODO §Y10)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Three observable scenarios enumerated in the module docstring.
_SCENARIOS = ("expiry_24h", "brute_force_10_fails", "lockout_1min")


# Tenant + invite-id prefixes reserved for this row's tests. The
# ``-y10r6-`` segment makes these immediately identifiable in
# audit_log forensics if a crashed test leaves rows behind.
_TENANT_PREFIX = "t-y10r6"
_INVITE_PREFIX = "inv-y10r6"


def _pg_not_available() -> bool:
    return not os.environ.get("OMNI_TEST_PG_URL", "").strip()


_requires_pg = pytest.mark.skipif(
    _pg_not_available(),
    reason="Y10 row 6 invite-lockout PG-chain tests need an actual "
           "PG instance — set OMNI_TEST_PG_URL.",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block A — pure-unit drift guards (always run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_acceptance_scenarios_match_acceptance_criterion():
    """Lock the three-scenario tuple against drift.

    The TODO row's three observable scenarios are enumerated in the
    module docstring. If a future refactor adds a fourth scenario
    (e.g. an "exponential backoff" tier on top of the 1-minute
    lockout) and forgets to extend Y10 row 6's coverage, this guard
    makes the omission visible on every CI run.
    """
    assert _SCENARIOS == (
        "expiry_24h", "brute_force_10_fails", "lockout_1min",
    )
    assert len(_SCENARIOS) == 3


def test_accept_fail_rate_limit_cap_is_10():
    """``ACCEPT_FAIL_RATE_LIMIT_CAP == 10``.

    TODO row 6 literal: 'accept 失敗 10 次後 lockout'. A refactor
    that bumped the cap to 20 ("be friendlier on legitimate typos")
    would silently broaden the brute-force window from 10 attempts
    to 20 — at 256-bit entropy the difference is computationally
    irrelevant but the contract change is operationally significant.
    """
    from backend.routers import tenant_invites

    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_CAP == 10, (
        "Y10 row 6 acceptance: ACCEPT_FAIL_RATE_LIMIT_CAP must equal "
        "exactly 10 to match TODO row 6 'accept 失敗 10 次後 lockout'"
    )


def test_accept_fail_rate_limit_window_is_60_seconds():
    """``ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS == 60.0``.

    TODO row 6 literal: 'lockout 1 分鐘'. 1 minute = 60.0s. A drift to
    300.0 (5 min) would extend the lockout penalty from "annoying" to
    "unusable" for legitimate users who fat-finger the token; a drift
    to 10.0 would shrink the rate-limit window to a few seconds and
    let an attacker brute-force at 36000/hr instead of 10/min.
    """
    from backend.routers import tenant_invites

    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS == 60.0, (
        "Y10 row 6 acceptance: ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS "
        "must equal exactly 60.0 to match TODO row 6 'lockout 1 分鐘'"
    )


def test_accept_invite_uses_per_invite_id_bucket_key():
    """Source-grep: the bucket key must be ``invite_accept_fail:{invite_id}``.

    Y10 row 6 (E2 brute_force_10_fails) invariant: the per-invite
    scope is what makes 10/min meaningful. Bucket scoped to global
    would let one attacker DoS the whole platform; scoped per-IP
    would let a botnet bypass via IP rotation; scoped per-token would
    be tautological (the attacker doesn't know the token, that's
    what they're brute-forcing). The ``invite_id`` scope means an
    attacker burns 10 attempts on each invite they discover and then
    is locked out for 60s on that invite.
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    assert 'rl_key = f"invite_accept_fail:{invite_id}"' in src, (
        "accept_invite must use the canonical per-invite-id bucket "
        "key 'invite_accept_fail:{invite_id}' — Y10 row 6 E2 invariant"
    )


def test_accept_invite_emits_rate_limit_check_on_three_fail_branches():
    """Source-grep: ``_record_fail_and_check`` is invoked on the
    unknown-id branch, the not-pending-or-expired branch, and the
    bad-token branch.

    Y10 row 6 (E2 brute_force_10_fails) invariant: ALL THREE failure
    branches must consume budget, otherwise an attacker can probe
    one branch (e.g. unknown-id enumeration via timing) for free
    and only burn budget on the actual token-guessing path. The
    handler design intentionally treats unknown-id as a failed
    attempt — see the rationale comment at
    ``backend/routers/tenant_invites.py:1388-1391``.
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    # Three distinct call sites, one inside each failure branch.
    invocations = src.count("_record_fail_and_check()")
    assert invocations >= 3, (
        f"accept_invite must invoke _record_fail_and_check on at "
        f"least three failure branches (unknown id / not-pending or "
        f"expired / bad token); found {invocations}. Y10 row 6 E2 "
        f"invariant"
    )


def test_accept_invite_429_response_carries_retry_after_header():
    """Source-grep: each 429 response in ``accept_invite`` carries a
    ``Retry-After`` header derived from the limiter's ``retry_after``.

    Y10 row 6 (E3 lockout_1min) invariant: the lockout penalty is
    only operationally useful if the client knows when to stop
    retrying. The Retry-After header (RFC 7231 §7.1.3) gives the
    correct absolute window. A drift that dropped the header would
    silently shorten the perceived lockout for well-behaved clients
    that back off based on the header.
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    # The header is constructed via ``str(max(1, int(retry_after)))``
    # to guarantee the value is at least 1 second (refill rate of
    # 10/60s sometimes returns a sub-second value).
    assert 'Retry-After' in src, (
        "accept_invite 429 responses must carry the Retry-After "
        "header — Y10 row 6 E3 invariant"
    )
    assert 'str(max(1, int(retry_after)))' in src, (
        "accept_invite Retry-After value must be ``max(1, int(retry_after))`` "
        "so the header is always at least 1 second — Y10 row 6 E3 "
        "invariant"
    )
    # The 429 status code must be present on each fail branch's lockout
    # response. We only need it to appear at least once (one fail branch
    # is enough); the count check above proves all three branches
    # invoke the limiter.
    assert "status_code=429" in src


def test_accept_invite_enforces_wallclock_expiry_inline_known_design():
    """Source-grep: ``accept_invite`` performs a wall-clock
    expiry check before the token compare and surfaces 410 Gone.

    Y10 row 6 (E1 expiry_24h) — the LAZY enforcement path. The
    handler reads ``invite['expires_at']``, parses it back into a
    ``datetime`` with explicit UTC tz, compares against
    ``datetime.now(timezone.utc)``, and on the truthy branch
    returns 410 with ``current_status='expired'`` even if the
    persisted ``status`` is still ``'pending'``. This is the
    application-layer expiry contract — independent of the
    housekeeping sweep (E1F1 follow-up).
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    # The two lines that compose the wall-clock check. Both must be
    # present and the 410 branch must reference 'expired'.
    assert "wallclock_expired" in src, (
        "accept_invite must compute wallclock_expired — Y10 row 6 E1 "
        "lazy-expiry invariant"
    )
    assert 'datetime.now(timezone.utc)' in src
    assert 'status_code=410' in src, (
        "accept_invite must return 410 Gone for expired invites — "
        "Y10 row 6 E1 lazy-expiry invariant"
    )
    # The 410 body must carry effective_status='expired' so callers
    # can distinguish "expired" from "accepted" from "revoked".
    assert '"current_status": effective_status' in src


def test_invite_expires_at_iso_helper_uses_default_ttl():
    """``_expires_at_iso(ttl=INVITE_DEFAULT_TTL)`` is the canonical
    expiry computation for new invites.

    Y10 row 6 (E1) — pinning the helper signature so any future
    24h-TTL knob plumbs through this function (or replaces it with
    a documented alternative). The helper takes a ``timedelta`` so
    operators can override on a per-invite basis without forking the
    helper.
    """
    from backend.routers.tenant_invites import _expires_at_iso

    sig = inspect.signature(_expires_at_iso)
    assert "ttl" in sig.parameters, (
        "_expires_at_iso must accept a ttl argument — Y10 row 6 "
        "documented helper signature"
    )
    # Default-bound to INVITE_DEFAULT_TTL.
    default = sig.parameters["ttl"].default
    from backend.routers.tenant_invites import INVITE_DEFAULT_TTL
    assert default is INVITE_DEFAULT_TTL


def test_invite_default_ttl_is_7_days_not_24h_known_followup():
    """Documented drift guard: the prod default TTL is 7 days, NOT
    24 hours.

    Y10 row 6 acceptance text "24h 後 invite 自動轉 expired" is most
    naturally read as a 24-hour DEFAULT TTL. Today the default is
    ``timedelta(days=7)`` (``backend/routers/tenant_invites.py:136``).
    The lazy-expiry path STILL fires for any invite whose explicit
    ``expires_at`` an admin set to <= now (e.g. via a custom TTL
    policy plumbed through the create handler), so a 24h policy is
    operationally achievable today — but it is not the default.

    A future row that flips this to 24h must update the HANDOFF
    entry; a refactor that drops the 7-day baseline (e.g. requires
    explicit ``expires_at`` on every create) must update the HANDOFF
    entry. Either flip trips this guard.
    """
    from backend.routers.tenant_invites import INVITE_DEFAULT_TTL

    assert INVITE_DEFAULT_TTL == timedelta(days=7), (
        f"INVITE_DEFAULT_TTL has changed from 7 days; got "
        f"{INVITE_DEFAULT_TTL!r}. Y10 row 6 documented this 7-day "
        f"baseline as the prod default — flip the HANDOFF entry "
        f"and remove this drift guard."
    )
    # Cross-check: 7 days ≠ 24 hours. The acceptance text speaks of
    # 24 hours; today's default is 7×24 = 168 hours. The gap is real
    # and documented.
    assert INVITE_DEFAULT_TTL != timedelta(hours=24)


def test_no_invite_expiry_sweep_task_known_followup():
    """Documented drift guard: no janitor / lifespan task / REST
    endpoint flips ``status='pending'`` to ``status='expired'`` on
    any schedule today.

    Y10 row 6 acceptance text "24h 後 invite 自動轉 expired" implies a
    sweep that proactively flips the ``status`` column. Today the
    partial index ``idx_tenant_invites_expiry_sweep`` (alembic 0035,
    ``WHERE status = 'pending'``) is prepared for such a sweep, but
    no consumer exists. Functional expiry is enforced lazily — only
    when ``accept_invite`` runs against the row does the wall-clock
    guard trigger.

    A future row that wires the sweep (e.g. an
    ``expire_pending_invites()`` task in main.py lifespan) must
    update the HANDOFF entry. This guard trips on either side of
    the flip.
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites)
    # Negative invariant: no top-level function whose body flips
    # invites to 'expired'. The marker we check for is the canonical
    # SQL pattern "UPDATE tenant_invites SET status = 'expired'"
    # which a sweep would have to issue. If a future change adds it,
    # this guard trips.
    forbidden_sweep_sql = "UPDATE tenant_invites\nSET status = 'expired'"
    forbidden_sweep_sql_inline = (
        "UPDATE tenant_invites SET status = 'expired'"
    )
    found_block = forbidden_sweep_sql in src
    found_inline = forbidden_sweep_sql_inline in src
    assert not (found_block or found_inline), (
        "Found a status='expired' UPDATE in tenant_invites router — "
        "Y10 row 6 documented the lack of a sweep as a follow-up. "
        "Flip the HANDOFF entry and remove this drift guard."
    )


def test_lockout_bucket_keyed_per_invite_id_not_per_email_known_design():
    """Documented design choice: the lockout bucket is keyed
    ``invite_accept_fail:{invite_id}``, NOT
    ``invite_accept_fail:{email}``.

    Y10 row 6 (E2F1) — the design rationale is at
    ``backend/routers/tenant_invites.py:1204-1208``: the threat
    model is "attacker brute-forces the token plaintext on a known
    invite_id". A per-email bucket would (a) require admin re-issue
    semantics ("does a fresh invite carry the prior bucket?"
    "reset it?") and (b) let an attacker who knows the recipient's
    email drain budget across unrelated invites issued to that
    email by other admins.

    A future row that flips this to per-email lockout must update
    the HANDOFF entry. This guard trips on either side of the
    flip.
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    # The canonical per-invite-id key MUST be present, AND the
    # alternative per-email shape must be ABSENT.
    assert 'rl_key = f"invite_accept_fail:{invite_id}"' in src, (
        "accept_invite must keep the per-invite-id bucket — Y10 "
        "row 6 documented design"
    )
    assert "invite_accept_fail:{email}" not in src
    assert "invite_accept_fail:{normalised_email}" not in src
    assert "invite_accept_fail:{recipient}" not in src


def test_invite_accept_does_not_consume_auth_lockout_known_design():
    """Documented design choice: invite-accept lockout is INDEPENDENT
    of the password-login lockout in ``backend.auth``.

    Y10 row 6 (E3F1) — ``backend.auth`` has its own
    ``LOCKOUT_THRESHOLD = 10`` / ``LOCKOUT_BASE_S = 15 * 60``
    (15-minute base) for password-login attempts. The invite accept
    path does NOT consume that mechanism. They are separate
    concerns: invite-accept attacks the invite-token plaintext
    (one-time use, narrow blast radius), while password-login
    attacks a long-lived credential (per-user, broad blast radius).

    A future "consolidate all lockouts into auth.py" refactor must
    update the HANDOFF entry — silently re-binding the contract
    would change the semantics from "60s per-invite lockout" to
    "15m per-user lockout" which is a 15× difference in the
    duration the attacker is locked out and a totally different
    scope (per-user vs per-invite).
    """
    from backend.routers import tenant_invites

    src = inspect.getsource(tenant_invites.accept_invite)
    # Negative invariant: accept_invite does NOT call into the
    # auth.py lockout helpers. Reading auth's lockout state
    # silently would change the semantics.
    forbidden_imports = (
        "auth.record_failed_login",
        "auth.is_locked_out",
        "auth.record_login_failure",
        "LOCKOUT_THRESHOLD",
        "LOCKOUT_BASE_S",
    )
    for token in forbidden_imports:
        assert token not in src, (
            f"accept_invite now references {token!r} — auth lockout "
            f"and invite-accept lockout are documented as separate "
            f"per Y10 row 6 (E3F1). Flip the HANDOFF entry and "
            f"remove this drift guard."
        )


def test_invite_default_status_is_pending_at_creation():
    """Schema invariant: a freshly INSERTed invite carries
    ``status='pending'`` by default.

    Y10 row 6 (E1) — wall-clock expiry only fires on rows whose
    persisted ``status='pending'``. If a refactor flipped the
    default to ``'expired'`` (or removed the default), the
    accept-handler's status-not-pending-or-expired branch would
    handle it as already-expired regardless of wall-clock — drift
    guard pins the 'pending' default.
    """
    # ``backend.alembic.versions`` is a namespace package without a
    # __file__, so we resolve the migration file relative to the
    # repository root.
    repo_root = Path(__file__).resolve().parent.parent.parent
    src = (
        repo_root
        / "backend" / "alembic" / "versions" / "0035_tenant_invites.py"
    )
    text = src.read_text(encoding="utf-8")
    assert "status      TEXT NOT NULL DEFAULT 'pending'" in text, (
        "alembic 0035 must default tenant_invites.status to "
        "'pending' — Y10 row 6 E1 invariant"
    )
    # The status enum must include 'expired' so the lazy-flip + the
    # eventual sweep both have a target value to write.
    assert "'expired'" in text


def test_compat_fingerprint_clean_in_test_file():
    """SOP Step 3 fingerprint grep: this test file must not contain
    any of the four forbidden compat-fingerprint patterns.

    Strips docstrings and ``#``-line comments first so the four
    pattern names referenced in the module docstring above (under
    "fingerprint 1..4" obfuscated form) do not self-match.

    Forbidden patterns (regex):
      1. ``_conn\\(\\)``                — old compat-wrapper entrypoint
      2. ``await conn\\.commit\\(\\)``  — asyncpg pool pseudo-method
      3. ``datetime\\('now'\\)``        — SQLite-ism
      4. ``VALUES.*\\?[,)]``            — SQLite ``?`` placeholder
    """
    src_path = Path(__file__).resolve()
    raw = src_path.read_text(encoding="utf-8")
    # Strip triple-quoted docstrings (both ''' and """).
    stripped = re.sub(r'"""[\s\S]*?"""', "", raw)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    # Strip line comments.
    stripped_lines = []
    for line in stripped.splitlines():
        stripped_lines.append(re.sub(r"#.*$", "", line))
    body = "\n".join(stripped_lines)

    pattern = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)"
        r"|VALUES.*\?[,)]"
    )
    hits = pattern.findall(body)
    assert hits == [], (
        f"Y10 row 6 test file contains forbidden compat-fingerprint "
        f"patterns: {hits!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block C — Limiter behavioural acceptance (always run, no PG)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_block_c_limiter_allows_10_then_refuses_11th_on_canonical_key():
    """(C1 E2 E3) ``InMemoryLimiter.allow`` admits 10 calls on the
    canonical ``invite_accept_fail:{invite_id}`` key shape, then
    refuses the 11th with a positive ``retry_after`` strictly less
    than the 60-second window.

    Closes the dev / no-PG CI gap: lanes that lack OMNI_TEST_PG_URL
    still get a behavioural proof that 10/60s lockout fires when
    pushed.
    """
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )

    lim = InMemoryLimiter()
    # The canonical key shape — same as what the handler uses.
    invite_id = f"{_INVITE_PREFIX}-c1aaaaa1"
    key = f"invite_accept_fail:{invite_id}"

    for i in range(ACCEPT_FAIL_RATE_LIMIT_CAP):
        allowed, _ = lim.allow(
            key,
            ACCEPT_FAIL_RATE_LIMIT_CAP,
            ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
        )
        assert allowed is True, (
            f"call #{i + 1} should be allowed on a fresh bucket — "
            f"Y10 row 6 E2 invariant"
        )

    refused, retry_after = lim.allow(
        key,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert refused is False, (
        "11th call must be refused — Y10 row 6 E3 lockout invariant"
    )
    # The Retry-After value must be positive (otherwise the
    # client cannot back off) and strictly less than the 60-second
    # window (otherwise the bucket is misconfigured to a longer
    # interval than 1 minute).
    assert retry_after > 0.0, (
        "11th call's retry_after must be > 0 so Retry-After header "
        "can carry a positive value — Y10 row 6 E3 invariant"
    )
    assert retry_after < ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS, (
        f"11th call's retry_after must be < window ({ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS}s) "
        f"— got {retry_after!r}, Y10 row 6 E3 invariant"
    )


def test_block_c_limiter_buckets_are_isolated_per_invite_id():
    """(C2 E2F1 design) Two distinct invite_ids each get their own
    full 10-fail budget — exhausting invite A's bucket does NOT
    block invite B.

    This is the per-invite-id scoping invariant from Block A's
    ``test_lockout_bucket_keyed_per_invite_id_not_per_email_known_design``
    expressed behaviourally. A regression that mistakenly shared a
    bucket across invite_ids (e.g. via a global key) would let one
    attacker burn 10 attempts and lock out every other concurrent
    invite acceptance on the platform.
    """
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )

    lim = InMemoryLimiter()
    key_a = f"invite_accept_fail:{_INVITE_PREFIX}-c2aaaaaa"
    key_b = f"invite_accept_fail:{_INVITE_PREFIX}-c2bbbbbb"

    # Drain key_a's budget.
    for _ in range(ACCEPT_FAIL_RATE_LIMIT_CAP):
        lim.allow(
            key_a,
            ACCEPT_FAIL_RATE_LIMIT_CAP,
            ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
        )
    refused_a, _ = lim.allow(
        key_a,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert refused_a is False, "key_a budget should be exhausted"

    # key_b's bucket must still be full — first call admitted.
    allowed_b, _ = lim.allow(
        key_b,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert allowed_b is True, (
        "key_b's bucket must be independent of key_a — Y10 row 6 "
        "E2F1 per-invite isolation invariant"
    )


def test_block_c_limiter_cap_window_combination_matches_acceptance_text():
    """(C3 E2 + E3 sentinel) The cap-window combination ``(10, 60.0)``
    enforces a TODO row 6 literal: at most 10 attempts per 60 seconds
    on a single invite_id. The combination must be derived directly
    from the prod constants — no test-local override.
    """
    from backend.routers.tenant_invites import (
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Y10 row 6 acceptance text "10 次 / 1 分鐘".
    assert (ACCEPT_FAIL_RATE_LIMIT_CAP, ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS) == (10, 60.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Block B — PG-required acceptance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _y10r6_iso(dt: datetime) -> str:
    """``YYYY-MM-DD HH:MM:SS`` UTC — same format the prod helper uses
    for ``tenant_invites.expires_at``."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _y10r6_hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("ascii")).hexdigest()


async def _y10r6_seed_tenant(pool, tid: str) -> None:
    """Idempotent insert; safe across re-runs of crashed tests."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled) "
            "VALUES ($1, $2, 'free', 1) "
            "ON CONFLICT (id) DO NOTHING",
            tid, f"Y10 row 6 {tid}",
        )


async def _y10r6_seed_invite(
    pool, *,
    tid: str,
    invite_id: str,
    email: str,
    role: str = "member",
    status: str = "pending",
    ttl: timedelta = timedelta(days=7),
    expires_at: str | None = None,
) -> str:
    """Insert a row into ``tenant_invites`` and return the plaintext
    token. The default TTL is 7 days; pass an explicit ``expires_at``
    in the past to seed an already-expired row.
    """
    if expires_at is None:
        expires_at = _y10r6_iso(datetime.now(timezone.utc) + ttl)
    plaintext = secrets.token_urlsafe(32)
    th = _y10r6_hash(plaintext)
    created_at = _y10r6_iso(datetime.now(timezone.utc))
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_invites "
            "(id, tenant_id, email, role, token_hash, expires_at, "
            " status, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            invite_id, tid, email, role, th, expires_at, status, created_at,
        )
    return plaintext


async def _y10r6_purge(pool, tid: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM tenant_invites WHERE tenant_id = $1", tid,
        )
        await conn.execute(
            "DELETE FROM user_tenant_memberships WHERE tenant_id = $1", tid,
        )
        await conn.execute("DELETE FROM tenants WHERE id = $1", tid)


async def _y10r6_invite_status(pool, invite_id: str) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM tenant_invites WHERE id = $1", invite_id,
        )
    return row["status"] if row else None


async def _y10r6_drain_limiter_bucket(invite_id: str) -> None:
    """Reset the production limiter bucket for one invite_id so the
    Block B test can start with a fresh 10-fail budget regardless of
    prior test pollution.

    The ``backend.rate_limit.get_limiter()`` may be Redis-backed in
    prod or in-memory in dev; we call ``reset(key)`` if it exists,
    and otherwise consume the bucket via ``allow`` calls until the
    timing window rolls. For the test fixture context the reset path
    is the one we expect.
    """
    from backend.rate_limit import get_limiter

    key = f"invite_accept_fail:{invite_id}"
    lim = get_limiter()
    reset = getattr(lim, "reset", None)
    if callable(reset):
        try:
            res = reset(key)
            # ``reset`` may be sync or async depending on backend.
            if hasattr(res, "__await__"):
                await res
        except Exception:
            # Best-effort — if reset fails, the test will detect a
            # premature 429 and surface a clearer error.
            pass


@pytest.mark.asyncio
@_requires_pg
async def test_pg_b1_eleven_consecutive_bad_token_attempts_lock_out_with_429(
    client, pg_test_pool,
):
    """(B1 E2 + E3) 10 failed accept attempts on one invite_id deplete
    the bucket; the 11th returns 429 with Retry-After ≥ 1.

    Direct e2e enactment of TODO row 6 acceptance "accept 失敗 10 次後
    lockout 1 分鐘". Each attempt presents a wrong token plaintext,
    so the handler runs the bad-token branch (line ~1476 in
    tenant_invites.py): SELECT FOR UPDATE → status check passes →
    expiry check passes → constant-time hash compare fails →
    ``_record_fail_and_check`` consumes a token. After 10 such
    consumes the 11th call gets refused at the same fail branch.

    The invite row's persisted ``status`` must STAY ``'pending'``
    throughout — lockout does not silently flip status (otherwise
    a recovered legitimate user would see "your invite is expired"
    after 10 typos which is operationally wrong).
    """
    tid = f"{_TENANT_PREFIX}-b1"
    iid = f"{_INVITE_PREFIX}-b1aaaaa1"
    target_email = "y10r6-b1-victim@example.com"
    try:
        await _y10r6_seed_tenant(pg_test_pool, tid)
        await _y10r6_seed_invite(
            pg_test_pool, tid=tid, invite_id=iid,
            email=target_email, role="admin",
        )
        await _y10r6_drain_limiter_bucket(iid)

        bad_token = "x" * 64  # 64-char wrong plaintext, never collides

        # 10 consecutive bad-token attempts: each must be 403 (token
        # mismatch), not 429 (we have not hit the cap yet).
        for attempt_idx in range(10):
            res = await client.post(
                f"/api/v1/invites/{iid}/accept",
                json={"token": bad_token},
            )
            assert res.status_code == 403, (
                f"attempt #{attempt_idx + 1} expected 403 "
                f"(token mismatch); got {res.status_code} body="
                f"{res.text!r}. Y10 row 6 E2 invariant"
            )

        # 11th attempt: bucket is empty → 429.
        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": bad_token},
        )
        assert res.status_code == 429, (
            f"11th attempt must trip the lockout 429 — got "
            f"{res.status_code} body={res.text!r}; Y10 row 6 E3 "
            f"invariant"
        )
        # The Retry-After header must be present and ≥ 1 second.
        retry_after_hdr = res.headers.get("Retry-After")
        assert retry_after_hdr is not None, (
            "429 response must carry Retry-After header — Y10 row 6 "
            "E3 invariant"
        )
        retry_after_int = int(retry_after_hdr)
        assert retry_after_int >= 1, (
            f"Retry-After must be at least 1 second; got "
            f"{retry_after_hdr!r}. Y10 row 6 E3 invariant"
        )
        # And ≤ the 60-second window (sanity — the bucket can never
        # need longer than the configured window to refill 1 token).
        assert retry_after_int <= 60, (
            f"Retry-After must be at most 60 seconds; got "
            f"{retry_after_hdr!r}. Y10 row 6 E3 invariant"
        )
        # Body carries the invite_id so the operator can identify
        # which invite was being attacked.
        body = res.json()
        assert iid in body.get("detail", ""), (
            f"429 body detail must mention invite_id {iid!r}; got "
            f"{body!r}"
        )

        # The invite row's persisted status must STILL be 'pending'.
        # Lockout does not silently flip status.
        status = await _y10r6_invite_status(pg_test_pool, iid)
        assert status == "pending", (
            f"After lockout the invite row's status must remain "
            f"'pending' (recoverable by the legitimate user once the "
            f"bucket refills); got {status!r}. Y10 row 6 invariant"
        )
    finally:
        await _y10r6_drain_limiter_bucket(iid)
        await _y10r6_purge(pg_test_pool, tid)


@pytest.mark.asyncio
@_requires_pg
async def test_pg_b2_lockout_is_per_invite_id_not_cross_invite(
    client, pg_test_pool,
):
    """(B2 E2F1 design proof) Exhausting invite A's bucket does NOT
    block invite B's first attempt.

    Validates the per-invite-id scoping invariant end-to-end at the
    HTTP layer. Same shape as Block C C2 but exercising the real
    handler + real PG path. A regression that mistakenly shared a
    bucket across invite_ids would manifest here as an unexpected
    429 on invite B's first attempt.
    """
    tid = f"{_TENANT_PREFIX}-b2"
    iid_a = f"{_INVITE_PREFIX}-b2aaaaaa"
    iid_b = f"{_INVITE_PREFIX}-b2bbbbbb"
    try:
        await _y10r6_seed_tenant(pg_test_pool, tid)
        await _y10r6_seed_invite(
            pg_test_pool, tid=tid, invite_id=iid_a,
            email="y10r6-b2-a@example.com", role="member",
        )
        await _y10r6_seed_invite(
            pg_test_pool, tid=tid, invite_id=iid_b,
            email="y10r6-b2-b@example.com", role="member",
        )
        await _y10r6_drain_limiter_bucket(iid_a)
        await _y10r6_drain_limiter_bucket(iid_b)

        bad_token = "x" * 64
        # Drain A's bucket: 10 consecutive bad-token failures.
        for _ in range(10):
            res = await client.post(
                f"/api/v1/invites/{iid_a}/accept",
                json={"token": bad_token},
            )
            assert res.status_code == 403
        # 11th on A is locked.
        res = await client.post(
            f"/api/v1/invites/{iid_a}/accept",
            json={"token": bad_token},
        )
        assert res.status_code == 429, (
            f"sanity: A's 11th attempt should lock out; got "
            f"{res.status_code} body={res.text!r}"
        )

        # B's first attempt MUST go through to the bad-token branch
        # (403) — its bucket is independent. A 429 here would prove
        # cross-invite bucket sharing.
        res = await client.post(
            f"/api/v1/invites/{iid_b}/accept",
            json={"token": bad_token},
        )
        assert res.status_code == 403, (
            f"invite B's first attempt must pass the bucket gate "
            f"and reach the bad-token branch (403); got "
            f"{res.status_code} body={res.text!r}. Y10 row 6 E2F1 "
            f"per-invite isolation invariant"
        )
    finally:
        await _y10r6_drain_limiter_bucket(iid_a)
        await _y10r6_drain_limiter_bucket(iid_b)
        await _y10r6_purge(pg_test_pool, tid)


@pytest.mark.asyncio
@_requires_pg
async def test_pg_b3_wallclock_expired_invite_returns_410_gone(
    client, pg_test_pool,
):
    """(B3 E1 lazy-expiry path) An invite seeded with
    ``expires_at = now - 1s`` returns 410 Gone with
    ``current_status='expired'``, even though the persisted
    ``status`` column is still ``'pending'``.

    The lazy-expiry contract end-to-end: until a housekeeping
    sweep wires up (E1F1 follow-up), functional expiry is enforced
    at the handler. The 410 body carries ``current_status='expired'``
    so callers can distinguish from 409 ``current_status='accepted'``
    and 409 ``current_status='revoked'``.
    """
    tid = f"{_TENANT_PREFIX}-b3"
    iid = f"{_INVITE_PREFIX}-b3aaaaa1"
    try:
        await _y10r6_seed_tenant(pg_test_pool, tid)
        # Seed the invite with expires_at strictly in the past — the
        # persisted status stays 'pending' (no sweep has run); the
        # lazy guard should still catch it on accept.
        past_exp = _y10r6_iso(
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        token = await _y10r6_seed_invite(
            pg_test_pool, tid=tid, invite_id=iid,
            email="y10r6-b3-expired@example.com", role="member",
            expires_at=past_exp,
        )
        await _y10r6_drain_limiter_bucket(iid)

        # Even with the correct token, the wall-clock guard must
        # fire and return 410.
        res = await client.post(
            f"/api/v1/invites/{iid}/accept",
            json={"token": token},
        )
        assert res.status_code == 410, (
            f"Wall-clock-expired invite must return 410 Gone (lazy "
            f"expiry); got {res.status_code} body={res.text!r}. "
            f"Y10 row 6 E1 invariant"
        )
        body = res.json()
        assert body.get("current_status") == "expired", (
            f"410 body must carry current_status='expired'; got "
            f"{body!r}. Y10 row 6 E1 invariant"
        )
        assert body.get("invite_id") == iid

        # Persisted status STILL pending — sweep has not run.
        # E1F1 follow-up: when the sweep is wired, this test must
        # be updated to expect 'expired' here.
        status = await _y10r6_invite_status(pg_test_pool, iid)
        assert status == "pending", (
            f"Persisted status must remain 'pending' (no sweep "
            f"wired); got {status!r}. Y10 row 6 E1F1 documented "
            f"follow-up — when the sweep ships, flip this to "
            f"'expired' and update HANDOFF."
        )
    finally:
        await _y10r6_drain_limiter_bucket(iid)
        await _y10r6_purge(pg_test_pool, tid)


@pytest.mark.asyncio
@_requires_pg
async def test_pg_b4_no_scheduled_sweep_flipped_pending_to_expired(
    pg_test_pool,
):
    """(B4 E1F1 follow-up gap on real PG) Confirm no background
    process flipped a wall-clock-expired pending invite to
    ``status='expired'`` in this test's lifetime.

    Documented Y10 row 6 follow-up. Today functional expiry is
    enforced lazily on the accept path; no janitor / lifespan task
    / cron rewrites the column. The partial index
    ``idx_tenant_invites_expiry_sweep`` is prepared for a future
    sweep (alembic 0035 line 213-215) but no consumer of it exists.

    Drift guard: if a future change adds the sweep AND wires it to
    the test environment, this test trips and forces a HANDOFF
    update.
    """
    tid = f"{_TENANT_PREFIX}-b4"
    iid = f"{_INVITE_PREFIX}-b4aaaaa1"
    try:
        await _y10r6_seed_tenant(pg_test_pool, tid)
        # Seed an already-expired pending invite.
        past_exp = _y10r6_iso(
            datetime.now(timezone.utc) - timedelta(hours=1),
        )
        await _y10r6_seed_invite(
            pg_test_pool, tid=tid, invite_id=iid,
            email="y10r6-b4-stale@example.com", role="member",
            expires_at=past_exp,
        )

        # Read the row as committed. There is no sweep, so the row
        # must STILL be 'pending' even though it is wall-clock-
        # expired. (If a future sweep flips it, this test trips.)
        status = await _y10r6_invite_status(pg_test_pool, iid)
        assert status == "pending", (
            f"Found a pre-flipped 'expired' status without doing an "
            f"accept call — Y10 row 6 documented the lack of a "
            f"sweep. If a sweep was wired, flip the HANDOFF entry "
            f"and remove this drift guard. Got status={status!r}."
        )
    finally:
        await _y10r6_purge(pg_test_pool, tid)


@pytest.mark.asyncio
@_requires_pg
async def test_pg_b5_unknown_invite_id_also_consumes_lockout_budget(
    client, pg_test_pool,
):
    """(B5 E2 enumeration defence) 10 attempts against an UNKNOWN
    invite_id (404 branch) drain the bucket and the 11th gets 429.

    Threat model: an attacker who does NOT know the invite_id can
    probe ``inv-XXXXXXXXXX`` patterns to enumerate live invites
    via timing differences (404 path is fast, 403 path runs the
    SELECT + token compare which is slower). The handler treats
    unknown-id as a failed attempt — see the rationale comment at
    ``backend/routers/tenant_invites.py:1388-1391``: "treat unknown
    id as a failed attempt for rate-limit purposes — otherwise an
    attacker could enumerate which inv-* prefixes are live by
    probing without cost".

    This test validates the rate-limit budget is consumed even on
    the 404 branch, so the enumeration cost is bounded.
    """
    tid = f"{_TENANT_PREFIX}-b5"
    bogus_iid = f"{_INVITE_PREFIX}-b5xxxxxx"
    try:
        # No invite seeded; just the tenant for symmetry.
        await _y10r6_seed_tenant(pg_test_pool, tid)
        await _y10r6_drain_limiter_bucket(bogus_iid)

        bad_token = "x" * 64
        # 10 consecutive 404s (unknown id branch) — each consumes a
        # token from the per-invite-id bucket.
        for attempt_idx in range(10):
            res = await client.post(
                f"/api/v1/invites/{bogus_iid}/accept",
                json={"token": bad_token},
            )
            assert res.status_code == 404, (
                f"attempt #{attempt_idx + 1} on unknown id must "
                f"return 404; got {res.status_code} body="
                f"{res.text!r}"
            )

        # 11th attempt: bucket exhausted → 429 even though the
        # underlying state is still "unknown id".
        res = await client.post(
            f"/api/v1/invites/{bogus_iid}/accept",
            json={"token": bad_token},
        )
        assert res.status_code == 429, (
            f"11th attempt on unknown id must trip the lockout; got "
            f"{res.status_code} body={res.text!r}. Y10 row 6 E2 "
            f"enumeration-defence invariant"
        )
        # Retry-After must be present and within bounds.
        retry_after_hdr = res.headers.get("Retry-After")
        assert retry_after_hdr is not None
        assert 1 <= int(retry_after_hdr) <= 60
    finally:
        await _y10r6_drain_limiter_bucket(bogus_iid)
        await _y10r6_purge(pg_test_pool, tid)

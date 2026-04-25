"""Y3 (#279) row 7 — drift-guard for the two invite rate-limits.

The TODO row literal:

    Rate limit: invite 一個 email 每 tenant 每小時不超過 5 次、
    accept 失敗每 token 每分鐘不超過 10 次（防爆破）。

Both limits already ship in ``backend/routers/tenant_invites.py`` —
``INVITE_RATE_LIMIT_*`` for ``POST /tenants/{tid}/invites`` (row 1)
and ``ACCEPT_FAIL_RATE_LIMIT_*`` for ``POST /invites/{id}/accept``
(row 4). This file is the **dedicated drift guard**: it consolidates
the literal-pinning + key-shape + behavioural sentinels so that a
future refactor (rename a constant, broaden a bucket key, lift the
cap to "be friendlier") trips CI before it ships.

Why this lives in its own file (rather than being satisfied by
``test_tenant_invites_create.py::test_rate_limit_constants_match_spec``
+ ``test_tenant_invites_accept.py::test_accept_fail_rate_limit_constants_match_spec``):

* Those two tests bind to the *creation* and *acceptance* row contexts
  respectively. TODO row 7 is its own row in the spec — security
  budget literals deserve a single, easy-to-find guard so an auditor
  reading "what enforces 5/email/tenant/hour?" can ``grep`` once and
  see the sentinel + behavioural proof side-by-side.
* The behavioural assertions here use the ``InMemoryLimiter``
  directly so they run **without PG** — the existing HTTP-layer 429
  tests in the row-1 / row-4 suites only fire when ``OMNI_TEST_PG_URL``
  is set, which means dev workstations and CI lanes that run pure-unit
  on every commit currently never exercise the budget exhaustion
  path. Adding it here closes that gap.

Module-global state audit (SOP Step 1)
──────────────────────────────────────
This file introduces *no* module-level state. Each test instantiates
its own ``InMemoryLimiter()`` (qualifying answer #1: every test gets
the same fresh state). The production rate-limiter selection
(Redis-vs-in-memory) lives in ``backend.rate_limit.get_limiter`` and
is already documented there — qualifying answers #2 (Redis prod) and
#3 (per-replica in-memory dev) on that module.

Read-after-write timing audit (SOP Step 1)
──────────────────────────────────────────
Pure logic tests; no DB writes, no async scheduling — the
``InMemoryLimiter`` is synchronous and single-threaded inside one
test process.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family A — Literal pinning to the TODO row 7 spec
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_invite_rate_limit_cap_is_5():
    """TODO row 7 literal: '5/email/tenant/hour'."""
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_RATE_LIMIT_CAP == 5, (
        "TODO row 7 says invite rate-limit must be exactly 5 calls "
        "per (tenant, email) per hour; raising or lowering the cap "
        "without updating the row is a contract drift"
    )


def test_invite_rate_limit_window_is_one_hour():
    """TODO row 7 literal: '5/email/tenant/hour'. 1 hour = 3600s."""
    from backend.routers import tenant_invites
    assert tenant_invites.INVITE_RATE_LIMIT_WINDOW_SECONDS == 3600.0


def test_accept_fail_rate_limit_cap_is_10():
    """TODO row 7 literal: 'accept 失敗每 token 每分鐘不超過 10 次'."""
    from backend.routers import tenant_invites
    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_CAP == 10, (
        "TODO row 7 says brute-force shield must allow at most 10 "
        "failed accept attempts per invite_id per minute"
    )


def test_accept_fail_rate_limit_window_is_one_minute():
    """TODO row 7 literal: '每分鐘'. 1 minute = 60s."""
    from backend.routers import tenant_invites
    assert tenant_invites.ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS == 60.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family B — Bucket-key shape sentinels
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# The literal "5 per email per tenant" only holds if the bucket key
# uniquely identifies the (tenant, email) pair. A future refactor
# that drops ``tenant_id`` (so the key becomes ``invite:{email}``)
# would silently broaden the bucket across all tenants — every
# tenant with the same recipient would share 5 calls/hour. Same for
# the accept side: dropping ``invite_id`` from the key would let an
# attacker brute-forcing one invite exhaust budget on unrelated ones.
# These tests freeze the source-of-truth key shapes by reading the
# router source.


def _read_router_source() -> str:
    from backend.routers import tenant_invites
    src_path = pathlib.Path(inspect.getsourcefile(tenant_invites))
    return src_path.read_text(encoding="utf-8")


def test_invite_rate_limit_key_is_per_tenant_per_normalised_email():
    """The bucket key MUST scope by both tenant_id AND the
    normalised email — otherwise the "5 per email per tenant" budget
    leaks across tenants or fails to lowercase A vs a."""
    src = _read_router_source()
    # Source must contain the canonical key construction. Pin both
    # the prefix and the two-part scoping.
    assert 'f"tenant_invite:{tenant_id}:{norm_email}"' in src, (
        "invite rate-limit key must be 'tenant_invite:{tid}:{norm_email}' "
        "to scope per-tenant + per-normalised-email; any drift "
        "regresses TODO row 7"
    )


def test_invite_rate_limit_key_uses_normalised_email():
    """``norm_email`` must come from ``_normalise_email(raw_email)``
    so 'Alice@x.com' and 'alice@x.com' share a bucket. Without the
    normalisation an attacker could just vary casing to dodge the
    cap."""
    src = _read_router_source()
    assert "norm_email = _normalise_email(raw_email)" in src, (
        "invite rate-limit must consume the normalised email — "
        "otherwise casing variants bypass the per-email budget"
    )


def test_accept_fail_rate_limit_key_is_per_invite_id():
    """The accept-fail bucket key MUST scope by invite_id alone —
    not per-IP (defeated by IP rotation), not global (one attacker
    starves the whole platform), not per-token (the attacker doesn't
    know the token, that's what they're brute-forcing)."""
    src = _read_router_source()
    assert 'f"invite_accept_fail:{invite_id}"' in src, (
        "accept-fail rate-limit key must be "
        "'invite_accept_fail:{invite_id}' so brute force on one "
        "invite cannot exhaust budget on unrelated invites"
    )


def test_normalise_email_lowercases_and_strips():
    """Underpins the per-email bucket: case-insensitive + whitespace-
    insensitive. Already covered by row-1 tests; re-asserted here so
    the rate-limit invariant is captured in one place."""
    from backend.routers.tenant_invites import _normalise_email
    assert _normalise_email("  Alice@Example.COM ") == "alice@example.com"
    assert _normalise_email("alice@example.com") == "alice@example.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family C — Behavioural proof via InMemoryLimiter (no PG required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Existing 429-flow tests in the row-1 / row-4 suites only fire when
# ``OMNI_TEST_PG_URL`` is set. These behavioural tests use the
# limiter directly so a dev workstation with no PG still verifies
# the budget exhaustion semantic.


def test_invite_budget_allows_5_then_refuses_6th():
    """5/email/tenant/hour: 5 successive ``allow`` calls return True;
    the 6th returns False with a positive ``retry_after`` (in seconds
    until the bucket has 1 token again)."""
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        INVITE_RATE_LIMIT_CAP,
        INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    lim = InMemoryLimiter()
    key = "tenant_invite:t-acme:alice@example.com"

    for i in range(INVITE_RATE_LIMIT_CAP):
        allowed, _wait = lim.allow(
            key, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
        )
        assert allowed is True, f"call #{i + 1} should be allowed"

    refused, retry_after = lim.allow(
        key, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert refused is False, "6th call must be refused"
    assert retry_after > 0.0, (
        "retry_after must be positive so the handler can populate "
        "Retry-After header in the 429 response"
    )
    # Refilling 1 token at rate 5/3600s takes 720s — sanity check
    # the wait is in the right ballpark (not zero, not absurd).
    assert retry_after < INVITE_RATE_LIMIT_WINDOW_SECONDS


def test_accept_fail_budget_allows_10_then_refuses_11th():
    """10/token/min: 10 successive ``allow`` calls return True;
    the 11th returns False with a positive ``retry_after``."""
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    lim = InMemoryLimiter()
    key = "invite_accept_fail:inv-deadbeef00"

    for i in range(ACCEPT_FAIL_RATE_LIMIT_CAP):
        allowed, _wait = lim.allow(
            key,
            ACCEPT_FAIL_RATE_LIMIT_CAP,
            ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
        )
        assert allowed is True, f"call #{i + 1} should be allowed"

    refused, retry_after = lim.allow(
        key,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert refused is False, "11th call must be refused"
    assert retry_after > 0.0
    assert retry_after < ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS


def test_invite_budget_is_independent_per_tenant():
    """5/email/tenant/hour: tenant A's exhausted bucket must NOT
    starve tenant B for the same recipient address. Without
    per-tenant scoping a malicious admin in one tenant could deny
    invites to the same recipient across the whole platform."""
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        INVITE_RATE_LIMIT_CAP,
        INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    lim = InMemoryLimiter()
    key_a = "tenant_invite:t-acme:alice@example.com"
    key_b = "tenant_invite:t-globex:alice@example.com"

    # Drain tenant A's bucket completely.
    for _ in range(INVITE_RATE_LIMIT_CAP):
        lim.allow(
            key_a, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
        )
    drained, _ = lim.allow(
        key_a, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert drained is False

    # Tenant B's first call to the same recipient must still pass.
    fresh, _ = lim.allow(
        key_b, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert fresh is True, (
        "tenant B's bucket must be independent — per-(tenant,email) "
        "scoping is the whole point of the key shape"
    )


def test_invite_budget_is_independent_per_email():
    """5/email/tenant/hour: alice's exhausted bucket must NOT starve
    bob's bucket on the same tenant. Without per-email scoping one
    spammed recipient denies invites to every other recipient."""
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        INVITE_RATE_LIMIT_CAP,
        INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    lim = InMemoryLimiter()
    key_alice = "tenant_invite:t-acme:alice@example.com"
    key_bob = "tenant_invite:t-acme:bob@example.com"

    for _ in range(INVITE_RATE_LIMIT_CAP):
        lim.allow(
            key_alice, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
        )
    drained, _ = lim.allow(
        key_alice, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert drained is False

    fresh, _ = lim.allow(
        key_bob, INVITE_RATE_LIMIT_CAP, INVITE_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert fresh is True


def test_accept_fail_budget_is_independent_per_invite_id():
    """10/token/min: brute-forcing invite A must NOT exhaust the
    budget on invite B. Without per-invite_id scoping one attacker
    starves the whole platform's accept flow."""
    from backend.rate_limit import InMemoryLimiter
    from backend.routers.tenant_invites import (
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    lim = InMemoryLimiter()
    key_a = "invite_accept_fail:inv-aaaaaaaa00"
    key_b = "invite_accept_fail:inv-bbbbbbbb00"

    for _ in range(ACCEPT_FAIL_RATE_LIMIT_CAP):
        lim.allow(
            key_a,
            ACCEPT_FAIL_RATE_LIMIT_CAP,
            ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
        )
    drained, _ = lim.allow(
        key_a,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert drained is False

    fresh, _ = lim.allow(
        key_b,
        ACCEPT_FAIL_RATE_LIMIT_CAP,
        ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS,
    )
    assert fresh is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family D — Module-global state audit (SOP Step 1 qualifying #1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("attr", [
    "INVITE_RATE_LIMIT_CAP",
    "INVITE_RATE_LIMIT_WINDOW_SECONDS",
    "ACCEPT_FAIL_RATE_LIMIT_CAP",
    "ACCEPT_FAIL_RATE_LIMIT_WINDOW_SECONDS",
])
def test_rate_limit_constants_are_module_level_immutable(attr):
    """SOP Step 1 qualifying answer #1: every uvicorn worker derives
    the same value from the same module source. The four budget
    knobs are bare numeric literals at module scope — re-importing
    the module returns the same value, no first-boot drift."""
    from backend.routers import tenant_invites
    val = getattr(tenant_invites, attr)
    # Numeric literal — int or float depending on knob.
    assert isinstance(val, (int, float)) and not isinstance(val, bool), (
        f"{attr} must be a bare numeric literal at module scope"
    )
    # Re-import returns the same identity (or same value for ints/
    # floats interned by CPython) — proves the constant isn't computed
    # from per-process state.
    import importlib
    reloaded = importlib.reload(tenant_invites)
    assert getattr(reloaded, attr) == val


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family E — Pre-commit fingerprint guard on this test file
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_self_fingerprint_clean():
    """SOP Step 3 fingerprint guard on the production router file.
    Catches accidental ``_conn()`` / ``await conn.commit()`` /
    ``datetime('now')`` / ``VALUES (?, ?)`` regressions on the prod
    surface this row's contract relies on. Mirrors the pattern used
    by the row-2/3/4 self-fingerprint guards."""
    fingerprint_re = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|"
        r"VALUES.*\?[,)]"
    )
    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "backend/routers/tenant_invites.py"
    ).read_text(encoding="utf-8")
    hits = [
        (i, line) for i, line in enumerate(src.splitlines(), start=1)
        if fingerprint_re.search(line)
    ]
    assert hits == [], f"compat-era fingerprint(s) hit: {hits}"

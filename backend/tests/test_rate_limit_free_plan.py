"""SP-8.1 / task #81 — free-plan per-IP rate-limit tuning.

Operator-reported regression: single-user dashboard cold load
produced HTTP 429 cascades. The dashboard's initial render issues
a burst of API calls (tenant metrics + recent activity + SSE
handshake + asset prefetch) that comfortably exceeds 60 req/min
from one IP. The free-tier per-IP budget was therefore the sole
bottleneck for first-time-user friction.

This test pins the post-tuning numbers so the next person who
edits ``backend.quota.PLAN_QUOTAS`` has to consciously revisit
the fix. It also confirms the other tiers scaled to keep the
plan hierarchy strictly monotonic (the original
``test_quota.test_plan_hierarchy`` invariant would otherwise
break if only free were bumped).
"""

from __future__ import annotations

from backend.quota import PLAN_QUOTAS, quota_for_plan


def test_free_per_ip_budget_matches_dashboard_burst():
    """The free tier's per-IP budget must accommodate a realistic
    single-user dashboard cold load PLUS 2-3 parallel tabs. 1200
    req/min (20/sec avg) is the operator-approved floor, bumped
    from the initial 300/60s after SP-8.1b (2026-04-21) when it
    became clear CF Free's Rate Limiting (10 req/10s only) can't
    usefully complement a lower backend cap — the backend is the
    only per-IP gate, so it needs realistic headroom. See
    ``docs/phase-3-runtime-v2/02-sub-phases.md`` Epic 8 SP-8.1."""
    free = quota_for_plan("free")
    assert free.per_ip.capacity == 1200
    assert free.per_ip.window_seconds == 60.0


def test_free_per_user_budget_matches_dashboard_burst():
    """SP-8.1c (2026-04-21): per_user was 120 — tripped by
    ``useEngine``'s 11-endpoint × 5s = 132 req/min single-tab
    burst. Bumped to match per_ip (1200/min) so a single operator
    running 3 tabs still has headroom. See operator screenshot
    2026-04-21 + ``backend/quota.py`` docstring for the full root
    cause."""
    free = quota_for_plan("free")
    assert free.per_user.capacity == 1200


def test_free_per_tenant_budget_matches_multi_user_envelope():
    """SP-8.1c (2026-04-21): per_tenant raised to 1500 — just above
    per_user (1200) so any single user can't individually saturate
    the tenant ceiling, but multi-user teams get a reasonable shared
    cap."""
    free = quota_for_plan("free")
    assert free.per_tenant.capacity == 1500


def test_plan_hierarchy_preserved_after_tuning():
    """SP-8.1 bumped every tier's per-IP capacity by 5x so the
    hierarchy ``free < starter < pro < enterprise`` stays strictly
    monotonic (the invariant ``test_quota.test_plan_hierarchy``
    asserts)."""
    free = quota_for_plan("free")
    starter = quota_for_plan("starter")
    pro = quota_for_plan("pro")
    enterprise = quota_for_plan("enterprise")
    assert free.per_ip.capacity < starter.per_ip.capacity
    assert starter.per_ip.capacity < pro.per_ip.capacity
    assert pro.per_ip.capacity < enterprise.per_ip.capacity


def test_all_tiers_per_ip_matches_spec():
    """Post-SP-8.1b (2026-04-21) numbers — locked in so a future
    tweak that changes free without touching the others (and breaks
    the hierarchy) fails a test instead of a dashboard. Scaled 4x
    from the initial SP-8.1 set; see the module docstring in
    ``backend/quota.py`` for the CF-Free-Rate-Limiting rationale."""
    assert PLAN_QUOTAS["free"].per_ip.capacity == 1200
    assert PLAN_QUOTAS["starter"].per_ip.capacity == 2400
    assert PLAN_QUOTAS["pro"].per_ip.capacity == 6000
    assert PLAN_QUOTAS["enterprise"].per_ip.capacity == 12000

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
    single-user dashboard cold load. 300 req/min is the operator-
    approved floor (see ``docs/phase-3-runtime-v2/02-sub-phases.md``
    Epic 8 SP-8.1)."""
    free = quota_for_plan("free")
    assert free.per_ip.capacity == 300
    assert free.per_ip.window_seconds == 60.0


def test_free_per_user_budget_unchanged():
    """The per-IP tuning deliberately left per_user alone. Raising
    per_user is a separate discussion (touches abuse-resistance on
    shared IPs) and is out of scope for SP-8.1."""
    free = quota_for_plan("free")
    assert free.per_user.capacity == 120


def test_free_per_tenant_budget_unchanged():
    free = quota_for_plan("free")
    assert free.per_tenant.capacity == 300


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
    """Post-SP-8.1 numbers — locked in so a future tweak that
    changes free without touching the others (and breaks the
    hierarchy) fails a test instead of a dashboard."""
    assert PLAN_QUOTAS["free"].per_ip.capacity == 300
    assert PLAN_QUOTAS["starter"].per_ip.capacity == 600
    assert PLAN_QUOTAS["pro"].per_ip.capacity == 1500
    assert PLAN_QUOTAS["enterprise"].per_ip.capacity == 3000

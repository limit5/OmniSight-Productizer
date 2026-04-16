"""I9 — Quota config unit tests."""

from __future__ import annotations

from backend.quota import PLAN_QUOTAS, PlanQuota, quota_for_plan


def test_all_plans_defined():
    for plan in ("free", "starter", "pro", "enterprise"):
        q = quota_for_plan(plan)
        assert isinstance(q, PlanQuota)
        assert q.per_ip.capacity > 0
        assert q.per_user.capacity > 0
        assert q.per_tenant.capacity > 0


def test_plan_hierarchy():
    free = quota_for_plan("free")
    starter = quota_for_plan("starter")
    pro = quota_for_plan("pro")
    enterprise = quota_for_plan("enterprise")

    assert free.per_ip.capacity < starter.per_ip.capacity
    assert starter.per_ip.capacity < pro.per_ip.capacity
    assert pro.per_ip.capacity < enterprise.per_ip.capacity

    assert free.per_tenant.capacity < enterprise.per_tenant.capacity


def test_unknown_plan_defaults_to_free():
    q = quota_for_plan("nonexistent")
    free = quota_for_plan("free")
    assert q == free


def test_per_tenant_greater_than_per_user():
    for plan in PLAN_QUOTAS:
        q = quota_for_plan(plan)
        assert q.per_tenant.capacity >= q.per_user.capacity, (
            f"Plan {plan}: per_tenant should be >= per_user"
        )

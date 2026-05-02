"""FS.1.6 — DB provisioning PEP HOLD cost estimate policy tests."""

from __future__ import annotations

import pytest

from backend.db_provisioning import (
    DBProvisionPepHoldUnsupportedTierError,
    pep_hold_supported_tiers,
    plan_pep_hold,
)


class TestDBProvisionPepHoldPolicy:

    @pytest.mark.parametrize(
        "provider,tier,expected",
        [
            ("supabase", "free", ("supabase", "free", 0.0, 0.0)),
            ("neon", "launch", ("neon", "launch", 0.0, None)),
            (
                "planet-scale",
                "pro",
                ("planetscale", "scaler-pro", 5.0, None),
            ),
            (
                "planetscale",
                "enterprise single tenant",
                ("planetscale", "enterprise-single-tenant", None, None),
            ),
        ],
    )
    def test_plan_pep_hold_by_provider_tier(self, provider, tier, expected):
        normalized_provider, normalized_tier, low, high = expected
        policy = plan_pep_hold(provider, tier)
        assert policy.provider == normalized_provider
        assert policy.provider_tier == normalized_tier
        assert policy.required is True
        assert policy.pep_tool == "db_provision"
        assert policy.pep_tier == "t2"
        assert policy.impact_scope == "provider-recurring-spend"
        assert policy.cost_estimate.monthly_low_usd == low
        assert policy.cost_estimate.monthly_high_usd == high

    def test_policy_to_dict_is_public_result_shape(self):
        policy = plan_pep_hold("neon", "scale")
        assert policy.to_dict() == {
            "provider": "neon",
            "provider_tier": "scale",
            "required": True,
            "pep_tool": "db_provision",
            "pep_tier": "t2",
            "impact_scope": "provider-recurring-spend",
            "reason": policy.reason,
            "cost_estimate": {
                "currency": "USD",
                "monthly_low_usd": 0.0,
                "monthly_high_usd": None,
                "estimate_basis": (
                    "$0.222/CU-hour plus $0.35/GB-month database storage"
                ),
                "variable_components": [
                    "cu_hours",
                    "database_storage",
                    "history_storage",
                    "extra_branches",
                ],
            },
        }

    def test_reason_carries_checked_provider_source(self):
        assert "supabase.com/docs" in plan_pep_hold("supabase", "free").reason
        assert "neon.com/pricing" in plan_pep_hold("neon", "launch").reason
        assert "planetscale.com/docs" in plan_pep_hold("planetscale", "pro").reason

    def test_unknown_provider_tier_is_rejected(self):
        with pytest.raises(DBProvisionPepHoldUnsupportedTierError) as excinfo:
            plan_pep_hold("neon", "hobby")
        assert excinfo.value.provider == "neon"
        assert excinfo.value.tier == "hobby"

    def test_supported_tiers_are_normalized_and_sorted(self):
        assert pep_hold_supported_tiers("supabase") == [
            "enterprise",
            "free",
            "pro",
            "team",
        ]

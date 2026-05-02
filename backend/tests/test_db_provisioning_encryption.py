"""FS.1.4 — DB provisioning encryption-at-rest policy tests."""

from __future__ import annotations

import pytest

from backend.db_provisioning import (
    EncryptionAtRestUnsupportedTierError,
    encryption_supported_tiers,
    normalize_provider_tier,
    plan_encryption_at_rest,
)


class TestEncryptionAtRestPolicy:

    @pytest.mark.parametrize(
        "provider,tier,normalized",
        [
            ("supabase", "free", "free"),
            ("supabase", "team", "team"),
            ("neon", "launch", "launch"),
            ("neon", "business", "business"),
            ("planetscale", "pro", "scaler-pro"),
            ("planet-scale", "enterprise", "enterprise-multi-tenant"),
            ("PLANETSCALE", "enterprise single tenant", "enterprise-single-tenant"),
        ],
    )
    def test_normalize_provider_tier(self, provider, tier, normalized):
        assert normalize_provider_tier(provider, tier) == normalized

    @pytest.mark.parametrize("provider", ["supabase", "neon", "planetscale"])
    def test_plan_enables_provider_managed_encryption(self, provider):
        policy = plan_encryption_at_rest(provider)
        assert policy.provider == provider
        assert policy.enabled is True
        assert policy.auto_enabled is True
        assert policy.mode == "provider-managed"
        assert policy.action == "default-on"
        assert "encrypt" in policy.reason.lower()

    def test_policy_to_dict_is_public_result_shape(self):
        policy = plan_encryption_at_rest("supabase", "team")
        assert policy.to_dict() == {
            "provider": "supabase",
            "provider_tier": "team",
            "enabled": True,
            "auto_enabled": True,
            "mode": "provider-managed",
            "action": "default-on",
            "reason": policy.reason,
        }

    def test_unknown_provider_tier_is_rejected(self):
        with pytest.raises(EncryptionAtRestUnsupportedTierError) as excinfo:
            plan_encryption_at_rest("supabase", "hobby")
        assert excinfo.value.provider == "supabase"
        assert excinfo.value.tier == "hobby"

    def test_supported_tiers_are_normalized_and_sorted(self):
        assert encryption_supported_tiers("planetscale") == [
            "enterprise-multi-tenant",
            "enterprise-single-tenant",
            "managed",
            "scaler-pro",
        ]

"""FS.1.5 — DB provisioning backup schedule policy tests."""

from __future__ import annotations

import pytest

from backend.db_provisioning import (
    BackupScheduleUnsupportedTierError,
    backup_supported_tiers,
    plan_backup_schedule,
)


class TestBackupSchedulePolicy:

    @pytest.mark.parametrize(
        "provider,tier,expected",
        [
            ("supabase", "free", ("operator-managed", False, "manual-offsite")),
            ("supabase", "team", ("provider-managed", True, "daily")),
            ("neon", "business", ("provider-managed-pitr", True, "continuous-wal-retention")),
            ("planetscale", "pro", ("provider-managed", True, "twice-daily")),
            (
                "planet-scale",
                "enterprise single tenant",
                ("provider-managed", True, "twice-daily"),
            ),
        ],
    )
    def test_plan_backup_schedule_by_provider_feature(self, provider, tier, expected):
        mode, enabled, schedule = expected
        policy = plan_backup_schedule(provider, tier)
        assert policy.mode == mode
        assert policy.enabled is enabled
        assert policy.auto_scheduled is enabled
        assert policy.schedule == schedule

    def test_policy_to_dict_is_public_result_shape(self):
        policy = plan_backup_schedule("supabase", "team")
        assert policy.to_dict() == {
            "provider": "supabase",
            "provider_tier": "team",
            "enabled": True,
            "auto_scheduled": True,
            "mode": "provider-managed",
            "schedule": "daily",
            "retention": "provider-tier-default",
            "action": "default-on-paid-tier",
            "reason": policy.reason,
        }

    def test_unknown_provider_tier_is_rejected(self):
        with pytest.raises(BackupScheduleUnsupportedTierError) as excinfo:
            plan_backup_schedule("neon", "hobby")
        assert excinfo.value.provider == "neon"
        assert excinfo.value.tier == "hobby"

    def test_supported_tiers_are_normalized_and_sorted(self):
        assert backup_supported_tiers("supabase") == [
            "enterprise",
            "free",
            "pro",
            "team",
        ]

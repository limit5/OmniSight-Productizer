"""FS.1.5 — Provider-feature backup schedule policy for DB provisioning.

Provider management APIs expose different backup primitives for
Supabase / Neon / PlanetScale. The automation records the provider
feature decision in the provisioning result while leaving provider-side
backup execution on each provider's managed control plane.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
This module defines immutable policy constants only. No module-level
cache, singleton, or mutable registry is read or written, so uvicorn
workers independently derive the same provider-feature backup policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from backend.db_provisioning.encryption import normalize_provider_tier


@dataclass(frozen=True)
class BackupSchedulePolicy:
    """Resolved provider-feature backup schedule decision."""

    provider: str
    provider_tier: str
    enabled: bool
    auto_scheduled: bool
    mode: str
    schedule: str
    retention: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "provider_tier": self.provider_tier,
            "enabled": self.enabled,
            "auto_scheduled": self.auto_scheduled,
            "mode": self.mode,
            "schedule": self.schedule,
            "retention": self.retention,
            "action": self.action,
            "reason": self.reason,
        }


class BackupScheduleUnsupportedTierError(ValueError):
    """Provider tier is not known to support FS.1.5 backup scheduling."""

    def __init__(self, provider: str, tier: str):
        super().__init__(
            f"Unsupported backup schedule tier '{tier}' for provider '{provider}'"
        )
        self.provider = provider
        self.tier = tier


_SUPABASE_POLICIES: Mapping[str, BackupSchedulePolicy] = MappingProxyType({
    "free": BackupSchedulePolicy(
        provider="supabase",
        provider_tier="free",
        enabled=False,
        auto_scheduled=False,
        mode="operator-managed",
        schedule="manual-offsite",
        retention="operator-defined",
        action="manual-export-required",
        reason=(
            "Supabase free tier does not expose a managed scheduled backup "
            "feature; operators must run off-site exports."
        ),
    ),
    "pro": BackupSchedulePolicy(
        provider="supabase",
        provider_tier="pro",
        enabled=True,
        auto_scheduled=True,
        mode="provider-managed",
        schedule="daily",
        retention="provider-tier-default",
        action="default-on-paid-tier",
        reason=(
            "Supabase paid projects use provider-managed scheduled backups; "
            "PITR remains a separate provider feature for tighter RPO."
        ),
    ),
    "team": BackupSchedulePolicy(
        provider="supabase",
        provider_tier="team",
        enabled=True,
        auto_scheduled=True,
        mode="provider-managed",
        schedule="daily",
        retention="provider-tier-default",
        action="default-on-paid-tier",
        reason=(
            "Supabase paid projects use provider-managed scheduled backups; "
            "PITR remains a separate provider feature for tighter RPO."
        ),
    ),
    "enterprise": BackupSchedulePolicy(
        provider="supabase",
        provider_tier="enterprise",
        enabled=True,
        auto_scheduled=True,
        mode="provider-managed",
        schedule="daily",
        retention="provider-tier-default",
        action="default-on-paid-tier",
        reason=(
            "Supabase paid projects use provider-managed scheduled backups; "
            "PITR remains a separate provider feature for tighter RPO."
        ),
    ),
})

_NEON_POLICIES: Mapping[str, BackupSchedulePolicy] = MappingProxyType({
    tier: BackupSchedulePolicy(
        provider="neon",
        provider_tier=tier,
        enabled=True,
        auto_scheduled=True,
        mode="provider-managed-pitr",
        schedule="continuous-wal-retention",
        retention="provider-tier-restore-window",
        action="use-restore-window",
        reason=(
            "Neon backup recovery is exposed through point-in-time restore "
            "over the branch restore window rather than a user-created "
            "scheduled snapshot job."
        ),
    )
    for tier in ("free", "launch", "scale", "business", "enterprise")
})

_PLANETSCALE_POLICIES: Mapping[str, BackupSchedulePolicy] = MappingProxyType({
    tier: BackupSchedulePolicy(
        provider="planetscale",
        provider_tier=tier,
        enabled=True,
        auto_scheduled=True,
        mode="provider-managed",
        schedule="twice-daily",
        retention="provider-tier-default",
        action="default-scheduled-backups",
        reason=(
            "PlanetScale provider-managed branches include scheduled backups; "
            "additional schedules remain a provider-side branch setting."
        ),
    )
    for tier in (
        "scaler-pro",
        "enterprise-multi-tenant",
        "enterprise-single-tenant",
        "managed",
    )
})

_POLICIES: Mapping[str, Mapping[str, BackupSchedulePolicy]] = MappingProxyType({
    "supabase": _SUPABASE_POLICIES,
    "neon": _NEON_POLICIES,
    "planetscale": _PLANETSCALE_POLICIES,
})


def _normalize_provider(provider: str) -> str:
    key = provider.strip().lower().replace("_", "-")
    if key == "planet-scale":
        key = "planetscale"
    return key


def plan_backup_schedule(
    provider: str,
    tier: str | None = None,
) -> BackupSchedulePolicy:
    """Return the provider-feature backup schedule decision for a tier."""
    key = _normalize_provider(provider)
    try:
        normalized_tier = normalize_provider_tier(key, tier)
    except ValueError as exc:
        raise BackupScheduleUnsupportedTierError(key, tier or "") from exc
    policies = _POLICIES.get(key)
    if policies is None or normalized_tier not in policies:
        raise BackupScheduleUnsupportedTierError(key, tier or "")
    return policies[normalized_tier]


def backup_supported_tiers(provider: str) -> list[str]:
    """Return normalized tier ids covered by the FS.1.5 policy."""
    key = _normalize_provider(provider)
    policies = _POLICIES.get(key)
    if policies is None:
        raise BackupScheduleUnsupportedTierError(provider, "")
    return sorted(policies)


__all__ = [
    "BackupSchedulePolicy",
    "BackupScheduleUnsupportedTierError",
    "backup_supported_tiers",
    "plan_backup_schedule",
]

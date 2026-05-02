"""FS.1.4 — Provider-tier encryption-at-rest policy for DB provisioning.

Provider management APIs do not expose a portable encryption toggle for
Supabase / Neon / PlanetScale. The automation therefore records the
provider-tier decision and relies on the provider-managed default where
that tier supports at-rest encryption.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
This module defines immutable policy constants only. No module-level
cache, singleton, or mutable registry is read or written, so uvicorn
workers independently derive the same provider-tier policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class EncryptionAtRestPolicy:
    """Resolved provider-tier encryption-at-rest decision."""

    provider: str
    provider_tier: str
    enabled: bool
    auto_enabled: bool
    mode: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "provider_tier": self.provider_tier,
            "enabled": self.enabled,
            "auto_enabled": self.auto_enabled,
            "mode": self.mode,
            "action": self.action,
            "reason": self.reason,
        }


_TIER_ALIASES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "supabase": MappingProxyType({
        "": "free",
        "default": "free",
        "free": "free",
        "pro": "pro",
        "team": "team",
        "enterprise": "enterprise",
    }),
    "neon": MappingProxyType({
        "": "free",
        "default": "free",
        "free": "free",
        "launch": "launch",
        "scale": "scale",
        "business": "business",
        "enterprise": "enterprise",
    }),
    "planetscale": MappingProxyType({
        "": "scaler-pro",
        "default": "scaler-pro",
        "scaler-pro": "scaler-pro",
        "scaler pro": "scaler-pro",
        "pro": "scaler-pro",
        "enterprise": "enterprise-multi-tenant",
        "enterprise-multi-tenant": "enterprise-multi-tenant",
        "enterprise multi tenant": "enterprise-multi-tenant",
        "enterprise-single-tenant": "enterprise-single-tenant",
        "enterprise single tenant": "enterprise-single-tenant",
        "managed": "managed",
        "planetscale-managed": "managed",
    }),
})

_REASONS: Mapping[str, str] = MappingProxyType({
    "supabase": (
        "Supabase projects are encrypted at rest by default; no Management "
        "API flag is required or exposed during project creation."
    ),
    "neon": (
        "Neon storage is provider-managed and encrypted at rest; the create "
        "project API does not expose a separate encryption toggle."
    ),
    "planetscale": (
        "PlanetScale databases and backups are provider-managed with at-rest "
        "encryption; the create database API does not expose a separate toggle."
    ),
})


class EncryptionAtRestUnsupportedTierError(ValueError):
    """Provider tier is not known to support FS.1.4 automatic enablement."""

    def __init__(self, provider: str, tier: str):
        super().__init__(
            f"Unsupported encryption-at-rest tier '{tier}' for provider '{provider}'"
        )
        self.provider = provider
        self.tier = tier


def normalize_provider_tier(provider: str, tier: str | None = None) -> str:
    """Normalize a provider-specific commercial tier name."""
    key = provider.strip().lower().replace("_", "-")
    if key == "planet-scale":
        key = "planetscale"
    raw = (tier or "").strip().lower().replace("_", "-")
    aliases = _TIER_ALIASES.get(key)
    if aliases is None:
        raise EncryptionAtRestUnsupportedTierError(provider, tier or "")
    normalized = aliases.get(raw)
    if normalized is None:
        raise EncryptionAtRestUnsupportedTierError(key, tier or "")
    return normalized


def plan_encryption_at_rest(
    provider: str,
    tier: str | None = None,
) -> EncryptionAtRestPolicy:
    """Return the automatic encryption-at-rest decision for a provider tier."""
    key = provider.strip().lower().replace("_", "-")
    if key == "planet-scale":
        key = "planetscale"
    normalized_tier = normalize_provider_tier(key, tier)
    return EncryptionAtRestPolicy(
        provider=key,
        provider_tier=normalized_tier,
        enabled=True,
        auto_enabled=True,
        mode="provider-managed",
        action="default-on",
        reason=_REASONS[key],
    )


def encryption_supported_tiers(provider: str) -> list[str]:
    """Return normalized tier ids supported by the FS.1.4 policy."""
    key = provider.strip().lower().replace("_", "-")
    if key == "planet-scale":
        key = "planetscale"
    aliases = _TIER_ALIASES.get(key)
    if aliases is None:
        raise EncryptionAtRestUnsupportedTierError(provider, "")
    return sorted(set(aliases.values()))


__all__ = [
    "EncryptionAtRestPolicy",
    "EncryptionAtRestUnsupportedTierError",
    "encryption_supported_tiers",
    "normalize_provider_tier",
    "plan_encryption_at_rest",
]

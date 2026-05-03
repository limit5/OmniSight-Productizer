"""FS.1.6 — PEP HOLD policy with DB provisioning cost estimate.

Tenant-owned database provisioning creates durable provider-side
resources and can create recurring cloud spend. The automation records a
PEP HOLD decision envelope and a conservative monthly cost estimate so
the operator can approve the spend before the provider API call is used
by higher-level orchestration.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
This module defines immutable policy constants only. No module-level
cache, singleton, or mutable registry is read or written, so uvicorn
workers independently derive the same provider-tier PEP HOLD policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from backend.db_provisioning.encryption import normalize_provider_tier


@dataclass(frozen=True)
class DBProvisionCostEstimate:
    """Operator-facing recurring cost estimate for one provisioned DB."""

    currency: str
    monthly_low_usd: float | None
    monthly_high_usd: float | None
    estimate_basis: str
    variable_components: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "monthly_low_usd": self.monthly_low_usd,
            "monthly_high_usd": self.monthly_high_usd,
            "estimate_basis": self.estimate_basis,
            "variable_components": list(self.variable_components),
        }


@dataclass(frozen=True)
class DBProvisionPepHoldPolicy:
    """PEP HOLD metadata attached to DB provisioning results."""

    provider: str
    provider_tier: str
    required: bool
    pep_tool: str
    pep_tier: str
    impact_scope: str
    reason: str
    cost_estimate: DBProvisionCostEstimate

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "provider_tier": self.provider_tier,
            "required": self.required,
            "pep_tool": self.pep_tool,
            "pep_tier": self.pep_tier,
            "impact_scope": self.impact_scope,
            "reason": self.reason,
            "cost_estimate": self.cost_estimate.to_dict(),
        }


class DBProvisionPepHoldUnsupportedTierError(ValueError):
    """Provider tier is not known to support FS.1.6 PEP HOLD metadata."""

    def __init__(self, provider: str, tier: str):
        super().__init__(
            f"Unsupported PEP HOLD tier '{tier}' for provider '{provider}'"
        )
        self.provider = provider
        self.tier = tier


_SOURCE_NOTES: Mapping[str, str] = MappingProxyType({
    "supabase": (
        "Supabase billing is organization-based; each project adds compute "
        "cost, while database size, egress, auth, storage, and other usage "
        "can add variable fees. Source checked 2026-05-03: "
        "https://supabase.com/docs/guides/platform/billing-on-supabase"
    ),
    "neon": (
        "Neon bills usage-based compute and storage; paid plans have no "
        "monthly minimum, and costs vary with CU-hours, GB-months, branches, "
        "and restore-window history. Source checked 2026-05-03: "
        "https://neon.com/pricing"
    ),
    "planetscale": (
        "PlanetScale Base/Vitess clusters vary by cluster size, region, "
        "storage, branch hours, replicas, VTGates, and read-only regions; "
        "enterprise and managed plans are custom. Source checked 2026-05-03: "
        "https://planetscale.com/docs/plans/scaler-pro-cluster-pricing"
    ),
})


_COST_ESTIMATES: Mapping[str, Mapping[str, DBProvisionCostEstimate]] = MappingProxyType({
    "supabase": MappingProxyType({
        "free": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=0.0,
            monthly_high_usd=0.0,
            estimate_basis="free organization project within included quota",
            variable_components=(
                "project_count_limit",
                "compute_upgrade",
                "database_size",
                "egress",
                "storage",
            ),
        ),
        "pro": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="paid organization plus per-project compute and usage",
            variable_components=(
                "organization_subscription",
                "project_compute",
                "database_size",
                "egress",
                "storage",
            ),
        ),
        "team": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="team organization plus per-project compute and usage",
            variable_components=(
                "organization_subscription",
                "project_compute",
                "database_size",
                "egress",
                "storage",
            ),
        ),
        "enterprise": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="custom enterprise contract",
            variable_components=("custom_contract", "usage_overages"),
        ),
    }),
    "neon": MappingProxyType({
        "free": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=0.0,
            monthly_high_usd=0.0,
            estimate_basis="free project within included CU-hour and storage quota",
            variable_components=("quota_exhaustion", "plan_upgrade"),
        ),
        "launch": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=0.0,
            monthly_high_usd=None,
            estimate_basis="$0.106/CU-hour plus $0.35/GB-month database storage",
            variable_components=(
                "cu_hours",
                "database_storage",
                "history_storage",
                "extra_branches",
            ),
        ),
        "scale": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=0.0,
            monthly_high_usd=None,
            estimate_basis="$0.222/CU-hour plus $0.35/GB-month database storage",
            variable_components=(
                "cu_hours",
                "database_storage",
                "history_storage",
                "extra_branches",
            ),
        ),
        "business": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="business contract or current billing-plan terms",
            variable_components=("contract_terms", "usage_overages"),
        ),
        "enterprise": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="custom enterprise contract",
            variable_components=("custom_contract", "usage_overages"),
        ),
    }),
    "planetscale": MappingProxyType({
        "scaler-pro": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=5.0,
            monthly_high_usd=None,
            estimate_basis=(
                "Base plan floor: single-node Postgres starts at $5/month; "
                "HA Vitess clusters depend on selected cluster and region"
            ),
            variable_components=(
                "cluster_size",
                "region",
                "storage",
                "branch_hours",
                "replicas",
                "vtgates",
            ),
        ),
        "enterprise-multi-tenant": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="custom enterprise single-tenant contract",
            variable_components=("custom_contract", "cloud_account_terms"),
        ),
        "enterprise-single-tenant": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="custom enterprise single-tenant contract",
            variable_components=("custom_contract", "cloud_account_terms"),
        ),
        "managed": DBProvisionCostEstimate(
            currency="USD",
            monthly_low_usd=None,
            monthly_high_usd=None,
            estimate_basis="PlanetScale Managed plus customer cloud account costs",
            variable_components=("custom_contract", "aws_or_gcp_account_cost"),
        ),
    }),
})


def _normalize_provider(provider: str) -> str:
    key = provider.strip().lower().replace("_", "-")
    if key == "planet-scale":
        key = "planetscale"
    return key


def plan_pep_hold(
    provider: str,
    tier: str | None = None,
) -> DBProvisionPepHoldPolicy:
    """Return the required PEP HOLD envelope for a provider tier."""
    key = _normalize_provider(provider)
    try:
        normalized_tier = normalize_provider_tier(key, tier)
    except ValueError as exc:
        raise DBProvisionPepHoldUnsupportedTierError(key, tier or "") from exc
    estimates = _COST_ESTIMATES.get(key)
    if estimates is None or normalized_tier not in estimates:
        raise DBProvisionPepHoldUnsupportedTierError(key, tier or "")
    return DBProvisionPepHoldPolicy(
        provider=key,
        provider_tier=normalized_tier,
        required=True,
        pep_tool="db_provision",
        pep_tier="t2",
        impact_scope="provider-recurring-spend",
        reason=(
            "Tenant DB provisioning creates provider-side resources and may "
            f"incur recurring spend. {_SOURCE_NOTES[key]}"
        ),
        cost_estimate=estimates[normalized_tier],
    )


def pep_hold_supported_tiers(provider: str) -> list[str]:
    """Return normalized tier ids covered by the FS.1.6 policy."""
    key = _normalize_provider(provider)
    estimates = _COST_ESTIMATES.get(key)
    if estimates is None:
        raise DBProvisionPepHoldUnsupportedTierError(provider, "")
    return sorted(estimates)


__all__ = [
    "DBProvisionCostEstimate",
    "DBProvisionPepHoldPolicy",
    "DBProvisionPepHoldUnsupportedTierError",
    "pep_hold_supported_tiers",
    "plan_pep_hold",
]

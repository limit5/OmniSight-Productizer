"""KS.2.8 / KS.2.9 -- CMEK tier transition rewrap helpers.

This module owns the stateless upgrade primitive used before KS.2.11's
durable ``cmek_configs`` / ``tier_assignments`` tables land. Callers
pass the tenant's current ``TenantDEKRef`` rows, and the helper returns
replacement refs whose per-tenant DEK ids and encryption contexts are
unchanged while the upper wrap moves between the Tier 1 master KEK and
the customer CMK adapter.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable status strings live at module scope. Upgrade progress is
computed from request input and returned in the response; no in-memory
job cache or singleton is introduced, so multi-worker correctness does
not depend on shared Python memory.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
This helper does not write PG / Redis / filesystem state. KS.2.11 will
own atomic persistence of the returned replacement DEK refs and tier
assignment; this row only builds the rewrap plan and progress payload.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from backend.security import cmek_wizard
from backend.security import envelope
from backend.security import kms_adapters as kms


UPGRADE_STATUS_COMPLETED = "completed"
UPGRADE_STATUS_FAILED = "failed"
DOWNGRADE_STATUS_COMPLETED = UPGRADE_STATUS_COMPLETED
DOWNGRADE_STATUS_FAILED = UPGRADE_STATUS_FAILED


@dataclass(frozen=True)
class CMEKUpgradeItemResult:
    dek_id: str
    status: str
    source_provider: str
    target_provider: str
    replacement_dek_ref: dict[str, Any] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dek_id": self.dek_id,
            "status": self.status,
            "source_provider": self.source_provider,
            "target_provider": self.target_provider,
            "replacement_dek_ref": self.replacement_dek_ref,
            "error": self.error,
        }


@dataclass(frozen=True)
class CMEKUpgradePlanResult:
    upgrade_id: str
    tenant_id: str
    from_security_tier: str
    to_security_tier: str
    provider: str
    key_id: str
    status: str
    total_deks: int
    completed_deks: int
    failed_deks: int
    progress_percent: int
    elapsed_ms: float
    persisted: bool
    ui: dict[str, Any] = field(default_factory=dict)
    items: tuple[CMEKUpgradeItemResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "upgrade_id": self.upgrade_id,
            "tenant_id": self.tenant_id,
            "from_security_tier": self.from_security_tier,
            "to_security_tier": self.to_security_tier,
            "provider": self.provider,
            "key_id": self.key_id,
            "status": self.status,
            "total_deks": self.total_deks,
            "completed_deks": self.completed_deks,
            "failed_deks": self.failed_deks,
            "progress_percent": self.progress_percent,
            "elapsed_ms": self.elapsed_ms,
            "persisted": self.persisted,
            "ui": dict(self.ui),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class CMEKDowngradeItemResult:
    dek_id: str
    status: str
    source_provider: str
    target_provider: str
    replacement_dek_ref: dict[str, Any] | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dek_id": self.dek_id,
            "status": self.status,
            "source_provider": self.source_provider,
            "target_provider": self.target_provider,
            "replacement_dek_ref": self.replacement_dek_ref,
            "error": self.error,
        }


@dataclass(frozen=True)
class CMEKDowngradePlanResult:
    downgrade_id: str
    tenant_id: str
    from_security_tier: str
    to_security_tier: str
    source_provider: str
    source_key_id: str
    target_provider: str
    target_key_id: str
    status: str
    total_deks: int
    completed_deks: int
    failed_deks: int
    progress_percent: int
    elapsed_ms: float
    persisted: bool
    customer_iam_dependency: str
    ui: dict[str, Any] = field(default_factory=dict)
    items: tuple[CMEKDowngradeItemResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "downgrade_id": self.downgrade_id,
            "tenant_id": self.tenant_id,
            "from_security_tier": self.from_security_tier,
            "to_security_tier": self.to_security_tier,
            "source_provider": self.source_provider,
            "source_key_id": self.source_key_id,
            "target_provider": self.target_provider,
            "target_key_id": self.target_key_id,
            "status": self.status,
            "total_deks": self.total_deks,
            "completed_deks": self.completed_deks,
            "failed_deks": self.failed_deks,
            "progress_percent": self.progress_percent,
            "elapsed_ms": self.elapsed_ms,
            "persisted": self.persisted,
            "customer_iam_dependency": self.customer_iam_dependency,
            "ui": dict(self.ui),
            "items": [item.to_dict() for item in self.items],
        }


def build_target_adapter(
    provider: cmek_wizard.CMEK_PROVIDER,
    *,
    key_id: str,
) -> kms.KMSAdapter:
    """Build the target CMK adapter used by the stateless upgrade path."""

    provider = cmek_wizard.normalise_provider(provider)
    key_id = cmek_wizard.validate_key_id(provider, key_id)
    if provider == "aws-kms":
        return kms.AWSKMSAdapter(key_id=key_id)
    if provider == "gcp-kms":
        return kms.GCPKMSAdapter(key_id=key_id)
    if provider == "vault-transit":
        return kms.VaultTransitKMSAdapter(
            key_id=key_id,
            url=_env_required("OMNISIGHT_VAULT_TRANSIT_URL", provider=provider),
            token=_env_required("OMNISIGHT_VAULT_TRANSIT_TOKEN", provider=provider),
            namespace=_env_optional("OMNISIGHT_VAULT_TRANSIT_NAMESPACE"),
            mount_point=_env_optional("OMNISIGHT_VAULT_TRANSIT_MOUNT_POINT") or "transit",
        )
    raise ValueError("provider must be one of aws-kms, gcp-kms, vault-transit")


def build_master_kek_adapter(*, key_id: str = "local-fernet") -> kms.KMSAdapter:
    """Build the Tier 1 master KEK adapter used by the downgrade path."""

    return kms.LocalFernetKMSAdapter(key_id=key_id)


def plan_tier1_to_tier2_upgrade(
    *,
    tenant_id: str,
    provider: cmek_wizard.CMEK_PROVIDER,
    key_id: str,
    dek_refs: Iterable[Mapping[str, Any]],
    target_kms_adapter: kms.KMSAdapter | None = None,
    source_kms_adapter: kms.KMSAdapter | None = None,
) -> CMEKUpgradePlanResult:
    """Rewrap every provided tenant DEK ref for a Tier 2 CMEK upgrade."""

    provider = cmek_wizard.normalise_provider(provider)
    key_id = cmek_wizard.validate_key_id(provider, key_id)
    target_adapter = target_kms_adapter or build_target_adapter(provider, key_id=key_id)
    started = time.perf_counter()
    upgrade_id = f"cmeku_{secrets.token_hex(8)}"

    items: list[CMEKUpgradeItemResult] = []
    for raw in dek_refs:
        try:
            dek_ref = envelope.TenantDEKRef.from_dict(raw)
            _validate_tenant_binding(tenant_id, dek_ref)
            replacement = envelope.rewrap_tenant_dek_ref(
                dek_ref,
                source_kms_adapter=source_kms_adapter,
                target_kms_adapter=target_adapter,
            )
            items.append(
                CMEKUpgradeItemResult(
                    dek_id=dek_ref.dek_id,
                    status=UPGRADE_STATUS_COMPLETED,
                    source_provider=dek_ref.provider,
                    target_provider=replacement.provider,
                    replacement_dek_ref=replacement.to_dict(),
                )
            )
        except Exception as exc:
            dek_id = str(raw.get("dek_id") or "")
            source_provider = str(raw.get("provider") or "")
            items.append(
                CMEKUpgradeItemResult(
                    dek_id=dek_id,
                    status=UPGRADE_STATUS_FAILED,
                    source_provider=source_provider,
                    target_provider=target_adapter.provider,
                    error=str(exc),
                )
            )

    completed = sum(1 for item in items if item.status == UPGRADE_STATUS_COMPLETED)
    failed = len(items) - completed
    status = UPGRADE_STATUS_COMPLETED if failed == 0 else UPGRADE_STATUS_FAILED
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    progress_percent = 100 if not items else int(round(completed / len(items) * 100))

    return CMEKUpgradePlanResult(
        upgrade_id=upgrade_id,
        tenant_id=tenant_id,
        from_security_tier="tier-1",
        to_security_tier="tier-2",
        provider=provider,
        key_id=key_id,
        status=status,
        total_deks=len(items),
        completed_deks=completed,
        failed_deks=failed,
        progress_percent=progress_percent,
        elapsed_ms=elapsed_ms,
        persisted=False,
        ui=_progress_ui(status, completed=completed, failed=failed, total=len(items)),
        items=tuple(items),
    )


def plan_tier2_to_tier1_downgrade(
    *,
    tenant_id: str,
    provider: cmek_wizard.CMEK_PROVIDER,
    key_id: str,
    dek_refs: Iterable[Mapping[str, Any]],
    source_kms_adapter: kms.KMSAdapter | None = None,
    target_kms_adapter: kms.KMSAdapter | None = None,
) -> CMEKDowngradePlanResult:
    """Rewrap every provided tenant DEK ref back to the Tier 1 master KEK.

    The returned plan keeps customer IAM in use only long enough to
    unwrap current Tier 2 DEK refs. After KS.2.11 persists the
    replacement refs and tier assignment, OmniSight no longer depends on
    the customer CMK for this tenant and the customer can remove the
    OmniSight IAM principal from their key policy.
    """

    provider = cmek_wizard.normalise_provider(provider)
    key_id = cmek_wizard.validate_key_id(provider, key_id)
    source_adapter = source_kms_adapter or build_target_adapter(provider, key_id=key_id)
    target_adapter = target_kms_adapter or build_master_kek_adapter()
    started = time.perf_counter()
    downgrade_id = f"cmekd_{secrets.token_hex(8)}"

    items: list[CMEKDowngradeItemResult] = []
    for raw in dek_refs:
        try:
            dek_ref = envelope.TenantDEKRef.from_dict(raw)
            _validate_tenant_binding(tenant_id, dek_ref)
            replacement = envelope.rewrap_tenant_dek_ref(
                dek_ref,
                source_kms_adapter=source_adapter,
                target_kms_adapter=target_adapter,
            )
            items.append(
                CMEKDowngradeItemResult(
                    dek_id=dek_ref.dek_id,
                    status=DOWNGRADE_STATUS_COMPLETED,
                    source_provider=dek_ref.provider,
                    target_provider=replacement.provider,
                    replacement_dek_ref=replacement.to_dict(),
                )
            )
        except Exception as exc:
            dek_id = str(raw.get("dek_id") or "")
            source_provider = str(raw.get("provider") or "")
            items.append(
                CMEKDowngradeItemResult(
                    dek_id=dek_id,
                    status=DOWNGRADE_STATUS_FAILED,
                    source_provider=source_provider,
                    target_provider=target_adapter.provider,
                    error=str(exc),
                )
            )

    completed = sum(1 for item in items if item.status == DOWNGRADE_STATUS_COMPLETED)
    failed = len(items) - completed
    status = DOWNGRADE_STATUS_COMPLETED if failed == 0 else DOWNGRADE_STATUS_FAILED
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    progress_percent = 100 if not items else int(round(completed / len(items) * 100))

    return CMEKDowngradePlanResult(
        downgrade_id=downgrade_id,
        tenant_id=tenant_id,
        from_security_tier="tier-2",
        to_security_tier="tier-1",
        source_provider=provider,
        source_key_id=key_id,
        target_provider=target_adapter.provider,
        target_key_id=getattr(target_adapter, "key_id", ""),
        status=status,
        total_deks=len(items),
        completed_deks=completed,
        failed_deks=failed,
        progress_percent=progress_percent,
        elapsed_ms=elapsed_ms,
        persisted=False,
        customer_iam_dependency=(
            "required-until-downgrade-persisted"
            if status == DOWNGRADE_STATUS_COMPLETED
            else "still-required"
        ),
        ui=_downgrade_progress_ui(
            status,
            completed=completed,
            failed=failed,
            total=len(items),
        ),
        items=tuple(items),
    )


def _validate_tenant_binding(tenant_id: str, dek_ref: envelope.TenantDEKRef) -> None:
    if dek_ref.tenant_id != tenant_id:
        raise envelope.BindingMismatchError("dek_ref tenant_id does not match path tenant")


def _env_optional(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    return value or None


def _env_required(name: str, *, provider: str) -> str:
    value = _env_optional(name)
    if not value:
        raise kms.KMSConfigurationError(f"{name} is required", provider=provider)
    return value


def _progress_ui(
    status: str,
    *,
    completed: int,
    failed: int,
    total: int,
) -> dict[str, Any]:
    if status == UPGRADE_STATUS_COMPLETED:
        label = "Tier 2 upgrade complete"
        current_step = "complete"
    else:
        label = "Tier 2 upgrade needs operator attention"
        current_step = "failed"
    return {
        "label": label,
        "current_step": current_step,
        "steps": [
            {
                "id": "collect",
                "label": "Load tenant DEKs",
                "status": "completed",
                "count": total,
            },
            {
                "id": "rewrap",
                "label": "Rewrap DEKs with customer CMK",
                "status": status,
                "completed": completed,
                "failed": failed,
                "total": total,
            },
            {
                "id": "persist",
                "label": "Persist Tier 2 assignment",
                "status": "pending",
                "blocked_by": "KS.2.11 durable cmek_configs/tier_assignments schema",
            },
        ],
    }


def _downgrade_progress_ui(
    status: str,
    *,
    completed: int,
    failed: int,
    total: int,
) -> dict[str, Any]:
    if status == DOWNGRADE_STATUS_COMPLETED:
        label = "Tier 1 downgrade complete"
        current_step = "complete"
        iam_status = "pending"
    else:
        label = "Tier 1 downgrade needs operator attention"
        current_step = "failed"
        iam_status = "blocked"
    return {
        "label": label,
        "current_step": current_step,
        "steps": [
            {
                "id": "collect",
                "label": "Load tenant DEKs",
                "status": "completed",
                "count": total,
            },
            {
                "id": "rewrap",
                "label": "Rewrap DEKs with master KEK",
                "status": status,
                "completed": completed,
                "failed": failed,
                "total": total,
            },
            {
                "id": "persist",
                "label": "Persist Tier 1 assignment",
                "status": "pending",
                "blocked_by": "KS.2.11 durable cmek_configs/tier_assignments schema",
            },
            {
                "id": "withdraw-customer-iam",
                "label": "Withdraw OmniSight IAM dependency on customer CMK",
                "status": iam_status,
                "blocked_by": "persisted Tier 1 DEK refs",
            },
        ],
    }


__all__ = [
    "CMEKDowngradeItemResult",
    "CMEKDowngradePlanResult",
    "CMEKUpgradeItemResult",
    "CMEKUpgradePlanResult",
    "DOWNGRADE_STATUS_COMPLETED",
    "DOWNGRADE_STATUS_FAILED",
    "UPGRADE_STATUS_COMPLETED",
    "UPGRADE_STATUS_FAILED",
    "build_master_kek_adapter",
    "build_target_adapter",
    "plan_tier1_to_tier2_upgrade",
    "plan_tier2_to_tier1_downgrade",
]

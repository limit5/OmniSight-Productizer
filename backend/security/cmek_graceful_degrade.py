"""KS.2.6 -- request-start graceful degrade for revoked CMEK tenants.

KS.2.5 records per-worker CMEK health snapshots. This module turns that
snapshot into a request-start decision:

* requests already past the guard are left alone and can finish;
* new CMEK-protected requests for a revoked tenant receive a friendly
  HTTP 403 payload; and
* the payload is explicitly non-retryable and points operators at the
  recovery runbook.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable response constants live at module scope. Revoke status is
read from ``backend.security.cmek_revoke_detector``'s per-worker health
snapshot; every worker polls the same external KMS/Vault source and
derives the same allow/deny decision without shared Python memory.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
This helper writes no shared state. It evaluates the latest detector
snapshot at request start, so it does not introduce a read-after-write
contract or cancel in-flight work after a request has already passed
the guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from backend.security import cmek_revoke_detector


RECOVERY_RUNBOOK_PATH = "docs/ops/cmek_revoke_recovery.md"
ERROR_CODE = "cmek_revoked"
FRIENDLY_DETAIL = (
    "Customer-managed encryption key access is revoked or unreachable for "
    "this tenant. Existing in-flight requests are allowed to finish, but "
    "new CMEK-protected requests are paused until the customer restores "
    "KMS access."
)


@dataclass(frozen=True)
class CMEKDegradeDecision:
    tenant_id: str
    allowed: bool
    error_code: str = ""
    detail: str = ""
    retryable: bool = False
    recovery_runbook: str = ""
    provider: str = ""
    key_id: str = ""
    reason: str = ""
    raw_state: str = ""
    checked_at: float | None = None

    def to_error_payload(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "error_code": self.error_code,
            "tenant_id": self.tenant_id,
            "retryable": self.retryable,
            "recovery_runbook": self.recovery_runbook,
            "in_flight_policy": (
                "Requests accepted before revoke detection are allowed to "
                "finish; this response applies only at request start."
            ),
            "provider": self.provider,
            "key_id": self.key_id,
            "reason": self.reason,
            "raw_state": self.raw_state,
            "checked_at": self.checked_at,
        }


def cmek_degrade_decision_for_tenant(
    tenant_id: str,
    *,
    latest_results: Iterable[Mapping[str, Any]] | None = None,
) -> CMEKDegradeDecision:
    """Return the request-start allow/deny decision for ``tenant_id``."""

    results = (
        list(latest_results)
        if latest_results is not None
        else cmek_revoke_detector.latest_cmek_health_results()
    )
    revoked = [
        result
        for result in results
        if result.get("tenant_id") == tenant_id and result.get("revoked") is True
    ]
    if not revoked:
        return CMEKDegradeDecision(tenant_id=tenant_id, allowed=True, retryable=False)

    latest = max(revoked, key=lambda result: float(result.get("checked_at") or 0.0))
    return CMEKDegradeDecision(
        tenant_id=tenant_id,
        allowed=False,
        error_code=ERROR_CODE,
        detail=FRIENDLY_DETAIL,
        retryable=False,
        recovery_runbook=RECOVERY_RUNBOOK_PATH,
        provider=str(latest.get("provider") or ""),
        key_id=str(latest.get("key_id") or ""),
        reason=str(latest.get("reason") or ""),
        raw_state=str(latest.get("raw_state") or ""),
        checked_at=float(latest["checked_at"]) if latest.get("checked_at") else None,
    )


__all__ = [
    "CMEKDegradeDecision",
    "ERROR_CODE",
    "FRIENDLY_DETAIL",
    "RECOVERY_RUNBOOK_PATH",
    "cmek_degrade_decision_for_tenant",
]

"""KS.2.5 -- CMEK revoke detection via provider key health checks.

Lifespan-scoped async task. Every ``DEFAULT_INTERVAL_S`` (30 s) the
loop calls the provider's DescribeKey-equivalent method for each
configured CMEK adapter:

* AWS KMS: ``DescribeKey`` via :class:`AWSKMSAdapter.describe_key`.
* Google Cloud KMS: ``GetCryptoKey`` via :class:`GCPKMSAdapter.describe_key`.
* Vault Transit: ``read_key`` via :class:`VaultTransitKMSAdapter.describe_key`.

The row's contract is detection only: a disabled key or removed
DescribeKey/read permission is surfaced in-process within 60 s. KS.2.6
owns turning that status into graceful 403 behaviour, and KS.2.11 owns
durable ``cmek_configs`` / ``cmek_revoke_events`` storage. Until that
schema lands, the production loop discovers environment-configured
adapters from the existing KS.2.2-KS.2.4 prefixes.

Module-global state audit (SOP Step 1)
--------------------------------------
``_LOOP_RUNNING`` and ``_LATEST_RESULTS`` are intentionally per-worker.
Each uvicorn worker polls the same external KMS/Vault source on the
same cadence, so revoke detection does not depend on shared Python
memory. The latest-result cache is observability-only; no request path
uses it for authorization in KS.2.5.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
This module does not write PG / Redis / filesystem state. Provider
polling is read-only, so there is no read-after-write downstream
timing contract to preserve.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from backend.security import kms_adapters as kms


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0
MAX_DETECTION_WINDOW_S = 60.0

_LOOP_RUNNING = False
_LATEST_RESULTS: dict[tuple[str, str, str], "CMEKHealthResult"] = {}


class _DescribeKeyAdapter(Protocol):
    provider: str
    key_id: str

    def describe_key(self) -> Any:
        """Return provider metadata or raise ``KMSOperationError``."""


@dataclass(frozen=True)
class CMEKKeyCheck:
    tenant_id: str
    adapter: _DescribeKeyAdapter


@dataclass(frozen=True)
class CMEKHealthResult:
    tenant_id: str
    provider: str
    key_id: str
    ok: bool
    revoked: bool
    reason: str
    checked_at: float
    elapsed_ms: float
    raw_state: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def cache_key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.provider, self.key_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "key_id": self.key_id,
            "ok": self.ok,
            "revoked": self.revoked,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "elapsed_ms": self.elapsed_ms,
            "raw_state": self.raw_state,
            "detail": dict(self.detail),
        }


def _metadata_get(metadata: Any, *path: str) -> Any:
    current = metadata
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _normalise_state(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    return str(value)


def _classify_provider_metadata(
    provider: str,
    metadata: Any,
) -> tuple[bool, str, str, dict[str, Any]]:
    if provider == "aws-kms":
        state = _normalise_state(_metadata_get(metadata, "KeyMetadata", "KeyState"))
        if state and state != "Enabled":
            return False, "key_disabled", state, {"key_state": state}
        return True, "describe_ok", state or "Enabled", {"key_state": state or "Enabled"}

    if provider == "gcp-kms":
        primary_state = _normalise_state(_metadata_get(metadata, "primary", "state"))
        if primary_state and "ENABLED" not in primary_state.upper():
            return (
                False,
                "key_disabled",
                primary_state,
                {"primary_state": primary_state},
            )
        return (
            True,
            "describe_ok",
            primary_state or "reachable",
            {"primary_state": primary_state or "reachable"},
        )

    if provider == "vault-transit":
        supports_encryption = _metadata_get(metadata, "data", "supports_encryption")
        supports_decryption = _metadata_get(metadata, "data", "supports_decryption")
        if supports_encryption is False or supports_decryption is False:
            return (
                False,
                "key_disabled",
                "encrypt_decrypt_disabled",
                {
                    "supports_encryption": supports_encryption,
                    "supports_decryption": supports_decryption,
                },
            )
        return (
            True,
            "describe_ok",
            "reachable",
            {
                "supports_encryption": supports_encryption,
                "supports_decryption": supports_decryption,
            },
        )

    return True, "describe_ok", "reachable", {}


async def check_cmek_key_health(check: CMEKKeyCheck) -> CMEKHealthResult:
    """Run one provider DescribeKey-equivalent health check."""

    started = time.perf_counter()
    checked_at = time.time()
    adapter = check.adapter
    try:
        metadata = await asyncio.to_thread(adapter.describe_key)
    except kms.KMSOperationError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return CMEKHealthResult(
            tenant_id=check.tenant_id,
            provider=adapter.provider,
            key_id=adapter.key_id,
            ok=False,
            revoked=True,
            reason="describe_failed",
            checked_at=checked_at,
            elapsed_ms=elapsed_ms,
            raw_state=type(exc).__name__,
            detail={"error": str(exc)},
        )

    ok, reason, raw_state, detail = _classify_provider_metadata(
        adapter.provider,
        metadata,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return CMEKHealthResult(
        tenant_id=check.tenant_id,
        provider=adapter.provider,
        key_id=adapter.key_id,
        ok=ok,
        revoked=not ok,
        reason=reason,
        checked_at=checked_at,
        elapsed_ms=elapsed_ms,
        raw_state=raw_state,
        detail=detail,
    )


async def check_all_cmek_keys(
    checks: Iterable[CMEKKeyCheck],
    *,
    record_result: Callable[[CMEKHealthResult], None] | None = None,
) -> list[CMEKHealthResult]:
    results: list[CMEKHealthResult] = []
    for check in checks:
        result = await check_cmek_key_health(check)
        if record_result is not None:
            record_result(result)
        results.append(result)
    return results


def record_cmek_health_result(result: CMEKHealthResult) -> None:
    _LATEST_RESULTS[result.cache_key()] = result


def latest_cmek_health_results() -> list[dict[str, Any]]:
    return [result.to_dict() for result in _LATEST_RESULTS.values()]


def load_env_cmek_key_checks() -> list[CMEKKeyCheck]:
    """Build checks from existing KS.2.2-KS.2.4 production env prefixes."""

    tenant_id = os.environ.get("OMNISIGHT_CMEK_HEALTH_TENANT_ID", "").strip()
    if not tenant_id:
        return []

    checks: list[CMEKKeyCheck] = []
    if os.environ.get("OMNISIGHT_AWS_KMS_KEY_ID", "").strip():
        checks.append(CMEKKeyCheck(tenant_id, kms.AWSKMSAdapter.from_environment()))
    if os.environ.get("OMNISIGHT_GCP_KMS_KEY_ID", "").strip():
        checks.append(CMEKKeyCheck(tenant_id, kms.GCPKMSAdapter.from_environment()))
    if os.environ.get("OMNISIGHT_VAULT_TRANSIT_KEY_ID", "").strip():
        checks.append(
            CMEKKeyCheck(tenant_id, kms.VaultTransitKMSAdapter.from_environment())
        )
    return checks


async def run_detection_loop(
    *,
    interval_s: float | None = None,
    load_checks: Callable[[], Iterable[CMEKKeyCheck]] = load_env_cmek_key_checks,
    record_result: Callable[[CMEKHealthResult], None] = record_cmek_health_result,
) -> None:
    """Background coroutine: poll CMEK key health within the 60 s window."""

    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True
    interval = float(interval_s if interval_s is not None else DEFAULT_INTERVAL_S)
    if interval <= 0 or interval > MAX_DETECTION_WINDOW_S:
        logger.warning(
            "cmek revoke detector interval %.1fs is outside the 60s contract; "
            "using %.1fs",
            interval,
            DEFAULT_INTERVAL_S,
        )
        interval = DEFAULT_INTERVAL_S

    try:
        while True:
            try:
                checks = list(load_checks())
                results = await check_all_cmek_keys(
                    checks,
                    record_result=record_result,
                )
                revoked = [r for r in results if r.revoked]
                if revoked:
                    logger.warning(
                        "cmek revoke detector: %d revoked/unhealthy key(s): %s",
                        len(revoked),
                        [
                            {
                                "tenant_id": r.tenant_id,
                                "provider": r.provider,
                                "key_id": r.key_id,
                                "reason": r.reason,
                                "raw_state": r.raw_state,
                            }
                            for r in revoked
                        ],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("cmek revoke detector loop error: %s", exc)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
    finally:
        _LOOP_RUNNING = False


def _reset_for_tests() -> None:
    global _LOOP_RUNNING
    _LOOP_RUNNING = False
    _LATEST_RESULTS.clear()


__all__ = [
    "CMEKHealthResult",
    "CMEKKeyCheck",
    "DEFAULT_INTERVAL_S",
    "MAX_DETECTION_WINDOW_S",
    "check_all_cmek_keys",
    "check_cmek_key_health",
    "latest_cmek_health_results",
    "load_env_cmek_key_checks",
    "record_cmek_health_result",
    "run_detection_loop",
]

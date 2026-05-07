"""MP.W16.2 -- subscription account expiry monitoring.

The monitor reuses the existing provider-adapter health-check contract:
adapters report whether their CLI subscription is active and, when the
vendor output exposes it, the subscription expiry timestamp.  This module
classifies those health snapshots and emits a lightweight SSE/log alert
for inactive, expired, or near-expiry accounts.

Module-global state audit (per project SOP)
-------------------------------------------
``_LOOP_RUNNING`` is a process-local singleton guard matching the existing
background refresher pattern in :mod:`backend.llm_balance_refresher`.
Every worker derives the same interval/window constants from code/env and
publishes independent best-effort alerts; no durable state is kept here.

Read-after-write timing audit
-----------------------------
N/A -- this path does not write database rows. The only side effects are
best-effort SSE frames and system-log lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from backend import feature_flags
from backend.agents import provider_orchestrator
from backend.agents.provider_orchestrator import HealthStatus, ProviderAdapter
from backend.agents.routing_policy import MP_ENABLED_ENV

# Register shipped MP subscription providers before list_adapters().
import backend.agents.provider_adapters.anthropic_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.gemini_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.openai_subscription  # noqa: F401,E402


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 6 * 60 * 60
DEFAULT_WARN_WINDOW_S = 14 * 24 * 60 * 60
DEFAULT_CRITICAL_WINDOW_S = 3 * 24 * 60 * 60

INTERVAL_ENV = "OMNISIGHT_MP_SUBSCRIPTION_MONITOR_INTERVAL_S"
WARN_WINDOW_ENV = "OMNISIGHT_MP_SUBSCRIPTION_EXPIRY_WARN_S"
CRITICAL_WINDOW_ENV = "OMNISIGHT_MP_SUBSCRIPTION_EXPIRY_CRITICAL_S"

AccountExpiryLevel = Literal["ok", "unknown", "warning", "critical", "error"]


@dataclass(frozen=True)
class SubscriptionAccountStatus:
    """Classified account-expiry state for one subscription provider."""

    provider_id: str
    level: AccountExpiryLevel
    subscription_active: bool
    reachable: bool
    checked_at: datetime
    expires_at: datetime | None = None
    seconds_until_expiry: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id,
            "level": self.level,
            "subscription_active": self.subscription_active,
            "reachable": self.reachable,
            "checked_at": self.checked_at.isoformat(),
            "expires_at": (
                None
                if self.expires_at is None
                else self.expires_at.isoformat()
            ),
            "seconds_until_expiry": self.seconds_until_expiry,
            "detail": self.detail,
        }


def monitor_once(
    *,
    orchestrator: object = provider_orchestrator,
    now: datetime | None = None,
    emit: bool = True,
) -> list[SubscriptionAccountStatus]:
    """Probe every registered subscription provider once.

    Returns statuses in provider-id order. Health-check exceptions are
    converted to ``level="error"`` entries so one broken adapter does not
    mask expiry status for the others.
    """
    checked_at = _normalise_now(now)
    statuses: list[SubscriptionAccountStatus] = []
    for provider_id in _list_provider_ids(orchestrator):
        adapter = _get_adapter(orchestrator, provider_id)
        if adapter is None:
            continue
        status = _status_for_adapter(adapter, checked_at)
        statuses.append(status)
        if emit and status.level in {"warning", "critical", "error"}:
            _emit_status(status)
    return statuses


async def run_monitor_loop(*, interval_s: float | None = None) -> None:
    """Run subscription-expiry monitoring until cancelled."""
    global _LOOP_RUNNING
    if _LOOP_RUNNING:
        return
    _LOOP_RUNNING = True
    interval = interval_s if interval_s is not None else _env_float(
        INTERVAL_ENV, DEFAULT_INTERVAL_S,
    )
    try:
        if _monitoring_enabled():
            try:
                monitor_once()
            except Exception as exc:
                logger.warning(
                    "subscription account monitor initial tick failed: %s", exc,
                )

        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            if not _monitoring_enabled():
                continue
            try:
                monitor_once()
            except Exception as exc:
                logger.warning("subscription account monitor tick failed: %s", exc)
    except asyncio.CancelledError:
        pass
    finally:
        _LOOP_RUNNING = False


def _status_for_adapter(
    adapter: ProviderAdapter,
    checked_at: datetime,
) -> SubscriptionAccountStatus:
    provider_id = adapter.provider_id()
    try:
        health = adapter.health_check()
    except Exception as exc:
        return SubscriptionAccountStatus(
            provider_id=provider_id,
            level="error",
            subscription_active=False,
            reachable=False,
            checked_at=checked_at,
            detail=f"health check failed: {type(exc).__name__}: {exc}",
        )
    return _classify_health(health, checked_at)


def _classify_health(
    health: HealthStatus,
    checked_at: datetime,
) -> SubscriptionAccountStatus:
    expires_at = _normalise_expires_at(health.subscription_expires_at)
    seconds_until_expiry = (
        None
        if expires_at is None
        else (expires_at - checked_at).total_seconds()
    )
    if not health.reachable or not health.subscription_active:
        level: AccountExpiryLevel = "critical"
    elif seconds_until_expiry is None:
        level = "unknown"
    elif seconds_until_expiry <= _critical_window_s():
        level = "critical"
    elif seconds_until_expiry <= _warn_window_s():
        level = "warning"
    else:
        level = "ok"
    return SubscriptionAccountStatus(
        provider_id=health.provider_id,
        level=level,
        subscription_active=health.subscription_active,
        reachable=health.reachable,
        checked_at=checked_at,
        expires_at=expires_at,
        seconds_until_expiry=seconds_until_expiry,
        detail=health.detail,
    )


def _emit_status(status: SubscriptionAccountStatus) -> None:
    payload = status.to_dict()
    try:
        from backend.events import _log, bus
        bus.publish(
            "provider.subscription.expiry",
            payload,
            broadcast_scope="user",
        )
        _log(
            "[PROVIDER-SUBSCRIPTION] "
            f"{status.provider_id} level={status.level} "
            f"expires_at={payload['expires_at']} detail={status.detail}",
            "warn" if status.level != "error" else "error",
        )
    except Exception as exc:
        logger.debug("subscription account alert emit failed: %s", exc)


def _list_provider_ids(orchestrator: object) -> list[str]:
    try:
        ids = orchestrator.list_adapters()  # type: ignore[attr-defined]
    except Exception:
        return []
    return [
        provider_id
        for provider_id in ids
        if isinstance(provider_id, str) and provider_id.endswith("-subscription")
    ]


def _get_adapter(orchestrator: object, provider_id: str) -> ProviderAdapter | None:
    try:
        return orchestrator.get_adapter(provider_id)  # type: ignore[attr-defined]
    except Exception:
        return None


def _normalise_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalise_expires_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _monitoring_enabled() -> bool:
    return feature_flags.resolve_env_backed_feature_flag(MP_ENABLED_ENV)


def _warn_window_s() -> float:
    return _env_float(WARN_WINDOW_ENV, DEFAULT_WARN_WINDOW_S)


def _critical_window_s() -> float:
    return _env_float(CRITICAL_WINDOW_ENV, DEFAULT_CRITICAL_WINDOW_S)


_LOOP_RUNNING = False


__all__ = [
    "CRITICAL_WINDOW_ENV",
    "DEFAULT_CRITICAL_WINDOW_S",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_WARN_WINDOW_S",
    "INTERVAL_ENV",
    "SubscriptionAccountStatus",
    "WARN_WINDOW_ENV",
    "monitor_once",
    "run_monitor_loop",
]

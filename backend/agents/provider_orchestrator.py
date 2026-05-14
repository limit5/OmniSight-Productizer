"""MP.W1.1 -- provider adapter interface and registry.

This module defines the shape shared by future Anthropic / OpenAI /
Gemini / xAI subscription adapters.  It intentionally does not choose a
provider or implement a concrete adapter; downstream MP.W1 tickets own
those pieces.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
The provider registry is a module-level ``dict[str, ProviderAdapter]``
guarded by a module-level ``threading.RLock``.  Mutation is only exposed
through ``register_adapter()``.  The re-entrant lock is intentional:
provider adapters may perform nested registration during construction or
module import, and an ordinary ``Lock`` would deadlock that path.

Read-after-write timing audit
-----------------------------
``register_adapter()`` writes the adapter and ``get_adapter()`` reads it
under the same ``RLock``.  A register -> get sequence in one process is
therefore atomic with respect to other registry readers and writers once
the registration call returns.

Smoke registry pattern
----------------------
::

    adapter = AnthropicSubscriptionAdapter(...)
    register_adapter(adapter)
    selected = get_adapter("anthropic-subscription")
    result = selected.dispatch(task)

``CircuitBreaker`` is a small synchronous primitive.  It tracks local
consecutive failures for fast trip decisions and reads persisted
``provider_quota_state.circuit_state`` through
``provider_quota_tracker.get_quota_state()`` so quota-enforced open
circuits are honoured by all workers.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from os import environ
from threading import RLock
from types import MappingProxyType

from backend.agents.provider_quota_tracker import (
    DEFAULT_5H_CAP_TOKENS,
    DEFAULT_WEEKLY_CAP_TOKENS,
    QuotaState,
)
from backend.agents.provider_quota_tracker import get_quota_state as _get_quota_state


@dataclass(frozen=True)
class TaskSpec:
    """Provider-neutral task payload passed to subscription adapters."""

    prompt: str
    agent_class: str
    tier: str
    area: list[str]
    correlation_id: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    """Provider-neutral result returned by subscription adapters."""

    success: bool
    tokens_used: int
    latency_seconds: float
    error: str | None
    provider_id: str


@dataclass(frozen=True)
class HealthStatus:
    """Fast liveness probe result for one provider adapter."""

    provider_id: str
    reachable: bool
    last_checked_at: datetime
    cli_installed: bool = False
    subscription_active: bool = False
    subscription_expires_at: datetime | None = None
    detail: str = ""


@dataclass(frozen=True)
class ProviderRegistryEntry:
    """Provider discovery row for available and future adapters."""

    provider_id: str
    status: str
    coming_version: str | None = None


@dataclass(frozen=True)
class PrePickupProviderDecision:
    """Provider quota/circuit decision for runner pre-pickup gates."""

    ok: bool
    reason: str
    provider_id: str | None = None


class ProviderNotRegistered(Exception):
    """Raised when a provider id has no registered adapter."""


class ProviderAdapter(ABC):
    """Abstract base for subscription provider adapters."""

    @abstractmethod
    def provider_id(self) -> str:
        """Return the stable provider identifier."""

    @abstractmethod
    def dispatch(self, task: TaskSpec) -> DispatchResult:
        """Synchronously invoke this provider for one task."""

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Return a quick provider liveness probe."""

    @abstractmethod
    def get_quota_state(self) -> QuotaState:
        """Return quota state for this provider via provider_quota_tracker."""
        return _get_quota_state(self.provider_id())


_REGISTRY_LOCK = RLock()
_REGISTRY: dict[str, ProviderAdapter] = {}
_COMING_PROVIDERS: dict[str, ProviderRegistryEntry] = {}

SUBSCRIPTION_VENDOR_REGISTRY = MappingProxyType({
    "anthropic": "anthropic-subscription",
    "google": "gemini-subscription",
    "grok": "grok-subscription",
    "openai": "openai-subscription",
    "xai": "xai-subscription",
})
_AGENT_CLASS_PROVIDER_PREFIXES = MappingProxyType({
    "api-anthropic": ("anthropic",),
    "api-gemini": ("gemini",),
    "api-openai": ("openai",),
    "api-xai": ("xai",),
    "subscription-claude": ("anthropic",),
    "subscription-codex": ("openai",),
    "subscription-gemini": ("gemini",),
    "subscription-xai": ("xai",),
})


def register_adapter(adapter: ProviderAdapter) -> None:
    """Register or replace a provider adapter by its stable provider id."""
    provider_id = _normalise_provider_id(adapter.provider_id())
    with _REGISTRY_LOCK:
        _REGISTRY[provider_id] = adapter
        _COMING_PROVIDERS.pop(provider_id, None)


def register_coming_provider(provider_id: str, *, coming_version: str) -> None:
    """Register a future provider for discovery without routing to it."""
    provider_id = _normalise_provider_id(provider_id)
    version = coming_version.strip()
    if not version:
        raise ValueError("coming_version must be non-empty")
    with _REGISTRY_LOCK:
        if provider_id not in _REGISTRY:
            _COMING_PROVIDERS[provider_id] = ProviderRegistryEntry(
                provider_id=provider_id,
                status="coming",
                coming_version=version,
            )


def get_adapter(provider_id: str) -> ProviderAdapter:
    """Return a registered adapter, or raise ``ProviderNotRegistered``."""
    provider_id = _normalise_provider_id(provider_id)
    with _REGISTRY_LOCK:
        try:
            return _REGISTRY[provider_id]
        except KeyError as exc:
            raise ProviderNotRegistered(provider_id) from exc


def list_adapters() -> list[str]:
    """Return registered provider ids in stable sort order."""
    with _REGISTRY_LOCK:
        return sorted(_REGISTRY)


def list_provider_entries() -> list[ProviderRegistryEntry]:
    """Return discovery rows for active adapters and future providers."""
    with _REGISTRY_LOCK:
        entries = [
            ProviderRegistryEntry(provider_id=provider_id, status="available")
            for provider_id in _REGISTRY
        ]
        entries.extend(_COMING_PROVIDERS.values())
    return sorted(entries, key=lambda entry: entry.provider_id)


def list_subscription_vendors() -> list[str]:
    """Return frontend-facing vendor ids covered by subscription adapters."""
    return sorted(SUBSCRIPTION_VENDOR_REGISTRY)


def subscription_adapter_id_for_vendor(vendor_id: str) -> str:
    """Return the subscription adapter id for a frontend-facing vendor."""
    key = vendor_id.strip().lower()
    try:
        return SUBSCRIPTION_VENDOR_REGISTRY[key]
    except KeyError as exc:
        raise ProviderNotRegistered(key) from exc


def pre_pickup_provider_decision(task: TaskSpec) -> PrePickupProviderDecision:
    """Return the provider quota/circuit gate decision before runner pickup.

    This gate is intentionally narrower than routing: it does not rank
    providers or probe health.  It only blocks when every provider matching the
    task's ``agent_class`` has an explicit quota/circuit reason that would make
    a cost-bearing dispatch fail after pickup.
    """
    blocked_reasons: list[str] = []
    unavailable_reasons: list[str] = []
    inspected = 0

    for provider_id in list_adapters():
        provider_id = _normalise_provider_id(provider_id)
        if not _agent_class_allows_provider(task.agent_class, provider_id):
            continue
        inspected += 1
        adapter = get_adapter(provider_id)
        try:
            state = adapter.get_quota_state()
        except Exception as exc:  # noqa: BLE001 - preserve pickup if telemetry is down
            unavailable_reasons.append(
                f"provider_quota_unavailable:{provider_id}:{type(exc).__name__}"
            )
            continue

        reason = _quota_or_circuit_block_reason(state)
        if reason is None:
            return PrePickupProviderDecision(
                ok=True,
                reason="pre-pickup provider checks passed",
                provider_id=provider_id,
            )
        blocked_reasons.append(reason)

    if inspected == 0:
        return PrePickupProviderDecision(
            ok=True,
            reason="pre-pickup provider checks skipped: no matching provider",
        )
    if not blocked_reasons:
        return PrePickupProviderDecision(
            ok=True,
            reason=(
                "pre-pickup provider checks skipped: "
                + "; ".join(unavailable_reasons)
            ),
        )
    return PrePickupProviderDecision(ok=False, reason="; ".join(blocked_reasons))


class CircuitBreaker:
    """Consecutive-failure breaker for one provider.

    ``record_outcome(False)`` records one failed dispatch, including
    rate-limit failures.  Five consecutive failures trip the local open
    state.  ``is_open()`` also consults persisted quota state so database
    quota caps from OP-15 keep routing away from exhausted providers.
    """

    trip_threshold = 5
    cooldown_seconds = 300

    def __init__(self, provider_id: str):
        self.provider_id = _normalise_provider_id(provider_id)
        self._lock = RLock()
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def record_outcome(self, success: bool) -> None:
        """Record one dispatch outcome and update local breaker state."""
        with self._lock:
            if success:
                self._consecutive_failures = 0
                self._opened_at = None
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.trip_threshold:
                self._opened_at = time.monotonic()

    def is_open(self) -> bool:
        """Return whether this provider should currently reject dispatch."""
        state = _get_quota_state(self.provider_id)
        if state.circuit_state == "open":
            return True

        with self._lock:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                self._opened_at = None
                self._consecutive_failures = 0
                return False
            return True


def _normalise_provider_id(provider_id: str) -> str:
    out = provider_id.strip()
    if not out:
        raise ValueError("provider_id must be non-empty")
    return out


def _agent_class_allows_provider(agent_class: str, provider_id: str) -> bool:
    prefixes = _AGENT_CLASS_PROVIDER_PREFIXES.get(agent_class.strip())
    if prefixes is None:
        return True
    provider_prefix = provider_id.split("-", 1)[0]
    return provider_prefix in prefixes


def _quota_or_circuit_block_reason(state: QuotaState) -> str | None:
    if state.circuit_state == "open":
        return f"provider_circuit_open:{state.provider}"
    if state.rolling_5h_tokens >= _cap_for(state.provider, "5h"):
        return f"provider_quota_exhausted:{state.provider}:5h"
    if state.weekly_tokens >= _cap_for(state.provider, "weekly"):
        return f"provider_quota_exhausted:{state.provider}:weekly"
    return None


def _cap_for(provider_id: str, scope: str) -> int:
    suffix = "5H" if scope == "5h" else "WEEKLY"
    env_name = f"OMNISIGHT_PROVIDER_CAP_{_env_provider(provider_id)}_{suffix}"
    raw = (environ.get(env_name) or "").strip()
    if raw:
        try:
            cap = int(raw)
        except ValueError:
            cap = (
                DEFAULT_5H_CAP_TOKENS
                if scope == "5h"
                else DEFAULT_WEEKLY_CAP_TOKENS
            )
        if cap > 0:
            return cap
    if scope == "5h":
        return DEFAULT_5H_CAP_TOKENS
    return DEFAULT_WEEKLY_CAP_TOKENS


def _env_provider(provider_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in provider_id.upper())


__all__ = [
    "CircuitBreaker",
    "DispatchResult",
    "HealthStatus",
    "PrePickupProviderDecision",
    "ProviderAdapter",
    "ProviderNotRegistered",
    "ProviderRegistryEntry",
    "QuotaState",
    "SUBSCRIPTION_VENDOR_REGISTRY",
    "TaskSpec",
    "get_adapter",
    "list_adapters",
    "list_provider_entries",
    "list_subscription_vendors",
    "pre_pickup_provider_decision",
    "register_adapter",
    "register_coming_provider",
    "subscription_adapter_id_for_vendor",
]

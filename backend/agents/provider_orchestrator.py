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
from threading import RLock

from backend.agents.provider_quota_tracker import QuotaState
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


def register_adapter(adapter: ProviderAdapter) -> None:
    """Register or replace a provider adapter by its stable provider id."""
    provider_id = _normalise_provider_id(adapter.provider_id())
    with _REGISTRY_LOCK:
        _REGISTRY[provider_id] = adapter


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


__all__ = [
    "CircuitBreaker",
    "DispatchResult",
    "HealthStatus",
    "ProviderAdapter",
    "ProviderNotRegistered",
    "QuotaState",
    "TaskSpec",
    "get_adapter",
    "list_adapters",
    "register_adapter",
]

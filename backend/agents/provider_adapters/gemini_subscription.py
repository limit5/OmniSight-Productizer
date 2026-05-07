"""Gemini subscription adapter placeholder.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only.  It does not keep mutable
module-level state.  ``GeminiSubscriptionAdapter`` is a regular instantiable
class; concrete dispatch, health, and quota behavior are intentionally deferred
to the ticket that wires the Gemini subscription runtime.

Import side-effect contract
---------------------------
Importing this module registers one ``GeminiSubscriptionAdapter`` instance
with ``backend.agents.provider_orchestrator``.  Downstream routing code can
therefore make the adapter structurally visible with::

    import backend.agents.provider_adapters.gemini_subscription
"""

from __future__ import annotations

from backend.agents.provider_orchestrator import (
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    TaskSpec,
    register_adapter,
)
from backend.agents.provider_quota_tracker import QuotaState


PROVIDER_ID = "gemini-subscription"


class GeminiSubscriptionAdapter(ProviderAdapter):
    """Structural slot for the future Gemini subscription adapter."""

    def provider_id(self) -> str:
        return PROVIDER_ID

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        raise NotImplementedError("Gemini subscription dispatch is not implemented")

    def health_check(self) -> HealthStatus:
        raise NotImplementedError("Gemini subscription health check is not implemented")

    def get_quota_state(self) -> QuotaState:
        raise NotImplementedError("Gemini subscription quota state is not implemented")


register_adapter(GeminiSubscriptionAdapter())


__all__ = ("GeminiSubscriptionAdapter",)

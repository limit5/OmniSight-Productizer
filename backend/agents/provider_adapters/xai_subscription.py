"""xAI subscription adapter placeholder.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only.  It does not keep mutable
module-level state.  ``XaiSubscriptionAdapter`` is a regular instantiable
class; concrete dispatch, health, and quota behavior are intentionally deferred
to the ticket that wires the xAI subscription runtime.

Import side-effect contract
---------------------------
Importing this module registers one ``XaiSubscriptionAdapter`` instance
with ``backend.agents.provider_orchestrator``.  Downstream routing code can
therefore make the adapter structurally visible with::

    import backend.agents.provider_adapters.xai_subscription
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


PROVIDER_ID = "xai-subscription"


class XaiSubscriptionAdapter(ProviderAdapter):
    """Structural slot for the future xAI subscription adapter."""

    def provider_id(self) -> str:
        return PROVIDER_ID

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        raise NotImplementedError("xAI subscription dispatch is not implemented")

    def health_check(self) -> HealthStatus:
        raise NotImplementedError("xAI subscription health check is not implemented")

    def get_quota_state(self) -> QuotaState:
        raise NotImplementedError("xAI subscription quota state is not implemented")


register_adapter(XaiSubscriptionAdapter())


__all__ = ("XaiSubscriptionAdapter",)

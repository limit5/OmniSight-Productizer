"""Grok subscription adapter shell.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only.  It does not keep mutable
module-level state.  ``GrokSubscriptionAdapter`` is a regular instantiable
class; concrete dispatch, health, and quota behavior are intentionally deferred
to MP.W14.

Import side-effect contract
---------------------------
Importing this module registers a ``coming`` provider discovery entry with
``backend.agents.provider_orchestrator``.  It does not register an active
adapter, so routing code can show Grok without dispatching work to it.
"""

from __future__ import annotations

from backend.agents.provider_orchestrator import (
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    TaskSpec,
    register_coming_provider,
)
from backend.agents.provider_quota_tracker import QuotaState


PROVIDER_ID = "grok-subscription"
COMING_VERSION = "v0.6.0"
NOT_IMPLEMENTED_MESSAGE = "xAI/Grok provider arrives in v0.6.0; tracked under MP.W14"


class GrokSubscriptionAdapter(ProviderAdapter):
    """Structural slot for the future xAI/Grok subscription adapter."""

    def provider_id(self) -> str:
        return PROVIDER_ID

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)

    def health_check(self) -> HealthStatus:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)

    def get_quota_state(self) -> QuotaState:
        raise NotImplementedError(NOT_IMPLEMENTED_MESSAGE)


register_coming_provider(PROVIDER_ID, coming_version=COMING_VERSION)


__all__ = ("GrokSubscriptionAdapter",)

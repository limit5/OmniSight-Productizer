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

import json
from datetime import datetime, timezone

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
        return DispatchResult(
            success=False,
            tokens_used=0,
            latency_seconds=0.0,
            error=json.dumps({"kind": "not_ready", "provider_id": PROVIDER_ID}),
            provider_id=PROVIDER_ID,
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(
            provider_id=PROVIDER_ID,
            reachable=False,
            last_checked_at=datetime.now(timezone.utc),
            cli_installed=False,
            subscription_active=False,
            detail="Gemini subscription runtime is not wired yet",
        )

    def get_quota_state(self) -> QuotaState:
        return QuotaState(
            provider=PROVIDER_ID,
            rolling_5h_tokens=0,
            weekly_tokens=0,
            last_reset_at=None,
            last_cap_hit_at=None,
            circuit_state="closed",
        )


register_adapter(GeminiSubscriptionAdapter())


__all__ = ("GeminiSubscriptionAdapter",)

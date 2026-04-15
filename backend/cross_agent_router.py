"""Cross-agent observation routing (B1 #209).

When an agent emits a ``cross_agent/observation`` finding, this module
creates a Decision Engine proposal so the orchestrator (or operator)
can decide how to relay the observation to the target agent.

The proposal carries ``blocking=True`` in its *source* dict when the
reporter is blocked, signalling the DE UI to prioritise it.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.finding_types import FindingType

logger = logging.getLogger(__name__)


def route_cross_agent_finding(
    *,
    finding_id: str,
    task_id: str,
    reporter_agent_id: str,
    target_agent_id: str | None = None,
    message: str,
    context: dict[str, Any] | None = None,
    blocking: bool = False,
) -> Any:
    """Create a DE proposal for a cross-agent observation.

    Returns the :class:`~backend.decision_engine.Decision` created by
    ``propose()``, or *None* if the proposal could not be filed.
    """
    from backend import decision_engine as de

    ctx = dict(context or {})
    target = target_agent_id or ctx.get("target_agent_id", "unknown")

    title = f"Cross-agent observation from {reporter_agent_id}"
    detail = (
        f"Agent **{reporter_agent_id}** reported an observation for "
        f"agent **{target}**:\n\n{message}"
    )

    severity = de.DecisionSeverity.routine
    if blocking:
        severity = de.DecisionSeverity.risky

    options = [
        {"id": "relay", "label": "Relay to target agent",
         "description": f"Forward observation to {target}"},
        {"id": "dismiss", "label": "Dismiss",
         "description": "Acknowledge without forwarding"},
    ]

    source: dict[str, Any] = {
        "finding_id": finding_id,
        "finding_type": FindingType.cross_agent_observation.value,
        "reporter_agent_id": reporter_agent_id,
        "target_agent_id": target,
        "task_id": task_id,
        "blocking": blocking,
    }

    try:
        decision = de.propose(
            kind="cross_agent/observation",
            title=title,
            detail=detail,
            options=options,
            default_option_id="relay",
            severity=severity,
            source=source,
        )
        logger.info(
            "Cross-agent proposal %s created (reporter=%s, target=%s, blocking=%s)",
            decision.id, reporter_agent_id, target, blocking,
        )
        _notify_target(decision, target)
        return decision
    except Exception as exc:
        logger.warning("Failed to create cross-agent proposal: %s", exc)
        return None


def _notify_target(decision: Any, target_agent_id: str) -> None:
    """Emit an SSE notification so the target agent's UI lights up."""
    try:
        from backend.events import bus
        bus.publish("cross_agent_observation", {
            "decision_id": decision.id,
            "target_agent_id": target_agent_id,
            "title": decision.title,
            "blocking": decision.source.get("blocking", False),
        })
    except Exception as exc:
        logger.debug("cross_agent notify failed: %s", exc)

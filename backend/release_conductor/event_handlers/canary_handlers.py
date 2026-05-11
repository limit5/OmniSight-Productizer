"""OP-947 H2 — Canary-side event handlers for the L3 conductor.

Three matrix rows live here:

* row 9 (``canary.stage.{started,transitioned}``) — advance the R8 /
  R9 release child to ``公開済み`` when the matching stage transition
  lands.
* row 10 (``canary.gate.failed`` / ``canary.rolled_back``) — halt the
  L3 advance loop and surface the gate failure to the operator. Per
  the ADR we do NOT roll back upstream stages here (the rollback is
  scoped to the canary stage by D10 contract).
* row 11 (``canary-stage-transition`` from D9 prod orchestrator,
  OP-881) — placeholder no-op until OP-881 ships. The handler exists
  so the dispatch table has a registered row from day one (so a
  premature event doesn't land in dead-letter); when OP-881 lands it
  replaces the placeholder body.

State-machine handoff: the actual ``release_state`` transitions are
called inside ``on_stage_event`` via
``backend.release_conductor.state_machine.transition`` — but ONLY when
the payload carries enough information to identify the release and
the target state is in the allowed graph. If either is missing the
handler returns a classified hint and lets the JIRA-graph path (G3
cron fallback per ADR-0018 §Failure modes) advance the state.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.release_conductor import state_machine
from backend.release_conductor.event_handlers import slo_handlers


logger = logging.getLogger(__name__)


# Stage name → release_state target. Mirrors the D10 stage names in
# ``backend/orchestrator/canary.py``.
_STAGE_TO_STATE: dict[str, str] = {
    "canary_5": state_machine.STATE_CANARY_5,
    "canary_25": state_machine.STATE_CANARY_25,
    "canary_100": state_machine.STATE_CANARY_100,
}


def _release_id(event: dict[str, Any]) -> str | None:
    rid = event.get("release_id") or event.get("rollout_id")
    return str(rid) if rid else None


def on_stage_event(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 row 9 — advance the release child / state machine.

    When ``event_type == canary.stage.transitioned`` we move the
    state machine to the target stage. ``canary.stage.started`` is
    treated as a dashboard breadcrumb (informational; the transition
    fires on the *completion* of the prior stage, not on entry).
    """
    release_id = _release_id(event)
    stage = str(event.get("stage_name") or event.get("stage") or "")
    transition_kind = str(event.get("transition_kind") or event.get("event") or "")
    is_completion = transition_kind in {"transitioned", "completed"} or bool(
        event.get("completed_at")
    )

    if not release_id:
        return {"outcome": "ignored_no_release_id", "stage": stage}

    target_state = _STAGE_TO_STATE.get(stage)
    if not target_state:
        return {
            "outcome": "ignored_unknown_stage",
            "release_id": release_id,
            "stage": stage,
        }

    if not is_completion:
        return {
            "outcome": "stage_started",
            "release_id": release_id,
            "stage": stage,
        }

    # Look up the current state to compute the from→to edge; refuse
    # if the release-state row doesn't exist yet (G1 hasn't created
    # it) — the G3 cron fallback will reconcile.
    try:
        row = state_machine.get_by_release_id(release_id=release_id)
    except state_machine.ReleaseNotFound:
        logger.info(
            "release_conductor.canary.stage release_id=%s not_yet_created",
            release_id,
        )
        return {
            "outcome": "release_not_found",
            "release_id": release_id,
            "stage": stage,
        }

    current = row["state"]
    if current == target_state:
        return {
            "outcome": "already_in_state",
            "release_id": release_id,
            "state": current,
        }
    if target_state not in state_machine.ALLOWED_TRANSITIONS.get(
        current, frozenset()
    ):
        # Out-of-order event — log + return; do NOT raise. ADR-0018
        # §Failure modes says we reconcile against the graph, not
        # against the event stream.
        logger.info(
            "release_conductor.canary.stage out_of_order release_id=%s "
            "current=%s target=%s",
            release_id,
            current,
            target_state,
        )
        return {
            "outcome": "out_of_order",
            "release_id": release_id,
            "current": current,
            "target": target_state,
        }

    state_machine.transition(
        release_id=release_id,
        from_state=current,
        to_state=target_state,
        reason=f"canary.stage.{stage}",
    )
    return {
        "outcome": "state_advanced",
        "release_id": release_id,
        "from_state": current,
        "to_state": target_state,
    }


def on_gate_failed(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 row 10 — canary.gate.failed halts the advance loop.

    We delegate the halt to ``slo_handlers._HALT_STATE`` so the runner
    pickup gate only has to consult one registry. The handler does
    not roll back upstream stages.
    """
    release_id = _release_id(event)
    halt_key = release_id or "__global__"
    slo_handlers.on_slo_breach(
        {
            "release_id": release_id,
            "breach_id": str(event.get("rollout_id") or "gate_failed"),
            "error_rate": event.get("error_rate"),
            "p95": event.get("p95"),
            "breach_window_start": event.get("failed_at"),
        }
    )
    logger.warning(
        "release_conductor.canary.gate_failed halt_key=%s payload=%s",
        halt_key,
        event,
    )
    return {
        "outcome": "halt_recorded",
        "release_id": release_id,
        "reason": "canary.gate.failed",
    }


def on_rolled_back(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 row 10 — canary.rolled_back records the rollback hint.

    Unlike ``on_gate_failed`` this is the *outcome* of a rollback that
    D10/D11 already drove. The L3 contract is to update the dashboard
    and (when the release-state row exists) advance the state machine
    to ``rolled_back`` if the current state is a canary stage. Out of
    order or unknown-release events are logged and reconciled later.
    """
    release_id = _release_id(event)
    if not release_id:
        return {"outcome": "ignored_no_release_id"}
    try:
        row = state_machine.get_by_release_id(release_id=release_id)
    except state_machine.ReleaseNotFound:
        return {"outcome": "release_not_found", "release_id": release_id}
    current = row["state"]
    if state_machine.STATE_ROLLED_BACK not in state_machine.ALLOWED_TRANSITIONS.get(
        current, frozenset()
    ):
        return {
            "outcome": "out_of_order",
            "release_id": release_id,
            "current": current,
        }
    state_machine.transition(
        release_id=release_id,
        from_state=current,
        to_state=state_machine.STATE_ROLLED_BACK,
        reason="canary.rolled_back",
    )
    return {
        "outcome": "rolled_back_recorded",
        "release_id": release_id,
        "from_state": current,
    }


def on_prod_orchestrator_placeholder(event: dict[str, Any]) -> dict[str, Any]:
    """ADR-0018 row 11 — placeholder until OP-881 (D9) ships.

    Returns a stable ``outcome="placeholder_not_yet_built"`` so the
    idempotent-replay cache pins the same answer every time.
    """
    logger.info(
        "release_conductor.prod_orchestrator.placeholder event=%s", event
    )
    return {
        "outcome": "placeholder_not_yet_built",
        "stage_id": str(event.get("stage_id") or ""),
    }

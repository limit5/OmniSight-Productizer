"""Cross-phase preflight: dependency_artifacts ↔ producing-phase.

A ticket whose ``dependency_artifacts`` lists ``X`` declares a runtime
dependency on artifact ``X``. By S12.G convention the producing ticket
lists ``X`` in one of its ``cross_phase_blockers[].blocker_artifact``
entries. This pure function walks a roster of TicketContractV1 instances
and emits one PreflightError per (ticket, artifact) pair where no OTHER
ticket in the roster produces the artifact.
"""

from __future__ import annotations

from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PreflightError:
    ticket_key: str
    missing_artifact: str
    detail: str
    rule_id: str = "dependency-orphan"


def check_dependency_artifacts(
    roster: list[TicketContractV1],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for ticket in roster:
        for artifact in ticket.dependency_artifacts:
            produced_elsewhere = any(
                blocker.blocker_artifact == artifact
                for other in roster
                if other.ticket_key != ticket.ticket_key
                for blocker in other.cross_phase_blockers
            )
            if not produced_elsewhere:
                errors.append(
                    PreflightError(
                        ticket_key=ticket.ticket_key,
                        missing_artifact=artifact,
                        detail=(
                            f"no other roster ticket produces '{artifact}' "
                            "via cross_phase_blockers[].blocker_artifact"
                        ),
                    )
                )
    return errors


__all__ = ["check_dependency_artifacts", "PreflightError"]

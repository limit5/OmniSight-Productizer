"""Cross-phase preflight: cross_phase_blockers shape validation.

`CrossPhaseBlocker` is structurally typed by pydantic; this preflight
enforces the semantics the schema cannot express — phase identifiers
from the canonical set, lowercase-kebab artifact ids, non-empty bounded
reason text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1

_PHASE_RE = re.compile(r"^31\.[A-K]$")
_GOVERNANCE_PHASES = frozenset({"G.A-v0", "G.A-v1", "G.B", "G.C", "G.D"})
_ARTIFACT_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REASON_MAX = 500


@dataclass(frozen=True)
class PreflightError:
    ticket_key: str
    field: str
    value: str
    detail: str
    rule_id: str = "cross-phase-blocker-shape"


def check_cross_phase_blocker_shape(
    roster: list[TicketContractV1],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for ticket in roster:
        for blocker in ticket.cross_phase_blockers:
            phase = blocker.blocker_phase
            if not (_PHASE_RE.match(phase) or phase in _GOVERNANCE_PHASES):
                errors.append(PreflightError(
                    ticket_key=ticket.ticket_key, field="blocker_phase",
                    value=phase,
                    detail="blocker_phase must match ^31\\.[A-K]$ or be in "
                           "{G.A-v0, G.A-v1, G.B, G.C, G.D}",
                ))
            if not _ARTIFACT_RE.match(blocker.blocker_artifact):
                errors.append(PreflightError(
                    ticket_key=ticket.ticket_key, field="blocker_artifact",
                    value=blocker.blocker_artifact,
                    detail="blocker_artifact must match ^[a-z][a-z0-9-]{0,62}$",
                ))
            reason = blocker.reason
            if not reason:
                errors.append(PreflightError(
                    ticket_key=ticket.ticket_key, field="reason", value=reason,
                    detail="reason must be non-empty",
                ))
            elif len(reason) > _REASON_MAX:
                errors.append(PreflightError(
                    ticket_key=ticket.ticket_key, field="reason", value=reason,
                    detail=f"reason exceeds {_REASON_MAX} chars (got {len(reason)})",
                ))
    return errors


__all__ = ["check_cross_phase_blocker_shape", "PreflightError"]

"""Cross-phase preflight: required_paths ↔ forbidden_paths reciprocity.

If ticket A declares ``required_paths: ["X"]`` it owns modification of X;
no OTHER ticket B may list X in its ``forbidden_paths`` (which would
declare "B refuses to touch X" — a contradiction with A's mandate).

The rule is asymmetric: required-by-A + forbidden-by-B is a collision,
but required-by-A + required-by-B is left to the mutex_with check
(G.A-v1-10) and is not flagged here.
"""

from __future__ import annotations

from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PreflightError:
    ticket_key_owner: str
    ticket_key_excluder: str
    path: str
    rule_id: str = "path-collision"


def check_path_reciprocity(
    roster: list[TicketContractV1],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for owner in roster:
        for path in owner.required_paths:
            for excluder in roster:
                if excluder.ticket_key == owner.ticket_key:
                    continue
                if path in excluder.forbidden_paths:
                    errors.append(
                        PreflightError(
                            ticket_key_owner=owner.ticket_key,
                            ticket_key_excluder=excluder.ticket_key,
                            path=path,
                        )
                    )
    return errors


__all__ = ["check_path_reciprocity", "PreflightError"]

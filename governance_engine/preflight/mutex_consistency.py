"""Cross-phase preflight: scope_components x mutex_with consistency.

If two non-meta tickets share a scope component, they are touching the
same subsystem and must declare each other in ``mutex_with`` so runner
pickup prevents concurrent conflicting work.
"""

from __future__ import annotations

from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PreflightError:
    ticket_key_a: str
    ticket_key_b: str
    shared_component: str
    rule_id: str = "mutex-asymmetric"


def check_mutex_consistency(
    roster: list[TicketContractV1],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    tickets = [ticket for ticket in roster if ticket.tag_type != "meta"]
    for left_index, left in enumerate(tickets):
        for right in tickets[left_index + 1:]:
            shared_components = set(left.scope_components) & set(right.scope_components)
            for component in sorted(shared_components):
                left_lists_right = right.ticket_key in left.mutex_with
                right_lists_left = left.ticket_key in right.mutex_with
                if left_lists_right and right_lists_left:
                    continue
                errors.append(
                    PreflightError(
                        ticket_key_a=left.ticket_key,
                        ticket_key_b=right.ticket_key,
                        shared_component=component,
                    )
                )
    return errors


__all__ = ["check_mutex_consistency", "PreflightError"]

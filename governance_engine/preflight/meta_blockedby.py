"""Cross-phase preflight: META claimed children vs blocked_by roster count."""

from __future__ import annotations

from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PreflightError:
    meta_key: str
    claimed: int
    actual: int
    rule_id: str = "meta-blockedby-count-mismatch"


def check_meta_blocked_by_completeness(
    roster: list[TicketContractV1],
    claimed_child_count: dict[str, int],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for ticket in roster:
        if ticket.tag_type != "meta":
            continue
        if ticket.ticket_key not in claimed_child_count:
            continue

        claimed = claimed_child_count[ticket.ticket_key]
        actual = sum(
            1 for child in roster if ticket.ticket_key in child.blocked_by
        )
        if actual != claimed:
            errors.append(
                PreflightError(
                    meta_key=ticket.ticket_key,
                    claimed=claimed,
                    actual=actual,
                )
            )
    return errors


__all__ = ["check_meta_blocked_by_completeness", "PreflightError"]

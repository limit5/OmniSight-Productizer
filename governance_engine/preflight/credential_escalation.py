from dataclasses import dataclass

from governance_engine.schema.v1 import TicketContractV1


@dataclass(frozen=True)
class PreflightError:
    ticket_key: str
    detail: str
    rule_id: str = "credential-material-not-l1-non-subscription"
    severity: str = "error"


def check_credential_material_requires_l1_non_subscription(
    roster: list[TicketContractV1],
) -> list[PreflightError]:
    errors: list[PreflightError] = []
    for ticket in roster:
        if ticket.external_payload_class != "credential-material":
            continue
        failed: list[str] = []
        if ticket.authority_required != "L1":
            failed.append("authority_required must be L1")
        if ticket.ticket_class.value.startswith("subscription-"):
            failed.append("class must not start with subscription-")
        if failed:
            errors.append(
                PreflightError(
                    ticket_key=ticket.ticket_key,
                    detail="; ".join(failed),
                )
            )
    return errors

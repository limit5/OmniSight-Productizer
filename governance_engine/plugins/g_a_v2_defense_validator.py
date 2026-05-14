"""G.A-v2 defense_contract validator plugin."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from pydantic import ValidationError

from governance_engine.plugins.base import BasePhasePlugin, PluginError
from governance_engine.schema.forbidden_combinations import (
    validate_forbidden_combinations,
)
from governance_engine.schema.v1 import TicketContractV1

BOOTSTRAP_TICKET_KEYS = frozenset(
    {
        "v2-A1",
        "v2-A2",
        "v2-A3",
        "v2-A4",
        "v2-A5",
        "v2-A6",
        "v2-A7",
    }
)


def _ticket_key(ticket: TicketContractV1 | dict[str, Any]) -> str:
    if isinstance(ticket, dict):
        return str(ticket.get("ticket_key", ""))
    return ticket.ticket_key


def _labels(ticket: TicketContractV1 | dict[str, Any]) -> list[str]:
    if isinstance(ticket, dict):
        labels = ticket.get("labels", [])
        return labels if isinstance(labels, list) else []
    return ticket.labels


def _is_bootstrap_ticket(ticket: TicketContractV1 | dict[str, Any]) -> bool:
    ticket_key = _ticket_key(ticket)
    return (
        ticket_key in BOOTSTRAP_TICKET_KEYS
        or "bootstrap:defense-contract" in _labels(ticket)
    )


def _defense_contract_missing(ticket: TicketContractV1 | dict[str, Any]) -> bool:
    if isinstance(ticket, dict):
        return ticket.get("defense_contract") is None
    return ticket.defense_contract is None


class GAV2DefenseValidatorPlugin(BasePhasePlugin):
    phase_id = "G.A-v2"
    schema_versions_supported = frozenset({"v1"})

    def __init__(
        self,
        *,
        audit_mode: bool = False,
        bootstrap_mode: bool = False,
    ) -> None:
        self.audit_mode = audit_mode
        self.bootstrap_mode = bootstrap_mode

    @property
    def ruleset_id(self) -> str:
        return "g-a-v2-defense-contract-rules-v1"

    def validate_payload(self, payload: dict[str, Any]) -> list[PluginError]:
        try:
            ticket = TicketContractV1.model_validate(payload)
        except ValidationError as exc:
            if self._allows_bootstrap_gap(payload):
                return [self._bootstrap_warning(payload)]
            return [
                PluginError(
                    phase_id=self.phase_id,
                    rule_id="defense-contract-shape",
                    severity="error",
                    detail=str(exc),
                )
            ]
        return self.validate(ticket)

    def validate(self, ticket: TicketContractV1) -> list[PluginError]:
        if _defense_contract_missing(ticket):
            if self._allows_bootstrap_gap(ticket):
                return [self._bootstrap_warning(ticket)]
            return [
                PluginError(
                    phase_id=self.phase_id,
                    rule_id="defense-contract-required",
                    severity="error",
                    detail="G.A-v2 tickets require defense_contract outside bootstrap/audit mode",
                )
            ]

        errors: list[PluginError] = []
        for error in validate_forbidden_combinations(ticket):
            if error.rule_id < 11:
                continue
            errors.append(
                PluginError(
                    phase_id=self.phase_id,
                    rule_id=error.rule_name,
                    severity="error",
                    detail=error.detail,
                )
            )
        return errors

    def dry_run_evidence_json(self, payload: dict[str, Any]) -> str:
        errors = self.validate_payload(payload)
        evidence = {
            "ruleset_id": self.ruleset_id,
            "audit_mode": self.audit_mode,
            "bootstrap_mode": self.bootstrap_mode,
            "ticket_key": _ticket_key(payload),
            "errors": [asdict(error) for error in errors],
        }
        return json.dumps(evidence, sort_keys=True)

    def _allows_bootstrap_gap(self, ticket: TicketContractV1 | dict[str, Any]) -> bool:
        return (self.bootstrap_mode or self.audit_mode) and _is_bootstrap_ticket(ticket)

    def _bootstrap_warning(self, ticket: TicketContractV1 | dict[str, Any]) -> PluginError:
        return PluginError(
            phase_id=self.phase_id,
            rule_id="defense-contract-bootstrap-gap",
            severity="warning",
            detail=(
                f"{_ticket_key(ticket)} is allowed to omit defense_contract "
                "while G.A-v2 bootstrap tickets v2-A1..A7 are being filed"
            ),
        )


__all__ = ["BOOTSTRAP_TICKET_KEYS", "GAV2DefenseValidatorPlugin"]

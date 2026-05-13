"""Pydantic v0 ticket contract schema for ADR-0033 §3 and S12.G v2 spec §5."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TicketClass(str, Enum):
    SUBSCRIPTION_CLAUDE = "subscription-claude"
    SUBSCRIPTION_CODEX = "subscription-codex"
    OPERATOR_WINDOW_TOP = "operator-window-top"
    OPERATOR_WINDOW_DEPUTY = "operator-window-deputy"
    OPERATOR_REHEARSAL = "operator-rehearsal"


class L2Reason(str, Enum):
    EXECUTION_ONLY = "execution-only"
    WITNESS_ONLY = "witness-only"
    APPROVAL_ONLY = "approval-only"
    CREDENTIAL_ENTRY = "credential-entry"
    PRODUCTION_TOUCH = "production-touch"
    RELEASE_GOVERNANCE = "release-governance"
    ROSTER_MUTATION = "roster-mutation"
    OVERRIDE_ACTION = "override-action"
    DESTRUCTIVE_ACTION = "destructive-action"


class TicketContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["v0"]
    ticket_class: TicketClass = Field(alias="class")
    loc_delta_max: int
    files_touched_max: int
    required_paths: list[str]
    forbidden_paths: list[str]
    non_goals: list[str]
    interface_contract: str | None
    test_scope: str
    destructive_op_classes: list[str]
    destructive_op_scope: Literal["none", "local", "staging", "production"]
    scope_components: list[str]
    external_side_effect: Literal["none", "read", "write", "irreversible"]
    external_payload_class: Literal["operational", "sensitive", "public"]
    runtime_capability: Literal["unit-only", "integration", "operator-witnessed"]
    dependency_artifacts: list[str]
    execution_mode: Literal["unit-testable", "integration", "operator-rehearsal"]
    mutex_with: list[str]
    evidence_class: Literal["unit", "integration", "operator", "audit-log"]
    on_scope_creep: Literal["file-followup", "abort", "escalate"]
    scope_summary_max_chars: int
    tag_type: Literal["story", "meta", "epic", "spike"]
    l1_exclusive_reason: str | None = None
    l2_reason: L2Reason | None = None
    authority_required: Literal["L1", "L2", "L3"]
    reversibility: Literal["reversible", "irreversible", "destructive"]
    environment_scope: Literal["local", "staging", "production"]
    external_systems: list[str]

    @model_validator(mode="after")
    def require_authority_reasons(self) -> TicketContract:
        if self.ticket_class is TicketClass.OPERATOR_WINDOW_TOP and self.l1_exclusive_reason is None:
            raise ValueError("l1_exclusive_reason is required for operator-window-top tickets")
        if self.ticket_class is TicketClass.OPERATOR_WINDOW_DEPUTY and self.l2_reason is None:
            raise ValueError("l2_reason is required for operator-window-deputy tickets")
        return self


__all__ = ["TicketContract", "TicketClass", "L2Reason"]

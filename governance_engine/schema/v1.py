"""Pydantic v1 ticket contract schema.

Extends v0 with G.A-v1 sprint spec §3.2 operational fields:
``ticket_key``, ``labels``, ``blocked_by``, and ``cross_phase_blockers``;
the structured blocker shape follows master S12.G v2 spec §0 lines 56-58.
Adds the master S12.G v2 spec §0 line 45 plugin schema lock via
``phase_plugin_version``; §0 line 63 ``credential-material`` payload
class; and §0 lines 64-65 evidence-class extensions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from governance_engine.schema.v0 import L2Reason, TicketClass, TicketContract
from governance_engine.schema.v1_validators import (
    EXTERNAL_ARTIFACT_NAME_PATTERN,
    L1_EXCLUSIVE_REASON_VALUES,
    validate_external_artifact_name,
    validate_l1_exclusive_reason_value,
    validate_required_path_shape,
)


class CrossPhaseBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocker_phase: str
    blocker_artifact: str
    reason: str


class TicketContractV1(TicketContract):
    schema_version: Literal["v0", "v1"]
    external_payload_class: Literal[
        "operational",
        "sensitive",
        "public",
        "credential-material",
    ]
    evidence_class: Literal[
        "unit",
        "integration",
        "operator",
        "audit-log",
        "structural",
        "behavioral-smoke",
        "semantic",
        "longitudinal",
    ]

    ticket_key: str = Field(pattern=r"^OP-[0-9]+$")
    labels: list[str]
    blocked_by: list[str] = Field(default_factory=list)
    cross_phase_blockers: list[CrossPhaseBlocker] = Field(default_factory=list)
    phase_plugin_version: Literal["v1"]

    @field_validator("required_paths")
    @classmethod
    def validate_required_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if not validate_required_path_shape(path):
                raise ValueError(
                    "required_paths entries must be POSIX-style relative paths "
                    f"(no '..', leading '/', or '\\\\'); offending value={path!r}"
                )
        return paths

    @field_validator("cross_phase_blockers")
    @classmethod
    def validate_cross_phase_blocker_artifacts(
        cls, blockers: list[CrossPhaseBlocker]
    ) -> list[CrossPhaseBlocker]:
        for blocker in blockers:
            if not validate_external_artifact_name(blocker.blocker_artifact):
                raise ValueError(
                    "cross_phase_blockers.blocker_artifact must match "
                    f"{EXTERNAL_ARTIFACT_NAME_PATTERN}; "
                    f"offending value={blocker.blocker_artifact!r}"
                )
        return blockers

    @field_validator("l1_exclusive_reason")
    @classmethod
    def validate_l1_exclusive_reason(cls, reason: str | None) -> str | None:
        if reason is None:
            return reason
        if not validate_l1_exclusive_reason_value(reason):
            allowed = ", ".join(sorted(L1_EXCLUSIVE_REASON_VALUES)) + ", <custom>"
            raise ValueError(
                "l1_exclusive_reason must be one of "
                f"{{{allowed}}}; offending value={reason!r}"
            )
        return reason


__all__ = ["TicketContractV1", "CrossPhaseBlocker", "TicketClass", "L2Reason"]

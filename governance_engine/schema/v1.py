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

from pydantic import BaseModel, ConfigDict, Field

from governance_engine.schema.v0 import L2Reason, TicketClass, TicketContract


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


__all__ = ["TicketContractV1", "CrossPhaseBlocker", "TicketClass", "L2Reason"]

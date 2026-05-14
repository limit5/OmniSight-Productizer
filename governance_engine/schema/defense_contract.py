"""Pydantic defense_contract schema for G.A-v2 spec §2 lines 60-100."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ErrorDetection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: Literal[
        "prometheus_metric",
        "structured_log",
        "exit_code",
        "endpoint_status",
        "none",
    ]
    channel: str | None
    detection_latency_p99: str | None
    observability_test: str
    alert_rule_id: str | None


class ExceptionHandling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enumerated_states: list[str]
    remediation_hint_contract: str
    user_facing: bool
    fail_loudly: bool


class ShutdownContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drain_seconds_p99: int | None
    cleanup_sequence: list[str]
    forced_termination_safe: bool
    state_persistence: str


class RecoveryPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_condition: str
    steps: list[str]
    idempotent: bool
    rto_seconds_p99: int
    evidence_file: str


class RescuePath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_condition: str
    operator_actions: list[str]
    authority_required: Literal["L1", "L2", "L3", "none"]
    audit_trail: str


class DefenseContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_detection: ErrorDetection
    exception_handling: ExceptionHandling
    shutdown_contract: ShutdownContract
    recovery_path: RecoveryPath
    rescue_path: RescuePath


__all__ = [
    "DefenseContract",
    "ErrorDetection",
    "ExceptionHandling",
    "ShutdownContract",
    "RecoveryPath",
    "RescuePath",
]

"""BP.D.6 - Compliance matrix auxiliary API response schema.

REST wrapper around ``backend.compliance_matrix``. The response model pins
``audit_type="advisory"`` and ``requires_human_signoff=True`` so every API
consumer receives the Phase D auxiliary disclaimer at the schema boundary.

SOP module-global state audit: qualified answer #1. The route registry is an
immutable tuple of callables imported from committed source; every worker
derives the same values at import time, with no cache, singleton, DB writer,
or read-after-write timing path.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend import auth as _au
from backend.compliance_matrix import automotive, industrial, medical, military
from backend.sandbox_tier import Guild, SandboxTier


AuditType = Literal["advisory"]
ComplianceMatrix = Literal["medical", "automotive", "industrial", "military"]

router = APIRouter(prefix="/compliance-matrix", tags=["compliance-matrix"])


class ComplianceMatrixCheckRequest(BaseModel):
    """Request body for one auxiliary compliance matrix check."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    compliance_matrix: ComplianceMatrix = Field(
        ...,
        description="Auxiliary compliance matrix to run.",
    )
    guild: Guild | None = Field(
        default=None,
        description="Optional Guild scope.",
    )
    tier: SandboxTier | None = Field(
        default=None,
        description="Optional sandbox tier scope.",
    )


class ComplianceMatrixResponse(BaseModel):
    """Schema returned by every BP.D compliance matrix API response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_type: AuditType = Field(
        default="advisory",
        description="Pinned Phase D auxiliary response type.",
    )
    requires_human_signoff: Literal[True] = Field(
        default=True,
        description="Pinned human-signoff requirement for all auxiliary claims.",
    )
    is_auxiliary_compliant: bool
    compliance_matrix: ComplianceMatrix
    disclaimer: str
    standards: list[str]
    claims: list[dict[str, Any]]
    gaps: list[str]


class ComplianceMatrixListResponse(BaseModel):
    """Schema returned by BP.D compliance matrix discovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_type: AuditType = Field(default="advisory")
    requires_human_signoff: Literal[True] = Field(default=True)
    items: list[dict[str, Any]]
    count: int


_MatrixRunner = Callable[[Guild | None, SandboxTier | None], object]

_MATRIX_RUNNERS: tuple[tuple[ComplianceMatrix, _MatrixRunner], ...] = (
    ("medical", medical._auxiliary_check_medical),
    ("automotive", automotive._auxiliary_check_automotive),
    ("industrial", industrial._auxiliary_check_industrial),
    ("military", military._auxiliary_check_military),
)


def _auxiliary_matrix_runner(matrix: ComplianceMatrix) -> _MatrixRunner:
    for name, runner in _MATRIX_RUNNERS:
        if name == matrix:
            return runner
    raise HTTPException(
        status_code=404,
        detail=f"Compliance matrix {matrix!r} not found",
    )


@router.get("/matrices", response_model=ComplianceMatrixListResponse)
async def list_matrices(
    _user=Depends(_au.require_operator),
) -> ComplianceMatrixListResponse:
    """List the BP.D auxiliary compliance matrices."""

    items = [
        {
            "compliance_matrix": name,
            "audit_type": "advisory",
            "requires_human_signoff": True,
        }
        for name, _runner in _MATRIX_RUNNERS
    ]
    return ComplianceMatrixListResponse(items=items, count=len(items))


@router.post("/check", response_model=ComplianceMatrixResponse)
async def check_matrix(
    req: ComplianceMatrixCheckRequest,
    _user=Depends(_au.require_operator),
) -> ComplianceMatrixResponse:
    """Run one BP.D auxiliary compliance matrix and return the pinned schema."""

    runner = _auxiliary_matrix_runner(req.compliance_matrix)
    result = runner(req.guild, req.tier)
    return ComplianceMatrixResponse.model_validate(result.to_dict())


__all__ = [
    "ComplianceMatrixCheckRequest",
    "ComplianceMatrixListResponse",
    "ComplianceMatrixResponse",
    "router",
]

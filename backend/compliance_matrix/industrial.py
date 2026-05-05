"""BP.D.3 - Industrial compliance matrix auxiliary check.

This is an auxiliary check tool. AI-assisted output MUST be reviewed by
a human certified engineer.

Maps the BP.S.3 sandbox-tier audit claims to the industrial-domain
standards named for Phase D:

* IEC 61508 - functional safety
* SIL 1-4 - safety integrity levels

Scope discipline
----------------
This module is advisory only. It does not certify a product, allocate a
SIL, validate IEC 61508 compliance, assert safety integrity, or replace
third-party legal / certified-engineer review. It only exposes the
engineering-level claims already documented in
``docs/design/sandbox-tier-audit.md`` for the industrial matrix.

SOP module-global state audit
-----------------------------
Qualified answer #1: all module-level state is immutable dataclass /
tuple data derived from the committed BP.S.3 audit doc plus
``backend.sandbox_tier`` constants. Every worker imports the same source
and reaches the same matrix; there is no singleton, cache, env knob, DB
writer, or read-after-write timing path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Literal

from backend.sandbox_tier import Guild, SandboxTier, is_admitted


AuditType = Literal["advisory"]
ComplianceMatrix = Literal["industrial"]

MODULE_DISCLAIMER = (
    "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
    "by a human certified engineer."
)
AUDIT_TYPE: AuditType = "advisory"
REQUIRES_HUMAN_SIGNOFF = True
COMPLIANCE_MATRIX: ComplianceMatrix = "industrial"


class IndustrialStandard(str, Enum):
    """Industrial-domain standards surfaced by BP.D.3."""

    iec61508 = "IEC 61508"
    sil_1_4 = "SIL 1-4"


@dataclass(frozen=True)
class IndustrialAuxiliaryClaim:
    """One advisory claim copied from ``sandbox-tier-audit.md`` BP.S.3."""

    claim_id: str
    standard: IndustrialStandard
    guild: Guild
    tiers: tuple[SandboxTier, ...]
    source: str
    summary: str
    human_review_note: str = MODULE_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "standard": self.standard.value,
            "guild": self.guild.value,
            "tiers": [tier.value for tier in self.tiers],
            "source": self.source,
            "summary": self.summary,
            "human_review_note": self.human_review_note,
        }


@dataclass(frozen=True)
class IndustrialAuxiliaryResult:
    """Schema returned by industrial auxiliary checks.

    ``audit_type`` and ``requires_human_signoff`` are pinned literals per
    Phase D's auxiliary-output contract.
    """

    is_auxiliary_compliant: bool
    claims: tuple[IndustrialAuxiliaryClaim, ...] = field(default_factory=tuple)
    gaps: tuple[str, ...] = field(default_factory=tuple)
    audit_type: AuditType = AUDIT_TYPE
    requires_human_signoff: Literal[True] = REQUIRES_HUMAN_SIGNOFF
    compliance_matrix: ComplianceMatrix = COMPLIANCE_MATRIX
    disclaimer: str = MODULE_DISCLAIMER

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_type": self.audit_type,
            "requires_human_signoff": self.requires_human_signoff,
            "is_auxiliary_compliant": self.is_auxiliary_compliant,
            "compliance_matrix": self.compliance_matrix,
            "disclaimer": self.disclaimer,
            "standards": [standard.value for standard in IndustrialStandard],
            "claims": [claim.to_dict() for claim in self.claims],
            "gaps": list(self.gaps),
        }


_SOURCE_SECTION = "docs/design/sandbox-tier-audit.md section 3"

_INDUSTRIAL_AUXILIARY_CLAIMS: tuple[IndustrialAuxiliaryClaim, ...] = (
    IndustrialAuxiliaryClaim(
        claim_id="industrial.bsp.iec61508.software_design_development",
        standard=IndustrialStandard.iec61508,
        guild=Guild.bsp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 bsp",
        summary=(
            "IEC 61508 Part 3 section 7.4: BSP software design and development "
            "evidence is limited to sandboxed compile reducing the "
            "toolchain-tampering attack surface."
        ),
    ),
    IndustrialAuxiliaryClaim(
        claim_id="industrial.bsp.sil_1_4.non_assignment",
        standard=IndustrialStandard.sil_1_4,
        guild=Guild.bsp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 bsp",
        summary=(
            "SIL 1-4 assignment is not asserted by sandboxing alone; BSP T1/T3 "
            "evidence is auxiliary input for human functional-safety review."
        ),
    ),
    IndustrialAuxiliaryClaim(
        claim_id="industrial.hal.iec61508.software_hardware_boundary",
        standard=IndustrialStandard.iec61508,
        guild=Guild.hal,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 hal",
        summary=(
            "IEC 61508 Part 3 section 7.4: HAL work follows the BSP T1/T3 "
            "sandbox boundary and adds hardware-software integration evidence "
            "through daemon-mediated read-back."
        ),
    ),
    IndustrialAuxiliaryClaim(
        claim_id="industrial.hal.sil_1_4.non_assignment",
        standard=IndustrialStandard.sil_1_4,
        guild=Guild.hal,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 hal",
        summary=(
            "SIL 1-4 integrity is not allocated by the HAL sandbox boundary; "
            "the isolated compile and T3 capture records are auxiliary evidence "
            "for certified-engineer review only."
        ),
    ),
)

_CLAIMS_BY_GUILD = MappingProxyType(
    {
        guild: tuple(
            claim for claim in _INDUSTRIAL_AUXILIARY_CLAIMS
            if claim.guild == guild
        )
        for guild in Guild
    }
)


def _coerce_guild(guild: Guild | str) -> Guild:
    if isinstance(guild, Guild):
        return guild
    return Guild(guild)


def _coerce_tier(tier: SandboxTier | str) -> SandboxTier:
    if isinstance(tier, SandboxTier):
        return tier
    return SandboxTier(tier)


def _filter_claims(
    claims: Iterable[IndustrialAuxiliaryClaim],
    tier: SandboxTier | None,
) -> tuple[IndustrialAuxiliaryClaim, ...]:
    if tier is None:
        return tuple(claims)
    return tuple(claim for claim in claims if tier in claim.tiers)


def _auxiliary_industrial_claims(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> tuple[IndustrialAuxiliaryClaim, ...]:
    """Return advisory industrial claims, optionally scoped by Guild/Tier."""

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    claims = (
        _CLAIMS_BY_GUILD[scoped_guild]
        if scoped_guild is not None
        else _INDUSTRIAL_AUXILIARY_CLAIMS
    )
    return _filter_claims(claims, scoped_tier)


def _auxiliary_check_industrial(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> IndustrialAuxiliaryResult:
    """Run the BP.D.3 industrial auxiliary check.

    With no arguments, returns the complete industrial matrix. With a
    Guild/Tier pair, first checks BP.S.1 admission and then returns only
    industrial-domain claims applicable to that cell.
    """

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    gaps: list[str] = []

    if scoped_guild is not None and scoped_tier is not None:
        if not is_admitted(scoped_guild, scoped_tier):
            gaps.append(
                f"Guild {scoped_guild.value!r} is not admitted to "
                f"{scoped_tier.value!r}; no industrial auxiliary claim may be made."
            )
            return IndustrialAuxiliaryResult(
                is_auxiliary_compliant=False,
                claims=(),
                gaps=tuple(gaps),
            )

    claims = _auxiliary_industrial_claims(scoped_guild, scoped_tier)
    if scoped_guild is not None and not claims:
        target = scoped_guild.value
        if scoped_tier is not None:
            target = f"{target}/{scoped_tier.value}"
        gaps.append(f"No IEC 61508 / SIL 1-4 claim is mapped for {target}.")

    return IndustrialAuxiliaryResult(
        is_auxiliary_compliant=bool(claims) and not gaps,
        claims=claims,
        gaps=tuple(gaps),
    )


__all__ = [
    "AUDIT_TYPE",
    "COMPLIANCE_MATRIX",
    "IndustrialAuxiliaryClaim",
    "IndustrialAuxiliaryResult",
    "IndustrialStandard",
    "MODULE_DISCLAIMER",
    "REQUIRES_HUMAN_SIGNOFF",
    "_auxiliary_check_industrial",
    "_auxiliary_industrial_claims",
]

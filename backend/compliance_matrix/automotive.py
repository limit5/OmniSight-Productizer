"""BP.D.2 - Automotive compliance matrix auxiliary check.

This is an auxiliary check tool. AI-assisted output MUST be reviewed by
a human certified engineer.

Maps the BP.S.3 sandbox-tier audit claims to the automotive-domain
standards named for Phase D:

* ISO 26262 - road vehicle functional safety
* MISRA C - automotive C coding rules
* AUTOSAR - automotive software architecture

Scope discipline
----------------
This module is advisory only. It does not certify a product, assign an
ASIL, validate MISRA conformance, assert AUTOSAR architecture compliance,
or replace third-party legal / certified-engineer review. It only exposes
the engineering-level claims already documented in
``docs/design/sandbox-tier-audit.md`` for the automotive matrix.

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
ComplianceMatrix = Literal["automotive"]

MODULE_DISCLAIMER = (
    "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
    "by a human certified engineer."
)
AUDIT_TYPE: AuditType = "advisory"
REQUIRES_HUMAN_SIGNOFF = True
COMPLIANCE_MATRIX: ComplianceMatrix = "automotive"


class AutomotiveStandard(str, Enum):
    """Automotive-domain standards surfaced by BP.D.2."""

    iso26262 = "ISO 26262"
    misra_c = "MISRA C"
    autosar = "AUTOSAR"


@dataclass(frozen=True)
class AutomotiveAuxiliaryClaim:
    """One advisory claim copied from ``sandbox-tier-audit.md`` BP.S.3."""

    claim_id: str
    standard: AutomotiveStandard
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
class AutomotiveAuxiliaryResult:
    """Schema returned by automotive auxiliary checks.

    ``audit_type`` and ``requires_human_signoff`` are pinned literals per
    Phase D's auxiliary-output contract.
    """

    is_auxiliary_compliant: bool
    claims: tuple[AutomotiveAuxiliaryClaim, ...] = field(default_factory=tuple)
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
            "standards": [standard.value for standard in AutomotiveStandard],
            "claims": [claim.to_dict() for claim in self.claims],
            "gaps": list(self.gaps),
        }


_SOURCE_SECTION = "docs/design/sandbox-tier-audit.md section 3"

_AUTOMOTIVE_AUXILIARY_CLAIMS: tuple[AutomotiveAuxiliaryClaim, ...] = (
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.architect.iso26262.project_management",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.architect,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 architect",
        summary=(
            "ISO 26262 Part 2 section 6: architecture and project-management "
            "artefacts are produced inside the T0/T2 boundary; no "
            "implementation-safety claim."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.sa_sd.iso26262.software_architecture",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.sa_sd,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 sa_sd",
        summary=(
            "ISO 26262 Part 6 section 7: software architectural design evidence "
            "is bounded to T0/T2 design work, not verification."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.reporter.iso26262.confirmation_measures",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.reporter,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 reporter",
        summary=(
            "ISO 26262 Part 2 section 6.4.7: reporter output may assemble "
            "confirmation-measure reports, with human signoff as the "
            "load-bearing signature."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.bsp.iso26262.unit_verification_boundary",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.bsp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 bsp",
        summary=(
            "ISO 26262 Part 6 section 10: BSP unit-verification evidence is "
            "limited to the sandboxing property; verification content still "
            "requires human review."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.bsp.misra_c.static_lint_boundary",
        standard=AutomotiveStandard.misra_c,
        guild=Guild.bsp,
        tiers=(SandboxTier.T1,),
        source=f"{_SOURCE_SECTION}.2 bsp",
        summary=(
            "MISRA C checks run inside T1 via checkpatch / lint pass; this "
            "records the isolated analysis boundary, not certified "
            "rule-by-rule conformance."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.bsp.autosar.architecture_non_assertion",
        standard=AutomotiveStandard.autosar,
        guild=Guild.bsp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 bsp",
        summary=(
            "AUTOSAR architecture compliance is not asserted by sandboxing "
            "alone; BSP T1/T3 evidence is auxiliary input for human "
            "architecture review."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.hal.iso26262.hardware_software_boundary",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.hal,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 hal",
        summary=(
            "ISO 26262 Part 5: HAL sits at the software-hardware boundary; "
            "sandboxed compile plus T3 read-back can support integration "
            "evidence only."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.hal.misra_c.static_lint_boundary",
        standard=AutomotiveStandard.misra_c,
        guild=Guild.hal,
        tiers=(SandboxTier.T1,),
        source=f"{_SOURCE_SECTION}.2 hal",
        summary=(
            "HAL C glue can be checked inside T1 using the same isolated "
            "checkpatch / lint boundary cited for BSP work."
        ),
    ),
    AutomotiveAuxiliaryClaim(
        claim_id="automotive.qa.iso26262.verification_evidence",
        standard=AutomotiveStandard.iso26262,
        guild=Guild.qa,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.3 qa",
        summary=(
            "ISO 26262 Part 8 section 9: QA produces verification evidence; "
            "sandboxing supports reproducibility of that evidence."
        ),
    ),
)

_CLAIMS_BY_GUILD = MappingProxyType(
    {
        guild: tuple(
            claim for claim in _AUTOMOTIVE_AUXILIARY_CLAIMS
            if claim.guild == guild
        )
        for guild in Guild
    }
)


def _auxiliary_coerce_guild(guild: Guild | str) -> Guild:
    if isinstance(guild, Guild):
        return guild
    return Guild(guild)


def _auxiliary_coerce_tier(tier: SandboxTier | str) -> SandboxTier:
    if isinstance(tier, SandboxTier):
        return tier
    return SandboxTier(tier)


def _auxiliary_filter_claims(
    claims: Iterable[AutomotiveAuxiliaryClaim],
    tier: SandboxTier | None,
) -> tuple[AutomotiveAuxiliaryClaim, ...]:
    if tier is None:
        return tuple(claims)
    return tuple(claim for claim in claims if tier in claim.tiers)


def _auxiliary_automotive_claims(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> tuple[AutomotiveAuxiliaryClaim, ...]:
    """Return advisory automotive claims, optionally scoped by Guild/Tier."""

    scoped_guild = _auxiliary_coerce_guild(guild) if guild is not None else None
    scoped_tier = _auxiliary_coerce_tier(tier) if tier is not None else None
    claims = (
        _CLAIMS_BY_GUILD[scoped_guild]
        if scoped_guild is not None
        else _AUTOMOTIVE_AUXILIARY_CLAIMS
    )
    return _auxiliary_filter_claims(claims, scoped_tier)


def _auxiliary_check_automotive(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> AutomotiveAuxiliaryResult:
    """Run the BP.D.2 automotive auxiliary check.

    With no arguments, returns the complete automotive matrix. With a
    Guild/Tier pair, first checks BP.S.1 admission and then returns only
    automotive-domain claims applicable to that cell.
    """

    scoped_guild = _auxiliary_coerce_guild(guild) if guild is not None else None
    scoped_tier = _auxiliary_coerce_tier(tier) if tier is not None else None
    gaps: list[str] = []

    if scoped_guild is not None and scoped_tier is not None:
        if not is_admitted(scoped_guild, scoped_tier):
            gaps.append(
                f"Guild {scoped_guild.value!r} is not admitted to "
                f"{scoped_tier.value!r}; no automotive auxiliary claim may be made."
            )
            return AutomotiveAuxiliaryResult(
                is_auxiliary_compliant=False,
                claims=(),
                gaps=tuple(gaps),
            )

    claims = _auxiliary_automotive_claims(scoped_guild, scoped_tier)
    if scoped_guild is not None and not claims:
        target = scoped_guild.value
        if scoped_tier is not None:
            target = f"{target}/{scoped_tier.value}"
        gaps.append(f"No ISO 26262 / MISRA C / AUTOSAR claim is mapped for {target}.")

    return AutomotiveAuxiliaryResult(
        is_auxiliary_compliant=bool(claims) and not gaps,
        claims=claims,
        gaps=tuple(gaps),
    )


__all__ = [
    "AUDIT_TYPE",
    "AutomotiveAuxiliaryClaim",
    "AutomotiveAuxiliaryResult",
    "AutomotiveStandard",
    "COMPLIANCE_MATRIX",
    "MODULE_DISCLAIMER",
    "REQUIRES_HUMAN_SIGNOFF",
    "_auxiliary_automotive_claims",
    "_auxiliary_check_automotive",
]

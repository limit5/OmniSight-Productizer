"""BP.D.1 - Medical compliance matrix auxiliary check.

This is an auxiliary check tool. AI-assisted output MUST be reviewed by
a human certified engineer.

Maps the BP.S.3 sandbox-tier audit claims to the medical-domain
standards named for Phase D:

* IEC 62304 - medical device software lifecycle
* ISO 13485 - medical-device quality management system
* HIPAA - protected health information handling

Scope discipline
----------------
This module is advisory only. It does not certify a product, validate a
device, classify medical software safety class, or replace third-party
legal / certified-engineer review. It only exposes the engineering-level
claims already documented in ``docs/design/sandbox-tier-audit.md`` for
the medical matrix.

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
ComplianceMatrix = Literal["medical"]

MODULE_DISCLAIMER = (
    "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
    "by a human certified engineer."
)
AUDIT_TYPE: AuditType = "advisory"
REQUIRES_HUMAN_SIGNOFF = True
COMPLIANCE_MATRIX: ComplianceMatrix = "medical"


class MedicalStandard(str, Enum):
    """Medical-domain standards surfaced by BP.D.1."""

    iec62304 = "IEC 62304"
    iso13485 = "ISO 13485"
    hipaa = "HIPAA"


@dataclass(frozen=True)
class MedicalAuxiliaryClaim:
    """One advisory claim copied from ``sandbox-tier-audit.md`` BP.S.3."""

    claim_id: str
    standard: MedicalStandard
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
class MedicalAuxiliaryResult:
    """Schema returned by medical auxiliary checks.

    ``audit_type`` and ``requires_human_signoff`` are pinned literals per
    Phase D's auxiliary-output contract.
    """

    is_auxiliary_compliant: bool
    claims: tuple[MedicalAuxiliaryClaim, ...] = field(default_factory=tuple)
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
            "standards": [standard.value for standard in MedicalStandard],
            "claims": [claim.to_dict() for claim in self.claims],
            "gaps": list(self.gaps),
        }


_SOURCE_SECTION = "docs/design/sandbox-tier-audit.md section 3"

_MEDICAL_AUXILIARY_CLAIMS: tuple[MedicalAuxiliaryClaim, ...] = (
    MedicalAuxiliaryClaim(
        claim_id="medical.architect.iec62304.planning",
        standard=MedicalStandard.iec62304,
        guild=Guild.architect,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 architect",
        summary=(
            "IEC 62304 section 5.1 planning: design-phase artefacts are produced "
            "inside the T0/T2 boundary; no implementation-safety claim."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.sa_sd.iec62304.requirements_architecture",
        standard=MedicalStandard.iec62304,
        guild=Guild.sa_sd,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 sa_sd",
        summary=(
            "IEC 62304 sections 5.2 and 5.3: requirements analysis and software "
            "architectural design are bounded to T0/T2 design work."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.ux.iso13485.design_inputs",
        standard=MedicalStandard.iso13485,
        guild=Guild.ux,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 ux",
        summary="ISO 13485 section 7.3.3: UX research artefacts map to design inputs.",
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.ux.hipaa.workstation_device",
        standard=MedicalStandard.hipaa,
        guild=Guild.ux,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 ux",
        summary=(
            "HIPAA section 164.310(d): UX has no T2 admission, limiting PHI "
            "exfiltration through the UX Guild path."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.pm.iso13485.design_controls",
        standard=MedicalStandard.iso13485,
        guild=Guild.pm,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 pm",
        summary="ISO 13485 section 7.3: PM work maps to design controls.",
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.auditor.iec62304.configuration_management",
        standard=MedicalStandard.iec62304,
        guild=Guild.auditor,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 auditor",
        summary=(
            "IEC 62304 section 8: the auditor reads the append-only audit trail "
            "from T0 and does not execute payloads."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.auditor.hipaa.audit_controls",
        standard=MedicalStandard.hipaa,
        guild=Guild.auditor,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 auditor",
        summary=(
            "HIPAA section 164.312(b): T0-only auditor access supports audit "
            "controls without granting payload-execution tiers."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.isp.iso13485.production_service",
        standard=MedicalStandard.iso13485,
        guild=Guild.isp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 isp",
        summary=(
            "ISO 13485 section 7.5: medical-imaging ISP work receives sandboxed "
            "compile plus hardware-daemon mediated capture evidence."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.isp.iec62304.unit_implementation",
        standard=MedicalStandard.iec62304,
        guild=Guild.isp,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 isp",
        summary=(
            "IEC 62304 section 5.5: ISP unit implementation evidence is "
            "reproducible inside the T1/T3 execution boundary."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.audio.iec62304.integration_testing",
        standard=MedicalStandard.iec62304,
        guild=Guild.audio,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 audio",
        summary=(
            "IEC 62304 section 5.6: audio DSP integration and test-vector "
            "regression evidence is isolated; functional correctness "
            "still requires human review."
        ),
    ),
    MedicalAuxiliaryClaim(
        claim_id="medical.optical.iso13485.design_inputs",
        standard=MedicalStandard.iso13485,
        guild=Guild.optical,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.3 optical",
        summary="ISO 13485 section 7.3.3: optical specifications map to design inputs.",
    ),
)

_CLAIMS_BY_GUILD = MappingProxyType(
    {
        guild: tuple(
            claim for claim in _MEDICAL_AUXILIARY_CLAIMS
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
    claims: Iterable[MedicalAuxiliaryClaim],
    tier: SandboxTier | None,
) -> tuple[MedicalAuxiliaryClaim, ...]:
    if tier is None:
        return tuple(claims)
    return tuple(claim for claim in claims if tier in claim.tiers)


def _auxiliary_medical_claims(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> tuple[MedicalAuxiliaryClaim, ...]:
    """Return advisory medical claims, optionally scoped by Guild/Tier."""

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    claims = (
        _CLAIMS_BY_GUILD[scoped_guild]
        if scoped_guild is not None
        else _MEDICAL_AUXILIARY_CLAIMS
    )
    return _filter_claims(claims, scoped_tier)


def _auxiliary_check_medical(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> MedicalAuxiliaryResult:
    """Run the BP.D.1 medical auxiliary check.

    With no arguments, returns the complete medical matrix. With a
    Guild/Tier pair, first checks BP.S.1 admission and then returns only
    medical-domain claims applicable to that cell.
    """

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    gaps: list[str] = []

    if scoped_guild is not None and scoped_tier is not None:
        if not is_admitted(scoped_guild, scoped_tier):
            gaps.append(
                f"Guild {scoped_guild.value!r} is not admitted to "
                f"{scoped_tier.value!r}; no medical auxiliary claim may be made."
            )
            return MedicalAuxiliaryResult(
                is_auxiliary_compliant=False,
                claims=(),
                gaps=tuple(gaps),
            )

    claims = _auxiliary_medical_claims(scoped_guild, scoped_tier)
    if scoped_guild is not None and not claims:
        target = scoped_guild.value
        if scoped_tier is not None:
            target = f"{target}/{scoped_tier.value}"
        gaps.append(f"No IEC 62304 / ISO 13485 / HIPAA claim is mapped for {target}.")

    return MedicalAuxiliaryResult(
        is_auxiliary_compliant=bool(claims) and not gaps,
        claims=claims,
        gaps=tuple(gaps),
    )


__all__ = [
    "AUDIT_TYPE",
    "COMPLIANCE_MATRIX",
    "MODULE_DISCLAIMER",
    "MedicalAuxiliaryClaim",
    "MedicalAuxiliaryResult",
    "MedicalStandard",
    "REQUIRES_HUMAN_SIGNOFF",
    "_auxiliary_check_medical",
    "_auxiliary_medical_claims",
]

"""BP.D.4 - Military compliance matrix auxiliary check.

This is an auxiliary check tool. AI-assisted output MUST be reviewed by
a human certified engineer.

Maps the BP.S.3 sandbox-tier audit claims to the military / aerospace
standards named for Phase D:

* DO-178C - airborne software lifecycle and verification evidence
* MIL-STD-882E - system safety and software hazard analysis

Scope discipline
----------------
This module is advisory only. It does not certify a product, assign a
DAL, validate DO-178C compliance, assert system-safety acceptance, or
replace third-party legal / certified-engineer review. It only exposes
the engineering-level claims already documented in
``docs/design/sandbox-tier-audit.md`` for the military matrix.

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
ComplianceMatrix = Literal["military"]

MODULE_DISCLAIMER = (
    "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
    "by a human certified engineer."
)
AUDIT_TYPE: AuditType = "advisory"
REQUIRES_HUMAN_SIGNOFF = True
COMPLIANCE_MATRIX: ComplianceMatrix = "military"


class MilitaryStandard(str, Enum):
    """Military / aerospace standards surfaced by BP.D.4."""

    do178c = "DO-178C"
    mil_std_882e = "MIL-STD-882E"


@dataclass(frozen=True)
class MilitaryAuxiliaryClaim:
    """One advisory claim copied from ``sandbox-tier-audit.md`` BP.S.3."""

    claim_id: str
    standard: MilitaryStandard
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
class MilitaryAuxiliaryResult:
    """Schema returned by military auxiliary checks.

    ``audit_type`` and ``requires_human_signoff`` are pinned literals per
    Phase D's auxiliary-output contract.
    """

    is_auxiliary_compliant: bool
    claims: tuple[MilitaryAuxiliaryClaim, ...] = field(default_factory=tuple)
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
            "standards": [standard.value for standard in MilitaryStandard],
            "claims": [claim.to_dict() for claim in self.claims],
            "gaps": list(self.gaps),
        }


_SOURCE_SECTION = "docs/design/sandbox-tier-audit.md section 3"

_MILITARY_AUXILIARY_CLAIMS: tuple[MilitaryAuxiliaryClaim, ...] = (
    MilitaryAuxiliaryClaim(
        claim_id="military.pm.do178c.software_planning",
        standard=MilitaryStandard.do178c,
        guild=Guild.pm,
        tiers=(SandboxTier.T0,),
        source=f"{_SOURCE_SECTION}.1 pm",
        summary=(
            "DO-178C section 4.2: PM requirements and planning artefacts are "
            "produced inside the T0 boundary; no implementation-safety claim."
        ),
    ),
    MilitaryAuxiliaryClaim(
        claim_id="military.reporter.do178c.software_accomplishment_summary",
        standard=MilitaryStandard.do178c,
        guild=Guild.reporter,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.1 reporter",
        summary=(
            "DO-178C section 11.20: reporter output may assemble software "
            "accomplishment summaries, with human signoff as the load-bearing "
            "signature."
        ),
    ),
    MilitaryAuxiliaryClaim(
        claim_id="military.algo_cv.do178c.verification_of_verification",
        standard=MilitaryStandard.do178c,
        guild=Guild.algo_cv,
        tiers=(SandboxTier.T1, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.2 algo_cv",
        summary=(
            "DO-178C Annex A Table A-7: numerical regression evidence is "
            "produced in the T1/T3 boundary as auxiliary verification evidence."
        ),
    ),
    MilitaryAuxiliaryClaim(
        claim_id="military.algo_cv.mil_std_882e.software_hazard_analysis",
        standard=MilitaryStandard.mil_std_882e,
        guild=Guild.algo_cv,
        tiers=(SandboxTier.T1,),
        source=f"{_SOURCE_SECTION}.2 algo_cv",
        summary=(
            "MIL-STD-882E section 4.4: T1 Cgroup controls partially reduce "
            "memory-corruption-as-denial hazards; algorithm-correctness hazards "
            "remain outside the auxiliary claim."
        ),
    ),
    MilitaryAuxiliaryClaim(
        claim_id="military.qa.do178c.verification_evidence",
        standard=MilitaryStandard.do178c,
        guild=Guild.qa,
        tiers=(SandboxTier.T0, SandboxTier.T2),
        source=f"{_SOURCE_SECTION}.3 qa",
        summary=(
            "DO-178C Annex A Table A-6: QA produces verification evidence; "
            "sandboxing supports reproducibility of that evidence."
        ),
    ),
    MilitaryAuxiliaryClaim(
        claim_id="military.forensics.do178c.configuration_management",
        standard=MilitaryStandard.do178c,
        guild=Guild.forensics,
        tiers=(SandboxTier.T0, SandboxTier.T1, SandboxTier.T2, SandboxTier.T3),
        source=f"{_SOURCE_SECTION}.4 forensics",
        summary=(
            "DO-178C section 10: forensic evidence preservation is supported "
            "by read-mostly access across T0/T1/T2/T3 for audit-chain, "
            "container, and hardware-daemon records."
        ),
    ),
)

_CLAIMS_BY_GUILD = MappingProxyType(
    {
        guild: tuple(
            claim for claim in _MILITARY_AUXILIARY_CLAIMS
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
    claims: Iterable[MilitaryAuxiliaryClaim],
    tier: SandboxTier | None,
) -> tuple[MilitaryAuxiliaryClaim, ...]:
    if tier is None:
        return tuple(claims)
    return tuple(claim for claim in claims if tier in claim.tiers)


def _auxiliary_military_claims(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> tuple[MilitaryAuxiliaryClaim, ...]:
    """Return advisory military claims, optionally scoped by Guild/Tier."""

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    claims = (
        _CLAIMS_BY_GUILD[scoped_guild]
        if scoped_guild is not None
        else _MILITARY_AUXILIARY_CLAIMS
    )
    return _filter_claims(claims, scoped_tier)


def _auxiliary_check_military(
    guild: Guild | str | None = None,
    tier: SandboxTier | str | None = None,
) -> MilitaryAuxiliaryResult:
    """Run the BP.D.4 military auxiliary check.

    With no arguments, returns the complete military matrix. With a
    Guild/Tier pair, first checks BP.S.1 admission and then returns only
    military-domain claims applicable to that cell.
    """

    scoped_guild = _coerce_guild(guild) if guild is not None else None
    scoped_tier = _coerce_tier(tier) if tier is not None else None
    gaps: list[str] = []

    if scoped_guild is not None and scoped_tier is not None:
        if not is_admitted(scoped_guild, scoped_tier):
            gaps.append(
                f"Guild {scoped_guild.value!r} is not admitted to "
                f"{scoped_tier.value!r}; no military auxiliary claim may be made."
            )
            return MilitaryAuxiliaryResult(
                is_auxiliary_compliant=False,
                claims=(),
                gaps=tuple(gaps),
            )

    claims = _auxiliary_military_claims(scoped_guild, scoped_tier)
    if scoped_guild is not None and not claims:
        target = scoped_guild.value
        if scoped_tier is not None:
            target = f"{target}/{scoped_tier.value}"
        gaps.append(f"No DO-178C / MIL-STD-882E claim is mapped for {target}.")

    return MilitaryAuxiliaryResult(
        is_auxiliary_compliant=bool(claims) and not gaps,
        claims=claims,
        gaps=tuple(gaps),
    )


__all__ = [
    "AUDIT_TYPE",
    "COMPLIANCE_MATRIX",
    "MODULE_DISCLAIMER",
    "MilitaryAuxiliaryClaim",
    "MilitaryAuxiliaryResult",
    "MilitaryStandard",
    "REQUIRES_HUMAN_SIGNOFF",
    "_auxiliary_check_military",
    "_auxiliary_military_claims",
]

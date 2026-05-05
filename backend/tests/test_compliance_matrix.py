"""BP.D.9 contract tests for the four auxiliary compliance matrices."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from backend.compliance_matrix import automotive, industrial, medical, military
from backend.sandbox_tier import Guild, SandboxTier, is_admitted


@dataclass(frozen=True)
class MatrixCase:
    name: str
    module: ModuleType
    standards: tuple[str, ...]
    claim_count: int
    supported_guild: Guild
    supported_tier: SandboxTier
    supported_claim_ids: tuple[str, ...]
    no_claim_gap: str
    denied_gap: str


MATRIX_CASES = (
    MatrixCase(
        name="medical",
        module=medical,
        standards=("IEC 62304", "ISO 13485", "HIPAA"),
        claim_count=11,
        supported_guild=Guild.architect,
        supported_tier=SandboxTier.T0,
        supported_claim_ids=("medical.architect.iec62304.planning",),
        no_claim_gap="No IEC 62304 / ISO 13485 / HIPAA claim is mapped",
        denied_gap="no medical auxiliary claim may be made",
    ),
    MatrixCase(
        name="automotive",
        module=automotive,
        standards=("ISO 26262", "MISRA C", "AUTOSAR"),
        claim_count=9,
        supported_guild=Guild.bsp,
        supported_tier=SandboxTier.T1,
        supported_claim_ids=(
            "automotive.bsp.iso26262.unit_verification_boundary",
            "automotive.bsp.misra_c.static_lint_boundary",
            "automotive.bsp.autosar.architecture_non_assertion",
        ),
        no_claim_gap="No ISO 26262 / MISRA C / AUTOSAR claim is mapped",
        denied_gap="no automotive auxiliary claim may be made",
    ),
    MatrixCase(
        name="industrial",
        module=industrial,
        standards=("IEC 61508", "SIL 1-4"),
        claim_count=4,
        supported_guild=Guild.bsp,
        supported_tier=SandboxTier.T1,
        supported_claim_ids=(
            "industrial.bsp.iec61508.software_design_development",
            "industrial.bsp.sil_1_4.non_assignment",
        ),
        no_claim_gap="No IEC 61508 / SIL 1-4 claim is mapped",
        denied_gap="no industrial auxiliary claim may be made",
    ),
    MatrixCase(
        name="military",
        module=military,
        standards=("DO-178C", "MIL-STD-882E"),
        claim_count=6,
        supported_guild=Guild.forensics,
        supported_tier=SandboxTier.T3,
        supported_claim_ids=(
            "military.forensics.do178c.configuration_management",
        ),
        no_claim_gap="No DO-178C / MIL-STD-882E claim is mapped",
        denied_gap="no military auxiliary claim may be made",
    ),
)


def _claims_func(case: MatrixCase) -> Any:
    return getattr(case.module, f"_auxiliary_{case.name}_claims")


def _check_func(case: MatrixCase) -> Any:
    return getattr(case.module, f"_auxiliary_check_{case.name}")


def _claim_ids(claims: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(claim.claim_id for claim in claims)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_matrix_constants_are_pinned(case: MatrixCase) -> None:
    assert case.module.AUDIT_TYPE == "advisory"
    assert case.module.REQUIRES_HUMAN_SIGNOFF is True
    assert case.module.COMPLIANCE_MATRIX == case.name
    assert "human certified engineer" in case.module.MODULE_DISCLAIMER


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_complete_check_returns_advisory_result(case: MatrixCase) -> None:
    result = _check_func(case)()

    assert result.audit_type == "advisory"
    assert result.requires_human_signoff is True
    assert result.compliance_matrix == case.name
    assert result.is_auxiliary_compliant is True


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_result_dict_preserves_auxiliary_envelope(case: MatrixCase) -> None:
    data = _check_func(case)().to_dict()

    assert data["audit_type"] == "advisory"
    assert data["requires_human_signoff"] is True
    assert data["compliance_matrix"] == case.name
    assert data["disclaimer"] == case.module.MODULE_DISCLAIMER


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_result_dict_lists_expected_standards(case: MatrixCase) -> None:
    data = _check_func(case)().to_dict()

    assert tuple(data["standards"]) == case.standards


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_complete_claim_set_has_expected_size(case: MatrixCase) -> None:
    claims = _claims_func(case)()

    assert len(claims) == case.claim_count


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_ids_are_unique_and_matrix_scoped(case: MatrixCase) -> None:
    claim_ids = _claim_ids(_claims_func(case)())

    assert len(claim_ids) == len(set(claim_ids))
    assert all(claim_id.startswith(f"{case.name}.") for claim_id in claim_ids)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_dicts_expose_required_fields(case: MatrixCase) -> None:
    for claim in _claims_func(case)():
        data = claim.to_dict()
        assert set(data) == {
            "claim_id",
            "standard",
            "guild",
            "tiers",
            "source",
            "summary",
            "human_review_note",
        }


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_dicts_repeat_human_review_disclaimer(case: MatrixCase) -> None:
    for claim in _claims_func(case)():
        data = claim.to_dict()
        assert data["human_review_note"] == case.module.MODULE_DISCLAIMER


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_sources_cite_sandbox_tier_audit_doc(case: MatrixCase) -> None:
    for claim in _claims_func(case)():
        assert claim.source.startswith("docs/design/sandbox-tier-audit.md section 3")


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claims_keep_enum_backed_fields(case: MatrixCase) -> None:
    for claim in _claims_func(case)():
        assert isinstance(claim.guild, Guild)
        assert claim.standard.value in case.standards
        assert all(isinstance(tier, SandboxTier) for tier in claim.tiers)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_tiers_are_admitted_for_claim_guild(case: MatrixCase) -> None:
    for claim in _claims_func(case)():
        assert all(is_admitted(claim.guild, tier) for tier in claim.tiers)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_filter_accepts_enum_guild(case: MatrixCase) -> None:
    claims = _claims_func(case)(case.supported_guild)

    assert set(_claim_ids(claims)).issuperset(case.supported_claim_ids)
    assert all(claim.guild == case.supported_guild for claim in claims)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_filter_accepts_string_guild(case: MatrixCase) -> None:
    enum_claims = _claims_func(case)(case.supported_guild)
    string_claims = _claims_func(case)(case.supported_guild.value)

    assert string_claims == enum_claims


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_filter_accepts_enum_tier(case: MatrixCase) -> None:
    claims = _claims_func(case)(tier=case.supported_tier)

    assert set(_claim_ids(claims)).issuperset(case.supported_claim_ids)
    assert all(case.supported_tier in claim.tiers for claim in claims)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_claim_filter_intersects_guild_and_tier(case: MatrixCase) -> None:
    claims = _claims_func(case)(case.supported_guild, case.supported_tier)

    assert _claim_ids(claims) == case.supported_claim_ids


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_scoped_check_accepts_supported_enum_cell(case: MatrixCase) -> None:
    result = _check_func(case)(case.supported_guild, case.supported_tier)

    assert result.is_auxiliary_compliant is True
    assert _claim_ids(result.claims) == case.supported_claim_ids
    assert result.gaps == ()


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_scoped_check_accepts_supported_string_cell(case: MatrixCase) -> None:
    result = _check_func(case)(
        case.supported_guild.value,
        case.supported_tier.value,
    )

    assert result.is_auxiliary_compliant is True
    assert _claim_ids(result.claims) == case.supported_claim_ids
    assert result.gaps == ()


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_admitted_cell_without_claim_reports_gap(case: MatrixCase) -> None:
    result = _check_func(case)(Guild.backend, SandboxTier.T0)

    assert result.is_auxiliary_compliant is False
    assert result.claims == ()
    assert result.gaps == (f"{case.no_claim_gap} for backend/T0.",)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_denied_cell_reports_admission_gap(case: MatrixCase) -> None:
    result = _check_func(case)(Guild.auditor, SandboxTier.T1)

    assert result.is_auxiliary_compliant is False
    assert result.claims == ()
    assert result.gaps == (
        f"Guild 'auditor' is not admitted to 'T1'; {case.denied_gap}.",
    )


@pytest.mark.parametrize("case", MATRIX_CASES, ids=lambda case: case.name)
def test_invalid_guild_or_tier_is_rejected(case: MatrixCase) -> None:
    with pytest.raises(ValueError):
        _claims_func(case)("not-a-guild")

    with pytest.raises(ValueError):
        _claims_func(case)(tier="T9")

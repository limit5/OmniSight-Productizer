"""KS.4.7 - SOC 2 Type II readiness checklist contract tests.

The deliverable is operational documentation, not runtime code. These
tests pin the control mapping, evidence collection, GRC platform
evaluation, third-party auditor evaluation, and N10 ledger evidence
shape for SOC 2 Type II readiness.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = PROJECT_ROOT / "docs" / "ops" / "soc2_type2_readiness_checklist.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.4.7 file: {path}"
    return path.read_text(encoding="utf-8")


def test_soc2_checklist_exists_in_docs_ops() -> None:
    assert CHECKLIST.is_file()
    assert CHECKLIST.parent == PROJECT_ROOT / "docs" / "ops"


def test_soc2_checklist_maps_trust_services_criteria_to_controls() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "aicpa 2017 trust services criteria",
        "revised points of focus - 2022",
        "control matrix",
        "tsc id",
        "point of focus",
        "omnisight control id",
        "security - common criteria",
        "availability",
        "confidentiality",
        "processing integrity",
        "privacy",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"soc2 checklist missing control mapping: {missing}"


def test_soc2_checklist_defines_evidence_collection_and_exceptions() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "evidence family",
        "governance",
        "access",
        "change management",
        "vulnerability management",
        "incident response",
        "backup / recovery",
        "private security evidence vault",
        "sha-256 fingerprints",
        "exception ticket",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"soc2 checklist missing evidence collection: {missing}"


def test_soc2_checklist_evaluates_grc_platforms() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "grc platform evaluation",
        "vanta",
        "drata",
        "secureframe",
        "automated evidence collection",
        "auditor access",
        "read-only cloud, identity, code, ticketing, and device evidence",
        "the platform is not the auditor",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"soc2 checklist missing GRC evaluation: {missing}"


def test_soc2_checklist_defines_third_party_auditor_evaluation() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "third-party auditor evaluation",
        "independent cpa firm",
        "auditor independence",
        "saas / ai experience",
        "scope alignment",
        "observation period",
        "shortlist at least two independent cpa firms",
        "engagement letter",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"soc2 checklist missing auditor evaluation: {missing}"


def test_soc2_checklist_requires_readiness_gates_and_n10_row() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "readiness gates",
        "do not start the type ii observation window",
        "evidence owners and collection cadence",
        "exception tracker",
        "soc 2 readiness",
        "evidence-index-sha256",
        "ready-for-observation",
        "correction ->",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"soc2 checklist missing readiness gate terms: {missing}"


def test_n10_ledger_has_soc2_readiness_table() -> None:
    body = _read(LEDGER)
    assert "## SOC 2 Readiness" in body
    assert (
        "| Quarter | GRC platform | Auditor | Criteria | Observation window "
        "(UTC) | Evidence index SHA-256 | Disposition | Notes |"
    ) in body
    assert "soc2_type2_readiness_checklist.md" in body


def test_n10_ledger_documents_soc2_append_only_governance() -> None:
    body = _read(LEDGER).lower()
    required = [
        "every soc 2 type ii readiness milestone",
        "control matrix",
        "evidence index",
        "independent cpa firm",
        "soc 2 readiness rows are append-only",
        "correction ->",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"N10 ledger missing SOC 2 governance terms: {missing}"


def test_readme_links_soc2_checklist_from_n10_section() -> None:
    body = _read(README)
    assert "soc2_type2_readiness_checklist.md" in body
    assert "SOC 2 Type II" in body
    assert "control mapping" in body

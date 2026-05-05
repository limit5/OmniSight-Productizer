"""KS.DOD.Cross-cutting -- evidence index drift guard.

The deliverable is operational evidence documentation, not new runtime
code. This mirrors the KS Phase 3 GA evidence test: source artifacts
remain the source of truth, and this guard fails when the cross-cutting
DoD index drifts away from the R46-R50 mitigations, incident response,
pentest, or SOC 2 readiness artifacts.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = PROJECT_ROOT / "docs" / "ops" / "ks_cross_cutting_evidence.md"
ADR = PROJECT_ROOT / "docs" / "security" / "ks-multi-tenant-secret-management.md"
README = PROJECT_ROOT / "README.md"
INCIDENT_RUNBOOK = PROJECT_ROOT / "docs" / "security" / "incident-response-runbook.md"
PENTEST_SOP = PROJECT_ROOT / "docs" / "ops" / "quarterly_pentest_sop.md"
SOC2_CHECKLIST = PROJECT_ROOT / "docs" / "ops" / "soc2_type2_readiness_checklist.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing KS cross-cutting evidence file: {path}"
    return path.read_text(encoding="utf-8")


def _normalized_lower(path: Path) -> str:
    return " ".join(_read(path).lower().split())


def test_cross_cutting_evidence_doc_exists_and_defines_scope() -> None:
    body = _normalized_lower(EVIDENCE)

    required = [
        "ks cross-cutting evidence index",
        "r46-r50 mitigation evidence",
        "incident response runbook ship evidence",
        "first external pentest gate evidence",
        "soc 2 type ii readiness checklist evidence",
        "current status is `dev-only`",
        "does not cover phase 1 envelope",
        "does not cover phase 2 cmek",
        "does not cover phase 3 byog proxy ga",
    ]

    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"KS cross-cutting evidence doc missing scope terms: {missing}"


def test_r46_r50_evidence_matrix_links_to_mitigation_artifacts() -> None:
    body = _read(EVIDENCE)

    for risk_id in ["R46", "R47", "R48", "R49", "R50"]:
        assert risk_id in body

    for phrase in [
        "backend/security/envelope.py",
        "backend/security/kms_adapters.py",
        "backend/security/decryption_audit.py",
        "docs/ops/priority_i_multi_tenancy_readiness.md",
        "backend/security/cmek_revoke_detector.py",
        "backend/security/cmek_graceful_degrade.py",
        "docs/ops/cmek_revoke_recovery.md",
        "omnisight-proxy/internal/auth/auth_test.go",
        "backend/tests/test_byog_proxy_fail_fast.py",
        "backend/audit.py",
        "pg_advisory_xact_lock",
    ]:
        assert phrase in body

    adr = _read(ADR)
    assert "## 10. Risk Register（R46-R50）" in adr
    assert "| **R46**" in adr
    assert "| **R50**" in adr


def test_incident_response_runbook_is_linked_and_pinned() -> None:
    doc = _read(EVIDENCE)
    runbook = _normalized_lower(INCIDENT_RUNBOOK)

    assert "docs/security/incident-response-runbook.md" in doc
    for phrase in [
        "first 24 hours",
        "detect and declare",
        "contain",
        "rotate and verify",
        "customer notification decision",
        "forensics and recovery",
        "blameless postmortem",
    ]:
        assert phrase in runbook


def test_external_pentest_gate_is_honest_about_operational_completion() -> None:
    doc = _normalized_lower(EVIDENCE)
    sop = _normalized_lower(PENTEST_SOP)
    ledger = _read(LEDGER)

    for phrase in [
        "repository evidence deliberately does not claim",
        "signed vendor msa / sow",
        "production-equivalent staging",
        "critical / high findings are fixed and retested",
        "ledger row disposition is `closed` or `risk-accepted`",
    ]:
        assert phrase in doc

    assert "external penetration test vendor" in sop
    assert "one assessment per calendar quarter" in sop
    assert "## Pentest Reports" in ledger
    assert "Findings C/H/M/L" in ledger


def test_soc2_readiness_checklist_is_linked_and_pinned() -> None:
    doc = _read(EVIDENCE)
    checklist = _normalized_lower(SOC2_CHECKLIST)
    ledger = _read(LEDGER)

    assert "docs/ops/soc2_type2_readiness_checklist.md" in doc
    for phrase in [
        "aicpa 2017 trust services criteria",
        "control matrix",
        "evidence collection",
        "exception tracker",
        "vanta",
        "drata",
        "secureframe",
        "independent cpa firm",
    ]:
        assert phrase in checklist

    assert "## SOC 2 Readiness" in ledger
    assert "Evidence index SHA-256" in ledger


def test_readme_and_adr_link_cross_cutting_evidence_index() -> None:
    readme = _read(README)
    adr = _read(ADR)

    assert "ks_cross_cutting_evidence.md" in readme
    assert "KS cross-cutting evidence index" in readme
    assert "ks_cross_cutting_evidence.md" in adr

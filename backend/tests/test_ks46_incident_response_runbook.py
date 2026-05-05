"""KS.4.6 - incident response runbook contract tests.

The deliverable is operational documentation, not runtime code. These
tests pin the first-24-hour incident response SOP so the runbook cannot
silently drift away from detect, contain, rotate, notify, forensics, and
blameless postmortem coverage.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "security" / "incident-response-runbook.md"
EVIDENCE = PROJECT_ROOT / "docs" / "ops" / "ks_cross_cutting_evidence.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.4.6 file: {path}"
    return path.read_text(encoding="utf-8")


def test_incident_response_runbook_exists_in_docs_security() -> None:
    assert RUNBOOK.is_file()
    assert RUNBOOK.parent == PROJECT_ROOT / "docs" / "security"


def test_incident_response_runbook_covers_first_24_hour_sop() -> None:
    body = _read(RUNBOOK).lower()
    required = [
        "first 24 hours",
        "0-15 minutes - detect and declare",
        "15-60 minutes - contain",
        "1-4 hours - rotate and verify",
        "4-8 hours - notify customer decision",
        "8-24 hours - forensics and recovery",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"incident-response-runbook.md missing timeline: {missing}"


def test_incident_response_runbook_defines_ship_contract() -> None:
    body = _read(RUNBOOK)
    ledger = _read(LEDGER)
    ledger_normalized = " ".join(ledger.split())
    evidence = _read(EVIDENCE)

    for phrase in [
        "## 0. Ship contract",
        "backend/tests/test_ks46_incident_response_runbook.py",
        "docs/ops/ks_cross_cutting_evidence.md",
        "Incident Response Drills",
        "deployed-active",
        "tabletop drill",
    ]:
        assert phrase in body

    for phrase in [
        "## Incident Response Drills",
        "Evidence SHA-256",
        "Postmortem SHA-256",
        "Disposition = passed",
    ]:
        assert phrase in ledger

    assert "raw incident artefacts" in ledger_normalized
    assert "Incident Response Drills" in evidence


def test_incident_response_runbook_defines_roles_and_severity() -> None:
    body = _read(RUNBOOK).lower()
    required = [
        "incident commander",
        "engineering owner",
        "forensics owner",
        "communications owner",
        "sev-1",
        "sev-2",
        "sev-3",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"incident-response runbook missing roles/severity: {missing}"


def test_incident_response_runbook_requires_containment_and_rotation() -> None:
    body = _read(RUNBOOK).lower()
    required = [
        "leaked api key",
        "cross-tenant access",
        "compromised account",
        "malicious integration",
        "host or container compromise",
        "rotate every credential",
        "old key returns unauthorized",
        "old credentials",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"incident-response runbook missing containment/rotation: {missing}"


def test_incident_response_runbook_requires_customer_notification_and_forensics() -> None:
    body = _read(RUNBOOK).lower()
    required = [
        "customer notification is required",
        "impacted tenants",
        "approved customer channels",
        "private security evidence vault",
        "sha-256 fingerprints",
        "raw customer data",
        "plaintext credentials",
        "exploit payloads",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"incident-response runbook missing notify/forensics: {missing}"


def test_incident_response_runbook_requires_blameless_postmortem() -> None:
    body = _read(RUNBOOK).lower()
    required = [
        "blameless postmortem",
        "within 5 business days",
        "root cause",
        "what worked",
        "what failed",
        "corrective actions",
        "must not name individuals as root cause",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"incident-response runbook missing postmortem: {missing}"

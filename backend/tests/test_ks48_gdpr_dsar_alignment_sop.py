"""KS.4.8 - GDPR / DSAR alignment SOP contract tests.

The deliverable is operational documentation, not runtime code. These
tests pin the tenant deletion purge contract, DEK destruction evidence,
audit metadata redaction posture, DSAR export workflow, and N10 ledger
evidence shape.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOP = PROJECT_ROOT / "docs" / "ops" / "gdpr_dsar_alignment_sop.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.4.8 file: {path}"
    return path.read_text(encoding="utf-8")


def test_gdpr_dsar_sop_exists_in_docs_ops() -> None:
    assert SOP.is_file()
    assert SOP.parent == PROJECT_ROOT / "docs" / "ops"


def test_gdpr_dsar_sop_defines_tenant_deletion_and_dek_purge() -> None:
    body = _read(SOP).lower()
    required = [
        "tenant deletion purge contract",
        "delete tenant-scoped business rows and filesystem roots",
        "purge tenant deks",
        "deleting data rows while leaving wrapped deks intact is not a completed tenant deletion",
        "sampled decrypt",
        "expecting failure",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"gdpr dsar SOP missing tenant purge terms: {missing}"


def test_gdpr_dsar_sop_keeps_audit_metadata_but_deletes_raw_payloads() -> None:
    body = _read(SOP).lower()
    required = [
        "audit metadata retention",
        "hash-chain fields",
        "retain `hash`, `prev_hash`",
        "delete or replace with a redaction marker",
        "before_json",
        "after_json",
        "raw customer content",
        "direct pii",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"gdpr dsar SOP missing audit redaction terms: {missing}"


def test_gdpr_dsar_sop_defines_dsar_export_workflow() -> None:
    body = _read(SOP).lower()
    required = [
        "dsar export workflow",
        "article 15",
        "article 20",
        "whitelist-shaped export",
        "exclude raw secrets",
        "plaintext tokens",
        "dek material",
        "compute sha-256 over the stored export",
        "secure channel",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"gdpr dsar SOP missing export workflow: {missing}"


def test_gdpr_dsar_sop_defines_erasure_and_subprocessor_receipts() -> None:
    body = _read(SOP).lower()
    required = [
        "dsar erasure workflow",
        "article 17",
        "legal hold",
        "tenant-wide erasure",
        "audit raw payloads",
        "subprocessor",
        "provider receipt hash",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"gdpr dsar SOP missing erasure workflow: {missing}"


def test_gdpr_dsar_sop_requires_n10_evidence_row() -> None:
    body = _read(SOP).lower()
    required = [
        "dsar evidence",
        "export-sha256-or-none",
        "evidence-sha256",
        "dek purge count",
        "audit redaction count",
        "do not edit previous rows",
        "correction ->",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"gdpr dsar SOP missing N10 evidence terms: {missing}"


def test_n10_ledger_has_dsar_evidence_table() -> None:
    body = _read(LEDGER)
    assert "## DSAR Evidence" in body
    assert (
        "| Completed (UTC) | Request ID | Request type | Subject scope | "
        "Export SHA-256 | Evidence SHA-256 | DEKs purged | Audit rows redacted | "
        "Disposition | Notes |"
    ) in body
    assert "gdpr_dsar_alignment_sop.md" in body


def test_n10_ledger_documents_dsar_append_only_governance() -> None:
    body = _read(LEDGER).lower()
    required = [
        "every gdpr / dsar request lifecycle change",
        "tenant deletion evidence",
        "dek purge proof",
        "audit redaction summary",
        "dsar evidence rows are append-only",
        "raw exports",
        "wrapped dek material",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"N10 ledger missing DSAR governance terms: {missing}"


def test_readme_links_gdpr_dsar_sop_from_n10_section() -> None:
    body = _read(README)
    assert "gdpr_dsar_alignment_sop.md" in body
    assert "GDPR / DSAR" in body
    assert "purge DEK" in body

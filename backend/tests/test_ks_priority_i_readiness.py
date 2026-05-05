"""KS.DOD - Priority I multi-tenancy readiness checklist contract tests.

The deliverable is operational documentation, not runtime code. These
tests pin the pre-start gate that lets Priority I consume KS Phase 1
without reintroducing single-Fernet or unaudited multi-tenant secret
paths.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKLIST = PROJECT_ROOT / "docs" / "ops" / "priority_i_multi_tenancy_readiness.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
ADR = PROJECT_ROOT / "docs" / "security" / "ks-multi-tenant-secret-management.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.DOD readiness file: {path}"
    return path.read_text(encoding="utf-8")


def test_priority_i_readiness_checklist_exists_in_docs_ops() -> None:
    assert CHECKLIST.is_file()
    assert CHECKLIST.parent == PROJECT_ROOT / "docs" / "ops"


def test_priority_i_checklist_pins_ks1_phase1_evidence_packet() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "ks.1 acceptance test transcript",
        "test_ks113_envelope_security_integration.py",
        "test_security_kms_adapters.py",
        "test_decryption_audit.py",
        "test_spend_anomaly.py",
        "test_security_secret_filter.py",
        "test_backup_dlp_scan.py",
        "aws kms",
        "gcp kms",
        "vault transit",
        "localfernet",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"Priority I checklist missing KS.1 evidence: {missing}"


def test_priority_i_checklist_blocks_single_fernet_and_unaudited_decrypts() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "legacy fernet deprecation evidence",
        "single-fernet reads and rollback writes remain deprecated",
        "no writer can create a single-fernet carrier",
        "legacy reads fail closed",
        'audit_log.action = "ks.decryption"',
        "every plaintext recovery path emits `ks.decryption`",
        "source guard and runtime smoke",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"Priority I checklist missing secret safety gates: {missing}"


def test_priority_i_checklist_requires_production_image_env_and_24h_observation() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "ci-green is not enough",
        "production image evidence",
        "backend image digest",
        "runtime env evidence",
        "omnisight_ks_envelope_enabled",
        "omnisight_redis_url",
        "24h observation evidence",
        "no legacy-fernet writes",
        "no unaudited decrypts",
        "no scrubber/dlp bypasses",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"Priority I checklist missing prod readiness gates: {missing}"


def test_priority_i_checklist_requires_multi_tenant_isolation_and_no_fallback() -> None:
    body = _read(CHECKLIST).lower()
    required = [
        "multi-tenant isolation smoke evidence",
        "two tenants can write, read, rotate, and delete their own secrets",
        "cross-tenant reads return authorization failure",
        "rollback/no-fallback evidence",
        "disabling them must not disable tier 1 envelope writes",
        "tenant isolation smoke",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"Priority I checklist missing tenant isolation gates: {missing}"


def test_n10_ledger_has_priority_i_readiness_table() -> None:
    body = _read(LEDGER)
    assert "## Priority I Readiness" in body
    assert (
        "| Signed off (UTC) | Commit | Backend image digest | KS.1 evidence "
        "SHA-256 | KMS evidence SHA-256 | Tenant smoke SHA-256 | Observation "
        "window | Disposition | Notes |"
    ) in body
    assert "priority_i_multi_tenancy_readiness.md" in body
    assert "ready-to-start" in body


def test_adr_points_priority_i_gate_at_checklist() -> None:
    body = _read(ADR).lower()
    required = [
        "priority_i_multi_tenancy_readiness.md",
        "priority i readiness gate",
        "ready-to-start",
        "phase 1 完工後、priority i 啟動前",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"KS ADR missing Priority I readiness linkage: {missing}"


def test_readme_links_priority_i_readiness_from_n10_section() -> None:
    body = _read(README)
    assert "priority_i_multi_tenancy_readiness.md" in body
    assert "Priority I" in body
    assert "ready-to-start" in body

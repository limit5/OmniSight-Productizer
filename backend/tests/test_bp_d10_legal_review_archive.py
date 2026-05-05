"""BP.D.10 contract tests for the legal-review archive."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = PROJECT_ROOT / "docs" / "compliance" / "legal-review"
README = ARCHIVE_DIR / "README.md"
REPORT = ARCHIVE_DIR / "2026-05-06-bp-d10-legal-review-report.md"
MANIFEST = ARCHIVE_DIR / "code-sync-manifest.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing BP.D.10 archive file: {path}"
    return path.read_text(encoding="utf-8")


def test_legal_review_archive_directory_exists() -> None:
    assert ARCHIVE_DIR.is_dir()
    assert README.is_file()
    assert REPORT.is_file()
    assert MANIFEST.is_file()


def test_legal_review_report_keeps_auxiliary_disclaimer() -> None:
    body = _read(REPORT)

    required = [
        "audit_type=\"advisory\"",
        "requires_human_signoff=true",
        "does **not** claim",
        "not certification language",
        "private evidence vault",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"legal-review report missing: {missing}"


def test_legal_review_report_covers_bp_d_surfaces() -> None:
    body = _read(REPORT)

    required = [
        "BP.D.1",
        "BP.D.2",
        "BP.D.3",
        "BP.D.4",
        "BP.D.5",
        "BP.D.6",
        "BP.D.7",
        "BP.D.8",
        "BP.D.9",
        "backend/compliance_matrix/medical.py",
        "backend/compliance_matrix/automotive.py",
        "backend/compliance_matrix/industrial.py",
        "backend/compliance_matrix/military.py",
        "backend/routers/compliance_matrix.py",
        "configs/skills/compliance-audit/SKILL.md",
        "docs/security/r12-gvisor-cost-weight-only.md",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"legal-review report missing BP.D surface: {missing}"


def test_code_sync_manifest_lists_runtime_skill_test_and_source_inputs() -> None:
    body = _read(MANIFEST)

    required = [
        "backend/compliance_matrix/medical.py",
        "backend/compliance_matrix/automotive.py",
        "backend/compliance_matrix/industrial.py",
        "backend/compliance_matrix/military.py",
        "backend/routers/compliance_matrix.py",
        "backend/pep_gateway.py",
        "configs/skills/compliance-audit/tasks.yaml",
        "backend/tests/test_compliance_matrix.py",
        "backend/tests/test_compliance_audit_skill.py",
        "backend/tests/test_pep_gateway.py",
        "docs/design/sandbox-tier-audit.md",
        "docs/design/pep-gateway-tier-policy.md",
        "docs/security/r12-gvisor-cost-weight-only.md",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"code sync manifest missing: {missing}"


def test_archive_readme_declares_update_rule() -> None:
    body = _read(README)

    assert "BP.D.10" in body
    assert "code-sync-manifest.md" in body
    assert "requires_human_signoff=true" in body
    assert "If any file listed in `code-sync-manifest.md` changes" in body

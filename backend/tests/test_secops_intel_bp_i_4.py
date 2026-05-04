"""BP.I.4 -- SecOps Intel / Renovate / Secret Scanning overlap guards.

This is intentionally not the full BP.I.5 ``test_secops_intel.py``
matrix. It only guards the ownership contract that prevents BP.I from
duplicating N2 Renovate or S2-8 GitHub Secret Scanning.
"""

from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
OVERLAP_DOC = REPO_ROOT / "docs" / "ops" / "secops_intel_overlap.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_bp_i_4_overlap_doc_exists_and_names_three_surfaces():
    assert OVERLAP_DOC.is_file(), (
        "BP.I.4 requires docs/ops/secops_intel_overlap.md as the "
        "single ownership contract for BP.I, N2, and S2-8 overlap"
    )
    body = _read(OVERLAP_DOC)
    for phrase in (
        "BP.I SecOps Intel",
        "N2 Renovate",
        "S2-8 GitHub Secret Scanning",
    ):
        assert phrase in body, f"overlap doc missing surface: {phrase!r}"


def test_bp_i_4_dependency_cves_stay_owned_by_renovate():
    body = _one_line(_read(OVERLAP_DOC))
    for phrase in (
        "remediation belongs to N2 Renovate",
        "Renovate vulnerability fast-path",
        "must not open a competing fix PR",
        "regenerate lockfiles",
    ):
        assert phrase in body, f"dependency CVE ownership drifted: {phrase!r}"


def test_bp_i_4_secret_scanning_does_not_store_secret_values():
    body = _one_line(_read(OVERLAP_DOC))
    assert "never quotes" in body
    assert "secret value" in body
    for phrase in (
        "GitHub Secret Scanning is the push-time backstop",
        "does not persist the leaked value",
        "revoke and rotate",
    ):
        assert phrase in body, f"secret-scanning ownership drifted: {phrase!r}"


def test_bp_i_4_cross_links_from_renovate_readme_and_security_role():
    expected = "docs/ops/secops_intel_overlap.md"
    assert expected in _read(REPO_ROOT / "README.md")
    assert "secops_intel_overlap.md" in _read(
        REPO_ROOT / "docs" / "ops" / "renovate_policy.md"
    )
    assert expected in _read(
        REPO_ROOT / "configs" / "roles" / "security-engineer.md"
    )

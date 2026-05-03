"""KS.4.5 - bug bounty program evaluation SOP contract tests.

The deliverable is operational documentation, not runtime code. These
tests pin the HackerOne / Bugcrowd comparison, post-GA launch gates,
payout policy, scope, triage SOP, and N10 ledger evidence shape.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOP = PROJECT_ROOT / "docs" / "ops" / "bug_bounty_program_sop.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required KS.4.5 file: {path}"
    return path.read_text(encoding="utf-8")


def test_bug_bounty_sop_exists_in_docs_ops() -> None:
    assert SOP.is_file()
    assert SOP.parent == PROJECT_ROOT / "docs" / "ops"


def test_bug_bounty_sop_compares_hackerone_and_bugcrowd() -> None:
    body = _read(SOP).lower()
    required = [
        "hackerone",
        "bugcrowd",
        "hackerone bounty",
        "managed bug bounty",
        "hackerone response",
        "bugcrowd vdp",
        "provider comparison",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"bug_bounty_program_sop.md missing comparison: {missing}"


def test_bug_bounty_sop_requires_post_ga_private_launch_gates() -> None:
    body = _read(SOP).lower()
    required = [
        "post-ga only",
        "private / invite-only managed bug bounty",
        "public launch gate",
        "30 calendar days",
        "ga release is complete",
        "n10 ledger has a `planned` row",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"bug bounty SOP missing launch gate terms: {missing}"


def test_bug_bounty_sop_defines_scope_and_exclusions() -> None:
    body = _read(SOP).lower()
    required = [
        "tenant isolation",
        "authentication",
        "agent invocation paths",
        "file upload",
        "out of scope",
        "denial of service",
        "test_assets",
        "production customer data",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"bug bounty SOP missing scope terms: {missing}"


def test_bug_bounty_sop_defines_payout_policy_and_triage_sla() -> None:
    body = _read(SOP).lower()
    required = [
        "initial range (usd)",
        "critical | 3,000 - 10,000",
        "quarterly reward pool",
        "managed triage",
        "intake acknowledgement",
        "validity and scope decision",
        "critical containment decision",
        "researcher update",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"bug bounty SOP missing payout/triage terms: {missing}"


def test_bug_bounty_sop_requires_n10_program_and_finding_rows() -> None:
    body = _read(SOP).lower()
    required = [
        "bug bounty programs",
        "bug bounty findings",
        "scope-sha256",
        "finding-sha256",
        "do not edit previous rows",
        "correction ->",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"bug bounty SOP missing N10 evidence contract: {missing}"


def test_n10_ledger_has_bug_bounty_tables() -> None:
    body = _read(LEDGER)
    assert "## Bug Bounty Programs" in body
    assert "## Bug Bounty Findings" in body
    assert (
        "| Quarter | Provider | Mode | Disposition | Reward pool USD | "
        "Scope SHA-256 | Remediation tracker | Notes |"
    ) in body
    assert (
        "| Validated (UTC) | Platform finding ID | Severity | Finding SHA-256 | "
        "Remediation ticket | Bounty USD | Disposition | Notes |"
    ) in body
    assert "bug_bounty_program_sop.md" in body


def test_n10_ledger_documents_bug_bounty_append_only_governance() -> None:
    body = _read(LEDGER).lower()
    required = [
        "every bug bounty program lifecycle change",
        "private security evidence vault",
        "researcher pii",
        "managed triage",
        "program rows are append-only",
        "finding rows are append-only",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"N10 ledger missing bug bounty governance terms: {missing}"


def test_readme_links_bug_bounty_sop_from_n10_section() -> None:
    body = _read(README)
    assert "bug_bounty_program_sop.md" in body
    assert "HackerOne / Bugcrowd" in body
    assert "post-GA bug bounty" in body

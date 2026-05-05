"""WP.7.6 -- quarterly feature flag review SOP contract tests.

The deliverable is operational documentation, not runtime toggle code.
These tests pin the quarterly review cadence, N10 ledger evidence shape,
and long-untouched flag alert policy.
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOP = PROJECT_ROOT / "docs" / "ops" / "feature_flag_review_sop.md"
LEDGER = PROJECT_ROOT / "docs" / "ops" / "upgrade_rollback_ledger.md"
README = PROJECT_ROOT / "README.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required WP.7.6 file: {path}"
    return path.read_text(encoding="utf-8")


def test_feature_flag_review_sop_exists_in_docs_ops() -> None:
    assert SOP.is_file()
    assert SOP.parent == PROJECT_ROOT / "docs" / "ops"


def test_feature_flag_review_sop_defines_quarterly_cadence() -> None:
    body = _read(SOP).lower()
    required = [
        "first working week of every quarter",
        "feature_flags",
        "audit_log",
        "entity_kind=\"feature_flag\"",
        "flag owner",
        "expires_at",
        "review disposition",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"feature flag SOP missing cadence terms: {missing}"


def test_feature_flag_review_sop_defines_stale_alert_thresholds() -> None:
    body = _read(SOP).lower()
    required = [
        "long-untouched flag alert",
        "90 calendar days",
        "180 calendar days",
        "no audit_log mutation",
        "owner acknowledgement",
        "stale feature flag alerts",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"feature flag SOP missing stale alert terms: {missing}"


def test_feature_flag_review_sop_requires_n10_review_and_alert_rows() -> None:
    body = _read(SOP).lower()
    required = [
        "feature flag quarterly reviews",
        "stale feature flag alerts",
        "review-sha256",
        "alert-sha256",
        "do not edit previous rows",
        "correction ->",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"feature flag SOP missing N10 evidence contract: {missing}"


def test_n10_ledger_has_feature_flag_review_tables() -> None:
    body = _read(LEDGER)
    assert "## Feature Flag Quarterly Reviews" in body
    assert "## Stale Feature Flag Alerts" in body
    assert (
        "| Quarter | Reviewed (UTC) | Registry snapshot SHA-256 | "
        "Review SHA-256 | Flags reviewed | Stale alerts | Disposition | Notes |"
    ) in body
    assert (
        "| Alerted (UTC) | Flag name | Tier | Owner | Last mutation (UTC) | "
        "Age days | Alert SHA-256 | Disposition | Notes |"
    ) in body
    assert "feature_flag_review_sop.md" in body


def test_n10_ledger_documents_feature_flag_append_only_governance() -> None:
    body = _read(LEDGER).lower()
    required = [
        "every quarterly feature flag review",
        "stale flag alert",
        "review rows are append-only",
        "alert rows are append-only",
        "no customer data",
    ]
    missing = [phrase for phrase in required if phrase not in body]
    assert not missing, f"N10 ledger missing feature flag governance: {missing}"


def test_readme_links_feature_flag_review_sop_from_n10_section() -> None:
    body = _read(README)
    assert "feature_flag_review_sop.md" in body
    assert "quarterly feature flag review" in body
    assert "long-untouched flag alert" in body

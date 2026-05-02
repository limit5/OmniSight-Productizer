"""FS.4.5 -- Email domain authentication runbook contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "ops" / "email_domain_authentication.md"


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK.exists(), f"FS.4.4 runbook missing at {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


class TestEmailDomainAuthenticationRunbook:

    def test_runbook_path_is_canonical(self):
        assert RUNBOOK.relative_to(PROJECT_ROOT).as_posix() == (
            "docs/ops/email_domain_authentication.md"
        )

    def test_runbook_has_required_sections_in_order(self, runbook_text):
        sections = [
            "## 1. Naming Policy",
            "## 2. Baseline DMARC",
            "## 3. Resend",
            "## 4. Postmark",
            "## 5. AWS SES",
            "## 6. Cutover Checklist",
            "## 7. Rollback",
        ]

        positions = [runbook_text.find(section) for section in sections]

        assert all(pos >= 0 for pos in positions)
        assert positions == sorted(positions)

    @pytest.mark.parametrize(
        "heading,required_terms",
        [
            ("## 3. Resend", ("DKIM", "SPF / return-path", "Restart verification")),
            ("## 4. Postmark", ("DKIM", "Custom Return-Path", "DMARC")),
            ("## 5. AWS SES", ("Easy DKIM", "Custom MAIL FROM", "Region-scoped")),
        ],
    )
    def test_provider_sections_cover_dns_and_verification(
        self,
        runbook_text,
        heading,
        required_terms,
    ):
        start = runbook_text.find(heading)
        assert start >= 0, f"missing provider section {heading}"
        next_heading = runbook_text.find("\n## ", start + len(heading))
        section = runbook_text[start:] if next_heading < 0 else runbook_text[start:next_heading]

        for term in required_terms:
            assert term in section
        assert "dig +short" in section
        assert "dkim=pass" in section
        assert "dmarc=pass" in section

    def test_cutover_checklist_pins_go_live_gates(self, runbook_text):
        required = [
            "DKIM is verified",
            "Return-Path / MAIL FROM is verified",
            "`_dmarc.<sender-domain>` exists",
            "`dig` returns the expected records",
            "test email",
            "FS.4.3 webhook endpoint is configured",
        ]

        for item in required:
            assert item in runbook_text

    def test_runbook_warns_against_secret_leakage(self, runbook_text):
        assert "Do not add provider API keys to DNS records or this file" in runbook_text
        assert "must contain only the hostnames / values" in runbook_text

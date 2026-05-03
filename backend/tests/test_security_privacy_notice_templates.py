"""SC.9 — Unit tests for per-jurisdiction privacy-notice templates."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from backend.security import privacy_notice_templates as pnt


class TestGdprPrivacyNotice:
    def test_builds_gdpr_notice_with_required_rights(self):
        notice = pnt.build_gdpr_privacy_notice()
        assert notice.jurisdiction == pnt.JURISDICTION_GDPR
        assert notice.title == "GDPR Privacy Notice Template"

        rights = {right.id: right for right in notice.rights}
        assert set(rights) == {"access", "portability", "erasure", "objection"}
        assert rights["access"].article == "GDPR Article 15"
        assert rights["portability"].article == "GDPR Article 20"
        assert rights["erasure"].article == "GDPR Article 17"
        assert rights["objection"].article == "GDPR Article 21"

    def test_markdown_contains_right_sections_and_deadline(self):
        notice = pnt.build_gdpr_privacy_notice()
        markdown = notice.markdown

        assert markdown.startswith("# GDPR Privacy Notice Template\n")
        assert "## Your GDPR Rights" in markdown
        assert "Right of access" in markdown
        assert "Right to data portability" in markdown
        assert "Right to erasure" in markdown
        assert "Right to object" in markdown
        assert f"within {pnt.GDPR_RESPONSE_DEADLINE}" in markdown

    def test_accepts_generated_app_placeholders_or_concrete_values(self):
        notice = pnt.build_gdpr_privacy_notice(
            controller_name="Acme Robotics",
            privacy_contact="privacy@example.com",
            dpo_contact="dpo@example.com",
            dsar_endpoint="/api/v1/privacy/requests",
            effective_date="2026-05-03",
        )

        markdown = notice.markdown
        assert "Controller: Acme Robotics." in markdown
        assert "Privacy contact: privacy@example.com." in markdown
        assert "Data Protection Officer or EU representative: dpo@example.com." in markdown
        assert "Submit GDPR requests through /api/v1/privacy/requests" in markdown
        assert "Effective date: 2026-05-03." in markdown

    def test_to_dict_is_json_ready_and_includes_markdown(self):
        payload = pnt.build_gdpr_privacy_notice().to_dict()
        assert payload["jurisdiction"] == "gdpr"
        assert payload["markdown"]
        assert len(payload["sections"]) == 8
        assert len(payload["rights"]) == 4
        assert payload["rights"][0]["id"] == "access"


class TestCcpaPrivacyNotice:
    def test_builds_ccpa_notice_with_required_rights(self):
        notice = pnt.build_ccpa_privacy_notice()
        assert notice.jurisdiction == pnt.JURISDICTION_CCPA
        assert notice.title == "CCPA Privacy Notice Template"

        rights = {right.id: right for right in notice.rights}
        assert set(rights) == {
            "know",
            "delete",
            "correct",
            "opt_out_sale_sharing",
            "limit_sensitive_pi",
            "non_discrimination",
        }
        assert rights["know"].article == "CCPA"
        assert rights["delete"].article == "CCPA"
        assert rights["correct"].article == "CCPA as amended by CPRA"
        assert rights["opt_out_sale_sharing"].article == "CCPA as amended by CPRA"
        assert rights["limit_sensitive_pi"].article == "CCPA as amended by CPRA"
        assert rights["non_discrimination"].article == "CCPA"

    def test_markdown_contains_california_rights_and_deadlines(self):
        notice = pnt.build_ccpa_privacy_notice()
        markdown = notice.markdown

        assert markdown.startswith("# CCPA Privacy Notice Template\n")
        assert "## Your California Privacy Rights" in markdown
        assert "Right to know and access" in markdown
        assert "Right to delete" in markdown
        assert "Right to correct" in markdown
        assert "Right to opt out of sale or sharing" in markdown
        assert "Right to limit sensitive personal information" in markdown
        assert "Right to non-discrimination" in markdown
        assert f"within {pnt.CCPA_RESPONSE_DEADLINE}" in markdown
        assert f"no later than {pnt.CCPA_OPT_OUT_LIMIT_DEADLINE}" in markdown

    def test_accepts_generated_app_ccpa_placeholders_or_concrete_values(self):
        notice = pnt.build_ccpa_privacy_notice(
            business_name="Acme Robotics",
            privacy_contact="privacy@example.com",
            ccpa_request_endpoint="/api/v1/privacy/ccpa",
            do_not_sell_or_share_link="/privacy/do-not-sell",
            limit_sensitive_pi_link="/privacy/limit-sensitive-info",
            effective_date="2026-05-03",
        )

        markdown = notice.markdown
        assert "Business: Acme Robotics." in markdown
        assert "Privacy contact: privacy@example.com." in markdown
        assert "California privacy request path: /api/v1/privacy/ccpa." in markdown
        assert "Submit CCPA requests through /api/v1/privacy/ccpa" in markdown
        assert "Do Not Sell or Share My Personal Information: /privacy/do-not-sell." in markdown
        assert (
            "Limit the Use of My Sensitive Personal Information: "
            "/privacy/limit-sensitive-info."
        ) in markdown
        assert "Effective date: 2026-05-03." in markdown

    def test_to_dict_is_json_ready_and_includes_ccpa_metadata(self):
        payload = pnt.build_ccpa_privacy_notice().to_dict()
        assert payload["jurisdiction"] == "ccpa"
        assert payload["markdown"]
        assert len(payload["sections"]) == 9
        assert len(payload["rights"]) == 6
        assert payload["rights"][0]["id"] == "know"


class TestPrivacyNoticeTemplateShape:
    def test_gdpr_rights_constant_is_tuple_of_frozen_dataclasses(self):
        assert isinstance(pnt.GDPR_RIGHTS, tuple)
        right = pnt.GDPR_RIGHTS[0]

        try:
            right.label = "changed"  # type: ignore[misc]
        except Exception as exc:
            assert type(exc).__name__ == "FrozenInstanceError"
        else:
            raise AssertionError("DataSubjectRight must be frozen")

    def test_module_global_state_has_no_mutable_containers(self):
        source = Path(pnt.__file__).read_text()
        tree = ast.parse(source)
        mutable_globals: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if isinstance(node.value, (ast.List, ast.Dict, ast.Set)):
                    mutable_globals.extend(targets)
        assert mutable_globals == []

    def test_reload_preserves_constants(self):
        reloaded = importlib.reload(pnt)
        assert reloaded.JURISDICTION_GDPR == "gdpr"
        assert reloaded.JURISDICTION_CCPA == "ccpa"
        assert reloaded.GDPR_RESPONSE_DEADLINE == "one month"
        assert reloaded.CCPA_RESPONSE_DEADLINE == "45 calendar days"
        assert reloaded.CCPA_OPT_OUT_LIMIT_DEADLINE == "15 business days"
        assert tuple(right.id for right in reloaded.GDPR_RIGHTS) == (
            "access",
            "portability",
            "erasure",
            "objection",
        )
        assert tuple(right.id for right in reloaded.CCPA_RIGHTS) == (
            "know",
            "delete",
            "correct",
            "opt_out_sale_sharing",
            "limit_sensitive_pi",
            "non_discrimination",
        )

    def test_public_exports_are_pinned(self):
        assert set(pnt.__all__) == {
            "CCPA_OPT_OUT_LIMIT_DEADLINE",
            "CCPA_RESPONSE_DEADLINE",
            "CCPA_RIGHTS",
            "GDPR_RESPONSE_DEADLINE",
            "GDPR_RIGHTS",
            "JURISDICTION_CCPA",
            "JURISDICTION_GDPR",
            "DataSubjectRight",
            "PrivacyNoticeSection",
            "PrivacyNoticeTemplate",
            "build_ccpa_privacy_notice",
            "build_gdpr_privacy_notice",
        }

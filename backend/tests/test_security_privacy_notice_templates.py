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


class TestPiplPrivacyNotice:
    def test_builds_pipl_notice_with_required_rights(self):
        notice = pnt.build_pipl_privacy_notice()
        assert notice.jurisdiction == pnt.JURISDICTION_PIPL
        assert notice.title == "PIPL Privacy Notice Template"

        rights = {right.id: right for right in notice.rights}
        assert set(rights) == {
            "know_decide",
            "restrict_refuse",
            "access_copy",
            "portability",
            "correction",
            "deletion",
            "explanation",
            "deceased_close_relative",
        }
        assert rights["know_decide"].article == "PIPL Article 44"
        assert rights["restrict_refuse"].article == "PIPL Article 44"
        assert rights["access_copy"].article == "PIPL Article 45"
        assert rights["portability"].article == "PIPL Article 45"
        assert rights["correction"].article == "PIPL Article 46"
        assert rights["deletion"].article == "PIPL Article 47"
        assert rights["explanation"].article == "PIPL Article 48"
        assert rights["deceased_close_relative"].article == "PIPL Article 49"

    def test_markdown_contains_pipl_rights_and_transfer_sections(self):
        notice = pnt.build_pipl_privacy_notice()
        markdown = notice.markdown

        assert markdown.startswith("# PIPL Privacy Notice Template\n")
        assert "## Your PIPL Rights" in markdown
        assert "Right to know and decide" in markdown
        assert "Right to restrict or refuse processing" in markdown
        assert "Right to access and copy" in markdown
        assert "Right to portability" in markdown
        assert "Right to correction or supplementation" in markdown
        assert "Right to deletion" in markdown
        assert "Right to request explanation" in markdown
        assert "Close-relative rights for deceased individuals" in markdown
        assert "## Cross-Border Transfers" in markdown
        assert "separate consent path" in markdown

    def test_accepts_generated_app_pipl_placeholders_or_concrete_values(self):
        notice = pnt.build_pipl_privacy_notice(
            processor_name="Acme Robotics",
            privacy_contact="privacy@example.com",
            pipl_request_endpoint="/api/v1/privacy/pipl",
            china_representative_contact="cn-rep@example.cn",
            effective_date="2026-05-03",
        )

        markdown = notice.markdown
        assert "Personal information processor: Acme Robotics." in markdown
        assert "Privacy contact: privacy@example.com." in markdown
        assert "required for an overseas processor: cn-rep@example.cn." in markdown
        assert "PIPL request path: /api/v1/privacy/pipl." in markdown
        assert "Submit PIPL requests through /api/v1/privacy/pipl" in markdown
        assert "Effective date: 2026-05-03." in markdown

    def test_to_dict_is_json_ready_and_includes_pipl_metadata(self):
        payload = pnt.build_pipl_privacy_notice().to_dict()
        assert payload["jurisdiction"] == "pipl"
        assert payload["markdown"]
        assert len(payload["sections"]) == 9
        assert len(payload["rights"]) == 8
        assert payload["rights"][0]["id"] == "know_decide"


class TestLgpdPrivacyNotice:
    def test_builds_lgpd_notice_with_required_rights(self):
        notice = pnt.build_lgpd_privacy_notice()
        assert notice.jurisdiction == pnt.JURISDICTION_LGPD
        assert notice.title == "LGPD Privacy Notice Template"

        rights = {right.id: right for right in notice.rights}
        assert set(rights) == {
            "confirmation_access",
            "correction",
            "anonymization_blocking_deletion",
            "portability",
            "consent_based_deletion",
            "sharing_information",
            "consent_information",
            "consent_revocation",
            "petition_anpd",
            "objection",
            "automated_decision_review",
        }
        assert rights["confirmation_access"].article == "LGPD Articles 18 and 19"
        assert rights["correction"].article == "LGPD Article 18"
        assert rights["anonymization_blocking_deletion"].article == "LGPD Article 18"
        assert rights["portability"].article == "LGPD Article 18"
        assert rights["consent_based_deletion"].article == "LGPD Articles 16 and 18"
        assert rights["sharing_information"].article == "LGPD Article 18"
        assert rights["consent_information"].article == "LGPD Article 18"
        assert rights["consent_revocation"].article == "LGPD Articles 8 and 18"
        assert rights["petition_anpd"].article == "LGPD Article 18"
        assert rights["objection"].article == "LGPD Article 18"
        assert rights["automated_decision_review"].article == "LGPD Article 20"

    def test_markdown_contains_lgpd_rights_and_transfer_sections(self):
        notice = pnt.build_lgpd_privacy_notice()
        markdown = notice.markdown

        assert markdown.startswith("# LGPD Privacy Notice Template\n")
        assert "## Your LGPD Rights" in markdown
        assert "Right to confirmation and access" in markdown
        assert "Right to correction" in markdown
        assert "Right to anonymization, blocking, or deletion" in markdown
        assert "Right to portability" in markdown
        assert "Right to deletion of consent-based data" in markdown
        assert "Right to information about sharing" in markdown
        assert "Right to consent refusal information" in markdown
        assert "Right to revoke consent" in markdown
        assert "Right to petition the ANPD" in markdown
        assert "Right to object" in markdown
        assert "Right to review automated decisions" in markdown
        assert "## International Transfers" in markdown
        assert f"within {pnt.LGPD_ACCESS_RESPONSE_DEADLINE}" in markdown

    def test_accepts_generated_app_lgpd_placeholders_or_concrete_values(self):
        notice = pnt.build_lgpd_privacy_notice(
            controller_name="Acme Robotics",
            privacy_contact="privacy@example.com",
            dpo_contact="dpo@example.com",
            lgpd_request_endpoint="/api/v1/privacy/lgpd",
            effective_date="2026-05-03",
        )

        markdown = notice.markdown
        assert "Controller: Acme Robotics." in markdown
        assert "Privacy contact: privacy@example.com." in markdown
        assert "Data protection officer contact: dpo@example.com." in markdown
        assert "LGPD request path: /api/v1/privacy/lgpd." in markdown
        assert "Submit LGPD requests through /api/v1/privacy/lgpd" in markdown
        assert "Effective date: 2026-05-03." in markdown

    def test_to_dict_is_json_ready_and_includes_lgpd_metadata(self):
        payload = pnt.build_lgpd_privacy_notice().to_dict()
        assert payload["jurisdiction"] == "lgpd"
        assert payload["markdown"]
        assert len(payload["sections"]) == 9
        assert len(payload["rights"]) == 11
        assert payload["rights"][0]["id"] == "confirmation_access"


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
        assert reloaded.JURISDICTION_PIPL == "pipl"
        assert reloaded.JURISDICTION_LGPD == "lgpd"
        assert reloaded.GDPR_RESPONSE_DEADLINE == "one month"
        assert reloaded.CCPA_RESPONSE_DEADLINE == "45 calendar days"
        assert reloaded.CCPA_OPT_OUT_LIMIT_DEADLINE == "15 business days"
        assert reloaded.LGPD_ACCESS_RESPONSE_DEADLINE == "15 days"
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
        assert tuple(right.id for right in reloaded.PIPL_RIGHTS) == (
            "know_decide",
            "restrict_refuse",
            "access_copy",
            "portability",
            "correction",
            "deletion",
            "explanation",
            "deceased_close_relative",
        )
        assert tuple(right.id for right in reloaded.LGPD_RIGHTS) == (
            "confirmation_access",
            "correction",
            "anonymization_blocking_deletion",
            "portability",
            "consent_based_deletion",
            "sharing_information",
            "consent_information",
            "consent_revocation",
            "petition_anpd",
            "objection",
            "automated_decision_review",
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
            "JURISDICTION_LGPD",
            "JURISDICTION_PIPL",
            "LGPD_ACCESS_RESPONSE_DEADLINE",
            "LGPD_RIGHTS",
            "PIPL_RIGHTS",
            "DataSubjectRight",
            "PrivacyNoticeSection",
            "PrivacyNoticeTemplate",
            "build_ccpa_privacy_notice",
            "build_gdpr_privacy_notice",
            "build_lgpd_privacy_notice",
            "build_pipl_privacy_notice",
        }

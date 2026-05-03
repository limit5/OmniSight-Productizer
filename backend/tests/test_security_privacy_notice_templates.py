"""SC.9.1 — Unit tests for GDPR privacy-notice templates."""

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
        assert reloaded.GDPR_RESPONSE_DEADLINE == "one month"
        assert tuple(right.id for right in reloaded.GDPR_RIGHTS) == (
            "access",
            "portability",
            "erasure",
            "objection",
        )

    def test_public_exports_are_pinned(self):
        assert set(pnt.__all__) == {
            "GDPR_RESPONSE_DEADLINE",
            "GDPR_RIGHTS",
            "JURISDICTION_GDPR",
            "DataSubjectRight",
            "PrivacyNoticeSection",
            "PrivacyNoticeTemplate",
            "build_gdpr_privacy_notice",
        }

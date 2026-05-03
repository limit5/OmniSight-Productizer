"""BP.A.4 — Contract tests for ``backend/templates/review.py``.

Pins the ReviewTemplate validation surface — especially the two Auxiliary
disclaimer fields (``audit_type`` and ``requires_human_signoff``) that are
schema-pinned to make it structurally impossible to produce an authoritative
AI review or skip the human gate.

BP.A.7 will fold a superset of these checks into the unified ~150-test
``test_templates.py`` suite. Until then this file is the authoritative
regression for ReviewTemplate alone — keep it green.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.templates.review import SCHEMA_VERSION, ReviewTemplate


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        findings=["No buffer overflow in image_proc.c"],
        severity="low",
        reviewer_id="auditor-guild-agent-01",
        recommendation="Approve as-is; no action required.",
    )
    base.update(overrides)
    return base


# ── Happy path ────────────────────────────────────────────────────────────────

class TestReviewTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        assert t.schema_version == SCHEMA_VERSION == "1.0.0"

    def test_default_audit_type_is_advisory(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        assert t.audit_type == "advisory"

    def test_default_requires_human_signoff_is_true(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        assert t.requires_human_signoff is True

    def test_is_frozen(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        with pytest.raises(ValidationError):
            t.severity = "critical"  # type: ignore[misc]

    def test_strips_whitespace_on_string_fields(self) -> None:
        t = ReviewTemplate(**_valid_payload(
            reviewer_id="  auditor-01  ",
            recommendation="  Apply patch.  ",
        ))
        assert t.reviewer_id == "auditor-01"
        assert t.recommendation == "Apply patch."

    def test_json_round_trip(self) -> None:
        original = ReviewTemplate(**_valid_payload())
        rebuilt = ReviewTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_multiple_findings_accepted(self) -> None:
        t = ReviewTemplate(**_valid_payload(
            findings=["finding A", "finding B", "finding C"],
            severity="high",
        ))
        assert len(t.findings) == 3

    def test_critical_severity_accepted(self) -> None:
        t = ReviewTemplate(**_valid_payload(severity="critical"))
        assert t.severity == "critical"


# ── Auxiliary disclaimer: audit_type ─────────────────────────────────────────

class TestAuditTypeAuxiliaryDisclaimer:
    """``audit_type`` must be ``"advisory"`` — the schema-level disclaimer
    that AI reviews are advisory-only and cannot authorise a merge."""

    def test_accepts_advisory(self) -> None:
        t = ReviewTemplate(**_valid_payload(audit_type="advisory"))
        assert t.audit_type == "advisory"

    def test_rejects_authoritative(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(audit_type="authoritative"))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_blocking(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(audit_type="blocking"))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_informational(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(audit_type="informational"))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(audit_type=""))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_none(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(audit_type=None))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_audit_type_in_json_output_is_advisory(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        data = t.model_dump()
        assert data["audit_type"] == "advisory"

    def test_audit_type_survives_json_round_trip(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        rebuilt = ReviewTemplate.model_validate_json(t.model_dump_json())
        assert rebuilt.audit_type == "advisory"


# ── Auxiliary disclaimer: requires_human_signoff ──────────────────────────────

class TestRequiresHumanSignoffAuxiliaryDisclaimer:
    """``requires_human_signoff`` must be ``True`` — the schema-level
    guarantee that every ReviewTemplate mandates a human gate."""

    def test_accepts_true(self) -> None:
        t = ReviewTemplate(**_valid_payload(requires_human_signoff=True))
        assert t.requires_human_signoff is True

    def test_rejects_false(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(requires_human_signoff=False))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_none(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(requires_human_signoff=None))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(requires_human_signoff=0))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_rejects_string_true(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(requires_human_signoff="true"))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_requires_human_signoff_in_json_output_is_true(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        data = t.model_dump()
        assert data["requires_human_signoff"] is True

    def test_requires_human_signoff_survives_json_round_trip(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        rebuilt = ReviewTemplate.model_validate_json(t.model_dump_json())
        assert rebuilt.requires_human_signoff is True


# ── findings ──────────────────────────────────────────────────────────────────

class TestReviewTemplateFindings:
    def test_single_finding_accepted(self) -> None:
        t = ReviewTemplate(**_valid_payload(findings=["only one"]))
        assert list(t.findings) == ["only one"]

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(findings=[]))
        assert any(
            e["type"] == "too_short"
            and tuple(e["loc"]) == ("findings",)
            for e in exc.value.errors()
        )

    def test_blank_entry_stripped_then_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(findings=["   "]))

    def test_empty_string_entry_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(findings=[""]))

    def test_missing_findings_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["findings"]
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("findings",) in locs

    def test_findings_entries_are_stripped(self) -> None:
        t = ReviewTemplate(**_valid_payload(findings=["  trimmed entry  "]))
        assert t.findings[0] == "trimmed entry"

    def test_many_findings_accepted(self) -> None:
        findings = [f"finding {i}" for i in range(10)]
        t = ReviewTemplate(**_valid_payload(findings=findings))
        assert len(t.findings) == 10


# ── severity ──────────────────────────────────────────────────────────────────

class TestReviewTemplateSeverity:
    @pytest.mark.parametrize("sev", ["low", "medium", "high", "critical"])
    def test_accepts_all_valid_severities(self, sev: str) -> None:
        t = ReviewTemplate(**_valid_payload(severity=sev))
        assert t.severity == sev

    @pytest.mark.parametrize("bad", ["info", "warning", "none", "", "LOW", "CRITICAL"])
    def test_rejects_unlisted_severities(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(severity=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_severity_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["severity"]
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("severity",) in locs


# ── reviewer_id ───────────────────────────────────────────────────────────────

class TestReviewTemplateReviewerId:
    def test_accepts_valid_reviewer_id(self) -> None:
        t = ReviewTemplate(**_valid_payload(reviewer_id="auditor-agent-42"))
        assert t.reviewer_id == "auditor-agent-42"

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(reviewer_id=""))
        assert any(
            e["type"] == "string_too_short"
            and tuple(e["loc"]) == ("reviewer_id",)
            for e in exc.value.errors()
        )

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(reviewer_id="   "))

    def test_missing_reviewer_id_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["reviewer_id"]
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("reviewer_id",) in locs


# ── recommendation ────────────────────────────────────────────────────────────

class TestReviewTemplateRecommendation:
    def test_accepts_valid_recommendation(self) -> None:
        t = ReviewTemplate(**_valid_payload(recommendation="Approve after fixing line 42."))
        assert t.recommendation == "Approve after fixing line 42."

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_valid_payload(recommendation=""))
        assert any(
            e["type"] == "string_too_short"
            and tuple(e["loc"]) == ("recommendation",)
            for e in exc.value.errors()
        )

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(recommendation="\t\n "))

    def test_missing_recommendation_reports_clear_loc(self) -> None:
        payload = _valid_payload()
        del payload["recommendation"]
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        locs = {tuple(e["loc"]) for e in exc.value.errors()}
        assert ("recommendation",) in locs


# ── Schema strictness ─────────────────────────────────────────────────────────

class TestReviewTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _valid_payload()
        payload["rogue_field"] = "not allowed"
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _valid_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_schema_version_default_equals_module_constant(self) -> None:
        t = ReviewTemplate(**_valid_payload())
        assert t.schema_version == SCHEMA_VERSION

    def test_all_required_fields_missing_raises(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate()  # type: ignore[call-arg]


# ── Cross-template alignment ──────────────────────────────────────────────────

class TestReviewTemplateAuxiliaryDisclaimerInvariant:
    """The two auxiliary-disclaimer fields are structurally immutable.

    No combination of kwargs can produce a ReviewTemplate whose
    ``audit_type`` is not ``"advisory"`` or whose
    ``requires_human_signoff`` is not ``True``.
    """

    def test_cannot_override_audit_type_to_authoritative(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(audit_type="authoritative"))

    def test_cannot_override_requires_human_signoff_to_false(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_valid_payload(requires_human_signoff=False))

    def test_both_disclaimer_fields_present_in_serialised_output(self) -> None:
        data = ReviewTemplate(**_valid_payload()).model_dump()
        assert "audit_type" in data
        assert "requires_human_signoff" in data
        assert data["audit_type"] == "advisory"
        assert data["requires_human_signoff"] is True

    def test_disclaimer_invariant_survives_json_round_trip(self) -> None:
        original = ReviewTemplate(**_valid_payload(severity="critical"))
        rebuilt = ReviewTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt.audit_type == "advisory"
        assert rebuilt.requires_human_signoff is True
        assert rebuilt.severity == "critical"

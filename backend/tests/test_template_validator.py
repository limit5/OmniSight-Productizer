"""BP.A.6 — Contract tests for ``backend/template_validator.py``.

Pins the validation-middleware surface:
  - CognitivePenaltyPrompt model invariants
  - format_validation_penalty: ValidationError → structured penalty (per template type)
  - format_cognitive_overload_penalty: exceeds ceiling → cognitive-overload penalty
  - format_critical_review_penalty: severity='critical' → hard-block penalty
  - FastAPI router: POST /validate/{spec,task,impl,review} HTTP contract

BP.A.7 will fold a superset of these checks into the unified ~150-test
``test_templates.py`` suite. Until then this file is the authoritative
regression for BP.A.6 alone — keep it green.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.template_validator import (
    CognitivePenaltyPrompt,
    ImplValidationResult,
    ReviewValidationResult,
    SpecValidationResult,
    TaskValidationResult,
    ValidationErrorDetail,
    _FIX_HINTS,
    _hint_for_loc,
    _loc_to_str,
    format_cognitive_overload_penalty,
    format_critical_review_penalty,
    format_validation_penalty,
    router,
)
from backend.templates.review import ReviewTemplate
from backend.templates.task import TaskTemplate
from backend.cognitive_load import scan_cognitive_load


# ── Test client ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _valid_spec_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        system_boundaries=["no billing", "no auth", "no payments"],
        hardware_constraints=["ARM Cortex-M4", "256KB RAM", "3.3V supply"],
        api_idl_schema="openapi: 3.0.0\ninfo:\n  title: test\n  version: 0.1.0\npaths: {}",
        bdd_executable_specs="Feature: camera\n  Scenario: boot\n    Given power on",
        edge_cases_handled=["brown-out reset", "i2c timeout", "buffer overflow"],
    )
    base.update(overrides)
    return base


def _valid_task_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        target_triple="x86_64-pc-linux-gnu",
        allowed_dependencies=["backend/templates/spec.py"],
        max_cognitive_load_tokens=10_000,
        guild_id="coder-guild",
        size="S",
    )
    base.update(overrides)
    return base


def _valid_impl_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        source_code_payload="int main() { return 0; }",
        compiled_exit_code=0,
        time_complexity="O(1)",
        target_triple="x86_64-pc-linux-gnu",
    )
    base.update(overrides)
    return base


def _valid_review_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        findings=["memory leak in line 42"],
        severity="low",
        reviewer_id="auditor-guild-alpha",
        recommendation="Fix the memory leak and resubmit.",
    )
    base.update(overrides)
    return base


def _make_validation_error(template_cls: type, **overrides: object) -> ValidationError:
    """Force a ValidationError from the given template class."""
    base: dict[str, object] = {}
    with pytest.raises(ValidationError) as exc_info:
        template_cls(**{**base, **overrides})
    return exc_info.value


# ── _loc_to_str helper ────────────────────────────────────────────────────────


class TestLocToStr:
    def test_empty_loc_returns_root(self) -> None:
        assert _loc_to_str(()) == "(root)"

    def test_single_string_segment(self) -> None:
        assert _loc_to_str(("target_triple",)) == "target_triple"

    def test_string_then_int_gives_indexed_path(self) -> None:
        assert _loc_to_str(("system_boundaries", 0)) == "system_boundaries[0]"

    def test_nested_string_segments(self) -> None:
        result = _loc_to_str(("parent", "child"))
        assert result == "parent.child"

    def test_int_index_only(self) -> None:
        result = _loc_to_str((2,))
        assert result == "[2]"


# ── _hint_for_loc helper ──────────────────────────────────────────────────────


class TestHintForLoc:
    def test_known_field_returns_hint(self) -> None:
        hint = _hint_for_loc(("target_triple",))
        assert "arch-vendor-os" in hint

    def test_nested_loc_uses_first_string(self) -> None:
        hint = _hint_for_loc(("system_boundaries", 0))
        assert "3" in hint  # "at least 3 entries"

    def test_unknown_field_returns_fallback(self) -> None:
        hint = _hint_for_loc(("completely_unknown_field",))
        assert len(hint) > 0
        assert "schema constraint" in hint.lower() or "correct" in hint.lower()

    def test_empty_loc_returns_fallback(self) -> None:
        hint = _hint_for_loc(())
        assert len(hint) > 0


# ── CognitivePenaltyPrompt model ──────────────────────────────────────────────


class TestCognitivePenaltyPromptModel:
    def test_constructs_validation_error_type(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="validation_error",
            template_type="spec",
            prompt="test",
        )
        assert p.penalty_type == "validation_error"
        assert p.status == "rejected"

    def test_constructs_cognitive_overload_type(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="cognitive_overload",
            template_type="task",
            prompt="test",
        )
        assert p.penalty_type == "cognitive_overload"

    def test_constructs_critical_review_type(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="critical_review",
            template_type="review",
            prompt="test",
        )
        assert p.penalty_type == "critical_review"

    def test_status_is_always_rejected(self) -> None:
        for pt in ("validation_error", "cognitive_overload", "critical_review"):
            p = CognitivePenaltyPrompt(penalty_type=pt, template_type="spec", prompt="x")  # type: ignore[arg-type]
            assert p.status == "rejected"

    def test_is_frozen(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="validation_error",
            template_type="spec",
            prompt="test",
        )
        with pytest.raises((ValidationError, TypeError)):
            p.status = "accepted"  # type: ignore[misc]

    def test_optional_fields_default_to_none(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="validation_error",
            template_type="impl",
            prompt="test",
        )
        assert p.errors is None
        assert p.cognitive_load is None
        assert p.findings is None

    def test_json_round_trip(self) -> None:
        p = CognitivePenaltyPrompt(
            penalty_type="validation_error",
            template_type="task",
            prompt="A penalty prompt.",
            errors=[ValidationErrorDetail(field="size", message="bad", fix_hint="use S/M/XL")],
        )
        dumped = p.model_dump()
        rebuilt = CognitivePenaltyPrompt(**dumped)
        assert rebuilt == p


# ── format_validation_penalty ─────────────────────────────────────────────────


class TestFormatValidationPenaltySpec:
    def test_missing_required_field_produces_validation_error_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(
                system_boundaries=["a", "b", "c"],
                hardware_constraints=["x", "y", "z"],
                # api_idl_schema missing
                bdd_executable_specs="Feature: x",
                edge_cases_handled=["a", "b", "c"],
            )
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert penalty.penalty_type == "validation_error"
        assert penalty.template_type == "spec"

    def test_penalty_prompt_contains_header(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(system_boundaries=["a", "b", "c"], hardware_constraints=["x", "y", "z"],
                         api_idl_schema="", bdd_executable_specs="f", edge_cases_handled=["a","b","c"])
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert "COGNITIVE PENALTY" in penalty.prompt

    def test_penalty_errors_list_is_populated(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(system_boundaries=[], hardware_constraints=["x","y","z"],
                         api_idl_schema="x", bdd_executable_specs="f", edge_cases_handled=["a","b","c"])
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert penalty.errors is not None
        assert len(penalty.errors) > 0

    def test_errors_contain_fix_hint(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(system_boundaries=["only one"], hardware_constraints=["x","y","z"],
                         api_idl_schema="x", bdd_executable_specs="f", edge_cases_handled=["a","b","c"])
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert penalty.errors is not None
        for err in penalty.errors:
            assert len(err.fix_hint) > 0

    def test_prompt_mentions_resubmit(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(system_boundaries=[], hardware_constraints=["x","y","z"],
                         api_idl_schema="x", bdd_executable_specs="f", edge_cases_handled=["a","b","c"])
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert "Resubmit" in penalty.prompt or "resubmit" in penalty.prompt.lower()

    def test_prompt_mentions_template_type(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.spec import SpecTemplate
            SpecTemplate(system_boundaries=[], hardware_constraints=["x","y","z"],
                         api_idl_schema="x", bdd_executable_specs="f", edge_cases_handled=["a","b","c"])
        penalty = format_validation_penalty(exc_info.value, "spec")
        assert "spec" in penalty.prompt.lower()


class TestFormatValidationPenaltyTask:
    def test_invalid_target_triple_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="not-valid",  # only 2 segments
                         allowed_dependencies=[], max_cognitive_load_tokens=100,
                         guild_id="g", size="S")
        penalty = format_validation_penalty(exc_info.value, "task")
        assert penalty.penalty_type == "validation_error"
        assert penalty.template_type == "task"

    def test_fix_hint_for_target_triple_mentions_pattern(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="bad",
                         allowed_dependencies=[], max_cognitive_load_tokens=100,
                         guild_id="g", size="S")
        penalty = format_validation_penalty(exc_info.value, "task")
        assert penalty.errors is not None
        triple_errors = [e for e in penalty.errors if "target_triple" in e.field]
        assert triple_errors
        assert "arch-vendor-os" in triple_errors[0].fix_hint

    def test_invalid_size_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="x86_64-pc-linux-gnu",
                         allowed_dependencies=[], max_cognitive_load_tokens=100,
                         guild_id="g", size="XXL")  # type: ignore[arg-type]
        penalty = format_validation_penalty(exc_info.value, "task")
        assert penalty.penalty_type == "validation_error"
        size_errors = [e for e in (penalty.errors or []) if "size" in e.field]
        assert size_errors
        assert "S" in size_errors[0].fix_hint

    def test_zero_max_cognitive_load_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="x86_64-pc-linux-gnu",
                         allowed_dependencies=[], max_cognitive_load_tokens=0,
                         guild_id="g", size="S")
        penalty = format_validation_penalty(exc_info.value, "task")
        assert penalty.penalty_type == "validation_error"

    def test_empty_guild_id_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="x86_64-pc-linux-gnu",
                         allowed_dependencies=[], max_cognitive_load_tokens=100,
                         guild_id="", size="S")
        penalty = format_validation_penalty(exc_info.value, "task")
        assert penalty.penalty_type == "validation_error"

    def test_prompt_is_non_empty_string(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TaskTemplate(target_triple="bad",
                         allowed_dependencies=[], max_cognitive_load_tokens=100,
                         guild_id="g", size="S")
        penalty = format_validation_penalty(exc_info.value, "task")
        assert isinstance(penalty.prompt, str)
        assert len(penalty.prompt) > 50


class TestFormatValidationPenaltyImpl:
    def test_nonzero_compiled_exit_code_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.impl import ImplTemplate
            ImplTemplate(source_code_payload="int x;", compiled_exit_code=1,  # type: ignore[arg-type]
                         time_complexity="O(1)", target_triple="x86_64-pc-linux-gnu")
        penalty = format_validation_penalty(exc_info.value, "impl")
        assert penalty.penalty_type == "validation_error"

    def test_fix_hint_for_compiled_exit_code_mentions_zero(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.impl import ImplTemplate
            ImplTemplate(source_code_payload="int x;", compiled_exit_code=1,  # type: ignore[arg-type]
                         time_complexity="O(1)", target_triple="x86_64-pc-linux-gnu")
        penalty = format_validation_penalty(exc_info.value, "impl")
        assert penalty.errors is not None
        exit_errors = [e for e in penalty.errors if "compiled_exit_code" in e.field]
        assert exit_errors
        assert "0" in exit_errors[0].fix_hint

    def test_invalid_time_complexity_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.impl import ImplTemplate
            ImplTemplate(source_code_payload="int x;", compiled_exit_code=0,
                         time_complexity="fast",  # does not match regex
                         target_triple="x86_64-pc-linux-gnu")
        penalty = format_validation_penalty(exc_info.value, "impl")
        assert penalty.penalty_type == "validation_error"
        tc_errors = [e for e in (penalty.errors or []) if "time_complexity" in e.field]
        assert tc_errors

    def test_empty_source_code_payload_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.impl import ImplTemplate
            ImplTemplate(source_code_payload="", compiled_exit_code=0,
                         time_complexity="O(1)", target_triple="x86_64-pc-linux-gnu")
        penalty = format_validation_penalty(exc_info.value, "impl")
        assert penalty.penalty_type == "validation_error"

    def test_template_type_is_impl(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            from backend.templates.impl import ImplTemplate
            ImplTemplate(source_code_payload="", compiled_exit_code=0,
                         time_complexity="O(1)", target_triple="x86_64-pc-linux-gnu")
        penalty = format_validation_penalty(exc_info.value, "impl")
        assert penalty.template_type == "impl"


class TestFormatValidationPenaltyReview:
    def test_empty_findings_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ReviewTemplate(findings=[], severity="low",  # findings min_length=1
                           reviewer_id="bot", recommendation="fix it")
        penalty = format_validation_penalty(exc_info.value, "review")
        assert penalty.penalty_type == "validation_error"

    def test_invalid_severity_produces_penalty(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ReviewTemplate(findings=["issue"], severity="blocker",  # type: ignore[arg-type]
                           reviewer_id="bot", recommendation="fix it")
        penalty = format_validation_penalty(exc_info.value, "review")
        assert penalty.penalty_type == "validation_error"
        sev_errors = [e for e in (penalty.errors or []) if "severity" in e.field]
        assert sev_errors
        assert "critical" in sev_errors[0].fix_hint

    def test_template_type_is_review(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ReviewTemplate(findings=[], severity="low",
                           reviewer_id="bot", recommendation="fix it")
        penalty = format_validation_penalty(exc_info.value, "review")
        assert penalty.template_type == "review"

    def test_errors_list_is_populated(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ReviewTemplate(findings=[], severity="low",
                           reviewer_id="bot", recommendation="fix it")
        penalty = format_validation_penalty(exc_info.value, "review")
        assert penalty.errors is not None
        assert len(penalty.errors) >= 1


# ── format_cognitive_overload_penalty ─────────────────────────────────────────


class TestFormatCognitiveOverloadPenalty:
    def _overload_task(self) -> tuple[TaskTemplate, object]:
        task = TaskTemplate(
            target_triple="x86_64-pc-linux-gnu",
            allowed_dependencies=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
            max_cognitive_load_tokens=500,  # very low ceiling
            guild_id="coder-guild",
            size="XL",
        )
        report = scan_cognitive_load(task)
        assert report.exceeds_ceiling, "test precondition: task must exceed ceiling"
        return task, report

    def test_penalty_type_is_cognitive_overload(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.penalty_type == "cognitive_overload"

    def test_template_type_is_task(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.template_type == "task"

    def test_status_is_rejected(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.status == "rejected"

    def test_prompt_mentions_estimated_tokens(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert str(report.estimated_tokens) in penalty.prompt  # type: ignore[union-attr]

    def test_prompt_mentions_ceiling(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert str(report.ceiling) in penalty.prompt  # type: ignore[union-attr]

    def test_prompt_mentions_decomposition(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert "decompos" in penalty.prompt.lower()  # type: ignore[union-attr]

    def test_cognitive_load_dict_is_populated(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.cognitive_load is not None
        assert "fan_in" in penalty.cognitive_load
        assert "estimated_tokens" in penalty.cognitive_load
        assert "exceeds_ceiling" in penalty.cognitive_load

    def test_cognitive_load_dict_matches_report(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.cognitive_load is not None
        assert penalty.cognitive_load["fan_in"] == report.fan_in  # type: ignore[union-attr]
        assert penalty.cognitive_load["ceiling"] == report.ceiling  # type: ignore[union-attr]

    def test_errors_field_is_none(self) -> None:
        task, report = self._overload_task()
        penalty = format_cognitive_overload_penalty(task, report)  # type: ignore[arg-type]
        assert penalty.errors is None


# ── format_critical_review_penalty ────────────────────────────────────────────


class TestFormatCriticalReviewPenalty:
    def _critical_review(self) -> ReviewTemplate:
        return ReviewTemplate(
            findings=["buffer overflow in codec", "unvalidated pointer deref"],
            severity="critical",
            reviewer_id="auditor-guild-alpha",
            recommendation="Halt deployment; fix memory safety issues immediately.",
        )

    def test_penalty_type_is_critical_review(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert penalty.penalty_type == "critical_review"

    def test_template_type_is_review(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert penalty.template_type == "review"

    def test_status_is_rejected(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert penalty.status == "rejected"

    def test_prompt_mentions_reviewer_id(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert review.reviewer_id in penalty.prompt

    def test_prompt_mentions_human_signoff(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert "human" in penalty.prompt.lower()

    def test_prompt_mentions_findings(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        for finding in review.findings:
            assert finding in penalty.prompt

    def test_findings_list_is_populated(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert penalty.findings is not None
        assert len(penalty.findings) == len(review.findings)
        assert set(penalty.findings) == set(review.findings)

    def test_prompt_mentions_recommendation(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert review.recommendation in penalty.prompt

    def test_errors_field_is_none(self) -> None:
        review = self._critical_review()
        penalty = format_critical_review_penalty(review)
        assert penalty.errors is None


# ── FastAPI endpoint: POST /validate/spec ─────────────────────────────────────


class TestValidateSpecEndpoint:
    def test_valid_spec_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/spec", json=_valid_spec_body())
        assert resp.status_code == 200

    def test_valid_spec_response_has_accepted_status(self, client: TestClient) -> None:
        resp = client.post("/validate/spec", json=_valid_spec_body())
        assert resp.json()["status"] == "accepted"
        assert resp.json()["template_type"] == "spec"

    def test_invalid_spec_returns_422(self, client: TestClient) -> None:
        body = _valid_spec_body()
        del body["api_idl_schema"]
        resp = client.post("/validate/spec", json=body)
        assert resp.status_code == 422

    def test_invalid_spec_body_is_penalty_prompt(self, client: TestClient) -> None:
        body = _valid_spec_body(system_boundaries=[])  # < 3 entries
        resp = client.post("/validate/spec", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["status"] == "rejected"
        assert data["penalty_type"] == "validation_error"
        assert data["template_type"] == "spec"
        assert len(data["prompt"]) > 0

    def test_invalid_spec_errors_list_populated(self, client: TestClient) -> None:
        body = _valid_spec_body(system_boundaries=[])
        resp = client.post("/validate/spec", json=body)
        data = resp.json()
        assert data["errors"] is not None
        assert len(data["errors"]) > 0


# ── FastAPI endpoint: POST /validate/task ─────────────────────────────────────


class TestValidateTaskEndpoint:
    def test_valid_task_within_ceiling_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/task", json=_valid_task_body())
        assert resp.status_code == 200

    def test_valid_task_response_contains_cognitive_load(self, client: TestClient) -> None:
        resp = client.post("/validate/task", json=_valid_task_body())
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["template_type"] == "task"
        assert "cognitive_load" in data
        assert "fan_in" in data["cognitive_load"]
        assert "estimated_tokens" in data["cognitive_load"]
        assert data["cognitive_load"]["exceeds_ceiling"] is False

    def test_invalid_task_returns_422(self, client: TestClient) -> None:
        body = _valid_task_body(target_triple="bad")
        resp = client.post("/validate/task", json=body)
        assert resp.status_code == 422
        assert resp.json()["penalty_type"] == "validation_error"

    def test_task_exceeding_ceiling_returns_422(self, client: TestClient) -> None:
        body = _valid_task_body(
            allowed_dependencies=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
            max_cognitive_load_tokens=100,  # impossibly low ceiling
            size="XL",
        )
        resp = client.post("/validate/task", json=body)
        assert resp.status_code == 422

    def test_task_exceeding_ceiling_penalty_type(self, client: TestClient) -> None:
        body = _valid_task_body(
            allowed_dependencies=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
            max_cognitive_load_tokens=100,
            size="XL",
        )
        resp = client.post("/validate/task", json=body)
        assert resp.json()["penalty_type"] == "cognitive_overload"

    def test_task_overload_prompt_mentions_ceiling(self, client: TestClient) -> None:
        body = _valid_task_body(
            allowed_dependencies=["a", "b", "c", "d"],
            max_cognitive_load_tokens=100,
            size="XL",
        )
        resp = client.post("/validate/task", json=body)
        assert "100" in resp.json()["prompt"]

    def test_task_overload_cognitive_load_dict_present(self, client: TestClient) -> None:
        body = _valid_task_body(
            allowed_dependencies=["a", "b", "c", "d", "e"],
            max_cognitive_load_tokens=100,
            size="XL",
        )
        resp = client.post("/validate/task", json=body)
        data = resp.json()
        assert data["cognitive_load"] is not None
        assert "exceeds_ceiling" in data["cognitive_load"]
        assert data["cognitive_load"]["exceeds_ceiling"] is True


# ── FastAPI endpoint: POST /validate/impl ─────────────────────────────────────


class TestValidateImplEndpoint:
    def test_valid_impl_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/impl", json=_valid_impl_body())
        assert resp.status_code == 200

    def test_valid_impl_response_has_accepted_status(self, client: TestClient) -> None:
        resp = client.post("/validate/impl", json=_valid_impl_body())
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["template_type"] == "impl"

    def test_nonzero_exit_code_returns_422(self, client: TestClient) -> None:
        body = _valid_impl_body(compiled_exit_code=1)
        resp = client.post("/validate/impl", json=body)
        assert resp.status_code == 422
        assert resp.json()["penalty_type"] == "validation_error"

    def test_invalid_time_complexity_returns_422(self, client: TestClient) -> None:
        body = _valid_impl_body(time_complexity="fast")
        resp = client.post("/validate/impl", json=body)
        assert resp.status_code == 422

    def test_empty_source_payload_returns_422(self, client: TestClient) -> None:
        body = _valid_impl_body(source_code_payload="")
        resp = client.post("/validate/impl", json=body)
        assert resp.status_code == 422
        data = resp.json()
        assert data["status"] == "rejected"


# ── FastAPI endpoint: POST /validate/review ───────────────────────────────────


class TestValidateReviewEndpoint:
    def test_valid_review_low_severity_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="low"))
        assert resp.status_code == 200

    def test_valid_review_medium_severity_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="medium"))
        assert resp.status_code == 200

    def test_valid_review_high_severity_returns_200(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="high"))
        assert resp.status_code == 200

    def test_critical_severity_returns_422(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="critical"))
        assert resp.status_code == 422

    def test_critical_severity_penalty_type(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="critical"))
        assert resp.json()["penalty_type"] == "critical_review"

    def test_critical_penalty_prompt_requires_human(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body(severity="critical"))
        assert "human" in resp.json()["prompt"].lower()

    def test_critical_penalty_findings_populated(self, client: TestClient) -> None:
        body = _valid_review_body(
            severity="critical",
            findings=["critical bug in codec", "null deref on shutdown"],
        )
        resp = client.post("/validate/review", json=body)
        data = resp.json()
        assert data["findings"] is not None
        assert len(data["findings"]) == 2

    def test_invalid_review_schema_returns_422_validation_error(self, client: TestClient) -> None:
        body = _valid_review_body(findings=[])  # < 1 entry
        resp = client.post("/validate/review", json=body)
        assert resp.status_code == 422
        assert resp.json()["penalty_type"] == "validation_error"

    def test_valid_review_response_has_correct_shape(self, client: TestClient) -> None:
        resp = client.post("/validate/review", json=_valid_review_body())
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["template_type"] == "review"


# ── _FIX_HINTS registry completeness ─────────────────────────────────────────


class TestFixHintsRegistry:
    def test_known_fields_have_non_empty_hints(self) -> None:
        for field, hint in _FIX_HINTS.items():
            assert isinstance(hint, str), f"hint for '{field}' must be a string"
            assert len(hint.strip()) > 0, f"hint for '{field}' must not be empty"

    def test_critical_template_fields_are_covered(self) -> None:
        critical_fields = {
            "target_triple", "size", "compiled_exit_code", "time_complexity",
            "severity", "audit_type", "requires_human_signoff",
            "system_boundaries", "hardware_constraints", "edge_cases_handled",
            "findings",
        }
        for field in critical_fields:
            assert field in _FIX_HINTS, f"'{field}' missing from _FIX_HINTS"

    def test_fix_hints_is_immutable_dict(self) -> None:
        assert isinstance(_FIX_HINTS, dict)
        # All keys are strings
        for key in _FIX_HINTS:
            assert isinstance(key, str)

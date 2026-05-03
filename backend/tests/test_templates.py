"""BP.A.7 — Unified ~150-test contract suite for all six BP.A modules.

Modules under test:
  - SpecTemplate        (backend/templates/spec.py)    — Architect Guild contract
  - TaskTemplate        (backend/templates/task.py)    — PM Guild contract
  - ImplTemplate        (backend/templates/impl.py)    — Coder Guild contract
  - ReviewTemplate      (backend/templates/review.py)  — Auditor Guild contract
  - CognitiveLoadScanner (backend/cognitive_load.py)
  - RLM dispatch        (backend/rlm_dispatch.py)

Structure: ~120 template + cognitive-load tests + ~30 RLM-pattern tests.
Individual per-module suites (test_template_spec.py through test_rlm_dispatch.py)
remain the per-module regressions; this file is the integration-level contract
that BP.A.6 template_validator and future Blueprint phases build on.

Cross-worker safety: all modules under test declare only immutable module-level
constants — safe under ``uvicorn --workers N`` by construction (SOP Step 1
answer #1: 不共享，因為每 worker 從同樣來源推導出同樣的值).
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from backend.templates.spec import SCHEMA_VERSION as SPEC_SCHEMA_VERSION, SpecTemplate
from backend.templates.task import SCHEMA_VERSION as TASK_SCHEMA_VERSION, TaskTemplate
from backend.templates.impl import SCHEMA_VERSION as IMPL_SCHEMA_VERSION, ImplTemplate
from backend.templates.review import SCHEMA_VERSION as REVIEW_SCHEMA_VERSION, ReviewTemplate
from backend.cognitive_load import (
    CognitiveLoadReport,
    _BASE_TOKENS,
    _FAN_IN_WEIGHT,
    _FAN_OUT_BASE,
    _FAN_OUT_DEP_STEP,
    _FAN_OUT_WEIGHT,
    _MOCK_FRACTION,
    _MOCK_MAX,
    scan_cognitive_load,
)
import backend.rlm_dispatch as rlm_mod
from backend.rlm_dispatch import (
    CONTEXT_TOKENS_THRESHOLD,
    DEPTH_CAP,
    MAX_PARTITIONS,
    PARTITION_SIZE_TOKENS,
    RLM_TASK_TYPES,
    SIMPLE_TASK_TYPES,
    RlmDispatchPlan,
    decide_dispatch_mode,
    partition_text,
    plan_dispatch,
)


# ══════════════════════════════════════════════════════════════════════════════
# Shared payload / factory helpers
# ══════════════════════════════════════════════════════════════════════════════


def _spec_payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        system_boundaries=[
            "does NOT touch billing service",
            "does NOT touch auth service",
            "does NOT mutate user storage",
        ],
        hardware_constraints=[
            "arm64 cortex-a55 SoC",
            "512 MB DDR4 RAM ceiling",
            "5 W TDP power budget",
        ],
        api_idl_schema="openapi: 3.0.0\ninfo:\n  title: example\n  version: 0.1\n",
        bdd_executable_specs=(
            "Feature: example\n  Scenario: minimal\n"
            "    Given a precondition\n    When an event\n    Then an outcome\n"
        ),
        edge_cases_handled=[
            "power-loss mid-write",
            "network partition during sync",
            "wall-clock skew across nodes",
        ],
    )
    base.update(kw)
    return base


def _task_payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        target_triple="x86_64-pc-linux-gnu",
        allowed_dependencies=["backend/templates/spec.py", "backend/cognitive_load.py"],
        max_cognitive_load_tokens=4096,
        guild_id="backend",
        size="M",
    )
    base.update(kw)
    return base


def _impl_payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        source_code_payload="int main(void) { return 0; }\n",
        compiled_exit_code=0,
        time_complexity="O(n log n)",
        target_triple="x86_64-pc-linux-gnu",
    )
    base.update(kw)
    return base


def _review_payload(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        findings=["No buffer overflow in image_proc.c"],
        severity="low",
        reviewer_id="auditor-guild-agent-01",
        recommendation="Approve as-is; no action required.",
    )
    base.update(kw)
    return base


def _task_obj(
    *,
    size: str = "M",
    allowed_dependencies: list[str] | None = None,
    max_cognitive_load_tokens: int = 10_000,
    target_triple: str = "x86_64-pc-linux-gnu",
    guild_id: str = "coder-guild",
) -> TaskTemplate:
    return TaskTemplate(
        target_triple=target_triple,
        allowed_dependencies=allowed_dependencies if allowed_dependencies is not None else [],
        max_cognitive_load_tokens=max_cognitive_load_tokens,
        guild_id=guild_id,
        size=size,  # type: ignore[arg-type]
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. SpecTemplate  (15 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestSpecTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = SpecTemplate(**_spec_payload())
        assert t.schema_version == SPEC_SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = SpecTemplate(**_spec_payload())
        with pytest.raises(ValidationError):
            t.system_boundaries = []  # type: ignore[misc]

    def test_strips_whitespace_on_string_field(self) -> None:
        t = SpecTemplate(**_spec_payload(api_idl_schema="  openapi: 3.0.0  "))
        assert t.api_idl_schema == "openapi: 3.0.0"

    def test_json_round_trip(self) -> None:
        original = SpecTemplate(**_spec_payload())
        rebuilt = SpecTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original


class TestSpecTemplateMinLengthGuards:
    @pytest.mark.parametrize(
        "field",
        ["system_boundaries", "hardware_constraints", "edge_cases_handled"],
    )
    def test_three_item_lists_reject_two_entries(self, field: str) -> None:
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**_spec_payload(**{field: ["only-a", "only-b"]}))
        assert any(e["type"] == "too_short" and e["loc"][0] == field for e in exc.value.errors())

    @pytest.mark.parametrize(
        "field",
        ["system_boundaries", "hardware_constraints", "edge_cases_handled"],
    )
    def test_three_item_lists_accept_exactly_three(self, field: str) -> None:
        t = SpecTemplate(**_spec_payload(**{field: ["x", "y", "z"]}))
        assert len(getattr(t, field)) == 3

    @pytest.mark.parametrize("field", ["api_idl_schema", "bdd_executable_specs"])
    def test_string_fields_reject_empty(self, field: str) -> None:
        with pytest.raises(ValidationError):
            SpecTemplate(**_spec_payload(**{field: ""}))


class TestSpecTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _spec_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _spec_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_required_field_reports_clear_error(self) -> None:
        payload = _spec_payload()
        del payload["edge_cases_handled"]
        with pytest.raises(ValidationError) as exc:
            SpecTemplate(**payload)
        assert ("edge_cases_handled",) in {tuple(e["loc"]) for e in exc.value.errors()}


# ══════════════════════════════════════════════════════════════════════════════
# 2. TaskTemplate  (39 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestTaskTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = TaskTemplate(**_task_payload())
        assert t.schema_version == TASK_SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = TaskTemplate(**_task_payload())
        with pytest.raises(ValidationError):
            t.size = "XL"  # type: ignore[misc]

    def test_strips_whitespace_on_guild_id(self) -> None:
        t = TaskTemplate(**_task_payload(guild_id="  backend  "))
        assert t.guild_id == "backend"

    def test_strips_whitespace_on_list_entry(self) -> None:
        t = TaskTemplate(**_task_payload(allowed_dependencies=["  spec.py  "]))
        assert t.allowed_dependencies == ["spec.py"]

    def test_json_round_trip(self) -> None:
        original = TaskTemplate(**_task_payload())
        rebuilt = TaskTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_empty_allowed_dependencies_accepted(self) -> None:
        t = TaskTemplate(**_task_payload(allowed_dependencies=[]))
        assert t.allowed_dependencies == []


class TestTaskTemplateTargetTriple:
    @pytest.mark.parametrize(
        "triple",
        [
            "x86_64-pc-linux-gnu",
            "aarch64-vendor-linux",
            "aarch64-unknown-linux-gnu",
            "armv7-unknown-linux-gnueabihf",
            "x86_64-apple-darwin",
        ],
    )
    def test_accepts_canonical_triples(self, triple: str) -> None:
        t = TaskTemplate(**_task_payload(target_triple=triple))
        assert t.target_triple == triple

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "x86_64",
            "x86_64-pc",
            "x86_64--pc-linux",
            "x86_64-pc-linux-gnu-x",
            "x86 64-pc-linux-gnu",
            "x86_64/pc/linux",
        ],
    )
    def test_rejects_malformed_triples(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_task_payload(target_triple=bad))
        codes = {e["type"] for e in exc.value.errors()}
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes


class TestTaskTemplateAllowedDependencies:
    def test_inner_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_task_payload(allowed_dependencies=["spec.py", ""]))
        assert any(
            e["type"] == "string_too_short" and e["loc"][:2] == ("allowed_dependencies", 1)
            for e in exc.value.errors()
        )

    def test_inner_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplate(**_task_payload(allowed_dependencies=["   "]))

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _task_payload()
        del payload["allowed_dependencies"]
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert ("allowed_dependencies",) in {tuple(e["loc"]) for e in exc.value.errors()}


class TestTaskTemplateCognitiveLoadCeiling:
    @pytest.mark.parametrize("bad", [0, -1, -1024])
    def test_rejects_non_positive(self, bad: int) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_task_payload(max_cognitive_load_tokens=bad))
        assert any(e["type"] == "greater_than" for e in exc.value.errors())

    def test_accepts_minimum_of_one(self) -> None:
        t = TaskTemplate(**_task_payload(max_cognitive_load_tokens=1))
        assert t.max_cognitive_load_tokens == 1


class TestTaskTemplateGuildId:
    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_task_payload(guild_id=""))
        assert any(e["type"] == "string_too_short" for e in exc.value.errors())

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplate(**_task_payload(guild_id="   "))


class TestTaskTemplateSize:
    @pytest.mark.parametrize("size", ["S", "M", "XL"])
    def test_accepts_canonical_sizes(self, size: str) -> None:
        t = TaskTemplate(**_task_payload(size=size))
        assert t.size == size

    @pytest.mark.parametrize("bad", ["s", "L", "XXL", "", "MEDIUM", "1"])
    def test_rejects_other_values(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**_task_payload(size=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())


class TestTaskTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _task_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned_to_one_zero_zero(self) -> None:
        payload = _task_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_missing_required_field_reports_clear_error(self) -> None:
        payload = _task_payload()
        del payload["max_cognitive_load_tokens"]
        with pytest.raises(ValidationError) as exc:
            TaskTemplate(**payload)
        assert ("max_cognitive_load_tokens",) in {tuple(e["loc"]) for e in exc.value.errors()}


# ══════════════════════════════════════════════════════════════════════════════
# 3. ImplTemplate  (24 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestImplTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = ImplTemplate(**_impl_payload())
        assert t.schema_version == IMPL_SCHEMA_VERSION == "1.0.0"

    def test_is_frozen(self) -> None:
        t = ImplTemplate(**_impl_payload())
        with pytest.raises(ValidationError):
            t.time_complexity = "O(1)"  # type: ignore[misc]

    def test_strips_whitespace_on_string_fields(self) -> None:
        t = ImplTemplate(**_impl_payload(
            source_code_payload="   payload\n",
            time_complexity="  O(n)  ",
            target_triple="  x86_64-pc-linux-gnu  ",
        ))
        assert t.source_code_payload == "payload"
        assert t.time_complexity == "O(n)"
        assert t.target_triple == "x86_64-pc-linux-gnu"

    def test_json_round_trip(self) -> None:
        original = ImplTemplate(**_impl_payload())
        rebuilt = ImplTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original


class TestImplTemplateSourceCodePayload:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_impl_payload(source_code_payload=""))
        assert any(
            e["type"] == "string_too_short" and tuple(e["loc"]) == ("source_code_payload",)
            for e in exc.value.errors()
        )

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ImplTemplate(**_impl_payload(source_code_payload="   \n\t "))

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _impl_payload()
        del payload["source_code_payload"]
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert ("source_code_payload",) in {tuple(e["loc"]) for e in exc.value.errors()}


class TestImplTemplateCompiledExitCode:
    def test_accepts_zero(self) -> None:
        t = ImplTemplate(**_impl_payload(compiled_exit_code=0))
        assert t.compiled_exit_code == 0

    @pytest.mark.parametrize("bad", [1, 2, 127, -1, 255])
    def test_rejects_non_zero(self, bad: int) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_impl_payload(compiled_exit_code=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())


class TestImplTemplateTimeComplexity:
    @pytest.mark.parametrize(
        "good",
        [
            "O(1)",
            "O(n)",
            "O(n log n)",
            "O(n^2)",
            "O(n!)",
            "O(n*log(n))",
            "Θ(n)",
            "Ω(log n)",
            "o(n)",
            "ω(1)",
        ],
    )
    def test_accepts_canonical_big_o(self, good: str) -> None:
        t = ImplTemplate(**_impl_payload(time_complexity=good))
        assert t.time_complexity == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "fast",
            "n^2",
            "O",
            "O()",
            "O(",
            "Big-O(n)",
        ],
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_impl_payload(time_complexity=bad))
        codes = {e["type"] for e in exc.value.errors()}
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes

    def test_missing_field_reports_clear_loc(self) -> None:
        payload = _impl_payload()
        del payload["time_complexity"]
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert ("time_complexity",) in {tuple(e["loc"]) for e in exc.value.errors()}


class TestImplTemplateTargetTriple:
    @pytest.mark.parametrize(
        "triple",
        [
            "x86_64-pc-linux-gnu",
            "aarch64-vendor-linux",
            "aarch64-unknown-linux-gnu",
        ],
    )
    def test_accepts_canonical_triples(self, triple: str) -> None:
        t = ImplTemplate(**_impl_payload(target_triple=triple))
        assert t.target_triple == triple

    @pytest.mark.parametrize(
        "bad",
        ["", "x86_64", "x86_64-pc", "x86_64--pc-linux", "x86_64-pc-linux-gnu-x"],
    )
    def test_rejects_malformed_triples(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**_impl_payload(target_triple=bad))
        codes = {e["type"] for e in exc.value.errors()}
        assert codes & {"string_pattern_mismatch", "string_too_short"}, codes


class TestImplTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _impl_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned(self) -> None:
        payload = _impl_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            ImplTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_target_triple_grammar_matches_task_template(self) -> None:
        triples = ["x86_64-pc-linux-gnu", "aarch64-unknown-linux-gnu"]
        for triple in triples:
            i = ImplTemplate(**_impl_payload(target_triple=triple))
            t = TaskTemplate(**_task_payload(target_triple=triple))
            assert i.target_triple == t.target_triple == triple


# ══════════════════════════════════════════════════════════════════════════════
# 4. ReviewTemplate  (31 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestReviewTemplateHappyPath:
    def test_constructs_with_default_schema_version(self) -> None:
        t = ReviewTemplate(**_review_payload())
        assert t.schema_version == REVIEW_SCHEMA_VERSION == "1.0.0"

    def test_default_audit_type_is_advisory(self) -> None:
        t = ReviewTemplate(**_review_payload())
        assert t.audit_type == "advisory"

    def test_default_requires_human_signoff_is_true(self) -> None:
        t = ReviewTemplate(**_review_payload())
        assert t.requires_human_signoff is True

    def test_is_frozen(self) -> None:
        t = ReviewTemplate(**_review_payload())
        with pytest.raises(ValidationError):
            t.severity = "critical"  # type: ignore[misc]

    def test_strips_whitespace_on_string_fields(self) -> None:
        t = ReviewTemplate(**_review_payload(
            reviewer_id="  auditor-01  ",
            recommendation="  Apply patch.  ",
        ))
        assert t.reviewer_id == "auditor-01"
        assert t.recommendation == "Apply patch."

    def test_json_round_trip(self) -> None:
        original = ReviewTemplate(**_review_payload())
        rebuilt = ReviewTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt == original

    def test_both_disclaimer_fields_in_serialised_output(self) -> None:
        data = ReviewTemplate(**_review_payload()).model_dump()
        assert data["audit_type"] == "advisory"
        assert data["requires_human_signoff"] is True


class TestAuditTypeAuxiliaryDisclaimer:
    def test_accepts_advisory(self) -> None:
        t = ReviewTemplate(**_review_payload(audit_type="advisory"))
        assert t.audit_type == "advisory"

    @pytest.mark.parametrize("bad", ["authoritative", "blocking", "informational", ""])
    def test_rejects_non_advisory(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_review_payload(audit_type=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_audit_type_survives_json_round_trip(self) -> None:
        rebuilt = ReviewTemplate.model_validate_json(
            ReviewTemplate(**_review_payload()).model_dump_json()
        )
        assert rebuilt.audit_type == "advisory"


class TestRequiresHumanSignoffAuxiliaryDisclaimer:
    def test_accepts_true(self) -> None:
        t = ReviewTemplate(**_review_payload(requires_human_signoff=True))
        assert t.requires_human_signoff is True

    @pytest.mark.parametrize("bad", [False, None, 0, "true"])
    def test_rejects_non_true(self, bad: object) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_review_payload(requires_human_signoff=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_requires_human_signoff_in_json_output(self) -> None:
        data = ReviewTemplate(**_review_payload()).model_dump()
        assert data["requires_human_signoff"] is True


class TestReviewTemplateFindings:
    def test_single_finding_accepted(self) -> None:
        t = ReviewTemplate(**_review_payload(findings=["one finding"]))
        assert list(t.findings) == ["one finding"]

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_review_payload(findings=[]))
        assert any(
            e["type"] == "too_short" and tuple(e["loc"]) == ("findings",)
            for e in exc.value.errors()
        )

    def test_blank_entry_stripped_then_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReviewTemplate(**_review_payload(findings=["   "]))

    def test_findings_entries_are_stripped(self) -> None:
        t = ReviewTemplate(**_review_payload(findings=["  trimmed  "]))
        assert t.findings[0] == "trimmed"


class TestReviewTemplateSeverity:
    @pytest.mark.parametrize("sev", ["low", "medium", "high", "critical"])
    def test_accepts_all_valid_severities(self, sev: str) -> None:
        t = ReviewTemplate(**_review_payload(severity=sev))
        assert t.severity == sev

    @pytest.mark.parametrize("bad", ["info", "warning", "", "LOW", "CRITICAL"])
    def test_rejects_unlisted_severities(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**_review_payload(severity=bad))
        assert any(e["type"] == "literal_error" for e in exc.value.errors())


class TestReviewTemplateStrictness:
    def test_extra_fields_rejected(self) -> None:
        payload = _review_payload()
        payload["rogue"] = "nope"
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_schema_version_pinned(self) -> None:
        payload = _review_payload()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError) as exc:
            ReviewTemplate(**payload)
        assert any(e["type"] == "literal_error" for e in exc.value.errors())

    def test_disclaimer_invariant_survives_critical_review_round_trip(self) -> None:
        original = ReviewTemplate(**_review_payload(severity="critical"))
        rebuilt = ReviewTemplate.model_validate_json(original.model_dump_json())
        assert rebuilt.audit_type == "advisory"
        assert rebuilt.requires_human_signoff is True
        assert rebuilt.severity == "critical"


# ══════════════════════════════════════════════════════════════════════════════
# 5. Cognitive Load Scanner  (21 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestCognitiveLoadModuleConstants:
    def test_base_tokens_covers_all_sizes(self) -> None:
        assert set(_BASE_TOKENS.keys()) == {"S", "M", "XL"}

    def test_size_ordering_s_lt_m_lt_xl(self) -> None:
        assert _BASE_TOKENS["S"] < _BASE_TOKENS["M"] < _BASE_TOKENS["XL"]
        assert _FAN_OUT_BASE["S"] < _FAN_OUT_BASE["M"] < _FAN_OUT_BASE["XL"]

    def test_all_weights_positive(self) -> None:
        assert _FAN_IN_WEIGHT > 0
        assert _FAN_OUT_WEIGHT > 0
        assert _FAN_OUT_DEP_STEP > 0

    def test_mock_fraction_and_max_valid(self) -> None:
        assert 0.0 < _MOCK_FRACTION <= 1.0
        assert _MOCK_MAX > 0


class TestCognitiveLoadFanIn:
    def test_zero_deps_gives_fan_in_zero(self) -> None:
        r = scan_cognitive_load(_task_obj(allowed_dependencies=[]))
        assert r.fan_in == 0

    def test_fan_in_counts_all_entries_including_duplicates(self) -> None:
        r = scan_cognitive_load(_task_obj(allowed_dependencies=["a.py", "b.py", "a.py"]))
        assert r.fan_in == 3


class TestCognitiveLoadFanOut:
    @pytest.mark.parametrize("size", ["S", "M", "XL"])
    def test_no_deps_gives_base_fan_out(self, size: str) -> None:
        r = scan_cognitive_load(_task_obj(size=size, allowed_dependencies=[]))
        assert r.fan_out == _FAN_OUT_BASE[size]

    def test_fan_out_formula_exact_size_m_four_deps(self) -> None:
        deps = [f"d{i}.py" for i in range(4)]
        r = scan_cognitive_load(_task_obj(size="M", allowed_dependencies=deps))
        expected = _FAN_OUT_BASE["M"] + (4 // _FAN_OUT_DEP_STEP)
        assert r.fan_out == expected


class TestCognitiveLoadMockLimit:
    def test_zero_deps_gives_zero_mock_limit(self) -> None:
        r = scan_cognitive_load(_task_obj(allowed_dependencies=[]))
        assert r.mock_limit == 0

    def test_mock_limit_formula_two_deps(self) -> None:
        r = scan_cognitive_load(_task_obj(allowed_dependencies=["a.py", "b.py"]))
        assert r.mock_limit == min(math.ceil(2 * _MOCK_FRACTION), _MOCK_MAX)

    def test_mock_limit_capped_at_mock_max(self) -> None:
        deps = [f"d{i}.py" for i in range(12)]
        r = scan_cognitive_load(_task_obj(allowed_dependencies=deps))
        assert r.mock_limit == _MOCK_MAX

    def test_mock_limit_never_exceeds_fan_in(self) -> None:
        for n in range(8):
            deps = [f"d{i}.py" for i in range(n)]
            r = scan_cognitive_load(_task_obj(allowed_dependencies=deps))
            assert r.mock_limit <= r.fan_in


class TestCognitiveLoadExceedsCeiling:
    def test_exactly_at_ceiling_does_not_exceed(self) -> None:
        tokens = scan_cognitive_load(_task_obj(size="S", allowed_dependencies=[])).estimated_tokens
        r = scan_cognitive_load(
            _task_obj(size="S", allowed_dependencies=[], max_cognitive_load_tokens=tokens)
        )
        assert r.exceeds_ceiling is False

    def test_one_below_ceiling_exceeds(self) -> None:
        tokens = scan_cognitive_load(_task_obj(size="S", allowed_dependencies=[])).estimated_tokens
        r = scan_cognitive_load(
            _task_obj(size="S", allowed_dependencies=[], max_cognitive_load_tokens=tokens - 1)
        )
        assert r.exceeds_ceiling is True

    def test_tiny_ceiling_always_exceeds(self) -> None:
        r = scan_cognitive_load(
            _task_obj(size="S", allowed_dependencies=[], max_cognitive_load_tokens=1)
        )
        assert r.exceeds_ceiling is True

    def test_ceiling_reflected_in_report(self) -> None:
        r = scan_cognitive_load(_task_obj(max_cognitive_load_tokens=7777))
        assert r.ceiling == 7777


class TestCognitiveLoadReportInvariants:
    def test_report_is_frozen(self) -> None:
        r = scan_cognitive_load(_task_obj())
        with pytest.raises(ValidationError):
            r.fan_in = 999  # type: ignore[misc]

    def test_json_round_trip(self) -> None:
        r = scan_cognitive_load(_task_obj(size="M", allowed_dependencies=["spec.py", "impl.py"]))
        rebuilt = CognitiveLoadReport.model_validate_json(r.model_dump_json())
        assert rebuilt == r

    def test_size_reflected_from_task(self) -> None:
        for size in ["S", "M", "XL"]:
            r = scan_cognitive_load(_task_obj(size=size))
            assert r.size == size


# ══════════════════════════════════════════════════════════════════════════════
# 6. Cross-template alignment  (2 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestCrossTemplateAlignment:
    def test_all_four_schema_versions_are_one_zero_zero(self) -> None:
        assert (
            SPEC_SCHEMA_VERSION
            == TASK_SCHEMA_VERSION
            == IMPL_SCHEMA_VERSION
            == REVIEW_SCHEMA_VERSION
            == "1.0.0"
        )

    def test_target_triple_grammar_identical_in_task_and_impl(self) -> None:
        triples = ["x86_64-pc-linux-gnu", "aarch64-unknown-linux-gnu"]
        for triple in triples:
            i = ImplTemplate(**_impl_payload(target_triple=triple))
            t = TaskTemplate(**_task_payload(target_triple=triple))
            assert i.target_triple == t.target_triple == triple


# ══════════════════════════════════════════════════════════════════════════════
# 7. RLM Dispatch  (30 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestDecideDispatchModeRlm:
    def test_analysis_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "analysis") == "partition_map_summarize"

    def test_audit_above_threshold(self) -> None:
        assert decide_dispatch_mode(150_000, "audit") == "partition_map_summarize"

    def test_forensics_above_threshold(self) -> None:
        assert decide_dispatch_mode(500_000, "forensics") == "partition_map_summarize"

    def test_exactly_one_above_threshold(self) -> None:
        assert decide_dispatch_mode(100_001, "analysis") == "partition_map_summarize"


class TestDecideDispatchModeStandard:
    def test_at_threshold_not_rlm(self) -> None:
        assert decide_dispatch_mode(100_000, "analysis") == "standard"

    def test_below_threshold(self) -> None:
        assert decide_dispatch_mode(50_000, "analysis") == "standard"

    def test_crud_excluded_even_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "crud") == "standard"

    def test_retrieval_excluded_even_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "retrieval") == "standard"

    def test_unknown_type_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "summary") == "standard"

    def test_empty_type_above_threshold(self) -> None:
        assert decide_dispatch_mode(200_000, "") == "standard"


class TestTaskTypeSets:
    def test_rlm_types_exact(self) -> None:
        assert RLM_TASK_TYPES == frozenset({"analysis", "audit", "forensics"})

    def test_simple_types_exact(self) -> None:
        assert SIMPLE_TASK_TYPES == frozenset({"crud", "retrieval", "simple_lookup"})

    def test_sets_are_disjoint(self) -> None:
        assert not (RLM_TASK_TYPES & SIMPLE_TASK_TYPES)


class TestPartitionText:
    def test_reassembly_lossless(self) -> None:
        text = "hello world this is a test payload with some real content in it"
        parts = partition_text(text, 200_000)
        assert "".join(parts) == text

    def test_count_200k_tokens(self) -> None:
        parts = partition_text("x" * 1000, 200_000)
        assert len(parts) == 4

    def test_count_150k_tokens(self) -> None:
        parts = partition_text("x" * 1500, 150_000)
        assert len(parts) == 3

    def test_count_capped_at_max_partitions(self) -> None:
        parts = partition_text("x" * 8000, 1_000_000)
        assert len(parts) == MAX_PARTITIONS

    def test_empty_text_lossless(self) -> None:
        parts = partition_text("", 200_000)
        assert "".join(parts) == ""


class TestPlanDispatch:
    def test_rlm_mode_returned(self) -> None:
        plan = plan_dispatch(200_000, "analysis", "some long payload")
        assert plan.mode == "partition_map_summarize"

    def test_standard_mode_returned(self) -> None:
        plan = plan_dispatch(50_000, "analysis", "short payload")
        assert plan.mode == "standard"

    def test_rlm_partitions_nonempty(self) -> None:
        plan = plan_dispatch(200_000, "analysis", "payload text")
        assert len(plan.partitions) > 0

    def test_standard_partitions_empty(self) -> None:
        plan = plan_dispatch(50_000, "analysis", "payload text")
        assert plan.partitions == ()

    def test_rlm_depth_cap_is_one(self) -> None:
        plan = plan_dispatch(200_000, "audit", "payload")
        assert plan.depth_cap == DEPTH_CAP == 1

    def test_standard_depth_cap_is_zero(self) -> None:
        plan = plan_dispatch(50_000, "crud", "payload")
        assert plan.depth_cap == 0

    def test_context_tokens_preserved(self) -> None:
        plan = plan_dispatch(150_000, "forensics", "payload")
        assert plan.context_tokens == 150_000

    def test_task_type_preserved(self) -> None:
        plan = plan_dispatch(200_000, "audit", "payload")
        assert plan.task_type == "audit"


class TestFailOpen:
    def test_decide_fail_open_on_broken_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BrokenSet:
            def __contains__(self, item: object) -> bool:
                raise RuntimeError("simulated heuristic failure")

        monkeypatch.setattr(rlm_mod, "RLM_TASK_TYPES", _BrokenSet())
        result = rlm_mod.decide_dispatch_mode(200_000, "analysis")
        assert result == "standard"

    def test_plan_dispatch_fail_open_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bad_decide(*_args: object) -> str:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(rlm_mod, "decide_dispatch_mode", _bad_decide)
        plan = rlm_mod.plan_dispatch(200_000, "analysis", "payload")
        assert plan.mode == "standard"
        assert plan.partitions == ()
        assert plan.depth_cap == 0


class TestRlmDispatchPlanModel:
    def test_plan_is_frozen(self) -> None:
        plan = RlmDispatchPlan(
            mode="standard",
            partitions=(),
            depth_cap=0,
            context_tokens=50_000,
            task_type="analysis",
        )
        with pytest.raises(Exception):
            plan.mode = "partition_map_summarize"  # type: ignore[misc]

    def test_json_roundtrip(self) -> None:
        plan = plan_dispatch(200_000, "audit", "sample payload for testing")
        restored = RlmDispatchPlan.model_validate_json(plan.model_dump_json())
        assert restored == plan

    def test_constants_types_are_frozensets_and_ints(self) -> None:
        assert isinstance(RLM_TASK_TYPES, frozenset)
        assert isinstance(SIMPLE_TASK_TYPES, frozenset)
        assert isinstance(CONTEXT_TOKENS_THRESHOLD, int)
        assert isinstance(PARTITION_SIZE_TOKENS, int)
        assert isinstance(MAX_PARTITIONS, int)
        assert isinstance(DEPTH_CAP, int)

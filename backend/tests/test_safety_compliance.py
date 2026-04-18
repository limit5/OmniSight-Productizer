"""C9 — L4-CORE-09 Safety & compliance framework tests (#223).

Covers:
  - Safety standard config loading + parsing
  - Level normalisation (shorthand → canonical)
  - DAG safety gate validation (pass + fail paths)
  - Artifact missing detection
  - Task type extraction from DAG
  - Multi-standard checks
  - Edge cases (unknown standard, unknown level, empty DAG)
  - Doc suite generator integration (get_safety_certs)
  - Audit log integration
  - REST endpoint smoke tests
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.dag_schema import DAG, Task
from backend.safety_compliance import (
    GateFinding,
    GateVerdict,
    SafetyGateResult,
    SafetyStandard,
    check_all_standards,
    check_compliance,
    clear_safety_certs,
    get_artifact_definition,
    get_safety_certs,
    get_standard,
    list_artifact_definitions,
    list_standards,
    log_safety_gate_result,
    register_safety_cert,
    reload_standards_for_tests,
    validate_safety_gate,
    _extract_task_types,
    _normalize_level,
)


# ── Fixtures ─────────────────────────────────────────────────────────

def _make_dag(tasks: list[Task] | None = None, dag_id: str = "test-dag") -> DAG:
    if tasks is None:
        tasks = [
            Task(
                task_id="placeholder",
                description="placeholder task",
                required_tier="t1",
                toolchain="cmake",
                expected_output="build/out.bin",
            ),
        ]
    return DAG(dag_id=dag_id, tasks=tasks)


def _make_full_safety_dag() -> DAG:
    return DAG(
        dag_id="safety-full",
        tasks=[
            Task(task_id="static-analysis", description="Run static_analysis with SAST tools",
                 required_tier="t1", toolchain="cmake", expected_output="reports/sast.xml"),
            Task(task_id="unit-tests", description="Execute unit_test suite",
                 required_tier="t1", toolchain="cmake", expected_output="reports/unit.xml"),
            Task(task_id="integ-tests", description="Run integration_test suite",
                 required_tier="t1", toolchain="cmake", expected_output="reports/integ.xml",
                 depends_on=["unit-tests"]),
            Task(task_id="review", description="Mandatory code_review gate",
                 required_tier="t1", toolchain="cmake", expected_output="reports/review.md",
                 depends_on=["static-analysis"]),
            Task(task_id="coverage", description="coverage_analysis and mc_dc_coverage report",
                 required_tier="t1", toolchain="cmake", expected_output="reports/coverage.xml",
                 depends_on=["unit-tests"]),
            Task(task_id="runtime-check", description="runtime_verification pass",
                 required_tier="t1", toolchain="cmake", expected_output="reports/runtime.xml"),
            Task(task_id="formal-proof", description="formal_verification of safety properties",
                 required_tier="t1", toolchain="cmake", expected_output="reports/formal.xml"),
            Task(task_id="fault-inject", description="fault_injection_test campaign",
                 required_tier="t1", toolchain="cmake", expected_output="reports/faults.xml"),
            Task(task_id="regression", description="regression_test suite",
                 required_tier="t1", toolchain="cmake", expected_output="reports/regression.xml"),
            Task(task_id="pentest", description="penetration_test scan",
                 required_tier="t1", toolchain="cmake", expected_output="reports/pentest.xml"),
        ],
    )


ALL_ARTIFACTS = [
    "hazard_analysis", "risk_assessment", "risk_file",
    "software_classification", "traceability_matrix",
    "fmea", "fta", "safety_plan", "safety_case",
    "software_architecture_doc", "software_plan",
    "anomaly_list", "test_plan",
    "configuration_management_plan", "formal_verification_report",
    "independence_assessment", "dependent_failure_analysis",
    "cybersecurity_assessment", "clinical_evaluation",
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loading tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigLoading:
    def setup_method(self):
        reload_standards_for_tests()

    def test_list_standards_returns_four(self):
        stds = list_standards()
        assert len(stds) == 4
        ids = {s.standard_id for s in stds}
        assert ids == {"iso26262", "iec60601", "do178", "iec61508"}

    def test_get_standard_iso26262(self):
        std = get_standard("iso26262")
        assert std is not None
        assert std.name == "ISO 26262"
        assert std.domain == "Automotive functional safety"
        assert len(std.levels) == 4
        assert std.level_ids == ["ASIL_A", "ASIL_B", "ASIL_C", "ASIL_D"]

    def test_get_standard_iec60601(self):
        std = get_standard("iec60601")
        assert std is not None
        assert len(std.levels) == 3
        assert std.level_ids == ["SW_A", "SW_B", "SW_C"]

    def test_get_standard_do178(self):
        std = get_standard("do178")
        assert std is not None
        assert len(std.levels) == 5
        assert std.level_ids == ["DAL_E", "DAL_D", "DAL_C", "DAL_B", "DAL_A"]

    def test_get_standard_iec61508(self):
        std = get_standard("iec61508")
        assert std is not None
        assert len(std.levels) == 4
        assert std.level_ids == ["SIL_1", "SIL_2", "SIL_3", "SIL_4"]

    def test_get_standard_unknown_returns_none(self):
        assert get_standard("nonexistent") is None

    def test_level_required_artifacts_populated(self):
        std = get_standard("iso26262")
        asil_d = std.get_level("ASIL_D")
        assert asil_d is not None
        assert "hazard_analysis" in asil_d.required_artifacts
        assert "formal_verification_report" in asil_d.required_artifacts
        assert len(asil_d.required_artifacts) == 10

    def test_level_required_dag_tasks_populated(self):
        std = get_standard("iso26262")
        asil_d = std.get_level("ASIL_D")
        assert asil_d is not None
        assert "formal_verification" in asil_d.required_dag_tasks
        assert "fault_injection_test" in asil_d.required_dag_tasks

    def test_asil_a_no_review_required(self):
        std = get_standard("iso26262")
        asil_a = std.get_level("ASIL_A")
        assert asil_a.review_required is False

    def test_asil_d_review_required(self):
        std = get_standard("iso26262")
        asil_d = std.get_level("ASIL_D")
        assert asil_d.review_required is True

    def test_list_artifact_definitions(self):
        arts = list_artifact_definitions()
        assert len(arts) >= 15
        ids = {a.artifact_id for a in arts}
        assert "hazard_analysis" in ids
        assert "traceability_matrix" in ids

    def test_get_artifact_definition(self):
        art = get_artifact_definition("hazard_analysis")
        assert art is not None
        assert art.name == "Hazard Analysis"
        assert "hazard" in art.file_pattern

    def test_get_artifact_definition_unknown(self):
        assert get_artifact_definition("nonexistent") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Level normalisation tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLevelNormalisation:
    @pytest.mark.parametrize("inp,expected", [
        ("B", "ASIL_B"),
        ("b", "ASIL_B"),
        ("ASIL_B", "ASIL_B"),
        ("asil_b", "ASIL_B"),
        ("asil-b", "ASIL_B"),
        ("D", "ASIL_D"),
    ])
    def test_iso26262_shorthand(self, inp, expected):
        assert _normalize_level("iso26262", inp) == expected

    @pytest.mark.parametrize("inp,expected", [
        ("C", "SW_C"),
        ("sw_c", "SW_C"),
        ("SW_A", "SW_A"),
    ])
    def test_iec60601_shorthand(self, inp, expected):
        assert _normalize_level("iec60601", inp) == expected

    @pytest.mark.parametrize("inp,expected", [
        ("A", "DAL_A"),
        ("dal_a", "DAL_A"),
        ("E", "DAL_E"),
    ])
    def test_do178_shorthand(self, inp, expected):
        assert _normalize_level("do178", inp) == expected

    @pytest.mark.parametrize("inp,expected", [
        ("1", "SIL_1"),
        ("4", "SIL_4"),
        ("sil_3", "SIL_3"),
        ("sil-2", "SIL_2"),
    ])
    def test_iec61508_shorthand(self, inp, expected):
        assert _normalize_level("iec61508", inp) == expected

    def test_unknown_standard_passthrough(self):
        assert _normalize_level("unknown", "X") == "X"

    def test_unknown_level_passthrough(self):
        assert _normalize_level("iso26262", "ASIL_Z") == "ASIL_Z"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Task type extraction tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTaskTypeExtraction:
    def test_extracts_from_task_id(self):
        dag = _make_dag([
            Task(task_id="static-analysis", description="run tools",
                 required_tier="t1", toolchain="cmake", expected_output="out/sa.xml"),
        ])
        types = _extract_task_types(dag)
        assert "static_analysis" in types

    def test_extracts_from_description(self):
        dag = _make_dag([
            Task(task_id="step1", description="perform unit_test on all modules",
                 required_tier="t1", toolchain="cmake", expected_output="out/ut.xml"),
        ])
        types = _extract_task_types(dag)
        assert "unit_test" in types

    def test_extracts_lint_as_static_analysis(self):
        dag = _make_dag([
            Task(task_id="lint-step", description="run lint checks",
                 required_tier="t1", toolchain="cmake", expected_output="out/lint.xml"),
        ])
        types = _extract_task_types(dag)
        assert "static_analysis" in types

    def test_extracts_multiple_types(self):
        dag = _make_full_safety_dag()
        types = _extract_task_types(dag)
        assert "static_analysis" in types
        assert "unit_test" in types
        assert "integration_test" in types
        assert "code_review" in types
        assert "coverage_analysis" in types
        assert "runtime_verification" in types
        assert "formal_verification" in types
        assert "fault_injection_test" in types

    def test_empty_dag_returns_empty_set(self):
        dag = _make_dag([
            Task(task_id="build", description="compile the firmware",
                 required_tier="t1", toolchain="cmake", expected_output="out/fw.bin"),
        ])
        types = _extract_task_types(dag)
        assert "static_analysis" not in types
        assert "unit_test" not in types


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Safety gate validation — pass paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafetyGatePass:
    def setup_method(self):
        reload_standards_for_tests()

    def test_iso26262_asil_a_full_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "A", ALL_ARTIFACTS)
        assert result.passed is True
        assert result.verdict == GateVerdict.passed
        assert result.missing_artifacts == []
        assert result.missing_tasks == []

    def test_iso26262_asil_b_full_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "B", ALL_ARTIFACTS)
        assert result.passed is True

    def test_iso26262_asil_d_full_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "ASIL_D", ALL_ARTIFACTS)
        assert result.passed is True

    def test_iec60601_sw_a_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iec60601", "A", ALL_ARTIFACTS)
        assert result.passed is True

    def test_iec60601_sw_c_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iec60601", "C", ALL_ARTIFACTS)
        assert result.passed is True

    def test_do178_dal_e_pass(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "do178", "E", ["software_classification"])
        assert result.passed is True

    def test_do178_dal_a_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "do178", "A", ALL_ARTIFACTS)
        assert result.passed is True

    def test_iec61508_sil_1_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iec61508", "1", ALL_ARTIFACTS)
        assert result.passed is True

    def test_iec61508_sil_4_pass(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iec61508", "4", ALL_ARTIFACTS)
        assert result.passed is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Safety gate validation — fail paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafetyGateFail:
    def setup_method(self):
        reload_standards_for_tests()

    def test_iso26262_asil_b_missing_artifacts(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "B", [])
        assert result.passed is False
        assert result.verdict == GateVerdict.failed
        assert "hazard_analysis" in result.missing_artifacts
        assert "traceability_matrix" in result.missing_artifacts

    def test_iso26262_asil_b_missing_tasks(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "iso26262", "B", ALL_ARTIFACTS)
        assert result.passed is False
        assert "static_analysis" in result.missing_tasks
        assert "integration_test" in result.missing_tasks

    def test_iso26262_asil_d_missing_formal(self):
        dag = _make_dag([
            Task(task_id="sa", description="static_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/sa.xml"),
            Task(task_id="ut", description="unit_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/ut.xml"),
        ])
        result = validate_safety_gate(dag, "iso26262", "D", ALL_ARTIFACTS)
        assert result.passed is False
        assert "formal_verification" in result.missing_tasks

    def test_iec60601_sw_c_missing_pentest(self):
        dag = _make_dag([
            Task(task_id="sa", description="static_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/sa.xml"),
            Task(task_id="ut", description="unit_test suite",
                 required_tier="t1", toolchain="cmake", expected_output="out/ut.xml"),
            Task(task_id="it", description="integration_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/it.xml"),
            Task(task_id="cr", description="code_review",
                 required_tier="t1", toolchain="cmake", expected_output="out/cr.md"),
            Task(task_id="rg", description="regression_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/rg.xml"),
            Task(task_id="cv", description="coverage_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/cv.xml"),
            Task(task_id="fi", description="fault_injection_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/fi.xml"),
        ])
        result = validate_safety_gate(dag, "iec60601", "C", ALL_ARTIFACTS)
        assert result.passed is False
        assert "penetration_test" in result.missing_tasks

    def test_do178_dal_a_missing_mc_dc(self):
        dag = _make_dag([
            Task(task_id="sa", description="static_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/sa.xml"),
            Task(task_id="ut", description="unit_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/ut.xml"),
            Task(task_id="it", description="integration_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/it.xml"),
            Task(task_id="cr", description="code_review",
                 required_tier="t1", toolchain="cmake", expected_output="out/cr.md"),
            Task(task_id="cv", description="coverage_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/cv.xml"),
            Task(task_id="rv", description="runtime_verification",
                 required_tier="t1", toolchain="cmake", expected_output="out/rv.xml"),
            Task(task_id="fv", description="formal_verification",
                 required_tier="t1", toolchain="cmake", expected_output="out/fv.xml"),
            Task(task_id="fi", description="fault_injection_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/fi.xml"),
        ])
        result = validate_safety_gate(dag, "do178", "A", ALL_ARTIFACTS)
        assert result.passed is False
        assert "mc_dc_coverage" in result.missing_tasks

    def test_partial_artifacts_detected(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(
            dag, "iso26262", "B",
            ["hazard_analysis", "risk_assessment"],
        )
        assert result.passed is False
        assert "software_classification" in result.missing_artifacts
        assert "traceability_matrix" in result.missing_artifacts
        assert "hazard_analysis" not in result.missing_artifacts

    def test_review_required_finding(self):
        dag = _make_dag([
            Task(task_id="sa", description="static_analysis",
                 required_tier="t1", toolchain="cmake", expected_output="out/sa.xml"),
            Task(task_id="ut", description="unit_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/ut.xml"),
            Task(task_id="it", description="integration_test",
                 required_tier="t1", toolchain="cmake", expected_output="out/it.xml"),
        ])
        result = validate_safety_gate(dag, "iso26262", "B", ALL_ARTIFACTS)
        assert result.passed is False
        review_findings = [
            f for f in result.findings if f.item == "review_required"
        ]
        assert len(review_findings) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Error paths
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafetyGateErrors:
    def setup_method(self):
        reload_standards_for_tests()

    def test_unknown_standard(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "bogus", "A")
        assert result.verdict == GateVerdict.error
        assert len(result.findings) == 1
        assert "Unknown safety standard" in result.findings[0].message

    def test_unknown_level(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "iso26262", "ASIL_Z")
        assert result.verdict == GateVerdict.error
        assert "Unknown level" in result.findings[0].message

    def test_none_artifacts_treated_as_empty(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "A", None)
        assert result.passed is False
        assert len(result.missing_artifacts) > 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SafetyGateResult model tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafetyGateResultModel:
    def test_to_dict_structure(self):
        result = SafetyGateResult(
            standard="iso26262",
            level="ASIL_B",
            verdict=GateVerdict.passed,
        )
        d = result.to_dict()
        assert d["standard"] == "iso26262"
        assert d["level"] == "ASIL_B"
        assert d["verdict"] == "passed"
        assert d["passed"] is True
        assert d["total_issues"] == 0

    def test_to_dict_with_issues(self):
        result = SafetyGateResult(
            standard="iso26262",
            level="ASIL_B",
            verdict=GateVerdict.failed,
            missing_artifacts=["hazard_analysis"],
            missing_tasks=["unit_test"],
            findings=[GateFinding("process", "x", "msg")],
        )
        d = result.to_dict()
        assert d["passed"] is False
        assert d["total_issues"] == 3
        assert len(d["findings"]) == 1
        assert d["findings"][0]["category"] == "process"

    def test_summary_passed(self):
        result = SafetyGateResult(
            standard="iso26262", level="ASIL_A",
            verdict=GateVerdict.passed,
        )
        assert "PASSED" in result.summary()

    def test_summary_failed(self):
        result = SafetyGateResult(
            standard="iso26262", level="ASIL_B",
            verdict=GateVerdict.failed,
            missing_artifacts=["x"],
            missing_tasks=["y", "z"],
        )
        s = result.summary()
        assert "FAILED" in s
        assert "1 missing artifact" in s
        assert "2 missing task type" in s

    def test_total_issues(self):
        result = SafetyGateResult(
            standard="test", level="L1",
            verdict=GateVerdict.failed,
            missing_artifacts=["a", "b"],
            missing_tasks=["c"],
            findings=[GateFinding("x", "y", "z")],
        )
        assert result.total_issues == 4


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  check_compliance alias
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCheckComplianceAlias:
    def setup_method(self):
        reload_standards_for_tests()

    def test_check_compliance_same_as_validate(self):
        dag = _make_full_safety_dag()
        r1 = validate_safety_gate(dag, "iso26262", "B", ALL_ARTIFACTS)
        r2 = check_compliance(dag, "iso26262", "B", ALL_ARTIFACTS)
        assert r1.verdict == r2.verdict
        assert r1.missing_artifacts == r2.missing_artifacts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Multi-standard checks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestMultiStandardCheck:
    def setup_method(self):
        reload_standards_for_tests()

    def test_multi_all_pass(self):
        dag = _make_full_safety_dag()
        results = check_all_standards(dag, [
            {"standard": "iso26262", "level": "A"},
            {"standard": "iec61508", "level": "1"},
        ], ALL_ARTIFACTS)
        assert len(results) == 2
        assert all(r.passed for r in results)

    def test_multi_mixed_results(self):
        dag = _make_full_safety_dag()
        results = check_all_standards(dag, [
            {"standard": "iso26262", "level": "A"},
            {"standard": "iso26262", "level": "D"},
        ], [])
        assert results[0].passed is False
        assert results[1].passed is False

    def test_multi_empty_requirements(self):
        dag = _make_dag()
        results = check_all_standards(dag, [], ALL_ARTIFACTS)
        assert results == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Doc suite generator integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDocSuiteIntegration:
    def setup_method(self):
        clear_safety_certs()

    def test_get_safety_certs_empty(self):
        assert get_safety_certs() == []

    def test_register_and_get(self):
        register_safety_cert("iso26262", "ASIL_B", status="Passed", cert_id="CERT-001")
        certs = get_safety_certs()
        assert len(certs) == 1
        assert certs[0]["standard"] == "iso26262 ASIL_B"
        assert certs[0]["status"] == "Passed"
        assert certs[0]["cert_id"] == "CERT-001"

    def test_register_multiple(self):
        register_safety_cert("iso26262", "ASIL_B")
        register_safety_cert("iec60601", "SW_C")
        assert len(get_safety_certs()) == 2

    def test_clear_certs(self):
        register_safety_cert("iso26262", "ASIL_A")
        clear_safety_certs()
        assert get_safety_certs() == []

    def teardown_method(self):
        clear_safety_certs()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Audit log integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAuditIntegration:
    @pytest.mark.asyncio
    async def test_log_safety_gate_result_calls_audit(self):
        result = SafetyGateResult(
            standard="iso26262", level="ASIL_B",
            verdict=GateVerdict.passed,
        )
        mock_log = AsyncMock(return_value=42)
        with patch("backend.safety_compliance.audit", create=True) as mock_audit:
            mock_audit.log = mock_log
            with patch.dict("sys.modules", {"backend.audit": mock_audit}):
                row_id = await log_safety_gate_result(result)
        assert row_id == 42

    @pytest.mark.asyncio
    async def test_log_safety_gate_result_handles_error(self):
        result = SafetyGateResult(
            standard="iso26262", level="ASIL_B",
            verdict=GateVerdict.failed,
        )
        with patch("backend.safety_compliance.audit", create=True) as mock_audit:
            mock_audit.log = AsyncMock(side_effect=RuntimeError("db error"))
            with patch.dict("sys.modules", {"backend.audit": mock_audit}):
                row_id = await log_safety_gate_result(result)
        assert row_id is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Enum tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEnums:
    def test_safety_standard_values(self):
        assert SafetyStandard.iso26262.value == "iso26262"
        assert SafetyStandard.iec60601.value == "iec60601"
        assert SafetyStandard.do178.value == "do178"
        assert SafetyStandard.iec61508.value == "iec61508"

    def test_gate_verdict_values(self):
        assert GateVerdict.passed.value == "passed"
        assert GateVerdict.failed.value == "failed"
        assert GateVerdict.error.value == "error"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Edge cases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestEdgeCases:
    def setup_method(self):
        reload_standards_for_tests()

    def test_do178_dal_e_minimal(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "do178", "E", ["software_classification"])
        assert result.passed is True

    def test_do178_dal_e_no_artifacts(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "do178", "E", [])
        assert result.passed is False
        assert "software_classification" in result.missing_artifacts

    def test_metadata_contains_detected_types(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(dag, "iso26262", "A", ALL_ARTIFACTS)
        assert "detected_task_types" in result.metadata
        assert "static_analysis" in result.metadata["detected_task_types"]

    def test_metadata_contains_provided_artifacts(self):
        dag = _make_dag()
        result = validate_safety_gate(dag, "iso26262", "A", ["hazard_analysis"])
        assert "provided_artifacts" in result.metadata
        assert "hazard_analysis" in result.metadata["provided_artifacts"]

    def test_duplicate_artifacts_no_double_count(self):
        dag = _make_full_safety_dag()
        result = validate_safety_gate(
            dag, "iso26262", "A",
            ["hazard_analysis", "hazard_analysis", "risk_assessment",
             "software_classification", "traceability_matrix"],
        )
        assert result.passed is True

    def test_iso26262_levels_increasing_strictness(self):
        dag = _make_dag()
        results = []
        for level in ["A", "B", "C", "D"]:
            r = validate_safety_gate(dag, "iso26262", level, [])
            results.append(r)
        issue_counts = [r.total_issues for r in results]
        for i in range(len(issue_counts) - 1):
            assert issue_counts[i] <= issue_counts[i + 1]

    def test_iec61508_levels_increasing_strictness(self):
        dag = _make_dag()
        results = []
        for level in ["1", "2", "3", "4"]:
            r = validate_safety_gate(dag, "iec61508", level, [])
            results.append(r)
        issue_counts = [r.total_issues for r in results]
        for i in range(len(issue_counts) - 1):
            assert issue_counts[i] <= issue_counts[i + 1]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  REST endpoint smoke tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafetyEndpoints:
    @pytest.mark.asyncio
    async def test_list_standards(self, client):
        resp = await client.get("/api/v1/safety/standards")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 4
        ids = {s["standard_id"] for s in data["items"]}
        assert "iso26262" in ids

    @pytest.mark.asyncio
    async def test_get_standard(self, client):
        resp = await client.get("/api/v1/safety/standards/iso26262")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ISO 26262"
        assert len(data["levels"]) == 4

    @pytest.mark.asyncio
    async def test_get_standard_404(self, client):
        resp = await client.get("/api/v1/safety/standards/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_artifacts(self, client):
        resp = await client.get("/api/v1/safety/artifacts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 15

    @pytest.mark.asyncio
    async def test_check_compliance_pass(self, client):
        resp = await client.post("/api/v1/safety/check", json={
            "standard": "do178",
            "level": "E",
            "artifacts": ["software_classification"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "passed"

    @pytest.mark.asyncio
    async def test_check_compliance_fail(self, client):
        resp = await client.post("/api/v1/safety/check", json={
            "standard": "iso26262",
            "level": "D",
            "artifacts": [],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "failed"
        assert len(data["missing_artifacts"]) > 0

    @pytest.mark.asyncio
    async def test_check_compliance_unknown_standard(self, client):
        resp = await client.post("/api/v1/safety/check", json={
            "standard": "bogus",
            "level": "A",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "error"

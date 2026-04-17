"""X1 #297 — unit tests for `backend.software_simulator`.

Exercises the Python library independently of the shell layer. The
integration with `scripts/simulate.sh` is covered separately by
`test_software_simulate.py`. Everything here runs under pure pytest
with temp dirs; no network, no external binaries required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import software_simulator as ss


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Language autodetect
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolveLanguage:
    @pytest.mark.parametrize("marker,expected", [
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("setup.cfg", "python"),
        ("requirements.txt", "python"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("pom.xml", "java"),
        ("build.gradle", "java"),
        ("build.gradle.kts", "java"),
        ("package.json", "node"),
    ])
    def test_single_marker(self, tmp_path: Path, marker: str, expected: str):
        (tmp_path / marker).write_text("# fixture")
        assert ss.resolve_language(tmp_path) == expected

    def test_csharp_csproj(self, tmp_path: Path):
        (tmp_path / "App.csproj").write_text("<Project/>")
        assert ss.resolve_language(tmp_path) == "csharp"

    def test_csharp_sln(self, tmp_path: Path):
        (tmp_path / "App.sln").write_text("")
        assert ss.resolve_language(tmp_path) == "csharp"

    def test_specific_before_generic(self, tmp_path: Path):
        # pyproject.toml should win over requirements.txt in a
        # mixed setup.
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "requirements.txt").write_text("pytest")
        assert ss.resolve_language(tmp_path) == "python"

    def test_missing_returns_empty(self, tmp_path: Path):
        assert ss.resolve_language(tmp_path) == ""

    def test_nonexistent_dir(self, tmp_path: Path):
        assert ss.resolve_language(tmp_path / "noexist") == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Threshold mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestThresholds:
    def test_x1_thresholds_pinned(self):
        # Pin the X1 ticket values so nobody silently softens them.
        assert ss.COVERAGE_THRESHOLDS["python"] == 80.0
        assert ss.COVERAGE_THRESHOLDS["go"] == 70.0
        assert ss.COVERAGE_THRESHOLDS["rust"] == 75.0
        assert ss.COVERAGE_THRESHOLDS["java"] == 70.0
        assert ss.COVERAGE_THRESHOLDS["node"] == 80.0
        assert ss.COVERAGE_THRESHOLDS["csharp"] == 70.0

    def test_override_wins(self):
        assert ss._threshold_for("python", 42.0) == 42.0

    def test_unknown_language_zero(self):
        assert ss._threshold_for("cobol", None) == 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Output parsers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPytestParser:
    def test_standard_summary(self):
        out = "===== 12 passed, 2 failed, 1 skipped in 0.42s ====="
        total, passed, failed = ss._parse_pytest_output(out)
        assert passed == 12
        assert failed == 2
        assert total == 15

    def test_error_is_failure(self):
        out = "===== 3 errors, 1 passed in 0.12s ====="
        _total, passed, failed = ss._parse_pytest_output(out)
        assert passed == 1
        assert failed == 3

    def test_empty(self):
        assert ss._parse_pytest_output("") == (0, 0, 0)


class TestGoParser:
    def test_json_pass_fail_mix(self):
        lines = [
            '{"Action":"run","Test":"TestOne"}',
            '{"Action":"pass","Test":"TestOne"}',
            '{"Action":"run","Test":"TestTwo"}',
            '{"Action":"fail","Test":"TestTwo"}',
            '{"Action":"pass","Test":"TestThree"}',
        ]
        total, passed, failed = ss._parse_go_json("\n".join(lines))
        assert passed == 2
        assert failed == 1
        assert total == 3

    def test_empty_or_non_json(self):
        assert ss._parse_go_json("FAIL\n") == (0, 0, 0)


class TestCargoParser:
    def test_single_block(self):
        out = "test result: ok. 14 passed; 0 failed; 0 ignored; finished in 0.02s"
        total, passed, failed = ss._parse_cargo_output(out)
        assert passed == 14 and failed == 0 and total == 14

    def test_multiple_blocks(self):
        # cargo emits one block per crate.
        out = (
            "test result: ok. 3 passed; 0 failed; 0 ignored\n"
            "test result: FAILED. 1 passed; 2 failed; 0 ignored\n"
        )
        total, passed, failed = ss._parse_cargo_output(out)
        assert passed == 4 and failed == 2 and total == 6


class TestMavenParser:
    def test_summary(self):
        out = "Tests run: 10, Failures: 1, Errors: 1, Skipped: 2"
        total, passed, failed = ss._parse_maven_output(out)
        assert total == 8  # 10 - 2 skipped
        assert failed == 2
        assert passed == 6


class TestNodeParser:
    def test_jest(self):
        out = "Tests:       2 failed, 5 passed, 7 total"
        total, passed, failed = ss._parse_node_output(out)
        assert total == 7 and passed == 5 and failed == 2

    def test_vitest(self):
        out = "Tests  12 passed | 1 failed"
        total, passed, failed = ss._parse_node_output(out)
        assert passed == 12 and failed == 1 and total == 13

    def test_mocha(self):
        out = "10 passing (23ms)\n2 failing\n"
        total, passed, failed = ss._parse_node_output(out)
        assert passed == 10 and failed == 2 and total == 12

    def test_nothing(self):
        assert ss._parse_node_output("no match") == (0, 0, 0)


class TestDotnetParser:
    def test_summary_line(self):
        out = "Passed!  - Failed:  0, Passed:  7, Skipped:  0, Total:  7"
        total, passed, failed = ss._parse_dotnet_output(out)
        assert passed == 7 and failed == 0 and total == 7


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Coverage parsers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCoverageParsers:
    def test_python_coverage(self):
        out = "Name         Stmts   Miss  Cover\n" \
              "----------------------------------\n" \
              "TOTAL         100     15    85%\n"
        assert ss._parse_python_coverage(out) == 85.0

    def test_python_coverage_decimal(self):
        out = "TOTAL        200    10   95.5%\n"
        assert ss._parse_python_coverage(out) == 95.5

    def test_go_coverage(self):
        out = "foo.go:1:  f1  100.0%\n" \
              "total:          (statements)         77.3%\n"
        assert ss._parse_go_coverage(out) == 77.3

    def test_rust_llvm_cov(self):
        out = "TOTAL                                        81.23%\n"
        assert ss._parse_rust_coverage(out) == 81.23


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  JaCoCo / cobertura XML fixture parsers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_JACOCO_XML = """<?xml version='1.0' encoding='UTF-8'?>
<report name='x'>
  <counter type='LINE' missed='20' covered='80'/>
  <counter type='BRANCH' missed='5' covered='45'/>
</report>
"""


class TestJaCoCoParse:
    def test_line_coverage_from_xml(self, tmp_path: Path):
        (tmp_path / "target").mkdir()
        (tmp_path / "target" / "site").mkdir()
        (tmp_path / "target" / "site" / "jacoco").mkdir()
        (tmp_path / "target" / "site" / "jacoco" / "jacoco.xml").write_text(_JACOCO_XML)
        rep = ss._coverage_java(tmp_path, threshold=70.0)
        assert rep.source == "jacoco"
        assert rep.percentage == 80.0  # 80 / (80 + 20)
        assert rep.status == "pass"

    def test_missing_report_degrades_to_mock(self, tmp_path: Path):
        rep = ss._coverage_java(tmp_path, threshold=70.0)
        assert rep.status == "mock"


_COBERTURA_XML = """<?xml version='1.0'?>
<coverage line-rate='0.82' branch-rate='0.73' version='1.0' timestamp='1'>
  <sources/><packages/>
</coverage>
"""


class TestCoberturaParse:
    def test_csharp_line_rate(self, tmp_path: Path):
        results = tmp_path / "TestResults" / "run-abc"
        results.mkdir(parents=True)
        (results / "coverage.cobertura.xml").write_text(_COBERTURA_XML)
        rep = ss._coverage_csharp(tmp_path, threshold=70.0)
        assert rep.source == "coverlet"
        assert rep.percentage == pytest.approx(82.0)
        assert rep.status == "pass"

    def test_missing_report_mock(self, tmp_path: Path):
        rep = ss._coverage_csharp(tmp_path, threshold=70.0)
        assert rep.status == "mock"


class TestNodeCoverageSummary:
    def test_valid_summary(self, tmp_path: Path):
        (tmp_path / "coverage").mkdir()
        (tmp_path / "coverage" / "coverage-summary.json").write_text(json.dumps({
            "total": {"lines": {"pct": 83.4}},
        }))
        rep = ss._coverage_node(tmp_path, threshold=80.0, timeout=1)
        assert rep.source == "c8/istanbul"
        assert rep.percentage == 83.4
        assert rep.status == "pass"

    def test_below_threshold(self, tmp_path: Path):
        (tmp_path / "coverage").mkdir()
        (tmp_path / "coverage" / "coverage-summary.json").write_text(json.dumps({
            "total": {"lines": {"pct": 50.0}},
        }))
        rep = ss._coverage_node(tmp_path, threshold=80.0, timeout=1)
        assert rep.status == "fail"

    def test_missing_summary_mock(self, tmp_path: Path):
        rep = ss._coverage_node(tmp_path, threshold=80.0, timeout=1)
        assert rep.status == "mock"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Benchmark regression
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBenchmarkRegression:
    def _write_baseline(self, workspace: Path, module: str, ms: float) -> None:
        bench = workspace / "test_assets" / "benchmarks"
        bench.mkdir(parents=True, exist_ok=True)
        (bench / f"{module}.json").write_text(json.dumps({"baseline_ms": ms}))

    def test_missing_baseline_skip(self, tmp_path: Path):
        rep = ss.run_benchmark_regression(
            tmp_path, module="nope", workspace=tmp_path, current_ms=10.0,
        )
        assert rep.status == "skip"

    def test_within_threshold_pass(self, tmp_path: Path):
        self._write_baseline(tmp_path, "mod-a", 100.0)
        rep = ss.run_benchmark_regression(
            tmp_path, module="mod-a", workspace=tmp_path, current_ms=105.0,
            threshold_pct=10.0,
        )
        assert rep.status == "pass"
        assert rep.regression_pct == pytest.approx(5.0)

    def test_over_threshold_fail(self, tmp_path: Path):
        self._write_baseline(tmp_path, "mod-b", 100.0)
        rep = ss.run_benchmark_regression(
            tmp_path, module="mod-b", workspace=tmp_path, current_ms=115.0,
            threshold_pct=10.0,
        )
        assert rep.status == "fail"
        assert rep.regression_pct == pytest.approx(15.0)

    def test_missing_current_ms_mock(self, tmp_path: Path):
        self._write_baseline(tmp_path, "mod-c", 100.0)
        rep = ss.run_benchmark_regression(
            tmp_path, module="mod-c", workspace=tmp_path, current_ms=None,
        )
        assert rep.status == "mock"
        assert rep.baseline_ms == 100.0

    def test_corrupt_baseline_fails(self, tmp_path: Path):
        bench = tmp_path / "test_assets" / "benchmarks"
        bench.mkdir(parents=True)
        (bench / "mod-d.json").write_text("not json")
        rep = ss.run_benchmark_regression(
            tmp_path, module="mod-d", workspace=tmp_path, current_ms=10.0,
        )
        assert rep.status == "fail"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  simulate_software — orchestrator + profile enforcement
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSimulateOrchestrator:
    def test_no_language_detected_returns_error(self, tmp_path: Path):
        r = ss.simulate_software(
            profile="linux-x86_64-native",
            app_path=tmp_path,
        )
        assert r.language == ""
        assert any("detect" in e for e in r.errors)
        assert r.overall_pass() is False

    def test_non_software_profile_raises(self):
        with pytest.raises(ss.SoftwareSimError):
            ss.simulate_software(
                profile="aarch64",      # embedded profile
                app_path=Path.cwd(),
            )

    def test_forced_language_override(self, tmp_path: Path):
        # No marker present, but we force `python` — the runner will
        # degrade to mock (no pytest on /tmp) but language pins.
        r = ss.simulate_software(
            profile="linux-x86_64-native",
            app_path=tmp_path,
            language="python",
        )
        assert r.language == "python"

    def test_benchmark_gate_opt_in(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.dummy]")
        r = ss.simulate_software(
            profile="linux-x86_64-native",
            app_path=tmp_path,
            benchmark=False,
        )
        assert r.benchmark.status == "skip"
        assert r.benchmark.detail == "benchmark gate disabled"


class TestResultToJson:
    def test_flat_shape(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module x\n")
        r = ss.simulate_software(
            profile="linux-arm64-native",
            app_path=tmp_path,
        )
        payload = ss.result_to_json(r)
        for key in (
            "profile", "app_path", "language", "packaging",
            "test_runner", "test_status", "test_total", "test_passed",
            "test_failed", "coverage_status", "coverage_pct",
            "coverage_threshold", "coverage_source",
            "benchmark_status", "benchmark_current_ms", "benchmark_baseline_ms",
            "benchmark_regression_pct", "benchmark_threshold_pct",
            "gates", "overall_pass", "errors",
        ):
            assert key in payload, f"missing key {key}"
        assert payload["language"] == "go"
        assert payload["packaging"] == "deb"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Supported languages surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSupportedLanguages:
    def test_surface_pinned(self):
        # X1 ticket lists 6 languages — if somebody adds a 7th they
        # must also add threshold + dispatcher + docs.
        assert set(ss.SUPPORTED_LANGUAGES) == {
            "python", "go", "rust", "java", "node", "csharp",
        }

    def test_every_lang_has_threshold(self):
        for lang in ss.SUPPORTED_LANGUAGES:
            assert lang in ss.COVERAGE_THRESHOLDS

    def test_every_lang_has_runner(self):
        for lang in ss.SUPPORTED_LANGUAGES:
            assert lang in ss._TEST_RUNNERS

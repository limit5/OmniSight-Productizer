"""BP.H.1 — Contract tests for ``backend.cognitive_penalty``.

Pins the CI-report → warning-prompt projection used by Blueprint Phase
H's cognitive-penalty tier.  The red-card strike counter and notification
mapping are sibling rows; this file only covers BP.H.1's pure prompt
feedback surface.
"""

from __future__ import annotations

from backend.cognitive_penalty import (
    COGNITIVE_PENALTY_SECTION_HEADER,
    COGNITIVE_PENALTY_WARNING_PREFIX,
    MAX_COGNITIVE_PENALTY_FAILURES,
    MAX_COGNITIVE_PENALTY_PROMPT_BYTES,
    CognitivePenaltyFailure,
    CognitivePenaltyReport,
    apply_cognitive_penalty_prompt,
    build_cognitive_penalty_prompt,
    parse_ci_report,
    parse_ci_report_json,
)


def _report(*, failed: int = 0, tests: list[dict] | None = None) -> dict:
    if tests is None:
        tests = []
    return {
        "summary": {
            "passed": 7,
            "failed": failed,
            "skipped": 1,
            "total": 8 + failed,
        },
        "tests": tests,
    }


def _failed_test(nodeid: str = "backend/tests/test_x.py::test_bad") -> dict:
    return {
        "nodeid": nodeid,
        "outcome": "failed",
        "call": {
            "crash": {
                "message": "AssertionError: expected 200, got 500",
            }
        },
    }


def test_section_header_is_stable() -> None:
    assert COGNITIVE_PENALTY_SECTION_HEADER == "# Cognitive Penalty (CI)"


def test_warning_prefix_mentions_verified_minus_one() -> None:
    assert "Verified -1" in COGNITIVE_PENALTY_WARNING_PREFIX
    assert COGNITIVE_PENALTY_WARNING_PREFIX.startswith("CI 驗證失敗")


def test_constants_are_bounded() -> None:
    assert 1 <= MAX_COGNITIVE_PENALTY_FAILURES <= 10
    assert 512 <= MAX_COGNITIVE_PENALTY_PROMPT_BYTES <= 8192


def test_parse_passing_report_maps_to_verified_plus_one() -> None:
    parsed = parse_ci_report(_report())
    assert parsed.status == "pass"
    assert parsed.verified_label == "Verified +1"
    assert parsed.tests_run == 8
    assert parsed.tests_failed == 0
    assert parsed.should_warn is False


def test_parse_failed_summary_maps_to_verified_minus_one() -> None:
    parsed = parse_ci_report(_report(failed=2))
    assert parsed.status == "fail"
    assert parsed.verified_label == "Verified -1"
    assert parsed.tests_failed == 2
    assert parsed.should_warn is True


def test_parse_failed_test_extracts_nodeid_and_crash_message() -> None:
    parsed = parse_ci_report(_report(tests=[_failed_test()]))
    assert parsed.status == "fail"
    assert parsed.failures == (
        CognitivePenaltyFailure(
            nodeid="backend/tests/test_x.py::test_bad",
            message="AssertionError: expected 200, got 500",
        ),
    )


def test_parse_ignores_passing_tests() -> None:
    parsed = parse_ci_report(
        _report(
            tests=[
                {"nodeid": "ok", "outcome": "passed"},
                _failed_test("bad"),
            ]
        )
    )
    assert [f.nodeid for f in parsed.failures] == ["bad"]


def test_parse_caps_failure_list() -> None:
    tests = [_failed_test(f"test_{i}") for i in range(MAX_COGNITIVE_PENALTY_FAILURES + 3)]
    parsed = parse_ci_report(_report(tests=tests))
    assert len(parsed.failures) == MAX_COGNITIVE_PENALTY_FAILURES
    assert parsed.failures[-1].nodeid == f"test_{MAX_COGNITIVE_PENALTY_FAILURES - 1}"


def test_parse_degraded_report_does_not_crash() -> None:
    parsed = parse_ci_report({"summary": "not a dict", "tests": [None, 42, "bad"]})
    assert parsed.status == "unknown"
    assert parsed.verified_label == "Verified 0"
    assert parsed.failures == ()


def test_parse_status_fail_without_tests_still_warns() -> None:
    parsed = parse_ci_report({"status": "fail", "summary": {}})
    assert parsed.status == "fail"
    assert parsed.verified_label == "Verified -1"


def test_parse_json_round_trips_valid_report() -> None:
    raw = (
        '{"summary":{"passed":1,"failed":1,"skipped":0,"total":2},'
        '"tests":[{"nodeid":"t::bad","outcome":"failed","message":"boom"}]}'
    )
    parsed = parse_ci_report_json(raw)
    assert parsed.status == "fail"
    assert parsed.failures[0].message == "boom"


def test_parse_json_bad_input_degrades_to_unknown() -> None:
    parsed = parse_ci_report_json("{not json")
    assert parsed.status == "unknown"
    assert parsed.verified_label == "Verified 0"


def test_build_prompt_returns_empty_for_non_warning_report() -> None:
    parsed = parse_ci_report(_report())
    assert build_cognitive_penalty_prompt(parsed) == ""


def test_build_prompt_contains_ci_summary_and_failures() -> None:
    parsed = parse_ci_report(_report(failed=1, tests=[_failed_test()]))
    prompt = build_cognitive_penalty_prompt(parsed)
    assert COGNITIVE_PENALTY_SECTION_HEADER in prompt
    assert COGNITIVE_PENALTY_WARNING_PREFIX in prompt
    assert "failed=1" in prompt
    assert "backend/tests/test_x.py::test_bad" in prompt
    assert "expected 200, got 500" in prompt


def test_build_prompt_truncates_to_byte_cap_without_breaking_utf8() -> None:
    report = CognitivePenaltyReport(
        status="fail",
        verified_label="Verified -1",
        tests_run=1,
        tests_failed=1,
        tests_passed=0,
        tests_skipped=0,
        failures=(
            CognitivePenaltyFailure(
                nodeid="backend/tests/test_x.py::test_bad",
                message="🟥" * (MAX_COGNITIVE_PENALTY_PROMPT_BYTES * 2),
            ),
        ),
    )
    prompt = build_cognitive_penalty_prompt(report)
    assert len(prompt.encode("utf-8")) <= MAX_COGNITIVE_PENALTY_PROMPT_BYTES
    assert prompt.encode("utf-8").decode("utf-8") == prompt


def test_apply_prompt_is_noop_for_pass() -> None:
    parsed = parse_ci_report(_report())
    assert apply_cognitive_penalty_prompt("BASE", parsed) == "BASE"


def test_apply_prompt_appends_warning_block_for_failure() -> None:
    parsed = parse_ci_report(_report(failed=1, tests=[_failed_test()]))
    prompt = apply_cognitive_penalty_prompt("BASE", parsed)
    assert prompt.startswith("BASE\n\n---\n\n")
    assert COGNITIVE_PENALTY_SECTION_HEADER in prompt

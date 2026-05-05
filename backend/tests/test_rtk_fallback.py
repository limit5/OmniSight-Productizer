"""BP.R.4 contract tests for ``backend.rtk_fallback``."""

from __future__ import annotations

import pytest

from backend.rtk_fallback import (
    RTK_FALLBACK_THRESHOLD,
    RTK_NO_RTK_FLAG,
    build_no_rtk_command,
    compile_failure_signature,
    is_compile_failure,
    update_rtk_fallback_history,
)


def test_threshold_literal_is_two() -> None:
    assert RTK_FALLBACK_THRESHOLD == 2


def test_build_no_rtk_command_prefixes_plain_compile_command() -> None:
    assert build_no_rtk_command("make all") == "rtk --no-rtk make all"


def test_build_no_rtk_command_inserts_flag_after_rtk_prefix() -> None:
    assert build_no_rtk_command("rtk make all") == "rtk --no-rtk make all"


def test_build_no_rtk_command_is_idempotent() -> None:
    command = "rtk --no-rtk make all"
    assert build_no_rtk_command(command) == command


def test_compile_failure_requires_shell_tool() -> None:
    assert is_compile_failure(
        tool_name="run_bash",
        output="src/main.c:10: error: missing ';'",
    )
    assert not is_compile_failure(
        tool_name="read_file",
        output="src/main.c:10: error: missing ';'",
    )


def test_compile_signature_is_task_scoped() -> None:
    out = "src/main.c:10: error: missing ';'"
    sig_a = compile_failure_signature(
        task_id="task-a",
        tool_name="run_bash",
        output=out,
        command="make all",
    )
    sig_b = compile_failure_signature(
        task_id="task-b",
        tool_name="run_bash",
        output=out,
        command="make all",
    )
    assert sig_a != sig_b


def test_first_compile_failure_only_records_history() -> None:
    history, decision = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="src/main.c:10: error: missing ';'",
        prior_history=[],
        command="make all",
    )
    assert len(history) == 1
    assert decision is None


def test_second_same_compile_failure_activates_no_rtk_decision() -> None:
    history, decision = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="src/main.c:10: error: missing ';'",
        prior_history=[],
        command="make all",
    )
    history, decision = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="src/main.c:10: error: missing ';'",
        prior_history=history,
        command="make all",
    )
    assert decision is not None
    assert decision.count == 2
    assert decision.raw_command == "rtk --no-rtk make all"
    assert RTK_NO_RTK_FLAG in decision.message


def test_non_compile_failure_breaks_consecutive_run() -> None:
    history, _ = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="src/main.c:10: error: missing ';'",
        prior_history=[],
        command="make all",
    )
    history, decision = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="[ERROR] file not found",
        prior_history=history,
        command="ls missing",
    )
    assert decision is None
    history, decision = update_rtk_fallback_history(
        task_id="BP.R.4",
        failed_tool_name="run_bash",
        failed_output="src/main.c:10: error: missing ';'",
        prior_history=history,
        command="make all",
    )
    assert decision is None


def test_threshold_validation() -> None:
    with pytest.raises(ValueError):
        update_rtk_fallback_history(
            task_id="BP.R.4",
            failed_tool_name="run_bash",
            failed_output="error: x",
            prior_history=[],
            command="make",
            threshold=0,
        )

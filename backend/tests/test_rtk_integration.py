"""BP.R.7 integration coverage for RTK hardening contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import pep_gateway as pep
from backend.agents.nodes import error_check_node
from backend.agents.state import GraphState, ToolResult
from backend.output_compressor import (
    _looks_binary,
    compress_output,
    get_compression_stats,
    reset_compression_stats,
)
from backend.prompt_loader import build_system_prompt
from backend.rtk_fallback import (
    MAX_RTK_FALLBACK_HISTORY,
    compile_failure_signature,
    update_rtk_fallback_history,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
RTKIGNORE = REPO_ROOT / "configs/.rtkignore"


@pytest.fixture(autouse=True)
def _reset_rtk_state(monkeypatch: pytest.MonkeyPatch):
    reset_compression_stats()
    monkeypatch.setattr("backend.config.settings.rtk_enabled", True)
    monkeypatch.setattr("backend.config.settings.rtk_compression_threshold", 1000)
    monkeypatch.setattr("backend.config.settings.rtk_dedup_lines", True)
    monkeypatch.setattr("backend.config.settings.rtk_strip_progress", True)
    monkeypatch.setattr("backend.config.settings.rtk_track_savings", True)
    yield
    reset_compression_stats()


def _rtkignore_lines() -> set[str]:
    return {
        line.strip()
        for line in RTKIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


class TestRtkCompressionIntegration:

    @pytest.mark.asyncio
    async def test_duplicate_build_warnings_are_compressed_and_tracked(self) -> None:
        output = "\n".join(
            ["warning: unused variable x in src/main.c"] * 80
        )

        compressed, saved = await compress_output(output, "run_bash")

        assert saved > 0
        assert "identical line(s) removed" in compressed
        stats = get_compression_stats()
        assert stats["compression_count"] == 1
        assert stats["total_lines_removed"] >= 79

    @pytest.mark.asyncio
    async def test_repeated_file_line_patterns_are_collapsed(self) -> None:
        output = "\n".join(
            f"/work/src/file{i}.c:10:5: warning: unused variable 'x'"
            for i in range(40)
        )

        compressed, saved = await compress_output(output, "run_bash")

        assert saved > 0
        assert "pattern repeated" in compressed
        assert "/work/src/file0.c:10:5" in compressed

    @pytest.mark.asyncio
    async def test_progress_and_ansi_noise_are_stripped(self) -> None:
        output = "\n".join(
            [
                "\x1b[31merror: real compiler failure\x1b[0m",
                "[==========] 90%",
                "normal diagnostic line with useful source context",
            ]
            * 40
        )

        compressed, saved = await compress_output(output, "run_bash")

        assert saved > 0
        assert "\x1b[" not in compressed
        assert "[==========] 90%" not in compressed
        assert "error: real compiler failure" in compressed

    @pytest.mark.asyncio
    async def test_binary_looking_payload_is_not_compressed(self) -> None:
        output = ("\x00\x01\x02\x03opaque-bytes" * 120)

        compressed, saved = await compress_output(output, "run_bash")

        assert _looks_binary(output)
        assert compressed == output
        assert saved == 0
        assert get_compression_stats()["compression_count"] == 0

    @pytest.mark.asyncio
    async def test_rtk_disabled_returns_raw_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("backend.config.settings.rtk_enabled", False)
        output = "\n".join(["same noisy warning"] * 100)

        compressed, saved = await compress_output(output, "run_bash")

        assert compressed == output
        assert saved == 0
        assert get_compression_stats()["compression_count"] == 0

    @pytest.mark.asyncio
    async def test_small_output_stays_raw_even_when_rtk_enabled(self) -> None:
        output = "short diagnostic"

        compressed, saved = await compress_output(output, "run_bash")

        assert compressed == output
        assert saved == 0


class TestRtkFallbackIntegration:

    def test_first_compile_failure_only_records_same_task_signature(self) -> None:
        history, decision = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=[],
            command="rtk make all",
        )

        assert decision is None
        assert len(history) == 1
        assert history[0].startswith("rtk_compile:bp.r.7:")

    def test_second_same_compile_failure_requests_no_rtk_raw_command(self) -> None:
        history, _ = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=[],
            command="rtk make all",
        )

        history, decision = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=history,
            command="rtk make all",
        )

        assert len(history) == 2
        assert decision is not None
        assert decision.raw_command == "rtk --no-rtk make all"
        assert "same compile failure repeated 2 times" in decision.message

    def test_same_output_on_different_task_does_not_trigger_fallback(self) -> None:
        first = compile_failure_signature(
            task_id="task-a",
            tool_name="run_bash",
            output="src/main.c:12: error: missing ';'",
            command="make all",
        )

        history, decision = update_rtk_fallback_history(
            task_id="task-b",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=[first],
            command="make all",
        )

        assert decision is None
        assert history[-1] != first

    def test_non_compile_failure_breaks_consecutive_fallback_chain(self) -> None:
        history, _ = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=[],
            command="make all",
        )
        history, _ = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="[ERROR] no such file or directory",
            prior_history=history,
            command="ls missing",
        )

        history, decision = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=history,
            command="make all",
        )

        assert decision is None
        assert history[-2] == "rtk_compile:_non_compile"

    def test_compile_command_triggers_detection_even_when_output_is_sparse(self) -> None:
        history, decision = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="exit code 2",
            prior_history=[],
            command="cmake --build build",
        )

        assert decision is None
        assert history and history[0].startswith("rtk_compile:bp.r.7:cmake")

    def test_history_is_bounded_for_long_running_agent_state(self) -> None:
        history = [f"old-{i}" for i in range(MAX_RTK_FALLBACK_HISTORY + 5)]

        updated, _ = update_rtk_fallback_history(
            task_id="BP.R.7",
            failed_tool_name="run_bash",
            failed_output="src/main.c:12: error: missing ';'",
            prior_history=history,
            command="make all",
        )

        assert len(updated) == MAX_RTK_FALLBACK_HISTORY
        assert "old-0" not in updated

    @pytest.mark.asyncio
    async def test_error_check_node_turns_second_compile_retry_into_rtk_bypass(self) -> None:
        signature = compile_failure_signature(
            task_id="BP.R.7",
            tool_name="run_bash",
            output="src/main.c:12: error: missing ';'",
            command="make all",
        )
        state = GraphState(
            task_id="BP.R.7",
            user_command="make all",
            tool_results=[
                ToolResult(
                    tool_name="run_bash",
                    output="src/main.c:12: error: missing ';'",
                    success=False,
                ),
            ],
            retry_count=1,
            max_retries=3,
            rtk_fallback_history=[signature],
        )

        update = await error_check_node(state)

        assert update["rtk_bypass"] is True
        assert "rtk --no-rtk make all" in update["last_error"]
        assert len(update["rtk_fallback_history"]) == 2


class TestRtkIgnoreIntegration:

    def test_rtkignore_exists_under_configs(self) -> None:
        assert RTKIGNORE.exists()

    def test_rtkignore_excludes_build_output_roots(self) -> None:
        lines = _rtkignore_lines()
        assert {"/build/", "/bin/", "/dist/"}.issubset(lines)

    def test_rtkignore_excludes_compiler_and_linker_artifacts(self) -> None:
        lines = _rtkignore_lines()
        assert {"*.o", "*.obj", "*.a", "*.lib", "*.so", "*.dylib", "*.dll"}.issubset(lines)

    def test_rtkignore_excludes_executables_and_firmware_images(self) -> None:
        lines = _rtkignore_lines()
        assert {"*.bin", "*.elf", "*.exe", "*.out", "*.hex", "*.img", "*.iso", "*.dmg"}.issubset(lines)

    def test_rtkignore_excludes_raw_sensor_buffers_and_media(self) -> None:
        lines = _rtkignore_lines()
        assert {"*.raw", "*.nv12", "*.yuv", "*.rgb", "*.rgba"}.issubset(lines)
        assert {"*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.mp4", "*.mov"}.issubset(lines)

    def test_rtkignore_excludes_binary_archive_payloads(self) -> None:
        lines = _rtkignore_lines()
        assert {"*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.xz", "*.txz", "*.7z", "*.rar"}.issubset(lines)

    def test_rtkignore_keeps_source_and_text_logs_available_for_compression(self) -> None:
        lines = _rtkignore_lines()
        assert "*.c" not in lines
        assert "*.cpp" not in lines
        assert "*.py" not in lines
        assert "*.log" not in lines


class TestRtkPromptAndPepIntegration:

    def test_system_prompt_includes_rtk_rules_for_lazy_specialist_prompt(self) -> None:
        prompt = build_system_prompt(
            model_name="",
            agent_type="firmware",
            sub_type="bsp",
            mode="lazy",
        )

        assert "RTK Output Compression Rules" in prompt
        assert "For high-noise commands" in prompt
        assert "put `rtk` before the command" in prompt

    def test_system_prompt_lists_high_noise_command_families(self) -> None:
        prompt = build_system_prompt(model_name="", agent_type="firmware", sub_type="bsp")

        for expected in (
            "`make`",
            "`cmake`",
            "`ninja`",
            "`pytest`",
            "`cargo`",
            "`go test`",
            "`git diff`",
            "`tail`",
            "`journalctl`",
            "`rg`",
            "`grep`",
            "`find`",
            "`cat`",
            "`sed`",
        ):
            assert expected in prompt

    def test_system_prompt_documents_raw_output_fallback_exception(self) -> None:
        prompt = build_system_prompt(model_name="", agent_type="firmware", sub_type="bsp")

        assert "Use the raw command only" in prompt
        assert "RTK fallback flow asks for it" in prompt

    def test_system_prompt_rtk_section_is_inserted_once(self) -> None:
        prompt = build_system_prompt(model_name="", agent_type="general", mode="lazy")

        assert prompt.count("RTK Output Compression Rules") == 1

    @pytest.mark.parametrize("tool,args", [
        ("read_file", {"path": "build/compile.log"}),
        ("Read", {"file_path": "logs/agent.out"}),
        ("read_file", {"path": "cmake-build-debug/CMakeFiles/output.log"}),
    ])
    def test_native_read_for_high_noise_paths_is_denied_with_rtk_guidance(self, tool: str, args: dict[str, str]) -> None:
        action, rule, reason, scope = pep.classify(tool, args, "t3")

        assert action is pep.PepAction.deny
        assert rule == "native_read_high_noise_path"
        assert "Bash" in reason
        assert "RTK" in reason
        assert scope == "local"

    def test_native_read_for_source_path_stays_on_existing_policy(self) -> None:
        action, rule, _reason, scope = pep.classify("read_file", {"path": "src/main.c"}, "t1")

        assert action is pep.PepAction.auto_allow
        assert rule == "tier_whitelist"
        assert scope == "local"

    def test_rtk_prefixed_bash_read_is_allowed_for_t3_agent_path(self) -> None:
        action, rule, _reason, scope = pep.classify(
            "run_bash",
            {"command": "rtk cat build/compile.log"},
            "t3",
        )

        assert action is pep.PepAction.auto_allow
        assert rule == "tier_whitelist"
        assert scope == "local"

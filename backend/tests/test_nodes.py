"""Tests for backend/agents/nodes.py — routing, tool extraction, error check."""

from __future__ import annotations

import pytest

from backend.agents.nodes import (
    _rule_based_route,
    _rule_based_tool_calls,
    error_check_node,
    tool_executor_node,
    _should_retry,
)
from backend.agents.state import GraphState, ToolCall, ToolResult


# ─── Rule-based routing ───


class TestRuleBasedRoute:

    def test_firmware_keywords(self):
        primary, _ = _rule_based_route("write a UVC driver for IMX335 sensor")
        assert primary == "firmware"

    def test_software_keywords(self):
        primary, _ = _rule_based_route("refactor the algorithm module")
        assert primary == "software"

    def test_validator_keywords(self):
        primary, _ = _rule_based_route("run test suite and check coverage")
        assert primary == "validator"

    def test_reporter_keywords(self):
        primary, _ = _rule_based_route("generate FCC compliance report")
        assert primary == "reporter"

    def test_no_match_returns_general(self):
        primary, secondary = _rule_based_route("hello world")
        assert primary == "general"
        assert secondary == []

    def test_highest_score_wins(self):
        primary, _ = _rule_based_route("firmware driver sensor test")
        assert primary == "firmware"

    def test_compound_returns_secondary(self):
        primary, secondary = _rule_based_route("write firmware driver and run tests")
        assert primary in ("firmware", "validator")
        assert len(secondary) >= 1


# ─── Rule-based tool extraction ───


class TestRuleBasedToolCalls:

    def test_read_file(self):
        calls = _rule_based_tool_calls("read file src/main.c")
        assert any(tc.tool_name == "read_file" for tc in calls)
        match = next(tc for tc in calls if tc.tool_name == "read_file")
        assert match.arguments["path"] == "src/main.c"

    def test_cat_file(self):
        calls = _rule_based_tool_calls("cat config.yaml")
        assert any(tc.tool_name == "read_file" for tc in calls)

    def test_git_status(self):
        calls = _rule_based_tool_calls("git status")
        assert any(tc.tool_name == "git_status" for tc in calls)

    def test_git_log(self):
        calls = _rule_based_tool_calls("git log")
        assert any(tc.tool_name == "git_log" for tc in calls)

    def test_list_directory(self):
        calls = _rule_based_tool_calls("ls src/")
        assert any(tc.tool_name == "list_directory" for tc in calls)

    def test_run_command(self):
        calls = _rule_based_tool_calls("run make -j4")
        assert any(tc.tool_name == "run_bash" for tc in calls)

    def test_make_command(self):
        calls = _rule_based_tool_calls("make clean")
        assert any(tc.tool_name == "run_bash" for tc in calls)

    def test_no_match(self):
        calls = _rule_based_tool_calls("explain the architecture")
        assert len(calls) == 0

    def test_search_pattern(self):
        calls = _rule_based_tool_calls("search 'init_sensor' in src/")
        assert any(tc.tool_name == "search_in_files" for tc in calls)

    def test_yaml_parse(self):
        calls = _rule_based_tool_calls("parse config.yaml")
        assert any(tc.tool_name == "read_yaml" for tc in calls)


# ─── Error check node (self-healing) ───


class TestErrorCheckNode:

    @pytest.mark.asyncio
    async def test_no_errors_passes_through(self):
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="file content", success=True),
            ],
            retry_count=0,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert update["rtk_bypass"] is False

    @pytest.mark.asyncio
    async def test_error_triggers_retry(self):
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="[ERROR] File not found", success=False),
            ],
            retry_count=0,
            max_retries=2,
        )
        update = await error_check_node(state)
        assert update["retry_count"] == 1
        assert "read_file" in update["last_error"]
        assert update["tool_calls"] == []
        assert update["tool_results"] == []

    @pytest.mark.asyncio
    async def test_retries_exhausted_escalates(self):
        """When retries exhausted with errors, escalate to human."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[
                ToolResult(tool_name="run_bash", output="[ERROR] compile failed", success=False),
            ],
            retry_count=3,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert len(update["actions"]) == 1
        assert update["actions"][0].status == "awaiting_confirmation"

    @pytest.mark.asyncio
    async def test_retries_exhausted_no_errors_passes_through(self):
        """When retries exhausted but no errors, go to summarizer and reset bypass."""
        state = GraphState(
            tool_results=[
                ToolResult(tool_name="read_file", output="ok", success=True),
            ],
            retry_count=3,
            max_retries=3,
        )
        update = await error_check_node(state)
        assert update["last_error"] == ""
        assert update["rtk_bypass"] is False

    def test_should_retry_routes_to_specialist(self):
        """After error_check sets last_error, should retry the specialist."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="run_bash: [ERROR] compile failed",
            retry_count=1,
            max_retries=2,
        )
        assert _should_retry(state) == "firmware"

    def test_should_retry_routes_to_summarizer_on_success(self):
        """After error_check clears last_error (no errors), go to summarizer."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="",
            retry_count=0,
            max_retries=2,
        )
        assert _should_retry(state) == "summarizer"

    def test_should_retry_routes_to_summarizer_when_exhausted(self):
        """When retries exhausted, error_check clears last_error → summarizer."""
        state = GraphState(
            routed_to="firmware",
            tool_results=[],
            last_error="",
            retry_count=3,
            max_retries=3,
        )
        assert _should_retry(state) == "summarizer"

    @pytest.mark.asyncio
    async def test_patch_failed_tool_output_enters_self_correction(
        self,
        monkeypatch,
    ):
        """WP.3.5: patch failures must be explicit tool errors so the
        existing error_check retry loop can ask the agent to correct
        the SEARCH context instead of silently treating the edit as done.
        """
        from backend.agents import nodes as _nodes
        from backend import pep_gateway as _pep

        class _FakePatchTool:
            async def ainvoke(self, _args):
                return "[PATCH-FAILED] PatchNotFound: SEARCH block did not match"

        async def _allow_pep(**kwargs):
            return _pep.PepDecision(
                id="pep-test",
                ts=0.0,
                agent_id=kwargs.get("agent_id", ""),
                tool=kwargs["tool"],
                command="",
                tier=kwargs["tier"],
                action=_pep.PepAction.auto_allow,
            )

        monkeypatch.setitem(_nodes.TOOL_MAP, "patch_file", _FakePatchTool())
        monkeypatch.setattr(_pep, "evaluate", _allow_pep)
        monkeypatch.setattr(_nodes, "emit_pipeline_phase", lambda *a, **kw: None)
        monkeypatch.setattr(_nodes, "emit_tool_progress", lambda *a, **kw: None)

        state = GraphState(
            routed_to="software",
            tool_calls=[ToolCall(tool_name="patch_file", arguments={})],
            retry_count=0,
            max_retries=2,
        )

        executor_update = await tool_executor_node(state)
        result = executor_update["tool_results"][0]
        assert result.success is False
        assert result.output.startswith("[PATCH-FAILED]")

        retry_update = await error_check_node(
            state.model_copy(update={"tool_results": executor_update["tool_results"]})
        )
        assert retry_update["retry_count"] == 1
        assert "patch_file" in retry_update["last_error"]


# ─── B15 #350: Skill-on-demand ReAct loop ───


class _FakeLLMResponse:
    """Stand-in for an LLM response — supports `.content` access and
    the optional `.tool_calls` list that LangChain bindings surface."""

    def __init__(self, content: str, tool_calls: list | None = None):
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _ScriptedLLM:
    """Returns one scripted response per `.invoke()` call. Records the
    messages it received so tests can assert the loaded-skill body was
    actually injected."""

    def __init__(self, scripted: list[_FakeLLMResponse]):
        self._scripted = list(scripted)
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        if not self._scripted:
            return _FakeLLMResponse("")
        return self._scripted.pop(0)


class TestSkillOnDemandReActLoop:
    """B15 #350 row 261 — when OMNISIGHT_SKILL_LOADING=lazy, the specialist
    node loops on `[LOAD_SKILL: <name>]` markers and injects full skill
    bodies before the agent produces its final tool-calls / answer."""

    @pytest.mark.asyncio
    async def test_lazy_mode_loads_skill_on_marker(self, monkeypatch):
        """Agent emits [LOAD_SKILL: ...] → skill body injected; second
        invocation sees the injected body and produces the final answer."""
        from backend.agents import nodes as _nodes

        monkeypatch.setenv("OMNISIGHT_SKILL_LOADING", "lazy")

        injection_payload = "## Skill: fake-skill\n\nBSP kernel driver body"

        scripted = [
            _FakeLLMResponse("Reasoning… [LOAD_SKILL: fake-skill]"),
            _FakeLLMResponse("Firmware analysis complete."),
        ]
        fake_llm = _ScriptedLLM(scripted)

        monkeypatch.setattr(_nodes, "_get_llm", lambda **kw: fake_llm)
        monkeypatch.setattr(
            _nodes, "build_system_prompt", lambda **kw: "PROMPT",
        )
        monkeypatch.setattr(
            _nodes, "build_skill_injection",
            lambda explicit_skills, domain_context, user_prompt: (
                injection_payload
                if explicit_skills == ["fake-skill"] else ""
            ),
        )

        state = GraphState(
            user_command="write a BSP driver",
            routed_to="firmware",
            model_name="",
        )
        firmware_node = _nodes._specialist_node_factory("firmware")
        update = await firmware_node(state)

        # Second invocation happened → skill body in message trail.
        assert len(fake_llm.calls) == 2
        second_call_text = "\n".join(
            getattr(m, "content", "") for m in fake_llm.calls[1]
        )
        assert "BSP kernel driver body" in second_call_text

        # Final answer comes from the second (post-injection) response.
        assert "Firmware analysis complete." in update["answer"]

        # Skill-load trail is returned as extra messages so retries see it.
        trail = "".join(
            getattr(m, "content", "") for m in update["messages"]
        )
        assert "[LOAD_SKILL: fake-skill]" in trail
        assert "BSP kernel driver body" in trail

    @pytest.mark.asyncio
    async def test_eager_mode_ignores_load_skill_markers(self, monkeypatch):
        """Back-compat: in eager mode the node returns the first response
        verbatim and never re-invokes the LLM for skill loading."""
        from backend.agents import nodes as _nodes

        monkeypatch.setenv("OMNISIGHT_SKILL_LOADING", "eager")

        scripted = [
            _FakeLLMResponse("No skills needed — [LOAD_SKILL: fake-skill]"),
        ]
        fake_llm = _ScriptedLLM(scripted)

        monkeypatch.setattr(_nodes, "_get_llm", lambda **kw: fake_llm)
        monkeypatch.setattr(
            _nodes, "build_system_prompt", lambda **kw: "PROMPT",
        )
        # This would blow up if called — eager must not hit it.
        monkeypatch.setattr(
            _nodes, "build_skill_injection",
            lambda **kw: (_ for _ in ()).throw(AssertionError("eager called")),
        )

        state = GraphState(
            user_command="run",
            routed_to="software",
        )
        software_node = _nodes._specialist_node_factory("software")
        update = await software_node(state)

        # Exactly one LLM call; marker text passes through unchanged in the
        # returned answer (eager path prepends `[SOFTWARE AGENT] ` prefix).
        assert len(fake_llm.calls) == 1
        assert "[LOAD_SKILL: fake-skill]" in update["answer"]

    @pytest.mark.asyncio
    async def test_lazy_mode_caps_at_max_iterations(self, monkeypatch):
        """An agent that keeps asking for skills is capped; we emit a
        `skill_load_capped` phase event and return the last response."""
        from backend.agents import nodes as _nodes

        monkeypatch.setenv("OMNISIGHT_SKILL_LOADING", "lazy")

        # Every scripted response asks for *another* skill. We should cap
        # after _MAX_SKILL_LOAD_ITERATIONS and break out of the loop.
        scripted = [
            _FakeLLMResponse(f"[LOAD_SKILL: skill-{i}]")
            for i in range(_nodes._MAX_SKILL_LOAD_ITERATIONS + 5)
        ]
        fake_llm = _ScriptedLLM(scripted)

        monkeypatch.setattr(_nodes, "_get_llm", lambda **kw: fake_llm)
        monkeypatch.setattr(
            _nodes, "build_system_prompt", lambda **kw: "PROMPT",
        )
        monkeypatch.setattr(
            _nodes, "build_skill_injection",
            lambda explicit_skills, domain_context, user_prompt: (
                f"body-for-{explicit_skills[0]}"
            ),
        )

        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            _nodes, "emit_pipeline_phase",
            lambda phase, detail: events.append((phase, detail)),
        )

        state = GraphState(user_command="x", routed_to="firmware")
        firmware_node = _nodes._specialist_node_factory("firmware")
        update = await firmware_node(state)

        # We cap total LLM invocations at _MAX_SKILL_LOAD_ITERATIONS + 1
        # (initial call + one re-invoke per iteration).
        assert len(fake_llm.calls) == _nodes._MAX_SKILL_LOAD_ITERATIONS + 1
        # Cap event was emitted.
        assert any(phase == "skill_load_capped" for phase, _ in events)
        # Node still returns a coherent answer (doesn't raise).
        assert "answer" in update

    @pytest.mark.asyncio
    async def test_lazy_mode_missing_skill_is_skipped(self, monkeypatch):
        """When `build_skill_injection` returns empty (skill name unknown),
        we emit skill_load_miss and do NOT loop forever on the same name."""
        from backend.agents import nodes as _nodes

        monkeypatch.setenv("OMNISIGHT_SKILL_LOADING", "lazy")

        # Agent keeps asking for "ghost" which never resolves.
        scripted = [
            _FakeLLMResponse("[LOAD_SKILL: ghost]"),
            _FakeLLMResponse("[LOAD_SKILL: ghost]"),
            _FakeLLMResponse("final answer"),
        ]
        fake_llm = _ScriptedLLM(scripted)

        monkeypatch.setattr(_nodes, "_get_llm", lambda **kw: fake_llm)
        monkeypatch.setattr(
            _nodes, "build_system_prompt", lambda **kw: "PROMPT",
        )
        monkeypatch.setattr(
            _nodes, "build_skill_injection",
            lambda explicit_skills, domain_context, user_prompt: "",
        )

        events: list[tuple[str, str]] = []
        monkeypatch.setattr(
            _nodes, "emit_pipeline_phase",
            lambda phase, detail: events.append((phase, detail)),
        )

        state = GraphState(user_command="x", routed_to="firmware")
        firmware_node = _nodes._specialist_node_factory("firmware")
        update = await firmware_node(state)

        # At least one miss event fired.
        assert any(phase == "skill_load_miss" for phase, _ in events)
        # We terminated (dedup prevented infinite re-requests for "ghost").
        assert "answer" in update
        # The second "[LOAD_SKILL: ghost]" is filtered by the dedup set so
        # we break immediately after the miss — only 2 LLM calls total.
        assert len(fake_llm.calls) == 2

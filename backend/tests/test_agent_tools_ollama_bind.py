"""Z.6.3 — AGENT_TOOLS mapping: ollama provider not short-circuited.

Verifies that:
  1. AGENT_TOOLS covers all six specialist agent types with non-empty tool lists.
  2. _get_llm(bind_tools_for=<agent_type>) routes bind_tools to get_llm() correctly
     regardless of the active provider — including ollama.
  3. When the active provider is ollama, get_llm() calls llm.bind_tools() on the
     ChatOllama instance (the core Z.6.2 fix is exercised end-to-end through the
     nodes._get_llm() call path, not just the adapter layer).
  4. The specialist node factory correctly processes resp.tool_calls when the LLM
     (backed by ollama) returns structured tool-call objects.

Module-global audit (SOP Step 1): AGENT_TOOLS is a module-const dict literal —
every uvicorn worker derives the same mapping from the same source (SOP answer #1
"不共享，因為每 worker 從同樣來源推導出同樣的值"). No shared mutable state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_llm_cache():
    """Blow the get_llm() LRU cache between tests so provider/tool combinations
    are rebuilt fresh — avoids stale bind_tools from a prior test poisoning
    assertions about call counts."""
    from backend.agents import llm as _llm_mod
    _llm_mod._cache.clear()
    yield
    _llm_mod._cache.clear()


# ─── 1. AGENT_TOOLS completeness ────────────────────────────────────────────

class TestAgentToolsMapping:
    """AGENT_TOOLS must have an entry for every specialist the graph routes to."""

    _REQUIRED_SPECIALIST_KEYS = {
        "firmware",
        "software",
        "validator",
        "reporter",
        "reviewer",
        "general",
    }

    def test_all_specialist_keys_present(self):
        from backend.agents.tools import AGENT_TOOLS
        missing = self._REQUIRED_SPECIALIST_KEYS - set(AGENT_TOOLS.keys())
        assert not missing, f"AGENT_TOOLS is missing specialist key(s): {missing}"

    def test_specialist_tool_lists_non_empty(self):
        from backend.agents.tools import AGENT_TOOLS
        for key in self._REQUIRED_SPECIALIST_KEYS:
            tools = AGENT_TOOLS.get(key, [])
            assert tools, f"AGENT_TOOLS[{key!r}] must be non-empty"

    def test_agent_tools_values_are_lists(self):
        from backend.agents.tools import AGENT_TOOLS
        for key, val in AGENT_TOOLS.items():
            assert isinstance(val, list), (
                f"AGENT_TOOLS[{key!r}] should be a list, got {type(val).__name__}"
            )


# ─── 2. _get_llm() routes bind_tools for all providers ──────────────────────

class TestGetLlmBindToolsRouting:
    """_get_llm(bind_tools_for=<agent>) must pass the tool list to get_llm()
    regardless of the provider resolved from settings."""

    @pytest.fixture
    def firmware_tools(self):
        from backend.agents.tools import AGENT_TOOLS
        return AGENT_TOOLS["firmware"]

    def _make_fake_llm(self):
        fake = MagicMock()
        fake.model_name = "test-model"
        bound = MagicMock()
        fake.bind_tools.return_value = bound
        fake.with_config.return_value = fake
        return fake, bound

    def test_get_llm_passes_firmware_tools_for_ollama(
        self, monkeypatch, firmware_tools
    ):
        """When provider is ollama and bind_tools_for='firmware',
        get_llm() must call bind_tools() on the resulting model."""
        from backend.agents.nodes import _get_llm
        from backend.agents import llm as _llm_mod

        fake_llm, bound_llm = self._make_fake_llm()

        with patch.object(_llm_mod, "_create_llm", return_value=fake_llm):
            # Point settings to ollama
            monkeypatch.setattr(
                "backend.agents.llm.settings",
                MagicMock(
                    llm_provider="ollama",
                    llm_temperature=0.0,
                    llm_fallback_chain="",
                    get_model_name=lambda: "llama3.1",
                    ollama_model="llama3.1",
                ),
            )
            result = _get_llm(bind_tools_for="firmware", model_name="")

        fake_llm.bind_tools.assert_called_once_with(firmware_tools)

    @pytest.mark.parametrize("agent_type", [
        "firmware", "software", "validator", "reporter", "reviewer", "general",
    ])
    def test_all_specialists_pass_tools_to_get_llm(
        self, monkeypatch, agent_type
    ):
        """Every specialist key in AGENT_TOOLS must result in bind_tools being
        called when the model is successfully built."""
        from backend.agents.nodes import _get_llm
        from backend.agents import llm as _llm_mod
        from backend.agents.tools import AGENT_TOOLS

        expected_tools = AGENT_TOOLS[agent_type]
        fake_llm, _ = self._make_fake_llm()

        with patch.object(_llm_mod, "_create_llm", return_value=fake_llm):
            monkeypatch.setattr(
                "backend.agents.llm.settings",
                MagicMock(
                    llm_provider="ollama",
                    llm_temperature=0.0,
                    llm_fallback_chain="",
                    get_model_name=lambda: "llama3.1",
                    ollama_model="llama3.1",
                ),
            )
            _get_llm(bind_tools_for=agent_type, model_name="")

        fake_llm.bind_tools.assert_called_once_with(expected_tools)

    def test_none_bind_tools_for_skips_bind(self, monkeypatch):
        """When bind_tools_for is None (conversation node), bind_tools() must
        NOT be called — tools must not be injected into the conversation path."""
        from backend.agents.nodes import _get_llm
        from backend.agents import llm as _llm_mod

        fake_llm, _ = self._make_fake_llm()

        with patch.object(_llm_mod, "_create_llm", return_value=fake_llm):
            monkeypatch.setattr(
                "backend.agents.llm.settings",
                MagicMock(
                    llm_provider="ollama",
                    llm_temperature=0.0,
                    llm_fallback_chain="",
                    get_model_name=lambda: "llama3.1",
                    ollama_model="llama3.1",
                ),
            )
            _get_llm(bind_tools_for=None, model_name="")

        fake_llm.bind_tools.assert_not_called()


# ─── 3. Specialist node processes ollama tool_calls ─────────────────────────

class TestSpecialistNodeOllamaToolCalls:
    """_specialist_node_factory must extract tool_calls from an LLM response
    regardless of whether the model is ollama-backed or cloud-backed.

    The LangChain ChatOllama AIMessage carries tool_calls as a list of dicts
    with keys {"name", "args", "id"} — same shape as every other provider
    after the adapter normalises them. This test confirms the node's extraction
    logic handles that shape correctly.
    """

    @pytest.fixture
    def mock_ollama_resp_with_tool_call(self):
        """Synthetic AIMessage-shaped object mimicking ChatOllama output with
        a single tool call (``run_bash`` with command='echo hi')."""
        resp = MagicMock()
        resp.content = ""
        resp.tool_calls = [
            {"name": "run_bash", "args": {"command": "echo hi"}, "id": "tc-abc123"},
        ]
        return resp

    @pytest.fixture
    def mock_ollama_resp_no_tool_calls(self):
        """Direct text answer — no tool calls."""
        resp = MagicMock()
        resp.content = "[FIRMWARE AGENT] Done."
        resp.tool_calls = []
        return resp

    @pytest.fixture
    def graph_state(self):
        from backend.agents.state import GraphState
        return GraphState(
            user_command="echo hi",
            routed_to="firmware",
        )

    @pytest.mark.asyncio
    async def test_tool_calls_extracted_from_ollama_response(
        self, monkeypatch, graph_state, mock_ollama_resp_with_tool_call
    ):
        """When ollama returns tool_calls, the specialist node must populate
        state.tool_calls with ToolCall objects."""
        from backend.agents import nodes as _nodes_mod
        from backend.agents.nodes import _specialist_node_factory

        fake_llm = MagicMock()
        fake_llm.invoke.return_value = mock_ollama_resp_with_tool_call

        with patch.object(_nodes_mod, "_get_llm", return_value=fake_llm):
            # Stub helpers that fire SSE or emit events
            with patch.object(_nodes_mod, "emit_pipeline_phase"):
                with patch.object(_nodes_mod, "build_system_prompt", return_value="sys"):
                    firmware_node = _specialist_node_factory("firmware")
                    result = await firmware_node(graph_state)

        assert "tool_calls" in result
        tc = result["tool_calls"]
        assert len(tc) == 1
        assert tc[0].tool_name == "run_bash"
        assert tc[0].arguments == {"command": "echo hi"}

    @pytest.mark.asyncio
    async def test_direct_answer_when_no_tool_calls(
        self, monkeypatch, graph_state, mock_ollama_resp_no_tool_calls
    ):
        """When ollama returns no tool_calls, the node must set state.answer
        directly and NOT populate tool_calls."""
        from backend.agents import nodes as _nodes_mod
        from backend.agents.nodes import _specialist_node_factory

        fake_llm = MagicMock()
        fake_llm.invoke.return_value = mock_ollama_resp_no_tool_calls

        with patch.object(_nodes_mod, "_get_llm", return_value=fake_llm):
            with patch.object(_nodes_mod, "emit_pipeline_phase"):
                with patch.object(_nodes_mod, "build_system_prompt", return_value="sys"):
                    firmware_node = _specialist_node_factory("firmware")
                    result = await firmware_node(graph_state)

        assert "answer" in result
        assert "FIRMWARE AGENT" in result["answer"]
        assert not result.get("tool_calls")


# ─── 4. AGENT_TOOLS keyed by agent_type, not provider ───────────────────────

class TestAgentToolsProviderAgnostic:
    """AGENT_TOOLS is keyed by agent_type — it must NOT contain provider names
    as keys.  This is the structural guard that prevents an accidental
    provider-specific bypass from being added."""

    _PROVIDER_NAMES = {
        "anthropic", "google", "openai", "xai", "groq",
        "deepseek", "together", "openrouter", "ollama",
    }

    def test_no_provider_keys_in_agent_tools(self):
        from backend.agents.tools import AGENT_TOOLS
        provider_keys_found = self._PROVIDER_NAMES & set(AGENT_TOOLS.keys())
        assert not provider_keys_found, (
            f"AGENT_TOOLS must not have provider-named keys "
            f"(found: {provider_keys_found}). Tool assignment is per-agent-type, "
            f"not per-provider — a provider key would create a silent bypass."
        )

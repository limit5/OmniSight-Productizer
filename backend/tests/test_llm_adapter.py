"""N4 — LangChain/LangGraph adapter firewall tests.

Covers:
  * Re-exported message, graph, and tool primitives resolve to the
    same classes LangChain / LangGraph ship (so isinstance checks
    across module boundaries keep working).
  * Stable-interface functions (`invoke_chat`, `stream_chat`,
    `tool_call`, `embed`, `build_chat_model`) behave correctly in
    both the configured-provider and no-provider paths.
  * `_coerce_messages` accepts tuples and dicts in addition to
    native BaseMessage objects.
  * `check_llm_adapter_firewall.py` detects violations (positive
    case) and accepts the current repo (negative case).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend import llm_adapter
from backend.llm_adapter import (
    AIMessage,
    AdapterToolCall,
    AdapterToolResponse,
    BaseCallbackHandler,
    BaseChatModel,
    BaseMessage,
    END,
    HumanMessage,
    LLMResult,
    RemoveMessage,
    StateGraph,
    SystemMessage,
    ToolMessage,
    add_messages,
    build_chat_model,
    embed,
    invoke_chat,
    stream_chat,
    tool,
    tool_call,
)
from backend.llm_adapter import _coerce_messages, _message_text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Re-exports — ensure they point at the actual LangChain classes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReExports:

    def test_message_classes_match_langchain(self):
        """The adapter re-exports must be the *same* classes as
        LangChain's, not shadow copies — isinstance() checks across
        module boundaries depend on identity."""
        from langchain_core.messages import (
            AIMessage as LC_AI,
            BaseMessage as LC_Base,
            HumanMessage as LC_Human,
            RemoveMessage as LC_Remove,
            SystemMessage as LC_Sys,
            ToolMessage as LC_Tool,
        )
        assert BaseMessage is LC_Base
        assert HumanMessage is LC_Human
        assert AIMessage is LC_AI
        assert SystemMessage is LC_Sys
        assert ToolMessage is LC_Tool
        assert RemoveMessage is LC_Remove

    def test_langgraph_primitives_match(self):
        from langgraph.graph import END as LG_END, StateGraph as LG_SG, add_messages as LG_add
        assert END is LG_END
        assert StateGraph is LG_SG
        assert add_messages is LG_add

    def test_tool_decorator_matches(self):
        from langchain_core.tools import tool as LC_tool
        assert tool is LC_tool

    def test_type_hints_match(self):
        from langchain_core.callbacks import BaseCallbackHandler as LC_CB
        from langchain_core.language_models.chat_models import BaseChatModel as LC_BCM
        from langchain_core.outputs import LLMResult as LC_LR
        assert BaseCallbackHandler is LC_CB
        assert BaseChatModel is LC_BCM
        assert LLMResult is LC_LR

    def test_messages_construct_correctly(self):
        assert HumanMessage(content="hi").content == "hi"
        assert AIMessage(content="hi").content == "hi"
        assert SystemMessage(content="hi").content == "hi"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Message coercion
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCoerceMessages:

    def test_passthrough_base_messages(self):
        msgs = [HumanMessage(content="a"), AIMessage(content="b")]
        out = _coerce_messages(msgs)
        assert out == msgs

    def test_tuple_system(self):
        out = _coerce_messages([("system", "rules")])
        assert len(out) == 1 and isinstance(out[0], SystemMessage)
        assert out[0].content == "rules"

    def test_tuple_user_and_assistant(self):
        out = _coerce_messages([("user", "hi"), ("assistant", "hello")])
        assert isinstance(out[0], HumanMessage) and out[0].content == "hi"
        assert isinstance(out[1], AIMessage) and out[1].content == "hello"

    def test_tuple_human_and_ai_aliases(self):
        out = _coerce_messages([("human", "q"), ("ai", "a")])
        assert isinstance(out[0], HumanMessage)
        assert isinstance(out[1], AIMessage)

    def test_dict_message(self):
        out = _coerce_messages([{"role": "user", "content": "ping"}])
        assert isinstance(out[0], HumanMessage) and out[0].content == "ping"

    def test_mixed_formats(self):
        out = _coerce_messages([
            ("system", "you are helpful"),
            {"role": "user", "content": "q"},
            AIMessage(content="a"),
        ])
        assert isinstance(out[0], SystemMessage)
        assert isinstance(out[1], HumanMessage)
        assert isinstance(out[2], AIMessage)

    def test_rejects_unknown_role(self):
        with pytest.raises(ValueError):
            _coerce_messages([("sidekick", "x")])

    def test_rejects_unknown_type(self):
        with pytest.raises(TypeError):
            _coerce_messages([42])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  _message_text helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMessageText:

    def test_string_content(self):
        assert _message_text(AIMessage(content="hello")) == "hello"

    def test_list_content_with_text_blocks(self):
        msg = MagicMock()
        msg.content = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
        assert _message_text(msg) == "AB"

    def test_list_content_with_strings(self):
        msg = MagicMock()
        msg.content = ["hello ", "world"]
        assert _message_text(msg) == "hello world"

    def test_none_content(self):
        msg = MagicMock()
        msg.content = None
        assert _message_text(msg) == ""

    def test_no_content_attribute(self):
        assert _message_text(object()) == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  invoke_chat
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestInvokeChat:

    def test_returns_empty_when_no_llm(self):
        """When no provider is configured the adapter must degrade
        gracefully to an empty string (rule-based fallback signal)."""
        with patch("backend.agents.llm.get_llm", return_value=None):
            out = invoke_chat([("user", "hi")])
        assert out == ""

    def test_calls_llm_and_returns_text(self):
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = AIMessage(content="the answer")
        out = invoke_chat([("system", "s"), ("user", "q")], llm=fake_llm)
        assert out == "the answer"
        fake_llm.invoke.assert_called_once()
        args, _ = fake_llm.invoke.call_args
        # Messages should have been coerced to BaseMessage objects.
        assert all(isinstance(m, BaseMessage) for m in args[0])

    def test_passes_explicit_llm_through(self):
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = AIMessage(content="x")
        out = invoke_chat([HumanMessage(content="q")], llm=fake_llm)
        assert out == "x"

    def test_uses_get_llm_when_no_explicit_llm(self):
        fake_llm = MagicMock()
        fake_llm.invoke.return_value = AIMessage(content="via get_llm")
        with patch("backend.agents.llm.get_llm", return_value=fake_llm) as p:
            out = invoke_chat([("user", "q")], provider="openai", model="gpt-4o")
        assert out == "via get_llm"
        p.assert_called_once_with(provider="openai", model="gpt-4o", bind_tools=None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  stream_chat
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestStreamChat:

    @pytest.mark.asyncio
    async def test_empty_stream_without_llm(self):
        chunks = []
        with patch("backend.agents.llm.get_llm", return_value=None):
            async for c in stream_chat([("user", "q")]):
                chunks.append(c)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_yields_chunks_in_order(self):
        async def fake_astream(_msgs):
            for text in ["Hel", "lo ", "world"]:
                yield AIMessage(content=text)

        fake_llm = MagicMock()
        fake_llm.astream = fake_astream
        chunks = []
        async for c in stream_chat([("user", "q")], llm=fake_llm):
            chunks.append(c)
        assert chunks == ["Hel", "lo ", "world"]

    @pytest.mark.asyncio
    async def test_skips_empty_chunks(self):
        async def fake_astream(_msgs):
            yield AIMessage(content="")
            yield AIMessage(content="x")

        fake_llm = MagicMock()
        fake_llm.astream = fake_astream
        chunks = []
        async for c in stream_chat([("user", "q")], llm=fake_llm):
            chunks.append(c)
        assert chunks == ["x"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  tool_call
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestToolCall:

    def test_no_llm_returns_empty_response(self):
        with patch("backend.agents.llm.get_llm", return_value=None):
            resp = tool_call([("user", "q")], tools=[])
        assert isinstance(resp, AdapterToolResponse)
        assert resp.text == ""
        assert resp.tool_calls == []

    def test_parses_dict_tool_calls(self):
        fake_resp = AIMessage(content="ok")
        fake_resp.tool_calls = [  # type: ignore[attr-defined]
            {"name": "read_file", "args": {"path": "/a"}, "id": "c1"},
        ]
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = fake_llm
        fake_llm.invoke.return_value = fake_resp
        resp = tool_call([("user", "q")], tools=[object()], llm=fake_llm)
        assert resp.text == "ok"
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert isinstance(tc, AdapterToolCall)
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "/a"}
        assert tc.call_id == "c1"

    def test_parses_attribute_tool_calls(self):
        fake_tc = MagicMock()
        fake_tc.name = "run_bash"
        fake_tc.args = {"command": "ls"}
        fake_tc.id = "c2"
        fake_resp = AIMessage(content="")
        fake_resp.tool_calls = [fake_tc]  # type: ignore[attr-defined]
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = fake_llm
        fake_llm.invoke.return_value = fake_resp
        resp = tool_call([("user", "q")], tools=[object()], llm=fake_llm)
        assert resp.tool_calls[0].name == "run_bash"
        assert resp.tool_calls[0].arguments == {"command": "ls"}

    def test_no_tool_calls(self):
        fake_resp = AIMessage(content="direct answer")
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = fake_llm
        fake_llm.invoke.return_value = fake_resp
        resp = tool_call([("user", "q")], tools=[object()], llm=fake_llm)
        assert resp.text == "direct answer"
        assert resp.tool_calls == []

    def test_binds_tools_on_llm(self):
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = fake_llm
        fake_llm.invoke.return_value = AIMessage(content="")
        tools_arg = [object(), object()]
        tool_call([("user", "q")], tools=tools_arg, llm=fake_llm)
        fake_llm.bind_tools.assert_called_once_with(tools_arg)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  embed
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEmbed:

    def test_empty_input_returns_empty(self):
        assert embed([]) == []

    def test_openai_no_key_returns_empty(self, monkeypatch):
        """With no OPENAI_API_KEY, embed() must degrade to []
        rather than raising — callers are expected to treat empty
        vectors as a soft-disable signal."""
        from backend import config
        monkeypatch.setattr(config.settings, "openai_api_key", "", raising=False)
        assert embed(["hello"]) == []

    def test_openai_with_key_uses_embeddings(self, monkeypatch):
        from backend import config
        monkeypatch.setattr(config.settings, "openai_api_key", "sk-fake", raising=False)

        fake_emb = MagicMock()
        fake_emb.embed_documents.return_value = [[0.1, 0.2], [0.3, 0.4]]
        fake_emb_cls = MagicMock(return_value=fake_emb)

        import langchain_openai
        monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", fake_emb_cls, raising=True)
        out = embed(["a", "b"], provider="openai", model="text-embedding-3-small")
        assert out == [[0.1, 0.2], [0.3, 0.4]]
        fake_emb.embed_documents.assert_called_once_with(["a", "b"])

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError):
            embed(["x"], provider="nonexistent-provider")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  build_chat_model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildChatModel:

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            build_chat_model(provider="nonexistent")

    def test_anthropic_builds(self, monkeypatch):
        fake_cls = MagicMock(return_value=MagicMock(spec=BaseChatModel))
        import langchain_anthropic
        monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", fake_cls, raising=True)
        build_chat_model("anthropic", model="claude-sonnet-4", api_key="sk-a")
        fake_cls.assert_called_once()
        kwargs = fake_cls.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4"
        assert kwargs["anthropic_api_key"] == "sk-a"

    def test_openai_family_uses_ChatOpenAI(self, monkeypatch):
        fake_cls = MagicMock(return_value=MagicMock(spec=BaseChatModel))
        import langchain_openai
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_cls, raising=True)
        for p in ("openai", "xai", "deepseek", "openrouter"):
            fake_cls.reset_mock()
            build_chat_model(p, api_key="k")
            fake_cls.assert_called_once()
            kwargs = fake_cls.call_args.kwargs
            assert kwargs["api_key"] == "k"
            # xai/deepseek/openrouter must get their base_url plugged in
            if p != "openai":
                assert "base_url" in kwargs

    def test_openrouter_default_headers(self, monkeypatch):
        fake_cls = MagicMock(return_value=MagicMock(spec=BaseChatModel))
        import langchain_openai
        monkeypatch.setattr(langchain_openai, "ChatOpenAI", fake_cls, raising=True)
        build_chat_model(
            "openrouter", api_key="k",
            default_headers={"HTTP-Referer": "https://foo", "X-Title": "Bar"},
        )
        kwargs = fake_cls.call_args.kwargs
        assert kwargs["default_headers"] == {
            "HTTP-Referer": "https://foo", "X-Title": "Bar",
        }

    # ── Z.6.2: ollama bind_tools path ──────────────────────────────

    def test_ollama_builds(self, monkeypatch):
        """ChatOllama is constructed with model + temperature; base_url
        is forwarded when provided."""
        import langchain_ollama
        fake_instance = MagicMock()
        fake_cls = MagicMock(return_value=fake_instance)
        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_cls, raising=True)
        build_chat_model("ollama", model="qwen2.5", base_url="http://gpu-box:11434")
        fake_cls.assert_called_once()
        kw = fake_cls.call_args.kwargs
        assert kw["model"] == "qwen2.5"
        assert kw["base_url"] == "http://gpu-box:11434"

    def test_ollama_bind_tools_forwarded(self, monkeypatch):
        """Z.6.2: build_chat_model("ollama", bind_tools=...) must call
        bind_tools on the ChatOllama instance and return the bound model —
        same path as the other seven providers."""
        import langchain_ollama
        fake_instance = MagicMock()
        bound_model = MagicMock()
        fake_instance.bind_tools.return_value = bound_model
        fake_cls = MagicMock(return_value=fake_instance)
        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_cls, raising=True)
        tools = [object(), object()]
        result = build_chat_model("ollama", bind_tools=tools)
        fake_instance.bind_tools.assert_called_once_with(tools)
        assert result is bound_model

    def test_bind_tools_not_called_when_none(self, monkeypatch):
        """When bind_tools is not passed, the raw model is returned and
        bind_tools() is never invoked (get_llm() handles that separately)."""
        import langchain_ollama
        fake_instance = MagicMock()
        fake_cls = MagicMock(return_value=fake_instance)
        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_cls, raising=True)
        result = build_chat_model("ollama")
        fake_instance.bind_tools.assert_not_called()
        assert result is fake_instance

    def test_bind_tools_common_step_applies_to_non_ollama(self, monkeypatch):
        """The common bind_tools step applies uniformly to all providers,
        not only to ollama — confirms the shared adapter tool_call() flow."""
        import langchain_anthropic
        fake_instance = MagicMock()
        bound_model = MagicMock()
        fake_instance.bind_tools.return_value = bound_model
        fake_cls = MagicMock(return_value=fake_instance)
        monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", fake_cls, raising=True)
        tools = [object()]
        result = build_chat_model("anthropic", api_key="sk-x", bind_tools=tools)
        fake_instance.bind_tools.assert_called_once_with(tools)
        assert result is bound_model


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Firewall CI script — detection + clean repo check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIREWALL_SCRIPT = _REPO_ROOT / "scripts" / "check_llm_adapter_firewall.py"


class TestFirewallScript:

    def test_script_exists(self):
        assert _FIREWALL_SCRIPT.is_file(), (
            f"Firewall script missing at {_FIREWALL_SCRIPT}"
        )

    def test_repo_is_currently_clean(self):
        """Running the firewall against the real repo must pass.
        If this test fails after a merge, someone introduced a new
        direct langchain import somewhere in backend/."""
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(_REPO_ROOT)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, (
            f"Firewall check failed:\nSTDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    def test_detects_violation(self, tmp_path):
        """Seed a fake repo root with a violating file and confirm
        the script returns exit code 1."""
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "llm_adapter.py").write_text("")
        (tmp_path / "backend" / "bad.py").write_text(
            "from langchain_core.messages import HumanMessage\n"
        )
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1
        assert "bad.py" in result.stdout

    def test_detects_langgraph_violation(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "llm_adapter.py").write_text("")
        (tmp_path / "backend" / "bad.py").write_text(
            "import langgraph.graph\n"
        )
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1

    def test_missing_backend_dir_errors(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2

    def test_missing_adapter_errors(self, tmp_path):
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "ok.py").write_text("x = 1\n")
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2

    def test_skips_vendored_dirs(self, tmp_path):
        """Files under __pycache__/.venv/site-packages must not
        trigger violations — those aren't first-party source."""
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "llm_adapter.py").write_text("")
        venv_dir = tmp_path / "backend" / ".venv" / "lib" / "python3.12" / "site-packages"
        venv_dir.mkdir(parents=True)
        (venv_dir / "dep.py").write_text("from langchain_core.messages import HumanMessage\n")
        result = subprocess.run(
            [sys.executable, str(_FIREWALL_SCRIPT), "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Adapter dogfood: make sure callers that rely on it still work
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCallerIntegration:

    def test_state_module_uses_adapter_symbols(self):
        """`backend.agents.state.GraphState` must accept our re-
        exported BaseMessage — the whole graph pipeline relies on
        this equivalence."""
        from backend.agents.state import GraphState
        state = GraphState(user_command="hello", messages=[HumanMessage(content="a")])
        assert len(state.messages) == 1
        assert isinstance(state.messages[0], BaseMessage)

    def test_graph_module_imports_cleanly(self):
        # Bare import is sufficient — any stray direct-import would
        # have made the earlier firewall test fail, and any broken
        # re-export would fail here.
        import backend.agents.graph  # noqa: F401

    def test_tool_decorator_from_adapter_is_usable(self):
        @tool
        def dummy_tool(x: str) -> str:
            """echo x."""
            return x
        assert dummy_tool.name == "dummy_tool"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API surface — fail loudly if someone removes a symbol
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPublicAPISurface:

    def test_all_listed_symbols_are_exported(self):
        for name in llm_adapter.__all__:
            assert hasattr(llm_adapter, name), f"__all__ lists {name} but it is missing"

    def test_core_interface_present(self):
        # The task spec mandates these four entry points:
        # invoke_chat, stream_chat, embed, tool_call.
        for name in ("invoke_chat", "stream_chat", "embed", "tool_call"):
            assert callable(getattr(llm_adapter, name))

"""Z.6.5 — Ollama graceful tool-call fallback tests.

Covers three failure scenarios that trigger the fallback path in
``backend.llm_adapter.tool_call()``:

  1. ``daemon_error``   — ChatOllama.invoke() raises (daemon unreachable).
  2. ``unsupported``    — ChatOllama.invoke() raises with "not support" text.
  3. ``parse_error``    — invoke succeeds but the tool_calls block is malformed.

Each scenario must:
  - Return an ``AdapterToolResponse`` (not raise).
  - Populate the ``SharedKV("ollama_tool_failures")`` counter.
  - Log a warning (not an error).
  - Degrade gracefully to a pure-chat text reply when possible.

Module-global state audit: SharedKV uses in-memory fallback when Redis is
absent (dev / CI), so counter values are per-test-process — which is exactly
what we want for unit test isolation.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from backend.llm_adapter import (
    AdapterToolResponse,
    _is_ollama_model,
    _ollama_tool_call_fallback,
    tool_call,
)
from backend.shared_state import SharedKV


# ─── helpers ──────────────────────────────────────────────────────────────────


def _fresh_kv(namespace: str = "ollama_tool_failures") -> SharedKV:
    """Return a SharedKV for *namespace* with its in-memory store cleared.

    Since SharedKV uses class-level in-memory storage (so that multiple
    instances of the same namespace share state), we clear the namespace
    bucket directly to ensure test isolation.
    """
    kv = SharedKV(namespace)
    kv._local = {}  # reset via the @_local.setter
    return kv


class _FakeChatOllama:
    """Minimal stand-in for langchain_ollama.ChatOllama."""
    __class__ = type("ChatOllama", (), {})  # makes type(x).__name__ == "ChatOllama"

    def __init__(self, *, raises=None, text="chat reply", tool_calls_raw=None):
        self._raises = raises
        self._text = text
        self._tool_calls_raw = tool_calls_raw

    def invoke(self, _msgs):
        if self._raises is not None:
            raise self._raises
        msg = MagicMock()
        msg.content = self._text
        msg.tool_calls = self._tool_calls_raw or []
        return msg

    def bind_tools(self, _tools):
        bound = MagicMock()
        bound.__class__ = type("RunnableBinding", (), {})
        bound.bound = self
        bound.invoke = self.invoke
        return bound


# ─── _is_ollama_model ─────────────────────────────────────────────────────────


def test_is_ollama_model_via_provider_string():
    dummy = MagicMock()
    assert _is_ollama_model("ollama", None, dummy) is True


def test_is_ollama_model_via_provider_string_case_insensitive():
    dummy = MagicMock()
    assert _is_ollama_model("OLLAMA", None, dummy) is True


def test_is_ollama_model_via_llm_class_name():
    class ChatOllama:
        pass
    assert _is_ollama_model(None, ChatOllama(), MagicMock()) is True


def test_is_ollama_model_via_resolved_bound():
    class ChatOllama:
        pass
    class RunnableBinding:
        bound = ChatOllama()
    assert _is_ollama_model(None, None, RunnableBinding()) is True


def test_is_ollama_model_false_for_openai():
    class ChatOpenAI:
        pass
    assert _is_ollama_model("openai", None, ChatOpenAI()) is False


def test_is_ollama_model_false_for_unknown_class():
    assert _is_ollama_model(None, None, MagicMock()) is False


# ─── SharedKV.incr ────────────────────────────────────────────────────────────


def test_sharedkv_incr_basic():
    kv = _fresh_kv("test_incr_z65")
    assert kv.incr("total") == 1
    assert kv.incr("total") == 2
    assert kv.incr("daemon_error", 3) == 3
    assert int(kv.get("total")) == 2
    assert int(kv.get("daemon_error")) == 3


def test_sharedkv_incr_independent_fields():
    kv = _fresh_kv("test_incr_fields")
    kv.incr("a")
    kv.incr("b")
    kv.incr("b")
    assert int(kv.get("a")) == 1
    assert int(kv.get("b")) == 2


# ─── _ollama_tool_call_fallback ───────────────────────────────────────────────


def test_fallback_increments_counter_and_returns_response():
    from langchain_core.messages import HumanMessage
    # Use _fresh_kv to reset the shared namespace bucket.
    kv = _fresh_kv()

    bare_llm = MagicMock()
    bare_llm.invoke.return_value = MagicMock(content="pure chat reply")

    resp = _ollama_tool_call_fallback(
        [HumanMessage(content="hello")],
        exc=ConnectionRefusedError("daemon offline"),
        failure_type="daemon_error",
        provider="ollama",
        model="llama3.1",
        original_llm=bare_llm,
    )

    assert isinstance(resp, AdapterToolResponse)
    assert resp.tool_calls == []
    assert resp.text == "pure chat reply"
    # Re-read via the same class-level namespace.
    kv2 = SharedKV("ollama_tool_failures")
    assert int(kv2.get("total")) == 1
    assert int(kv2.get("daemon_error")) == 1


def test_fallback_logs_warning(caplog):
    from langchain_core.messages import HumanMessage
    _fresh_kv()  # reset namespace

    bare_llm = MagicMock()
    bare_llm.invoke.return_value = MagicMock(content="fallback text")

    with caplog.at_level(logging.WARNING, logger="backend.llm_adapter"):
        _ollama_tool_call_fallback(
            [HumanMessage(content="hi")],
            exc=RuntimeError("parse failed"),
            failure_type="parse_error",
            provider="ollama",
            model=None,
            original_llm=bare_llm,
        )

    assert any("ollama tool_call fallback" in r.message for r in caplog.records)
    assert any("parse_error" in r.message for r in caplog.records)


def test_fallback_returns_empty_when_bare_chat_also_fails():
    from langchain_core.messages import HumanMessage
    _fresh_kv()  # reset namespace

    bare_llm = MagicMock()
    bare_llm.invoke.side_effect = RuntimeError("bare chat also broke")

    resp = _ollama_tool_call_fallback(
        [HumanMessage(content="hi")],
        exc=RuntimeError("tool call broke"),
        failure_type="daemon_error",
        provider="ollama",
        model=None,
        original_llm=bare_llm,
    )

    assert resp.text == ""
    assert resp.tool_calls == []
    # Counter still incremented despite bare-chat failure.
    kv = SharedKV("ollama_tool_failures")
    assert int(kv.get("total")) == 1


# ─── tool_call() integration with ollama fallback ────────────────────────────


def _make_tool():
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def dummy_tool(query: str) -> str:
        """A dummy tool."""
        return f"result: {query}"

    return dummy_tool


@pytest.fixture(autouse=True)
def _reset_kv():
    """Reset the SharedKV("ollama_tool_failures") class-level store before/after each test."""
    _fresh_kv()  # clears the namespace bucket
    yield
    _fresh_kv()  # clean up after test


def test_tool_call_ollama_daemon_error_falls_back(monkeypatch):
    """ChatOllama.invoke raises ConnectionError → fallback, counter incremented."""
    from langchain_core.messages import HumanMessage
    tool = _make_tool()

    ollama = _FakeChatOllama(raises=ConnectionError("daemon offline"), text="chat reply")
    bound = ollama.bind_tools([tool])

    # Patch _resolve_chat_model to return the bound mock for tool path
    # and the bare mock for fallback pure-chat path.
    call_count = {"n": 0}

    def _mock_resolve(provider, model, bind_tools_list, llm):
        call_count["n"] += 1
        if bind_tools_list:
            return bound
        return ollama  # bare for fallback

    monkeypatch.setattr("backend.llm_adapter._resolve_chat_model", _mock_resolve)

    resp = tool_call(
        [HumanMessage(content="what is the weather?")],
        [tool],
        provider="ollama",
        model="llama3.1",
    )

    assert isinstance(resp, AdapterToolResponse)
    assert resp.tool_calls == []
    # Counter incremented
    kv = SharedKV("ollama_tool_failures")
    assert int(kv.get("total")) >= 1
    assert int(kv.get("daemon_error")) >= 1


def test_tool_call_ollama_unsupported_falls_back(monkeypatch):
    """ChatOllama.invoke raises with 'not support' → unsupported counter."""
    from langchain_core.messages import HumanMessage
    tool = _make_tool()

    ollama = _FakeChatOllama(
        raises=ValueError("model does not support tool calling"),
        text="chat reply",
    )
    bound = ollama.bind_tools([tool])

    def _mock_resolve(provider, model, bind_tools_list, llm):
        if bind_tools_list:
            return bound
        return ollama

    monkeypatch.setattr("backend.llm_adapter._resolve_chat_model", _mock_resolve)

    resp = tool_call(
        [HumanMessage(content="search for X")],
        [tool],
        provider="ollama",
        model="some-model",
    )

    assert isinstance(resp, AdapterToolResponse)
    assert resp.tool_calls == []
    kv = SharedKV("ollama_tool_failures")
    assert int(kv.get("unsupported")) >= 1
    assert int(kv.get("total")) >= 1


def test_tool_call_ollama_parse_error_falls_back(monkeypatch):
    """tool_calls block raises during parsing → parse_error counter."""
    from langchain_core.messages import HumanMessage
    tool = _make_tool()

    # Return a response where tool_calls is iterable but blows up mid-iteration.
    class _BrokenIterable:
        def __iter__(self):
            yield {"name": "dummy_tool", "args": {}}
            raise ValueError("malformed JSON in tool_call block")

    msg = MagicMock()
    msg.content = "chat text"
    msg.tool_calls = _BrokenIterable()

    ollama = MagicMock()
    ollama.invoke.return_value = msg

    class ChatOllama:
        pass
    ollama.__class__ = ChatOllama

    def _mock_resolve(provider, model, bind_tools_list, llm):
        if bind_tools_list:
            bound = MagicMock()
            bound.invoke.return_value = msg
            bound.bound = ollama
            return bound
        return ollama

    monkeypatch.setattr("backend.llm_adapter._resolve_chat_model", _mock_resolve)

    resp = tool_call(
        [HumanMessage(content="run tool")],
        [tool],
        provider="ollama",
        model="llama3.1",
    )

    assert isinstance(resp, AdapterToolResponse)
    # Parse failed so tool_calls may be partial (from the first yield) or empty.
    kv = SharedKV("ollama_tool_failures")
    assert int(kv.get("parse_error")) >= 1
    assert int(kv.get("total")) >= 1


def test_tool_call_non_ollama_reraises(monkeypatch):
    """Non-Ollama exceptions are NOT swallowed — they propagate normally."""
    from langchain_core.messages import HumanMessage
    tool = _make_tool()

    class ChatOpenAI:
        pass

    openai_llm = MagicMock()
    openai_llm.__class__ = ChatOpenAI
    openai_llm.invoke.side_effect = RuntimeError("openai rate limit")

    def _mock_resolve(provider, model, bind_tools_list, llm):
        bound = MagicMock()
        bound.invoke.side_effect = RuntimeError("openai rate limit")
        bound.bound = openai_llm
        return bound

    monkeypatch.setattr("backend.llm_adapter._resolve_chat_model", _mock_resolve)

    with pytest.raises(RuntimeError, match="openai rate limit"):
        tool_call(
            [HumanMessage(content="hi")],
            [tool],
            provider="openai",
            model="gpt-4o",
        )

    # No counter incremented for non-ollama failures.
    kv = SharedKV("ollama_tool_failures")
    assert int(kv.get("total", "0")) == 0

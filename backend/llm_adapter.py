"""N4 — LangChain/LangGraph firewall adapter.

This is the **only** module in `backend/` that is permitted to import
from `langchain*` or `langgraph*`.  Every other module must import the
symbols it needs from `backend.llm_adapter` instead.  A CI check
(`scripts/check_llm_adapter_firewall.py`) enforces this on every push.

The goal is to decouple the rest of the codebase from LangChain's
volatile surface area: when LangChain (or LangGraph) ships a breaking
change, only this file needs to be updated.

──────────────────────────────────────────────────────────────────
Stable public interface
──────────────────────────────────────────────────────────────────

Message primitives (re-exported):
    BaseMessage, HumanMessage, AIMessage, SystemMessage,
    ToolMessage, RemoveMessage

LangGraph primitives (re-exported):
    StateGraph, END, add_messages

Tool decorator (re-exported):
    tool

Type hints (re-exported for callers that need them):
    BaseChatModel, BaseCallbackHandler, LLMResult

High-level adapter functions (the actual firewall — stable across
LangChain upgrades):
    invoke_chat(messages, ...)   → str
    stream_chat(messages, ...)   → AsyncIterator[str]
    embed(texts, ...)            → list[list[float]]
    tool_call(messages, tools, ...) → AdapterToolResponse

Provider factory (re-exported for the provider-matrix builder in
`backend.agents.llm`; callers that need *just* a configured chat
model should prefer `invoke_chat` / `tool_call` instead):
    build_chat_model(provider, model, **kwargs) → BaseChatModel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Sequence

# ─── LangChain / LangGraph imports — this is the ONLY place they live ───
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from langgraph.graph import END, StateGraph, add_messages

logger = logging.getLogger(__name__)


__all__ = [
    # ── Message primitives ──
    "BaseMessage",
    "HumanMessage",
    "AIMessage",
    "SystemMessage",
    "ToolMessage",
    "RemoveMessage",
    # ── LangGraph primitives ──
    "StateGraph",
    "END",
    "add_messages",
    # ── Tool decorator ──
    "tool",
    # ── Type hints ──
    "BaseChatModel",
    "BaseCallbackHandler",
    "LLMResult",
    "ChatGeneration",
    # ── Stable interface ──
    "invoke_chat",
    "stream_chat",
    "embed",
    "tool_call",
    "stream_tool_call",
    "build_chat_model",
    "AdapterToolResponse",
    "AdapterToolCall",
]


# ──────────────────────────────────────────────────────────────────
#  Provider factory — lazy-imports the provider SDK so an environment
#  missing one provider's extras doesn't fail to import the adapter.
# ──────────────────────────────────────────────────────────────────


def build_chat_model(
    provider: str,
    model: str | None = None,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    max_retries: int = 3,
    api_key: str | None = None,
    base_url: str | None = None,
    default_headers: dict | None = None,
    bind_tools: list | None = None,
) -> BaseChatModel:
    """Construct a configured chat model for the given provider.

    The exact LangChain class used per provider is an implementation
    detail of this adapter — callers must not depend on it.

    bind_tools: Optional list of tools (LangChain ``@tool`` functions,
        Pydantic schemas, or OpenAI function-spec dicts) to bind before
        returning.  Callers that bind tools after construction (e.g.
        ``get_llm()``) should leave this ``None``.  With
        ``langchain-ollama >= 0.2`` (locked in Z.6.1), the ollama branch
        supports ``bind_tools`` on the same path as the other seven
        providers (Z.6.2).

    Raises:
        ValueError: if the provider is unknown.
        ImportError: if the provider's extras package is not installed.
    """
    p = provider.lower()
    llm: BaseChatModel

    if p == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs: dict[str, Any] = {
            "model": model or "claude-sonnet-4-20250514",
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["anthropic_api_key"] = api_key
        llm = ChatAnthropic(**kwargs)

    elif p == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        kwargs = {
            "model": model or "gemini-1.5-pro",
            "temperature": temperature,
        }
        if api_key:
            kwargs["google_api_key"] = api_key
        llm = ChatGoogleGenerativeAI(**kwargs)

    elif p in ("openai", "xai", "deepseek", "openrouter"):
        from langchain_openai import ChatOpenAI
        defaults_by_provider = {
            "openai": ("gpt-4o", None),
            "xai": ("grok-3-mini", "https://api.x.ai/v1"),
            "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
            "openrouter": ("anthropic/claude-sonnet-4", "https://openrouter.ai/api/v1"),
        }
        default_model, default_base_url = defaults_by_provider[p]
        kwargs = {
            "model": model or default_model,
            "temperature": temperature,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["api_key"] = api_key
        effective_base_url = base_url or default_base_url
        if effective_base_url:
            kwargs["base_url"] = effective_base_url
        if default_headers:
            kwargs["default_headers"] = default_headers
        llm = ChatOpenAI(**kwargs)

    elif p == "groq":
        from langchain_groq import ChatGroq
        kwargs = {
            "model": model or "llama-3.3-70b-versatile",
            "temperature": temperature,
            "max_retries": max_retries,
        }
        if api_key:
            kwargs["groq_api_key"] = api_key
        llm = ChatGroq(**kwargs)

    elif p == "together":
        from langchain_together import ChatTogether
        kwargs = {
            "model": model or "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "temperature": temperature,
        }
        if api_key:
            kwargs["together_api_key"] = api_key
        llm = ChatTogether(**kwargs)

    elif p == "ollama":
        from langchain_ollama import ChatOllama
        kwargs = {
            "model": model or "llama3.1",
            "temperature": temperature,
        }
        if base_url:
            kwargs["base_url"] = base_url
        # Z.6.2: do NOT set format="json" — it conflicts with tool calling.
        # ChatOllama.bind_tools() is supported as of langchain-ollama >= 0.2
        # (locked in Z.6.1) for models that advertise function-calling
        # capability (llama3.1/3.2, qwen2.5, qwen3, mistral-nemo, command-r,
        # mixtral).  The common bind_tools step below handles this branch on
        # the same path as the other seven providers.
        llm = ChatOllama(**kwargs)

    else:
        raise ValueError(f"Unknown provider: {provider!r}")

    # Z.6.2: common bind_tools step — applies to ollama on the same path as
    # the other seven providers.  get_llm() leaves this None and calls
    # llm.bind_tools() separately; direct factory callers can pass tools here
    # to receive a ready-to-invoke bound model in one call.
    # Module-global audit: bind_tools is a parameter; no module state touched.
    if bind_tools:
        llm = llm.bind_tools(bind_tools)
    return llm


# ──────────────────────────────────────────────────────────────────
#  Internal helpers
# ──────────────────────────────────────────────────────────────────


def _coerce_messages(messages: Sequence[Any]) -> list[BaseMessage]:
    """Accept either LangChain BaseMessage objects or (role, content)
    tuples / dicts, and return canonical BaseMessage list.

    This is the main wire-format compatibility shim — callers that
    don't want to import message classes can pass
    ``[("system", "..."), ("user", "...")]`` instead.
    """
    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            out.append(m)
            continue
        if isinstance(m, tuple) and len(m) == 2:
            role, content = m
            role_l = str(role).lower()
            if role_l in ("system", "sys"):
                out.append(SystemMessage(content=str(content)))
            elif role_l in ("user", "human"):
                out.append(HumanMessage(content=str(content)))
            elif role_l in ("assistant", "ai"):
                out.append(AIMessage(content=str(content)))
            else:
                raise ValueError(f"Unknown role in tuple message: {role!r}")
            continue
        if isinstance(m, dict) and "role" in m and "content" in m:
            out.extend(_coerce_messages([(m["role"], m["content"])]))
            continue
        raise TypeError(
            f"Cannot coerce object of type {type(m).__name__} into a BaseMessage"
        )
    return out


def _resolve_chat_model(
    provider: str | None,
    model: str | None,
    bind_tools: list | None,
    llm: BaseChatModel | None,
) -> BaseChatModel | None:
    """Either use the caller-supplied llm, or look it up via the
    high-level `backend.agents.llm.get_llm()` factory (which handles
    settings defaults, per-tenant circuit breakers, and failover).
    """
    if llm is not None:
        if bind_tools:
            llm = llm.bind_tools(bind_tools)
        return llm
    # Lazy import to avoid a circular import at module load time —
    # backend.agents.llm itself imports from this adapter module.
    from backend.agents.llm import get_llm
    return get_llm(provider=provider, model=model, bind_tools=bind_tools)


# ──────────────────────────────────────────────────────────────────
#  Tool-call response shape (stable across LangChain versions)
# ──────────────────────────────────────────────────────────────────


@dataclass
class AdapterToolCall:
    """A provider-agnostic tool invocation request."""
    name: str
    arguments: dict = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class AdapterToolResponse:
    """What `tool_call()` returns.

    `text`: assistant's natural-language text reply (may be empty if
             the model chose to reply exclusively via tool calls).
    `tool_calls`: list of tool invocations the model requested.
    `raw_message`: the underlying LangChain AIMessage (callers that
             need callback/id metadata can reach through here).
    """
    text: str
    tool_calls: list[AdapterToolCall]
    raw_message: BaseMessage | None = None


# ──────────────────────────────────────────────────────────────────
#  Stable public interface
# ──────────────────────────────────────────────────────────────────


def invoke_chat(
    messages: Sequence[Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    llm: BaseChatModel | None = None,
) -> str:
    """Run a single synchronous chat turn and return the text reply.

    Returns an empty string if no LLM provider is available (graceful
    degradation — callers should treat "" as "fall back to rule-based
    logic").  Any exception raised by the provider propagates so the
    caller can classify / retry.
    """
    chat = _resolve_chat_model(provider, model, None, llm)
    if chat is None:
        return ""
    msgs = _coerce_messages(messages)
    resp = chat.invoke(msgs)
    return _message_text(resp)


async def stream_chat(
    messages: Sequence[Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    llm: BaseChatModel | None = None,
) -> AsyncIterator[str]:
    """Stream text chunks from the provider.

    Yields one string per chunk (text fragments in arrival order).
    If the LLM isn't configured, the async iterator yields nothing
    and returns immediately.
    """
    chat = _resolve_chat_model(provider, model, None, llm)
    if chat is None:
        return
    msgs = _coerce_messages(messages)
    # `astream` returns an async iterator of message *chunks* (AIMessageChunk).
    async for chunk in chat.astream(msgs):
        text = _message_text(chunk)
        if text:
            yield text


def embed(
    texts: Iterable[str],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[list[float]]:
    """Embed a list of texts using the specified provider.

    N4 ships with minimal embedding support — only `openai` and
    `ollama` are wired up because those are the only backends the
    project currently has credentials for in practice.  Additional
    providers can be added here without touching any caller.

    Returns a list of vectors (one per input) or [] if the provider
    isn't configured.
    """
    text_list = [str(t) for t in texts]
    if not text_list:
        return []

    p = (provider or "openai").lower()

    if p == "openai":
        try:
            from backend.config import settings
            from langchain_openai import OpenAIEmbeddings
        except ImportError as exc:
            logger.warning("OpenAIEmbeddings unavailable: %s", exc)
            return []
        key = getattr(settings, "openai_api_key", None)
        if not key:
            logger.info("embed(openai): no OMNISIGHT_OPENAI_API_KEY set")
            return []
        emb = OpenAIEmbeddings(
            model=model or "text-embedding-3-small",
            api_key=key,
        )
        return emb.embed_documents(text_list)

    if p == "ollama":
        try:
            from backend.config import settings
            from langchain_ollama import OllamaEmbeddings
        except ImportError as exc:
            logger.warning("OllamaEmbeddings unavailable: %s", exc)
            return []
        emb = OllamaEmbeddings(
            model=model or "nomic-embed-text",
            base_url=getattr(settings, "ollama_base_url", "http://localhost:11434"),
        )
        return emb.embed_documents(text_list)

    raise ValueError(f"embed(): provider {provider!r} not supported")


def _is_ollama_model(
    provider: str | None,
    llm: BaseChatModel | None,
    resolved: BaseChatModel,
) -> bool:
    """Return True if the resolved model is a ChatOllama instance.

    Checks the provider string first; falls back to the class name of
    the resolved model (which may be a RunnableBinding wrapping ChatOllama
    after bind_tools() is applied — unwrap via .bound).
    """
    if provider and provider.lower() == "ollama":
        return True
    if llm is not None and type(llm).__name__ == "ChatOllama":
        return True
    # Unwrap a single level of RunnableBinding (added by bind_tools)
    target = getattr(resolved, "bound", resolved)
    return type(target).__name__ == "ChatOllama"


def _ollama_tool_call_fallback(
    msgs: list[BaseMessage],
    exc: Exception,
    failure_type: str,
    provider: str | None,
    model: str | None,
    original_llm: BaseChatModel | None,
) -> AdapterToolResponse:
    """Handle an Ollama tool-call failure: count it, warn, degrade to pure chat.

    failure_type: "daemon_error" | "parse_error" | "unsupported"
    Never raises — returns an empty-tool-calls response so callers can
    continue with plain text output.
    """
    from backend.shared_state import SharedKV  # lazy to avoid circular import
    _kv = SharedKV("ollama_tool_failures")
    _kv.incr(failure_type)
    _kv.incr("total")
    logger.warning(
        "ollama tool_call fallback (%s): %s — degrading to pure chat",
        failure_type,
        exc,
    )
    # Attempt pure-chat fallback (no tools bound) so the caller gets
    # at least a text reply instead of silence.
    try:
        # Re-use the original llm (unbound) or resolve a fresh one.
        bare: BaseChatModel | None = original_llm
        if bare is None:
            bare = _resolve_chat_model(provider, model, None, None)
        if bare is not None:
            resp = bare.invoke(msgs)
            return AdapterToolResponse(
                text=_message_text(resp),
                tool_calls=[],
                raw_message=resp if isinstance(resp, BaseMessage) else None,
            )
    except Exception as chat_exc:  # noqa: BLE001
        logger.warning("ollama pure-chat fallback also failed: %s", chat_exc)
    return AdapterToolResponse(text="", tool_calls=[], raw_message=None)


def tool_call(
    messages: Sequence[Any],
    tools: list,
    *,
    provider: str | None = None,
    model: str | None = None,
    llm: BaseChatModel | None = None,
) -> AdapterToolResponse:
    """Invoke the chat model with tools bound, returning a normalized
    tool-call response.

    Callers can use either:
      * LangChain `@tool`-decorated functions (re-exported as
        `llm_adapter.tool`), or
      * any object accepted by LangChain's `bind_tools(...)` (OpenAI
        function specs, Pydantic schemas, etc.).

    Z.6.5 — Ollama graceful fallback: if the Ollama daemon raises an
    exception (daemon_error), or the response tool_calls block cannot be
    parsed (parse_error), the adapter degrades to a pure-chat response
    (no tool calls), increments SharedKV("ollama_tool_failures") counters
    for dashboard observability, and returns without raising.
    """
    chat = _resolve_chat_model(provider, model, tools, llm)
    if chat is None:
        return AdapterToolResponse(text="", tool_calls=[], raw_message=None)
    msgs = _coerce_messages(messages)

    # Z.6.5: detect ollama before the invoke so we can route fallback
    is_ollama = _is_ollama_model(provider, llm, chat)

    try:
        resp = chat.invoke(msgs)
    except Exception as exc:  # noqa: BLE001
        if is_ollama:
            # Classify: connection errors are daemon_error; others may be
            # "unsupported" (e.g. Ollama 400 on unsupported tool schema).
            failure_type = "daemon_error"
            exc_msg = str(exc).lower()
            if any(kw in exc_msg for kw in ("not support", "unsupported", "400", "invalid tool")):
                failure_type = "unsupported"
            return _ollama_tool_call_fallback(msgs, exc, failure_type, provider, model, llm)
        raise

    calls: list[AdapterToolCall] = []
    raw_tc = getattr(resp, "tool_calls", None) or []
    try:
        for tc in raw_tc:
            if isinstance(tc, dict):
                calls.append(AdapterToolCall(
                    name=tc.get("name", ""),
                    arguments=tc.get("args") or tc.get("arguments") or {},
                    call_id=tc.get("id"),
                ))
            else:
                calls.append(AdapterToolCall(
                    name=getattr(tc, "name", ""),
                    arguments=getattr(tc, "args", None) or getattr(tc, "arguments", {}) or {},
                    call_id=getattr(tc, "id", None),
                ))
    except Exception as exc:  # noqa: BLE001
        if is_ollama:
            return _ollama_tool_call_fallback(msgs, exc, "parse_error", provider, model, llm)
        raise

    return AdapterToolResponse(
        text=_message_text(resp),
        tool_calls=calls,
        raw_message=resp if isinstance(resp, BaseMessage) else None,
    )


async def stream_tool_call(
    messages: Sequence[Any],
    tools: list,
    *,
    provider: str | None = None,
    model: str | None = None,
    llm: BaseChatModel | None = None,
) -> AdapterToolResponse:
    """Stream a tool-calling request and return the accumulated result.

    Uses ``astream`` internally so the request travels through the provider's
    streaming path.  Accumulates all ``AIMessageChunk`` objects with the ``+``
    operator and extracts tool_calls from the final chunk.

    This is the streaming analogue of ``tool_call()``.  The difference is the
    wire path — streaming delivery of tool calls is an important correctness
    invariant: some providers send tool-call deltas across multiple chunks,
    requiring proper accumulation.

    Provider notes (Z.7.5):
    - Anthropic (claude-*): streaming + function calling fully supported.
    - OpenAI (gpt-4o*): streaming + function calling fully supported.
    - Gemini (gemini-1.5-*): streaming + function calling supported since
      gemini-1.5-flash/pro.  Earlier models (gemini-pro, gemini-1.0-*) raise
      a google.api_core.exceptions.InvalidArgument or NotImplementedError —
      callers should catch these and call ``pytest.skip(reason=...)`` in tests.

    Module-global state: none — each call resolves its own chat model
    instance (per-worker independent, no cross-worker coordination needed).
    """
    chat = _resolve_chat_model(provider, model, tools, llm)
    if chat is None:
        return AdapterToolResponse(text="", tool_calls=[], raw_message=None)
    msgs = _coerce_messages(messages)

    accumulated = None
    async for chunk in chat.astream(msgs):
        accumulated = chunk if accumulated is None else accumulated + chunk

    if accumulated is None:
        return AdapterToolResponse(text="", tool_calls=[], raw_message=None)

    calls: list[AdapterToolCall] = []
    raw_tc = getattr(accumulated, "tool_calls", None) or []
    for tc in raw_tc:
        if isinstance(tc, dict):
            calls.append(AdapterToolCall(
                name=tc.get("name", ""),
                arguments=tc.get("args") or tc.get("arguments") or {},
                call_id=tc.get("id"),
            ))
        else:
            calls.append(AdapterToolCall(
                name=getattr(tc, "name", ""),
                arguments=getattr(tc, "args", None) or getattr(tc, "arguments", {}) or {},
                call_id=getattr(tc, "id", None),
            ))

    return AdapterToolResponse(
        text=_message_text(accumulated),
        tool_calls=calls,
        raw_message=accumulated if isinstance(accumulated, BaseMessage) else None,
    )


def _message_text(msg: Any) -> str:
    """Extract plain text from a LangChain message / chunk."""
    content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Some providers return a list of content blocks — concatenate the
    # text blocks.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)

"""Multi-provider LLM factory.

Supports: Anthropic (default), Google, OpenAI, xAI, Groq, DeepSeek, Together, Ollama.

Usage:
    from backend.agents.llm import get_llm
    llm = get_llm()                    # uses configured default provider
    llm = get_llm("openai")           # override provider
    llm = get_llm("groq", "mixtral-8x7b-32768")  # override provider + model
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel

from backend.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Cache to avoid re-creating LLM instances
_cache: dict[str, BaseChatModel] = {}


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    bind_tools: list | None = None,
) -> BaseChatModel | None:
    """Create or retrieve a cached LLM instance.

    Args:
        provider: Override the configured provider.
        model: Override the model name.
        bind_tools: Optional list of LangChain tools to bind.

    Returns:
        A LangChain chat model, or None if the provider can't be initialized.
    """
    provider = provider or settings.llm_provider
    model = model or (settings.get_model_name() if provider == settings.llm_provider else None)

    cache_key = f"{provider}:{model}:{id(bind_tools) if bind_tools else 'none'}"
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        llm = _create_llm(provider, model)
        if llm is None:
            return None
        if bind_tools:
            llm = llm.bind_tools(bind_tools)
        _cache[cache_key] = llm
        logger.info("LLM initialized: provider=%s model=%s", provider, model or "(default)")
        return llm
    except Exception as exc:
        logger.warning("Failed to init LLM [%s]: %s", provider, exc)
        return None


def _create_llm(provider: str, model: str | None) -> BaseChatModel | None:
    """Instantiate the appropriate LangChain chat model."""
    temp = settings.llm_temperature

    if provider == "anthropic":
        key = settings.anthropic_api_key
        if not key:
            logger.info("No OMNISIGHT_ANTHROPIC_API_KEY set")
            return None
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model or "claude-sonnet-4-20250514",
            anthropic_api_key=key,
            temperature=temp,
            max_tokens=4096,
        )

    if provider == "google":
        key = settings.google_api_key
        if not key:
            logger.info("No OMNISIGHT_GOOGLE_API_KEY set")
            return None
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model or "gemini-1.5-pro",
            google_api_key=key,
            temperature=temp,
        )

    if provider == "openai":
        key = settings.openai_api_key
        if not key:
            logger.info("No OMNISIGHT_OPENAI_API_KEY set")
            return None
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model or "gpt-4o",
            api_key=key,
            temperature=temp,
        )

    if provider == "xai":
        key = settings.xai_api_key
        if not key:
            logger.info("No OMNISIGHT_XAI_API_KEY set")
            return None
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model or "grok-3-mini",
            api_key=key,
            base_url="https://api.x.ai/v1",
            temperature=temp,
        )

    if provider == "groq":
        key = settings.groq_api_key
        if not key:
            logger.info("No OMNISIGHT_GROQ_API_KEY set")
            return None
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=model or "llama-3.3-70b-versatile",
            groq_api_key=key,
            temperature=temp,
        )

    if provider == "deepseek":
        key = settings.deepseek_api_key
        if not key:
            logger.info("No OMNISIGHT_DEEPSEEK_API_KEY set")
            return None
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model or "deepseek-chat",
            api_key=key,
            base_url="https://api.deepseek.com",
            temperature=temp,
        )

    if provider == "together":
        key = settings.together_api_key
        if not key:
            logger.info("No OMNISIGHT_TOGETHER_API_KEY set")
            return None
        from langchain_together import ChatTogether
        return ChatTogether(
            model=model or "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            together_api_key=key,
            temperature=temp,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=model or "llama3.1",
            base_url=settings.ollama_base_url,
            temperature=temp,
        )

    logger.warning("Unknown LLM provider: %s", provider)
    return None


def list_providers() -> list[dict]:
    """Return metadata about all supported providers."""
    providers = [
        {
            "id": "anthropic",
            "name": "Anthropic",
            "default_model": "claude-sonnet-4-20250514",
            "models": [
                "claude-opus-4-20250514",
                "claude-sonnet-4-20250514",
                "claude-haiku-4-20250506",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_ANTHROPIC_API_KEY",
            "configured": bool(settings.anthropic_api_key),
        },
        {
            "id": "google",
            "name": "Google Gemini",
            "default_model": "gemini-1.5-pro",
            "models": [
                "gemini-1.5-pro",
                "gemini-1.5-flash",
                "gemini-2.5-pro-preview-05-06",
                "gemini-2.5-flash-preview-04-17",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_GOOGLE_API_KEY",
            "configured": bool(settings.google_api_key),
        },
        {
            "id": "openai",
            "name": "OpenAI",
            "default_model": "gpt-4o",
            "models": [
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4-turbo",
                "o3-mini",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_OPENAI_API_KEY",
            "configured": bool(settings.openai_api_key),
        },
        {
            "id": "xai",
            "name": "xAI (Grok)",
            "default_model": "grok-3-mini",
            "models": [
                "grok-3",
                "grok-3-mini",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_XAI_API_KEY",
            "configured": bool(settings.xai_api_key),
        },
        {
            "id": "groq",
            "name": "Groq",
            "default_model": "llama-3.3-70b-versatile",
            "models": [
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "mixtral-8x7b-32768",
                "gemma2-9b-it",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_GROQ_API_KEY",
            "configured": bool(settings.groq_api_key),
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "default_model": "deepseek-chat",
            "models": [
                "deepseek-chat",
                "deepseek-reasoner",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_DEEPSEEK_API_KEY",
            "configured": bool(settings.deepseek_api_key),
        },
        {
            "id": "together",
            "name": "Together.ai",
            "default_model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "models": [
                "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
                "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
                "mistralai/Mixtral-8x7B-Instruct-v0.1",
                "Qwen/Qwen2.5-72B-Instruct-Turbo",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_TOGETHER_API_KEY",
            "configured": bool(settings.together_api_key),
        },
        {
            "id": "ollama",
            "name": "Ollama (Local)",
            "default_model": "llama3.1",
            "models": [
                "llama3.1",
                "llama3.2",
                "qwen2.5",
                "mistral",
                "codellama",
                "deepseek-r1",
            ],
            "requires_key": False,
            "env_var": None,
            "configured": True,  # always available if Ollama is running
            "base_url": settings.ollama_base_url,
        },
    ]
    return providers

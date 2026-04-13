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
import time
from typing import TYPE_CHECKING

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import LLMResult

from backend.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TokenTrackingCallback(BaseCallbackHandler):
    """LangChain callback that feeds token usage into the system tracker."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._start: float = 0

    def on_llm_start(self, *args, **kwargs) -> None:  # noqa: ANN002
        self._start = time.time()

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:  # noqa: ANN003
        try:
            from backend.routers.system import track_tokens

            latency_ms = int((time.time() - self._start) * 1000)
            usage: dict = {}
            if response.llm_output:
                usage = response.llm_output.get("token_usage", {})
                if not usage:
                    usage = response.llm_output.get("usage", {})
            track_tokens(
                self.model_name,
                usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
                usage.get("completion_tokens", 0) or usage.get("output_tokens", 0),
                latency_ms,
            )
        except Exception as exc:
            logger.warning("Token tracking failed for %s: %s", self.model_name, exc)

# Cache to avoid re-creating LLM instances
_cache: dict[str, BaseChatModel] = {}
_provider_failures: dict[str, float] = {}  # provider → last_failure_timestamp
PROVIDER_COOLDOWN = 300  # 5 minutes — don't retry a failed provider within this window


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
    # Check token freeze — return None to trigger rule-based fallback
    from backend.routers import system as _sys_mod
    if _sys_mod.token_frozen:
        logger.info("Token budget frozen — LLM disabled, using rule-based fallback")
        return None

    provider = provider or settings.llm_provider
    model = model or (settings.get_model_name() if provider == settings.llm_provider else None)

    cache_key = f"{provider}:{model}:{id(bind_tools) if bind_tools else 'none'}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        llm = _create_llm(provider, model)

        # Failover: if primary fails, try fallback chain with cooldown
        if llm is None:
            chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
            for fallback_provider in chain:
                if fallback_provider == provider:
                    continue  # Skip the one that already failed
                # Circuit breaker: skip providers that failed recently
                last_fail = _provider_failures.get(fallback_provider, 0)
                if time.time() - last_fail < PROVIDER_COOLDOWN:
                    logger.debug("Skipping %s (cooldown, failed %ds ago)", fallback_provider, int(time.time() - last_fail))
                    continue
                try:
                    llm = _create_llm(fallback_provider, None)
                except Exception:
                    _provider_failures[fallback_provider] = time.time()
                    continue
                if llm is not None:
                    provider = fallback_provider
                    model = None
                    logger.info("Failover: %s → %s", settings.llm_provider, fallback_provider)
                    break
                else:
                    _provider_failures[fallback_provider] = time.time()
            if llm is None:
                from backend.events import emit_token_warning
                emit_token_warning("all_providers_failed", "All LLM providers failed. Using rule-based fallback.")
                return None

        # Inject token tracking callback (graceful if provider doesn't support it)
        model_name = model or (llm.model_name if hasattr(llm, "model_name") else f"{provider}:default")
        try:
            llm = llm.with_config(callbacks=[TokenTrackingCallback(model_name)])
        except (AttributeError, NotImplementedError):
            logger.warning("Provider %s does not support with_config — token tracking disabled", provider)
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

    if provider == "openrouter":
        key = settings.openrouter_api_key
        if not key:
            logger.info("No OMNISIGHT_OPENROUTER_API_KEY set")
            return None
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model or "anthropic/claude-sonnet-4",
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            temperature=temp,
            default_headers={
                "HTTP-Referer": "https://omnisight.local",
                "X-Title": "OmniSight Productizer",
            },
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
            "id": "openrouter",
            "name": "OpenRouter",
            "default_model": "anthropic/claude-sonnet-4",
            "models": [
                # Anthropic (via OpenRouter)
                "anthropic/claude-sonnet-4",
                "anthropic/claude-haiku-4",
                # OpenAI (via OpenRouter)
                "openai/gpt-4o",
                "openai/gpt-4o-mini",
                # Google (via OpenRouter)
                "google/gemini-2.5-flash-preview",
                "google/gemini-2.5-pro-preview",
                # OpenRouter exclusive — not available via direct providers
                "qwen/qwen3-235b-a22b",
                "qwen/qwen3-32b",
                "cohere/command-r-plus",
                "cohere/command-a",
                "mistralai/mistral-large",
                "mistralai/codestral",
                "meta-llama/llama-4-maverick",
                "meta-llama/llama-4-scout",
                "nvidia/llama-3.1-nemotron-ultra-253b",
                "perplexity/sonar-pro",
            ],
            "requires_key": True,
            "env_var": "OMNISIGHT_OPENROUTER_API_KEY",
            "configured": bool(settings.openrouter_api_key),
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


def validate_model_spec(model_spec: str) -> dict:
    """Validate a model spec and check if the provider has an API key configured.

    Args:
        model_spec: Model spec like "openrouter:qwen/qwen3-235b" or "claude-sonnet-4"

    Returns:
        {"valid": True/False, "provider": str, "model": str, "configured": bool, "warning": str}
    """
    if not model_spec:
        return {"valid": True, "provider": "", "model": "", "configured": True, "warning": ""}

    # Parse provider:model format
    if ":" in model_spec:
        provider, _, model = model_spec.partition(":")
        provider = provider.strip()
        model = model.strip()
    else:
        # Plain model name — check which provider it belongs to
        provider = ""
        model = model_spec
        for p in list_providers():
            if model in p.get("models", []) or model == p.get("default_model"):
                provider = p["id"]
                break

    if not provider:
        # No provider identified — will use global default, which is fine
        return {"valid": True, "provider": settings.llm_provider, "model": model, "configured": True, "warning": ""}

    # Check if provider is known
    providers_map = {p["id"]: p for p in list_providers()}
    if provider not in providers_map:
        return {"valid": False, "provider": provider, "model": model, "configured": False,
                "warning": f"Unknown provider: {provider}"}

    # Check if provider has API key
    p_info = providers_map[provider]
    if p_info.get("requires_key") and not p_info.get("configured"):
        return {"valid": False, "provider": provider, "model": model, "configured": False,
                "warning": f"Provider '{p_info['name']}' requires an API key but none is configured. "
                           f"Set {p_info.get('env_var', '')} in .env or enter it in Settings."}

    return {"valid": True, "provider": provider, "model": model, "configured": True, "warning": ""}
